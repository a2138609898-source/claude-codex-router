"""Router shell completion must not depend on EOF from its detached children."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

import sota_registry as registry


class StarterProcessTests(unittest.TestCase):
    def test_shell_exit_returns_structured_output_without_waiting_for_pipe_eof(self):
        retained = []

        class Shell:
            def __init__(self, command, **options):
                self.options = options
                self.waits = []
                options["stdout"].write(b'noise\n' + json.dumps(
                    {"status": "ready", "started": True}).encode() + b'\n')
                retained.append(self)

            def wait(self, *, timeout):
                self.waits.append(timeout)
                return 0

            def communicate(self, *args, **kwargs):
                raise AssertionError("A detached child's EOF must never be awaited")

        with mock.patch.object(registry.subprocess, "Popen", side_effect=Shell):
            result = registry._run_router_starter(["fixture-shell", "fixture.ps1"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["status"], "ready")
        self.assertNotEqual(retained[0].options["stdout"], subprocess.PIPE)
        self.assertEqual(retained[0].options["stdin"], subprocess.DEVNULL)
        self.assertEqual(retained[0].options["stderr"], subprocess.STDOUT)
        self.assertTrue(retained[0].options["close_fds"])
        self.assertEqual(retained[0].waits, [30.0])

    def test_timeout_cleanup_is_bounded_even_when_shell_does_not_reap(self):
        process = mock.Mock()
        process.wait.side_effect = [
            subprocess.TimeoutExpired("fixture", 30),
            subprocess.TimeoutExpired("fixture", 2),
        ]
        with mock.patch.object(registry.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "within its timeout"):
                registry._run_router_starter(["fixture-shell"])
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=30.0), mock.call(timeout=2.0)])
        process.kill.assert_called_once_with()
        process.communicate.assert_not_called()

    def test_shell_already_exited_during_timeout_still_reports_failure(self):
        process = mock.Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 30), 0]
        process.kill.side_effect = OSError("already exited")
        with mock.patch.object(registry.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "workspace router status"):
                registry._run_router_starter(["fixture-shell"])
        process.communicate.assert_not_called()

    def test_nonzero_exit_retains_error_output(self):
        process = mock.Mock()
        process.wait.return_value = 2

        def start(command, **options):
            options["stdout"].write('停止被拒绝'.encode("utf-8"))
            return process

        with mock.patch.object(registry.subprocess, "Popen", side_effect=start):
            result = registry._run_router_starter(["fixture-shell"])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "停止被拒绝")

    def test_verbose_shell_keeps_the_final_structured_result(self):
        process = mock.Mock()
        process.wait.return_value = 0

        def start(command, **options):
            options["stdout"].write(b"x" * 200000 + b'\n{"status":"ready"}\n')
            return process

        with mock.patch.object(registry.subprocess, "Popen", side_effect=start):
            result = registry._run_router_starter(["fixture-shell"])
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {"status": "ready"})
        self.assertLessEqual(len(result.stdout), 131072)


if __name__ == "__main__":
    unittest.main()
