"""Read-only-first audit and recoverable repair of legacy launch-sync history.

Running without --apply never changes profile files, caches, databases or locks.
Reports contain identifiers, paths, sizes and counts, never conversation text.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


INSTALL_DIR = Path(__file__).resolve().parent
DEFAULT_ROOTS = tuple(Path.home() / name for name in (
    ".codex-personal", ".codex-plus", ".codex-sota"))
STORAGE = ("sessions", "archived_sessions")
CONFLICT_SUFFIX = "\uff08\u540c\u6b65\u51b2\u7a81\u526f\u672c\uff09"
UUID_PATTERN = (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Codex's own paginated pages append _<physical-page-id> to the logical ID.
# These are canonical too and must not be treated as broken legacy sync files.
CANONICAL = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    + UUID_PATTERN + r"(?:_" + UUID_PATTERN + r")?\.jsonl$")


class RepairError(RuntimeError):
    pass


class PlanDrift(RepairError):
    pass


class RepairDeferred(RepairError):
    pass


def plain_path(value: str | Path) -> Path:
    value = str(value)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def checked_root(root: Path) -> Path:
    root = plain_path(root).resolve(strict=True)
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise RepairError("A dedicated Codex profile directory is required")
    if not (root / "state_5.sqlite").is_file():
        raise RepairError("Profile has no state_5.sqlite: " + str(root))
    return root


def physical_id(path: Path) -> str | None:
    try:
        return str(uuid.UUID(path.stem[-36:]))
    except ValueError:
        return None


def family_name(name: str) -> str:
    while name.endswith(CONFLICT_SUFFIX):
        name = name[:-len(CONFLICT_SUFFIX)]
    return name


def read_rows(root: Path) -> dict[str, dict]:
    with closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro",
                                 uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        return {str(row["id"]): dict(row) for row in db.execute("SELECT * FROM threads")}


def read_inventory(root: Path) -> tuple[dict[Path, dict], list[dict]]:
    pages, errors = {}, []
    for storage in STORAGE:
        directory = root / storage
        if not directory.exists():
            continue
        if directory.resolve() != directory or directory.is_symlink():
            raise RepairError("Linked history storage is not supported: " + str(directory))
        for path in sorted(directory.rglob("*.jsonl")):
            try:
                resolved = path.resolve(strict=True)
                if resolved != path or not resolved.is_relative_to(root):
                    raise RepairError("History path escapes its profile: " + str(path))
                with path.open("rb") as stream:
                    header = json.loads(stream.readline())
                meta = header.get("payload") if isinstance(header, dict) else None
                if not isinstance(meta, dict) or header.get("type") != "session_meta":
                    raise ValueError("invalid session metadata")
                stat = path.stat()
                pages[path] = {"owner": meta.get("id") or meta.get("session_id"),
                               "page_id": physical_id(path), "metadata": meta,
                               "timestamp": header.get("timestamp"),
                               "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            except (OSError, ValueError, RepairError) as exc:
                errors.append({"path": str(path), "error_type": type(exc).__name__})
    return pages, errors


def fingerprint(path: Path) -> dict:
    """A stable read, including bytes, for compare-before-write protection."""
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RepairError("File changed during review: " + str(path))
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest}


def canonical_name(path: Path, page: dict) -> str:
    if CANONICAL.fullmatch(path.name):
        return path.name
    page_id = page["page_id"]
    if not page_id:
        raise RepairError("A physical page UUID is required: " + str(path))
    try:
        owner = str(uuid.UUID(str(page["owner"])))
        stamp = str(page["metadata"].get("timestamp") or page["timestamp"])
        timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise RepairError("Cannot safely determine canonical metadata: " + str(path)) from exc
    suffix = owner if owner == page_id else owner + "_" + page_id
    return "rollout-" + timestamp.strftime("%Y-%m-%dT%H-%M-%S-") + suffix + ".jsonl"


def inside(root: Path, path: Path) -> Path:
    resolved = plain_path(path).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RepairError("Target is outside its exact profile: " + str(path))
    if resolved != path:
        raise RepairError("Linked or non-normalized target is not supported: " + str(path))
    return resolved


PATH_FIELDS = frozenset({"rollout_path", "rolloutPath", "rollout_file_path",
                         "source_rollout_path", "base_rollout_path", "history_path"})


def path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(plain_path(value)))


def replace_path_pointers(value, mapping: dict[str, str]):
    """Change typed path pointers, never free-form messages containing a path."""
    if isinstance(value, dict):
        return {key: mapping.get(path_key(item), item)
                if key in PATH_FIELDS and isinstance(item, str)
                else replace_path_pointers(item, mapping)
                for key, item in value.items()}
    if isinstance(value, list):
        return [replace_path_pointers(item, mapping) for item in value]
    return value


def active_codex_processes() -> list[dict]:
    """Fail closed if the process list is unavailable; no kill or bypass option."""
    if os.name != "nt":
        result = subprocess.run(["ps", "-A", "-o", "pid=,comm="],
                                capture_output=True, text=True, timeout=15, check=True)
        return [{"pid": int(line.strip().split(None, 1)[0]), "name": line.strip().split(None, 1)[1]}
                for line in result.stdout.splitlines()
                if len(line.strip().split(None, 1)) == 2
                and Path(line.strip().split(None, 1)[1]).name.casefold() in {"codex", "codex-app"}]
    result = subprocess.run(["tasklist.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, errors="replace", timeout=15,
                            check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or any(len(row) < 2 or not row[1].isdigit() for row in rows):
        raise RepairError("Cannot verify that Codex has exited")
    return [{"pid": int(row[1]), "name": row[0]} for row in rows
            if row[0].casefold() in {"codex.exe", "codex-app.exe"}]


def require_codex_closed() -> None:
    import codex_app_lifecycle as lifecycle
    lifecycle.assert_quiescent()
    processes = active_codex_processes()
    if processes:
        raise RepairDeferred("Close Codex App and Codex CLI before applying or restoring history; "
                          "running process IDs: " + ",".join(str(p["pid"]) for p in processes))


def chain_paths(path: Path, pages: dict[Path, dict]) -> list[Path]:
    by_id = {page["page_id"]: p for p, page in pages.items()}
    result = []
    while path is not None:
        if path in result or path not in pages:
            raise RepairError("Cyclic or missing history while proving duplicate ancestry")
        result.append(path)
        base = pages[path]["metadata"].get("history_base")
        if not base:
            break
        path = by_id.get(str(base.get("thread_id")))
        if path is None:
            raise RepairError("Missing physical ancestor while comparing duplicates")
    return result


def exact_family_pairs(roots: list[Path], original_id: str) -> list[tuple[str, str]]:
    """A title selects candidates only. It is never accepted as duplicate proof."""
    uuid.UUID(original_id)
    candidates = set()
    for root in roots:
        rows = read_rows(checked_root(root))
        original = rows.get(original_id)
        if original is None or original.get("archived"):
            raise RepairError("The requested original must be active in every profile")
        name = family_name(str(original.get("title") or original.get("name") or ""))
        for thread_id, row in rows.items():
            title = str(row.get("title") or row.get("name") or "")
            if (thread_id != original_id and not row.get("archived")
                    and title.endswith(CONFLICT_SUFFIX) and family_name(title) == name):
                candidates.add(thread_id)
    return [(original_id, duplicate) for duplicate in sorted(candidates)]


def prove_exact_pairs(roots: list[Path], pairs: list[tuple[str, str]]) -> list[dict]:
    """Conservative, per-profile proof: same ancestry AND full effective history.

    Provider/self-ID and storage ordinals use the synchronizer's normalization;
    timestamps, instructions, user messages and tool outputs remain significant.
    Cross-profile equality is deliberately not assumed: each original retains
    its own profile's full branch. Prefixes and real branches are never archived.
    """
    import sync_codex_histories as core
    if len({duplicate for _, duplicate in pairs}) != len(pairs):
        raise RepairError("Each duplicate must have exactly one keeper")
    duplicates = {duplicate for _, duplicate in pairs}
    if any(keep in duplicates or keep == duplicate for keep, duplicate in pairs):
        raise RepairError("Duplicate archive chains are not supported")
    if os.environ.get("CODEX_THREAD_ID") in duplicates:
        raise RepairError("The currently executing conversation cannot be archived")
    result = [{"keep": keep, "duplicate": duplicate, "relation": "equal", "evidence_by_root": {}}
              for keep, duplicate in pairs]
    for raw_root in roots:
        root = checked_root(raw_root)
        rows = read_rows(root)
        pages, errors = read_inventory(root)
        if errors:
            raise RepairError("Malformed history prevents exact duplicate proof")
        referenced = {str((page["metadata"].get("history_base") or {}).get("thread_id"))
                      for page in pages.values() if page["metadata"].get("history_base")}
        guarded_owners = {page["owner"] for page in pages.values() if page["page_id"] in referenced}
        with closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            edge_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='thread_spawn_edges' AND type='table'").fetchone()
            if edge_table:
                for parent, child in db.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                    guarded_owners.update((parent, child))
        digests, signatures = {}, {}
        def evidence(thread_id):
            if thread_id not in rows:
                raise RepairError("An exact pair is absent from a requested profile: " + thread_id)
            path = plain_path(rows[thread_id]["rollout_path"])
            if path not in pages or pages[path]["owner"] != thread_id:
                raise RepairError("Thread does not point to its own physical head: " + thread_id)
            chain = chain_paths(path, pages)
            for page_path in chain:
                if page_path not in signatures:
                    signatures[page_path] = fingerprint(page_path)
            if thread_id not in digests:
                stat = path.stat()
                session = core.SessionFile(thread_id, path, path.relative_to(root), stat.st_size, stat.st_mtime_ns)
                digest, records = hashlib.sha256(), 0
                for line in core.normalized_session_lines(session):
                    digest.update(line)
                    records += 1
                digests[thread_id] = (digest.hexdigest(), records)
            return chain, digests[thread_id]
        for entry in result:
            keep, duplicate = entry["keep"], entry["duplicate"]
            if duplicate in guarded_owners:
                raise RepairError("A duplicate is a lineage/agent dependency: " + duplicate)
            if sum(page["owner"] == duplicate for page in pages.values()) != 1:
                raise RepairError("A clone with multiple physical pages requires separate branch review")
            keep_chain, keep_digest = evidence(keep)
            duplicate_chain, duplicate_digest = evidence(duplicate)
            if rows[keep].get("archived") or rows[duplicate].get("archived"):
                raise RepairError("Both members of an explicit cleanup pair must still be active")
            keep_title = family_name(str(rows[keep].get("title") or rows[keep].get("name") or ""))
            duplicate_title = str(rows[duplicate].get("title") or rows[duplicate].get("name") or "")
            if not duplicate_title.endswith(CONFLICT_SUFFIX) or family_name(duplicate_title) != keep_title:
                raise RepairError("Only explicitly marked members of the same conflict family are eligible")
            if not set(keep_chain).intersection(duplicate_chain):
                raise RepairError("No shared physical ancestry: preserve this unproven clone")
            if keep_digest != duplicate_digest:
                raise RepairError("A candidate has distinct or prefix-only history and must be preserved: " + duplicate)
            chain = sorted(set(keep_chain + duplicate_chain))
            entry["evidence_by_root"][str(root)] = {"normalized_sha256": keep_digest[0], "records": keep_digest[1],
                                                  "files": [{"path": str(p), "before": signatures[p]} for p in chain]}
        # Normalizing a large ancestor may take time; reject concurrent changes
        # before the proof is ever put into a saved plan.
        if any(fingerprint(path) != before for path, before in signatures.items()):
            raise PlanDrift("History changed while establishing duplicate proof")
    return result


def build_repair_plan(roots: list[Path], duplicate_pairs: list[dict] | None = None) -> dict:
    roots = [checked_root(root) for root in roots]
    if len(set(roots)) != len(roots) or len({root.name for root in roots}) != len(roots):
        raise RepairError("Profile roots and directory names must be distinct")
    if any(a != b and a.is_relative_to(b) for a in roots for b in roots):
        raise RepairError("Profile roots may not overlap")
    duplicate_pairs = duplicate_pairs or []
    duplicate_ids = {pair["duplicate"] for pair in duplicate_pairs}
    keeper_ids = {pair["keep"] for pair in duplicate_pairs}
    if any(pair.get("relation") != "equal" or not pair.get("evidence_by_root") for pair in duplicate_pairs):
        raise RepairError("Duplicate pairs require full per-profile exact-match evidence")
    if os.environ.get("CODEX_THREAD_ID") in duplicate_ids:
        raise RepairError("Refusing to archive the conversation executing this repair")
    profiles = []
    for root in roots:
        rows = read_rows(root)
        pages, errors = read_inventory(root)
        if errors:
            raise RepairError("Invalid history pages must be reviewed first: " + str(root))
        page_ids = [page["page_id"] for page in pages.values()]
        if len(page_ids) != len(set(page_ids)) or None in page_ids:
            raise RepairError("Ambiguous physical page IDs require separate repair: " + str(root))
        referenced = {str((page["metadata"].get("history_base") or {}).get("thread_id"))
                      for page in pages.values() if page["metadata"].get("history_base")}
        if referenced - set(page_ids):
            raise RepairError("Missing local pagination sources: " + str(root))
        operations = []
        for path, page in pages.items():
            archive = page["owner"] in duplicate_ids
            target = path.with_name(canonical_name(path, page))
            if archive:
                if page["page_id"] in referenced:
                    raise RepairError("A duplicate is a referenced history page; refusing archive")
                target = root / "archived_sessions" / "verified-launch-duplicates" / target.name
            if target == path:
                continue
            inside(root, path)
            inside(root, target)
            if target.exists():
                raise RepairError("Never overwrite an existing history destination: " + str(target))
            if physical_id(path) != physical_id(target):
                raise RepairError("A repair may not change a physical page ID")
            operations.append({"source": str(path), "destination": str(target),
                               "owner": page["owner"], "page_id": page["page_id"],
                               "quarantine": archive, "before": fingerprint(path)})
        if len({op["destination"] for op in operations}) != len(operations):
            raise RepairError("Multiple pages target the same canonical filename")
        mapping = {path_key(op["source"]): op["destination"] for op in operations}
        # Current Codex pagination uses IDs and byte cutoffs, not paths. Rewriting
        # a metadata header would shift descendants' byte cursors, so unknown
        # path-bearing metadata is blocked instead of silently corrupting it.
        for path, page in pages.items():
            if replace_path_pointers(page["metadata"], mapping) != page["metadata"]:
                raise RepairError("Path-bearing rollout metadata requires a separate cursor migration: " + str(path))
        affected = {key: {"rollout_path": row.get("rollout_path"), "archived": row.get("archived"),
                          "archived_at": row.get("archived_at")}
                    for key, row in rows.items()
                    if path_key(row.get("rollout_path") or "") in mapping or key in duplicate_ids | keeper_ids}
        caches = {name: fingerprint(root / name) for name in ("session_index.jsonl", ".codex-global-state.json")
                  if (root / name).is_file()}
        evidence = {}
        for pair in duplicate_pairs:
            if str(root) not in pair["evidence_by_root"]:
                raise RepairError("Duplicate proof does not cover every profile")
            for item in pair["evidence_by_root"][str(root)]["files"]:
                evidence[item["path"]] = item
        profiles.append({"root": str(root), "operations": operations, "rows_before": affected,
                         "caches_before": caches, "archive_ids": sorted(duplicate_ids & rows.keys()),
                         "comparison_evidence": list(evidence.values())})
    return {"version": 1, "scope": "codex-sota-launch-history-v1", "plan_id": str(uuid.uuid4()),
            "created_utc": datetime.now(timezone.utc).isoformat(), "archived_at": int(time.time()),
            "profiles": profiles, "duplicate_pairs": duplicate_pairs, "deleted_history_files": 0}


def refresh_plan_after_drift(plan: dict) -> dict:
    """Re-prove only the duplicate families represented by a stale queue.

    A normal sync may rename a reviewed rollout briefly while converging the
    three profiles. Rebuilding the entire cleanup scope would be too broad, so
    keepers from the saved plan are used as the explicit family boundary and
    every current member is proved again before a replacement queue is built.
    """
    roots = [Path(profile["root"]) for profile in plan["profiles"]]
    keepers = sorted({str(pair["keep"]) for pair in plan.get("duplicate_pairs", [])})
    if not keepers:
        raise PlanDrift("The stale queue contains no duplicate family to refresh")
    pairs = []
    for keeper in keepers:
        pairs.extend(exact_family_pairs(roots, keeper))
    if not pairs:
        raise PlanDrift("The reviewed duplicate family no longer has active exact candidates")
    return build_repair_plan(roots, prove_exact_pairs(roots, pairs))


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".history-repair-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_manifest(backup: Path, manifest: dict) -> None:
    atomic_bytes(backup / "manifest.json", (json.dumps(manifest, ensure_ascii=True, indent=2) + "\n").encode())


def sqlite_backup(source: Path, destination: Path) -> None:
    deadline = time.monotonic() + 60
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise RepairError("SQLite backup exceeded its bounded timeout")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=3)) as src, \
         closing(sqlite3.connect(destination, timeout=3)) as dst:
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RepairError("SQLite backup integrity check failed")


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def update_database_paths(db: sqlite3.Connection, mapping: dict[str, str]) -> int:
    count = 0
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        for _, column, *_ in db.execute("PRAGMA table_info(" + quote_identifier(table) + ")"):
            if column not in PATH_FIELDS:
                continue
            query = "SELECT DISTINCT " + quote_identifier(column) + " FROM " + quote_identifier(table)
            for (value,) in db.execute(query).fetchall():
                if not isinstance(value, str) or path_key(value) not in mapping:
                    continue
                count += db.execute("UPDATE " + quote_identifier(table) + " SET " + quote_identifier(column)
                                    + "=? WHERE " + quote_identifier(column) + "=?",
                                    (mapping[path_key(value)], value)).rowcount
    return count


def cache_replacements(root: Path, mapping: dict[str, str], archive_ids: set[str]) -> dict[Path, bytes]:
    replacements = {}
    for name in ("session_index.jsonl", ".codex-global-state.json"):
        path = root / name
        if not path.is_file():
            continue
        original = path.read_bytes()
        if name == "session_index.jsonl":
            output = []
            for line in original.decode("utf-8-sig").splitlines(keepends=True):
                if not line.strip():
                    output.append(line)
                    continue
                item = json.loads(line)
                if item.get("id") in archive_ids:
                    continue
                updated = replace_path_pointers(item, mapping)
                output.append(line if updated == item else json.dumps(updated, ensure_ascii=False, separators=(",", ":")) + "\n")
            content = "".join(output).encode("utf-8")
        else:
            state = json.loads(original.decode("utf-8-sig"))
            updated = replace_path_pointers(state, mapping)
            if archive_ids:
                import sync_codex_histories as core
                core.remove_archived_sidebar_entries(updated, archive_ids)
            content = (json.dumps(updated, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") if updated != state else original
        if content != original:
            replacements[path] = content
    return replacements


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps({key: value for key, value in plan.items() if key != "plan_sha256"},
                                    sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def save_plan(path: Path, plan: dict) -> None:
    """Explicit queue creation is outside profiles, never part of a plain dry run."""
    path = path.resolve()
    if any(path.is_relative_to(Path(profile["root"])) for profile in plan["profiles"]):
        raise RepairError("Save the repair queue outside the profiles")
    plan["plan_sha256"] = plan_digest(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write((json.dumps(plan, ensure_ascii=True, indent=2) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())


def validate_plan(plan: dict) -> None:
    if plan.get("version") != 1 or plan.get("scope") != "codex-sota-launch-history-v1":
        raise RepairError("Unsupported repair plan")
    uuid.UUID(plan["plan_id"])
    if plan.get("plan_sha256") and plan["plan_sha256"] != plan_digest(plan):
        raise PlanDrift("The saved plan has changed")
    roots = [checked_root(Path(profile["root"])) for profile in plan["profiles"]]
    if len(set(roots)) != len(roots) or len({root.name for root in roots}) != len(roots):
        raise RepairError("Duplicate profile roots in repair plan")
    allowed_duplicates = {pair["duplicate"] for pair in plan.get("duplicate_pairs", [])}
    for profile, root in zip(plan["profiles"], roots):
        if not set(profile["archive_ids"]) <= allowed_duplicates:
            raise RepairError("The plan contains an unverified archive operation")
        for op in profile["operations"]:
            source, target = inside(root, Path(op["source"])), inside(root, Path(op["destination"]))
            if source.relative_to(root).parts[0] not in STORAGE or target.relative_to(root).parts[0] not in STORAGE:
                raise RepairError("Only rollout storage files may be moved")
            if (not CANONICAL.fullmatch(target.name) or source.suffix != ".jsonl"
                    or physical_id(source) != physical_id(target) or physical_id(source) != op["page_id"]):
                raise RepairError("Invalid canonical rename in saved plan")
            if op["quarantine"]:
                expected = root / "archived_sessions" / "verified-launch-duplicates" / target.name
                if op["owner"] not in allowed_duplicates or target != expected:
                    raise RepairError("Invalid quarantine target")
            elif source.parent != target.parent:
                raise RepairError("A canonical rename may not change storage directories")
            if not re.fullmatch(r"[a-f0-9]{64}", op["before"]["sha256"]):
                raise RepairError("Missing exact file fingerprint")


def verify_targets(plan: dict, *, resuming: bool = False) -> None:
    validate_plan(plan)
    for profile in plan["profiles"]:
        root = Path(profile["root"])
        rows = read_rows(root)
        mapping = {path_key(op["source"]): op["destination"] for op in profile["operations"]}
        for op in profile["operations"]:
            source, target = Path(op["source"]), Path(op["destination"])
            if source.exists() and target.exists():
                raise PlanDrift("Both source and destination exist: " + str(source))
            current = source if source.exists() else target if resuming and target.exists() else None
            if current is None or fingerprint(current) != op["before"]:
                raise PlanDrift("Reviewed rollout changed or disappeared: " + str(source))
        for thread_id, before in profile["rows_before"].items():
            row = rows.get(thread_id)
            if row is None:
                raise PlanDrift("Reviewed thread row disappeared: " + thread_id)
            expected = dict(before)
            expected["rollout_path"] = mapping.get(path_key(before["rollout_path"]), before["rollout_path"])
            if thread_id in profile["archive_ids"]:
                expected.update(archived=1, archived_at=plan["archived_at"])
            current = {key: row.get(key) for key in before}
            if current != before and (not resuming or current != expected):
                raise PlanDrift("Reviewed thread state changed: " + thread_id)
        for evidence in profile.get("comparison_evidence", []):
            evidence_path = inside(root, Path(evidence["path"]))
            if not evidence_path.exists() and resuming and path_key(evidence_path) in mapping:
                evidence_path = Path(mapping[path_key(evidence_path)])
            if not evidence_path.exists() or fingerprint(evidence_path) != evidence["before"]:
                raise PlanDrift("Duplicate comparison source changed: " + evidence["path"])


def database_paths(root: Path) -> list[Path]:
    paths = [root / "state_5.sqlite"]
    path = root / "thread_history_1.sqlite"
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if any(column[1] in PATH_FIELDS for table in tables
                   for column in db.execute("PRAGMA table_info(" + quote_identifier(table) + ")")):
                paths.append(path)
    return paths


def prepare_backup(plan: dict, backup: Path) -> dict:
    backup = backup.resolve()
    if any(backup.is_relative_to(Path(profile["root"])) for profile in plan["profiles"]):
        raise RepairError("Backup must be outside every repaired profile")
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "preparing", "plan": plan, "cache_changes": [], "databases": [], "rollout_backups": []}
    write_manifest(backup, manifest)
    for profile in plan["profiles"]:
        root = Path(profile["root"])
        saved = backup / root.name
        saved.mkdir()
        for source in database_paths(root):
            sqlite_backup(source, saved / source.name)
            manifest["databases"].append({"source": str(source), "backup": str((saved / source.name).relative_to(backup))})
        for op in profile["operations"]:
            source = Path(op["source"])
            # Flat UUID backup names avoid another Windows MAX_PATH failure in
            # long profile paths and in deferred-attempt subdirectories.
            destination = saved / "rollouts" / (op["page_id"] + ".jsonl")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if fingerprint(destination) != op["before"]:
                raise PlanDrift("Rollout changed while making its backup")
            manifest["rollout_backups"].append({"source": str(source), "backup": str(destination.relative_to(backup)),
                                                "sha256": op["before"]["sha256"]})
        mapping = {path_key(op["source"]): op["destination"] for op in profile["operations"]}
        cache_before = {path: fingerprint(path) for path in (root / "session_index.jsonl", root / ".codex-global-state.json")
                        if path.is_file()}
        for path, content in cache_replacements(root, mapping, set(profile["archive_ids"])).items():
            if fingerprint(path) != cache_before[path]:
                raise PlanDrift("Cache changed while preparing its path-only transformation")
            original = path.read_bytes()
            shutil.copy2(path, saved / path.name)
            if (hashlib.sha256(original).hexdigest() != cache_before[path]["sha256"]
                    or fingerprint(saved / path.name)["sha256"] != cache_before[path]["sha256"]):
                raise PlanDrift("Cache changed while making its current-state backup")
            after_path = saved / (path.name + ".after")
            atomic_bytes(after_path, content)
            manifest["cache_changes"].append({"path": str(path), "backup": str((saved / path.name).relative_to(backup)),
                                              "after": str(after_path.relative_to(backup)),
                                              "before_sha256": hashlib.sha256(original).hexdigest(),
                                              "after_sha256": hashlib.sha256(content).hexdigest()})
    manifest["status"] = "prepared"
    write_manifest(backup, manifest)
    return manifest


def refresh_prepared_backup(plan: dict, backup: Path) -> dict:
    """No profile mutation has occurred in 'prepared'; preserve old snapshots and
    stage the latest caches/SQLite under a new attempt before a queued retry.
    """
    relative = Path("attempts") / uuid.uuid4().hex[:12]
    fresh = prepare_backup(plan, backup / relative)
    for entry in fresh["databases"]:
        entry["backup"] = str(relative / entry["backup"])
    for change in fresh["cache_changes"]:
        change["backup"] = str(relative / change["backup"])
        change["after"] = str(relative / change["after"])
    for entry in fresh["rollout_backups"]:
        entry["backup"] = str(relative / entry["backup"])
    fresh["attempt"] = str(relative)
    write_manifest(backup, fresh)
    return fresh


def verified_cache(path: Path, expected: set[str]) -> None:
    if not path.is_file() or fingerprint(path)["sha256"] not in expected:
        raise PlanDrift("A cache changed during offline repair: " + str(path))


def mutate_plan(plan: dict, backup: Path, manifest: dict) -> None:
    """All SQLite writes are transactional; a journal covers cross-file recovery."""
    with ExitStack() as stack:
        databases = {}
        for entry in manifest["databases"]:
            path = Path(entry["source"])
            db = stack.enter_context(closing(sqlite3.connect(path, timeout=3)))
            db.execute("BEGIN IMMEDIATE")
            databases[path] = db
        verify_targets(plan, resuming=manifest["status"] == "applying")
        require_codex_closed()
        manifest["status"] = "applying"
        write_manifest(backup, manifest)
        try:
            for profile in plan["profiles"]:
                root = Path(profile["root"])
                mapping = {path_key(op["source"]): op["destination"] for op in profile["operations"]}
                for op in profile["operations"]:
                    source, target = Path(op["source"]), Path(op["destination"])
                    if source.exists():
                        if fingerprint(source) != op["before"]:
                            raise PlanDrift("A rollout changed just before its rename")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(target)
                for path, db in databases.items():
                    if path.parent == root:
                        update_database_paths(db, mapping)
                db = databases[root / "state_5.sqlite"]
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                for thread_id in profile["archive_ids"]:
                    if "archived_at" in columns:
                        db.execute("UPDATE threads SET archived=1,archived_at=? WHERE id=?", (plan["archived_at"], thread_id))
                    else:
                        db.execute("UPDATE threads SET archived=1 WHERE id=?", (thread_id,))
            for change in manifest["cache_changes"]:
                path = Path(change["path"])
                verified_cache(path, {change["before_sha256"], change["after_sha256"]})
                content = (backup / change["after"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != change["after_sha256"]:
                    raise RepairError("Staged cache checksum mismatch")
                atomic_bytes(path, content)
            for db in databases.values():
                db.commit()
        except BaseException:
            for db in databases.values():
                db.rollback()
            raise


def rollback_manifest(backup: Path, manifest: dict, *, preserve_changed_caches: bool = False) -> None:
    plan = manifest["plan"]
    validate_plan(plan)
    # Check every recovery target before reverting any: unrelated new data wins.
    verify_targets(plan, resuming=True)
    safe_caches, changed_caches = [], []
    for change in manifest["cache_changes"]:
        try:
            verified_cache(Path(change["path"]), {change["before_sha256"], change["after_sha256"]})
        except PlanDrift:
            if not preserve_changed_caches:
                raise
            changed_caches.append(change["path"])
            continue
        if hashlib.sha256((backup / change["backup"]).read_bytes()).hexdigest() != change["before_sha256"]:
            raise RepairError("Backup cache checksum mismatch")
        safe_caches.append(change)
    for profile in plan["profiles"]:
        root = Path(profile["root"])
        inverse = {path_key(op["destination"]): op["source"] for op in profile["operations"]}
        for entry in manifest["databases"]:
            path = Path(entry["source"])
            if path.parent != root:
                continue
            with closing(sqlite3.connect(path, timeout=3)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                update_database_paths(db, inverse)
                if path.name == "state_5.sqlite":
                    columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                    for thread_id in profile["archive_ids"]:
                        before = profile["rows_before"][thread_id]
                        if "archived_at" in columns:
                            db.execute("UPDATE threads SET archived=?,archived_at=? WHERE id=?",
                                       (before["archived"], before["archived_at"], thread_id))
                        else:
                            db.execute("UPDATE threads SET archived=? WHERE id=?", (before["archived"], thread_id))
        for op in reversed(profile["operations"]):
            source, target = Path(op["source"]), Path(op["destination"])
            if target.exists():
                target.rename(source)
    for change in safe_caches:
        atomic_bytes(Path(change["path"]), (backup / change["backup"]).read_bytes())
    manifest["status"] = "rolled_back"
    if changed_caches:
        # Do not let an unrelated cache write strand a renamed rollout after
        # SQLite has already rolled back. Preserve that cache for review.
        manifest["preserved_changed_caches"] = changed_caches
    write_manifest(backup, manifest)


def apply_plan(plan: dict, backup: Path | None = None) -> dict:
    import codex_app_lifecycle as lifecycle
    import sync_codex_histories as core
    validate_plan(plan)
    backup = (backup or INSTALL_DIR / "backups" / "launch-history-repair" / plan["plan_id"]).resolve()
    with lifecycle.lifecycle_lock(), core.SingleInstanceLock(core.LOCK_PATH):
        require_codex_closed()
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
        if manifest is not None:
            if plan_digest(manifest["plan"]) != plan_digest(plan):
                raise RepairError("Backup belongs to a different reviewed plan")
            if manifest["status"] == "applied":
                # The durable receipt was written only after verification. A
                # later user continuation is allowed to change these files;
                # completing queue bookkeeping must never replay the repair.
                return {"status": "already-applied", "backup": str(backup), "plan_id": plan["plan_id"]}
            if manifest["status"] not in {"prepared", "applying", "rolled_back"}:
                raise RepairError("Existing backup needs review; status=" + manifest["status"])
        verify_targets(plan, resuming=manifest is not None and manifest["status"] == "applying")
        if manifest is None:
            manifest = prepare_backup(plan, backup)
        elif manifest["status"] in {"prepared", "rolled_back"}:
            manifest = refresh_prepared_backup(plan, backup)
        try:
            mutate_plan(plan, backup, manifest)
            verify_targets(plan, resuming=True)
        except BaseException:
            if manifest["status"] == "prepared":
                # A launch request can arrive during a backup. Nothing has
                # moved yet, so leave the queue retryable, without a rollback.
                raise
            try:
                rollback_manifest(backup, manifest, preserve_changed_caches=True)
            except BaseException as recovery_error:
                manifest["status"] = "recovery_required"
                manifest["recovery_error_type"] = type(recovery_error).__name__
                write_manifest(backup, manifest)
            raise
        manifest["status"] = "applied"
        write_manifest(backup, manifest)
        return {"status": "applied", "backup": str(backup), "plan_id": plan["plan_id"],
                "renamed_files": sum(len(p["operations"]) for p in plan["profiles"]),
                "archived_rows": sum(len(p["archive_ids"]) for p in plan["profiles"]),
                "deleted_history_files": 0}


def rollback_backup(backup: Path) -> dict:
    import codex_app_lifecycle as lifecycle
    import sync_codex_histories as core
    backup = backup.resolve(strict=True)
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    with lifecycle.lifecycle_lock(), core.SingleInstanceLock(core.LOCK_PATH):
        require_codex_closed()
        rollback_manifest(backup, manifest)
    return {"status": "rolled_back", "backup": str(backup), "plan_id": manifest["plan"]["plan_id"]}


def audit_root(root: Path) -> dict:
    root = checked_root(root)
    rows = read_rows(root)
    pages, errors = read_inventory(root)
    family_ids = defaultdict(list)
    for thread_id, row in rows.items():
        family_ids[family_name(str(row.get("title") or row.get("name") or ""))].append(thread_id)
    conflict_ids = {thread_id for thread_id, row in rows.items()
                    if str(row.get("title") or row.get("name") or "").endswith(CONFLICT_SUFFIX)}
    families = []
    for name, ids in family_ids.items():
        clones = sorted(set(ids) & conflict_ids)
        if clones:
            families.append({"family_digest": hashlib.sha256(name.encode()).hexdigest(),
                             "original_ids": sorted(set(ids) - conflict_ids),
                             "conflict_rows": len(clones), "conflict_ids": clones})
    page_ids = Counter(page["page_id"] for page in pages.values() if page["page_id"])
    references = Counter(str((page["metadata"].get("history_base") or {}).get("thread_id"))
                         for page in pages.values() if page["metadata"].get("history_base"))
    malformed = [{"path": str(path), "page_id": page["page_id"],
                  "owner": page["owner"], "size": page["size"]}
                 for path, page in pages.items() if not CANONICAL.fullmatch(path.name)]
    return {"root": str(root), "thread_rows": len(rows),
            "conflict_rows": len(conflict_ids),
            "active_conflict_rows": sum(not rows[key].get("archived") for key in conflict_ids),
            "session_files": len(pages), "noncanonical_files": len(malformed),
            "legacy_conflict_files": sum(path.name.startswith("rollout-conflict-") for path in pages),
            "missing_rollout_rows": sum(not plain_path(row.get("rollout_path") or "").is_file()
                                        for row in rows.values()),
            "duplicate_physical_ids": {key: count for key, count in page_ids.items() if count > 1},
            "missing_base_ids": sorted(references.keys() - page_ids.keys()),
            "invalid_files": errors, "families": sorted(families, key=lambda value: -value["conflict_rows"]),
            "noncanonical": malformed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", type=Path, nargs="+", default=list(DEFAULT_ROOTS))
    parser.add_argument("--apply", action="store_true", help="Apply an exact reviewed plan only while Codex is closed")
    parser.add_argument("--save-plan", type=Path, help="Explicitly save a one-time queue outside the profiles")
    parser.add_argument("--execute-plan", type=Path, help="Execute this exact queue, refusing target drift")
    parser.add_argument("--pair", action="append", default=[], metavar="KEEP_ID:DUPLICATE_ID",
                        help="Prove and quarantine this exact same-ancestry clone, never a prefix or real branch")
    parser.add_argument("--exact-family", action="append", default=[], metavar="ORIGINAL_ID",
                        help="Select marked active clones of this original, then require exact proof for every one")
    parser.add_argument("--backup", type=Path, help="New dedicated backup directory; default uses the plan UUID")
    parser.add_argument("--rollback", type=Path, help="Recover the precise changes recorded in this backup")
    parser.add_argument("--defer-if-app-running", action="store_true", help="Return exit 75 when launch/app activity requires deferral")
    parser.add_argument("--refresh-on-drift", action="store_true",
                        help="Re-prove the saved duplicate families once when a queued target drifts")
    parser.add_argument("--summary", action="store_true", help="Only output counts and family sizes")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.save_plan, args.execute_plan, args.rollback)) > 1:
        parser.error("Choose only one of --save-plan, --execute-plan or --rollback")
    if args.save_plan and args.apply:
        parser.error("Saving a queue and applying it are separate explicit actions")
    if (args.execute_plan or args.rollback) and (args.pair or args.exact_family):
        parser.error("A saved plan cannot be expanded with additional cleanup pairs")
    if args.rollback:
        result = rollback_backup(args.rollback) if args.apply else {"status": "dry-run", "rollback": str(args.rollback)}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    if args.save_plan or args.execute_plan or args.apply or args.pair or args.exact_family:
        pairs = []
        for raw_pair in args.pair:
            parts = raw_pair.split(":")
            if len(parts) != 2:
                parser.error("--pair requires KEEP_ID:DUPLICATE_ID")
            for value in parts:
                uuid.UUID(value)
            pairs.append(tuple(parts))
        for original in args.exact_family:
            pairs.extend(exact_family_pairs(args.roots, original))
        proven = prove_exact_pairs(args.roots, pairs) if pairs else []
        plan = (json.loads(args.execute_plan.read_text(encoding="utf-8-sig"))
                if args.execute_plan else build_repair_plan(args.roots, proven))
        validate_plan(plan)
        if args.save_plan:
            save_plan(args.save_plan, plan)
        if args.apply:
            def apply_checked(current_plan: dict, backup_path: Path | None = args.backup) -> dict:
                try:
                    return apply_plan(current_plan, backup_path)
                except Exception as exc:
                    import codex_app_lifecycle as lifecycle
                    import sync_codex_histories as core
                    deferred = isinstance(exc, (RepairDeferred, lifecycle.AppNotQuiescent, lifecycle.LifecycleBusy))
                    deferred = deferred or isinstance(exc, core.SyncError) and "already running" in str(exc)
                    if deferred and args.defer_if_app_running:
                        return {"status": "deferred", "plan_id": current_plan["plan_id"],
                                "reason": str(exc)}
                    raise

            try:
                result = apply_checked(plan)
                if result.get("status") == "deferred":
                    print(json.dumps(result, ensure_ascii=True))
                    return 75
            except PlanDrift:
                if not (args.refresh_on_drift and args.execute_plan):
                    raise
                refreshed = refresh_plan_after_drift(plan)
                stale = args.execute_plan.with_name(
                    args.execute_plan.stem + ".drifted-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                    + "-" + uuid.uuid4().hex[:8] + ".json"
                )
                args.execute_plan.rename(stale)
                try:
                    save_plan(args.execute_plan, refreshed)
                except BaseException:
                    if not args.execute_plan.exists() and stale.exists():
                        stale.rename(args.execute_plan)
                    raise
                plan = refreshed
                retry_backup = args.backup
                if retry_backup is not None:
                    retry_backup = retry_backup.with_name(
                        retry_backup.name + ".refreshed-" + plan["plan_id"]
                    )
                result = apply_checked(plan, retry_backup)
                if result.get("status") == "deferred":
                    print(json.dumps(result, ensure_ascii=True))
                    return 75
                result["refreshed_from_drift"] = True
                result["stale_plan"] = str(stale)
            if args.execute_plan:
                completed = args.execute_plan.with_name(args.execute_plan.stem + ".completed-" + plan["plan_id"] + ".json")
                if completed.exists():
                    previous = json.loads(completed.read_text(encoding="utf-8-sig"))
                    if plan_digest(previous) != plan_digest(plan):
                        raise RepairError("Completed queue belongs to another plan; it was not overwritten")
                    completed = args.execute_plan.with_name(args.execute_plan.stem + ".duplicate-receipt-" + str(uuid.uuid4()) + ".json")
                args.execute_plan.rename(completed)
                result["completed_plan"] = str(completed)
        else:
            result = {"status": "queued" if args.save_plan else "dry-run", "plan_id": plan["plan_id"],
                      "profiles_modified": 0, "renames": sum(len(p["operations"]) for p in plan["profiles"]),
                      "exact_duplicate_pairs": len(plan["duplicate_pairs"]),
                      "archive_rows": sum(len(p["archive_ids"]) for p in plan["profiles"]),
                      "rollout_backup_bytes": sum(op["before"]["size"] for p in plan["profiles"] for op in p["operations"]),
                      "queue": str(args.save_plan) if args.save_plan else None}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    reports = [audit_root(root) for root in args.roots]
    if args.summary:
        for report in reports:
            report.pop("noncanonical")
            for family in report["families"]:
                family.pop("conflict_ids")
    print(json.dumps({"status": "dry-run", "profiles_modified": 0, "reports": reports},
                     ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlanDrift as error:
        print(json.dumps({"status": "drifted", "error": str(error)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(3)
    except (RepairError, OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(1)
