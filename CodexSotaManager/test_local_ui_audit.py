"""Isolated checks for the local UI repair; never import an installed application."""
from __future__ import annotations

import ast
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import queue
import socketserver
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import ModuleType, SimpleNamespace
from typing import Any, Callable
import unittest
from unittest import mock
import urllib.request


HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "CodexHistorySync"
sys.path.insert(0, str(CORE))
import claude_desktop as claude_library
SOURCE = ast.parse((HERE / "CodexSotaManager.py").read_text(encoding="utf-8-sig"))
SOURCE_CLASS = next(node for node in SOURCE.body if isinstance(node, ast.ClassDef)
                    and node.name == "CodexSotaApp")
METHODS = {
    "_ensure_router_running", "_set_editor_protected", "_configure_styles", "_build_models_tab",
    "_refresh_claude_status", "_cancel_claude_status_refresh", "_set_claude_status_controls",
    "_start_claude_status_refresh", "_finish_claude_status_refresh", "_render_claude_status",
    "_reconcile_claude_slot", "_claude_slot_note", "_drain_task_events",
    "_launch_claude", "_release_claude_slot", "_publish_claude_profile", "_switch_claude_entry",
    "_set_busy",
}
CODEX = SimpleNamespace(name="codex")
CLAUDE = SimpleNamespace(name="claude", registry_path=Path("fixture-only/providers.json"))
SPACE = dict(Any=Any, Callable=Callable, deepcopy=deepcopy, json=json, os=os, queue=queue,
             threading=threading, time=time, tk=tk, ttk=ttk, urllib=urllib, CODEX=CODEX, CLAUDE=CLAUDE)
for assignment in SOURCE.body:
    if isinstance(assignment, ast.Assign) and len(assignment.targets) == 1:
        target = assignment.targets[0]
        if isinstance(target, ast.Name):
            try:
                SPACE[target.id] = ast.literal_eval(assignment.value)
            except (ValueError, TypeError):
                if target.id == "LOCAL_HEALTH_OPENER":
                    exec(compile(ast.Module(body=[assignment], type_ignores=[]), "<local-opener>", "exec"), SPACE)
fixture_class = ast.ClassDef(name="CodexSotaApp", bases=[], keywords=[], decorator_list=[],
                             body=[node for node in SOURCE_CLASS.body
                                   if isinstance(node, ast.FunctionDef) and node.name in METHODS])
exec(compile(ast.fix_missing_locations(ast.Module(body=[fixture_class], type_ignores=[])),
             "<candidate-manager-fixture>", "exec"), SPACE)
Fixture = SPACE["CodexSotaApp"]


class Widget:
    def __init__(self, owner: int):
        self.owner = owner
        self.value = ""
        self.options = {}

    def check(self):
        if threading.get_ident() != self.owner:
            raise AssertionError("background worker touched a Tk fixture")

    def configure(self, **kwargs):
        self.check()
        self.options.update(kwargs)

    def set(self, value):
        self.check()
        self.value = value

    def state(self, value):
        self.check()
        self.options["state"] = value

    def start(self, interval):
        self.check()

    def stop(self):
        self.check()


def app_fixture():
    app = Fixture()
    app._closing = app._busy = False
    app._claude_status_epoch = 0
    app._claude_status_running = app._claude_status_pending = app._claude_status_cancel = None
    app._task_events = queue.Queue()
    app.workspace = CODEX
    app.registry = {"providers": []}
    app._claude_entries = []
    app.logs = []
    owner = threading.get_ident()
    app.claude_status_var = Widget(owner)
    app.claude_entry_combo = Widget(owner)
    for name in ("claude_publish_button", "claude_release_button", "claude_switch_button", "launch_button"):
        setattr(app, name, Widget(owner))
    def append_log(message):
        app.claude_status_var.check()
        app.logs.append(message)
    app._append_log = append_log
    return app


class LocalHealthTests(unittest.TestCase):
    def test_proxy_environment_cannot_intercept_a_healthy_local_router(self):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(2048)
                body = b'{"status":"ok"}'
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                                     + b"\r\nConnection: close\r\n\r\n" + body)
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                workspace = SimpleNamespace(health_url=f"http://127.0.0.1:{server.server_address[1]}/healthz")
                restart = mock.Mock()
                with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
                                                 "NO_PROXY": "", "no_proxy": ""}), \
                        mock.patch.dict(SPACE, restart_router=restart,
                                        router_health_matches_workspace=lambda health, ws: health["status"] == "ok"):
                    self.assertFalse(Fixture._ensure_router_running(SimpleNamespace(workspace=workspace)))
                restart.assert_not_called()
            finally:
                server.shutdown()
                worker.join(2)

    def test_both_health_paths_use_the_proxy_free_opener(self):
        for name in ("_ensure_router_running", "_run_audit"):
            node = next(item for item in SOURCE_CLASS.body if isinstance(item, ast.FunctionDef) and item.name == name)
            self.assertIn("LOCAL_HEALTH_OPENER.open", ast.unparse(node))
            self.assertNotIn("urllib.request.urlopen", ast.unparse(node))

    def test_bridge_control_is_locked_with_other_identity_fields(self):
        names = ("id_entry base_url_entry prefix_entry api_key_entry models_path_entry responses_path_entry "
                 "messages_path_entry auth_header_entry auth_prefix_entry proto_responses_check chat_bridge_check "
                 "chat_path_entry proto_messages_check show_key_button name_entry timeout_spin add_model_button "
                 "select_models_button clear_models_button remove_models_button enabled_check failover_check "
                 "headers_text model_tree fetch_button connection_button test_models_button measure_fast_button "
                 "speed_button rank_button reasoning_combo save_button delete_button").split()
        app = app_fixture()
        for name in names:
            setattr(app, name, Widget(threading.get_ident()))
        for busy, protected, state in ((False, False, "normal"), (True, False, "disabled"),
                                       (False, True, "disabled")):
            app._busy = busy
            app._set_editor_protected(protected)
            self.assertEqual(app.chat_bridge_check.options["state"], state)


class ClaudeStatusTests(unittest.TestCase):
    def setUp(self):
        self.app = app_fixture()
        self.order = []
        self.status = {"available": True, "entries": [{"name": "fixture", "applied": True}], "applied_name": "fixture"}
        self.patch = mock.patch.dict(SPACE, {
            "migrate_legacy_entry": lambda: self.order.append("migrate"),
            "claude_window_present": lambda: False,
            "claude_pids": lambda: set(),
            "reconcile_claude_slot": lambda **kw: self.order.append("reconcile") or {"status": "no-claim"},
            "claude_library_status": lambda: self.order.append("status") or deepcopy(self.status),
            "load_registry": lambda *a, **kw: {"providers": []},
            "build_inference_models": lambda registry: registry.get("models", []),
            "claude_slot_state": lambda: {"available": True, "claimed": False},
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def receive(self):
        kind, epoch, payload = self.app._task_events.get(timeout=3)
        self.assertEqual(kind, "claude_status")
        self.app._finish_claude_status_refresh(epoch, payload)
        return payload

    def test_status_refresh_is_read_only_and_runs_off_the_ui_thread(self):
        entered, release = threading.Event(), threading.Event()
        def status():
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            self.assertFalse(threading.current_thread().daemon)
            self.order.append("status")
            entered.set()
            self.assertTrue(release.wait(3))
            return deepcopy(self.status)
        with mock.patch.dict(SPACE,
                             migrate_legacy_entry=mock.Mock(side_effect=AssertionError("status migrated")),
                             reconcile_claude_slot=mock.Mock(side_effect=AssertionError("status reconciled")),
                             claude_library_status=status):
            started = time.monotonic()
            self.app._refresh_claude_status()
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(self.app.claude_publish_button.options["state"], "disabled")
            finally:
                release.set()
            self.receive()
        self.assertEqual(self.order, ["status"])
        self.assertIn("fixture", self.app.claude_status_var.value)
        self.assertEqual(self.app.claude_publish_button.options["state"], "normal")

    def test_repeated_refreshes_coalesce_and_discard_the_previous_workspace_result(self):
        entered, release = threading.Event(), threading.Event()
        def status():
            self.order.append("status")
            if len(self.order) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return deepcopy(self.status)
        with mock.patch.dict(SPACE,
                             migrate_legacy_entry=mock.Mock(side_effect=AssertionError("status migrated")),
                             reconcile_claude_slot=mock.Mock(side_effect=AssertionError("status reconciled")),
                             claude_library_status=status):
            self.app._refresh_claude_status()
            self.assertTrue(entered.wait(2))
            try:
                self.app._cancel_claude_status_refresh()
                self.app.workspace = CLAUDE
                self.app.registry = {"models": [1, 2]}
                self.app._refresh_claude_status()
                self.app._refresh_claude_status()
                self.assertEqual(self.order, ["status"])
            finally:
                release.set()
            self.receive()  # cancelled generation starts exactly one latest refresh
            self.assertEqual(self.app.claude_status_var.value, "")
            self.receive()
        self.assertEqual(self.order, ["status", "status"])
        self.assertIn("2 个", self.app.claude_status_var.value)

    def test_close_cancels_remaining_maintenance_and_ignores_late_callbacks(self):
        entered, release = threading.Event(), threading.Event()
        def status():
            entered.set()
            self.assertTrue(release.wait(3))
        with mock.patch.dict(SPACE, claude_library_status=status):
            self.app._refresh_claude_status()
            self.assertTrue(entered.wait(2))
            self.app._closing = True
            self.app._cancel_claude_status_refresh()
            release.set()
            self.receive()
        self.assertEqual(self.order, [])
        self.assertEqual(self.app.claude_status_var.value, "")
        self.assertEqual(self.app.logs, [])

    def test_busy_operations_defer_refresh_until_their_completion(self):
        self.app._busy = True
        self.app._refresh_claude_status()
        self.assertIsNone(self.app._claude_status_running)
        self.assertEqual(self.order, [])
        busy_method = next(node for node in SOURCE_CLASS.body
                           if isinstance(node, ast.FunctionDef) and node.name == "_set_busy")
        for node in ast.walk(busy_method):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "self" and not node.attr.startswith("_")
                    and not hasattr(self.app, node.attr)):
                setattr(self.app, node.attr, Widget(threading.get_ident()))
        self.app._protected = False
        self.app._set_editor_protected = mock.Mock()
        self.app._set_busy(False, "fixture task finished")
        self.receive()
        self.assertFalse(self.app._busy)
        self.assertEqual(self.order, ["status"])

    def test_library_error_is_visible_and_releases_the_controls(self):
        with mock.patch.dict(SPACE, claude_library_status=mock.Mock(side_effect=TimeoutError("fixture library busy"))):
            self.app._refresh_claude_status()
            self.receive()
        self.assertIn("fixture library busy", self.app.claude_status_var.value)
        self.assertTrue(self.app.logs)
        self.assertEqual(self.app.claude_entry_combo.options["values"], [])
        self.assertEqual(self.app.claude_publish_button.options["state"], "normal")

    def test_duplicate_completion_cannot_replace_the_current_generation(self):
        self.app._claude_status_epoch = 3
        self.app._claude_status_running = 3
        self.app._finish_claude_status_refresh(2, {"error": "stale"})
        self.assertEqual(self.app._claude_status_running, 3)
        self.assertEqual(self.app.claude_status_var.value, "")

    def test_background_refresh_does_not_reenter_claude_configuration_actions(self):
        self.app._claude_status_running = 1
        with mock.patch.dict(SPACE, build_inference_models=mock.Mock(side_effect=AssertionError("action reentered"))):
            self.app._launch_claude()
            self.app._release_claude_slot()
            self.app._publish_claude_profile()
            self.app._switch_claude_entry()

    def test_worker_start_failure_is_visible_and_does_not_leave_controls_locked(self):
        with mock.patch.object(threading, "Thread", side_effect=RuntimeError("fixture cannot start worker")):
            self.app._refresh_claude_status()
        self.assertIn("fixture cannot start worker", self.app.claude_status_var.value)
        self.assertIsNone(self.app._claude_status_running)
        self.assertEqual(self.app.launch_button.options["state"], "normal")

    def test_unavailable_library_does_not_load_another_registry(self):
        self.status = {"available": False, "reason": "fixture library absent"}
        with mock.patch.dict(SPACE, load_registry=mock.Mock(side_effect=AssertionError("unexpected registry read"))):
            self.app._refresh_claude_status()
            self.receive()
        self.assertEqual(self.app.claude_status_var.value, "fixture library absent")

    def test_readonly_library_status_does_not_migrate_or_claim_shared_slot(self):
        with tempfile.TemporaryDirectory(prefix="sota-readonly-claude-library-") as temporary:
            root = Path(temporary)
            library = root / "configLibrary"
            library.mkdir()
            other_id = "d00c1234-5678-4abc-9def-0123456789ab"
            legacy_id = claude_library.LEGACY_ENTRY_IDS[0]
            index = library / "_meta.json"
            index.write_text(json.dumps({
                "appliedId": other_id,
                "entries": [
                    {"id": other_id, "name": "Other manager"},
                    {"id": legacy_id, "name": "Legacy SOTA"},
                ],
            }), encoding="utf-8")
            (library / f"{other_id}.json").write_text("{}", encoding="utf-8")
            before = {path.name: path.read_bytes() for path in library.iterdir()}
            with mock.patch.object(claude_library, "CONFIG_LIBRARY", library), \
                 mock.patch.object(claude_library, "META_PATH", index):
                status = claude_library.library_status_readonly()
            after = {path.name: path.read_bytes() for path in library.iterdir()}
            self.assertEqual(status["applied_id"], other_id)
            self.assertIn(legacy_id, {entry["id"] for entry in status["entries"]})
            self.assertEqual(before, after)
            self.assertFalse((root / "claude-library.lock").exists())

    def test_readonly_status_of_absent_library_does_not_create_it(self):
        with tempfile.TemporaryDirectory(prefix="sota-absent-claude-library-") as temporary:
            library = Path(temporary) / "missing-library"
            with mock.patch.object(claude_library, "CONFIG_LIBRARY", library):
                status = claude_library.library_status_readonly()
            self.assertFalse(status["available"])
            self.assertFalse(library.exists())

    def test_result_event_does_not_complete_an_unrelated_foreground_task(self):
        self.app._refresh_claude_status()
        kind, epoch, payload = self.app._task_events.get(timeout=3)
        self.app._task_events.put((kind, epoch, payload))
        self.app._busy = True
        self.app.after = mock.Mock()
        self.app._drain_task_events()
        self.assertTrue(self.app._busy)
        self.assertEqual(self.app.launch_button.options["state"], "disabled")
        self.app.after.assert_called_once()

    def test_switching_to_codex_does_not_leave_its_launch_control_disabled(self):
        self.app.workspace = CLAUDE
        self.app._claude_status_running = 1
        self.app._set_claude_status_controls()
        self.assertEqual(self.app.launch_button.options["state"], "disabled")
        self.app.workspace = CODEX
        self.app._set_claude_status_controls()
        self.assertEqual(self.app.launch_button.options["state"], "normal")


class ModelLayoutTests(unittest.TestCase):
    def test_existing_actions_remain_fully_reachable_at_narrow_widths(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.addCleanup(root.destroy)
        root.withdraw()
        root.geometry("1040x700+10000+10000")
        Fixture._configure_styles(root)
        root.columnconfigure(0, minsize=285)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(root, width=285)
        sidebar.grid(row=0, column=0, sticky="nsew")
        content = ttk.Frame(root, padding=(18, 14, 18, 0))
        content.grid(row=0, column=1, sticky="nsew")
        root.models_tab = ttk.Frame(content, padding=18)
        root.models_tab.pack(fill="both", expand=True)
        root.model_count_var = tk.StringVar(root, value="0")
        root.model_filter_var = tk.StringVar(root, value="")
        for name in ("_add_custom_model", "_set_all_models", "_edit_model_mapping", "_test_selected_models",
                     "_measure_fast_selected_models", "_toggle_speed_selected", "_rank_model_latency",
                     "_remove_selected_models", "_toggle_model_event", "_show_route_chain"):
            setattr(root, name, lambda *a, **kw: None)
        Fixture._build_models_tab(root)
        root.deiconify()
        names = ("map_model_button", "test_models_button", "measure_fast_button", "speed_button",
                 "rank_button", "remove_models_button")
        for width in (1040, 1220, 900, 1040):
            root.geometry(f"{width}x700+10000+10000")
            root.update()
            for name in names:
                widget = getattr(root, name)
                self.assertTrue(widget.winfo_ismapped(), (width, name))
                self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), (width, name))
        self.assertTrue(root.model_tree.cget("xscrollcommand"))


class FakeLease:
    def __init__(self, *args, **kwargs):
        self.releases = 0
    def refresh(self):
        pass
    def release(self):
        self.releases += 1


class FakeFileLock:
    def __init__(self, *args, **kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


# The controller is loaded from this candidate file, with coordination replaced
# by in-memory fixtures. Nothing can discover or launch an installed application.
stub = ModuleType("codex_app_lifecycle")
stub.FileLock = FakeFileLock
stub.LaunchRequestLease = FakeLease
stub.LifecycleBusy = type("LifecycleBusy", (RuntimeError,), {})
spec = importlib.util.spec_from_file_location("_local_ui_controller", HERE / "codex_launch_controller.py")
controller = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, codex_app_lifecycle=stub):
    spec.loader.exec_module(controller)


class PendingWindowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = [100.0]
        self.calls = []
        self.report = dict(status="deferred", phase="waiting_for_window", request_id="abcd",
                           app_process_id=42, app_creation_ticks="123", window_owned=False)
        self.desktop = dict(pid=42, role="desktop", creation_ticks="123", window_handle=0)
        self.inspector = mock.Mock(side_effect=lambda pid: deepcopy(self.desktop))

    def job(self, replies=None):
        replies = iter(replies or [self.report])
        def popen(command, **kwargs):
            self.calls.append(command)
            kwargs["stdout"].write((json.dumps(next(replies)) + "\n").encode())
            return SimpleNamespace(pid=900, poll=lambda: 0)
        attempt = controller.LaunchAttempt(["fixture-launcher"], self.root, self.root,
                                          mock.Mock(side_effect=AssertionError("must inspect only known PID")),
                                          inspect_process=self.inspector, request_id="abcd",
                                          popen=popen, clock=lambda: self.clock[0])
        self.addCleanup(attempt.detach)
        return attempt

    def test_known_desktop_is_polled_without_another_shell_then_becomes_ready(self):
        attempt = self.job()
        self.assertEqual(attempt.poll()["status"], "deferred")
        for _ in range(30):
            self.clock[0] += 10
            self.assertEqual(attempt.poll()["status"], "deferred")
        self.desktop["window_handle"] = 100
        result = attempt.poll()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["window_owned"])
        self.assertTrue(result["window_shown"])
        self.assertEqual(len(self.calls), 1)
        self.inspector.assert_called_with(42)
        self.assertFalse(attempt.can_cancel_queued)
        receipt = json.loads(attempt.status_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(receipt["request_id"], "abcd")
        self.assertEqual(controller.parse_result(attempt.log_path.read_text(encoding="utf-8"), "abcd")["status"], "ready")

    def test_receipt_write_failure_does_not_discard_a_verified_window(self):
        attempt = self.job()
        attempt.poll()
        self.desktop["window_handle"] = 1
        with mock.patch.object(Path, "replace", side_effect=OSError("fixture receipt locked")):
            result = attempt.poll()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["window_shown"])
        self.assertIn("fixture receipt locked", result["warnings"][-1])
        self.assertEqual(list(attempt.status_path.parent.glob("*.observed-*")), [])

    def test_recycled_pid_or_backend_never_completes_an_owned_window(self):
        for change in ({"creation_ticks": "999", "window_handle": 100},
                       {"role": "app_server", "window_handle": 100}):
            with self.subTest(change=change):
                self.calls.clear()
                self.desktop.update(change)
                attempt = self.job([self.report, dict(request_id="abcd", status="error", error="fixture identity changed")])
                attempt.poll()
                self.clock[0] += 3
                self.assertEqual(attempt.poll()["status"], "error")
                self.assertEqual(len(self.calls), 2)

    def test_inspection_failure_is_bounded_without_relaunching(self):
        attempt = self.job()
        attempt.poll()
        self.inspector.side_effect = OSError("fixture inspection denied")
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.clock[0] += 16
        result = attempt.poll()
        self.assertEqual(result["status"], "error")
        self.assertIn("fixture inspection denied", result["verification_error"])
        self.assertEqual(len(self.calls), 1)

    def test_missing_identity_uses_the_original_launcher_checks(self):
        del self.report["app_creation_ticks"]
        attempt = self.job([self.report, dict(request_id="abcd", status="error", error="fixture no identity")])
        attempt.poll()
        self.clock[0] += 3
        self.assertEqual(attempt.poll()["status"], "error")
        self.inspector.assert_not_called()

    def test_detach_and_cancel_do_not_poll_or_restart_a_live_desktop(self):
        for action, status in (("detach", "detached"), ("cancel_queued", "cancelled")):
            with self.subTest(action=action):
                attempt = self.job()
                attempt.poll()
                getattr(attempt, action)()
                self.assertEqual(attempt.poll()["status"], status)
        self.inspector.assert_not_called()

    def test_waiting_for_history_keeps_the_original_retry_backoff(self):
        history = dict(self.report, phase="waiting_for_history_sync")
        attempt = self.job([history, history])
        attempt.poll()
        self.clock[0] += 1
        attempt.poll()
        self.assertEqual(len(self.calls), 1)
        self.clock[0] += 2
        attempt.poll()
        self.assertEqual(len(self.calls), 2)
        self.inspector.assert_not_called()

    def test_transient_inspection_failure_recovers_without_another_launcher(self):
        attempt = self.job()
        attempt.poll()
        self.inspector.side_effect = OSError("fixture transient failure")
        self.assertEqual(attempt.poll()["status"], "deferred")
        self.inspector.side_effect = None
        self.inspector.return_value = dict(self.desktop, window_handle=1)
        self.assertEqual(attempt.poll()["status"], "ready")
        self.assertEqual(len(self.calls), 1)

    def test_live_launcher_is_not_completed_or_relaunched_by_the_direct_inspector(self):
        attempt = self.job()
        attempt.process = SimpleNamespace(pid=900, poll=lambda: None)
        attempt.last_result = self.report
        self.clock[0] += 1000
        self.assertEqual(attempt.poll()["status"], "starting")
        self.inspector.assert_not_called()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
