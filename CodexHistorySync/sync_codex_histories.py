#!/usr/bin/env python3
"""One-click, bidirectional local Codex history synchronizer for Windows.

This intentionally synchronizes only local conversation/history metadata. It
never copies auth.json, API keys, access tokens, config.toml, logs, or plugin
credentials.  It reads only the non-secret account_id field from auth.json so
the desktop app's account-scoped sidebar section can be rebuilt after a login
change.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid
from typing import Any, Iterable

if os.name == "nt":
    import msvcrt


APP_NAME = "CodexHistorySync"
INSTALL_DIR = Path(__file__).resolve().parent
DEFAULT_LEFT = Path.home() / ".codex-personal"
DEFAULT_RIGHT = Path.home() / ".codex-plus"
DEFAULT_BACKUP_BASE = INSTALL_DIR / "backups"
LOG_DIR = INSTALL_DIR / "logs"
LOCK_PATH = INSTALL_DIR / "sync.lock"
LAST_RESULT_PATH = INSTALL_DIR / "last-result.json"
MAX_BACKUPS = 20
COCKPIT_MODEL_PROVIDER = "codex_local_access"
PLUS_MODEL_PROVIDER = "openai"
CONFLICT_CLONE_SUFFIX = "（同步冲突副本）"
ATOM_STATE_KEY = "electron-persisted-atom-state"
SIDEBAR_PREFERENCES_KEY = "flat-project-sidebar-preferences-v1"
CUSTOM_SECTIONS_KEY = "sidebar-custom-sections-v3"
MANAGED_SECTION_ID = "6a2f6de3-f00e-50a3-906e-4c431676aac7"
MANAGED_SECTION_ORDER_KEY = f"custom:{MANAGED_SECTION_ID}"

LIST_STATE_KEYS = (
    "projectless-thread-ids",
    "pinned-thread-ids",
    "project-order",
)
DICT_STATE_KEYS = (
    "local-projects",
    "thread-project-assignments",
    "thread-workspace-root-hints",
    "thread-projectless-output-directories",
    "thread-writable-roots",
)
SESSION_STORAGE_DIRS = ("sessions", "archived_sessions")


@dataclasses.dataclass(frozen=True)
class SessionFile:
    session_id: str
    path: Path
    relative_path: Path
    size: int
    mtime_ns: int


@dataclasses.dataclass
class RootSnapshot:
    root: Path
    threads: dict[str, dict[str, Any]]
    thread_columns: list[str]
    dynamic_tools: dict[str, list[dict[str, Any]]]
    spawn_edges: list[dict[str, Any]]
    global_state: dict[str, Any]
    session_index: dict[str, dict[str, Any]]


@dataclasses.dataclass
class CloneSpec:
    old_id: str
    new_id: str
    source_root: Path
    source_session: SessionFile
    paths_by_root: dict[Path, Path]


@dataclasses.dataclass(frozen=True)
class DuplicateCloneSpec:
    canonical_id: str
    duplicate_id: str
    is_auxiliary: bool


class SyncError(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "sync.log"
    handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    return log_path


class SingleInstanceLock:
    def __init__(self, path: Path, wait_timeout: float = 0.0, poll_interval: float = 0.25):
        self.path = path
        self.wait_timeout = max(0.0, float(wait_timeout))
        self.poll_interval = max(0.05, float(poll_interval))
        self.file: Any | None = None
        self.waited_for_existing = False

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait_timeout
        while True:
            self.file = self.path.open("a+b")
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name != "nt":
                return self
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError as exc:
                self.waited_for_existing = True
                self.file.close()
                self.file = None
                if time.monotonic() >= deadline:
                    raise SyncError("History sync is already running; wait for it to finish.") from exc
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.file is None:
            return
        if os.name == "nt":
            with contextlib.suppress(OSError):
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
        self.file = None

def validate_root(root: Path) -> None:
    if not root.is_dir():
        raise SyncError(f"Codex 数据目录不存在：{root}")
    state_db = root / "state_5.sqlite"
    if not state_db.is_file():
        raise SyncError(f"Codex 数据目录不完整：{root}；缺少：{state_db}")
    sessions_root = root / "sessions"
    if sessions_root.exists() and not sessions_root.is_dir():
        raise SyncError(f"Codex 会话路径不是目录：{sessions_root}")
    sessions_root.mkdir(parents=True, exist_ok=True)


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({qident(table)})")]


def rows_as_dicts(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not table_exists(connection, table):
        return []
    columns = table_columns(connection, table)
    sql = f"SELECT {','.join(qident(c) for c in columns)} FROM {qident(table)}"
    return [dict(zip(columns, row)) for row in connection.execute(sql)]


def read_json_retry(path: Path, attempts: int = 10) -> dict[str, Any]:
    if not path.exists():
        return {}
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            before = path.stat()
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
                if not isinstance(value, dict):
                    raise ValueError("top-level JSON value is not an object")
                return value
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise SyncError(f"无法稳定读取 JSON：{path}；{last_error}")


def atomic_write_bytes(path: Path, payload: bytes, attempts: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        temp_path: Path | None = None
        try:
            fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.sync-", dir=path.parent)
            temp_path = Path(raw_temp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
        finally:
            if temp_path is not None and temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()


def atomic_copy_file(source: Path, destination: Path, attempts: int = 20) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        temp_path: Path | None = None
        try:
            fd, raw_temp = tempfile.mkstemp(
                prefix=f".{destination.name}.sync-", dir=destination.parent
            )
            os.close(fd)
            temp_path = Path(raw_temp)
            shutil.copy2(source, temp_path)
            os.replace(temp_path, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
        finally:
            if temp_path is not None and temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_write_bytes(path, payload)


def read_session_index(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("忽略损坏的会话索引行 %s:%s", path, line_number)
                continue
            if isinstance(item, dict) and item.get("id"):
                result[str(item["id"])] = item
    return result


def write_session_index(path: Path, index: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(index.values(), key=lambda item: str(item.get("updated_at", "")))
    lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in ordered]
    atomic_write_bytes(path, (("\n".join(lines) + "\n") if lines else "").encode("utf-8"))


def session_id_from_file(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
    item = json.loads(first_line)
    if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
        raise ValueError("missing session metadata payload")
    payload = item["payload"]
    session_id = payload.get("id") or payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session id")
    return session_id


def scan_sessions(root: Path, preferred_paths: dict[str, str] | None = None) -> dict[str, SessionFile]:
    preferred_paths = preferred_paths or {}
    catalog: dict[str, SessionFile] = {}
    for directory_name in SESSION_STORAGE_DIRS:
        storage_root = root / directory_name
        if not storage_root.exists():
            continue
        if not storage_root.is_dir():
            raise SyncError(f"Codex 会话路径不是目录：{storage_root}")
        for path in storage_root.rglob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                session_id = session_id_from_file(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logging.warning("忽略无法识别的会话文件 %s：%s", path, exc)
                continue
            stat = path.stat()
            current = SessionFile(
                session_id=session_id,
                path=path,
                relative_path=path.relative_to(root),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
            previous = catalog.get(session_id)
            if previous is None:
                catalog[session_id] = current
                continue
            preferred = preferred_paths.get(session_id)
            if preferred and os.path.normcase(os.path.abspath(preferred)) == os.path.normcase(
                os.path.abspath(path)
            ):
                catalog[session_id] = current
            elif not preferred and (current.mtime_ns, current.size) > (
                previous.mtime_ns,
                previous.size,
            ):
                catalog[session_id] = current
            logging.warning("会话 %s 在 %s 中存在重复文件", session_id, root)
    return catalog


def normalize_session_self_references(value: Any, session_id: str) -> Any:
    """Normalize only references that identify the rollout itself.

    Conflict clones receive a new session/thread ID.  Replacing only self-ID
    fields lets us prove that two rollouts are otherwise JSON-semantically
    identical without ignoring timestamps, messages, paths, or tool output.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"id", "session_id", "thread_id"} and item == session_id:
                result[key] = "<SELF_SESSION_ID>"
            else:
                result[key] = normalize_session_self_references(item, session_id)
        return result
    if isinstance(value, list):
        return [normalize_session_self_references(item, session_id) for item in value]
    return value


def normalized_session_digest(session: SessionFile) -> str:
    """Hash a rollout after removing only profile/provider and self-ID differences."""
    digest = hashlib.sha256()
    with session.path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            item = json.loads(line)
            if line_number == 1 and isinstance(item, dict):
                payload = item.get("payload")
                if isinstance(payload, dict):
                    payload.pop("model_provider", None)
            item = normalize_session_self_references(item, session.session_id)
            digest.update(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def sessions_semantically_equal(left: SessionFile, right: SessionFile) -> bool:
    try:
        return normalized_session_digest(left) == normalized_session_digest(right)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("无法计算会话语义指纹：%s", exc)
        return False


def thread_display_name(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    return str(row.get("title") or row.get("name") or "")


def find_legacy_exact_clones(
    left: RootSnapshot, right: RootSnapshot
) -> tuple[list[DuplicateCloneSpec], list[str]]:
    """Find only old conflict clones that are provably exact duplicates.

    A deletion candidate must have the explicit conflict-clone suffix, exactly
    one unsuffixed canonical row with the matching base name, and the same full
    normalized rollout digest in every root where it exists.  Same-title or
    prefix-related chats are intentionally not considered duplicates.
    """
    snapshots = (left, right)
    catalogs: dict[Path, dict[str, SessionFile]] = {}
    digest_cache: dict[tuple[Path, str], str] = {}
    all_ids = set(left.threads) | set(right.threads)
    for snapshot in snapshots:
        preferred = {
            session_id: str(row.get("rollout_path") or "")
            for session_id, row in snapshot.threads.items()
        }
        catalogs[snapshot.root] = scan_sessions(snapshot.root, preferred)

    def digest_values(session_id: str) -> set[str]:
        values: set[str] = set()
        for snapshot in snapshots:
            session = catalogs[snapshot.root].get(session_id)
            if session is None:
                continue
            key = (snapshot.root, session_id)
            if key not in digest_cache:
                digest_cache[key] = normalized_session_digest(session)
            values.add(digest_cache[key])
        return values

    plan: list[DuplicateCloneSpec] = []
    warnings: list[str] = []
    for duplicate_id in sorted(all_ids):
        duplicate_rows = [
            snapshot.threads[duplicate_id]
            for snapshot in snapshots
            if duplicate_id in snapshot.threads
        ]
        duplicate_names = {thread_display_name(row) for row in duplicate_rows}
        if not duplicate_names or not all(
            name.endswith(CONFLICT_CLONE_SUFFIX) for name in duplicate_names
        ):
            continue
        base_names = {
            name[: -len(CONFLICT_CLONE_SUFFIX)] for name in duplicate_names
        }
        if len(base_names) != 1:
            continue
        base_name = next(iter(base_names))
        duplicate_digests = digest_values(duplicate_id)
        if len(duplicate_digests) != 1:
            continue

        matching_canonicals: list[str] = []
        for candidate_id in sorted(all_ids - {duplicate_id}):
            candidate_rows = [
                snapshot.threads[candidate_id]
                for snapshot in snapshots
                if candidate_id in snapshot.threads
            ]
            if not candidate_rows:
                continue
            candidate_names = {thread_display_name(row) for row in candidate_rows}
            if candidate_names != {base_name}:
                continue
            candidate_digests = digest_values(candidate_id)
            if len(candidate_digests) == 1 and candidate_digests == duplicate_digests:
                matching_canonicals.append(candidate_id)

        if len(matching_canonicals) != 1:
            if len(matching_canonicals) > 1:
                warnings.append(
                    f"精确副本 {duplicate_id} 对应多个原会话，已为安全起见保留。"
                )
            continue
        is_auxiliary = all(
            is_auxiliary_thread_source(row.get("source")) for row in duplicate_rows
        )
        plan.append(
            DuplicateCloneSpec(
                canonical_id=matching_canonicals[0],
                duplicate_id=duplicate_id,
                is_auxiliary=is_auxiliary,
            )
        )
    return plan, warnings


def validate_jsonl(path: Path) -> None:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for count, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError(f"line {count} is incomplete")
            json.loads(line)
    if count == 0:
        raise ValueError("empty JSONL")


def stable_snapshot(source: Path, temp_dir: Path, attempts: int = 6) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(attempts):
        destination = temp_dir / f"snapshot-{uuid.uuid4().hex}.jsonl"
        try:
            before = source.stat()
            shutil.copy2(source, destination)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                destination.unlink(missing_ok=True)
                time.sleep(0.08)
                continue
            validate_jsonl(destination)
            return destination
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            destination.unlink(missing_ok=True)
            if attempt < attempts - 1:
                time.sleep(0.12)
    raise SyncError(f"无法取得完整会话快照：{source}；{last_error}")


def compare_files(left: SessionFile, right: SessionFile) -> str:
    """Return equal, left_prefix, right_prefix, or divergent."""
    # The same conversation intentionally has a different model_provider in
    # each profile (Plus=openai, Cockpit=codex_local_access). Compare the JSONL
    # header without that profile-specific field, then compare the transcript.
    with left.path.open("rb") as handle_left, right.path.open("rb") as handle_right:
        try:
            left_header = json.loads(handle_left.readline())
            right_header = json.loads(handle_right.readline())
            for header in (left_header, right_header):
                payload = header.get("payload") if isinstance(header, dict) else None
                if isinstance(payload, dict):
                    payload.pop("model_provider", None)
        except (json.JSONDecodeError, AttributeError):
            return "equal" if sessions_semantically_equal(left, right) else "divergent"
        if left_header != right_header:
            return "equal" if sessions_semantically_equal(left, right) else "divergent"
        left_start = handle_left.tell()
        right_start = handle_right.tell()
        left_tail_size = left.size - left_start
        right_tail_size = right.size - right_start
        remaining = min(left_tail_size, right_tail_size)
        while remaining:
            chunk_size = min(1024 * 1024, remaining)
            chunk_left = handle_left.read(chunk_size)
            chunk_right = handle_right.read(chunk_size)
            if chunk_left != chunk_right:
                return "equal" if sessions_semantically_equal(left, right) else "divergent"
            remaining -= len(chunk_left)
    if left_tail_size == right_tail_size:
        return "equal"
    if left_tail_size < right_tail_size:
        return "left_prefix"
    return "right_prefix"


def set_session_model_provider(path: Path, provider: str) -> None:
    """Atomically set the profile-specific provider in a rollout header."""
    temp_path: Path | None = None
    with path.open("rb") as source:
        first_line = source.readline()
        item = json.loads(first_line)
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            raise SyncError(f"会话元数据无效：{path}")
        if payload.get("model_provider") == provider:
            return
        payload["model_provider"] = provider
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.provider-", dir=path.parent)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(encoded + b"\n")
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            if temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()
            raise
    assert temp_path is not None
    try:
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()


def provider_for_root(root: Path, root_providers: dict[Path, str]) -> str:
    provider = root_providers.get(root.resolve())
    if not provider:
        raise SyncError(f"未知的同步目标目录或模型提供商：{root}")
    return provider


def copy_snapshot_to(snapshot: Path, destination: Path) -> None:
    atomic_copy_file(snapshot, destination)


def recursive_replace_thread_refs(value: Any, old_id: str, new_id: str) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"thread_id", "session_id"} and item == old_id:
                result[key] = new_id
            else:
                result[key] = recursive_replace_thread_refs(item, old_id, new_id)
        return result
    if isinstance(value, list):
        return [recursive_replace_thread_refs(item, old_id, new_id) for item in value]
    return value


def make_conflict_clone(
    snapshot: Path, destination: Path, old_id: str, new_id: str
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{destination.name}.clone-", dir=destination.parent)
    temp_path = Path(raw_temp)
    try:
        with snapshot.open("r", encoding="utf-8") as source, os.fdopen(
            fd, "w", encoding="utf-8", newline="\n"
        ) as target:
            for line_number, line in enumerate(source, start=1):
                item = json.loads(line)
                item = recursive_replace_thread_refs(item, old_id, new_id)
                if (
                    line_number == 1
                    and isinstance(item, dict)
                    and isinstance(item.get("payload"), dict)
                ):
                    payload = item["payload"]
                    if payload.get("id") == old_id:
                        payload["id"] = new_id
                    if payload.get("session_id") == old_id:
                        payload["session_id"] = new_id
                target.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()
    validate_jsonl(destination)


def load_root_snapshot(root: Path) -> RootSnapshot:
    db_path = root / "state_5.sqlite"
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        thread_columns = table_columns(connection, "threads")
        threads = {str(row["id"]): row for row in rows_as_dicts(connection, "threads")}
        dynamic_tools: dict[str, list[dict[str, Any]]] = {}
        for row in rows_as_dicts(connection, "thread_dynamic_tools"):
            dynamic_tools.setdefault(str(row["thread_id"]), []).append(row)
        spawn_edges = rows_as_dicts(connection, "thread_spawn_edges")
    finally:
        connection.close()
    return RootSnapshot(
        root=root,
        threads=threads,
        thread_columns=thread_columns,
        dynamic_tools=dynamic_tools,
        spawn_edges=spawn_edges,
        global_state=read_json_retry(root / ".codex-global-state.json"),
        session_index=read_session_index(root / "session_index.jsonl"),
    )


def thread_recency(row: dict[str, Any] | None) -> tuple[int, int, int, int]:
    if not row:
        return (0, 0, 0, 0)
    return (
        int(row.get("recency_at_ms") or 0),
        int(row.get("updated_at_ms") or 0),
        int(row.get("updated_at") or 0) * 1000,
        int(row.get("tokens_used") or 0),
    )


def choose_thread_source(
    session_id: str, left: RootSnapshot, right: RootSnapshot
) -> tuple[RootSnapshot, dict[str, Any]] | None:
    left_row = left.threads.get(session_id)
    right_row = right.threads.get(session_id)
    if left_row is None and right_row is None:
        return None
    if left_row is None:
        return right, right_row  # type: ignore[return-value]
    if right_row is None:
        return left, left_row
    left_key = thread_recency(left_row)
    right_key = thread_recency(right_row)
    if right_key > left_key:
        return right, right_row
    if left_key > right_key:
        return left, left_row
    # Prefer the newer schema when recency is tied.
    if len(right.thread_columns) >= len(left.thread_columns):
        return right, right_row
    return left, left_row


def create_backup(
    roots: list[Path], backup_base: Path, temp_work: Path
) -> Path:
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_base / timestamp
    destination.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"created_at": iso_now(), "roots": {}}
    for label, root in zip(("cockpit", "plus"), roots):
        root_destination = destination / label
        root_destination.mkdir(parents=True)
        source_db = root / "state_5.sqlite"
        target_db = root_destination / "state_5.sqlite"
        source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(target_db)
        try:
            source.backup(target)
            target.commit()
            count = target.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        finally:
            target.close()
            source.close()
        copied: list[str] = ["state_5.sqlite"]
        for name in (".codex-global-state.json", "session_index.jsonl"):
            source_path = root / name
            if source_path.exists():
                shutil.copy2(source_path, root_destination / name)
                copied.append(name)
        manifest["roots"][label] = {
            "root": str(root),
            "thread_count": count,
            "files": copied,
        }
    atomic_write_json(destination / "manifest.json", manifest)
    return destination


def rotate_backups(backup_base: Path, keep: int = MAX_BACKUPS) -> None:
    if not backup_base.exists():
        return
    backups = sorted(
        [path for path in backup_base.iterdir() if path.is_dir()],
        key=lambda path: path.name,
        reverse=True,
    )
    for old in backups[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def backup_conflict_file(
    backup_dir: Path, root: Path, label: str, session_file: SessionFile
) -> None:
    destination = backup_dir / "conflicts" / label / session_file.relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(session_file.path, destination)


def sync_session_files(
    left: RootSnapshot,
    right: RootSnapshot,
    backup_dir: Path,
    temp_dir: Path,
    root_providers: dict[Path, str],
) -> tuple[
    dict[Path, dict[str, Path]],
    list[CloneSpec],
    dict[str, int],
    list[str],
]:
    preferred_left = {sid: str(row.get("rollout_path") or "") for sid, row in left.threads.items()}
    preferred_right = {sid: str(row.get("rollout_path") or "") for sid, row in right.threads.items()}
    left_files = scan_sessions(left.root, preferred_left)
    right_files = scan_sessions(right.root, preferred_right)
    relevant_ids = set(left.threads) | set(right.threads)
    target_paths: dict[Path, dict[str, Path]] = {left.root: {}, right.root: {}}
    clones: list[CloneSpec] = []
    counters = {"new_files": 0, "updated_files": 0, "conflicts": 0, "unchanged": 0}
    warnings: list[str] = []

    for session_id in sorted((set(left_files) | set(right_files)) & relevant_ids):
        left_file = left_files.get(session_id)
        right_file = right_files.get(session_id)
        if left_file is None or right_file is None:
            source_file = left_file or right_file
            assert source_file is not None
            snapshot = stable_snapshot(source_file.path, temp_dir)
            for root, existing in ((left.root, left_file), (right.root, right_file)):
                destination = (
                    existing.path
                    if existing is not None
                    else root / source_file.relative_path
                )
                target_paths[root][session_id] = destination
                if existing is None:
                    copy_snapshot_to(snapshot, destination)
                    counters["new_files"] += 1
            snapshot.unlink(missing_ok=True)
            continue

        target_paths[left.root][session_id] = left_file.path
        target_paths[right.root][session_id] = right_file.path
        relation = compare_files(left_file, right_file)
        if relation == "equal":
            counters["unchanged"] += 1
            continue
        if relation in {"left_prefix", "right_prefix"}:
            source_file = right_file if relation == "left_prefix" else left_file
            destination_file = left_file if relation == "left_prefix" else right_file
            snapshot = stable_snapshot(source_file.path, temp_dir)
            copy_snapshot_to(snapshot, destination_file.path)
            snapshot.unlink(missing_ok=True)
            counters["updated_files"] += 1
            continue

        # True divergence: keep the most recent branch under the original ID and
        # create a second, visible thread for the alternate branch.
        left_key = (thread_recency(left.threads.get(session_id)), left_file.mtime_ns, left_file.size)
        right_key = (
            thread_recency(right.threads.get(session_id)),
            right_file.mtime_ns,
            right_file.size,
        )
        if right_key >= left_key:
            canonical_snapshot_root, canonical_file = right, right_file
            alternate_snapshot_root, alternate_file = left, left_file
        else:
            canonical_snapshot_root, canonical_file = left, left_file
            alternate_snapshot_root, alternate_file = right, right_file

        backup_conflict_file(backup_dir, left.root, "cockpit", left_file)
        backup_conflict_file(backup_dir, right.root, "plus", right_file)
        # Snapshot both branches before replacing either file. This prevents the
        # alternate branch from disappearing between conflict detection and clone creation.
        canonical_snapshot = stable_snapshot(canonical_file.path, temp_dir)
        alternate_snapshot = stable_snapshot(alternate_file.path, temp_dir)
        if canonical_file.path != left_file.path:
            copy_snapshot_to(canonical_snapshot, left_file.path)
        if canonical_file.path != right_file.path:
            copy_snapshot_to(canonical_snapshot, right_file.path)

        new_id = str(uuid.uuid4())
        filename = alternate_file.path.name
        if session_id in filename:
            filename = filename.replace(session_id, new_id)
        else:
            filename = f"rollout-conflict-{new_id}.jsonl"
        relative = alternate_file.relative_path.with_name(filename)
        paths_by_root: dict[Path, Path] = {}
        for root in (left.root, right.root):
            destination = root / relative
            make_conflict_clone(alternate_snapshot, destination, session_id, new_id)
            paths_by_root[root] = destination
            target_paths[root][new_id] = destination
        canonical_snapshot.unlink(missing_ok=True)
        alternate_snapshot.unlink(missing_ok=True)
        clones.append(
            CloneSpec(
                old_id=session_id,
                new_id=new_id,
                source_root=alternate_snapshot_root.root,
                source_session=alternate_file,
                paths_by_root=paths_by_root,
            )
        )
        counters["conflicts"] += 1
        warnings.append(f"会话 {session_id} 两边均被续写，已保留同步冲突副本 {new_id}。")

    # Make every copied rollout loadable and resumable under the destination
    # login. This is what the App's provider-filtered sidebar actually reads.
    for root, paths in target_paths.items():
        provider = provider_for_root(root, root_providers)
        for path in paths.values():
            set_session_model_provider(path, provider)

    return target_paths, clones, counters, warnings


def make_clone_row(source_row: dict[str, Any], clone: CloneSpec) -> dict[str, Any]:
    row = dict(source_row)
    row["id"] = clone.new_id
    for key in ("title", "name"):
        if key in row and row.get(key):
            row[key] = str(row[key]) + CONFLICT_CLONE_SUFFIX
    return row


def upsert_thread(
    connection: sqlite3.Connection, row: dict[str, Any], rollout_path: Path
) -> None:
    destination_columns = table_columns(connection, "threads")
    values = dict(row)
    values["rollout_path"] = str(rollout_path)
    columns = [column for column in destination_columns if column in values]
    if "id" not in columns:
        raise SyncError("threads 表缺少 id 列")
    insert_sql = (
        f"INSERT INTO threads ({','.join(qident(c) for c in columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})"
    )
    update_columns = [column for column in columns if column != "id"]
    if update_columns:
        insert_sql += " ON CONFLICT(id) DO UPDATE SET " + ",".join(
            f"{qident(column)}=excluded.{qident(column)}" for column in update_columns
        )
    connection.execute(insert_sql, [values[column] for column in columns])


def insert_dynamic_tools(
    connection: sqlite3.Connection, rows: Iterable[dict[str, Any]], thread_id: str
) -> None:
    if not table_exists(connection, "thread_dynamic_tools"):
        return
    destination_columns = table_columns(connection, "thread_dynamic_tools")
    for source_row in rows:
        row = dict(source_row)
        row["thread_id"] = thread_id
        columns = [column for column in destination_columns if column in row]
        sql = (
            f"INSERT OR IGNORE INTO thread_dynamic_tools "
            f"({','.join(qident(c) for c in columns)}) VALUES "
            f"({','.join('?' for _ in columns)})"
        )
        connection.execute(sql, [row[column] for column in columns])


def insert_spawn_edges(
    connection: sqlite3.Connection, edges: Iterable[dict[str, Any]]
) -> None:
    if not table_exists(connection, "thread_spawn_edges"):
        return
    destination_columns = table_columns(connection, "thread_spawn_edges")
    for row in edges:
        columns = [column for column in destination_columns if column in row]
        sql = (
            f"INSERT OR IGNORE INTO thread_spawn_edges "
            f"({','.join(qident(c) for c in columns)}) VALUES "
            f"({','.join('?' for _ in columns)})"
        )
        connection.execute(sql, [row[column] for column in columns])


def remap_persisted_thread_references(value: Any, mapping: dict[str, str]) -> Any:
    """Replace duplicate IDs with canonical IDs in persisted UI metadata.

    Canonical dictionary entries are processed first and win on collision;
    duplicate entries only fill a value that the canonical ID did not already
    have.  Lists are de-duplicated after remapping.
    """
    if isinstance(value, str):
        result = value
        for duplicate_id, canonical_id in mapping.items():
            result = result.replace(duplicate_id, canonical_id)
        return result
    if isinstance(value, list):
        result: list[Any] = []
        seen: set[str] = set()
        for item in value:
            remapped = remap_persisted_thread_references(item, mapping)
            try:
                marker = json.dumps(remapped, sort_keys=True, ensure_ascii=False)
            except TypeError:
                marker = repr(remapped)
            if marker not in seen:
                seen.add(marker)
                result.append(remapped)
        return result
    if isinstance(value, dict):
        result: dict[Any, Any] = {}

        def is_duplicate_key(item: tuple[Any, Any]) -> bool:
            key = item[0]
            return isinstance(key, str) and any(old_id in key for old_id in mapping)

        # Existing canonical keys win even when a duplicate key appeared first
        # in the JSON object's insertion order.
        for key, item in sorted(value.items(), key=is_duplicate_key):
            remapped_key = remap_persisted_thread_references(key, mapping)
            remapped_item = remap_persisted_thread_references(item, mapping)
            if remapped_key not in result:
                result[remapped_key] = remapped_item
        return result
    return value


def session_paths_for_ids(root: Path, session_ids: set[str]) -> dict[str, list[Path]]:
    result = {session_id: [] for session_id in session_ids}
    for directory_name in SESSION_STORAGE_DIRS:
        sessions_root = (root / directory_name).resolve()
        if not sessions_root.is_dir():
            continue
        for path in sessions_root.rglob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                session_id = session_id_from_file(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if session_id not in result:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(sessions_root):
                raise SyncError(f"拒绝清理会话目录之外的文件：{resolved}")
            result[session_id].append(resolved)
    return result


def purge_legacy_exact_clones(
    roots: list[Path],
    plan: list[DuplicateCloneSpec],
    backup_dir: Path,
) -> dict[str, Any]:
    """Back up and remove exact old conflict clones from every local profile."""
    if not plan:
        return {"threads": 0, "main_threads": 0, "auxiliary_threads": 0, "files": 0}

    mapping = {item.duplicate_id: item.canonical_id for item in plan}
    duplicate_ids = set(mapping)
    atomic_write_json(
        backup_dir / "removed-exact-duplicates" / "manifest.json",
        {
            "created_at": iso_now(),
            "mappings": [
                {
                    "duplicate_id": item.duplicate_id,
                    "canonical_id": item.canonical_id,
                    "is_auxiliary": item.is_auxiliary,
                }
                for item in plan
            ],
        },
    )
    removed_files = 0
    for label, root in zip(("cockpit", "plus"), roots):
        paths_by_id = session_paths_for_ids(root, duplicate_ids)
        for duplicate_id, paths in paths_by_id.items():
            for path in paths:
                relative = path.relative_to(root.resolve())
                destination = (
                    backup_dir
                    / "removed-exact-duplicates"
                    / label
                    / relative
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)

        connection = sqlite3.connect(root / "state_5.sqlite", timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for item in plan:
                if table_exists(connection, "thread_dynamic_tools"):
                    columns = table_columns(connection, "thread_dynamic_tools")
                    sql = (
                        f"SELECT {','.join(qident(column) for column in columns)} "
                        "FROM thread_dynamic_tools WHERE thread_id=?"
                    )
                    tools = [
                        dict(zip(columns, row))
                        for row in connection.execute(sql, (item.duplicate_id,))
                    ]
                    insert_dynamic_tools(connection, tools, item.canonical_id)
                    connection.execute(
                        "DELETE FROM thread_dynamic_tools WHERE thread_id=?",
                        (item.duplicate_id,),
                    )

            if table_exists(connection, "thread_spawn_edges"):
                related_edges = [
                    row
                    for row in rows_as_dicts(connection, "thread_spawn_edges")
                    if str(row.get("parent_thread_id")) in duplicate_ids
                    or str(row.get("child_thread_id")) in duplicate_ids
                ]
                remapped_edges: list[dict[str, Any]] = []
                for edge in related_edges:
                    remapped = dict(edge)
                    for key in ("parent_thread_id", "child_thread_id"):
                        old_value = str(remapped.get(key) or "")
                        if old_value in mapping:
                            remapped[key] = mapping[old_value]
                    if remapped.get("parent_thread_id") != remapped.get("child_thread_id"):
                        remapped_edges.append(remapped)
                insert_spawn_edges(connection, remapped_edges)
                for duplicate_id in duplicate_ids:
                    connection.execute(
                        "DELETE FROM thread_spawn_edges "
                        "WHERE parent_thread_id=? OR child_thread_id=?",
                        (duplicate_id, duplicate_id),
                    )

            for duplicate_id in duplicate_ids:
                connection.execute("DELETE FROM threads WHERE id=?", (duplicate_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        state_path = root / ".codex-global-state.json"
        state = read_json_retry(state_path)
        remapped_state = remap_persisted_thread_references(state, mapping)
        if not isinstance(remapped_state, dict):
            raise SyncError(f"全局状态格式无效：{state_path}")
        atomic_write_json(state_path, remapped_state)

        index_path = root / "session_index.jsonl"
        index = read_session_index(index_path)
        for item in plan:
            duplicate_entry = index.pop(item.duplicate_id, None)
            if item.canonical_id not in index and duplicate_entry is not None:
                replacement = remap_persisted_thread_references(
                    duplicate_entry, mapping
                )
                replacement["id"] = item.canonical_id
                name = str(replacement.get("thread_name") or "")
                if name.endswith(CONFLICT_CLONE_SUFFIX):
                    replacement["thread_name"] = name[: -len(CONFLICT_CLONE_SUFFIX)]
                index[item.canonical_id] = replacement
        write_session_index(index_path, index)

        for paths in paths_by_id.values():
            for path in paths:
                path.unlink()
                removed_files += 1

    main_count = sum(not item.is_auxiliary for item in plan)
    return {
        "threads": len(plan),
        "main_threads": main_count,
        "auxiliary_threads": len(plan) - main_count,
        "files": removed_files,
    }


def sync_databases(
    left: RootSnapshot,
    right: RootSnapshot,
    target_paths: dict[Path, dict[str, Path]],
    clones: list[CloneSpec],
    root_providers: dict[Path, str],
) -> dict[str, Any]:
    selected: dict[str, tuple[RootSnapshot, dict[str, Any]]] = {}
    for session_id in set(left.threads) | set(right.threads):
        chosen = choose_thread_source(session_id, left, right)
        if chosen is not None:
            selected[session_id] = chosen
    for clone in clones:
        source_snapshot = left if clone.source_root == left.root else right
        source_row = source_snapshot.threads.get(clone.old_id)
        if source_row is None:
            raise SyncError(f"无法为冲突副本找到原始数据库记录：{clone.old_id}")
        selected[clone.new_id] = (source_snapshot, make_clone_row(source_row, clone))

    all_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[Any, ...]] = set()
    for edge in left.spawn_edges + right.spawn_edges:
        key = tuple(edge.get(name) for name in ("parent_thread_id", "child_thread_id"))
        if key not in seen_edges:
            seen_edges.add(key)
            all_edges.append(edge)

    integrity: dict[str, str] = {}
    for destination_snapshot in (left, right):
        connection = sqlite3.connect(destination_snapshot.root / "state_5.sqlite", timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for session_id, (source_snapshot, row) in selected.items():
                rollout_path = target_paths[destination_snapshot.root].get(session_id)
                if rollout_path is None:
                    continue
                destination_row = dict(row)
                destination_row["model_provider"] = provider_for_root(
                    destination_snapshot.root, root_providers
                )
                upsert_thread(connection, destination_row, rollout_path)
                tools = source_snapshot.dynamic_tools.get(
                    session_id if session_id in source_snapshot.dynamic_tools else row.get("id"), []
                )
                if session_id in {clone.new_id for clone in clones}:
                    clone = next(item for item in clones if item.new_id == session_id)
                    tools = source_snapshot.dynamic_tools.get(clone.old_id, [])
                insert_dynamic_tools(connection, tools, session_id)
            insert_spawn_edges(connection, all_edges)
            connection.commit()
            integrity[str(destination_snapshot.root)] = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    return integrity


def merge_unique(preferred: list[Any], other: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for item in preferred + other:
        try:
            marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        except TypeError:
            marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def collect_main_candidates(
    left: RootSnapshot, right: RootSnapshot, clones: list[CloneSpec]
) -> list[tuple[str, dict[str, Any]]]:
    """Return every unarchived top-level chat in newest-first order."""
    candidates: dict[str, dict[str, Any]] = {}
    for session_id in set(left.threads) | set(right.threads):
        chosen = choose_thread_source(session_id, left, right)
        if chosen is None:
            continue
        _, row = chosen
        if int(row.get("archived") or 0) != 0 or is_auxiliary_thread_source(
            row.get("source")
        ):
            continue
        candidates[session_id] = row
    for clone in clones:
        source_snapshot = left if clone.source_root == left.root else right
        source_row = source_snapshot.threads.get(clone.old_id)
        if source_row is not None:
            candidates[clone.new_id] = make_clone_row(source_row, clone)
    return sorted(candidates.items(), key=lambda item: thread_recency(item[1]), reverse=True)


def assignment_project_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        project_id = value.get("projectId")
        if isinstance(project_id, str) and project_id:
            return project_id
    return None


def read_sidebar_account_ids(root: Path) -> set[str]:
    """Read only account IDs; token values are never returned, logged, or copied."""
    auth_path = root / "auth.json"
    if not auth_path.exists():
        return set()
    try:
        auth = read_json_retry(auth_path)
    except SyncError as exc:
        logging.warning("无法读取 %s 中的账号 ID：%s", auth_path, exc)
        return set()
    tokens = auth.get("tokens")
    candidates = [auth.get("account_id"), auth.get("accountId")]
    if isinstance(tokens, dict):
        candidates.extend((tokens.get("account_id"), tokens.get("accountId")))
    return {str(item) for item in candidates if isinstance(item, str) and item}


def custom_section_account_ids(state: dict[str, Any]) -> set[str]:
    atom_state = state.get(ATOM_STATE_KEY)
    if not isinstance(atom_state, dict):
        return set()
    by_account = atom_state.get(CUSTOM_SECTIONS_KEY)
    if not isinstance(by_account, dict):
        return set()
    return {str(account_id) for account_id in by_account if str(account_id)}


def build_managed_section_items(
    merged_values: dict[str, Any],
    main_candidates: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    """Build one unlimited sidebar catalog without flattening project chats."""
    assignments = merged_values.get("thread-project-assignments", {})
    if not isinstance(assignments, dict):
        assignments = {}
    local_projects = merged_values.get("local-projects", {})
    if not isinstance(local_projects, dict):
        local_projects = {}
    project_ids = {str(project_id) for project_id in local_projects}

    project_recency: dict[str, tuple[int, int, int, int]] = {}
    entries: list[tuple[tuple[int, int, int, int], str]] = []
    for thread_id, row in main_candidates:
        project_id = assignment_project_id(assignments.get(thread_id))
        if project_id in project_ids:
            project_recency[project_id] = max(
                project_recency.get(project_id, (0, 0, 0, 0)),
                thread_recency(row),
            )
        else:
            entries.append((thread_recency(row), f"codex:thread:local:{thread_id}"))

    for project_id, project in local_projects.items():
        project_id = str(project_id)
        updated_at = 0
        if isinstance(project, dict):
            with contextlib.suppress(TypeError, ValueError):
                updated_at = int(project.get("updatedAt") or 0)
        recency = max(
            project_recency.get(project_id, (0, 0, 0, 0)),
            (updated_at, updated_at, updated_at, 0),
        )
        entries.append((recency, f"codex:project:{project_id}"))

    entries.sort(key=lambda item: item[0], reverse=True)
    return merge_unique([item_key for _, item_key in entries], [])


def merge_global_state_values(
    left: RootSnapshot, right: RootSnapshot, clones: list[CloneSpec]
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    # Prefer Plus ordering/settings on ties because it is the newer schema.
    for key in LIST_STATE_KEYS:
        right_value = right.global_state.get(key)
        left_value = left.global_state.get(key)
        merged[key] = merge_unique(
            right_value if isinstance(right_value, list) else [],
            left_value if isinstance(left_value, list) else [],
        )
    for key in DICT_STATE_KEYS:
        left_value = left.global_state.get(key)
        right_value = right.global_state.get(key)
        value: dict[str, Any] = {}
        if isinstance(left_value, dict):
            value.update(left_value)
        if isinstance(right_value, dict):
            for item_key, item_value in right_value.items():
                if item_key not in value:
                    value[item_key] = item_value
                elif key == "local-projects":
                    old_updated = int((value[item_key] or {}).get("updatedAt") or 0)
                    new_updated = int((item_value or {}).get("updatedAt") or 0)
                    if new_updated >= old_updated:
                        value[item_key] = item_value
                else:
                    chosen = choose_thread_source(str(item_key), left, right)
                    if chosen is not None and chosen[0] is right:
                        value[item_key] = item_value
        merged[key] = value

    projectless = list(merged.get("projectless-thread-ids", []))
    assignments = merged.get("thread-project-assignments", {})
    if isinstance(assignments, dict):
        projectless = [item for item in projectless if item not in assignments]

    # A database/session copy alone is not enough for every historical chat to
    # appear in the desktop sidebar.  Ensure each unarchived top-level thread,
    # including CLI/API-origin threads, has a sidebar container assignment.
    main_candidates = collect_main_candidates(left, right, clones)
    assigned_ids = set(assignments) if isinstance(assignments, dict) else set()
    for session_id, _ in main_candidates:
        if session_id not in assigned_ids and session_id not in projectless:
            projectless.append(session_id)

    for clone in clones:
        if clone.new_id not in projectless:
            projectless.append(clone.new_id)
        hints = merged.setdefault("thread-workspace-root-hints", {})
        source_state = left.global_state if clone.source_root == left.root else right.global_state
        source_hints = source_state.get("thread-workspace-root-hints", {})
        if isinstance(hints, dict) and isinstance(source_hints, dict):
            if clone.old_id in source_hints:
                hints[clone.new_id] = source_hints[clone.old_id]
    merged["projectless-thread-ids"] = projectless
    return merged


def set_project_sidebar_mode(state: dict[str, Any]) -> None:
    """Preserve project folders while keeping chats ordered by last update."""
    atom_state = state.get(ATOM_STATE_KEY)
    if not isinstance(atom_state, dict):
        atom_state = {}
    else:
        atom_state = dict(atom_state)

    preferences = atom_state.get(SIDEBAR_PREFERENCES_KEY)
    if not isinstance(preferences, dict):
        preferences = {}
    else:
        preferences = dict(preferences)

    preferences.update(
        {
            "initialized": True,
            "mode": "project",
            "chatSortMode": "updated_at",
            "projectSortMode": "updated_at",
        }
    )
    atom_state[SIDEBAR_PREFERENCES_KEY] = preferences
    state[ATOM_STATE_KEY] = atom_state


def remove_managed_sidebar_section(state: dict[str, Any]) -> None:
    """Remove the custom all-chats section created by older sync-tool versions."""
    atom_state = state.get(ATOM_STATE_KEY)
    if not isinstance(atom_state, dict):
        return
    atom_state = dict(atom_state)

    existing_by_account = atom_state.get(CUSTOM_SECTIONS_KEY)
    if not isinstance(existing_by_account, dict):
        return

    by_account: dict[str, Any] = {}
    for account_id, existing in existing_by_account.items():
        if not isinstance(existing, dict):
            by_account[str(account_id)] = existing
            continue
        sections = existing.get("sections")
        if not isinstance(sections, list):
            sections = []
        sections = [
            section
            for section in sections
            if isinstance(section, dict) and section.get("id") != MANAGED_SECTION_ID
        ]

        section_order = existing.get("sectionOrder")
        if not isinstance(section_order, list):
            section_order = []
        section_order = [
            str(item) for item in section_order if str(item) != MANAGED_SECTION_ORDER_KEY
        ]

        collapsed = existing.get("collapsedSectionIds")
        if not isinstance(collapsed, list):
            collapsed = []
        collapsed = [str(item) for item in collapsed if str(item) != MANAGED_SECTION_ID]

        by_account[str(account_id)] = {
            **existing,
            "sections": sections,
            "collapsedSectionIds": collapsed,
            "sectionOrder": section_order,
        }

    atom_state[CUSTOM_SECTIONS_KEY] = by_account
    state[ATOM_STATE_KEY] = atom_state


def apply_global_state(
    root: Path,
    merged_values: dict[str, Any],
    main_candidates: list[tuple[str, dict[str, Any]]],
    account_ids: set[str],
) -> None:
    path = root / ".codex-global-state.json"
    for _ in range(12):
        before = path.stat() if path.exists() else None
        current = read_json_retry(path)
        for key, value in merged_values.items():
            current[key] = value
        set_project_sidebar_mode(current)
        remove_managed_sidebar_section(current)
        after = path.stat() if path.exists() else None
        if before is not None and after is not None and (
            before.st_mtime_ns,
            before.st_size,
        ) != (after.st_mtime_ns, after.st_size):
            time.sleep(0.05)
            continue
        atomic_write_json(path, current)
        return
    raise SyncError(f"全局状态持续变化，无法安全写入：{path}")


def parse_index_timestamp(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        return dt.datetime.fromisoformat(text).timestamp()
    return 0.0


def timestamp_from_row(row: dict[str, Any]) -> str:
    milliseconds = int(row.get("updated_at_ms") or 0)
    seconds = milliseconds / 1000 if milliseconds else int(row.get("updated_at") or 0)
    if not seconds:
        return iso_now()
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def sync_global_state_and_index(
    left: RootSnapshot,
    right: RootSnapshot,
    clones: list[CloneSpec],
) -> tuple[int, int, set[str]]:
    merged_values = merge_global_state_values(left, right, clones)
    main_candidates = collect_main_candidates(left, right, clones)
    account_ids = (
        read_sidebar_account_ids(left.root)
        | read_sidebar_account_ids(right.root)
        | custom_section_account_ids(left.global_state)
        | custom_section_account_ids(right.global_state)
    )
    for root in (left.root, right.root):
        apply_global_state(root, merged_values, main_candidates, account_ids)

    merged_index: dict[str, dict[str, Any]] = {}
    for source_index in (left.session_index, right.session_index):
        for session_id, item in source_index.items():
            existing = merged_index.get(session_id)
            if existing is None or parse_index_timestamp(item.get("updated_at")) >= parse_index_timestamp(
                existing.get("updated_at")
            ):
                merged_index[session_id] = dict(item)

    visible_ids = list(merged_values.get("projectless-thread-ids", []))
    assignments = merged_values.get("thread-project-assignments", {})
    if isinstance(assignments, dict):
        visible_ids += list(assignments)
    for session_id in merge_unique(visible_ids, []):
        chosen = choose_thread_source(str(session_id), left, right)
        clone = next((item for item in clones if item.new_id == session_id), None)
        if clone is not None:
            source_snapshot = left if clone.source_root == left.root else right
            original = source_snapshot.threads.get(clone.old_id)
            if original is not None:
                row = make_clone_row(original, clone)
                merged_index[str(session_id)] = {
                    "id": str(session_id),
                    "thread_name": row.get("title") or row.get("name") or "同步冲突副本",
                    "updated_at": timestamp_from_row(row),
                }
            continue
        if chosen is None:
            continue
        _, row = chosen
        candidate = {
            "id": str(session_id),
            "thread_name": row.get("name") or row.get("title") or row.get("preview") or "Codex 对话",
            "updated_at": timestamp_from_row(row),
        }
        existing = merged_index.get(str(session_id))
        if existing is None or parse_index_timestamp(candidate["updated_at"]) >= parse_index_timestamp(
            existing.get("updated_at")
        ):
            merged_index[str(session_id)] = candidate

    valid_ids = set(left.threads) | set(right.threads) | {
        clone.new_id for clone in clones
    }
    merged_index = {
        session_id: item
        for session_id, item in merged_index.items()
        if session_id in valid_ids
    }
    for root in (left.root, right.root):
        write_session_index(root / "session_index.jsonl", merged_index)
    return len(merged_index), len(set(visible_ids)), account_ids


def is_auxiliary_thread_source(source: Any) -> bool:
    text = str(source or "").strip()
    if text.lower().startswith("subagent"):
        return True
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(text)
        return isinstance(parsed, dict) and "subagent" in parsed
    return False


def managed_section_coverage(
    global_state: dict[str, Any],
    account_ids: set[str],
    main_thread_ids: set[str],
    assignments: dict[str, Any],
) -> dict[str, set[str]]:
    atom_state = global_state.get(ATOM_STATE_KEY)
    by_account = (
        atom_state.get(CUSTOM_SECTIONS_KEY)
        if isinstance(atom_state, dict)
        else None
    )
    if not isinstance(by_account, dict):
        return {account_id: set() for account_id in account_ids}

    result: dict[str, set[str]] = {}
    for account_id in account_ids:
        account_state = by_account.get(account_id)
        sections = (
            account_state.get("sections") if isinstance(account_state, dict) else None
        )
        if not isinstance(sections, list):
            result[account_id] = set()
            continue
        managed = next(
            (
                section
                for section in sections
                if isinstance(section, dict) and section.get("id") == MANAGED_SECTION_ID
            ),
            None,
        )
        item_keys = managed.get("itemKeys") if isinstance(managed, dict) else None
        if not isinstance(item_keys, list):
            result[account_id] = set()
            continue

        direct_threads = {
            str(item)[len("codex:thread:local:") :]
            for item in item_keys
            if str(item).startswith("codex:thread:local:")
        }
        project_ids = {
            str(item)[len("codex:project:") :]
            for item in item_keys
            if str(item).startswith("codex:project:")
        }
        covered = set(direct_threads)
        covered.update(
            thread_id
            for thread_id, assignment in assignments.items()
            if assignment_project_id(assignment) in project_ids
        )
        result[account_id] = covered & main_thread_ids
    return result


def verify_roots(
    roots: list[Path],
    expected_sidebar_account_ids: set[str] | None = None,
    root_providers: dict[Path, str] | None = None,
) -> dict[str, Any]:
    roots = [root.resolve() for root in roots]
    if root_providers is None:
        if len(roots) != 2:
            raise SyncError("多目录校验必须显式提供每个目录的模型提供商。")
        root_providers = {
            roots[0]: COCKPIT_MODEL_PROVIDER,
            roots[1]: PLUS_MODEL_PROVIDER,
        }
    else:
        root_providers = {
            root.resolve(): provider for root, provider in root_providers.items()
        }
    result: dict[str, Any] = {}
    id_sets: list[set[str]] = []
    expected_sidebar_account_ids = expected_sidebar_account_ids or set()
    for root in roots:
        connection = sqlite3.connect(
            f"file:{root / 'state_5.sqlite'}?mode=ro", uri=True, timeout=30
        )
        try:
            rows = connection.execute(
                "SELECT id, rollout_path, source, archived, model_provider FROM threads"
            ).fetchall()
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
        db_ids = {str(row[0]) for row in rows}
        main_thread_ids = {
            str(row[0]) for row in rows if not is_auxiliary_thread_source(row[2])
        }
        unarchived_main_thread_ids = {
            str(row[0])
            for row in rows
            if not is_auxiliary_thread_source(row[2]) and int(row[3] or 0) == 0
        }
        missing_rollouts = [str(row[1]) for row in rows if not Path(str(row[1])).is_file()]
        expected_provider = provider_for_root(root, root_providers)
        wrong_provider_ids = {str(row[0]) for row in rows if str(row[4]) != expected_provider}
        preferred_paths = {str(row[0]): str(row[1] or "") for row in rows}
        all_sessions = scan_sessions(root, preferred_paths)
        orphan_session_ids = set(all_sessions) - db_ids
        sessions = {
            session_id: session
            for session_id, session in all_sessions.items()
            if session_id in db_ids
        }
        session_ids = set(sessions)
        wrong_rollout_provider_ids: set[str] = set()
        for session_id, session in sessions.items():
            try:
                with session.path.open("r", encoding="utf-8") as handle:
                    header = json.loads(handle.readline())
                payload = header.get("payload") if isinstance(header, dict) else None
                if not isinstance(payload, dict) or payload.get("model_provider") != expected_provider:
                    wrong_rollout_provider_ids.add(session_id)
            except (OSError, json.JSONDecodeError):
                wrong_rollout_provider_ids.add(session_id)
        global_state = read_json_retry(root / ".codex-global-state.json")
        projectless_ids = {
            str(item) for item in global_state.get("projectless-thread-ids", [])
        }
        assignments = global_state.get("thread-project-assignments", {})
        assigned_ids = set(assignments) if isinstance(assignments, dict) else set()
        local_projects = global_state.get("local-projects", {})
        project_ids = set(local_projects) if isinstance(local_projects, dict) else set()
        assignment_values = assignments.values() if isinstance(assignments, dict) else []
        unresolved_project_thread_ids = sorted(assigned_ids - db_ids)
        unresolved_project_ids = sorted(
            {
                project_id
                for assignment in assignment_values
                if (project_id := assignment_project_id(assignment)) is not None
                and project_id not in project_ids
            }
        )
        project_assigned_main_ids = assigned_ids & unarchived_main_thread_ids
        pinned_ids = {
            str(item) for item in global_state.get("pinned-thread-ids", [])
        }
        sidebar_ids = projectless_ids | assigned_ids | pinned_ids
        missing_sidebar_main_ids = sorted(unarchived_main_thread_ids - sidebar_ids)
        atom_state = global_state.get(ATOM_STATE_KEY, {})
        preferences = (
            atom_state.get(SIDEBAR_PREFERENCES_KEY, {})
            if isinstance(atom_state, dict)
            else {}
        )
        sidebar_mode = preferences.get("mode") if isinstance(preferences, dict) else None
        sidebar_sort_mode = (
            preferences.get("chatSortMode") if isinstance(preferences, dict) else None
        )
        project_sort_mode = (
            preferences.get("projectSortMode") if isinstance(preferences, dict) else None
        )
        managed_section_count = 0
        if isinstance(atom_state, dict):
            custom_by_account = atom_state.get(CUSTOM_SECTIONS_KEY)
            if isinstance(custom_by_account, dict):
                for account_state in custom_by_account.values():
                    sections = (
                        account_state.get("sections")
                        if isinstance(account_state, dict)
                        else None
                    )
                    if isinstance(sections, list):
                        managed_section_count += sum(
                            1
                            for section in sections
                            if isinstance(section, dict)
                            and section.get("id") == MANAGED_SECTION_ID
                        )
        id_sets.append(db_ids)
        result[str(root)] = {
            "threads": len(db_ids),
            "main_threads": len(main_thread_ids),
            "unarchived_main_threads": len(unarchived_main_thread_ids),
            "auxiliary_sessions": len(db_ids - main_thread_ids),
            "session_files": len(session_ids),
            "orphan_session_files": len(orphan_session_ids),
            "db_matches_sessions": db_ids == session_ids,
            "missing_rollout_paths": len(missing_rollouts),
            "expected_model_provider": expected_provider,
            "provider_visible_threads": len(db_ids - wrong_provider_ids),
            "provider_visible_main_threads": len(
                unarchived_main_thread_ids - wrong_provider_ids
            ),
            "wrong_model_provider_threads": len(wrong_provider_ids),
            "wrong_rollout_provider_sessions": len(wrong_rollout_provider_ids),
            "integrity": integrity,
            "sidebar_visible_main_threads": len(
                unarchived_main_thread_ids & sidebar_ids
            ),
            "missing_sidebar_main_threads": len(missing_sidebar_main_ids),
            "sidebar_mode": sidebar_mode,
            "sidebar_sort_mode": sidebar_sort_mode,
            "project_sort_mode": project_sort_mode,
            "projects": len(project_ids),
            "project_assigned_main_threads": len(project_assigned_main_ids),
            "unresolved_project_threads": len(unresolved_project_thread_ids),
            "unresolved_projects": len(unresolved_project_ids),
            "removed_sync_custom_sections": managed_section_count == 0,
        }
        if (
            integrity.lower() != "ok"
            or missing_rollouts
            or wrong_provider_ids
            or wrong_rollout_provider_ids
            or db_ids != session_ids
            or missing_sidebar_main_ids
            or unresolved_project_thread_ids
            or unresolved_project_ids
            or sidebar_mode != "project"
            or sidebar_sort_mode != "updated_at"
            or project_sort_mode != "updated_at"
            or managed_section_count
        ):
            raise SyncError(f"同步后校验失败：{root}；{result[str(root)]}")
    result["same_thread_ids"] = bool(id_sets) and all(
        current == id_sets[0] for current in id_sets[1:]
    )
    if not result["same_thread_ids"]:
        raise SyncError("同步后各数据目录的会话 ID 集仍不一致。")
    return result


def run_sync(
    left_root: Path,
    right_root: Path,
    backup_base: Path,
    left_provider: str = COCKPIT_MODEL_PROVIDER,
    right_provider: str = PLUS_MODEL_PROVIDER,
) -> dict[str, Any]:
    started = utc_now()
    roots = [left_root.resolve(), right_root.resolve()]
    if roots[0] == roots[1]:
        raise SyncError("两套 Codex 数据目录不能相同。")
    if not left_provider or not right_provider:
        raise SyncError("同步两端都必须指定非空的模型提供商。")
    root_providers = {
        roots[0]: left_provider,
        roots[1]: right_provider,
    }
    for root in roots:
        validate_root(root)

    backup_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-history-sync-", dir=INSTALL_DIR / "work") as raw_temp:
        temp_dir = Path(raw_temp)
        backup_dir = create_backup(roots, backup_base, temp_dir)
        left = load_root_snapshot(roots[0])
        right = load_root_snapshot(roots[1])
        duplicate_plan, duplicate_warnings = find_legacy_exact_clones(left, right)
        duplicate_cleanup = purge_legacy_exact_clones(
            roots, duplicate_plan, backup_dir
        )
        if duplicate_plan:
            # Never continue with stale snapshots: otherwise the just-removed
            # IDs would be selected and written back into both databases.
            left = load_root_snapshot(roots[0])
            right = load_root_snapshot(roots[1])

        target_paths, clones, counters, sync_warnings = sync_session_files(
            left, right, backup_dir, temp_dir, root_providers
        )
        integrity = sync_databases(
            left, right, target_paths, clones, root_providers
        )
        index_entries, visible_threads, sidebar_account_ids = sync_global_state_and_index(
            left, right, clones
        )
        verification = verify_roots(
            roots, sidebar_account_ids, root_providers
        )
        verified_left = load_root_snapshot(roots[0])
        verified_right = load_root_snapshot(roots[1])
        remaining_duplicates, verification_warnings = find_legacy_exact_clones(
            verified_left, verified_right
        )
        verification["legacy_exact_conflict_duplicates"] = len(
            remaining_duplicates
        )
        if remaining_duplicates:
            raise SyncError(
                "同步后仍存在精确重复的冲突副本："
                + ", ".join(item.duplicate_id for item in remaining_duplicates)
            )
        rotate_backups(backup_base)

    finished = utc_now()
    return {
        "status": "ok",
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": finished.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "cockpit_root": str(roots[0]),
        "plus_root": str(roots[1]),
        "left_root": str(roots[0]),
        "right_root": str(roots[1]),
        "left_provider": left_provider,
        "right_provider": right_provider,
        "backup_dir": str(backup_dir),
        "new_files": counters["new_files"],
        "updated_files": counters["updated_files"],
        "unchanged_files": counters["unchanged"],
        "conflicts_preserved": counters["conflicts"],
        "exact_duplicates_removed": duplicate_cleanup["threads"],
        "duplicate_main_threads_removed": duplicate_cleanup["main_threads"],
        "duplicate_auxiliary_threads_removed": duplicate_cleanup[
            "auxiliary_threads"
        ],
        "duplicate_session_files_removed": duplicate_cleanup["files"],
        "index_entries": index_entries,
        "visible_top_level_threads": visible_threads,
        "removed_sync_custom_sections": True,
        "sidebar_mode": "project",
        "integrity": integrity,
        "verification": verification,
        "warnings": duplicate_warnings + sync_warnings + verification_warnings,
    }


def write_last_result(result: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        atomic_write_json(LAST_RESULT_PATH, result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="双向同步 Cockpit 与 Plus 的本地 Codex 对话")
    parser.add_argument("--left", type=Path, default=DEFAULT_LEFT, help="Cockpit Codex 数据目录")
    parser.add_argument("--right", type=Path, default=DEFAULT_RIGHT, help="Plus Codex 数据目录")
    parser.add_argument(
        "--backup-base", type=Path, default=DEFAULT_BACKUP_BASE, help="同步备份目录"
    )
    parser.add_argument(
        "--left-provider",
        default=COCKPIT_MODEL_PROVIDER,
        help="左侧 Codex 配置使用的模型提供商 ID",
    )
    parser.add_argument(
        "--right-provider",
        default=PLUS_MODEL_PROVIDER,
        help="右侧 Codex 配置使用的模型提供商 ID",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def main() -> int:
    log_path = setup_logging()
    args = build_parser().parse_args()
    result: dict[str, Any]
    try:
        with SingleInstanceLock(LOCK_PATH):
            logging.info("开始同步：%s <-> %s", args.left, args.right)
            result = run_sync(
                args.left,
                args.right,
                args.backup_base,
                args.left_provider,
                args.right_provider,
            )
            result["log_path"] = str(log_path)
            logging.info("同步完成：%s", result)
            write_last_result(result)
            if args.json:
                print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        result = {
            "status": "error",
            "finished_at": iso_now(),
            "error": str(exc),
            "log_path": str(log_path),
        }
        logging.error("同步失败：%s\n%s", exc, traceback.format_exc())
        write_last_result(result)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
