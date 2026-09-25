"""No production writes: temporary locks and mocked process/watcher behavior only."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import codex_app_lifecycle as lifecycle
import sync_after_codex_exit as watcher


class LifecycleTests(unittest.TestCase):
    def test_quoted_backend_subcommand_is_recognized(self):
        self.assertTrue(lifecycle._is_app_server_command_line('"C:\\Program Files\\Codex\\codex.exe" "app-server" --listen stdio://'))
        self.assertTrue(lifecycle._is_app_server_command_line('codex.exe app-server --listen stdio://'))
        self.assertFalse(lifecycle._is_app_server_command_line('codex.exe exec "describe app-server-settings"'))

    @unittest.skipUnless(os.name == "nt", "Windows process identity")
    def test_single_pid_verification_does_not_scan_other_processes(self):
        identity = {"pid": 123, "exe": r"D:\WindowsApps\OpenAI.Codex_26_x64__id\app\ChatGPT.exe", "creation_ticks": "456"}
        with patch.object(lifecycle, "process_identity", return_value=identity.copy()) as inspect, patch.object(lifecycle, "_attach_visible_windows", side_effect=lambda rows: rows), patch.object(lifecycle, "detect_codex_app_processes", side_effect=PermissionError("unrelated elevated process")) as scan:
            result = lifecycle.inspect_codex_app_process(123)
        self.assertEqual(result["pid"], 123)
        self.assertEqual(result["creation_ticks"], "456")
        inspect.assert_called_once_with(123)
        scan.assert_not_called()

    def test_unrelated_chatgpt_is_not_codex(self):
        self.assertIsNone(lifecycle._role_for_path(r"C:\Users\x\AppData\Local\ChatGPT\ChatGPT.exe"))
        self.assertIsNone(lifecycle._role_for_path(r"C:\tools\codex.exe"))
        self.assertEqual(lifecycle._role_for_path(r"D:\WindowsApps\OpenAI.Codex_26_x64__id\app\ChatGPT.exe"), "desktop")
        self.assertEqual(lifecycle._role_for_path(r"C:\Users\x\AppData\Local\OpenAI\CodexCliBundled\codex.exe"), "backend_candidate")

    def test_uninspectable_candidate_keeps_offline_work_busy(self):
        unknown = lifecycle._unknown_process(7, "ChatGPT.exe", "access_denied")
        self.assertEqual(unknown["role"], "unknown")
        with patch.object(lifecycle, "detect_codex_app_processes", return_value=[unknown]):
            self.assertTrue(lifecycle.app_running())

    def test_recycled_non_app_pid_does_not_wait(self):
        with patch.object(watcher, "process_identity", return_value={"exe": r"C:\tools\node_repl.exe", "creation_ticks": "123"}), patch.object(watcher.ctypes, "WinDLL") as dll:
            watcher.wait_for_process(41)
        dll.assert_not_called()

    def test_recycled_creation_time_does_not_wait(self):
        exe = r"D:\WindowsApps\OpenAI.Codex_26_x64__id\app\ChatGPT.exe"
        with patch.object(watcher, "process_identity", return_value={"exe": exe, "creation_ticks": "123"}), patch.object(watcher.ctypes, "WinDLL") as dll:
            watcher.wait_for_process(41, expected_creation_ticks="122")
        dll.assert_not_called()

    def test_active_app_does_not_spawn_sync(self):
        with patch.object(watcher, "chatgpt_processes_running", return_value=True), patch.object(watcher.subprocess, "run") as run:
            result = watcher.run_sync_with_retry()
        self.assertEqual(result["status"], "deferred")
        run.assert_not_called()

    def test_pending_launch_does_not_spawn_sync(self):
        with patch.object(watcher, "chatgpt_processes_running", return_value=False), patch.object(watcher, "launch_pending", return_value=True), patch.object(watcher.subprocess, "run") as run:
            result = watcher.run_sync_with_retry()
        self.assertEqual(result["status"], "deferred")
        run.assert_not_called()

    def test_sync_busy_is_deferred_without_retry_storm(self):
        completed = subprocess.CompletedProcess([], 0, '{"status":"deferred","reason":"sync_busy"}', '')
        with patch.object(watcher, "chatgpt_processes_running", return_value=False), patch.object(watcher, "launch_pending", return_value=False), patch.object(watcher.subprocess, "run", return_value=completed) as run:
            result = watcher.run_sync_with_retry()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("--wait-for-existing", run.call_args.args[0])
        self.assertIn("--defer-if-app-running", run.call_args.args[0])

    def test_legacy_busy_is_deferred(self):
        completed = subprocess.CompletedProcess([], 1, '', 'History sync is already running, wait for it to finish.')
        with patch.object(watcher, "chatgpt_processes_running", return_value=False), patch.object(watcher, "launch_pending", return_value=False), patch.object(watcher.subprocess, "run", return_value=completed):
            self.assertEqual(watcher.run_sync_with_retry()["status"], "deferred")

    def test_quiet_exit_resets_on_relaunch(self):
        # First quiet period is interrupted. Only a full, new quiet period may sync.
        states = [False, False, True, False, False, False]
        with patch.object(watcher, "chatgpt_processes_running", side_effect=states), patch.object(watcher, "launch_pending", return_value=False), patch.object(watcher.time, "monotonic", side_effect=[0, 1, 2, 3, 4]), patch.object(watcher.time, "sleep") as sleep:
            watcher.wait_until_app_is_fully_closed(2)
        self.assertEqual(sleep.call_count, 5)

    def test_launch_request_expiry(self):
        with tempfile.TemporaryDirectory(prefix="codex-launch-request-test-") as temporary:
            request = Path(temporary) / "launch.json"
            with patch.object(lifecycle, "LAUNCH_REQUEST_PATH", request), patch.object(lifecycle.time, "time", return_value=100):
                request.write_text(json.dumps({"expires_at": 101}), encoding="utf-8")
                self.assertTrue(lifecycle.launch_pending())
                request.write_text(json.dumps({"expires_at": 99}), encoding="utf-8")
                self.assertFalse(lifecycle.launch_pending())

    def test_live_launcher_request_requires_matching_birth_identity(self):
        with tempfile.TemporaryDirectory(prefix="codex-launch-request-live-") as temporary:
            request = Path(temporary) / "launch.json"
            payload = {"expires_at": 101, "launcher_pid": 41,
                       "launcher_creation_ticks": "123"}
            with patch.object(lifecycle, "LAUNCH_REQUEST_PATH", request), \
                    patch.object(lifecycle.time, "time", return_value=100), \
                    patch.object(lifecycle, "process_identity", return_value={
                        "pid": 41, "creation_ticks": "123"}):
                request.write_text(json.dumps(payload), encoding="utf-8")
                self.assertTrue(lifecycle.launch_pending())

            with patch.object(lifecycle, "LAUNCH_REQUEST_PATH", request), \
                    patch.object(lifecycle.time, "time", return_value=100), \
                    patch.object(lifecycle, "process_identity", return_value={
                        "pid": 41, "creation_ticks": "999"}):
                self.assertFalse(lifecycle.launch_pending())
                self.assertFalse(request.exists())

    def test_dead_launcher_request_is_retired_without_waiting_for_expiry(self):
        with tempfile.TemporaryDirectory(prefix="codex-launch-request-dead-") as temporary:
            request = Path(temporary) / "launch.json"
            request.write_text(json.dumps({"expires_at": 101, "launcher_pid": 41}), encoding="utf-8")
            with patch.object(lifecycle, "LAUNCH_REQUEST_PATH", request), \
                    patch.object(lifecycle.time, "time", return_value=100), \
                    patch.object(lifecycle, "process_identity", return_value=None):
                self.assertFalse(lifecycle.launch_pending())
            self.assertFalse(request.exists())

    def test_no_queued_repair_does_not_scan_or_start_a_process(self):
        with tempfile.TemporaryDirectory(prefix="codex-repair-queue-test-") as temporary:
            with patch.object(watcher, "REPAIR_PLAN", Path(temporary) / "missing.json"), patch.object(watcher.subprocess, "run") as run:
                self.assertEqual(watcher.run_pending_repair()["status"], "not-needed")
            run.assert_not_called()

    def test_queued_repair_can_defer_without_failure(self):
        completed = subprocess.CompletedProcess([], 75, '{"status":"deferred","reason":"app_running"}', '')
        with patch.object(Path, "is_file", return_value=True), patch.object(watcher, "chatgpt_processes_running", return_value=False), patch.object(watcher, "launch_pending", return_value=False), patch.object(watcher.subprocess, "run", return_value=completed) as run:
            self.assertEqual(watcher.run_pending_repair()["status"], "deferred")
        self.assertIn("--execute-plan", run.call_args.args[0])
        self.assertIn("--defer-if-app-running", run.call_args.args[0])

    def test_queued_repair_drift_aborts_before_normal_sync(self):
        completed = subprocess.CompletedProcess([], 3, '{"status":"drifted"}', '')
        with patch.object(Path, "is_file", return_value=True), patch.object(watcher, "chatgpt_processes_running", return_value=False), patch.object(watcher, "launch_pending", return_value=False), patch.object(watcher.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "normal sync was not started"):
                watcher.run_pending_repair()

    @unittest.skipUnless(os.name == "nt", "Windows byte-range lock interoperability")
    def test_powershell_and_python_share_the_same_lock(self):
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = "$s=[IO.File]::Open($env:CODEX_TEST_LIFECYCLE_LOCK,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite); try { $s.Lock(0,1); 'free'; $s.Unlock(0,1) } catch [IO.IOException] { 'busy' } finally { $s.Dispose() }"
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        with tempfile.TemporaryDirectory(prefix="codex-lifecycle-lock-test-") as temporary:
            target = Path(temporary) / "lock"
            environment = dict(os.environ, CODEX_TEST_LIFECYCLE_LOCK=str(target))
            with lifecycle.FileLock(target):
                blocked = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], env=environment, capture_output=True, text=True, timeout=10, creationflags=watcher.CREATE_NO_WINDOW)
            free = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], env=environment, capture_output=True, text=True, timeout=10, creationflags=watcher.CREATE_NO_WINDOW)
        self.assertEqual(blocked.stdout.strip(), "busy", blocked.stderr)
        self.assertEqual(free.stdout.strip(), "free", free.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows process identity")
    def test_process_identity_is_read_only_and_has_creation_time(self):
        result = lifecycle.process_identity(os.getpid())
        self.assertEqual(result["pid"], os.getpid())
        self.assertGreater(result["creation_time"], 0)
        self.assertTrue(result["creation_ticks"].isdigit())


if __name__ == "__main__":
    unittest.main(verbosity=2)
