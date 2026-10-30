#!/usr/bin/env python3
"""Offline conversation catch-up for SOTA and direct ChatGPT account launches.

The source is NEVER passed to a writer. This module deliberately does not call
run_sync, the three-way engine, duplicate cleanup, or backup retention. Only
history files in the destination profile are installed, from a validated sparse
staging area. Each invocation has one read-only source and one destination;
--to-sota returns account-side changes without rewriting authentication or
configuration. A durable journal makes an interrupted install recoverable.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import tomllib
import uuid

import codex_app_lifecycle as lifecycle
import sync_codex_histories as core

STATE = "state_5.sqlite"
HISTORY = "thread_history_1.sqlite"
GLOBAL = ".codex-global-state.json"
INDEX = "session_index.jsonl"
LEDGER = ".sota-account-history-sync.json"
RETURN_LEDGER = ".account-sota-history-sync.json"
SCHEMA = 1
BRANCH_SUFFIX = "（SOTA 分支）"
RETURN_BRANCH_SUFFIX = "（GPT 账号分支）"
UI_FIELDS = (
    "title", "name", "archived", "archived_at", "is_pinned", "project_id",
    "thread_section_id", "section_position", "section_entered_at_ms",
)
THREAD_MAP_KEYS = (
    "thread-project-assignments", "thread-project-membership-host-ids",
    "thread-workspace-root-hints", "thread-projectless-output-directories",
    "thread-writable-roots",
)
SHARED_UI_KEYS = (
    "local-projects", "project-order", "project-appearances",
    "sidebar-project-thread-orders", "app-server-project-id-by-legacy-project-id-by-host",
    "app-server-projects-migration-by-host", "app-server-migrated-pinned-thread-ids-by-host",
    "unified-sidebar-project-order-v1", "unified-sidebar-pinned-order-v1",
)
REFERENCE_TABLES = ("projects", "project_roots", "project_idempotency_keys", "thread_sections")
CACHE_TABLES = ("thread_items", "thread_turns", "thread_realtime_items", "thread_history_projection_state")


class AccountSyncError(RuntimeError):
    pass


class HistoryUnavailable(AccountSyncError):
    """A source head/page is absent; an existing account copy may be kept."""


def safe_path(path: Path, root: Path) -> Path:
    """Reject traversal, reparse points and hardlinks, including ancestor aliases."""
    path, root = core.canonical_storage_path(path), core.canonical_storage_path(root)
    if ".." in path.parts or not path.is_relative_to(root):
        raise AccountSyncError(f"History path escaped its profile: {path}")
    for node in (path, *path.parents):
        if node.exists() or node.is_symlink():
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AccountSyncError(f"Linked history paths are not supported: {node}")
            if node == path and node.is_file() and info.st_nlink > 1:
                raise AccountSyncError(f"Hardlinked history files are not supported: {node}")
    return path


def history_relative(value: str | Path) -> Path:
    rel = Path(value)
    allowed = {GLOBAL, INDEX, LEDGER, RETURN_LEDGER, STATE, HISTORY,
               STATE + "-wal", STATE + "-shm", HISTORY + "-wal", HISTORY + "-shm"}
    if (rel.is_absolute() or ".." in rel.parts or not rel.parts or ":" in str(rel)
            or not (str(rel) in allowed or
                    (rel.parts[0] in core.SESSION_STORAGE_DIRS and
                     (rel.name.endswith(".jsonl") or rel.name.endswith(".jsonl.zst"))))):
        raise AccountSyncError(f"Not an account history write target: {rel}")
    return rel


def stamp(path: Path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    value = core.read_json_retry(path)
    if not isinstance(value, dict):
        raise AccountSyncError(f"Expected a JSON object: {path}")
    return value


def read_index(path: Path) -> dict:
    """Do not silently drop damaged or unknown account index entries."""
    result = {}
    if path.exists():
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict) or not item.get("id"):
                    raise AccountSyncError(f"Invalid account session index: {path}")
                result[str(item["id"])] = item
    return result


def tree_state(root: Path) -> dict:
    """A bounded inventory: never visit secrets, logs, plugins or config trees."""
    # safe_path expands Windows 8.3 names; relative keys need the same root.
    root = core.canonical_storage_path(root)
    result = {}
    for name in (STATE, HISTORY, GLOBAL, INDEX, LEDGER, RETURN_LEDGER, STATE + "-wal", HISTORY + "-wal"):
        path = safe_path(root / name, root)
        if path.exists():
            result[name] = stamp(path)
    for storage in core.SESSION_STORAGE_DIRS:
        base = safe_path(root / storage, root)
        if not base.exists():
            continue
        for directory, dirs, names in os.walk(base, followlinks=False):
            for name in dirs:
                safe_path(Path(directory) / name, root)
            for name in names:
                if name.endswith((".jsonl", ".jsonl.zst")):
                    path = safe_path(Path(directory) / name, root)
                    result[str(path.relative_to(root))] = stamp(path)
    return result


def logical_rollout_path(path: Path) -> Path:
    """Codex keeps the logical .jsonl DB path after compressing it to .zst."""
    return path.with_suffix("") if path.name.endswith(".jsonl.zst") else path


def zstandard_module():
    try:
        import zstandard
        return zstandard
    except ModuleNotFoundError as error:
        if error.name != "zstandard":
            raise
    # Source installations may keep this one dependency private to the
    # synchronizer instead of altering Codex's shared Python environment.
    package = (Path(__file__).resolve().parent / ".account-history-deps" /
               sys.implementation.cache_tag / "zstandard" / "__init__.py")
    if not package.is_file():
        raise AccountSyncError("Compressed Codex history requires the bundled zstandard dependency")
    spec = importlib.util.spec_from_file_location("zstandard", package,
                                                submodule_search_locations=[str(package.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["zstandard"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("zstandard", None)
        raise
    return module


class TranscriptStore:
    """Read compressed rollouts without ever decompressing inside a profile.

    Inventories and ledger identities use logical/native paths. Only a page
    actually needed by the existing lineage copier is materialized, privately.
    Comparisons stream, and reject incomplete/truncated Zstandard frames even
    when the available output happens to end at a complete JSONL record.
    """
    def __init__(self, scratch):
        self.scratch = scratch
        self.physical = {}
        self.decoded = {}
        self.origins = {}
        self.logical_stamps = {}

    def logical(self, path):
        return self.origins.get(path, logical_rollout_path(path))

    def actual(self, path):
        logical = self.logical(path)
        return self.physical.get(logical, logical)

    def logical_stamp(self, path):
        actual = self.actual(path)
        current = stamp(actual)
        if not actual.name.endswith(".jsonl.zst"):
            return current
        key = (actual, *current)
        if key not in self.logical_stamps:
            # Native compression preserves the rollout's modification time.
            # Keep the old ledger's logical byte length too, so changing the
            # storage encoding is not mistaken for a user edit. Read to the
            # verified frame end; frame headers may omit the content size.
            size = sum(len(block) for block in self.blocks(path))
            if stamp(actual) != current:
                raise AccountSyncError("Compressed history changed while reading its logical identity")
            self.logical_stamps[key] = [size, current[1]]
        return self.logical_stamps[key]

    def blocks(self, path):
        actual = self.actual(path)
        with actual.open("rb") as stream:
            if not actual.name.endswith(".jsonl.zst"):
                while block := stream.read(64 * 1024):
                    core.history_checkpoint()
                    yield block
                return
            zstd = zstandard_module()
            decoder, complete_frames = None, 0
            try:
                while block := stream.read(16 * 1024):
                    core.history_checkpoint()
                    while block:
                        if decoder is None:
                            decoder = zstd.ZstdDecompressor().decompressobj()
                        data = decoder.decompress(block)
                        if data:
                            yield data
                        if decoder.eof:
                            block = decoder.unused_data
                            decoder = None
                            complete_frames += 1
                        else:
                            block = b""
            except zstd.ZstdError as error:
                raise AccountSyncError(f"Invalid compressed history: {actual}") from error
            if decoder is not None or not complete_frames:
                raise AccountSyncError(f"Truncated compressed history: {actual}")

    def lines(self, path):
        tail = b""
        with contextlib.closing(self.blocks(path)) as blocks:
            for block in blocks:
                records = (tail + block).split(b"\n")
                tail = records.pop()
                for record in records:
                    yield record + b"\n"
        if tail:
            yield tail

    def readable(self, path):
        logical, actual = self.logical(path), self.actual(path)
        if not actual.name.endswith(".jsonl.zst"):
            return actual
        if logical not in self.decoded:
            before = stamp(actual)
            directory = hashlib.sha256(str(logical).encode()).hexdigest()[:24]
            decoded = self.scratch / "decoded" / directory / logical.name
            decoded.parent.mkdir(parents=True, exist_ok=True)
            with decoded.open("xb") as stream:
                for block in self.blocks(logical):
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if stamp(actual) != before:
                raise AccountSyncError("Compressed history changed while preparing a private snapshot")
            self.decoded[logical] = decoded
            self.origins[decoded] = logical
        return self.decoded[logical]

    def retire_paths(self, path, root):
        logical = self.logical(path)
        # Retire both native representations with backups. Leaving an obsolete
        # compressed twin behind can make a superseded head reappear later.
        return {candidate.relative_to(root) for candidate in
                (logical, logical.with_name(logical.name + ".zst"))
                if safe_path(candidate, root).is_file()}


def account_inventory(root, store):
    files, metadata = {}, {}
    for storage in core.SESSION_STORAGE_DIRS:
        for path in sorted((root / storage).rglob("*")):
            if not path.name.endswith((".jsonl", ".jsonl.zst")) or not path.is_file():
                continue
            core.history_checkpoint()
            path = safe_path(path, root)
            logical = logical_rollout_path(path)
            # The uncompressed head is authoritative when both forms exist;
            # the compressed twin may predate a new native append.
            if path != logical and logical.is_file():
                continue
            store.physical[logical] = path
            with contextlib.closing(store.lines(logical)) as lines:
                try:
                    item = json.loads(next(lines, b"{}"))
                except ValueError:
                    continue
            meta = item.get("payload", {}) if isinstance(item, dict) else {}
            if not isinstance(meta, dict):
                continue
            metadata[logical] = meta
            page_id = core.physical_page_id(logical)
            files.setdefault(page_id, logical)
    return files, metadata


class ReadablePages(dict):
    """Adapt only the account copier's inventory; the shared engine is intact."""
    def __init__(self, files, metadata, store):
        super().__init__(files)
        self.metadata, self.store = metadata, store

    def get(self, key, default=None):
        path = super().get(key)
        if path is None:
            return default
        readable = self.store.readable(path)
        if readable != path:
            self.metadata[readable] = self.metadata[path]
        return readable


def sqlite_copy(source: Path, destination: Path) -> None:
    """Snapshot DB+WAL passively, then use SQLite only on that private copy.

    Even mode=ro SQLite connections update the source SHM read marks. Avoid
    that surprising write entirely: checkpoint/recovery and SQLite's backup
    API operate inside a disposable directory, never on either real profile.
    Offline guards plus before/after file identities reject a torn DB/WAL pair.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = (source, source.with_name(source.name + "-wal"))
    def identities():
        return [(path.stat().st_ino, *stamp(path)) if path.exists() else None for path in paths]
    with tempfile.TemporaryDirectory(prefix=".sqlite-read-", dir=destination.parent) as directory:
        private = Path(directory) / "snapshot.sqlite"
        for attempt in range(4):
            before = identities()
            core.copy_file_checkpointed(source, private)
            if before[1] is not None:
                core.copy_file_checkpointed(paths[1], private.with_name(private.name + "-wal"))
            if identities() == before:
                break
            private.with_name(private.name + "-wal").unlink(missing_ok=True)
            if attempt == 3:
                raise AccountSyncError(f"Database changed while taking a read-only snapshot: {source}")
        with contextlib.closing(sqlite3.connect(private, timeout=5)) as src:
            with contextlib.closing(sqlite3.connect(destination)) as dst:
                src.backup(dst, pages=256, sleep=0.05)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AccountSyncError(f"Invalid history database: {source}")
                dst.execute("PRAGMA journal_mode=DELETE")


def root_snapshot(root: Path, db_path: Path) -> core.RootSnapshot:
    with contextlib.closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        tables = {name: core.rows_as_dicts(db, name) for name in REFERENCE_TABLES if core.table_exists(db, name)}
        tools = {}
        for row in core.rows_as_dicts(db, "thread_dynamic_tools"):
            tools.setdefault(str(row["thread_id"]), []).append(row)
        return core.RootSnapshot(
            root=root,
            threads={str(row["id"]): row for row in core.rows_as_dicts(db, "threads")},
            thread_columns=core.table_columns(db, "threads"), dynamic_tools=tools,
            spawn_edges=core.rows_as_dicts(db, "thread_spawn_edges"),
            global_state=read_object(root / GLOBAL), session_index=read_index(root / INDEX),
            reference_tables=tables,
            reference_columns={name: core.table_columns(db, name) for name in tables},
        )


class HistoryView:
    """One inventory per profile, including sparse staged dependency overlays."""
    def __init__(self, inventory, store=None):
        self.files, self.metadata = inventory
        self.store = store
        self.digests = {}

    def signature(self, path: Path):
        chain, visiting = [], set()
        while True:
            if path in visiting:
                raise AccountSyncError("Cyclic paginated history")
            visiting.add(path)
            meta = self.metadata[path]
            base = meta.get("history_base")
            page = self.store.logical(path) if self.store else path
            identity = self.store.logical_stamp(path) if self.store else stamp(path)
            entry = [core.physical_page_id(page), *identity, base]
            chain.append(entry)
            if not base:
                return chain
            path = self.files.get(str(base.get("thread_id")))
            if path is None:
                raise HistoryUnavailable(f"Missing paginated history page: {base.get('thread_id')}")

    def matches_signature(self, path, baseline):
        current = self.signature(path)
        if current == baseline:
            return True
        if not self.store or not isinstance(baseline, list) or len(current) != len(baseline):
            return False
        for actual, previous in zip(current, baseline):
            if (not isinstance(previous, list) or len(previous) != 4 or
                    actual[0] != previous[0] or actual[2:] != previous[2:]):
                return False
            if actual[1] != previous[1] and not self.store.actual(path).name.endswith(".jsonl.zst"):
                return False
            base = self.metadata[path].get("history_base")
            if base:
                path = self.files.get(str(base.get("thread_id")))
        # The native migration reserializes old record schemas as it compresses
        # them, preserving their original mtime, page IDs and ancestry. Their
        # byte lengths can change without a new user turn. Do not echo these
        # pre-existing imports into a new conversation branch. Appends update
        # mtime and therefore must go through the content/conflict checks.
        return True

    def normalized(self, path: Path, owner: str):
        visiting = set()

        def read(page, cutoff=None):
            if page in visiting:
                raise AccountSyncError("Cyclic paginated history")
            visiting.add(page)
            meta = self.metadata[page]
            base = meta.get("history_base")
            if base:
                ancestor = self.files.get(str(base.get("thread_id")))
                end = base.get("end_ordinal_exclusive")
                if ancestor is None or not isinstance(end, int) or end < 0:
                    raise AccountSyncError("Invalid/missing paginated history ancestry")
                yield from read(ancestor, min(cutoff, end) if cutoff is not None else end)
            reader = (contextlib.closing(self.store.lines(page)) if self.store else page.open("rb"))
            with reader as stream:
                for line in stream:
                    core.history_checkpoint()
                    if not line.endswith(b"\n"):
                        raise AccountSyncError(f"Incomplete transcript: {page}")
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise AccountSyncError(f"Invalid transcript record: {page}")
                    if cutoff is not None and int(item.get("ordinal", 0)) >= cutoff:
                        break
                    if base and item.get("type") == "session_meta":
                        continue
                    yield core.normalize_session_self_references(item, str(meta.get("id") or owner))
            visiting.remove(page)

        for number, item in enumerate(read(path)):
            item.pop("ordinal", None)
            if number == 0:
                payload = item.get("payload")
                if item.get("type") != "session_meta" or not isinstance(payload, dict):
                    raise AccountSyncError(f"Invalid transcript header: {path}")
                payload.pop("model_provider", None)
                payload.pop("history_mode", None)
            item = core.canonicalize_rollout_models(item)
            item = core.normalize_session_self_references(item, owner)
            yield json_bytes(item)

    def digest(self, path, owner):
        return self.content_summary(path, owner)[0]

    def content_summary(self, path, owner):
        key = (owner, json.dumps(self.signature(path), sort_keys=True))
        if key in self.digests:
            return self.digests[key]
        result = hashlib.sha256()
        count = 0
        for line in self.normalized(path, owner):
            result.update(line)
            count += 1
        self.digests[key] = (result.hexdigest(), count)
        return self.digests[key]

    def extends_baseline(self, path, owner, entry, prefix):
        count = entry.get(prefix + "_record_count")
        digest = entry.get(prefix + "_content_digest")
        if not isinstance(count, int) or count < 1 or not digest:
            return False
        result = hashlib.sha256()
        with contextlib.closing(self.normalized(path, owner)) as records:
            for _ in range(count):
                line = next(records, None)
                if line is None:
                    return False
                result.update(line)
            return result.hexdigest() == digest and next(records, None) is not None


def relation(left: HistoryView, left_path: Path, left_id: str,
             right: HistoryView, right_path: Path, right_id: str) -> str:
    sentinel = object()
    for a, b in itertools.zip_longest(left.normalized(left_path, left_id),
                                    right.normalized(right_path, right_id), fillvalue=sentinel):
        if a is sentinel:
            return "source_prefix"
        if b is sentinel:
            return "target_prefix"
        if a != b:
            return "divergent"
    return "equal"


def head_for(root, row, view):
    path = safe_path(core.plain_storage_path(row["rollout_path"]), root)
    history_relative(path.relative_to(root))
    path = logical_rollout_path(path)
    actual = view.store.actual(path) if view.store else path
    if not actual.exists():
        raise HistoryUnavailable(f"Missing current transcript for {row['id']}")
    if path not in view.metadata or str(view.metadata[path].get("id")) != str(row["id"]):
        raise AccountSyncError(f"Missing or mismatched current transcript for {row['id']}")
    view.signature(path)
    return path


def remap_ui(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [remap_ui(item, mapping) for item in value]
    if isinstance(value, dict):
        return {mapping.get(key, key): remap_ui(item, mapping) for key, item in value.items()}
    return value


def additive(preferred, other):
    if isinstance(preferred, dict) and isinstance(other, dict):
        result = copy.deepcopy(preferred)
        for key, value in other.items():
            result[key] = additive(result[key], value) if key in result else copy.deepcopy(value)
        return result
    if isinstance(preferred, list) and isinstance(other, list):
        return core.merge_unique(preferred, other)
    return copy.deepcopy(preferred)


def remap_profile_hosts(value, source_root, target_root):
    """Recognize one local host across 8.3 spellings without following links."""
    source_key = core.storage_path_key(source_root)
    source_anchor = os.path.normcase(source_root.anchor)
    target_host = "local:" + str(core.canonical_storage_path(target_root))

    def host(text):
        if not isinstance(text, str) or not text.startswith("local:"):
            return text
        path = core.plain_storage_path(text[6:])
        if (path.is_absolute() and ".." not in path.parts
                and os.path.normcase(path.anchor) == source_anchor):
            try:
                if core.storage_path_key(path) == source_key:
                    return target_host
            except (OSError, ValueError):
                # Unknown/malformed host IDs are opaque sidebar metadata.
                pass
        return text

    def visit(item):
        if isinstance(item, str):
            return host(item)
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                key, child = host(key), visit(child)
                # Short and long host keys can coexist after a native migration.
                # Keep both project maps when they become one destination host.
                result[key] = additive(result[key], child) if key in result else child
            return result
        return item

    return visit(value)


def merge_sidebar(source, target, mapping, rows, previous):
    """Only sidebar/history fields; never copy an Atom object or account settings."""
    incoming = remap_ui(remap_profile_hosts(source.global_state, source.root, target.root), mapping)
    result = copy.deepcopy(target.global_state)
    for container_key in (None, core.ATOM_STATE_KEY):
        src = incoming if container_key is None else incoming.get(container_key, {})
        if not isinstance(src, dict):
            continue
        dst = result if container_key is None else result.setdefault(container_key, {})
        if not isinstance(dst, dict):
            raise AccountSyncError("Invalid account sidebar container")
        for key in SHARED_UI_KEYS:
            if key in src:
                dst[key] = additive(dst[key], src[key]) if key in dst else copy.deepcopy(src[key])
        for key in THREAD_MAP_KEYS:
            values = src.get(key, {})
            if not isinstance(values, dict):
                continue
            current = dst.setdefault(key, {})
            if not isinstance(current, dict):
                raise AccountSyncError(f"Invalid account sidebar field: {key}")
            for sid, did in mapping.items():
                if did not in values:
                    continue
                baseline = previous.get(sid, {}).get("sidebar", {}).get(str(container_key), {}).get(key, {})
                if did not in current or ("value" in baseline and current[did] == baseline["value"]):
                    current[did] = copy.deepcopy(values[did])
        # The database is authoritative for imported pins/archive state. Keep
        # account-only IDs, ordering and unknown future fields as they were.
        for key in ("projectless-thread-ids", "pinned-thread-ids"):
            existing = dst.get(key, [])
            if not isinstance(existing, list):
                raise AccountSyncError(f"Invalid account sidebar field: {key}")
            kept = [value for value in existing if value not in rows or
                    (not rows[value].get("archived") and
                     (key != "pinned-thread-ids" or rows[value].get("is_pinned")))]
            wanted = []
            for did, row in rows.items():
                if row.get("archived") or core.is_auxiliary_thread_source(row.get("source")):
                    continue
                if key == "pinned-thread-ids":
                    include = bool(row.get("is_pinned"))
                else:
                    include = not row.get("project_id") and did not in dst.get("thread-project-assignments", {})
                if include:
                    wanted.append(did)
            # Do not invent an obsolete nested sidebar representation.
            if container_key is None or key in src or key in dst:
                dst[key] = core.merge_unique(kept, wanted)
    return result


def sidebar_baseline(state, did):
    result = {}
    for container_key in (None, core.ATOM_STATE_KEY):
        container = state if container_key is None else state.get(container_key, {})
        result[str(container_key)] = {
            key: {"value": container[key][did]}
            for key in THREAD_MAP_KEYS if isinstance(container.get(key), dict) and did in container[key]
        }
    return result


def inbound_mirrors(source, target, return_to_sota):
    """Pair already-imported branches back to their original, not a new clone.

    This is only a read of the opposite direction's ledger. Its source/target
    identity must match exactly; no unrelated history/recovery plan is read.
    """
    ledger = read_object(source / (LEDGER if return_to_sota else RETURN_LEDGER))
    if not ledger:
        return {}
    if (ledger.get("schema") != SCHEMA or ledger.get("source") != str(target)
            or ledger.get("target") != str(source)):
        raise AccountSyncError("Inbound conversation ledger belongs to different profiles")
    mirrors = {}
    for original_id, entry in ledger.get("threads", {}).items():
        imported_id = entry.get("target_id")
        if not isinstance(imported_id, str) or imported_id in mirrors:
            raise AccountSyncError("Ambiguous inbound conversation identity")
        mirrors[imported_id] = {"original_id": original_id, "entry": entry}
    return mirrors


def source_ui(row):
    return {field: row.get(field) for field in UI_FIELDS}


def recover_pending(base: Path, target: Path, guard) -> bool:
    pending = base / "pending.json"
    if not pending.exists():
        return False
    journal = read_object(pending)
    if journal.get("target") != str(target) or journal.get("schema") != SCHEMA:
        raise AccountSyncError("Account history recovery journal does not match this profile")
    run = safe_path(base / str(journal.get("run", "")), base)
    if run.parent != base or not re.fullmatch(r"run-[a-f0-9]{16}", run.name):
        raise AccountSyncError("Invalid account history recovery directory")
    operations = journal.get("operations")
    if not isinstance(operations, list):
        raise AccountSyncError("Invalid account history recovery operations")
    # Verify every rollback target before touching any. An external edit made
    # after an interrupted switch must never be silently overwritten.
    for op in operations:
        rel = history_relative(op["path"])
        current = safe_path(target / rel, target)
        backup = safe_path(run / "before" / rel, run)
        current_hash = digest_file(current) if current.exists() else None
        if current_hash not in (op["before"], op["after"]):
            raise AccountSyncError(f"Account history changed after an interrupted sync; keep backup: {run}")
        if op["before"] is not None and (not backup.exists() or digest_file(backup) != op["before"]):
            raise AccountSyncError(f"Account history recovery backup failed verification: {run}")
    for op in reversed(operations):
        guard()
        rel = history_relative(op["path"])
        current = safe_path(target / rel, target)
        if op["before"] is not None:
            core.atomic_copy_file(run / "before" / rel, current)
        elif current.exists():
            saved = safe_path(run / "aborted" / rel, run)
            saved.parent.mkdir(parents=True, exist_ok=True)
            os.replace(current, saved)
    core.atomic_write_json(run / "recovery.json", {"status": "restored", "target": str(target)})
    pending.unlink()
    return True


def install_stage(base, run, stage, target, paths, retire, guard, rollback_guard):
    operations = []
    # Retire WAL/SHM before replacing a consolidated SQLite database. Retired
    # transcripts and all replaced files have verified, non-pruned backups.
    ordered = sorted(retire, key=str) + sorted(paths, key=lambda p: (p.name == LEDGER, str(p)))
    for rel in ordered:
        rel = history_relative(rel)
        dest = safe_path(target / rel, target)
        after_path = safe_path(stage / rel, stage) if rel in paths else None
        before = digest_file(dest) if dest.exists() else None
        after = digest_file(after_path) if after_path is not None else None
        if before == after:
            continue
        if before is not None:
            backup = safe_path(run / "before" / rel, run)
            core.atomic_copy_file(dest, backup)
            if digest_file(backup) != before:
                raise AccountSyncError("Account history backup verification failed")
        operations.append({"path": str(rel), "before": before, "after": after})
    if not operations:
        return 0
    guard()
    journal = {"schema": SCHEMA, "target": str(target), "run": run.name, "operations": operations}
    core.atomic_write_json(run / "journal.json", journal)
    core.atomic_write_json(base / "pending.json", journal)
    try:
        for op in operations:
            guard()
            rel = history_relative(op["path"])
            dest = safe_path(target / rel, target)
            if (digest_file(dest) if dest.exists() else None) != op["before"]:
                raise AccountSyncError("Account history changed before installation")
            if op["after"] is None:
                retired = safe_path(run / "retired" / rel, run)
                retired.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dest, retired)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / rel, dest)
            if (digest_file(dest) if dest.exists() else None) != op["after"]:
                raise AccountSyncError("Installed account history failed verification")
        guard()
        (base / "pending.json").unlink()
    except BaseException:
        # A launcher disappearing is different from a new app using history.
        # Keep our own history lease and roll back only while all apps are gone.
        recover_pending(base, target, rollback_guard)
        raise
    return len(operations)


def no_app_guard():
    if lifecycle.app_running():
        raise lifecycle.AppNotQuiescent("Codex is still using history; nothing may be installed")


def sync_account(source: Path, target: Path, work_base: Path, *,
                 guard=lifecycle.assert_quiescent, rollback_guard=no_app_guard,
                 dry_run=False, return_to_sota=False) -> dict:
    disposable_runs = []
    try:
        return _sync_account(source, target, work_base, guard=guard,
                             rollback_guard=rollback_guard, dry_run=dry_run,
                              disposable_runs=disposable_runs, return_to_sota=return_to_sota)
    finally:
        # Failed preflights must not accumulate gigabytes of disposable copies.
        # Pending recovery, originals and all older backups are always kept.
        for base, run in disposable_runs:
            if not (base / "pending.json").exists():
                for name in ("scratch", "stage"):
                    disposable = safe_path(run / name, run)
                    if disposable.is_dir():
                        with contextlib.suppress(OSError):
                            shutil.rmtree(disposable)


def _sync_account(source, target, work_base, *, guard, rollback_guard, dry_run, disposable_runs,
                  return_to_sota=False):
    started = time.monotonic()
    source, target = core.canonical_storage_path(source), core.canonical_storage_path(target)
    work_base = core.canonical_storage_path(work_base)
    for root in (source, target, work_base):
        safe_path(root, root)
    for left, right in ((source, target), (source, work_base), (target, work_base)):
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise AccountSyncError("Source, account and staging roots must not overlap")
    guard()
    if not (source / STATE).is_file() or not (target / STATE).is_file():
        raise AccountSyncError("Both profiles need an initialized Codex history database")
    key = hashlib.sha256(str(target).encode()).hexdigest()[:12]
    base = safe_path(work_base / key, work_base)
    base.mkdir(parents=True, exist_ok=True)
    if dry_run and (base / "pending.json").exists():
        raise AccountSyncError("Pending account recovery must be resolved before a dry run")
    if not dry_run:
        recover_pending(base, target, rollback_guard)
    run = base / ("run-" + uuid.uuid4().hex[:16])
    stage, scratch = run / "stage", run / "scratch"
    stage.mkdir(parents=True)
    disposable_runs.append((base, run))
    scratch.mkdir()
    before_source, before_target = tree_state(source), tree_state(target)
    ledger_name = RETURN_LEDGER if return_to_sota else LEDGER
    ledger = read_object(target / ledger_name)
    if ledger and (ledger.get("schema") != SCHEMA or ledger.get("source") != str(source)
                   or ledger.get("target") != str(target)):
        raise AccountSyncError("Account history sync ledger has an incompatible origin")
    previous = ledger.get("threads", {})
    mirrors = inbound_mirrors(source, target, return_to_sota)
    sqlite_copy(source / STATE, run / "source-state.sqlite")
    sqlite_copy(target / STATE, stage / STATE)
    src, dst = root_snapshot(source, run / "source-state.sqlite"), root_snapshot(target, stage / STATE)
    store = TranscriptStore(scratch)
    source_inventory, target_inventory = account_inventory(source, store), account_inventory(target, store)
    source_view, target_view = HistoryView(source_inventory, store), HistoryView(target_inventory, store)
    importer = core.LineageImporter((), scratch, set())
    for root, inventory in ((source, source_inventory), (stage, target_inventory)):
        metadata = dict(inventory[1])
        importer.inventories[root] = (ReadablePages(inventory[0], metadata, store), metadata)
    staged_view = HistoryView(importer.inventories[stage], store)
    heads, source_issues = {}, {}
    for sid, row in src.threads.items():
        try:
            heads[sid] = head_for(source, row, source_view)
        except HistoryUnavailable as error:
            source_issues[sid] = str(error)
    source_rows = src.threads
    if return_to_sota:
        # Most account rows were just copied out of SOTA. Do not import them
        # back, touch unrelated legacy damage, or create echo branches.
        source_rows = {}
        for sid, row in src.threads.items():
            path = heads.get(sid)
            signature = source_view.signature(path) if path else None
            ui, sidebar = source_ui(row), sidebar_baseline(src.global_state, sid)
            old = previous.get(sid, {})
            inbound = mirrors.get(sid, {}).get("entry", {})
            def same_content(entry, signature_key, digest_key):
                if path and source_view.matches_signature(path, entry.get(signature_key)):
                    return True
                return bool(path and entry.get(digest_key) and
                            source_view.digest(path, sid) == entry[digest_key])

            already_returned = (old.get("source_ui") == ui and old.get("source_sidebar") == sidebar)
            already_returned = already_returned and same_content(old, "source_signature", "source_content_digest")
            unchanged_import = (inbound.get("ui") == ui and inbound.get("sidebar") == sidebar)
            unchanged_import = unchanged_import and same_content(inbound, "target_signature", "target_content_digest")
            # Old ledgers predate content digests. Compression alone must not
            # return an unchanged import, nor roll back a longer original.
            if (not already_returned and not unchanged_import and path and inbound and
                    not inbound.get("target_content_digest") and inbound.get("ui") == ui and
                    inbound.get("sidebar") == sidebar):
                original_id = mirrors[sid]["original_id"]
                original = dst.threads.get(original_id)
                if original:
                    try:
                        original_path = head_for(target, original, target_view)
                        unchanged_import = relation(source_view, path, sid, target_view,
                                                    original_path, original_id) in ("equal", "source_prefix")
                    except HistoryUnavailable:
                        pass
            if not (already_returned or unchanged_import):
                source_rows[sid] = row
    target_heads = {} if return_to_sota else {
        sid: head_for(target, row, target_view) for sid, row in dst.threads.items()}

    def target_head(thread_id):
        if thread_id not in dst.threads:
            return None
        if thread_id not in target_heads:
            try:
                target_heads[thread_id] = head_for(target, dst.threads[thread_id], target_view)
            except HistoryUnavailable:
                if not return_to_sota:
                    raise
                # Never restore an old damaged SOTA row as a side effect. If an
                # account continuation needs importing, preserve it as a branch.
                target_heads[thread_id] = None
        return target_heads[thread_id]
    model_config = {}
    config_path = safe_path(target / "config.toml", target)
    if config_path.exists():
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
        model_config = {key: config[key] for key in ("model", "model_reasoning_effort", "model_provider") if key in config}
    destination_provider = model_config.get("model_provider") if return_to_sota else "openai"
    if return_to_sota and source_rows and (not isinstance(destination_provider, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", destination_provider) or not model_config.get("model")):
        raise AccountSyncError("SOTA return requires its existing configured provider and default model")
    branch_suffix = RETURN_BRANCH_SUFFIX if return_to_sota else BRANCH_SUFFIX
    identity_prefix = "codex-sota-return" if return_to_sota else "codex-account"
    storage_folder = "account-return-sync" if return_to_sota else "account-sync"
    mapping, selected, imports, retire, final_heads = {}, {}, {}, set(), {}
    counters = dict(new_threads=0, updated_threads=0, conflicts_preserved=0, account_ahead=0,
                    cached_threads=0, source_unavailable_kept_account=0)
    new_ledger = {"schema": SCHEMA, "source": str(source), "target": str(target),
                  "threads": copy.deepcopy(previous) if return_to_sota else {}}

    with core.history_checkpoint_scope(guard, None):
        for sid, source_row in sorted(source_rows.items()):
            core.history_checkpoint()
            old = previous.get(sid, {})
            mirror = mirrors.get(sid, {})
            did = old.get("target_id") or mirror.get("original_id", sid)
            if did not in dst.threads:
                did = sid
            existing = dst.threads.get(did)
            source_path = heads.get(sid)
            source_signature = source_view.signature(source_path) if source_path else None
            target_path = target_head(did)
            cached = bool(source_path and existing and target_path and old.get("target_id") == did and
                          source_view.matches_signature(source_path, old.get("source_signature")) and
                          target_view.matches_signature(target_path, old.get("target_signature")))
            if source_path is None:
                if not existing or not target_path:
                    raise HistoryUnavailable(f"Neither profile has complete history for {sid}; source was not repaired")
                if not (old.get("relation") == "source_unavailable" and
                        old.get("target_signature") == target_view.signature(target_path)):
                    # Read every available account record before using it as
                    # the fallback. Never reconstruct from caches or an old
                    # restoration manifest, and never write to the source.
                    for _ in target_view.normalized(target_path, did):
                        pass
                match = "source_unavailable"
                counters["source_unavailable_kept_account"] += 1
            elif cached:
                match = old["relation"]
                counters["cached_threads"] += 1
            else:
                match = ("divergent" if existing and not target_path else
                         relation(source_view, source_path, sid, target_view, target_path, did) if existing else "missing")
            if match == "divergent" and target_path:
                # Two native storage versions of an imported baseline can have
                # different serialized records. Adopt a proved append only if
                # the destination still matches its own baseline; independent
                # continuations on both sides always remain separate branches.
                baseline_pairs = [(old, "source", "target")]
                if did == mirror.get("original_id"):
                    baseline_pairs.append((mirror["entry"], "target", "source"))
                for baseline, incoming_key, destination_key in baseline_pairs:
                    if (target_view.matches_signature(target_path, baseline.get(destination_key + "_signature")) and
                            source_view.extends_baseline(source_path, sid, baseline, incoming_key)):
                        match = "target_prefix"
                        break
            if match == "divergent":
                content_key = source_view.digest(source_path, sid)
                serial = 0
                while True:
                    did = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{identity_prefix}-branch:{sid}:{content_key}:{serial}"))
                    existing, target_path = dst.threads.get(did), target_head(did)
                    if existing is None:
                        match = "missing"
                        break
                    if target_path is None:
                        raise HistoryUnavailable("A previously imported conflict branch has unavailable history")
                    match = relation(source_view, source_path, sid, target_view, target_path, did)
                    if match in ("equal", "source_prefix"):
                        break
                    serial += 1
                counters["conflicts_preserved"] += 1
            mapping[sid] = did
            overrides = set(old.get("overrides", [])) if old.get("target_id") == did else set()
            if match in ("source_prefix", "source_unavailable"):
                row = dict(existing)
                if match == "source_prefix":
                    counters["account_ahead"] += 1
                else:
                    # A rename/pin/archive can be read from the source index
                    # even when its old rollout has gone missing. Content,
                    # timestamps and model choice continue to come from the
                    # intact account transcript, not from the missing source.
                    for field in UI_FIELDS:
                        baseline = old.get("ui", {})
                        if field in baseline and existing.get(field) != baseline[field]:
                            overrides.add(field)
                        if field not in overrides and field in source_row:
                            row[field] = source_row[field]
                    if row.get("archived"):
                        row.update(is_pinned=0, thread_section_id=None, section_position=None)
            else:
                row = dict(source_row)
                row["id"], row["model_provider"] = did, destination_provider
                if did != sid and did != mirror.get("original_id"):
                    for field in ("title", "name"):
                        if row.get(field):
                            row[field] = str(row[field]).removesuffix(branch_suffix) + branch_suffix
                if existing:
                    # An imported conflict's local display suffix must not
                    # rename its original branch when it travels back home.
                    if did != sid and did == mirror.get("original_id"):
                        baseline_ui = mirror["entry"].get("ui", {})
                        prior_source_ui = old.get("source_ui", {})
                        for field in ("name", "title"):
                            if (field in baseline_ui and source_row.get(field) == baseline_ui[field] and
                                    (field not in prior_source_ui or source_row.get(field) == prior_source_ui[field])):
                                row[field] = existing.get(field)
                    for field in UI_FIELDS:
                        baseline = old.get("ui", {})
                        account_changed = field in baseline and existing.get(field) != baseline[field]
                        initially_newer = not baseline and core.thread_recency(existing) > core.thread_recency(source_row)
                        if account_changed or initially_newer:
                            overrides.add(field)
                        if field in overrides and existing.get(field) == row.get(field):
                            overrides.discard(field)
                        if field in overrides and field in existing:
                            row[field] = existing[field]
                    # Account model/permission choices are not SOTA configuration.
                    for field in ("model", "reasoning_effort", "sandbox_policy", "approval_mode"):
                        if field in existing:
                            row[field] = existing[field]
                else:
                    # An unset account default means the official client chooses
                    # it. Do not import a SOTA provider-qualified model/effort as
                    # an account selection. Existing account choices and historical
                    # per-turn annotations remain untouched; SOTA return is unchanged.
                    if model_config.get("model") or not return_to_sota:
                        row["model"] = model_config.get("model")
                    if model_config.get("model_reasoning_effort") or not return_to_sota:
                        row["reasoning_effort"] = model_config.get("model_reasoning_effort")
                if row.get("archived"):
                    row.update(is_pinned=0, thread_section_id=None, section_position=None)
            if match in ("missing", "target_prefix"):
                source_digest = source_view.digest(source_path, sid)
                page_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{identity_prefix}-head:{did}:{source_digest}"))
                name = core.independent_head_name(source_path, page_id, session_id=did)
                storage = "archived_sessions" if row.get("archived") else "sessions"
                rel = Path(storage) / storage_folder / name
                destination = stage / rel
                if store.retire_paths(target / rel, target):
                    raise AccountSyncError("A staged account head collides with an existing physical page")
                snapshot = core.stable_snapshot(store.readable(source_path), scratch)
                if did != sid:
                    clone = scratch / ("clone-" + did + ".jsonl")
                    core.make_conflict_clone(snapshot, clone, sid, did)
                    snapshot = clone
                importer.copy_head(snapshot, source, stage, destination)
                core.rewrite_session_for_destination(destination, destination_provider, str(source_row.get("model_provider")))
                staged_view.metadata[destination] = read_header(destination)
                if relation(source_view, source_path, sid, staged_view, destination, did) != "equal":
                    raise AccountSyncError("Copied account transcript did not preserve complete conversation content")
                imports[did] = sid
                final_heads[did] = destination
                if target_path:
                    retire.update(store.retire_paths(target_path, target))
                    counters["updated_threads"] += 1
                else:
                    counters["new_threads"] += 1
                row["rollout_path"] = str(target / rel)
                match = "equal"
            else:
                final_heads[did] = target_path
                row["rollout_path"] = str(target_path)
            final_path = final_heads[did]
            expected_storage = "archived_sessions" if row.get("archived") else "sessions"
            final_root = stage if final_path.is_relative_to(stage) else target
            if final_path.relative_to(final_root).parts[0] != expected_storage:
                rel = Path(expected_storage) / storage_folder / final_path.name
                moved = stage / rel
                if store.retire_paths(target / rel, target):
                    raise AccountSyncError("Account archive placement collides with another history page")
                core.atomic_copy_file(store.readable(final_path), moved)
                retire.update(store.retire_paths(final_path, target))
                staged_view.files[core.physical_page_id(final_path)] = moved
                staged_view.metadata[moved] = staged_view.metadata[final_path]
                final_heads[did] = moved
                row["rollout_path"] = str(target / rel)
            row = {key: value for key, value in row.items() if key in dst.thread_columns}
            selected[did] = row
            source_content = source_view.content_summary(source_path, sid) if source_path else (None, None)
            new_ledger["threads"][sid] = {
                "target_id": did, "source_signature": source_signature,
                "source_content_digest": source_content[0], "source_record_count": source_content[1],
                "relation": match, "ui": {key: row.get(key) for key in UI_FIELDS},
                "overrides": sorted(overrides),
                "source_ui": source_ui(source_row),
                "source_sidebar": sidebar_baseline(src.global_state, sid),
            }

    # An old head may be an immutable ancestor. Preserve it under the same
    # physical UUID in archived storage instead of replacing/deleting its bytes.
    referenced = {str(meta["history_base"]["thread_id"]) for meta in staged_view.metadata.values()
                  if isinstance(meta.get("history_base"), dict) and meta["history_base"].get("thread_id")}
    still_used = {logical_rollout_path(Path(row["rollout_path"]))
                  for row in {**dst.threads, **selected}.values()}
    for rel in list(retire):
        old_path = logical_rollout_path(target / rel)
        if old_path in still_used:
            retire.remove(rel)
        elif core.physical_page_id(old_path) in referenced:
            if rel.parts[0] == "archived_sessions":
                retire.remove(rel)
            else:
                archived_rel = Path("archived_sessions") / (storage_folder + "-ancestors") / old_path.name
                archived = stage / archived_rel
                if (target / archived_rel).exists():
                    if not core.same_file_bytes(store.readable(old_path), target / archived_rel):
                        raise AccountSyncError("Immutable account ancestor collision")
                    archived = target / archived_rel
                else:
                    core.atomic_copy_file(store.readable(old_path), archived)
                staged_view.files[core.physical_page_id(old_path)] = archived
                staged_view.metadata[archived] = staged_view.metadata[old_path]

    for sid in mapping:
        entry = new_ledger["threads"][sid]
        entry["target_signature"] = staged_view.signature(final_heads[entry["target_id"]])
        content = staged_view.content_summary(final_heads[entry["target_id"]], entry["target_id"])
        entry["target_content_digest"], entry["target_record_count"] = content
    update_rows = {did: row for did, row in selected.items() if row != dst.threads.get(did)}
    selected_refs = [(src, row) for row in update_rows.values()]
    reference_plan = core.plan_thread_reference_rows(dst, selected_refs)
    # Archived rows may still reference an existing section; do not import
    # source migration bookkeeping or synthesize a newer account schema.
    with contextlib.closing(sqlite3.connect(run / "source-state.sqlite")) as src_db:
        attachments = core.rows_as_dicts(src_db, "thread_attachments")
    with contextlib.closing(sqlite3.connect(stage / STATE)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        before_changes = db.total_changes
        with db:
            core.insert_thread_reference_rows(db, reference_plan)
            for did, row in update_rows.items():
                core.upsert_thread(db, row, Path(row["rollout_path"]))
            for sid, did in mapping.items():
                core.insert_dynamic_tools(db, src.dynamic_tools.get(sid, []), did)
            edges = []
            for edge in src.spawn_edges:
                if return_to_sota and not ({str(edge["parent_thread_id"]), str(edge["child_thread_id"])} & mapping.keys()):
                    continue
                edge = dict(edge)
                edge["parent_thread_id"] = mapping.get(str(edge["parent_thread_id"]), edge["parent_thread_id"])
                edge["child_thread_id"] = mapping.get(str(edge["child_thread_id"]), edge["child_thread_id"])
                if edge["parent_thread_id"] in {**dst.threads, **selected} and edge["child_thread_id"] in {**dst.threads, **selected}:
                    edges.append(edge)
            core.insert_spawn_edges(db, edges)
            for attachment in attachments:
                sid = str(attachment.get("thread_id"))
                if sid not in mapping:
                    continue
                if not core.table_exists(db, "thread_attachments"):
                    raise AccountSyncError("Account schema cannot store attachments yet; update/open the account App first")
                row = dict(attachment)
                row["thread_id"] = mapping[sid]
                if row["thread_id"] != sid:
                    row["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"account-attachment:{row['id']}:{row['thread_id']}"))
                columns = [key for key in core.table_columns(db, "thread_attachments") if key in row]
                db.execute(f"INSERT OR IGNORE INTO thread_attachments ({','.join(core.qident(c) for c in columns)}) "
                           f"VALUES ({','.join('?' for _ in columns)})", [row[c] for c in columns])
        db_dirty = db.total_changes != before_changes
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise AccountSyncError("Account history database/references failed validation")

    # Projection tables are rebuildable caches of the checked JSONL, not the
    # canonical transcript. Old offsets refer to different physical headers.
    history_dirty = False
    if imports and (target / HISTORY).exists():
        sqlite_copy(target / HISTORY, stage / HISTORY)
        with contextlib.closing(sqlite3.connect(stage / HISTORY)) as db:
            with db:
                for table in CACHE_TABLES:
                    if core.table_exists(db, table):
                        db.executemany(f"DELETE FROM {core.qident(table)} WHERE thread_id=?", [(did,) for did in imports])
            history_dirty = db.total_changes > 0

    sidebar = (copy.deepcopy(dst.global_state) if return_to_sota and not mapping else
               merge_sidebar(src, dst, mapping, selected, previous))
    index = copy.deepcopy(dst.session_index)
    for did, row in selected.items():
        if row.get("archived"):
            index.pop(did, None)
        elif not core.is_auxiliary_thread_source(row.get("source")):
            index[did] = {**index.get(did, {}), "id": did,
                          "thread_name": row.get("name") or row.get("title") or row.get("preview") or "Codex 对话",
                          "updated_at": core.timestamp_from_row(row)}
    for sid in mapping:
        entry = new_ledger["threads"][sid]
        entry["sidebar"] = sidebar_baseline(sidebar, entry["target_id"])
        # Retain the last supplied baseline when an account-side edit wins;
        # otherwise the following run would mistake that edit for our own write.
        for container, values in previous.get(sid, {}).get("sidebar", {}).items():
            for field, baseline in values.items():
                current = entry["sidebar"].get(container, {}).get(field)
                if current is not None and current != baseline:
                    actual = dst.global_state if container == "None" else dst.global_state.get(container, {})
                    original = actual.get(field, {}).get(entry["target_id"])
                    if "value" in baseline and original != baseline["value"]:
                        entry["sidebar"].setdefault(container, {})[field] = baseline
    paths = {path.relative_to(stage) for storage in core.SESSION_STORAGE_DIRS
             for path in (stage / storage).rglob("*.jsonl")}
    for name, dirty in ((STATE, db_dirty), (HISTORY, history_dirty)):
        if dirty:
            paths.add(Path(name))
            for suffix in ("-wal", "-shm"):
                if (target / (name + suffix)).exists():
                    retire.add(Path(name + suffix))
    for name, value, old_value in ((GLOBAL, sidebar, dst.global_state), (ledger_name, new_ledger, ledger)):
        if value != old_value:
            core.atomic_write_bytes(stage / name, json_bytes(value))
            paths.add(Path(name))
    if index != dst.session_index:
        core.write_session_index(stage / INDEX, index)
        paths.add(Path(INDEX))
    guard()
    source_drift = tree_state(source) != before_source
    target_drift = tree_state(target) != before_target
    if not dry_run and (source_drift or target_drift):
        raise AccountSyncError("History changed during preparation; no account data was installed")
    installed = 0 if dry_run else install_stage(base, run, stage, target, paths, retire, guard, rollback_guard)
    report = {
        "status": "ok", "mode": ("sota-return-history" if return_to_sota else "account-history") + ("-dry-run" if dry_run else "-one-way"),
        "source": str(source), "target": str(target), "source_read_only": True,
        "source_changed_during_preview": bool(source_drift),
        "target_changed_during_preview": bool(target_drift),
        "source_unavailable_account_preserved": source_issues,
        "source_threads": len(source_rows), "total_source_threads": len(src.threads),
        "unchanged_imports_skipped": len(src.threads) - len(source_rows),
        "account_threads": len(set(dst.threads) | set(selected)),
        "unarchived_main_threads": sum(not row.get("archived") and not core.is_auxiliary_thread_source(row.get("source"))
                                        for row in {**dst.threads, **selected}.values()),
        "mapped_source_threads": len(mapping), "account_only_threads_preserved": len(set(dst.threads) - set(src.threads)),
        "compressed_source_pages": sum(path.is_relative_to(source) and actual.name.endswith(".jsonl.zst")
                                       for path, actual in store.physical.items()),
        "compressed_target_pages": sum(path.is_relative_to(target) and actual.name.endswith(".jsonl.zst")
                                       for path, actual in store.physical.items()),
        "installed_files": installed, "planned_files": len(paths | retire), "backup_dir": str(run), **counters,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    core.atomic_write_json(run / "result.json", report)
    core.atomic_write_json(base / "last-result.json", report)
    return report


def read_header(path):
    with path.open(encoding="utf-8") as stream:
        return json.loads(stream.readline())["payload"]


def launcher_is_ancestor(pid: int) -> bool:
    """Windows venv redirectors can insert a Python process before the shell."""
    parent = os.getppid()
    if parent == pid:
        return True
    if os.name != "nt":
        return False
    kernel = lifecycle._kernel32()

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        parents = {}
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            parents[int(entry.pid)] = int(entry.parent)
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    allowed_images = {core.storage_path_key(sys.executable),
                      core.storage_path_key(getattr(sys, "_base_executable", sys.executable))}
    for _ in range(3):
        identity = lifecycle.process_identity(parent)
        if not identity or core.storage_path_key(identity["exe"]) not in allowed_images:
            return False
        parent = parents.get(parent)
        if parent == pid:
            return True
        if parent is None:
            return False
    return False


def borrowed_launcher_guard(pid: int, ticks: str, request_id: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id):
        raise AccountSyncError("Invalid account launch request identity")
    if not launcher_is_ancestor(pid):
        raise AccountSyncError("Only the invoking account launcher may lend its lifecycle lease")
    marker = lifecycle.LAUNCH_REQUEST_DIR / f"shell-{request_id}-{pid}.json"

    def check():
        identity = lifecycle.process_identity(pid)
        # This guard is itself a history checkpoint callback. The stable JSON
        # reader also checkpoints, so suppress only its nested callbacks while
        # reading the marker. Identity and app-quiescence checks still run.
        with core.suspend_history_checkpoints():
            request = read_object(marker)
        if (not identity or identity.get("creation_ticks") != ticks or request.get("request_id") != request_id
                or request.get("launcher_pid") != pid or str(request.get("launcher_creation_ticks")) != ticks):
            raise AccountSyncError("Account launcher identity/lease expired")
        no_app_guard()

    check()
    try:
        with lifecycle.lifecycle_lock():
            pass
    except lifecycle.LifecycleBusy:
        return check
    raise AccountSyncError("The account launcher must hold the lifecycle lease")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--work-base", type=Path)
    parser.add_argument("--to-sota", action="store_true", help="Return only account-side changes to the SOTA profile")
    parser.add_argument("--launcher-pid", type=int)
    parser.add_argument("--launcher-creation-ticks")
    parser.add_argument("--launch-request-id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sota = core.configured_root("CODEX_SOTA_CODEX_ROOT", ".codex-sota")
    account = core.configured_root("CODEX_SOTA_PLUS_ROOT", ".codex-plus")
    args.source = args.source or (account if args.to_sota else sota)
    args.target = args.target or (sota if args.to_sota else account)
    args.work_base = args.work_base or core.INSTALL_DIR / "work" / ("sota-return-sync" if args.to_sota else "account-sync")
    mode = "sota-return-history-one-way" if args.to_sota else "account-history-one-way"
    try:
        with contextlib.ExitStack() as stack:
            if args.launcher_pid:
                guard = borrowed_launcher_guard(args.launcher_pid, args.launcher_creation_ticks, args.launch_request_id or "")
            else:
                if not args.dry_run:
                    lifecycle.assert_quiescent()
                stack.enter_context(lifecycle.lifecycle_lock())
                # A preview never installs or rolls back account history. It
                # can inspect live profiles; drift is checked before reporting.
                guard = (lambda: None) if args.dry_run else lifecycle.assert_quiescent
            # The child owns history protection even if the parent exits. The
            # parent keeps lifecycle protection until this child has completed.
            stack.enter_context(lifecycle.FileLock(core.LOCK_PATH))
            guard()
            result = sync_account(args.source, args.target, args.work_base, guard=guard,
                                  dry_run=args.dry_run, return_to_sota=args.to_sota)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (lifecycle.LifecycleBusy, lifecycle.AppNotQuiescent) as error:
        print(json.dumps({"status": "deferred", "mode": mode, "error": str(error)}, ensure_ascii=True))
        return 2
    except Exception as error:
        print(json.dumps({"status": "error", "mode": mode, "error": str(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
