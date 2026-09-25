"""Repair archived visibility caches without changing thread content or settings."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3

import sync_codex_histories as core
from codex_app_lifecycle import app_running


def clear_archived_local_catalog_entries(
    connection: sqlite3.Connection, archived_ids: set[str]
) -> int:
    """Drop only local catalog rows whose owning history row is archived."""
    archived_ids = {str(thread_id) for thread_id in archived_ids if str(thread_id)}
    if not archived_ids:
        return 0

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "local_thread_catalog" not in tables:
        return 0

    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(local_thread_catalog)")
    }
    if not {"host_id", "thread_id"}.issubset(columns):
        return 0

    removed = 0
    ordered_ids = sorted(archived_ids)
    for offset in range(0, len(ordered_ids), 800):
        batch = ordered_ids[offset:offset + 800]
        placeholders = ",".join("?" for _ in batch)
        cursor = connection.execute(
            "DELETE FROM local_thread_catalog "
            f"WHERE host_id = 'local' AND thread_id IN ({placeholders})",
            batch,
        )
        removed += max(0, cursor.rowcount)

    if removed and "local_thread_catalog_metadata" in tables:
        metadata_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(local_thread_catalog_metadata)"
            )
        }
        if {"id", "catalog_revision"}.issubset(metadata_columns):
            connection.execute(
                "UPDATE local_thread_catalog_metadata "
                "SET catalog_revision = catalog_revision + 1 WHERE id = 1"
            )
    return removed


def backup_sqlite_database(source_path: Path, backup_path: Path) -> None:
    """Take a consistent SQLite backup, including committed WAL contents."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def repair_local_thread_catalog(
    root: Path, name: str, archived_ids: set[str], backup: Path
) -> int:
    """Remove archived local-sidebar projections without touching rollout files."""
    catalog_path = root / "sqlite" / "codex-dev.db"
    if not catalog_path.is_file() or not archived_ids:
        return 0

    with sqlite3.connect(catalog_path.as_uri() + "?mode=ro", uri=True) as probe:
        tables = {
            str(row[0])
            for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "local_thread_catalog" not in tables:
            return 0
        columns = {
            str(row[1])
            for row in probe.execute("PRAGMA table_info(local_thread_catalog)")
        }
        if not {"host_id", "thread_id"}.issubset(columns):
            return 0
        archived_list = sorted(archived_ids)
        matches = None
        for offset in range(0, len(archived_list), 800):
            batch = archived_list[offset:offset + 800]
            placeholders = ",".join("?" for _ in batch)
            matches = probe.execute(
                "SELECT 1 FROM local_thread_catalog "
                f"WHERE host_id = 'local' AND thread_id IN ({placeholders}) LIMIT 1",
                batch,
            ).fetchone()
            if matches:
                break
    if not matches:
        return 0

    backup_path = backup / name / "sqlite" / "codex-dev.db"
    backup_sqlite_database(catalog_path, backup_path)
    with sqlite3.connect(catalog_path, timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        removed = clear_archived_local_catalog_entries(db, archived_ids)
        db.commit()
    if removed:
        print(
            f"{name}/sqlite/codex-dev.db: removed {removed} archived local catalog entry/entries"
        )
    return removed


def main():
    if app_running():
        raise RuntimeError("Codex App must be fully closed before archived sidebar repair")

    backup = Path(__file__).resolve().parent / "backups" / (
        "archive-cache-repair-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    )
    for name in (".codex-personal", ".codex-plus", ".codex-sota"):
        root = Path.home() / name
        with sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            archived = {str(row[0]) for row in db.execute("SELECT id FROM threads WHERE archived = 1")}
        if name == ".codex-sota":
            repair_local_thread_catalog(root, name, archived, backup)
        # The desktop renders a sidebar section by membership, ignoring the
        # archive bit: an archived thread that still carries thread_section_id
        # keeps showing up in the sidebar as an already-archived chat.  The App
        # itself leaves those columns behind when it archives, so detach them
        # here exactly like the sync engine does (core.clear_archived_sidebar_placement).
        # Snapshot the database first so the repair is always reversible.
        db_path = root / "state_5.sqlite"
        backup_db = backup / name / "state_5.sqlite"
        backup_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source, \
                sqlite3.connect(backup_db) as target:
            source.backup(target)
        with sqlite3.connect(db_path, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            detached = core.clear_archived_sidebar_placement(db)
            db.commit()
            print(f"{name}/state_5.sqlite: detached {detached} archived thread(s) from sidebar sections")
        for filename in (".codex-global-state.json", "session_index.jsonl"):
            path = root / filename
            original = path.read_bytes()
            if filename.endswith(".jsonl"):
                lines = original.decode("utf-8").splitlines(keepends=True)
                remaining = []
                for line in lines:
                    if not line.strip():
                        remaining.append(line)
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        # Preserve an unrelated damaged line; the regular sync will
                        # report it rather than deleting user history during repair.
                        remaining.append(line)
                        continue
                    if not isinstance(item, dict) or str(item.get("id") or "") not in archived:
                        remaining.append(line)
                updated = "".join(remaining).encode("utf-8")
                removed = len(lines) - len(remaining)
            else:
                state = json.loads(original)
                before = json.dumps(state, ensure_ascii=False)
                core.remove_archived_sidebar_entries(state, archived)
                if json.dumps(state, ensure_ascii=False) == before:
                    continue
                updated = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                removed = "visibility references"
            if updated == original:
                continue
            destination = backup / name / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            core.atomic_write_bytes(destination, original)
            if path.read_bytes() != original:
                raise RuntimeError(f"State changed during repair; left untouched: {path}")
            core.atomic_write_bytes(path, updated)
            print(f"{name}/{filename}: removed {removed}")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
