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
import tempfile
import traceback
from typing import Any

import sync_codex_histories as core


INSTALL_DIR = Path(__file__).resolve().parent
DEFAULT_COCKPIT_ROOT = Path.home() / ".codex-personal"
DEFAULT_PLUS_ROOT = Path.home() / ".codex-plus"
DEFAULT_SOTA_ROOT = Path.home() / ".codex-sota"
DEFAULT_BACKUP_BASE = INSTALL_DIR / "backups" / "three-way"
COCKPIT_PROVIDER = "codex_local_access"
PLUS_PROVIDER = "openai"
SOTA_PROVIDER = "tango_relay"
MAX_THREE_WAY_BACKUPS = 10
# How many runs keep their outer-snapshot undo image.  Raise it to be able to undo an older
# sync; each extra run costs a full second copy of every session tree it touched.
KEEP_OUTER_SNAPSHOTS = 1
# Session rollout files past this size are recorded in the snapshot manifest but not copied
# into the undo image.  Two runaway conversations on this install are 255 MB and 327 MB, so
# one full snapshot used to weigh 7.5 GB and the copy dominated every sync run. Skipping them
# is safe: every session mutation the sync performs is an atomic temp-file swap, so there is
# never a torn copy to roll back, and pass-* backups plus the sibling roots still hold the
# pre-sync content of anything the sync actually changed.
OUTER_SNAPSHOT_MAX_FILE_BYTES = 64 * 1024 * 1024
OUTER_SNAPSHOT_FILES = (
    "state_5.sqlite",
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
            source.backup(target)
            target.commit()
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise core.SyncError(
                    f"Bootstrap SQLite integrity check failed: {integrity}"
                )
        os.replace(temp_path, destination_path)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure_sota_initialized(
    source_root: Path, sota_root: Path, run_backup_root: Path
) -> bool:
    """Create only the missing SOTA history store; never touch auth/config."""
    source_db = source_root / "state_5.sqlite"
    target_db = sota_root / "state_5.sqlite"
    sota_root.mkdir(parents=True, exist_ok=True)
    (sota_root / "sessions").mkdir(parents=True, exist_ok=True)
    (sota_root / "archived_sessions").mkdir(parents=True, exist_ok=True)

    if target_db.is_file():
        return False
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

    sqlite_snapshot(source_db, target_db)
    copied = ["state_5.sqlite"]
    for name in (".codex-global-state.json", "session_index.jsonl"):
        source = source_root / name
        destination = sota_root / name
        if source.is_file() and not destination.exists():
            shutil.copy2(source, destination)
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
    for old in runs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


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

    A run leaves up to two full copies of every session tree it touched: `outer-snapshot`, the undo
    image, and -- when it failed -- `failed-mutated-state`, the half-mutated state the rollback
    moved aside for inspection. Both are read only from the failure branch of `run_three_way_sync`,
    inside the very call that wrote them, so once a newer run exists nothing will ever read either
    again. Retaining them is what turned a repeatedly-failing preflight into tens of gigabytes: one
    session tree here is ~11 GB, and three retries wrote six copies of it.

    The newest `keep` runs survive intact so undoing or diagnosing the most recent sync stays
    possible; `pass-*` keeps every run auditable for a few megabytes each.
    """
    if not backup_base.is_dir():
        return []
    runs = sorted(
        (path for path in backup_base.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    removed: list[str] = []
    for run in runs[max(keep, 0) :]:
        for name in ("outer-snapshot", "failed-mutated-state"):
            image = run / name
            if not image.is_dir():
                continue
            shutil.rmtree(image, ignore_errors=True)
            if not image.exists():
                removed.append(str(image))
    return removed


def oversize_ignore_filter(skip_log: list[dict[str, Any]]):
    """copytree ignore hook that leaves files past the snapshot cap out of the copy.

    Every skip is recorded so the manifest and the rollback can account for exactly which
    files a snapshot copy does not contain.
    """

    def ignore(directory: str, names: list[str]) -> list[str]:
        drop: list[str] = []
        for entry in names:
            candidate = Path(directory) / entry
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if candidate.is_file() and size > OUTER_SNAPSHOT_MAX_FILE_BYTES:
                drop.append(entry)
                skip_log.append({"path": str(candidate), "bytes": size})
        return drop

    return ignore


def create_outer_snapshot(roots: list[Path], run_backup_root: Path) -> dict[str, Any]:
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
    for label, root in zip(("cockpit", "plus", "sota"), roots):
        destination = snapshot_root / label
        destination.mkdir(parents=True, exist_ok=False)
        root_record: dict[str, Any] = {
            "root": str(root),
            "root_existed": root.exists(),
            "files": {},
            "directories": {},
        }
        for name in OUTER_SNAPSHOT_FILES:
            source = root / name
            exists = source.is_file()
            root_record["files"][name] = exists
            if not exists:
                continue
            target = destination / name
            if name == "state_5.sqlite":
                sqlite_snapshot(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(core.extended_path(source), core.extended_path(target))
        for name in OUTER_SNAPSHOT_DIRS:
            source = root / name
            exists = source.is_dir()
            root_record["directories"][name] = exists
            if exists:
                # Extended-length on both ends: this walks the whole session tree, whose deepest
                # rollout names already run past 100 characters, into a backup root that is itself
                # deep. See core.extended_path -- the plain form fails with a bare WinError 3 that
                # names neither path.
                skipped: list[dict[str, Any]] = []
                shutil.copytree(
                    core.extended_path(source),
                    core.extended_path(destination / name),
                    copy_function=shutil.copy2,
                    symlinks=True,
                    ignore=oversize_ignore_filter(skipped),
                )
                if skipped:
                    root_record["oversize_skipped"] = skipped
        manifest["roots"][label] = root_record
    core.atomic_write_json(snapshot_root / "manifest.json", manifest)
    return {
        "root": snapshot_root,
        "manifest": manifest,
    }


def restore_outer_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Restore all managed state while retaining both the snapshot and failed state."""
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
            try:
                if current.exists():
                    recovery_skipped: list[dict[str, Any]] = []
                    shutil.copytree(
                        current,
                        recovery_copy,
                        copy_function=shutil.copy2,
                        symlinks=True,
                        dirs_exist_ok=True,
                        ignore=oversize_ignore_filter(recovery_skipped),
                    )
                    shutil.rmtree(current)
                if expected:
                    shutil.copytree(
                        source_root / name,
                        current,
                        copy_function=shutil.copy2,
                        symlinks=True,
                    )
            except Exception as error:  # noqa: BLE001 - rollback must report every failure
                errors.append(f"{root}\\{name}: {error}")
                with contextlib.suppress(Exception):
                    if current.exists():
                        shutil.rmtree(current)
                    if recovery_copy.exists():
                        shutil.copytree(
                            recovery_copy,
                            current,
                            copy_function=shutil.copy2,
                            symlinks=True,
                        )
        restore_preserved()

        for name in OUTER_SNAPSHOT_FILES:
            current = root / name
            recovery_copy = failed_destination / name
            expected = bool(root_record["files"].get(name))
            try:
                if current.is_file():
                    recovery_copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(current, recovery_copy)
                if name == "state_5.sqlite":
                    for suffix in ("-wal", "-shm"):
                        sidecar = root / f"{name}{suffix}"
                        if sidecar.is_file():
                            shutil.copy2(sidecar, failed_destination / sidecar.name)
                            sidecar.unlink()
                if expected:
                    core.atomic_copy_file(source_root / name, current)
                else:
                    current.unlink(missing_ok=True)
            except Exception as error:  # noqa: BLE001 - rollback must report every failure
                errors.append(f"{root}\\{name}: {error}")
                with contextlib.suppress(Exception):
                    if recovery_copy.is_file():
                        core.atomic_copy_file(recovery_copy, current)

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


def execute_three_way_mutations(
    started: Any,
    roots: list[Path],
    providers: dict[Path, str],
    run_backup_root: Path,
    backup_base: Path,
) -> dict[str, Any]:
    bootstrap_created = ensure_sota_initialized(
        roots[0], roots[2], run_backup_root
    )
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
        "index_entries": int(last_result.get("index_entries") or 0),
        "visible_top_level_threads": int(
            last_result.get("visible_top_level_threads") or 0
        ),
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
    rotate_three_way_backups(backup_base)
    # Reached only once every pass has committed, so no run older than the newest still
    # needs the image it would have been rolled back from.
    prune_outer_snapshots(backup_base)
    return result


def run_three_way_sync(
    cockpit_root: Path,
    plus_root: Path,
    sota_root: Path,
    backup_base: Path,
    cockpit_provider: str = COCKPIT_PROVIDER,
    plus_provider: str = PLUS_PROVIDER,
    sota_provider: str = SOTA_PROVIDER,
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

    backup_base.mkdir(parents=True, exist_ok=True)
    run_name = core.utc_now().strftime("%Y%m%d-%H%M%S-%f")
    run_backup_root = backup_base / run_name
    run_backup_root.mkdir(parents=True, exist_ok=False)
    try:
        outer_snapshot = create_outer_snapshot(roots, run_backup_root)
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
        shutil.rmtree(run_backup_root, ignore_errors=True)
        raise
    try:
        return execute_three_way_mutations(
            started,
            roots,
            providers,
            run_backup_root,
            backup_base,
        )
    except Exception as error:
        rollback = restore_outer_snapshot(outer_snapshot)
        if rollback["status"] != "restored":
            details = "; ".join(rollback["errors"]) or "unknown rollback error"
            raise core.SyncError(
                f"Three-way sync failed and rollback was incomplete: {error}; {details}; "
                f"recovery data: {rollback['failed_state_root']}"
            ) from error
        raise
    finally:
        # Prune on EVERY exit, not just the committing one.  The success path prunes from inside
        # execute_three_way_mutations, so a run that failed never pruned at all -- and a failing run
        # is the expensive one: it leaves its outer-snapshot *and* the failed-mutated-state copy the
        # rollback moved aside, two full images of every session tree. Three consecutive failures of
        # a preflight that rejects the user's own history filled a 238 GB drive to zero, and each
        # retry made it worse because nothing on the failure path ever reclaimed anything.
        # prune_outer_snapshots keeps the newest KEEP_OUTER_SNAPSHOTS runs by name, and this run's
        # name sorts newest, so the image this rollback may still need is never the one dropped.
        try:
            prune_outer_snapshots(backup_base)
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
    parser.add_argument("--cockpit-provider", default=COCKPIT_PROVIDER)
    parser.add_argument("--plus-provider", default=PLUS_PROVIDER)
    parser.add_argument("--sota-provider", default=SOTA_PROVIDER)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--wait-for-existing",
        action="store_true",
        help="Wait for an in-progress sync to release the lock before syncing.",
    )
    return parser


def main() -> int:
    log_path = core.setup_logging()
    args = build_parser().parse_args()
    if args.audit_only:
        result = audit_roots(
            args.cockpit_root,
            args.plus_root,
            args.sota_root,
            args.cockpit_provider,
            args.plus_provider,
            args.sota_provider,
        )
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    try:
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
        previous_result_mtime = (
            core.LAST_RESULT_PATH.stat().st_mtime_ns
            if core.LAST_RESULT_PATH.exists()
            else 0
        )
        # A real three-way sync takes minutes (280 s observed on this install), so a 180 s
        # wait used to expire while the winning sync was still running and turned a launch
        # into "History sync is already running" failure.  15 minutes covers any sync this
        # machine actually performs; the wait ends the moment the holder releases.
        lock_timeout = 900.0 if args.wait_for_existing else 0.0
        with core.SingleInstanceLock(
            core.LOCK_PATH, wait_timeout=lock_timeout
        ) as lock:
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
            result = run_three_way_sync(
                args.cockpit_root,
                args.plus_root,
                args.sota_root,
                args.backup_base,
                args.cockpit_provider,
                args.plus_provider,
                args.sota_provider,
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
    except Exception as exc:
        result = {
            "status": "error",
            "mode": "three-way",
            "finished_at": core.iso_now(),
            "error": str(exc),
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
        if requested_roots == default_roots:
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
