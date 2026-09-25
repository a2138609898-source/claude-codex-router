"""Synthetic-only regression tests. Never opens or mutates real profile history."""
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

import codex_app_lifecycle as lifecycle
import repair_sota_launch_history as repair
import sync_codex_histories as core


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-history-repair-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / ".codex-fixture"
        self.root.mkdir()
        self.backup = self.base / "backup"
        self.thread_id = str(uuid.uuid4())
        self.old = self.root / "sessions" / "2026" / "09" / "23" / ("rollout-conflict-" + self.thread_id + ".jsonl")
        self.old.parent.mkdir(parents=True)
        self.header = {"type": "session_meta", "ordinal": 0, "timestamp": "2026-09-23T11:00:00Z",
                       "payload": {"id": self.thread_id, "session_id": self.thread_id,
                                   "timestamp": "2026-09-23T11:00:00Z", "history_mode": "paginated"}}
        self.old.write_text(json.dumps(self.header) + "\n" + json.dumps({"type": "event_msg", "ordinal": 1,
                                                                     "payload": {"type": "agent_message", "message": "fixture-only"}}) + "\n", encoding="utf-8")
        self.original = self.old.read_bytes()
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.executescript("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,title TEXT,archived INTEGER,archived_at INTEGER);"
                             "CREATE TABLE rollout_migration_skipped_rollouts(rollout_path TEXT PRIMARY KEY, skip_reason TEXT);")
            db.execute("INSERT INTO threads VALUES(?,?,?,0,NULL)", (self.thread_id, str(self.old), "fixture" + repair.CONFLICT_SUFFIX))
            db.execute("INSERT INTO rollout_migration_skipped_rollouts VALUES(?,?)", (str(self.old), "fixture-only"))
        with closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE path_cache(thread_id TEXT,rollout_path TEXT)")
            db.execute("INSERT INTO path_cache VALUES(?,?)", (self.thread_id, str(self.old)))
        self.index = self.root / "session_index.jsonl"
        self.index.write_text(json.dumps({"id": self.thread_id, "rollout_path": str(self.old), "thread_name": "fixture"}) + "\n", encoding="utf-8")
        self.state = self.root / ".codex-global-state.json"
        self.state.write_text(json.dumps({"typed": {"rollout_path": str(self.old)}, "text": str(self.old),
                                          "pinned-thread-ids": [self.thread_id]}), encoding="utf-8")
        self.closed = patch.object(repair, "require_codex_closed", return_value=None).start()
        self.addCleanup(patch.stopall)
        patch.object(core, "LOCK_PATH", self.base / "sync.lock").start()
        patch.object(lifecycle, "LIFECYCLE_LOCK_PATH", self.base / "lifecycle.lock").start()

    def plan(self):
        return repair.build_repair_plan([self.root])

    def row(self):
        return repair.read_rows(self.root)[self.thread_id]

    def duplicate_fixture(self):
        keeper = str(uuid.uuid4())
        parent = self.old.parent / ("rollout-2026-09-23T10-00-00-" + keeper + ".jsonl")
        header = {"type": "session_meta", "ordinal": 0, "timestamp": "2026-09-23T10:00:00Z",
                  "payload": {"id": keeper, "session_id": keeper, "timestamp": "2026-09-23T10:00:00Z"}}
        parent.write_text(json.dumps(header) + "\n" + json.dumps({"type": "event_msg", "ordinal": 1,
                                                               "payload": {"type": "agent_message", "message": "shared-fixture"}}) + "\n", encoding="utf-8")
        base = {"thread_id": keeper, "end_ordinal_exclusive": 2, "end_byte_offset": parent.stat().st_size}
        page_id = str(uuid.uuid4())
        head = self.old.parent / ("rollout-2026-09-23T11-00-00-" + keeper + "_" + page_id + ".jsonl")
        for path, owner in ((head, keeper), (self.old, self.thread_id)):
            item = {"type": "session_meta", "ordinal": 2, "timestamp": "2026-09-23T11:00:00Z",
                    "payload": {"id": owner, "session_id": owner, "timestamp": "2026-09-23T11:00:00Z", "history_base": base}}
            path.write_text(json.dumps(item) + "\n" + json.dumps({"type": "event_msg", "ordinal": 3,
                                                               "payload": {"type": "agent_message", "message": "same-tail"}}) + "\n", encoding="utf-8")
        self.original = self.old.read_bytes()
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("INSERT INTO threads VALUES(?,?,?,0,NULL)", (keeper, str(head), "fixture"))
        return keeper, head, parent

    def test_default_audit_does_not_change_any_profile_file(self):
        before = {p: repair.fingerprint(p) for p in self.root.rglob("*") if p.is_file()}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main(["--roots", str(self.root), "--summary"]), 0)
        self.assertEqual(before, {p: repair.fingerprint(p) for p in self.root.rglob("*") if p.is_file()})

    def test_native_paginated_names_are_already_canonical(self):
        page_id = str(uuid.uuid4())
        name = "rollout-2026-09-23T11-00-00-" + self.thread_id + "_" + page_id + ".jsonl"
        self.assertTrue(repair.CANONICAL.fullmatch(name))
        self.assertEqual(repair.physical_id(Path(name)), page_id)
        self.assertEqual(repair.canonical_name(Path(name), {}), name)

    def test_legacy_head_preserves_logical_and_physical_ids(self):
        page_id = str(uuid.uuid4())
        page = {"page_id": page_id, "owner": self.thread_id, "timestamp": "2026-09-23T11:00:00Z", "metadata": {}}
        name = repair.canonical_name(Path("rollout-sync-" + page_id + ".jsonl"), page)
        self.assertTrue(name.endswith(self.thread_id + "_" + page_id + ".jsonl"))

    def test_apply_updates_all_typed_paths_and_preserves_text_and_bytes(self):
        plan = self.plan()
        target = Path(plan["profiles"][0]["operations"][0]["destination"])
        result = repair.apply_plan(plan, self.backup)
        self.assertEqual(result["status"], "applied")
        self.assertFalse(self.old.exists())
        self.assertEqual(target.read_bytes(), self.original)
        self.assertEqual(self.row()["rollout_path"], str(target))
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT rollout_path FROM rollout_migration_skipped_rollouts").fetchone()[0], str(target))
        with closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as db:
            self.assertEqual(db.execute("SELECT rollout_path FROM path_cache").fetchone()[0], str(target))
        self.assertEqual(json.loads(self.index.read_text())["rollout_path"], str(target))
        state = json.loads(self.state.read_text())
        self.assertEqual(state["typed"]["rollout_path"], str(target))
        self.assertEqual(state["text"], str(self.old))
        self.assertTrue((self.backup / self.root.name / "state_5.sqlite").is_file())

    def test_lineage_ids_and_byte_offsets_are_unchanged(self):
        child_id = str(uuid.uuid4())
        child = self.old.parent / ("rollout-2026-09-23T12-00-00-" + child_id + ".jsonl")
        header = {"type": "session_meta", "ordinal": 2, "timestamp": "2026-09-23T12:00:00Z",
                  "payload": {"id": child_id, "history_base": {"thread_id": self.thread_id,
                              "end_ordinal_exclusive": 2, "end_byte_offset": len(self.original)}}}
        child.write_text(json.dumps(header) + "\n", encoding="utf-8")
        child_before = child.read_bytes()
        core.validate_lineage([self.root], check_offsets=True)
        repair.apply_plan(self.plan(), self.backup)
        self.assertEqual(child.read_bytes(), child_before)
        core.validate_lineage([self.root], check_offsets=True)

    def test_running_app_defers_without_backup_or_profile_writes(self):
        plan_path = self.base / "queue.json"
        repair.save_plan(plan_path, self.plan())
        with patch.object(repair, "require_codex_closed", side_effect=lifecycle.AppNotQuiescent("app_running")), redirect_stdout(io.StringIO()):
            code = repair.main(["--execute-plan", str(plan_path), "--apply", "--backup", str(self.backup), "--defer-if-app-running"])
        self.assertEqual(code, 75)
        self.assertTrue(self.old.exists())
        self.assertFalse(self.backup.exists())
        self.assertTrue(plan_path.exists())

    def test_changed_rollout_refuses_without_backup(self):
        plan = self.plan()
        with self.old.open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaises(repair.PlanDrift):
            repair.apply_plan(plan, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.old.exists())

    def test_unrelated_cache_and_thread_changes_after_queue_are_preserved(self):
        plan = self.plan()
        state = json.loads(self.state.read_text())
        state["unrelated-window-size"] = 1234
        self.state.write_text(json.dumps(state), encoding="utf-8")
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("UPDATE threads SET title='new user title' WHERE id=?", (self.thread_id,))
        repair.apply_plan(plan, self.backup)
        self.assertEqual(json.loads(self.state.read_text())["unrelated-window-size"], 1234)
        self.assertEqual(self.row()["title"], "new user title")

    def test_saved_plan_tampering_is_rejected(self):
        path = self.base / "plan.json"
        plan = self.plan()
        repair.save_plan(path, plan)
        modified = json.loads(path.read_text())
        modified["profiles"][0]["operations"][0]["before"]["size"] += 1
        with self.assertRaises(repair.PlanDrift):
            repair.apply_plan(modified, self.backup)

    def test_destination_collision_never_overwrites(self):
        plan = self.plan()
        target = Path(plan["profiles"][0]["operations"][0]["destination"])
        target.write_bytes(b"unrelated")
        with self.assertRaises(repair.PlanDrift):
            repair.apply_plan(plan, self.backup)
        self.assertEqual(target.read_bytes(), b"unrelated")
        self.assertEqual(self.old.read_bytes(), self.original)

    def test_failure_after_rename_rolls_back_everything(self):
        plan = self.plan()
        original_update = repair.update_database_paths
        calls = 0
        def fail_once(db, mapping):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected after rename")
            return original_update(db, mapping)
        with patch.object(repair, "update_database_paths", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                repair.apply_plan(plan, self.backup)
        self.assertEqual(self.old.read_bytes(), self.original)
        self.assertEqual(self.row()["rollout_path"], str(self.old))
        self.assertEqual(json.loads((self.backup / "manifest.json").read_text())["status"], "rolled_back")

    def test_resume_after_crash_between_rename_and_database_commit(self):
        plan = self.plan()
        manifest = repair.prepare_backup(plan, self.backup)
        manifest["status"] = "applying"
        repair.write_manifest(self.backup, manifest)
        target = Path(plan["profiles"][0]["operations"][0]["destination"])
        self.old.rename(target)
        self.assertEqual(repair.apply_plan(plan, self.backup)["status"], "applied")
        self.assertEqual(self.row()["rollout_path"], str(target))

    def test_successful_plan_is_idempotent(self):
        plan = self.plan()
        repair.apply_plan(plan, self.backup)
        self.assertEqual(repair.apply_plan(plan, self.backup)["status"], "already-applied")

    def test_applied_receipt_never_replays_after_user_continues_history(self):
        plan = self.plan()
        repair.apply_plan(plan, self.backup)
        target = Path(self.row()["rollout_path"])
        with target.open("ab") as stream:
            stream.write(b'{"type":"event_msg","payload":{"message":"new fixture data"}}\n')
        continued = target.read_bytes()
        self.assertEqual(repair.apply_plan(plan, self.backup)["status"], "already-applied")
        self.assertEqual(target.read_bytes(), continued)

    def test_rollback_restores_paths_and_original_cache_bytes(self):
        before_index, before_state = self.index.read_bytes(), self.state.read_bytes()
        plan = self.plan()
        repair.apply_plan(plan, self.backup)
        result = repair.rollback_backup(self.backup)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.old.read_bytes(), self.original)
        self.assertEqual(self.row()["rollout_path"], str(self.old))
        self.assertEqual(self.index.read_bytes(), before_index)
        self.assertEqual(self.state.read_bytes(), before_state)

    def test_rollback_refuses_new_user_cache_changes(self):
        repair.apply_plan(self.plan(), self.backup)
        self.state.write_text('{"new user data":true}', encoding="utf-8")
        with self.assertRaises(repair.PlanDrift):
            repair.rollback_backup(self.backup)
        self.assertEqual(self.state.read_text(), '{"new user data":true}')

    def test_queue_is_recoverably_renamed_only_after_success(self):
        plan = self.plan()
        queue = self.base / "pending.json"
        repair.save_plan(queue, plan)
        with redirect_stdout(io.StringIO()):
            code = repair.main(["--execute-plan", str(queue), "--apply", "--backup", str(self.backup)])
        self.assertEqual(code, 0)
        self.assertFalse(queue.exists())
        self.assertTrue(queue.with_name("pending.completed-" + plan["plan_id"] + ".json").exists())

    def test_unknown_path_bearing_header_is_rejected_before_writes(self):
        self.header["payload"]["rollout_path"] = str(self.old)
        self.old.write_text(json.dumps(self.header) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(repair.RepairError, "Path-bearing"):
            self.plan()

    def test_destination_escape_is_rejected(self):
        plan = self.plan()
        plan["profiles"][0]["operations"][0]["destination"] = str(self.base / "outside.jsonl")
        with self.assertRaises(repair.RepairError):
            repair.apply_plan(plan, self.backup)

    def test_defer_after_backup_keeps_queue_retryable_with_new_cache(self):
        plan = self.plan()
        with patch.object(repair, "require_codex_closed", side_effect=[None, lifecycle.AppNotQuiescent("launch_requested")]):
            with self.assertRaises(lifecycle.AppNotQuiescent):
                repair.apply_plan(plan, self.backup)
        self.assertEqual(json.loads((self.backup / "manifest.json").read_text())["status"], "prepared")
        self.assertTrue(self.old.exists())
        state = json.loads(self.state.read_text())
        state["new-shutdown-state"] = 42
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(repair.apply_plan(plan, self.backup)["status"], "applied")
        self.assertEqual(json.loads(self.state.read_text())["new-shutdown-state"], 42)

    def test_cache_drift_after_rename_cannot_strand_rollout(self):
        plan = self.plan()
        original_update = repair.update_database_paths
        calls = 0
        def drift_once(db, mapping):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.state.write_text('{"new user cache":true}', encoding="utf-8")
            return original_update(db, mapping)
        with patch.object(repair, "update_database_paths", side_effect=drift_once):
            with self.assertRaises(repair.PlanDrift):
                repair.apply_plan(plan, self.backup)
        self.assertTrue(self.old.exists())
        self.assertEqual(self.row()["rollout_path"], str(self.old))
        self.assertEqual(self.state.read_text(), '{"new user cache":true}')
        manifest = json.loads((self.backup / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "rolled_back")
        self.assertEqual(manifest["preserved_changed_caches"], [str(self.state)])

    def test_completed_receipt_does_not_poison_repeated_queue(self):
        plan = self.plan()
        queue = self.base / "pending.json"
        repair.save_plan(queue, plan)
        args = ["--execute-plan", str(queue), "--apply", "--backup", str(self.backup)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main(args), 0)
        repair.save_plan(queue, plan)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main(args), 0)
        self.assertFalse(queue.exists())
        self.assertEqual(len(list(self.base.glob("pending.duplicate-receipt-*.json"))), 1)

    def test_exact_clone_is_archived_recoverably_without_deleting_any_history(self):
        keeper, head, parent = self.duplicate_fixture()
        pairs = repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)])
        plan = repair.build_repair_plan([self.root], pairs)
        result = repair.apply_plan(plan, self.backup)
        self.assertEqual(result["archived_rows"], 1)
        self.assertEqual(result["deleted_history_files"], 0)
        row = self.row()
        self.assertEqual(row["archived"], 1)
        self.assertEqual(Path(row["rollout_path"]).read_bytes(), self.original)
        self.assertIn("verified-launch-duplicates", row["rollout_path"])
        self.assertEqual(repair.read_rows(self.root)[keeper]["archived"], 0)
        self.assertTrue(parent.exists())
        self.assertTrue(head.exists())
        self.assertEqual(self.index.read_text(), "")
        repair.rollback_backup(self.backup)
        self.assertEqual(self.row()["archived"], 0)
        self.assertEqual(self.old.read_bytes(), self.original)
        self.assertEqual(json.loads(self.index.read_text())["id"], self.thread_id)

    def test_real_branch_is_never_archived(self):
        keeper, head, _ = self.duplicate_fixture()
        head.write_text(head.read_text().replace("same-tail", "distinct-tail"), encoding="utf-8")
        with self.assertRaisesRegex(repair.RepairError, "distinct or prefix-only"):
            repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)])
        self.assertEqual(self.row()["archived"], 0)

    def test_prefix_only_candidate_is_preserved(self):
        keeper, _, _ = self.duplicate_fixture()
        self.old.write_text(self.old.read_text().splitlines(keepends=True)[0], encoding="utf-8")
        with self.assertRaisesRegex(repair.RepairError, "distinct or prefix-only"):
            repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)])

    def test_same_title_and_equal_bytes_without_ancestry_are_not_proof(self):
        keeper = str(uuid.uuid4())
        path = self.old.parent / ("rollout-2026-09-23T11-00-00-" + keeper + ".jsonl")
        path.write_text(self.old.read_text().replace(self.thread_id, keeper), encoding="utf-8")
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("INSERT INTO threads VALUES(?,?,?,0,NULL)", (keeper, str(path), "fixture"))
        with self.assertRaisesRegex(repair.RepairError, "No shared physical ancestry"):
            repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)])

    def test_ancestor_drift_rejects_saved_exact_proof(self):
        keeper, _, parent = self.duplicate_fixture()
        plan = repair.build_repair_plan([self.root], repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)]))
        with parent.open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(repair.PlanDrift, "comparison source changed"):
            repair.apply_plan(plan, self.backup)
        self.assertFalse(self.backup.exists())

    def test_referenced_clone_cannot_be_archived(self):
        keeper, _, _ = self.duplicate_fixture()
        child_id = str(uuid.uuid4())
        path = self.old.parent / ("rollout-2026-09-23T11-00-00-" + child_id + ".jsonl")
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": child_id,
                              "history_base": {"thread_id": self.thread_id, "end_ordinal_exclusive": 4,
                                               "end_byte_offset": self.old.stat().st_size}}}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(repair.RepairError, "lineage/agent dependency"):
            repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)])

    def test_exact_family_cli_builds_only_a_reviewed_dry_run(self):
        keeper, _, _ = self.duplicate_fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(repair.main(["--roots", str(self.root), "--exact-family", keeper]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(result["exact_duplicate_pairs"], 1)
        self.assertEqual(result["archive_rows"], 1)
        self.assertEqual(self.row()["archived"], 0)

    def test_refresh_plan_after_drift_reproves_only_the_saved_family(self):
        keeper, _, _ = self.duplicate_fixture()
        pairs = repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)])
        original = repair.build_repair_plan([self.root], pairs)
        refreshed = repair.refresh_plan_after_drift(original)
        self.assertNotEqual(refreshed["plan_id"], original["plan_id"])
        self.assertEqual(refreshed["duplicate_pairs"][0]["keep"], keeper)
        self.assertEqual(refreshed["duplicate_pairs"][0]["duplicate"], self.thread_id)
        self.assertEqual(refreshed["profiles"][0]["archive_ids"], [self.thread_id])
        self.assertEqual(self.row()["archived"], 0)

    def test_execute_queue_refreshes_once_after_target_drift(self):
        keeper, _, _ = self.duplicate_fixture()
        pairs = repair.prove_exact_pairs([self.root], [(keeper, self.thread_id)])
        queue = self.base / "pending.json"
        repair.save_plan(queue, repair.build_repair_plan([self.root], pairs))
        real_apply = repair.apply_plan
        calls = 0

        def drift_once(plan, backup=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise repair.PlanDrift("synthetic target drift")
            return real_apply(plan, backup)

        with patch.object(repair, "apply_plan", side_effect=drift_once), redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main([
                "--execute-plan", str(queue), "--apply", "--refresh-on-drift"
            ]), 0)
        self.assertEqual(calls, 2)
        self.assertFalse(queue.exists())
        self.assertEqual(len(list(self.base.glob("pending.drifted-*.json"))), 1)
        self.assertEqual(self.row()["archived"], 1)

    def test_unproven_pair_payload_is_rejected(self):
        with self.assertRaisesRegex(repair.RepairError, "full per-profile"):
            repair.build_repair_plan([self.root], [{"keep": str(uuid.uuid4()), "duplicate": self.thread_id}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
