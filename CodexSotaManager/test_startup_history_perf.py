"""Real synthetic history transactions; run only with run_guarded_tests.py."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock
import uuid

import test_hardening_history as fixtures
import codex_app_lifecycle as lifecycle
import sync_codex_histories as core
import sync_codex_histories_three_way as three_way


class StartupHistoryPerformanceTests(unittest.TestCase):
    setUp = fixtures.HistoryHardeningTests.setUp
    history = fixtures.HistoryHardeningTests.history
    rollout = fixtures.HistoryHardeningTests.rollout

    def roots(self):
        return [self.history(name) for name in ("a", "b", "c")]

    def content(self, roots):
        result = {}
        for root in roots:
            files = {p.relative_to(root).as_posix(): p.read_bytes()
                     for p in root.rglob("*") if p.is_file() and ".sqlite" not in p.name}
            databases = {}
            for p in root.glob("*.sqlite"):
                with closing(sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)) as db:
                    databases[p.name] = list(db.iterdump())
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            result[root] = (files, databases)
        return result

    def snapshot(self, roots):
        run = self.base / "snapshots" / uuid.uuid4().hex
        run.mkdir(parents=True)
        return three_way.create_outer_snapshot(roots, run)

    def guard(self, root):
        return core.ModelGuardContext(
            root, root / "providers.json", {},
            global_model_candidates={"gpt-5": [("vendor", "vendor--")]},
            registered_slug_models={"vendor--gpt-5": "gpt-5"},
        )

    def model_rollout(self, root, model="vendor--gpt-5"):
        path = self.rollout(root, str(uuid.uuid4()))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "turn_context", "payload": {"model": model}}) + "\n")
        return path

    def test_noop_model_rewrite_preserves_bytes_mtime_and_creates_no_temp(self):
        root = self.history()
        path = self.model_rollout(root)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with core.history_checkpoint_scope(None), \
                mock.patch.object(core.tempfile, "mkstemp", side_effect=AssertionError("unnecessary rewrite")):
            core.rewrite_session_for_destination(path, "fixture", "fixture", self.guard(root))
            core.rewrite_session_for_destination(path, "fixture", "fixture", self.guard(root))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_cached_projection_requires_fresh_full_content_digest(self):
        root = self.history()
        path = self.model_rollout(root)
        replica = root / "sessions" / "replica.jsonl"
        replica.write_bytes(path.read_bytes())
        with core.history_checkpoint_scope(None), \
                mock.patch.object(core, "_file_sha256", wraps=core._file_sha256) as digest, \
                mock.patch.object(core, "_parse_rollout_model_index", wraps=core._parse_rollout_model_index) as parse:
            first = core.rollout_model_index(path)
            self.assertIs(core.rollout_model_index(replica), first)
            self.assertIs(core.rollout_model_index(path), first)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(digest.call_count, 3)

    def test_equal_size_and_mtime_changed_content_forces_reparse(self):
        root = self.history()
        path = self.model_rollout(root, "gpt-5")
        with core.history_checkpoint_scope(None), \
                mock.patch.object(core, "_parse_rollout_model_index", wraps=core._parse_rollout_model_index) as parse:
            first = core.rollout_model_index(path)
            stat = path.stat()
            path.write_bytes(path.read_bytes().replace(b'gpt-5', b'gpt-6'))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(path.stat().st_size, stat.st_size)
            self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
            second = core.rollout_model_index(path)
            self.assertNotEqual(first.slots, second.slots)
            self.assertEqual(parse.call_count, 2)

    def test_change_between_digest_and_parse_is_rejected_without_caching(self):
        root = self.history()
        path = self.model_rollout(root, "gpt-5")
        actual_parse = core._parse_rollout_model_index

        def changing_parse(target):
            target.write_bytes(target.read_bytes().replace(b'gpt-5', b'gpt-6'))
            return actual_parse(target)

        with core.history_checkpoint_scope(None) as work, \
                mock.patch.object(core, "_parse_rollout_model_index", side_effect=changing_parse):
            with self.assertRaisesRegex(core.SyncError, "changed during"):
                core.rollout_model_index(path)
            self.assertEqual(work.model_indexes, {})

    def test_rewrite_rechecks_cached_content_and_qualifies_new_bare_model(self):
        root = self.history()
        path = self.model_rollout(root)
        with core.history_checkpoint_scope(None):
            core.rewrite_session_for_destination(path, "fixture", "fixture", self.guard(root))
            path.write_bytes(path.read_bytes().replace(b'vendor--gpt-5', b'gpt-5'))
            context = self.guard(root)
            core.rewrite_session_for_destination(path, "fixture", "fixture", context)
            self.assertIn(b'vendor--gpt-5', path.read_bytes())
            self.assertEqual(context.normalized_fields, 1)
            self.assertEqual(core.find_unqualified_rollout_models(path, context), [])

    def test_noop_unknown_model_preserves_telemetry_and_provider_overrides(self):
        root = self.history()
        path = self.model_rollout(root, "old-model")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {
                "type": "thread_settings_applied", "thread_settings": {
                    "model_provider": "other-account", "model": "retired-model"}}}) + "\n")
        before = path.read_bytes()
        context = self.guard(root)
        with core.history_checkpoint_scope(None), \
                mock.patch.object(core.tempfile, "mkstemp", side_effect=AssertionError("noop wrote a temp")):
            core.rewrite_session_for_destination(path, "fixture", "original-account", context)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(context.unresolved_fields, 2)
        self.assertEqual([x["source_provider"] for x in context.unresolved_examples],
                         ["original-account", "other-account"])

    def test_invalid_json_never_bypasses_validation_using_projection(self):
        root = self.history()
        path = self.model_rollout(root)
        path.write_bytes(path.read_bytes() + b'{broken}\n')
        before = path.read_bytes()
        with core.history_checkpoint_scope(None):
            with self.assertRaises(core.SyncError):
                core.rewrite_session_for_destination(path, "fixture", "fixture", self.guard(root))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(".models-*")), [])

    def test_stream_comparison_has_no_stale_stat_cache(self):
        left, right = self.base / "left", self.base / "right"
        left.write_bytes(b"same-content")
        right.write_bytes(left.read_bytes())
        self.assertTrue(core.same_file_bytes(left, right))
        stat = right.stat()
        right.write_bytes(b"diff-content")
        os.utime(right, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertFalse(core.same_file_bytes(left, right))

    def test_atomic_copy_yield_discards_partial_temp_and_preserves_destination(self):
        root = self.history()
        source, target = self.base / "source", root / "sessions" / "target.jsonl"
        source.write_bytes(b"x" * (3 * 1024 * 1024))
        target.write_bytes(b"original")
        calls = [0]

        def cancel():
            calls[0] += 1
            if calls[0] == 2:
                raise lifecycle.AppNotQuiescent("launch_in_progress")

        with core.history_checkpoint_scope(cancel, lambda: None, rollback_available=True, managed_roots=[root]), \
                mock.patch.object(core, "_HISTORY_CHECK_INTERVAL", 0):
            with self.assertRaises(lifecycle.AppNotQuiescent):
                core.atomic_copy_file(source, target)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(target.parent.glob(".sync-*")), [])

    def test_header_stamp_can_yield_mid_stream_before_replacing_live_file(self):
        root = self.history()
        path = self.rollout(root, str(uuid.uuid4()), content="x" * (3 * 1024 * 1024))
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        calls = [0]

        def cancel():
            calls[0] += 1
            if calls[0] == 2:
                raise lifecycle.AppNotQuiescent("launch_in_progress")

        with core.history_checkpoint_scope(cancel, lambda: None, rollback_available=True, managed_roots=[root]), \
                mock.patch.object(core, "_HISTORY_CHECK_INTERVAL", 0):
            with self.assertRaises(lifecycle.AppNotQuiescent):
                core.set_session_model_provider(path, "new-provider")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(list(path.parent.glob(".provider-*")), [])

    def test_snapshot_cancel_mid_copy_only_removes_own_partial_image(self):
        roots = self.roots()
        self.rollout(roots[0], str(uuid.uuid4()), content="x" * (3 * 1024 * 1024))
        before = self.content(roots)
        backups = self.base / "backups"
        old = backups / "000-old" / "sentinel"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"prior recovery image")

        def cancel_partial():
            for image in backups.glob("*/outer-snapshot/cockpit/sessions/*.jsonl"):
                if image.stat().st_size >= 1024 * 1024:
                    raise lifecycle.AppNotQuiescent("launch_in_progress")

        with mock.patch.object(core, "_HISTORY_CHECK_INTERVAL", 0), \
                mock.patch.object(three_way, "restore_outer_snapshot") as restore:
            with self.assertRaises(lifecycle.AppNotQuiescent):
                three_way.run_three_way_sync(*roots, backups, quiescence_check=cancel_partial)
        restore.assert_not_called()
        self.assertEqual(self.content(roots), before)
        self.assertEqual(old.read_bytes(), b"prior recovery image")
        self.assertEqual([p.name for p in backups.iterdir()], ["000-old"])

    def test_readonly_yield_after_snapshot_does_not_restore_untouched_histories(self):
        roots = self.roots()
        before = self.content(roots)
        failure = lifecycle.AppNotQuiescent("launch_in_progress")
        with mock.patch.object(three_way, "execute_three_way_mutations", side_effect=failure), \
                mock.patch.object(three_way, "restore_outer_snapshot") as restore:
            with self.assertRaises(lifecycle.AppNotQuiescent):
                three_way.run_three_way_sync(*roots, self.base / "backups")
        restore.assert_not_called()
        self.assertEqual(failure.history_recovery_status, "not_needed")
        self.assertEqual(self.content(roots), before)
        self.assertEqual(list((self.base / "backups").iterdir()), [])

    def test_real_merge_yield_restores_every_journalled_file_and_row(self):
        roots = self.roots()
        for i, root in enumerate(roots):
            self.rollout(root, str(uuid.uuid4()), content=f"distinct-{i}")
        before = self.content(roots)
        writes = [0]

        def guard_write():
            writes[0] += 1
            if writes[0] == 8:
                raise lifecycle.AppNotQuiescent("launch_in_progress")

        actual_restore = three_way.restore_outer_snapshot
        with mock.patch.object(three_way, "restore_outer_snapshot", wraps=actual_restore) as restore, \
                mock.patch.object(three_way, "prune_outer_snapshots") as prune, \
                mock.patch.object(three_way, "rotate_three_way_backups") as rotate:
            with self.assertRaises(lifecycle.AppNotQuiescent) as raised:
                three_way.run_three_way_sync(*roots, self.base / "backups",
                                             "fixture", "fixture", "fixture",
                                             before_write_check=guard_write)
        self.assertEqual(writes[0], 8)  # Full guards are suspended during recovery.
        self.assertEqual(raised.exception.history_recovery_status, "restored")
        self.assertTrue(restore.call_args.kwargs["changed_paths"])
        self.assertEqual(self.content(roots), before)
        prune.assert_not_called()
        rotate.assert_not_called()
        run = next((self.base / "backups").iterdir())
        state = json.loads((run / "run-state.json").read_text())
        self.assertEqual(state["status"], "rolled_back")
        rollback = json.loads((run / "rollback-result.json").read_text())
        self.assertEqual(rollback["restoration_scope"], "recorded_mutations")

    def test_selective_restore_recovers_delete_move_create_and_sqlite_without_copying_untouched(self):
        roots = self.roots()
        changed = self.rollout(roots[0], str(uuid.uuid4()))
        deleted = self.rollout(roots[1], str(uuid.uuid4()))
        moved = self.rollout(roots[2], str(uuid.uuid4()))
        untouched = self.rollout(roots[0], str(uuid.uuid4()), content="untouched")
        before = self.content(roots)
        snapshot = self.snapshot(roots)
        relocated = roots[2] / "archived_sessions" / "new-dir" / moved.name
        created = roots[0] / "sessions" / "new-dir" / "created.jsonl"
        database = roots[1] / "state_5.sqlite"
        with core.history_checkpoint_scope(None, rollback_available=True, managed_roots=roots) as work:
            core.atomic_write_bytes(changed, b"mutated")
            core.before_history_mutation(deleted)
            deleted.unlink()
            core.before_history_mutation(moved)
            core.before_history_mutation(relocated)
            relocated.parent.mkdir(parents=True)
            moved.rename(relocated)
            core.atomic_write_bytes(created, b"created")
            core.before_history_mutation(database)
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("INSERT INTO threads(id,title) VALUES('new','new')")
            actual_copy = core.atomic_copy_file
            with core.suspend_history_checkpoints(), \
                    mock.patch.object(core, "atomic_copy_file", wraps=actual_copy) as copy:
                result = three_way.restore_outer_snapshot(snapshot, changed_paths=work.mutated_paths)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["restoration_scope"], "recorded_mutations")
        self.assertEqual(self.content(roots), before)
        self.assertFalse(created.parent.exists())
        self.assertFalse(relocated.parent.exists())
        self.assertFalse(any(Path(call.args[1]) == untouched for call in copy.call_args_list))

    def test_unknown_journal_target_refuses_recovery_outside_snapshot_roots(self):
        roots = self.roots()
        snapshot = self.snapshot(roots)
        outside = self.base / "outside"
        outside.write_bytes(b"must remain")
        with self.assertRaisesRegex(core.SyncError, "escaped"):
            three_way.restore_recorded_mutations(snapshot, {outside})
        self.assertEqual(outside.read_bytes(), b"must remain")

    def test_directory_bootstrap_journal_falls_back_to_full_recovery(self):
        roots = self.roots()
        snapshot = self.snapshot(roots)
        extra = roots[2] / "sessions" / "new.jsonl"
        extra.write_bytes(b"new")
        result = three_way.restore_outer_snapshot(snapshot, changed_paths={roots[2]})
        self.assertEqual(result["status"], "restored")
        self.assertNotIn("restoration_scope", result)
        self.assertFalse(extra.exists())

    def test_missing_required_snapshot_file_refuses_selective_recovery(self):
        roots = self.roots()
        snapshot = self.snapshot(roots)
        target = roots[0] / ".codex-global-state.json"
        (snapshot["root"] / "cockpit" / target.name).unlink()
        target.write_bytes(b"failed state must be retained")
        with self.assertRaisesRegex(core.SyncError, "missing a recorded"):
            three_way.restore_outer_snapshot(snapshot, changed_paths={target})
        self.assertEqual(target.read_bytes(), b"failed state must be retained")

    def test_selective_restore_failure_retains_failed_state_and_recovery_required_marker(self):
        roots = self.roots()
        target = roots[0] / ".codex-global-state.json"

        def mutate_then_yield(*_args):
            core.atomic_write_bytes(target, b"mutated")
            raise lifecycle.AppNotQuiescent("launch_in_progress")

        actual_copy = core.atomic_copy_file

        def fail_original_copy(source, destination, *args, **kwargs):
            if "outer-snapshot" in Path(source).parts and Path(destination) == target:
                raise PermissionError("fixture recovery blocked")
            return actual_copy(source, destination, *args, **kwargs)

        with mock.patch.object(three_way, "execute_three_way_mutations", side_effect=mutate_then_yield), \
                mock.patch.object(core, "atomic_copy_file", side_effect=fail_original_copy):
            with self.assertRaisesRegex(core.SyncError, "rollback was incomplete"):
                three_way.run_three_way_sync(*roots, self.base / "backups")
        run = next((self.base / "backups").iterdir())
        self.assertEqual(json.loads((run / "run-state.json").read_text())["status"], "recovery_required")
        self.assertTrue(three_way.recovery_must_be_preserved(run))
        self.assertEqual(target.read_bytes(), b"mutated")
        self.assertEqual((run / "failed-mutated-state" / "cockpit" / target.name).read_bytes(), b"mutated")
        self.assertTrue((run / "outer-snapshot" / "cockpit" / target.name).is_file())

    def test_standalone_pair_finishes_after_first_write_instead_of_yielding_partial_state(self):
        left, right = self.history("a"), self.history("b")
        self.rollout(left, str(uuid.uuid4()), content="left only")
        self.rollout(right, str(uuid.uuid4()), content="right only")
        guard_calls = [0]

        def checkpoint():
            work = core._HISTORY_WORK.get()
            if work.mutation_started:
                raise lifecycle.AppNotQuiescent("unsafe pair yield")

        def before_write():
            guard_calls[0] += 1

        result = core.run_sync(left, right, self.base / "backup", "fixture", "fixture",
                               quiescence_check=checkpoint, before_write_check=before_write)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(guard_calls[0], 1)
        self.assertEqual(set(core.load_root_snapshot(left).threads), set(core.load_root_snapshot(right).threads))

    def test_scratch_writes_do_not_mark_history_mutated(self):
        root = self.history()
        guard = mock.Mock()
        with core.history_checkpoint_scope(guard, managed_roots=[root]) as work:
            core.atomic_write_bytes(self.base / "scratch", b"scratch")
            self.assertFalse(work.mutation_started)
            self.assertEqual(work.mutated_paths, set())
        guard.assert_not_called()

    def test_lexical_escape_is_rejected_before_write(self):
        root = self.history()
        escaped = root / ".." / "escaped"
        with core.history_checkpoint_scope(None, managed_roots=[root]):
            with self.assertRaisesRegex(core.SyncError, "outside its managed root"):
                core.atomic_write_bytes(escaped, b"must not write")
        self.assertFalse((self.base / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
