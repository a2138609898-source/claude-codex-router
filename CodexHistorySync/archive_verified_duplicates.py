"""Offline, explicit-pair archival; no automatic discovery, deletion or CLI.

Callers must stop writers to the supplied profile before applying a plan.
Only verified conflict duplicates are marked archived. Rollouts are never moved
or rewritten, and all SQLite/cache changes have recoverable pre-change backups.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import time
import uuid

import repair_sota_launch_history as repair
import sync_codex_histories as core


@dataclass(frozen=True)
class ArchivePlan:
    root: Path
    keep: str
    duplicate: str
    rows: tuple[dict, dict]
    evidence: dict[Path, dict]
    digest: str


def plan_root(root: Path, pairs: list[tuple[str, str]]) -> list[ArchivePlan]:
    """Prove full effective history equality without changing any files."""
    try:
        root = repair.checked_root(root)
        repair.inside(root, root / "state_5.sqlite")
        pairs = list(pairs)
        duplicates = {duplicate for _, duplicate in pairs}
        if len(duplicates) != len(pairs) or any(keep in duplicates for keep, _ in pairs):
            raise core.SyncError("Duplicate pairs must be unique and may not form chains")
        for keep, duplicate in pairs:
            uuid.UUID(keep)
            uuid.UUID(duplicate)
        if not pairs:
            return []
        if os.environ.get("CODEX_THREAD_ID") in duplicates:
            raise core.SyncError("Duplicate is active or referenced; keep it untouched")
        rows = repair.read_rows(root)
        pages, errors = repair.read_inventory(root)
        if errors:
            raise core.SyncError("Malformed or linked history prevents duplicate proof")
        page_ids = [page["page_id"] for page in pages.values()]
        if None in page_ids or len(page_ids) != len(set(page_ids)):
            raise core.SyncError("Ambiguous physical history pages prevent duplicate proof")
        referenced = {str((page["metadata"].get("history_base") or {}).get("thread_id"))
                      for page in pages.values() if page["metadata"].get("history_base")}
        guarded = {page["owner"] for page in pages.values() if page["page_id"] in referenced}
        with closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro",
                                     uri=True, timeout=3)) as db:
            if core.table_exists(db, "thread_spawn_edges"):
                for parent, child in db.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                    guarded.update((parent, child))
        plans = []
        for keep, duplicate in pairs:
            if duplicate in guarded:
                raise core.SyncError("Duplicate is active or referenced; keep it untouched")
            if keep not in rows or duplicate not in rows:
                raise core.SyncError("Both explicit pair members must exist in this profile")
            if rows[keep].get("archived") or rows[duplicate].get("archived"):
                raise core.SyncError("Both explicit pair members must be unarchived")
            title = str(rows[duplicate].get("title") or rows[duplicate].get("name") or "")
            original_title = str(rows[keep].get("title") or rows[keep].get("name") or "")
            if not title.endswith(core.CONFLICT_CLONE_SUFFIX) or repair.family_name(title) != repair.family_name(original_title):
                raise core.SyncError("Only explicitly marked conflict-family duplicates are eligible")
            if sum(page["owner"] == duplicate for page in pages.values()) != 1:
                raise core.SyncError("Multiple duplicate pages need separate branch review")
            evidence, digests = {}, []
            for thread_id in (keep, duplicate):
                path = repair.plain_path(rows[thread_id]["rollout_path"])
                if path not in pages or pages[path]["owner"] != thread_id:
                    raise core.SyncError("Thread does not point to its own physical head")
                for page in repair.chain_paths(path, pages):
                    evidence[page] = repair.fingerprint(page)
                stat = path.stat()
                session = core.SessionFile(thread_id, path, path.relative_to(root), stat.st_size, stat.st_mtime_ns)
                digests.append(core.normalized_session_digest(session))
            if digests[0] != digests[1]:
                raise core.SyncError("Duplicate contents differ; distinct histories must be preserved")
            if any(repair.fingerprint(path) != before for path, before in evidence.items()):
                raise core.SyncError("History changed while proving duplicate contents")
            plans.append(ArchivePlan(root, keep, duplicate, (rows[keep], rows[duplicate]), evidence, digests[0]))
        return plans
    except (repair.RepairError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        raise core.SyncError(str(exc)) from exc


def archive(root: Path, plans: list[ArchivePlan], backup: Path) -> list[str]:
    """Apply an unchanged explicit plan; raise on failure, return [] on success.

The caller supplies an unused backup directory outside the profile. SQLite
writes share one transaction; cache replacements are atomic and restored on
failure before the transaction is committed. No history files are deleted.
"""
    plans = list(plans)
    if not plans:
        return []
    root = repair.checked_root(root)
    if any(not isinstance(plan, ArchivePlan) or plan.root != root for plan in plans):
        raise core.SyncError("Plans belong to a different profile")
    pairs = [(plan.keep, plan.duplicate) for plan in plans]
    if plan_root(root, pairs) != plans:
        raise core.SyncError("Reviewed history changed; make a fresh plan")
    backup = Path(backup).resolve()
    if backup.is_relative_to(root) or root.is_relative_to(backup) or backup.exists():
        raise core.SyncError("Use an unused backup directory outside the profile")
    database = repair.inside(root, root / "state_5.sqlite")
    catalog = root / "sqlite" / "codex-dev.db"
    if catalog.exists():
        repair.inside(root, catalog)
    cache_paths = [root / name for name in ("session_index.jsonl", ".codex-global-state.json")]
    for path in cache_paths:
        if path.exists():
            repair.inside(root, path)
    originals = {path: path.read_bytes() for path in cache_paths if path.is_file()}
    ids = {plan.duplicate for plan in plans}
    replacements = repair.cache_replacements(root, {}, ids)
    if any(path.read_bytes() != original for path, original in originals.items()):
        raise core.SyncError("Sidebar state changed while preparing archive")
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "preparing", "root": str(root),
                "pairs": [{"keep": keep, "duplicate": duplicate} for keep, duplicate in pairs]}
    repair.write_manifest(backup, manifest)
    written = []
    with closing(sqlite3.connect(database, timeout=3)) as db:
        if catalog.exists():
            db.execute("ATTACH DATABASE ? AS sidebar", (str(catalog),))
        db.execute("BEGIN IMMEDIATE")
        try:
            if plan_root(root, pairs) != plans:
                raise core.SyncError("Reviewed history changed; make a fresh plan")
            repair.sqlite_backup(database, backup / database.name)
            if catalog.exists():
                (backup / "sqlite").mkdir()
                repair.sqlite_backup(catalog, backup / "sqlite" / catalog.name)
            for path in replacements:
                repair.atomic_bytes(backup / path.name, originals[path])
            columns = set(core.table_columns(db, "threads"))
            changes = ["archived=1"]
            if "archived_at" in columns:
                changes.append("archived_at=" + str(int(time.time())))
            for column in ("thread_section_id", "section_position", "is_pinned"):
                if column in columns:
                    changes.append(column + ("=0" if column == "is_pinned" else "=NULL"))
            for thread_id in sorted(ids):
                db.execute("UPDATE threads SET " + ",".join(changes) + " WHERE id=?", (thread_id,))
            if catalog.exists():
                tables = {row[0] for row in db.execute("SELECT name FROM sidebar.sqlite_master WHERE type='table'")}
                if "local_thread_catalog" in tables:
                    for thread_id in sorted(ids):
                        db.execute("DELETE FROM sidebar.local_thread_catalog WHERE host_id='local' AND thread_id=?", (thread_id,))
                    if "local_thread_catalog_metadata" in tables:
                        db.execute("UPDATE sidebar.local_thread_catalog_metadata SET catalog_revision=catalog_revision+1 WHERE id=1")
            for path, content in replacements.items():
                if path.read_bytes() != originals[path]:
                    raise core.SyncError("Sidebar state changed; refusing to overwrite it")
                repair.atomic_bytes(path, content)
                written.append(path)
            for plan in plans:
                if any(repair.fingerprint(path) != before for path, before in plan.evidence.items()):
                    raise core.SyncError("History changed before archival completed")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise core.SyncError("Archive database integrity check failed")
            manifest["status"] = "commit-pending"
            repair.write_manifest(backup, manifest)
            db.commit()
        except BaseException:
            db.rollback()
            restore_errors = []
            for path in reversed(written):
                try:
                    if path.read_bytes() != replacements[path]:
                        raise core.SyncError("Cache changed concurrently: " + str(path))
                    repair.atomic_bytes(path, originals[path])
                except (OSError, core.SyncError) as exc:
                    restore_errors.append(str(exc))
            manifest["status"] = "recovery-required" if restore_errors else "rolled-back"
            try:
                repair.write_manifest(backup, manifest)
            except OSError:
                pass
            if restore_errors:
                raise core.SyncError("Cache rollback needs manual recovery from " + str(backup) + ": " + "; ".join(restore_errors))
            raise
    # A final journal write must not make a committed archive look rolled back.
    manifest["status"] = "committed"
    try:
        repair.write_manifest(backup, manifest)
    except OSError:
        return ["Archive committed; backup manifest still says commit-pending"]
    return []
