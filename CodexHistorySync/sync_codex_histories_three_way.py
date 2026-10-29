#!/usr/bin/env python3
"""Converge Cockpit, ChatGPT Plus, and multi-vendor SOTA local Codex histories.

Authentication, provider configuration, plugins, and secrets are never copied.
Only local conversation files, thread databases, indexes, and sidebar metadata
are synchronized. Each destination receives its own model_provider value so the
ChatGPT desktop app can display and resume the same chats in every profile.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from typing import Any, Callable

import codex_app_lifecycle as lifecycle
import sync_codex_histories as core


INSTALL_DIR = Path(__file__).resolve().parent


def configured_root(env_name: str, default_name: str) -> Path:
    value = str(os.environ.get(env_name) or "").strip()
    if value:
        return Path(os.path.expandvars(value)).expanduser().resolve()
    return Path.home() / default_name
DEFAULT_COCKPIT_ROOT = configured_root("CODEX_SOTA_COCKPIT_ROOT", ".codex-personal")
DEFAULT_PLUS_ROOT = configured_root("CODEX_SOTA_PLUS_ROOT", ".codex-plus")
DEFAULT_SOTA_ROOT = configured_root("CODEX_SOTA_CODEX_ROOT", ".codex-sota")
DEFAULT_BACKUP_BASE = INSTALL_DIR / "backups" / "three-way"
COCKPIT_PROVIDER = "codex_local_access"
PLUS_PROVIDER = "openai"
SOTA_PROVIDER = "tango_relay"
MAX_THREE_WAY_BACKUPS = 10
# How many runs keep their outer-snapshot undo image.  Raise it to be able to undo an older
# sync; each extra run costs a full second copy of every session tree it touched.
KEEP_OUTER_SNAPSHOTS = 1
OUTER_SNAPSHOT_FILES = (
    "state_5.sqlite",
    "thread_history_1.sqlite",
    ".codex-global-state.json",
    "session_index.jsonl",
)
OUTER_SNAPSHOT_DIRS = core.SESSION_STORAGE_DIRS


def sqlite_snapshot(source_path: Path, destination_path: Path) -> None:
    """Create an atomic SQLite snapshot without copying WAL/SHM files."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination_path.name}.bootstrap-", dir=destination_path.parent
    )
    os.close(fd)
    temp_path = Path(raw_temp)
    # Both connects belong inside the try.  The scratch file is created in the user's Codex
    # home, so a failure while opening either database -- a vanished source, a permissions
    # error -- used to leave a stray .state_5.sqlite.bootstrap-* there on every failed run,
    # and leak the source connection with it.  Closing before os.replace still matters:
    # Windows will not replace a file that anything still holds open.
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
        ) as source, contextlib.closing(sqlite3.connect(temp_path)) as target:
            source.backup(target, pages=256,
                          progress=lambda _status, _remaining, _total: core.history_checkpoint(),
                          sleep=0.05)
            target.commit()
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise core.SyncError(
                    f"Bootstrap SQLite integrity check failed: {integrity}"
                )
        core.before_history_mutation(destination_path)
        os.replace(temp_path, destination_path)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure_sota_initialized(
    source_root: Path, sota_root: Path, run_backup_root: Path
) -> bool:
    """Create only the missing SOTA history store; never touch auth/config."""
    source_db = source_root / "state_5.sqlite"
    target_db = sota_root / "state_5.sqlite"
    if target_db.is_file():
        with contextlib.closing(sqlite3.connect(target_db.as_uri() + "?mode=ro", uri=True)) as db:
            if core.table_exists(db, "threads"):
                return False
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                raise core.SyncError(f"SOTA database has an unexpected schema: {target_db}")
    if not source_db.is_file():
        raise core.SyncError(f"Bootstrap source database is missing: {source_db}")
    stale_sidecars = [
        path
        for path in (
            sota_root / "state_5.sqlite-wal",
            sota_root / "state_5.sqlite-shm",
        )
        if path.exists()
    ]
    if stale_sidecars:
        raise core.SyncError(
            "Refusing to bootstrap SOTA while orphan SQLite sidecars exist: "
            + ", ".join(str(path) for path in stale_sidecars)
        )

    core.before_history_mutation(sota_root)
    sota_root.mkdir(parents=True, exist_ok=True)
    (sota_root / "sessions").mkdir(parents=True, exist_ok=True)
    (sota_root / "archived_sessions").mkdir(parents=True, exist_ok=True)

    sqlite_snapshot(source_db, target_db)
    copied = ["state_5.sqlite"]
    for name in (".codex-global-state.json", "session_index.jsonl"):
        source = source_root / name
        destination = sota_root / name
        if source.is_file() and not destination.exists():
            core.before_history_mutation(destination)
            core.copy_file_checkpointed(source, destination)
            copied.append(name)

    core.atomic_write_json(
        run_backup_root / "bootstrap-manifest.json",
        {
            "created_at": core.iso_now(),
            "source_root": str(source_root),
            "target_root": str(sota_root),
            "created_files": copied,
            "preserved_files": [
                name
                for name in ("config.toml", "auth.json")
                if (sota_root / name).exists()
            ],
        },
    )
    return True


def rotate_three_way_backups(
    backup_base: Path, keep: int = MAX_THREE_WAY_BACKUPS
) -> None:
    if not backup_base.is_dir():
        return
    runs = sorted(
        [path for path in backup_base.iterdir() if path.is_dir()],
        key=lambda path: path.name,
        reverse=True,
    )
    resolved_runs = [run for run in runs if not recovery_must_be_preserved(run)]
    for old in resolved_runs[max(keep, 0):]:
        shutil.rmtree(old, ignore_errors=True)


def recovery_must_be_preserved(run: Path) -> bool:
    state_path = run / "run-state.json"
    if state_path.exists():
        try:
            status = core.read_json_retry(state_path).get("status")
            return not isinstance(status, str) or status not in {"completed", "rolled_back"}
        except (OSError, ValueError, core.SyncError):
            return True
    marker = run / "rollback-result.json"
    if not marker.exists():
        # Legacy runs did not record success in their own backup directory.
        # Absence of a rollback marker is not proof of a completed transaction:
        # it is also the exact state left by a killed/interrupted writer.
        return (run / "outer-snapshot").exists() or (run / "failed-mutated-state").exists()
    try:
        return core.read_json_retry(marker).get("status") != "restored"
    except (OSError, ValueError, core.SyncError):
        return True


def thread_ids_for_root(root: Path) -> set[str]:
    """Every thread id a root's database currently holds, read-only.

    Used to decide whether another convergence round is needed. Deliberately lighter than
    `core.verify_roots`: that one raises on the first inconsistency, which is exactly what the
    round loop is trying to avoid triggering prematurely.
    """
    database = root / "state_5.sqlite"
    if not database.is_file():
        return set()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
    try:
        return {str(row[0]) for row in connection.execute("SELECT id FROM threads")}
    finally:
        connection.close()


def prune_outer_snapshots(
    backup_base: Path, keep: int = KEEP_OUTER_SNAPSHOTS
) -> list[str]:
    """Drop the full-size recovery images of runs that are no longer the newest.

    A run can retain both its original image and failed-mutated-state. Prune
    only transactions proven completed or fully rolled back; a newer run does
    not resolve an interrupted/unknown predecessor. The newest `keep` resolved
    runs remain available for undo while unresolved recovery images are always
    retained. This keeps routine successful recovery bounded without deleting
    the only evidence needed after a hard interruption.
    """
    if not backup_base.is_dir():
        return []
    runs = sorted(
        (path for path in backup_base.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    removed: list[str] = []
    resolved_runs = [run for run in runs if not recovery_must_be_preserved(run)]
    for run in resolved_runs[max(keep, 0) :]:
        for name in ("outer-snapshot", "failed-mutated-state"):
            image = run / name
            if not image.is_dir():
                continue
            shutil.rmtree(image, ignore_errors=True)
            if not image.exists():
                removed.append(str(image))
    return removed


def create_outer_snapshot(roots: list[Path], run_backup_root: Path,
                          quiescence_check: Callable[[], None] | None = None) -> dict[str, Any]:
    scope = (contextlib.nullcontext() if core._HISTORY_WORK.get() is not None else
             core.history_checkpoint_scope(quiescence_check))
    with scope:
        return _create_outer_snapshot_impl(roots, run_backup_root, quiescence_check)


def _create_outer_snapshot_impl(roots: list[Path], run_backup_root: Path,
                                quiescence_check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Snapshot every file the three-way sync is allowed to mutate.

    Authentication, provider configuration, plugins, and secrets are intentionally outside
    this snapshot.  SQLite is copied through its backup API so WAL-backed databases produce a
    consistent recovery image.
    """
    snapshot_root = run_backup_root / "outer-snapshot"
    snapshot_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "created_at": core.iso_now(),
        "roots": {},
    }
    previous = next((run / "outer-snapshot" for run in sorted(run_backup_root.parent.iterdir(), reverse=True)
                     if run != run_backup_root and (run / "outer-snapshot" / "manifest.json").is_file()), None)
    for label, root in zip(("cockpit", "plus", "sota"), roots):
        destination = snapshot_root / label
        destination.mkdir(parents=True, exist_ok=False)
        root_record: dict[str, Any] = {
            "root": str(root),
            "root_existed": root.exists(),
            "files": {},
            "directories": {},
            "reused_bytes": 0,
        }

        def copy_rollout(source: str, target: str) -> str:
            if quiescence_check is not None:
                quiescence_check()
            # Only immutable backup copies share storage, never the live rollout.
            relative = Path(source.removeprefix('\\\\?\\')).relative_to(root)
            candidate = previous / label / relative if previous else None
            if candidate is not None and candidate.is_file():
                try:
                    if core.same_file_bytes(Path(source), Path(core.extended_path(candidate))):
                        os.link(core.extended_path(candidate), target)
                        root_record["reused_bytes"] += candidate.stat().st_size
                        return target
                except OSError:
                    pass
            return core.copy_file_checkpointed(source, target)
        for name in OUTER_SNAPSHOT_FILES:
            if quiescence_check is not None:
                quiescence_check()
            source = root / name
            exists = source.is_file()
            root_record["files"][name] = exists
            if not exists:
                continue
            target = destination / name
            if name.endswith(".sqlite"):
                sqlite_snapshot(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                core.copy_file_checkpointed(source, target)
        for name in OUTER_SNAPSHOT_DIRS:
            source = root / name
            exists = source.is_dir()
            root_record["directories"][name] = exists
            if exists:
                # Extended-length on both ends: this walks the whole session tree, whose deepest
                # rollout names already run past 100 characters, into a backup root that is itself
                # deep. See core.extended_path -- the plain form fails with a bare WinError 3 that
                # names neither path.
                shutil.copytree(
                    core.extended_path(source),
                    core.extended_path(destination / name),
                    copy_function=copy_rollout,
                    symlinks=True,
                )
        manifest["roots"][label] = root_record
    core.atomic_write_json(snapshot_root / "manifest.json", manifest)
    return {
        "root": snapshot_root,
        "manifest": manifest,
    }


def restore_sqlite_snapshot(source_path: Path, destination_path: Path) -> None:
    """Restore transactionally without unlinking a database's active WAL/SHM files."""
    deadline = time.monotonic() + 30

    def check_progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise core.SyncError(f"SQLite restore timed out: {destination_path}")

    with contextlib.closing(
        sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    ) as source, contextlib.closing(sqlite3.connect(destination_path, timeout=1)) as target:
        source.backup(target, pages=256, progress=check_progress, sleep=0.05)


def restore_outer_snapshot(snapshot: dict[str, Any], *,
                           changed_paths: set[Path] | None = None) -> dict[str, Any]:
    """Restore all managed state while retaining both the snapshot and failed state."""
    if changed_paths is not None:
        selective = restore_recorded_mutations(snapshot, changed_paths)
        if selective is not None:
            return selective
    snapshot_root = Path(snapshot["root"])
    manifest = snapshot["manifest"]
    failed_root = snapshot_root.parent / "failed-mutated-state"
    failed_root.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    for label, root_record in manifest["roots"].items():
        root = Path(root_record["root"])
        source_root = snapshot_root / label
        failed_destination = failed_root / label
        failed_destination.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)

        # Oversize files are not in the snapshot, and the rollback below deletes and restores
        # whole directories. Move each one into the failed-state area first and move it back
        # after the restore, so rolling back can never cost the user a 300 MB conversation.
        preserved_oversize: list[tuple[Path, Path]] = []
        for record in root_record.get("oversize_skipped") or []:
            try:
                relative = Path(str(record["path"])).relative_to(
                    Path(root_record["root"])
                )
            except (KeyError, TypeError, ValueError):
                continue
            current_file = root / relative
            if not current_file.is_file():
                continue
            staged = failed_destination / "oversize-preserved" / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(current_file), str(staged))
                preserved_oversize.append((staged, current_file))
            except OSError:
                continue

        def restore_preserved() -> None:
            for staged, destination in preserved_oversize:
                with contextlib.suppress(Exception):
                    if staged.is_file():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(staged), str(destination))

        for name in OUTER_SNAPSHOT_DIRS:
            current = root / name
            recovery_copy = failed_destination / name
            expected = bool(root_record["directories"].get(name))
            recovery_ready = False
            mutation_started = False
            try:
                if current.exists():
                    shutil.copytree(
                        core.extended_path(current),
                        core.extended_path(recovery_copy),
                        copy_function=shutil.copy2,
                        symlinks=True,
                        dirs_exist_ok=True,
                    )
                    recovery_ready = True
                    mutation_started = True
                    shutil.rmtree(core.extended_path(current))
                if expected:
                    mutation_started = True
                    shutil.copytree(
                        core.extended_path(source_root / name),
                        core.extended_path(current),
                        copy_function=shutil.copy2,
                        symlinks=True,
                    )
            except Exception as error:  # noqa: BLE001 - rollback must report every failure
                errors.append(f"{root}\\{name}: {error}")
                if mutation_started:
                    try:
                        if current.exists():
                            shutil.rmtree(core.extended_path(current))
                        if recovery_ready:
                            shutil.copytree(
                                core.extended_path(recovery_copy),
                                core.extended_path(current),
                                copy_function=shutil.copy2,
                                symlinks=True,
                            )
                    except Exception as recovery_error:
                        errors.append(f"{root}\\{name} recovery: {recovery_error}")
        restore_preserved()

        for name in OUTER_SNAPSHOT_FILES:
            current = root / name
            recovery_copy = failed_destination / name
            expected = bool(root_record["files"].get(name))
            recovery_ready = False
            mutation_started = False
            try:
                if current.is_file():
                    recovery_copy.parent.mkdir(parents=True, exist_ok=True)
                    if name.endswith(".sqlite"):
                        sqlite_snapshot(current, recovery_copy)
                    else:
                        shutil.copy2(current, recovery_copy)
                    recovery_ready = True
                mutation_started = True
                if expected:
                    if name.endswith(".sqlite"):
                        restore_sqlite_snapshot(source_root / name, current)
                    else:
                        core.atomic_copy_file(source_root / name, current)
                else:
                    current.unlink(missing_ok=True)
                    if name.endswith(".sqlite"):
                        for suffix in ("-wal", "-shm"):
                            (root / f"{name}{suffix}").unlink(missing_ok=True)
            except Exception as error:  # noqa: BLE001 - rollback must report every failure
                errors.append(f"{root}\\{name}: {error}")
                if mutation_started and recovery_ready:
                    try:
                        if name.endswith(".sqlite"):
                            restore_sqlite_snapshot(recovery_copy, current)
                        else:
                            core.atomic_copy_file(recovery_copy, current)
                    except Exception as recovery_error:
                        errors.append(f"{root}\\{name} recovery: {recovery_error}")

        if not root_record.get("root_existed"):
            with contextlib.suppress(OSError):
                root.rmdir()

    result = {
        "status": "restored" if not errors else "incomplete",
        "restored_at": core.iso_now(),
        "snapshot_root": str(snapshot_root),
        "failed_state_root": str(failed_root),
        "errors": errors,
    }
    with contextlib.suppress(Exception):
        core.atomic_write_json(snapshot_root.parent / "rollback-result.json", result)
    return result


def restore_recorded_mutations(snapshot: dict[str, Any],
                              changed_paths: set[Path]) -> dict[str, Any] | None:
    """Restore the complete write journal on a cooperative lifecycle yield.

    Unexpected directory/bootstrap/symlink changes fall back to the existing
    full recovery path.  This never changes the outer image or retention: it
    only avoids copying untouched gigabytes when one head was updated before
    a launch request arrived.  The caller keeps both leases until completion.
    """
    snapshot_root = Path(snapshot["root"])
    manifest = snapshot["manifest"]
    records = []
    for target in sorted(changed_paths):
        matched = False
        for label, root_record in manifest["roots"].items():
            root = Path(root_record["root"])
            if target == root:
                return None  # Bootstrap creates a complete history store.
            if not target.is_relative_to(root):
                continue
            relative = target.relative_to(root)
            source = snapshot_root / label / relative
            if ((len(relative.parts) == 1 and relative.name not in OUTER_SNAPSHOT_FILES)
                    or (len(relative.parts) > 1 and relative.parts[0] not in OUTER_SNAPSHOT_DIRS)
                    or target.is_dir() or source.is_dir()
                    or target.is_symlink() or source.is_symlink()):
                return None
            expected = (bool(root_record["files"].get(relative.name))
                        if len(relative.parts) == 1 else source.is_file())
            if expected and not source.is_file():
                raise core.SyncError(f"Recovery image is missing a recorded history file: {source}")
            records.append((label, root, relative, target, source, expected))
            matched = True
            break
        if not matched:
            raise core.SyncError(f"History write journal escaped its recovery roots: {target}")

    failed_root = snapshot_root.parent / "failed-mutated-state"
    failed_root.mkdir(parents=True, exist_ok=True)
    errors = []
    for label, root, relative, current, source, expected in records:
        recovery_copy = failed_root / label / relative
        recovery_ready = False
        mutation_started = False
        is_database = len(relative.parts) == 1 and current.name.endswith(".sqlite")
        try:
            if expected and not is_database and current.is_file() and core.same_file_bytes(source, current):
                continue
            if current.is_file():
                recovery_copy.parent.mkdir(parents=True, exist_ok=True)
                if is_database:
                    sqlite_snapshot(current, recovery_copy)
                else:
                    core.copy_file_checkpointed(current, recovery_copy)
                recovery_ready = True
            mutation_started = True
            if expected:
                current.parent.mkdir(parents=True, exist_ok=True)
                if is_database:
                    restore_sqlite_snapshot(source, current)
                else:
                    core.atomic_copy_file(source, current)
            else:
                current.unlink(missing_ok=True)
                if is_database:
                    for suffix in ("-wal", "-shm"):
                        (root / f"{current.name}{suffix}").unlink(missing_ok=True)
                # Only remove empty parents absent from the original tree.
                parent = current.parent
                while parent != root and not (snapshot_root / label / parent.relative_to(root)).exists():
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        except Exception as error:
            errors.append(f"{current}: {error}")
            if mutation_started and recovery_ready:
                try:
                    current.parent.mkdir(parents=True, exist_ok=True)
                    if is_database:
                        restore_sqlite_snapshot(recovery_copy, current)
                    else:
                        core.atomic_copy_file(recovery_copy, current)
                except Exception as recovery_error:
                    errors.append(f"{current} recovery: {recovery_error}")
    result = {
        "status": "restored" if not errors else "incomplete",
        "restored_at": core.iso_now(), "snapshot_root": str(snapshot_root),
        "failed_state_root": str(failed_root), "errors": errors,
        "restoration_scope": "recorded_mutations", "recorded_paths": len(records),
    }
    with contextlib.suppress(Exception):
        core.atomic_write_json(snapshot_root.parent / "rollback-result.json", result)
    return result


def account_ids_for_roots(roots: list[Path]) -> set[str]:
    account_ids: set[str] = set()
    for root in roots:
        account_ids.update(core.read_sidebar_account_ids(root))
        account_ids.update(
            core.custom_section_account_ids(
                core.read_json_retry(root / ".codex-global-state.json")
            )
        )
    return account_ids


def purge_final_exact_duplicates(
    roots: list[Path],
    backup_root: Path,
) -> tuple[dict[str, int], list[str]]:
    """Remove legacy exact clones left by the final pairwise pass.

    Pairwise sync can create a clone after its own pre-sync cleanup.  At the
    end of a three-way run, inspect each pair again with fresh snapshots.  A
    clone is only removed from a pair where both the clone and its canonical
    row are present and equal, so a profile that lacks proof is untouched.
    Repeating the three pairs makes the cleanup independent of which pair
    propagated the last clone.
    """
    totals = {
        "exact_duplicates_removed": 0,
        "duplicate_main_threads_removed": 0,
        "duplicate_auxiliary_threads_removed": 0,
        "duplicate_session_files_removed": 0,
    }
    warnings: list[str] = []
    pairs = ((0, 1), (0, 2), (1, 2))
    model_guards = core.build_model_guard_contexts(roots)

    for round_number in range(1, 4):
        removed_this_round = False
        for pair_number, (left_index, right_index) in enumerate(pairs, start=1):
            left_root, right_root = roots[left_index], roots[right_index]
            left = core.load_root_snapshot(left_root)
            right = core.load_root_snapshot(right_root)
            plan, find_warnings = core.find_legacy_exact_clones(
                left, right, model_guards
            )
            warnings.extend(find_warnings)
            if not plan:
                continue
            pair_backup = (
                backup_root
                / "final-exact-cleanup"
                / f"round-{round_number:02d}-pair-{pair_number:02d}"
            )
            cleanup = core.purge_legacy_exact_clones(
                [left_root, right_root], plan, pair_backup
            )
            for result_key, cleanup_key in (
                ("exact_duplicates_removed", "threads"),
                ("duplicate_main_threads_removed", "main_threads"),
                ("duplicate_auxiliary_threads_removed", "auxiliary_threads"),
                ("duplicate_session_files_removed", "files"),
            ):
                totals[result_key] += int(cleanup.get(cleanup_key) or 0)
            removed_this_round = True
        if not removed_this_round:
            break
    return totals, warnings


def execute_three_way_mutations(
    started: Any,
    roots: list[Path],
    providers: dict[Path, str],
    run_backup_root: Path,
    backup_base: Path,
    quiescence_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if quiescence_check is not None:
        quiescence_check()
    bootstrap_created = ensure_sota_initialized(
        roots[0], roots[2], run_backup_root
    )
    # SOTA is the UI this launcher opens. Preserve the user's archive/unarchive
    # decisions there throughout every pass, even if another profile's metadata
    # has a newer updated_at due to background migrations.
    archive_states = {
        thread_id: {"archived": int(row.get("archived") or 0),
                    "archived_at": row.get("archived_at")}
        for thread_id, row in core.load_root_snapshot(roots[2]).threads.items()
    }
    passes = (
        ("cockpit-plus", roots[0], roots[1]),
        ("cockpit-sota", roots[0], roots[2]),
        ("plus-sota", roots[1], roots[2]),
    )
    pass_results: list[dict[str, Any]] = []
    totals = {
        "new_files": 0,
        "updated_files": 0,
        "unchanged_files": 0,
        "conflicts_preserved": 0,
        "exact_duplicates_removed": 0,
        "duplicate_main_threads_removed": 0,
        "duplicate_auxiliary_threads_removed": 0,
        "duplicate_session_files_removed": 0,
        "normalized_model_fields": 0,
        "unresolved_model_fields": 0,
    }
    warnings: list[str] = []
    last_result: dict[str, Any] | None = None
    # Run the three pairwise passes until the three roots agree, not just once.
    #
    # A single round is not enough, and the reason is structural rather than a flaky edge case: the
    # passes both propagate sessions AND remove duplicates. Pass 2 (cockpit-sota) copies sota's new
    # threads into cockpit; pass 3 (plus-sota) then dedupes sota, and any thread it drops there is
    # still sitting in cockpit -- so the pairwise verification inside each pass passes while the
    # final three-way check finds the sets unequal and fails the whole sync after four minutes of
    # work. Re-running the passes lets a removal made late in one round propagate in the next.
    #
    # Bounded, because "repeat until equal" on a genuine non-convergence would loop forever: three
    # rounds is enough for a removal in the last pass to reach both other roots, and if the sets
    # still disagree the verification below reports it as the real problem it is.
    MAX_CONVERGENCE_ROUNDS = 3
    rounds_run = 0
    for round_number in range(1, MAX_CONVERGENCE_ROUNDS + 1):
        rounds_run = round_number
        for index, (label, left_root, right_root) in enumerate(passes, start=1):
            if quiescence_check is not None:
                quiescence_check()
            # Keep round 1's directory names exactly as they were; only a repeat needs the suffix.
            pass_name = f"pass-{index:02d}-{label}"
            if round_number > 1:
                pass_name = f"r{round_number}-{pass_name}"
            pass_backup_base = run_backup_root / pass_name
            result = core.run_sync(
                left_root,
                right_root,
                pass_backup_base,
                providers[left_root],
                providers[right_root],
                archive_state_overrides=archive_states,
            )
            last_result = result
            for key in totals:
                totals[key] += int(result.get(key) or 0)
            warnings.extend(str(item) for item in result.get("warnings", []))
            pass_results.append(
                {
                    "round": round_number,
                    "label": label,
                    "left_root": str(left_root),
                    "right_root": str(right_root),
                    "left_provider": providers[left_root],
                    "right_provider": providers[right_root],
                    "backup_dir": result.get("backup_dir"),
                    "new_files": result.get("new_files"),
                    "updated_files": result.get("updated_files"),
                    "conflicts_preserved": result.get("conflicts_preserved"),
                    "normalized_model_fields": result.get("normalized_model_fields", 0),
                    "unresolved_model_fields": result.get("unresolved_model_fields", 0),
                }
            )
        id_sets = [thread_ids_for_root(root) for root in roots]
        if all(current == id_sets[0] for current in id_sets[1:]):
            break
        logging.info(
            "三向同步第 %d 轮后会话 ID 集仍不一致（%s），再跑一轮",
            round_number,
            "/".join(str(len(ids)) for ids in id_sets),
        )

    assert last_result is not None
    final_cleanup, final_cleanup_warnings = purge_final_exact_duplicates(
        roots, run_backup_root
    )
    for key in final_cleanup:
        totals[key] += final_cleanup[key]
    warnings.extend(final_cleanup_warnings)
    final_snapshots = [core.load_root_snapshot(root) for root in roots]
    for root, snapshot in zip(roots, final_snapshots):
        for thread_id, state in archive_states.items():
            row = snapshot.threads.get(thread_id)
            if row is not None and int(row.get("archived") or 0) != state["archived"]:
                raise core.SyncError(f"归档保护校验失败：{root} / {thread_id}")
    verification = core.verify_roots(
        roots,
        account_ids_for_roots(roots),
        providers,
    )
    verification["three_way_same_thread_ids"] = verification["same_thread_ids"]
    verification["convergence_rounds"] = rounds_run
    finished = core.utc_now()
    result = {
        "status": "ok",
        "mode": "three-way",
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": finished.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "cockpit_root": str(roots[0]),
        "plus_root": str(roots[1]),
        "sota_root": str(roots[2]),
        "providers": {str(root): provider for root, provider in providers.items()},
        "backup_dir": str(run_backup_root),
        "outer_snapshot": str(run_backup_root / "outer-snapshot"),
        "bootstrap_created": bootstrap_created,
        "sync_passes": len(pass_results),
        "pass_results": pass_results,
        **totals,
        # Final cleanup can remove clones after the last pair's counters were
        # produced. Report the final, verified SOTA projections, not that stale
        # intermediate state.
        "index_entries": len(core.read_session_index(roots[2] / "session_index.jsonl")),
        "visible_top_level_threads": len(core.collect_main_candidates(
            final_snapshots[2], final_snapshots[2], []
        )),
        "removed_sync_custom_sections": True,
        "sidebar_mode": "project",
        "integrity": {
            str(root): verification[str(root)]["integrity"] for root in roots
        },
        "normalized_model_fields": sum(
            int(item.get("normalized_model_fields") or 0) for item in pass_results
        ),
        "unresolved_model_fields": sum(
            int(item.get("unresolved_model_fields") or 0) for item in pass_results
        ),
        "verification": verification,
        "warnings": warnings,
    }
    # The transaction wrapper records durable completion before best-effort
    # rotation. Cleanup failures must not roll back an otherwise valid sync.
    return result


def preflight_rollout_availability(roots: list[Path]) -> None:
    """Reject irrecoverable missing files before snapshots or any mutation/rollback."""
    core.validate_lineage(roots)
    missing: set[str] = set()
    available: set[str] = set()
    for index, root in enumerate(roots):
        database = root / "state_5.sqlite"
        if not database.is_file():
            continue
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            if not core.table_exists(connection, "threads"):
                user_table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()
                if index == 2 and user_table is None:
                    continue
                raise core.SyncError(f"History database is missing its threads table: {database}")
            for thread_id, rollout_path in connection.execute("SELECT id, rollout_path FROM threads"):
                if rollout_path and Path(rollout_path).is_file():
                    available.add(str(thread_id))
                else:
                    missing.add(str(thread_id))
        finally:
            connection.close()
    missing -= available
    if missing:
        for root in roots:
            missing.difference_update(core.scan_sessions(root))
    if missing:
        raise core.SyncError(
            "同步前发现会话文件缺失，未修改任何历史数据，也未执行回滚："
            + ", ".join(sorted(missing))
        )


def run_three_way_sync(
    cockpit_root: Path,
    plus_root: Path,
    sota_root: Path,
    backup_base: Path,
    cockpit_provider: str = COCKPIT_PROVIDER,
    plus_provider: str = PLUS_PROVIDER,
    sota_provider: str = SOTA_PROVIDER,
    *,
    quiescence_check: Callable[[], None] | None = None,
    before_write_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    with core.history_checkpoint_scope(
        quiescence_check, before_write_check, rollback_available=True,
        managed_roots=(cockpit_root, plus_root, sota_root),
    ):
        return _run_three_way_sync_impl(
            cockpit_root, plus_root, sota_root, backup_base, cockpit_provider,
            plus_provider, sota_provider, quiescence_check=quiescence_check,
        )


def _run_three_way_sync_impl(
    cockpit_root: Path,
    plus_root: Path,
    sota_root: Path,
    backup_base: Path,
    cockpit_provider: str = COCKPIT_PROVIDER,
    plus_provider: str = PLUS_PROVIDER,
    sota_provider: str = SOTA_PROVIDER,
    *,
    quiescence_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    started = core.utc_now()
    roots = [
        cockpit_root.resolve(),
        plus_root.resolve(),
        sota_root.resolve(),
    ]
    if len(set(roots)) != 3:
        raise core.SyncError("Three-way Codex history roots must be distinct.")
    providers = {
        roots[0]: cockpit_provider,
        roots[1]: plus_provider,
        roots[2]: sota_provider,
    }
    if any(not provider for provider in providers.values()):
        raise core.SyncError("Every history root must have a model provider ID.")

    for root in roots[:2]:
        if not (root / "state_5.sqlite").is_file():
            raise core.SyncError(f"Required Codex database is missing: {root}")

    if quiescence_check is not None:
        quiescence_check()
    preflight_rollout_availability(roots)
    backup_base.mkdir(parents=True, exist_ok=True)
    run_name = core.utc_now().strftime("%Y%m%d-%H%M%S-%f")
    run_backup_root = backup_base / run_name
    run_backup_root.mkdir(parents=True, exist_ok=False)
    state_path = run_backup_root / "run-state.json"

    def record_state(status: str, **detail: Any) -> None:
        core.atomic_write_json(state_path, {
            "status": status, "updated_at": core.iso_now(),
            "roots": [str(root) for root in roots], **detail,
        })

    try:
        record_state("preparing")
        outer_snapshot = create_outer_snapshot(roots, run_backup_root, quiescence_check)
        if quiescence_check is not None:
            quiescence_check()
    except BaseException:
        # The snapshot itself failed, so nothing has been mutated and there is nothing to roll back;
        # this run's backup dir holds only a half-written image with no recovery value. Drop it so a
        # run that died on a full disk gives back the space it just took. This cannot live in the
        # rollback try below: that block's `except` restores from `outer_snapshot`, which would be
        # unbound here -- masking the real "disk is full" with an UnboundLocalError -- and restoring
        # from a partial image would overwrite intact history with an incomplete copy.
        # BaseException so an interrupt during a multi-gigabyte copy reclaims too; re-raised at once
        # so the real error still reaches the caller and the exit code is unchanged. Only this run's
        # own directory is removed, so the previous run's still-usable undo image is untouched.
        shutil.rmtree(core.extended_path(run_backup_root), ignore_errors=True)
        raise
    try:
        record_state("applying")
        result = execute_three_way_mutations(
            started,
            roots,
            providers,
            run_backup_root,
            backup_base,
            quiescence_check,
        )
        record_state("completed")
        return result
    except BaseException as error:
        work = core._HISTORY_WORK.get()
        if isinstance(error, lifecycle.AppNotQuiescent) and work is not None and not work.mutation_started:
            # All history is still byte-for-byte original.  Restoring ~7 GiB
            # here would make an otherwise safe yield block the launcher for
            # minutes.  Discard only this run's prepared image.
            with contextlib.suppress(Exception):
                record_state("rolled_back", error_type=type(error).__name__, history_mutated=False)
            shutil.rmtree(core.extended_path(run_backup_root), ignore_errors=True)
            with contextlib.suppress(Exception):
                error.history_recovery_status = "not_needed"
            raise
        try:
            # A launch request must wait until restoration finishes; repeated
            # callbacks must never interrupt rollback or release either lease.
            with core.suspend_history_checkpoints():
                if isinstance(error, lifecycle.AppNotQuiescent) and work is not None:
                    rollback = restore_outer_snapshot(outer_snapshot, changed_paths=work.mutated_paths)
                else:
                    rollback = restore_outer_snapshot(outer_snapshot)
        except BaseException as recovery_error:
            with contextlib.suppress(Exception):
                record_state("recovery_required", error_type=type(error).__name__,
                             recovery_error_type=type(recovery_error).__name__)
            raise
        if rollback["status"] != "restored":
            with contextlib.suppress(Exception):
                record_state("recovery_required", error_type=type(error).__name__)
            details = "; ".join(rollback["errors"]) or "unknown rollback error"
            raise core.SyncError(
                f"Three-way sync failed and rollback was incomplete: {error}; {details}; "
                f"recovery data: {rollback['failed_state_root']}"
            ) from error
        with contextlib.suppress(Exception):
            record_state("rolled_back", error_type=type(error).__name__)
        with contextlib.suppress(Exception):
            error.history_recovery_status = "restored"
        raise
    finally:
        # Resolved successes and full rollbacks keep their existing bounded
        # retention. The durable run-state guard preserves every interrupted,
        # unknown or incomplete recovery regardless of its age.
        try:
            # A cancelled run should return its leases promptly.  Retention
            # will run after the next resolved sync, never in a launch's way.
            if not isinstance(sys.exc_info()[1], lifecycle.AppNotQuiescent):
                with core.suspend_history_checkpoints():
                    prune_outer_snapshots(backup_base)
                    rotate_three_way_backups(backup_base)
        except OSError:
            # Reclaiming disk is best-effort cleanup; it must never replace the real error with an
            # error about tidying up, nor fail a sync that actually committed.
            pass


def audit_roots(
    cockpit_root: Path,
    plus_root: Path,
    sota_root: Path,
    cockpit_provider: str = COCKPIT_PROVIDER,
    plus_provider: str = PLUS_PROVIDER,
    sota_provider: str = SOTA_PROVIDER,
) -> dict[str, Any]:
    roots = [cockpit_root.resolve(), plus_root.resolve(), sota_root.resolve()]
    providers = (cockpit_provider, plus_provider, sota_provider)
    provider_map = {
        root: provider for root, provider in zip(roots, providers)
    }
    account_ids = account_ids_for_roots(roots)
    root_results: dict[str, Any] = {}
    id_sets: dict[Path, set[str]] = {}
    errors: list[str] = []

    for root, provider in zip(roots, providers):
        base = {
            "exists": root.is_dir(),
            "state_db_exists": (root / "state_5.sqlite").is_file(),
            "sessions_exists": (root / "sessions").is_dir(),
            "expected_model_provider": provider,
        }
        if not all((base["exists"], base["state_db_exists"], base["sessions_exists"])):
            base["validation_status"] = "error"
            base["validation_error"] = "required history storage is missing"
            root_results[str(root)] = base
            errors.append(f"{root}: required history storage is missing")
            continue
        try:
            verified = core.verify_roots(
                [root],
                account_ids,
                {root: provider},
            )
            root_result = dict(verified[str(root)])
            root_result.update(base)
            root_result["validation_status"] = "ok"
            root_results[str(root)] = root_result
            id_sets[root] = set(
                core.load_root_snapshot(root).threads
            )
        except Exception as error:
            base["validation_status"] = "error"
            base["validation_error"] = str(error)
            root_results[str(root)] = base
            errors.append(f"{root}: {error}")

    union_ids = set().union(*id_sets.values()) if id_sets else set()
    intersection_ids = (
        set.intersection(*id_sets.values()) if len(id_sets) == len(roots) else set()
    )
    thread_sets_equal = (
        len(id_sets) == len(roots)
        and all(current == next(iter(id_sets.values())) for current in id_sets.values())
    )
    pending = not errors and not thread_sets_equal
    status = "error" if errors else ("pending" if pending else "ok")
    return {
        "status": status,
        "mode": "three-way-audit",
        "roots": root_results,
        "providers": {
            str(root): provider for root, provider in provider_map.items()
        },
        "thread_sets_equal": thread_sets_equal,
        "content_verified": False,
        "validation_scope": "storage, providers, sidebar and thread IDs; cross-profile conversation content is not compared",
        "thread_union_count": len(union_ids),
        "thread_intersection_count": len(intersection_ids),
        "pending_reason": "thread_ids_differ" if pending else None,
        "thread_differences": {
            str(root): {
                "threads": len(ids),
                "missing_from_union": len(union_ids - ids),
                "unique_to_root": len(ids - set().union(*(other for other_root, other in id_sets.items() if other_root != root))),
            }
            for root, ids in id_sets.items()
        },
        "errors": errors,
    }


def reusable_result_matches_request(
    result: dict[str, Any],
    roots: tuple[Path, Path, Path],
    providers: tuple[str, str, str],
) -> bool:
    """Return whether an already-finished sync exactly matches this invocation."""
    resolved_roots = tuple(root.resolve() for root in roots)
    expected_provider_map = {
        str(root): provider for root, provider in zip(resolved_roots, providers)
    }
    verification = result.get("verification") or {}
    return bool(
        result.get("status") == "ok"
        and result.get("mode") == "three-way"
        and result.get("cockpit_root") == str(resolved_roots[0])
        and result.get("plus_root") == str(resolved_roots[1])
        and result.get("sota_root") == str(resolved_roots[2])
        and result.get("providers") == expected_provider_map
        and verification.get("three_way_same_thread_ids") is True
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize Cockpit, Plus, and Tango Relay Codex histories."
    )
    parser.add_argument("--cockpit-root", type=Path, default=DEFAULT_COCKPIT_ROOT)
    parser.add_argument("--plus-root", type=Path, default=DEFAULT_PLUS_ROOT)
    parser.add_argument("--sota-root", type=Path, default=DEFAULT_SOTA_ROOT)
    parser.add_argument("--backup-base", type=Path, default=DEFAULT_BACKUP_BASE)
    parser.add_argument("--cockpit-provider", default=None)
    parser.add_argument("--plus-provider", default=None)
    parser.add_argument("--sota-provider", default=None)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--wait-for-existing",
        action="store_true",
        help="Wait for an in-progress sync to release the lock before syncing.",
    )
    parser.add_argument("--lock-wait-seconds", type=float, default=None,
                        help="Override the sync-lock wait; 0 coalesces a busy request immediately.")
    parser.add_argument("--defer-if-app-running", action="store_true",
                        help="Sync only while the app is offline; return deferred when it is busy.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_path = core.LOG_DIR / "sync.log"
    synchronization_started = False
    try:
        args.cockpit_provider = args.cockpit_provider or core.configured_model_provider(args.cockpit_root, COCKPIT_PROVIDER)
        args.plus_provider = args.plus_provider or core.configured_model_provider(args.plus_root, PLUS_PROVIDER)
        args.sota_provider = args.sota_provider or core.configured_model_provider(args.sota_root, SOTA_PROVIDER)
        default_roots = {
            DEFAULT_COCKPIT_ROOT.resolve(),
            DEFAULT_PLUS_ROOT.resolve(),
            DEFAULT_SOTA_ROOT.resolve(),
        }
        requested_roots = {
            args.cockpit_root.resolve(),
            args.plus_root.resolve(),
            args.sota_root.resolve(),
        }
        missing_optional = [
            root.resolve() for root in (args.cockpit_root, args.plus_root)
            if not (root / "state_5.sqlite").is_file()
        ]
        if requested_roots == default_roots and missing_optional:
            # Cockpit and Plus are optional companion products. A standalone
            # installation must remain healthy without inventing their stores.
            result = {
                "status": "skipped", "reason": "optional_profiles_absent",
                "mode": "three-way-audit" if args.audit_only else "three-way",
                "sync_performed": False,
                "missing_optional_profiles": [str(root) for root in missing_optional],
                "message": "Cross-profile history sync is inactive until both optional Cockpit and Plus history stores exist.",
            }
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
            return 0
        if args.audit_only:
            result = audit_roots(
                args.cockpit_root, args.plus_root, args.sota_root,
                args.cockpit_provider, args.plus_provider, args.sota_provider,
            )
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
            return 0
        log_path = core.setup_logging()
        previous_result_mtime = (
            core.LAST_RESULT_PATH.stat().st_mtime_ns
            if core.LAST_RESULT_PATH.exists()
            else 0
        )
        lock_timeout = args.lock_wait_seconds
        if lock_timeout is None:
            lock_timeout = 900.0 if args.wait_for_existing else 0.0
        # Real profiles are never written while Electron/app-server owns them,
        # even through an older manual entrypoint without the new watcher flag.
        use_lifecycle = args.defer_if_app_running or bool(requested_roots & default_roots)
        with contextlib.ExitStack() as stack:
            quiescence_check = None
            if use_lifecycle:
                lifecycle.assert_quiescent()
                stack.enter_context(lifecycle.lifecycle_lock(wait_timeout=0.0))
                lifecycle.assert_quiescent()
                quiescence_check = lifecycle.quiescence_checkpoint
            lock = stack.enter_context(core.SingleInstanceLock(
                core.LOCK_PATH, wait_timeout=lock_timeout, cancel_check=quiescence_check
            ))
            if quiescence_check is not None:
                quiescence_check()
            if (
                args.wait_for_existing
                and lock.waited_for_existing
                and requested_roots == default_roots
                and core.LAST_RESULT_PATH.exists()
                and core.LAST_RESULT_PATH.stat().st_mtime_ns > previous_result_mtime
            ):
                existing_result = core.read_json_retry(core.LAST_RESULT_PATH)
                if reusable_result_matches_request(
                    existing_result,
                    (
                        args.cockpit_root,
                        args.plus_root,
                        args.sota_root,
                    ),
                    (
                        args.cockpit_provider,
                        args.plus_provider,
                        args.sota_provider,
                    ),
                ):
                    existing_result["reused_existing_sync"] = True
                    print(
                        json.dumps(
                            existing_result,
                            ensure_ascii=args.json,
                            separators=(",", ":") if args.json else None,
                            indent=None if args.json else 2,
                        )
                    )
                    return 0
            logging.info(
                "Starting three-way sync: %s <-> %s <-> %s",
                args.cockpit_root,
                args.plus_root,
                args.sota_root,
            )
            synchronization_started = True
            result = run_three_way_sync(
                args.cockpit_root,
                args.plus_root,
                args.sota_root,
                args.backup_base,
                args.cockpit_provider,
                args.plus_provider,
                args.sota_provider,
                quiescence_check=quiescence_check,
                before_write_check=lifecycle.assert_quiescent if use_lifecycle else None,
            )
            result["log_path"] = str(log_path)
            if requested_roots == default_roots:
                core.write_last_result(result)
            logging.info("Three-way sync completed: %s", result)
            print(
                json.dumps(
                    result,
                    ensure_ascii=args.json,
                    separators=(",", ":") if args.json else None,
                    indent=None if args.json else 2,
                )
            )
            return 0
    except (core.SyncBusy, lifecycle.LifecycleBusy, lifecycle.AppNotQuiescent) as exc:
        if isinstance(exc, core.SyncBusy):
            reason = "sync_busy"
        elif isinstance(exc, lifecycle.AppNotQuiescent):
            reason = str(exc)
        else:
            reason = "launch_in_progress" if lifecycle.launch_pending() else "sync_busy"
        result = {"status": "deferred", "mode": "three-way", "reason": reason,
                  "finished_at": core.iso_now(), "log_path": str(log_path)}
        # A losing/idle watcher must not replace the winner's success marker
        # with an error. The next offline opportunity will retry this request.
        logging.info("Three-way sync deferred: %s", reason)
        print(json.dumps(result, ensure_ascii=args.json, separators=(",", ":") if args.json else None,
                         indent=None if args.json else 2))
        return 0
    except Exception as exc:
        result = {
            "status": "error",
            "mode": "three-way",
            "finished_at": core.iso_now(),
            "error": str(exc),
            "recovery_status": getattr(exc, "history_recovery_status", "not_confirmed") if synchronization_started else "not_needed",
            "log_path": str(log_path),
        }
        logging.error("Three-way sync failed: %s\n%s", exc, traceback.format_exc())
        requested_roots = {
            args.cockpit_root.resolve(),
            args.plus_root.resolve(),
            args.sota_root.resolve(),
        }
        default_roots = {
            DEFAULT_COCKPIT_ROOT.resolve(),
            DEFAULT_PLUS_ROOT.resolve(),
            DEFAULT_SOTA_ROOT.resolve(),
        }
        if requested_roots == default_roots and synchronization_started:
            core.write_last_result(result)
        print(
            json.dumps(
                result,
                ensure_ascii=args.json,
                separators=(",", ":") if args.json else None,
                indent=None if args.json else 2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
