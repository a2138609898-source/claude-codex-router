"""Continuation regressions; synthetic histories only, via the guarded runner."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock
import uuid

import test_hardening_history as fixtures
import sync_codex_histories as core
import sync_codex_histories_three_way as three_way


class HistoryContinuationTests(unittest.TestCase):
    # Reuse only the audited fixture builders, not their test class/discovery.
    setUp = fixtures.HistoryHardeningTests.setUp
    history = fixtures.HistoryHardeningTests.history
    rollout = fixtures.HistoryHardeningTests.rollout
    reference_schema = fixtures.HistoryHardeningTests.reference_schema
    add_project = fixtures.HistoryHardeningTests.add_project
    assign_refs = fixtures.HistoryHardeningTests.assign_refs

    def test_malformed_state_status_preserves_recovery_images(self):
        for index, status in enumerate(([], {}, ["completed"], {"value": "rolled_back"})):
            with self.subTest(status=status):
                run = self.base / "backups" / str(index)
                (run / "outer-snapshot").mkdir(parents=True)
                (run / "outer-snapshot" / "fixture").write_bytes(b"recovery")
                (run / "run-state.json").write_text(json.dumps({"status": status}), encoding="utf-8")
                self.assertTrue(three_way.recovery_must_be_preserved(run))
        three_way.prune_outer_snapshots(self.base / "backups", keep=0)
        three_way.rotate_three_way_backups(self.base / "backups", keep=0)
        self.assertEqual(len(list((self.base / "backups").glob("*/outer-snapshot/fixture"))), 4)

    def test_unknown_old_backup_does_not_replace_mutation_failure(self):
        roots = [self.history(name) for name in ("a", "b", "c")]
        old = self.base / "backups" / "0000-old"
        (old / "outer-snapshot").mkdir(parents=True)
        (old / "run-state.json").write_text('{"status":[]}', encoding="utf-8")
        with mock.patch.object(three_way, "execute_three_way_mutations", side_effect=RuntimeError("fixture mutation error")):
            with self.assertRaisesRegex(RuntimeError, "fixture mutation error"):
                three_way.run_three_way_sync(*roots, self.base / "backups")
        self.assertTrue((old / "outer-snapshot").is_dir())

    def test_final_cleanup_removes_asymmetrically_remaining_exact_clones_and_counts_them(self):
        roots = [self.history(name) for name in ("a", "b", "c")]
        keeper, duplicate = str(uuid.uuid4()), str(uuid.uuid4())
        for root in roots:
            self.rollout(root, keeper)
            self.rollout(root, duplicate, title="fixture" + core.CONFLICT_CLONE_SUFFIX)
        totals, warnings = three_way.purge_final_exact_duplicates(roots, self.base / "cleanup")
        self.assertEqual([set(core.load_root_snapshot(root).threads) for root in roots], [{keeper}] * 3)
        self.assertEqual(totals["duplicate_session_files_removed"], 3)
        self.assertGreater(totals["exact_duplicates_removed"], 0)
        self.assertEqual(totals["duplicate_main_threads_removed"], totals["exact_duplicates_removed"])
        self.assertEqual(totals["duplicate_auxiliary_threads_removed"], 0)

    def test_missing_local_keeper_row_never_authorizes_exact_clone_deletion(self):
        left, right = self.history("a"), self.history("b")
        keeper, duplicate = str(uuid.uuid4()), str(uuid.uuid4())
        self.rollout(left, keeper)
        self.rollout(right, keeper, title=None)
        for root in (left, right):
            self.rollout(root, duplicate, title="fixture" + core.CONFLICT_CLONE_SUFFIX)
        plan, _ = core.find_legacy_exact_clones(core.load_root_snapshot(left), core.load_root_snapshot(right))
        self.assertEqual(plan, [])

    def test_missing_clone_rollout_is_not_treated_as_proven_content(self):
        left, right = self.history("a"), self.history("b")
        keeper, duplicate = str(uuid.uuid4()), str(uuid.uuid4())
        for root in (left, right):
            self.rollout(root, keeper)
            clone = self.rollout(root, duplicate, title="fixture" + core.CONFLICT_CLONE_SUFFIX)
            if root == right:
                clone.unlink()
        plan, _ = core.find_legacy_exact_clones(core.load_root_snapshot(left), core.load_root_snapshot(right))
        self.assertEqual(plan, [])

    def project_conflict_pair(self, *, divergent=True, with_holder=False):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        self.rollout(left, owner, content="left alternate")
        self.rollout(right, owner, content="right newest" if divergent else "left alternate")
        if with_holder:
            holder = str(uuid.uuid4())
            for root in (left, right):
                self.rollout(root, holder, content="left alternate")
        for root in (left, right):
            self.reference_schema(root)
            with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
                db.execute("ALTER TABLE threads ADD COLUMN updated_at_ms INTEGER")
                db.execute("UPDATE threads SET updated_at_ms=?", (1 if root == left else 2,))
        self.add_project(left, "collision", created=10)
        self.add_project(right, "collision", created=99)
        self.add_project(right, "newest-project")
        self.assign_refs(left, owner, "collision", section_id="left-section")
        self.assign_refs(right, owner, "newest-project", section_id="right-section")
        return left, right, owner

    def profile_fingerprints(self, roots):
        return {root: {path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in root.rglob("*") if path.is_file()} for root in roots}

    def test_alternate_branch_project_conflict_is_rejected_before_any_rollout_write(self):
        left, right, _ = self.project_conflict_pair()
        before = self.profile_fingerprints((left, right))
        with self.assertRaisesRegex(core.SyncError, "身份冲突"):
            core.run_sync(left, right, self.base / "backup", "fixture", "fixture")
        self.assertEqual(before, self.profile_fingerprints((left, right)))

    def test_equal_history_does_not_import_conflicting_unused_metadata(self):
        left, right, owner = self.project_conflict_pair(divergent=False)
        result = core.run_sync(left, right, self.base / "backup", "fixture", "fixture")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["conflicts_preserved"], 0)
        for root, created in ((left, 10), (right, 99)):
            with closing(sqlite3.connect(root / "state_5.sqlite")) as db:
                self.assertEqual(db.execute("SELECT created_at_ms FROM projects WHERE id='collision'").fetchone(), (created,))
                self.assertEqual(db.execute("SELECT project_id FROM threads WHERE id=?", (owner,)).fetchone(), ("newest-project",))

    def test_prefix_history_does_not_import_conflicting_unused_metadata(self):
        left, right, owner = self.project_conflict_pair(divergent=False)
        path = Path(core.load_root_snapshot(right).threads[owner]["rollout_path"])
        path.write_bytes(path.read_bytes() + b'{"type":"event_msg","ordinal":2,"payload":{"message":"continuation"}}\n')
        result = core.run_sync(left, right, self.base / "backup", "fixture", "fixture")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["conflicts_preserved"], 0)
        self.assertEqual(result["updated_files"], 1)

    def test_existing_branch_holder_avoids_unneeded_conflicting_entity_import(self):
        left, right, _ = self.project_conflict_pair(with_holder=True)
        result = core.run_sync(left, right, self.base / "backup", "fixture", "fixture")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["conflicts_preserved"], 1)
        self.assertEqual([len(core.load_root_snapshot(root).threads) for root in (left, right)], [2, 2])

    def test_compatible_entity_preflight_does_not_reread_rollout_histories(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        for root in (left, right):
            self.rollout(root, owner)
            self.reference_schema(root)
            self.add_project(root, "shared")
            self.assign_refs(root, owner, "shared")
        snapshots = [core.load_root_snapshot(root) for root in (left, right)]
        with mock.patch.object(core, "scan_sessions", side_effect=AssertionError("unnecessary rollout scan")):
            core.preflight_thread_references(*snapshots)

    def test_final_cleanup_counts_use_final_verified_sidebar_and_index(self):
        roots = [self.history(name) for name in ("a", "b", "c")]
        keeper, duplicate = str(uuid.uuid4()), str(uuid.uuid4())
        for root in roots:
            self.rollout(root, keeper)
            self.rollout(root, duplicate, title="fixture" + core.CONFLICT_CLONE_SUFFIX)
            state_path = root / ".codex-global-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["projectless-thread-ids"] = [keeper, duplicate]
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (root / "session_index.jsonl").write_text(
                "".join(json.dumps({"id": sid, "thread_name": "fixture"}) + "\n" for sid in (keeper, duplicate)),
                encoding="utf-8",
            )
        # Leave an exact clone for the final phase, as a last pair can do.
        with mock.patch.object(core, "run_sync", return_value={"index_entries": 2, "visible_top_level_threads": 2}):
            result = three_way.execute_three_way_mutations(
                core.utc_now(), roots, {root: "fixture" for root in roots}, self.base / "run", self.base / "backups"
            )
        self.assertEqual(result["verification"][str(roots[0])]["unarchived_main_threads"], 1)
        self.assertEqual(result["index_entries"], 1)
        self.assertEqual(result["visible_top_level_threads"], 1)
        self.assertEqual(result["duplicate_session_files_removed"], 3)


if __name__ == "__main__":
    unittest.main()
