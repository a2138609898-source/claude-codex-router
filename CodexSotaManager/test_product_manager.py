"""First-run, frozen-runtime and responsive-editor regressions using temporary data."""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest import mock

import CodexSotaManager as manager
import sota_registry as registry


class ProductManagerTests(unittest.TestCase):
    @contextmanager
    def empty_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new profile"
            workspace = replace(manager.CODEX, root=root)
            with mock.patch.object(manager, "CODEX", workspace), \
                 mock.patch.object(manager.CodexSotaApp, "_refresh_claude_status"), \
                 mock.patch.object(manager.CodexSotaApp, "_run_audit"), \
                 mock.patch.object(manager.messagebox, "showerror") as show_error:
                app = manager.CodexSotaApp()
                errors = []
                app.report_callback_exception = lambda *error: errors.append(error)
                try:
                    app._initial_load()
                    app.update()
                    yield app, root, show_error, errors
                finally:
                    app.destroy()

    def test_new_account_opens_empty_editor_without_creating_profile(self):
        with self.empty_app() as (app, root, show_error, errors):
            self.assertEqual(app.registry["providers"], [])
            self.assertIsNone(app.current_id)
            self.assertEqual(app.name_var.get(), "")
            self.assertEqual(str(app.save_button.cget("state")), "normal")
            self.assertFalse(root.exists())
            self.assertFalse(errors)
            show_error.assert_not_called()

    def test_corrupt_existing_registry_remains_visible_and_unchanged(self):
        with self.empty_app() as (app, root, _show_error, _errors):
            root.mkdir()
            source = root / "providers.json"
            source.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                app._load_registry()
            self.assertEqual(source.read_text(encoding="utf-8"), "{broken")

    def test_first_explicit_save_creates_working_local_profile_and_clears_key_editor(self):
        with self.empty_app() as (app, root, show_error, errors):
            app.name_var.set("Example Gateway")
            app.id_var.set("example")
            app.base_url_var.set("https://example.invalid")
            app.prefix_var.set("example--")
            app.api_key_var.set("fixture-only-upstream-key")
            app.draft_models = [{"id": "sample-model", "enabled": True}]

            def complete_task(_label, worker, success, _secrets):
                success(worker())

            with mock.patch.dict(registry.WORKSPACES, {"codex": app.workspace}), \
                 mock.patch.object(app, "_run_task", side_effect=complete_task), \
                 mock.patch.object(app, "_ensure_router_running", return_value=False), \
                 mock.patch.object(manager.messagebox, "showinfo"), \
                 mock.patch.object(registry, "restart_router", side_effect=AssertionError("No real processes")):
                app._save_current()
            self.assertEqual(app.current_id, "example")
            self.assertEqual(app.api_key_var.get(), "")
            self.assertTrue((root / "config.toml").is_file())
            self.assertTrue((root / "auth.json").is_file())
            self.assertTrue(app.workspace.catalog_path.is_file())
            self.assertNotIn("fixture-only-upstream-key", app.workspace.registry_path.read_text(encoding="utf-8"))
            self.assertNotEqual(json.loads(app.workspace.auth_path.read_text(encoding="utf-8"))["OPENAI_API_KEY"], "fixture-only-upstream-key")
            self.assertFalse(errors)
            show_error.assert_not_called()

    def test_bridge_control_cannot_change_during_save_or_for_protected_identity(self):
        with self.empty_app() as (app, _root, _show_error, errors):
            app._set_busy(True, "Saving")
            self.assertEqual(str(app.chat_bridge_check.cget("state")), "disabled")
            app.chat_bridge_check.invoke()
            self.assertFalse(app.chat_bridge_var.get())
            app._set_busy(False, "Ready")
            app.chat_bridge_check.invoke()
            self.assertTrue(app.chat_bridge_var.get())
            app._set_editor_protected(True)
            app.chat_bridge_check.invoke()
            self.assertTrue(app.chat_bridge_var.get())
            self.assertFalse(errors)

    def test_model_actions_remain_reachable_at_minimum_window_width(self):
        with self.empty_app() as (app, _root, _show_error, errors):
            app.notebook.select(app.models_tab)
            for width in (1220, manager.MIN_WINDOW_WIDTH, 1220):
                app.geometry(f"{width}x{manager.MIN_WINDOW_HEIGHT}")
                app.update()
                for button in (app.map_model_button, app.test_models_button,
                               app.measure_fast_button, app.speed_button,
                               app.rank_button, app.remove_models_button):
                    self.assertTrue(button.winfo_ismapped())
                    self.assertLessEqual(
                        button.winfo_x() + button.winfo_width(),
                        button.master.winfo_width(),
                    )
                self.assertTrue(app.model_tree.cget("xscrollcommand"))
                self.assertTrue(app.provider_tree.cget("yscrollcommand"))
            self.assertFalse(errors)

    def test_frozen_manager_uses_its_bundled_runtime_without_system_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "codex-sota-runtime.exe"
            runtime.touch()
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "executable", str(root / "codex-sota.exe")), \
                 mock.patch.dict(os.environ, {"CODEX_SOTA_PYTHON": "", "CODEX_PYTHON": ""}):
                self.assertEqual(manager.resolve_helper_python(), runtime)

    def test_local_health_bypasses_system_proxy_and_does_not_restart_healthy_router(self):
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            workspace = SimpleNamespace(health_url=f"http://127.0.0.1:{server.server_port}/healthz")
            try:
                with mock.patch.dict(os.environ, {
                    "HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": "",
                    "http_proxy": "http://127.0.0.1:1", "no_proxy": "",
                }), mock.patch.object(manager, "router_health_matches_workspace", return_value=True), \
                     mock.patch.object(manager, "restart_router") as restart:
                    started = manager.CodexSotaApp._ensure_router_running(SimpleNamespace(), workspace)
                    self.assertFalse(started)
                    restart.assert_not_called()
            finally:
                server.shutdown()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
