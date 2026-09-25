from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from codex_launch_controller import LaunchAttempt, owns_reported_window, parse_result


class FakeProcess:
    pid = 901

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class CodexLaunchControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = [100.0]
        self.request = "abcd1234"
        self.ready = {"status": "ready", "request_id": self.request,
                      "app_process_id": 42, "app_creation_ticks": "123456",
                      "window_owned": True, "phase": "ready"}
        self.processes = [{"pid": 42, "role": "desktop", "window_handle": 100,
                           "creation_ticks": "123456"}]
        self.calls = []

    def job(self, replies, processes=None, **kwargs):
        replies = iter(replies)

        def popen(command, **kwargs):
            process, reply = next(replies)
            self.calls.append((command, kwargs, process))
            if reply is not None:
                kwargs["stdout"].write((json.dumps(reply) + "\n").encode())
            return process

        attempt = LaunchAttempt(["powershell.exe", "-NonInteractive", "-File", "launcher.ps1"],
                                self.root, self.root,
                                lambda: self.processes if processes is None else processes,
                                request_id=self.request, clock=lambda: self.now[0], popen=popen,
                                **kwargs)
        self.addCleanup(lambda: attempt.sink.close() if attempt.sink else None)
        return attempt

    def test_request_scoped_parse_ignores_unrelated_json(self):
        other = {**self.ready, "request_id": "ffff"}
        result = parse_result(json.dumps(self.ready) + "\n" + json.dumps(other), self.request)
        self.assertEqual(result, self.ready)

    def test_pid_reuse_is_not_a_ready_window(self):
        self.processes[0]["creation_ticks"] = "654321"
        self.assertFalse(owns_reported_window(self.ready, self.processes))

    def test_backend_or_unrelated_window_is_not_ready(self):
        for change in ({"role": "app_server"}, {"pid": 43}, {"window_handle": 0}):
            with self.subTest(change=change):
                self.assertFalse(owns_reported_window(self.ready, [{**self.processes[0], **change}]))

    def test_ready_requires_birth_identity(self):
        del self.ready["app_creation_ticks"]
        self.assertFalse(owns_reported_window(self.ready, self.processes))

    def test_launcher_must_also_confirm_ownership(self):
        self.assertFalse(owns_reported_window({**self.ready, "window_owned": False}, self.processes))

    def test_success_verifies_the_reported_window(self):
        attempt = self.job([(FakeProcess(0), self.ready)])
        result = attempt.poll()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["window_shown"])
        self.assertEqual(len(self.calls), 1)

    def test_success_exit_without_window_is_an_actionable_error(self):
        attempt = self.job([(FakeProcess(0), self.ready)], processes=[],
                           verification_grace_seconds=0)
        self.assertEqual(attempt.poll()["status"], "error")

    def test_ready_verification_grace_rechecks_without_respawn(self):
        attempt = self.job([(FakeProcess(0), self.ready)])
        attempt.inspect_process = mock.Mock(side_effect=[None, self.processes[0]])
        self.assertEqual(attempt.poll()["phase"], "waiting_for_window")
        self.assertEqual(attempt.poll()["status"], "ready")
        self.assertEqual(len(self.calls), 1)

    def test_ready_verification_fails_only_after_grace_without_respawn(self):
        attempt = self.job([(FakeProcess(0), self.ready)], verification_grace_seconds=15)
        attempt.inspect_process = mock.Mock(return_value=None)
        self.assertEqual(attempt.poll()["status"], "starting")
        self.now[0] += 15.1
        self.assertEqual(attempt.poll()["status"], "error")
        self.assertEqual(len(self.calls), 1)

    def test_real_launch_failure_is_not_hidden_by_an_old_window(self):
        error = {"request_id": self.request, "status": "error", "error": "invalid configuration"}
        attempt = self.job([(FakeProcess(1), error)])
        self.assertEqual(attempt.poll()["error"], "invalid configuration")

    def test_nonzero_exit_cannot_be_reported_as_ready(self):
        attempt = self.job([(FakeProcess(1), self.ready)])
        self.assertEqual(attempt.poll()["status"], "error")

    def test_running_shell_is_never_killed_or_relaunched_on_elapsed_time(self):
        process = FakeProcess()
        attempt = self.job([(process, self.ready)])
        self.assertEqual(attempt.poll()["status"], "starting")
        self.now[0] += 1500
        self.assertEqual(attempt.poll()["status"], "starting")
        self.assertEqual(len(self.calls), 1)
        process.code = 0
        self.assertEqual(attempt.poll()["status"], "ready")

    def test_deferred_sync_reuses_request_and_never_overlaps(self):
        deferred = {"status": "deferred", "request_id": self.request,
                    "phase": "waiting_for_history_sync"}
        attempt = self.job([(FakeProcess(75), deferred), (FakeProcess(0), self.ready)])
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.assertEqual(len(self.calls), 1)
        self.now[0] += 2.1
        self.assertEqual(attempt.poll()["status"], "ready")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0], self.calls[1][0])

    def test_deferred_history_sync_retries_with_bounded_backoff(self):
        deferred = {"status": "deferred", "request_id": self.request,
                    "phase": "waiting_for_history_sync"}
        attempt = self.job([
            (FakeProcess(0), deferred),
            (FakeProcess(0), deferred),
            (FakeProcess(0), self.ready),
        ])
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.now[0] += 1.9
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.assertEqual(len(self.calls), 1)
        self.now[0] += 0.2
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.assertEqual(len(self.calls), 2)
        # The second deferred result doubles the delay to four seconds.
        self.now[0] += 3.9
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.assertEqual(len(self.calls), 2)
        self.now[0] += 0.2
        self.assertEqual(attempt.poll()["status"], "ready")
        self.assertEqual(len(self.calls), 3)

    def test_deferred_backoff_never_exceeds_its_cap(self):
        deferred = {"status": "deferred", "request_id": self.request,
                    "phase": "waiting_for_history_sync"}
        attempt = self.job([(FakeProcess(0), deferred) for _ in range(10)])
        delays = []
        for _ in range(10):
            self.now[0] = max(self.now[0], attempt.next_retry)
            self.assertEqual(attempt.poll()["status"], "deferred")
            delays.append(attempt.next_retry - self.now[0])
        self.assertEqual(delays[:5], [2.0, 4.0, 8.0, 16.0, 30.0])
        self.assertTrue(all(delay <= 30.0 for delay in delays))

    def test_pending_sync_can_be_cancelled_without_stopping_any_process(self):
        deferred = {"status": "deferred", "request_id": self.request}
        attempt = self.job([(FakeProcess(0), deferred)])
        attempt.poll()
        self.assertTrue(attempt.cancel_queued())
        self.now[0] += 20
        self.assertEqual(attempt.poll()["status"], "cancelled")
        self.assertEqual(len(self.calls), 1)

    def test_cannot_cancel_running_launcher_as_if_it_were_only_queued(self):
        process = FakeProcess()
        attempt = self.job([(process, None)])
        attempt.poll()
        self.assertFalse(attempt.cancel_queued())

    def test_output_is_file_backed_with_no_stdin_prompt_or_pipe(self):
        attempt = self.job([(FakeProcess(0), self.ready)])
        attempt.poll()
        command, kwargs, _ = self.calls[0]
        self.assertIn("-NoGui", command)
        self.assertIn("-LaunchRequestId", command)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertTrue(kwargs["close_fds"])

    def test_stale_status_of_a_previous_retry_cannot_forge_success(self):
        attempt = self.job([(FakeProcess(0), None)])
        attempt.status_path.parent.mkdir(parents=True)
        attempt.status_path.write_text(json.dumps(self.ready), encoding="utf-8")
        self.assertEqual(attempt.poll()["status"], "error")

    def test_result_is_terminal_and_does_not_spawn_on_later_polls(self):
        attempt = self.job([(FakeProcess(0), self.ready)])
        for _ in range(3):
            self.assertEqual(attempt.poll()["status"], "ready")
        self.assertEqual(len(self.calls), 1)

    def test_only_reported_pid_is_inspected_when_targeted_inspector_is_available(self):
        attempt = self.job([(FakeProcess(0), self.ready)])
        attempt.inspect_process = mock.Mock(return_value=self.processes[0])
        attempt.inspect_processes = mock.Mock(side_effect=OSError("unrelated elevated ChatGPT"))
        self.assertEqual(attempt.poll()["status"], "ready")
        attempt.inspect_process.assert_called_once_with(42)
        attempt.inspect_processes.assert_not_called()

    def test_verification_error_keeps_completed_attempt_and_retries_without_respawn(self):
        attempt = self.job([(FakeProcess(0), self.ready)])
        attempt.inspect_process = mock.Mock(side_effect=[OSError("temporarily unavailable"), self.processes[0]])
        self.assertEqual(attempt.poll()["status"], "starting")
        self.assertIsNotNone(attempt.completed_result)
        self.assertEqual(attempt.poll()["status"], "ready")
        self.assertEqual(len(self.calls), 1)

    def test_window_transition_keeps_same_process_without_respawn(self):
        attempt = self.job([(FakeProcess(0), self.ready)])
        attempt.inspect_process = mock.Mock(side_effect=[{**self.processes[0], "window_handle": 0}, self.processes[0]])
        self.assertEqual(attempt.poll()["phase"], "waiting_for_window")
        self.assertEqual(attempt.poll()["status"], "ready")
        self.assertEqual(len(self.calls), 1)

    def test_detaching_running_attempt_does_not_kill_or_spawn(self):
        process = mock.Mock(pid=901)
        process.poll.return_value = None
        attempt = self.job([(process, None)])
        attempt.poll()
        attempt.detach()
        self.now[0] += 1500
        self.assertEqual(attempt.poll()["status"], "detached")
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertEqual(len(self.calls), 1)

    def test_request_id_is_not_a_path(self):
        with self.assertRaises(ValueError):
            LaunchAttempt([], self.root, self.root, lambda: [], request_id="../escape")


class ManagerLaunchIntegrationTests(unittest.TestCase):
    def test_manager_uses_direct_nongui_launcher_not_interactive_cli_wrapper(self):
        import CodexSotaManager as manager
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Switch-CodexSota.ps1").touch()
            scheduled = []
            app = SimpleNamespace(_busy=False, workspace=manager.CODEX,
                                  after=lambda delay, fn: scheduled.append((delay, fn)),
                                  _append_log=lambda _: None)
            app._set_busy = lambda busy, label: setattr(app, "_busy", busy)
            with mock.patch.object(manager, "CORE_ROOT", root), \
                    mock.patch.object(manager, "sota_auth_problem", return_value=None), \
                    mock.patch.object(manager, "resolve_codex_sota_command") as wrapper:
                manager.CodexSotaApp._launch_codex(app)
            wrapper.assert_not_called()
            self.assertIn(str(root / "Switch-CodexSota.ps1"), app._codex_launch_job.invocation)
            self.assertIn("-NoGui", app._codex_launch_job.invocation)
            self.assertIn("-NonInteractive", app._codex_launch_job.invocation)
            self.assertIn("-SkipSync", app._codex_launch_job.invocation)
            self.assertEqual(len(scheduled), 1)
            # A double-click must not allocate a second request or subprocess.
            job = app._codex_launch_job
            manager.CodexSotaApp._launch_codex(app)
            self.assertIs(app._codex_launch_job, job)

    def test_stale_timer_does_not_poll_a_replacement_request(self):
        import CodexSotaManager as manager
        old = mock.Mock()
        app = SimpleNamespace(_closing=False, _codex_launch_job=object())
        manager.CodexSotaApp._poll_codex_launch(app, old)
        old.poll.assert_not_called()

    def test_deferred_result_stays_busy_without_error_dialog(self):
        import CodexSotaManager as manager
        job = mock.Mock()
        job.poll.return_value = {"status": "deferred", "phase": "waiting_for_history_sync", "elapsed": 1500}
        app = SimpleNamespace(_closing=False, _codex_launch_job=job, _busy=True,
                              _launch_phase="", _append_log=mock.Mock(),
                              status_var=mock.Mock(), after=mock.Mock(), _task_failed=mock.Mock())
        manager.CodexSotaApp._poll_codex_launch(app, job)
        self.assertIs(app._codex_launch_job, job)
        self.assertTrue(app._busy)
        app._task_failed.assert_not_called()
        app.after.assert_called_once()


if __name__ == "__main__":
    unittest.main()
