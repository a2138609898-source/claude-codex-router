"""Claude startup regressions; fixture-only through run_guarded_tests.py."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import queue
from types import SimpleNamespace
import unittest
from unittest import mock

import CodexSotaManager as manager
import claude_desktop as desktop


class ClaudeDispatchTests(unittest.TestCase):
    def test_aumid_dispatch_returns_without_waiting_for_shell_or_inherited_pipes(self):
        # A shell activation may leave its output handles in the launched app.
        # No application/child exit should be needed before window supervision.
        process = SimpleNamespace(pid=4242, poll=mock.Mock(return_value=None),
                                  wait=mock.Mock(side_effect=AssertionError("live shell wait")),
                                  communicate=mock.Mock(side_effect=AssertionError("inherited pipe read")))
        target = {"kind": "aumid", "value": manager.CLAUDE_SQUIRREL_AUMID}
        with mock.patch.object(manager.subprocess, "Popen", return_value=process) as spawn, \
             mock.patch.object(manager.subprocess, "run", side_effect=AssertionError("captured pipe wait")):
            result = manager.start_claude_process(target)
        self.assertEqual(result["pid"], 4242)
        self.assertIsNone(result["returncode"])
        self.assertEqual(spawn.call_args.args[0],
                         [manager.EXPLORER_EXE, f"shell:AppsFolder\\{manager.CLAUDE_SQUIRREL_AUMID}"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(spawn.call_args.kwargs[stream], manager.subprocess.DEVNULL)
        self.assertTrue(spawn.call_args.kwargs["close_fds"])
        process.wait.assert_not_called()
        process.communicate.assert_not_called()

    def test_aumid_createprocess_failure_is_reported_to_claim_cleanup(self):
        with mock.patch.object(manager.subprocess, "Popen", side_effect=OSError("fixture dispatcher denied")):
            with self.assertRaisesRegex(OSError, "dispatcher denied"):
                manager.start_claude_process({"kind": "aumid", "value": manager.CLAUDE_SQUIRREL_AUMID})


class ClaudeNativeSnapshotTests(unittest.TestCase):
    @staticmethod
    def kernel(entries, snapshot=123):
        pending = iter(entries)

        def step(_snapshot, pointer):
            row = next(pending, None)
            if row is None:
                return False
            pointer._obj.th32ProcessID, pointer._obj.szExeFile = row
            return True

        return SimpleNamespace(CreateToolhelp32Snapshot=mock.Mock(return_value=snapshot),
                               Process32FirstW=mock.Mock(side_effect=step),
                               Process32NextW=mock.Mock(side_effect=step), CloseHandle=mock.Mock())

    def test_native_snapshot_matches_case_and_releases_handle_without_tasklist(self):
        kernel = self.kernel([(9, "Claude.exe"), (10, "other.exe"), (11, "CLAUDE.EXE")])
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel), \
             mock.patch.object(ctypes, "get_last_error", return_value=18), \
             mock.patch.object(manager.subprocess, "run", side_effect=AssertionError("tasklist spawn")):
            self.assertEqual(desktop.image_process_pids("claude.exe"), {9, 11})
        kernel.CloseHandle.assert_called_once_with(123)

    def test_partial_snapshot_failure_cannot_masquerade_as_no_claude(self):
        for entries in ([], [(9, "other.exe")], [(9, "Claude.exe")]):
            kernel = self.kernel(entries)
            with self.subTest(entries=entries), \
                 mock.patch.object(ctypes, "WinDLL", return_value=kernel), \
                 mock.patch.object(ctypes, "get_last_error", return_value=5):
                with self.assertRaises(OSError):
                    desktop.image_process_pids("claude.exe")
            kernel.CloseHandle.assert_called_once_with(123)

    def test_invalid_snapshot_handle_is_not_a_successful_empty_result(self):
        kernel = self.kernel([], snapshot=ctypes.c_void_p(-1).value)
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel), \
             mock.patch.object(ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError):
                desktop.image_process_pids("claude.exe")
        kernel.CloseHandle.assert_not_called()

    def test_successful_empty_snapshot_is_absent(self):
        kernel = self.kernel([])
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel), \
             mock.patch.object(ctypes, "get_last_error", return_value=18):
            self.assertEqual(desktop.image_process_pids("claude.exe"), set())
        kernel.CloseHandle.assert_called_once_with(123)

    def test_native_snapshot_still_filters_desktop_install_identity(self):
        paths = {1: r"C:\Fixture\AnthropicClaude\app-1.2\Claude.exe",
                 2: r"C:\Fixture\AnthropicClaudeCode\claude.exe", 3: None}
        with mock.patch.dict(desktop.os.environ, {"LOCALAPPDATA": r"C:\Fixture"}), \
             mock.patch.object(desktop, "image_process_pids", return_value=set(paths)) as images:
            self.assertEqual(desktop.claude_desktop_pids(image_path=paths.get), {1})
        images.assert_called_once_with("claude.exe")

    def test_native_snapshot_failure_stays_fail_closed_in_manager_and_watcher(self):
        with mock.patch.object(manager, "image_process_pids", side_effect=OSError("fixture snapshot denied")):
            with self.assertRaisesRegex(RuntimeError, "snapshot denied"):
                manager.claude_pids()
        with mock.patch.object(desktop, "image_process_pids", side_effect=OSError("fixture snapshot denied")):
            with self.assertRaisesRegex(OSError, "snapshot denied"):
                desktop.claude_desktop_pids()


class ClaudeWindowIdentityTests(unittest.TestCase):
    @staticmethod
    def window_api(*, owners=None, owner_error=False, enum_failure=False, post=False):
        windows = owners if owners is not None else {0x1234567801: 42}

        def owner(hwnd, pointer):
            pointer._obj.value = windows[int(hwnd)]
            return not owner_error

        def enum(callback, _data):
            if enum_failure:
                return False
            for handle in windows:
                if not callback(handle, 0):
                    return False
            return True

        api = SimpleNamespace(EnumWindows=mock.Mock(side_effect=enum),
                              GetWindowThreadProcessId=mock.Mock(side_effect=owner),
                              IsWindowVisible=mock.Mock(return_value=True),
                              PostMessageW=mock.Mock(return_value=post))
        return api

    def test_large_hwnd_is_verified_without_32bit_truncation(self):
        api = self.window_api()
        with mock.patch.object(ctypes, "WinDLL", return_value=api):
            self.assertTrue(manager.pids_own_visible_window({42}))
        self.assertEqual(api.GetWindowThreadProcessId.call_args.args[0], 0x1234567801)
        self.assertEqual(api.GetWindowThreadProcessId.argtypes[0], wintypes.HWND)
        self.assertEqual(api.IsWindowVisible.argtypes[0], wintypes.HWND)

    def test_unknown_owner_and_failed_enumeration_raise_instead_of_waiting_300_seconds(self):
        for owner_error, enum_failure in ((True, False), (False, True)):
            api = self.window_api(owner_error=owner_error, enum_failure=enum_failure)
            with self.subTest(owner_error=owner_error, enum_failure=enum_failure), \
                 mock.patch.object(ctypes, "WinDLL", return_value=api), \
                 mock.patch.object(ctypes, "get_last_error", return_value=5):
                with self.assertRaises(OSError):
                    manager.pids_own_visible_window({42})

    def test_gone_window_does_not_poison_other_process_window_discovery(self):
        api = self.window_api(owners={0x1234567801: 42}, owner_error=True)
        with mock.patch.object(ctypes, "WinDLL", return_value=api), \
             mock.patch.object(ctypes, "get_last_error", return_value=1400):
            self.assertFalse(manager.pids_own_visible_window({42}))

    def test_close_error_prevents_launch_over_the_old_desktop(self):
        api = self.window_api(post=False)
        with mock.patch.object(ctypes, "WinDLL", return_value=api), \
             mock.patch.object(ctypes, "get_last_error", return_value=5), \
             mock.patch.object(manager, "claude_pids", return_value={42}):
            with self.assertRaisesRegex(OSError, "request Claude window close"):
                manager.close_running_claude()

    def test_close_only_posts_to_verified_desktop_owner_with_typed_handle(self):
        api = self.window_api(owners={0x1234567801: 42, 0x1234567802: 99}, post=True)
        with mock.patch.object(ctypes, "WinDLL", return_value=api):
            self.assertEqual(manager.close_visible_process_windows({42}), {42})
        api.PostMessageW.assert_called_once_with(0x1234567801, 0x0010, 0, 0)
        self.assertEqual(api.PostMessageW.argtypes[0], wintypes.HWND)


class ClaudeTaskProgressTests(unittest.TestCase):
    def app(self):
        app = SimpleNamespace(_busy=False, _closing=False, _task_events=queue.Queue(),
                              status_var=mock.Mock(), _append_log=mock.Mock(),
                              _task_failed=mock.Mock(), _show_error=mock.Mock(), after=mock.Mock())

        def set_busy(value, label):
            app._busy = value
            app.status_var.set(label)

        app._set_busy = mock.Mock(side_effect=set_busy)
        app._tick_task_status = lambda request: manager.CodexSotaApp._tick_task_status(app, request)
        app._drain_task_events = lambda: manager.CodexSotaApp._drain_task_events(app)
        app._task_succeeded = lambda cb, result: manager.CodexSotaApp._task_succeeded(app, cb, result)
        return app

    def test_worker_phases_touch_widgets_only_when_tk_drains_them(self):
        app = self.app()
        manager.CodexSotaApp._task_progress(app, "正在等待 Claude Desktop 窗口")
        app.status_var.set.assert_not_called()
        app._append_log.assert_not_called()
        app._busy = True
        app._drain_task_events()
        app.status_var.set.assert_called_once_with("正在等待 Claude Desktop 窗口")
        app._append_log.assert_called_once_with("正在等待 Claude Desktop 窗口")
        self.assertTrue(app._busy, "progress must not unlock an unfinished worker")

    def test_task_stops_busy_state_on_actual_result_after_phase_events(self):
        app = self.app()
        threads = []

        def thread(target, **_options):
            threads.append(target)
            return SimpleNamespace(start=mock.Mock())

        def worker():
            manager.CodexSotaApp._task_progress(app, "正在等待 Claude Desktop 窗口")
            return {"window": True}

        success = mock.Mock()
        with mock.patch.object(manager.threading, "Thread", side_effect=thread):
            manager.CodexSotaApp._run_task(app, "正在启动 Claude Desktop", worker, success)
        self.assertTrue(app._busy)
        threads[0]()
        self.assertTrue(app._busy, "worker completion is delivered by the Tk event loop")
        app._drain_task_events()
        success.assert_called_once_with({"window": True})
        self.assertFalse(app._busy)

    def test_stale_elapsed_timer_cannot_follow_a_different_task_or_clear_busy(self):
        app = self.app()
        app._busy = True
        app._task_request_id = "replacement"
        app._task_started = 10.0
        app._task_label = "正在发布 Claude 模型并借用生效档"
        manager.CodexSotaApp._tick_task_status(app, "previous")
        app.status_var.set.assert_not_called()
        app.after.assert_not_called()
        with mock.patch.object(manager.time, "monotonic", return_value=22.0):
            manager.CodexSotaApp._tick_task_status(app, "replacement")
        app.status_var.set.assert_called_once_with("正在发布 Claude 模型并借用生效档（已等待 12 秒）")
        self.assertTrue(app._busy)

    def test_completed_worker_timer_cannot_overwrite_next_codex_launch_status(self):
        widget = mock.Mock()
        widgets = ("add_button", "move_up_button", "move_down_button", "reload_button",
                   "audit_button", "refresh_button", "launch_button", "open_config_button",
                   "open_registry_button", "checkup_button", "export_log_button",
                   "clear_log_button", "shortcuts_button", "restart_router_button",
                   "export_config_button", "backup_config_button", "restore_config_button",
                   "lint_button", "map_model_button", "claude_publish_button",
                   "claude_release_button", "claude_switch_button", "claude_refresh_button")
        app = self.app()
        for name in widgets:
            setattr(app, name, widget)
        app.progress = mock.Mock()
        app.provider_tree = mock.Mock()
        app._set_editor_protected = mock.Mock()
        app._set_claude_status_controls = mock.Mock()
        app._start_claude_status_refresh = mock.Mock()
        app._protected = False
        app._task_request_id = "completed-claude"
        manager.CodexSotaApp._set_busy(app, False, "就绪")
        self.assertIsNone(app._task_request_id)
        manager.CodexSotaApp._set_busy(app, True, "正在检查本地启动配置")
        app.status_var.set.reset_mock()
        manager.CodexSotaApp._tick_task_status(app, "completed-claude")
        app.status_var.set.assert_not_called()
        app.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
