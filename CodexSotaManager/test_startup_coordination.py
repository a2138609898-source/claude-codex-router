"""Fixture-only launch intent, safe writer handoff, and watcher regressions."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import codex_app_lifecycle as lifecycle
from codex_launch_controller import LaunchAttempt
import sync_after_codex_exit as watcher


class FakeLauncher:
    pid = 902

    def __init__(self, code=0):
        self.code = code

    def poll(self):
        return self.code


class StartupCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = [100.0]
        self.wall = [1000.0]
        self.requests = self.root / "work" / "codex-launch-requests"
        self.legacy = self.root / "work" / "codex-launch-request.json"
        self.guard = self.root / "work" / "codex-launch-requests.lock"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("LAUNCH_REQUEST_PATH", self.legacy),
                            ("LAUNCH_REQUEST_DIR", self.requests),
                            ("LAUNCH_REQUEST_GUARD_PATH", self.guard)):
            self.stack.enter_context(mock.patch.object(lifecycle, name, value))
        self.stack.enter_context(mock.patch.object(lifecycle.time, "time", side_effect=lambda: self.wall[0]))
        # No actual app enumeration is needed: all test owners use exact birth
        # identities from this synthetic process table.
        self.live_owners = {os.getpid(): "manager-birth", 901: "shell-birth"}
        self.stack.enter_context(mock.patch.object(lifecycle, "process_identity", side_effect=self.identity))
        self.calls = []

    def identity(self, pid):
        ticks = self.live_owners.get(pid)
        return {"pid": pid, "creation_ticks": ticks} if ticks is not None else None

    def lease(self, request="abc1"):
        return lifecycle.LaunchRequestLease(self.root, request,
                                           clock=lambda: self.now[0], wall_clock=lambda: self.wall[0])

    def job(self, replies, *, request="abc1", invocation=None):
        replies = iter(replies)

        def popen(command, **kwargs):
            process, reply = next(replies)
            self.calls.append(command)
            if reply is not None:
                kwargs["stdout"].write((json.dumps({**reply, "request_id": request}) + "\n").encode())
            return process

        attempt = LaunchAttempt(invocation or ["powershell.exe", "-File", "fixture.ps1"],
                                self.root, self.root, lambda: [], request_id=request,
                                clock=lambda: self.now[0], popen=popen)
        attempt.request_lease.wall_clock = lambda: self.wall[0]
        self.addCleanup(attempt.detach)
        return attempt

    def test_request_is_published_before_slow_preflight_and_survives_child_exit(self):
        attempt = self.job([(FakeLauncher(75), {"status": "deferred", "phase": "waiting_for_history_sync"})])
        popen = attempt.popen

        def check_at_spawn(*args, **kwargs):
            self.assertTrue(lifecycle.launch_pending(), "sync must see intent before shell preflight")
            return popen(*args, **kwargs)

        attempt.popen = check_at_spawn
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.assertIsNone(attempt.process)
        self.assertTrue(lifecycle.launch_pending(), "a dead deferred child is not a cancelled manager request")
        self.assertEqual(len(self.calls), 1)

    def test_retry_backoff_keeps_renewing_same_request_beyond_shell_expiry(self):
        attempt = self.job([(FakeLauncher(75), {"status": "deferred", "phase": "waiting_for_history_sync"})
                            for _ in range(10)])
        for _ in range(10):
            self.now[0] = max(self.now[0], attempt.next_retry)
            self.wall[0] += 31.0
            self.assertEqual(attempt.poll()["status"], "deferred")
            self.assertTrue(lifecycle.launch_pending())
        self.assertGreater(self.wall[0], 1300)
        self.assertEqual(len(list(self.requests.glob("*.json"))), 1)
        self.assertEqual(len({tuple(command) for command in self.calls}), 1)

    def test_cancel_retires_only_queued_manager_request(self):
        attempt = self.job([(FakeLauncher(75), {"status": "deferred"})])
        attempt.poll()
        self.assertTrue(lifecycle.launch_pending())
        self.assertTrue(attempt.cancel_queued())
        self.assertFalse(lifecycle.launch_pending())
        self.assertEqual(attempt.poll()["status"], "cancelled")
        self.assertEqual(len(self.calls), 1)

    def test_detach_does_not_delete_an_active_shells_intent(self):
        attempt = self.job([(FakeLauncher(None), None)])
        attempt.poll()
        self.legacy.write_text(json.dumps({"request_id": attempt.request_id, "launcher_pid": 901,
                                          "launcher_creation_ticks": "shell-birth", "expires_at": 1060}), encoding="utf-8")
        attempt.detach()
        self.assertTrue(lifecycle.launch_pending())
        self.assertEqual(attempt.poll()["status"], "detached")
        self.assertTrue(self.legacy.exists())

    def test_dead_or_recycled_manager_request_does_not_block_future_sync(self):
        lease = self.lease()
        lease.refresh()
        self.live_owners[os.getpid()] = "other-birth"
        self.assertFalse(lifecycle.launch_pending())
        self.assertFalse(lease.path.exists())

    def test_unrenewed_manager_lease_expires_and_later_refresh_republishes(self):
        lease = self.lease()
        lease.refresh()
        self.wall[0] += lifecycle.LAUNCH_REQUEST_LEASE_SECONDS + 1
        self.assertFalse(lifecycle.launch_pending())
        self.now[0] += lifecycle.LAUNCH_REQUEST_REFRESH_SECONDS + 1
        lease.refresh()
        self.assertTrue(lifecycle.launch_pending())

    def test_two_launch_owners_cannot_erase_each_others_requests(self):
        first, second = self.lease("abc1"), self.lease("abc2")
        first.refresh()
        second.refresh()
        first.release()
        self.assertTrue(lifecycle.launch_pending())
        second.release()
        self.assertFalse(lifecycle.launch_pending())

    def test_old_controller_cannot_retire_a_new_owner_of_same_request_id(self):
        first, replacement = self.lease(), self.lease()
        first.refresh()
        replacement.refresh()
        first.release()
        self.assertTrue(lifecycle.launch_pending())
        replacement.release()
        self.assertFalse(lifecycle.launch_pending())

    def test_busy_publication_guard_fails_closed(self):
        lease = self.lease()
        lease.refresh()
        with lifecycle.FileLock(self.guard):
            self.assertTrue(lifecycle.launch_pending())

    def test_explicit_sync_does_not_publish_self_cancelling_intent(self):
        attempt = self.job([(FakeLauncher(None), None)], invocation=["powershell.exe", "-SyncBeforeLaunch"])
        attempt.poll()
        self.assertFalse(lifecycle.launch_pending())

    def test_spawn_error_retires_request_without_touching_history(self):
        attempt = self.job([])
        attempt.popen = mock.Mock(side_effect=OSError("fixture spawn error"))
        with self.assertRaisesRegex(OSError, "fixture spawn error"):
            attempt.poll()
        self.assertFalse(lifecycle.launch_pending())

    def test_writer_release_wakes_launch_before_long_backoff_expires(self):
        attempt = self.job([(FakeLauncher(75), {"status": "deferred", "phase": "waiting_for_history_sync"}),
                            (FakeLauncher(1), {"status": "error", "error": "fixture finished"})])
        lifecycle_path, sync_path = self.root / "work/codex-app-lifecycle.lock", self.root / "sync.lock"
        with lifecycle.FileLock(lifecycle_path), lifecycle.FileLock(sync_path):
            self.assertEqual(attempt.poll()["status"], "deferred")
            attempt.next_retry = self.now[0] + 30
            self.now[0] += 0.1
            self.assertEqual(attempt.poll()["status"], "deferred")
            self.assertEqual(len(self.calls), 1, "a live writer still blocks early retry")
        self.assertEqual(attempt.poll()["status"], "error")
        self.assertEqual(len(self.calls), 2, "safe writer release should wake the same request promptly")

    def test_legacy_sync_lock_alone_blocks_early_retry(self):
        attempt = self.job([(FakeLauncher(75), {"status": "deferred", "phase": "waiting_for_history_sync"})])
        with lifecycle.FileLock(self.root / "work/codex-app-lifecycle.lock"):
            pass
        with lifecycle.FileLock(self.root / "sync.lock"):
            attempt.poll()
            self.now[0] += 0.1
            self.assertEqual(attempt.poll()["status"], "deferred")
            self.assertEqual(len(self.calls), 1)

    def test_checkpoint_checks_requests_even_between_throttled_app_probes(self):
        state = mock.Mock(last_app_probe=100)
        with mock.patch.object(lifecycle, "_checkpoint_state", state), \
                mock.patch.object(lifecycle.time, "monotonic", return_value=100.1), \
                mock.patch.object(lifecycle, "app_running") as scan:
            lifecycle.quiescence_checkpoint()
            scan.assert_not_called()
            self.lease().refresh()
            with self.assertRaisesRegex(lifecycle.AppNotQuiescent, "launch_requested"):
                lifecycle.quiescence_checkpoint()
            scan.assert_not_called()


class WatcherWriteCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(watcher, "INSTALL_DIR", self.root))
        self.stack.enter_context(mock.patch.object(lifecycle, "LIFECYCLE_LOCK_PATH", self.root / "lifecycle.lock"))
        self.stack.enter_context(mock.patch.object(watcher.ARCHIVE_SIDEBAR_REPAIR_SCRIPT.__class__, "is_file", return_value=True))

    def test_launch_between_quiet_period_and_repair_prevents_any_spawn(self):
        with mock.patch.object(watcher, "assert_quiescent", side_effect=lifecycle.AppNotQuiescent("launch_requested")), \
                mock.patch.object(watcher.subprocess, "run") as run:
            self.assertEqual(watcher.run_archived_sidebar_repair()["status"], "deferred")
            run.assert_not_called()

    def test_lifecycle_writer_defers_sidebar_repair_without_spawn(self):
        with lifecycle.FileLock(self.root / "lifecycle.lock"), \
                mock.patch.object(watcher, "assert_quiescent"), \
                mock.patch.object(watcher.subprocess, "run") as run:
            self.assertEqual(watcher.run_archived_sidebar_repair()["status"], "deferred")
            run.assert_not_called()

    def test_legacy_sync_writer_also_defers_sidebar_repair(self):
        with lifecycle.FileLock(self.root / "sync.lock"), \
                mock.patch.object(watcher, "assert_quiescent"), \
                mock.patch.object(watcher.subprocess, "run") as run:
            self.assertEqual(watcher.run_archived_sidebar_repair()["status"], "deferred")
            run.assert_not_called()

    def test_sidebar_repair_holds_both_writer_leases_during_child(self):
        def repair(*args, **kwargs):
            for path in (self.root / "lifecycle.lock", self.root / "sync.lock"):
                with self.assertRaises(lifecycle.LifecycleBusy):
                    with lifecycle.FileLock(path):
                        self.fail("repair must hold both locks")
            return subprocess.CompletedProcess([], 0, "fixture repaired", "")

        with mock.patch.object(watcher, "assert_quiescent") as check, \
                mock.patch.object(watcher.subprocess, "run", side_effect=repair):
            self.assertEqual(watcher.run_archived_sidebar_repair()["status"], "ok")
            self.assertEqual(check.call_count, 2)


if __name__ == "__main__":
    unittest.main()
