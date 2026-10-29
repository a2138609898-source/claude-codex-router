"""Repair archived visibility caches without changing thread content or settings."""
from datetime import datetime
from contextlib import closing
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import stat

import sync_codex_histories as core
from codex_app_lifecycle import app_running


BACKUP_PREFIX = "archive-cache-repair-"
BACKUP_STATE_FILE = "repair-state.json"
BACKUP_FORMAT = "codex-archived-sidebar-backup-v1"
MAX_COMPLETED_BACKUPS = 5


def _plain_backup_tree(path: Path) -> bool:
    """Never prune a backup containing a symlink, junction, or special file."""
    pending = [path]
    while pending:
        item = pending.pop()
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            return False
        if stat.S_ISDIR(info.st_mode):
            pending.extend(item.iterdir())
        elif not stat.S_ISREG(info.st_mode):
            return False
    return True


def prune_completed_backups(
    backup_base: Path, keep: int = MAX_COMPLETED_BACKUPS
) -> list[str]:
    """Bound only new-format, explicitly completed repairs; keep every unknown run."""
    if not backup_base.is_dir():
        return []
    resolved_base = backup_base.resolve()
    eligible = []
    for backup in backup_base.iterdir():
        if not backup.name.startswith(BACKUP_PREFIX):
            continue
        try:
            info = backup.lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or backup.resolve().parent != resolved_base):
                continue
            marker = backup / BACKUP_STATE_FILE
            marker_info = marker.lstat()
            if (not stat.S_ISREG(marker_info.st_mode)
                    or getattr(marker_info, "st_file_attributes", 0) & 0x400):
                continue
            state_bytes = marker.read_bytes()
            state = json.loads(state_bytes)
            if (not isinstance(state, dict)
                    or state.get("format") != BACKUP_FORMAT
                    or state.get("status") != "completed"
                    or state.get("backup_name") != backup.name
                    or not _plain_backup_tree(backup)):
                continue
        except (OSError, ValueError, UnicodeError):
            # Legacy backups have no completion receipt. A missing, corrupt,
            # in-progress, or interrupted receipt is never permission to prune.
            continue
        eligible.append((backup, state_bytes))
    removed = []
    ordered = sorted(eligible, key=lambda entry: entry[0].name, reverse=True)
    for backup, state_bytes in ordered[max(keep, 0):]:
        try:
            if (backup.resolve().parent != resolved_base
                    or not _plain_backup_tree(backup)
                    or (backup / BACKUP_STATE_FILE).read_bytes() != state_bytes):
                continue
            shutil.rmtree(backup)
            removed.append(str(backup))
        except OSError as error:
            logging.warning("Could not rotate completed sidebar backup %s: %s", backup, error)
    return removed


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
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(backup_path)) as target:
        source.backup(target)


def repair_local_thread_catalog(
    root: Path, name: str, archived_ids: set[str], backup: Path
) -> int:
    """Remove archived local-sidebar projections without touching rollout files."""
    catalog_path = root / "sqlite" / "codex-dev.db"
    if not catalog_path.is_file() or not archived_ids:
        return 0

    with closing(sqlite3.connect(catalog_path.as_uri() + "?mode=ro", uri=True)) as probe:
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
    with closing(sqlite3.connect(catalog_path, timeout=30)) as db:
        db.execute("PRAGMA busy_timeout=30000")
        removed = clear_archived_local_catalog_entries(db, archived_ids)
        db.commit()
    if removed:
        print(
            f"{name}/sqlite/codex-dev.db: removed {removed} archived local catalog entry/entries"
        )
    return removed


def configured_profiles():
    return tuple(
        (name, core.configured_root(variable, name))
        for name, variable in (
            (".codex-personal", "CODEX_SOTA_COCKPIT_ROOT"),
            (".codex-plus", "CODEX_SOTA_PLUS_ROOT"),
            (".codex-sota", "CODEX_SOTA_CODEX_ROOT"),
        )
    )


def main():
    if app_running():
        raise RuntimeError("Codex App must be fully closed before archived sidebar repair")

    backup = Path(__file__).resolve().parent / "backups" / (
        BACKUP_PREFIX + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    )
    for name, root in configured_profiles():
        if not (root / "state_5.sqlite").is_file():
            continue
        with closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            archived = {str(row[0]) for row in db.execute("SELECT id FROM threads WHERE archived = 1")}
            needs_detach = core.clear_archived_sidebar_placement(db, dry_run=True)
        # Every profile owns a local catalog; the probe inside is a safe no-op
        # for older schemas (checked 2026-09-30: .codex-personal held 25 stale
        # archived entries that only cleaning .codex-sota left untouched).
        repair_local_thread_catalog(root, name, archived, backup)
        # The desktop renders a sidebar section by membership, ignoring the
        # archive bit: an archived thread that still carries thread_section_id
        # keeps showing up in the sidebar as an already-archived chat.  The App
        # itself leaves those columns behind when it archives, so detach them
        # here exactly like the sync engine does (core.clear_archived_sidebar_placement).
        # Snapshot the database first so the repair is always reversible.
        db_path = root / "state_5.sqlite"
        backup_db = backup / name / "state_5.sqlite"
        if needs_detach:
            backup_sqlite_database(db_path, backup_db)
            with closing(sqlite3.connect(db_path, timeout=30)) as db:
                db.execute("PRAGMA busy_timeout=30000")
                detached = core.clear_archived_sidebar_placement(db)
                db.commit()
                # Print only real work: the launcher parses stdout for "removed|detached".
                if detached:
                    print(f"{name}/state_5.sqlite: detached {detached} archived thread(s) from sidebar sections")
        for filename in (".codex-global-state.json", "session_index.jsonl"):
            path = root / filename
            if not path.is_file():
                continue
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
    if backup.exists():
        # The receipt is the final operation after all profile changes succeed.
        # A crash or failure at any earlier point leaves an unknown backup which
        # retention must preserve. Existing unmarked backups remain untouched.
        core.atomic_write_json(backup / BACKUP_STATE_FILE, {
            "format": BACKUP_FORMAT,
            "status": "completed",
            "backup_name": backup.name,
            "completed_at": core.iso_now(),
        })
        try:
            prune_completed_backups(backup.parent)
        except OSError as error:
            logging.warning("Sidebar repair completed; backup rotation failed: %s", error)
    print(f"Backup: {backup}" if backup.exists() else "No archived sidebar changes; no backup needed.")


if __name__ == "__main__":
    main()
