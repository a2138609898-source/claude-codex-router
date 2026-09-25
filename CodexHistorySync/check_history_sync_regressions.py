"""Offline regression fixtures: no live profiles, app processes, or credentials.

Run: py -3.11 -B check_history_sync_regressions.py
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import sync_codex_histories as core
import sync_codex_histories_three_way as three_way
from check_history_model_guard import THREAD_SCHEMA


SID = "00000000-0000-0000-0000-000000000001"
PARENT = "00000000-0000-0000-0000-000000000002"
HEAD = "00000000-0000-0000-0000-000000000003"
DUPLICATE = "00000000-0000-0000-0000-000000000004"
STAMP = "2026-09-23T00:00:00Z"
CANONICAL = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f-]{36}\.jsonl$")


def descriptor(root: Path, path: Path, owner: str = SID) -> core.SessionFile:
    stat = path.stat()
    return core.SessionFile(owner, path, path.relative_to(root), stat.st_size, stat.st_mtime_ns)


def page(root: Path, page_id: str, provider: str, events: list[str], *,
         owner: str = SID, base: Path | None = None, cutoff: int = 0,
         start: int = 0) -> Path:
    path = root / "sessions" / "2026" / "09" / "23" / f"rollout-2026-09-23T00-00-00-{page_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": owner, "session_id": owner, "timestamp": STAMP,
               "model_provider": provider, "cwd": "fixture"}
    if base is not None:
        payload["history_mode"] = "paginated"
        payload["history_base"] = {"thread_id": core.physical_page_id(base),
                                   "end_ordinal_exclusive": cutoff,
                                   "end_byte_offset": core.lineage_byte_boundary(base, cutoff)}
    rows = [{"type": "session_meta", "timestamp": STAMP, "ordinal": start, "payload": payload}]
    rows.extend({"type": "event_msg", "timestamp": STAMP, "ordinal": start + number,
                 "payload": {"type": "user_message", "message": event, "thread_id": owner}}
                for number, event in enumerate(events, 1))
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    return path


def register(root: Path, head: Path, provider: str, recency: int) -> None:
    with contextlib.closing(sqlite3.connect(root / "state_5.sqlite")) as db:
        db.execute(THREAD_SCHEMA)
        db.execute("INSERT INTO threads (id,rollout_path,created_at,updated_at,source,model_provider,"
                   "cwd,title,sandbox_policy,approval_mode) VALUES (?,?,1,?,'cli',?,'fixture',"
                   "'same synthetic thread','','')", (SID, str(head), recency, provider))
        db.commit()
    (root / ".codex-global-state.json").write_text(json.dumps({
        "projectless-thread-ids": [SID], "thread-project-assignments": {}, "local-projects": {},
        "pinned-thread-ids": [], "electron-persisted-atom-state": {
            "flat-project-sidebar-preferences-v1": {"mode": "project", "chatSortMode": "updated_at", "projectSortMode": "updated_at"}}}), encoding="utf-8")
    (root / "session_index.jsonl").write_text(json.dumps({"id": SID, "thread_name": "same synthetic thread", "updated_at": STAMP}) + "\n", encoding="utf-8")


class HistorySyncRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="history-sync-regression-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.engine = self.base / "engine"
        (self.engine / "work").mkdir(parents=True)
        self.install_patch = mock.patch.object(core, "INSTALL_DIR", self.engine)
        self.install_patch.start()
        self.addCleanup(self.install_patch.stop)

    def roots(self, branches: tuple[str, ...]) -> list[Path]:
        roots = []
        for index, branch in enumerate(branches):
            root = self.base / f"profile-{index}"
            root.mkdir()
            parent = page(root, PARENT, f"provider-{index}", ["shared", branch])
            head = page(root, HEAD, f"provider-{index}", ["tail"], base=parent, cutoff=3, start=3)
            register(root, head, f"provider-{index}", index + 1)
            roots.append(root)
        return roots

    def sync(self, roots: list[Path]) -> dict:
        return core.run_sync(roots[0], roots[1], self.base / "backups", "provider-0", "provider-1")

    def add_exact_clone(self, root: Path, source: Path, provider: str) -> None:
        duplicate = page(
            root,
            DUPLICATE,
            provider,
            ["tail"],
            owner=DUPLICATE,
            base=next((root / "sessions").rglob(f"*{PARENT}.jsonl")),
            cutoff=3,
            start=3,
        )
        with contextlib.closing(sqlite3.connect(root / "state_5.sqlite")) as db:
            db.execute(
                "INSERT INTO threads (id,rollout_path,created_at,updated_at,source,model_provider,"
                "cwd,title,sandbox_policy,approval_mode) VALUES (?,?,1,?,'cli',?,'fixture',?,?,?)",
                (
                    DUPLICATE,
                    str(duplicate),
                    2,
                    provider,
                    "same synthetic thread" + core.CONFLICT_CLONE_SUFFIX,
                    "",
                    "",
                ),
            )
            db.commit()

    def archive_clone_row(self, root: Path) -> None:
        with contextlib.closing(sqlite3.connect(root / "state_5.sqlite")) as db:
            rollout = db.execute(
                "SELECT rollout_path FROM threads WHERE id=?", (DUPLICATE,)
            ).fetchone()[0]
            source = Path(rollout)
            destination = root / "archived_sessions" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            db.execute(
                "UPDATE threads SET rollout_path=?, archived=1 WHERE id=?",
                (str(destination), DUPLICATE),
            )
            db.commit()

    def assert_converged(self, roots: list[Path], expected_threads: int) -> None:
        snapshots = [core.load_root_snapshot(root) for root in roots]
        self.assertEqual(len(snapshots[0].threads), expected_threads)
        for snapshot in snapshots[1:]:
            self.assertEqual(set(snapshots[0].threads), set(snapshot.threads))
        catalogs = [core.scan_sessions(root, {sid: str(row["rollout_path"]) for sid, row in snap.threads.items()})
                    for root, snap in zip(roots, snapshots)]
        for sid in snapshots[0].threads:
            for catalog in catalogs[1:]:
                self.assertEqual(core.compare_files(catalogs[0][sid], catalog[sid]), "equal", sid)
        for root in roots:
            core.validate_lineage([root], check_offsets=True)

    def test_colliding_ancestry_is_preserved_and_second_sync_is_noop(self) -> None:
        roots = self.roots(("branch-A", "branch-B"))
        original_pages = {root: next((root / "sessions").rglob(f"*{PARENT}.jsonl")) for root in roots}
        original_bytes = {root: path.read_bytes() for root, path in original_pages.items()}
        first = self.sync(roots)
        self.assertEqual(first["conflicts_preserved"], 1)
        self.assert_converged(roots, 2)
        for root, path in original_pages.items():
            self.assertEqual(path.read_bytes(), original_bytes[root], "immutable ancestry was overwritten")
        second = self.sync(roots)
        self.assertEqual(second["conflicts_preserved"], 0)
        self.assertEqual(second["new_files"], 0)
        self.assert_converged(roots, 2)

    def test_three_way_converges_without_clone_growth(self) -> None:
        roots = self.roots(("branch-A", "branch-A", "branch-B"))
        run = lambda: three_way.run_three_way_sync(*roots, self.base / "three-backups",
                    "provider-0", "provider-1", "provider-2")
        first = run()
        self.assertLessEqual(first["sync_passes"], 6)
        self.assert_converged(roots, 2)
        second = run()
        self.assertEqual(second["sync_passes"], 3)
        self.assertEqual(second["conflicts_preserved"], 0)
        self.assertEqual(second["new_files"], 0)
        self.assert_converged(roots, 2)

    def test_exact_clone_detection_keeps_profile_local_digests(self) -> None:
        roots = self.roots(("branch-A", "branch-A"))
        for root, provider in zip(roots, ("provider-0", "provider-1")):
            canonical = next((root / "sessions").rglob(f"*{HEAD}.jsonl"))
            self.add_exact_clone(root, canonical, provider)
        snapshots = [core.load_root_snapshot(root) for root in roots]

        # A rollout can normalize to different digests in different provider
        # registries.  The canonical and clone still match within each profile.
        original_digest = core.normalized_session_digest

        def profile_digest(session: core.SessionFile, guard=None) -> str:
            root_digest = original_digest(session, guard)
            profile = "personal" if session.path.is_relative_to(roots[0]) else "plus"
            return f"{profile}:{root_digest}"

        with mock.patch.object(core, "normalized_session_digest", side_effect=profile_digest):
            plan, warnings = core.find_legacy_exact_clones(snapshots[0], snapshots[1])
        self.assertEqual(warnings, [])
        self.assertEqual(
            [(item.canonical_id, item.duplicate_id) for item in plan],
            [(SID, DUPLICATE)],
        )

    def test_post_sync_cleanup_removes_clone_created_after_preflight(self) -> None:
        roots = self.roots(("branch-A", "branch-A"))
        for root, provider in zip(roots, ("provider-0", "provider-1")):
            canonical = next((root / "sessions").rglob(f"*{HEAD}.jsonl"))
            self.add_exact_clone(root, canonical, provider)
        snapshots = [core.load_root_snapshot(root) for root in roots]
        original_find = core.find_legacy_exact_clones
        plan, _ = original_find(snapshots[0], snapshots[1])
        self.assertEqual(len(plan), 1)

        # Pretend the clone appeared during the file merge: preflight sees no
        # clone, the first post-sync check sees it, and the recheck is clean.
        with mock.patch.object(
            core,
            "find_legacy_exact_clones",
            side_effect=[([], []), (plan, []), ([], [])],
        ):
            result = self.sync(roots)
        self.assertEqual(result["exact_duplicates_removed"], 1)
        self.assertEqual(result["duplicate_session_files_removed"], 2)
        self.assertNotIn(DUPLICATE, core.load_root_snapshot(roots[0]).threads)
        self.assertNotIn(DUPLICATE, core.load_root_snapshot(roots[1]).threads)

    def test_archived_exact_clone_is_not_selected_for_purge(self) -> None:
        roots = self.roots(("branch-A", "branch-A"))
        for index, (root, provider) in enumerate(zip(roots, ("provider-0", "provider-1"))):
            canonical = next((root / "sessions").rglob(f"*{HEAD}.jsonl"))
            self.add_exact_clone(root, canonical, provider)
            if index == 0:
                self.archive_clone_row(root)
        snapshots = [core.load_root_snapshot(root) for root in roots]
        plan, warnings = core.find_legacy_exact_clones(snapshots[0], snapshots[1])
        self.assertEqual(plan, [])
        self.assertEqual(warnings, [])
        self.assertTrue(any((roots[0] / "archived_sessions").rglob("*.jsonl")))
        self.assertTrue(any((roots[1] / "sessions").rglob("*.jsonl")))

    def test_clone_filename_is_canonical_even_for_paginated_heads(self) -> None:
        roots = self.roots(("branch-A", "branch-B"))
        self.sync(roots)
        for root in roots:
            for sid, row in core.load_root_snapshot(root).threads.items():
                if sid != SID:
                    self.assertRegex(Path(row["rollout_path"]).name, CANONICAL)

    def test_canonical_name_repairs_legacy_conflict_prefix(self) -> None:
        root = self.base / "names"
        root.mkdir()
        path = page(root, HEAD, "p", [], owner=HEAD)
        legacy = path.with_name(f"rollout-conflict-{HEAD}.jsonl")
        path.rename(legacy)
        self.assertRegex(core.independent_head_name(legacy, HEAD), CANONICAL)
        self.assertEqual(core.physical_page_id(Path(core.independent_head_name(legacy, HEAD))), HEAD)

    def test_full_and_paginated_history_ignore_storage_ordinals(self) -> None:
        root = self.base / "pagination"
        root.mkdir()
        full = page(root, SID, "p", ["one", "two"])
        source = page(root, PARENT, "p", ["one"])
        head = page(root, HEAD, "q", ["two"], base=source, cutoff=2, start=2)
        self.assertEqual(core.compare_files(descriptor(root, full), descriptor(root, head)), "equal")

    def test_true_message_difference_is_not_ignored(self) -> None:
        root = self.base / "different"
        root.mkdir()
        left = page(root, SID, "p", ["one", "different"])
        right = page(root, HEAD, "q", ["one", "two"])
        self.assertEqual(core.compare_files(descriptor(root, left), descriptor(root, right)), "divergent")

    def test_native_independent_head_keeps_logical_and_physical_ids(self) -> None:
        root = self.base / "native-name"
        root.mkdir()
        path = page(root, SID, "p", [])
        name = core.independent_head_name(path, HEAD)
        self.assertEqual(name, f"rollout-2026-09-23T00-00-00-{SID}_{HEAD}.jsonl")
        self.assertEqual(core.physical_page_id(Path(name)), HEAD)

    def test_recursive_collisions_preserve_each_original_page_and_offsets(self) -> None:
        roots = self.roots(("branch-A", "branch-B"))
        middle_id = "00000000-0000-0000-0000-000000000004"
        originals = {}
        for index, root in enumerate(roots):
            parent = next((root / "sessions").rglob(f"*{PARENT}.jsonl"))
            middle = page(root, middle_id, f"provider-{index}", ["middle"], base=parent, cutoff=3, start=3)
            page(root, HEAD, f"provider-{index}", ["tail"], base=middle, cutoff=5, start=5)
            originals.update({parent: parent.read_bytes(), middle: middle.read_bytes()})
        self.sync(roots)
        self.assert_converged(roots, 2)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        second = self.sync(roots)
        self.assertEqual(second["conflicts_preserved"], 0)
        self.assertEqual(second["new_files"], 0)
        self.assert_converged(roots, 2)

    def test_orphan_ancestry_is_not_reimported_as_a_visible_conflict(self) -> None:
        roots = self.roots(("branch-A", "orphan-branch"))
        with contextlib.closing(sqlite3.connect(roots[1] / "state_5.sqlite")) as db:
            db.execute("DELETE FROM threads")
            db.commit()
        orphan_head = next((roots[1] / "sessions").rglob(f"*{HEAD}.jsonl"))
        orphan_bytes = orphan_head.read_bytes()
        first = self.sync(roots)
        self.assertEqual(first["conflicts_preserved"], 0)
        self.assertEqual(orphan_head.read_bytes(), orphan_bytes)
        self.assert_converged(roots, 1)
        self.assertEqual(self.sync(roots)["new_files"], 0)

    def frozen_profiles(self, roots: list[Path]) -> dict:
        return {root: {"threads": core.load_root_snapshot(root).threads,
                       "files": {path.relative_to(root).as_posix(): path.read_bytes()
                                 for path in root.rglob("*") if path.is_file()
                                 and not path.name.startswith("state_5.sqlite")}}
                for root in roots}

    def test_launch_preempts_snapshot_without_live_mutation_or_rollback(self) -> None:
        roots = self.roots(("branch-A", "branch-A", "branch-B"))
        before = self.frozen_profiles(roots)
        count = 0

        def check() -> None:
            nonlocal count
            count += 1
            if count == 7:
                raise three_way.lifecycle.AppNotQuiescent("launch_requested")

        with mock.patch.object(three_way, "restore_outer_snapshot") as rollback:
            with self.assertRaises(three_way.lifecycle.AppNotQuiescent):
                three_way.run_three_way_sync(*roots, self.base / "three-backups",
                    "provider-0", "provider-1", "provider-2", quiescence_check=check)
            rollback.assert_not_called()
        self.assertEqual(self.frozen_profiles(roots), before)

    def test_launch_after_partial_round_rolls_back_all_profiles(self) -> None:
        roots = self.roots(("branch-A", "branch-B", "branch-C"))
        before = self.frozen_profiles(roots)
        committed_pass = False
        run_sync = core.run_sync

        def do_pass(*args, **kwargs):
            nonlocal committed_pass
            result = run_sync(*args, **kwargs)
            committed_pass = True
            return result

        def check() -> None:
            if committed_pass:
                raise three_way.lifecycle.AppNotQuiescent("launch_requested")

        with mock.patch.object(core, "run_sync", side_effect=do_pass):
            with self.assertRaises(three_way.lifecycle.AppNotQuiescent):
                three_way.run_three_way_sync(*roots, self.base / "three-backups",
                    "provider-0", "provider-1", "provider-2", quiescence_check=check)
        self.assertTrue(committed_pass)
        self.assertEqual(self.frozen_profiles(roots), before)

    def cli_deferred(self, check_side_effect=None, lock_error=None) -> dict:
        marker = self.base / "last-result.json"
        marker.write_text('{"status":"ok","saved":true}', encoding="utf-8")
        before = marker.read_bytes()
        argv = ["sync", "--json", "--defer-if-app-running", "--lock-wait-seconds", "0"]
        for flag, name in (("--cockpit-root", "a"), ("--plus-root", "b"), ("--sota-root", "c")):
            argv.extend((flag, str(self.base / name)))
        output = io.StringIO()
        with (mock.patch.object(sys, "argv", argv),
              mock.patch.object(core, "setup_logging", return_value=self.base / "sync.log"),
              mock.patch.object(core, "LAST_RESULT_PATH", marker),
              mock.patch.object(core, "write_last_result") as save,
              mock.patch.object(three_way.lifecycle, "assert_quiescent", side_effect=check_side_effect),
              mock.patch.object(three_way.lifecycle, "lifecycle_lock", return_value=contextlib.nullcontext()),
              mock.patch.object(core, "SingleInstanceLock", side_effect=lock_error,
                                return_value=contextlib.nullcontext(mock.Mock(waited_for_existing=False))),
              mock.patch.object(three_way, "run_three_way_sync") as mutate,
              contextlib.redirect_stdout(output)):
            self.assertEqual(three_way.main(), 0)
            save.assert_not_called()
            mutate.assert_not_called()
        self.assertEqual(marker.read_bytes(), before)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "deferred")
        return result

    def test_cli_app_running_is_deferred_success_without_touching_last_result(self) -> None:
        result = self.cli_deferred(three_way.lifecycle.AppNotQuiescent("app_running"))
        self.assertEqual(result["reason"], "app_running")

    def test_cli_busy_sync_is_coalesced_without_touching_last_result(self) -> None:
        result = self.cli_deferred(lock_error=core.SyncBusy("fixture busy"))
        self.assertEqual(result["reason"], "sync_busy")

    def test_cli_rechecks_launch_after_acquiring_lifecycle_lock(self) -> None:
        result = self.cli_deferred([None, three_way.lifecycle.AppNotQuiescent("launch_requested")])
        self.assertEqual(result["reason"], "launch_requested")


if __name__ == "__main__":
    unittest.main(verbosity=2)
