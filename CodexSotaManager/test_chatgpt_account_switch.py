"""Account-mode regressions: temporary profiles and simulated processes only."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "CodexHistorySync"
sys.path.insert(0, str(CORE))

import validate_codex_profile as validator


def fingerprints(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class AccountProfileValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plus = self.root / ".codex-plus"
        self.sota = self.root / ".codex-sota"
        self.plus.mkdir()
        self.sota.mkdir()
        self.account_config = (
            '# Preserve these comments and personal settings.\n'
            'forced_login_method = "chatgpt"\n'
            'cli_auth_credentials_store = "file"\n'
            'model = "account-model"\n'
            'personality = "none"\n'
        )
        (self.plus / "config.toml").write_text(self.account_config, encoding="utf-8")
        (self.plus / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"fixture":true}}')
        (self.sota / "config.toml").write_text('forced_login_method="api"\nmodel_provider="tango_relay"\n')
        (self.sota / "auth.json").write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"fixture-only"}')
        (self.sota / "providers.json").write_text('{"providers":[{"id":"keep-original"}]}')
        (self.sota / "secrets").mkdir()
        (self.sota / "secrets" / "fixture.dpapi").write_bytes(b"not-a-real-secret")
        self.before = fingerprints(self.root)

    def validate(self):
        return validator.validate_chatgpt_direct_profile(self.plus, self.sota)

    def test_existing_account_and_entire_sota_config_stay_byte_identical(self):
        self.assertEqual(self.validate(), (True, "ok"))
        self.assertEqual(fingerprints(self.root), self.before)

    def test_signed_out_account_is_valid_without_creating_credentials(self):
        (self.plus / "auth.json").unlink()
        before = fingerprints(self.root)
        self.assertEqual(self.validate(), (True, "ok"))
        self.assertEqual(fingerprints(self.root), before)

    def test_missing_config_is_not_created_or_copied_from_sota(self):
        (self.plus / "config.toml").unlink()
        before = fingerprints(self.root)
        self.assertEqual(self.validate(), (False, "config_unreadable_or_invalid"))
        self.assertEqual(fingerprints(self.root), before)

    def test_custom_or_sota_provider_is_rejected_without_repair(self):
        for provider in ("tango_relay", "custom"):
            with self.subTest(provider=provider):
                (self.plus / "config.toml").write_text(
                    self.account_config + f'model_provider = "{provider}"\n', encoding="utf-8")
                before = fingerprints(self.root)
                self.assertEqual(self.validate(), (False, "unexpected_model_provider"))
                self.assertEqual(fingerprints(self.root), before)

    def test_builtin_openai_name_cannot_hide_custom_credentials_or_endpoints(self):
        overrides = (
            'base_url = "https://relay.example.invalid"',
            'env_key = "OPENAI_API_KEY"',
            'experimental_bearer_token = "fixture-only"',
            'http_headers = { Authorization = "fixture-only" }',
            'env_http_headers = { Authorization = "OPENAI_API_KEY" }',
            'auth = { command = "fixture-command" }',
            'gateway_oauth = { issuer = "https://relay.example.invalid" }',
        )
        for override in overrides:
            with self.subTest(override=override.split("=")[0].strip()):
                (self.plus / "config.toml").write_text(
                    self.account_config + '\n[model_providers.openai]\n' + override + '\n',
                    encoding="utf-8")
                before = fingerprints(self.root)
                self.assertEqual(self.validate(), (False, "unexpected_openai_provider_override"))
                self.assertEqual(fingerprints(self.root), before)

    def test_chatgpt_endpoint_override_and_disabled_auth_are_rejected(self):
        cases = (
            ('chatgpt_base_url = "https://relay.example.invalid"\n', "unexpected_chatgpt_base_url"),
            ('\n[model_providers.openai]\nrequires_openai_auth = false\n', "openai_auth_disabled"),
        )
        for extra, reason in cases:
            with self.subTest(reason=reason):
                (self.plus / "config.toml").write_text(self.account_config + extra, encoding="utf-8")
                self.assertEqual(self.validate(), (False, reason))

    def test_resolved_roots_must_not_overlap_in_either_direction(self):
        for root, protected in ((self.sota, self.sota), (self.sota / "child", self.sota),
                                (self.root, self.sota)):
            with self.subTest(root=root):
                self.assertEqual(validator.validate_chatgpt_direct_profile(root, protected),
                                 (False, "profile_roots_overlap"))
        self.assertEqual(fingerprints(self.root), self.before)

    def test_account_junction_resolving_to_sota_is_rejected_before_config_read(self):
        resolve = Path.resolve
        def redirected(path, *args, **kwargs):
            return resolve(self.sota) if path == self.plus else resolve(path, *args, **kwargs)
        with mock.patch.object(Path, "resolve", redirected), \
                mock.patch.object(validator, "_read_config") as read:
            self.assertEqual(self.validate(), (False, "profile_roots_overlap"))
        read.assert_not_called()

    def test_cli_cannot_combine_account_validation_with_repair(self):
        result = subprocess.run(
            [sys.executable, "-B", str(CORE / "validate_codex_profile.py"),
             "--profile", "Plus", "--root", str(self.plus), "--chatgpt-direct", "--repair"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"],
                         "chatgpt_direct_requires_readonly_plus_profile")
        self.assertEqual(fingerprints(self.root), self.before)


class AccountButtonTests(unittest.TestCase):
    def setUp(self):
        import CodexSotaManager as manager
        self.manager = manager
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Path(self.temp.name)
        (self.core / "Switch-CodexProfile.ps1").touch()
        (self.core / "Switch-CodexSota.ps1").touch()
        self.scheduled = []
        self.app = SimpleNamespace(
            _busy=False, workspace=manager.CODEX, _append_log=mock.Mock(),
            after=lambda delay, fn: self.scheduled.append((delay, fn)),
            draft_models=[{"id": "unsaved-model-edit"}],
        )
        self.app._set_busy = lambda busy, label: setattr(self.app, "_busy", busy)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(manager, "CORE_ROOT", self.core).start()
        self.confirm = mock.patch.object(manager.messagebox, "askyesno", return_value=True).start()
        self.error = mock.patch.object(manager.messagebox, "showerror").start()
        self.sota_auth = mock.patch.object(manager, "sota_auth_problem",
                                         side_effect=AssertionError("Must not read SOTA keys")).start()
        self.router_health = mock.patch.object(manager, "read_router_vendor_health",
                                             side_effect=AssertionError("Must not consult SOTA router")).start()

    def test_account_button_uses_isolated_profile_without_sota_key_or_config_write(self):
        self.manager.CodexSotaApp._launch_chatgpt_codex(self.app)
        invocation = self.app._codex_launch_job.invocation
        for argument in (str(self.core / "Switch-CodexProfile.ps1"), "-Profile", "Plus",
                         "-ChatGptDirect", "-SkipSync", "-Restart", "-NoGui", "-NonInteractive"):
            self.assertIn(argument, invocation)
        self.assertNotIn("-SyncBeforeLaunch", invocation)
        self.assertNotIn("Switch-CodexSota.ps1", " ".join(invocation))
        self.assertIsNone(self.app._codex_launch_job.process)
        self.assertEqual(self.app.draft_models, [{"id": "unsaved-model-edit"}])
        self.assertEqual(len(self.scheduled), 1)
        self.sota_auth.assert_not_called()
        self.router_health.assert_not_called()

    def test_cancel_never_allocates_request_or_starts_process(self):
        self.confirm.return_value = False
        self.manager.CodexSotaApp._launch_chatgpt_codex(self.app)
        self.assertFalse(hasattr(self.app, "_codex_launch_job"))
        self.assertEqual(self.scheduled, [])
        self.assertFalse(self.app._busy)

    def test_missing_script_reports_error_without_requesting_close(self):
        (self.core / "Switch-CodexProfile.ps1").unlink()
        self.manager.CodexSotaApp._launch_chatgpt_codex(self.app)
        self.confirm.assert_not_called()
        self.error.assert_called_once()
        self.assertEqual(self.scheduled, [])

    def test_claude_workspace_and_busy_manager_cannot_switch_codex(self):
        self.app.workspace = self.manager.CLAUDE
        self.manager.CodexSotaApp._launch_chatgpt_codex(self.app)
        self.app.workspace = self.manager.CODEX
        self.app._busy = True
        self.manager.CodexSotaApp._launch_chatgpt_codex(self.app)
        self.confirm.assert_not_called()
        self.assertEqual(self.scheduled, [])

    def test_double_click_cannot_start_a_second_request(self):
        self.manager.CodexSotaApp._launch_chatgpt_codex(self.app)
        job = self.app._codex_launch_job
        self.manager.CodexSotaApp._launch_chatgpt_codex(self.app)
        self.assertIs(self.app._codex_launch_job, job)
        self.assertEqual(len(self.scheduled), 1)
        self.confirm.assert_called_once()

    def test_account_ready_never_uses_sota_vendor_health(self):
        result = {"profile": "Plus", "status": "ready", "window_shown": True, "elapsed": 2}
        job = mock.Mock()
        job.poll.return_value = result
        self.app._closing = False
        self.app._codex_launch_job = job
        self.app._launch_finished = mock.Mock()
        self.manager.CodexSotaApp._poll_codex_launch(self.app, job)
        self.app._launch_finished.assert_called_once_with(result)
        self.router_health.assert_not_called()
        self.assertFalse(self.app._busy)

    def test_open_login_window_is_not_misreported_as_authenticated(self):
        self.app.header_status_var = mock.Mock()
        self.app._report_degraded_vendors = mock.Mock()
        self.manager.CodexSotaApp._launch_finished(
            self.app, {"profile": "Plus", "window_shown": True, "elapsed": 2})
        self.app.header_status_var.set.assert_called_once_with("GPT 账号版已打开")
        self.app._report_degraded_vendors.assert_not_called()
        self.assertIn("完成登录", self.app._append_log.call_args.args[0])

    def test_account_launch_reports_sync_counts_without_claiming_logged_in(self):
        self.app.header_status_var = mock.Mock()
        self.manager.CodexSotaApp._launch_finished(self.app, {
            "profile":"Plus", "window_shown":True, "elapsed":3,
            "account_history_sync":{"status":"ok","new_threads":150,"updated_threads":4,"account_threads":1123},
        })
        messages = [call.args[0] for call in self.app._append_log.call_args_list]
        self.assertTrue(any("新增 150 条" in message and "更新 4 条" in message for message in messages))
        self.assertIn("完成登录", messages[-1])

    def test_switch_back_retains_existing_sota_launcher_and_key_validation(self):
        self.sota_auth.side_effect = None
        self.sota_auth.return_value = None
        self.manager.CodexSotaApp._launch_codex(self.app, restart=True)
        invocation = self.app._codex_launch_job.invocation
        self.assertIn(str(self.core / "Switch-CodexSota.ps1"), invocation)
        self.assertIn("-Restart", invocation)
        self.assertIn("-SkipSync", invocation)
        self.assertNotIn("-ChatGptDirect", invocation)
        self.sota_auth.assert_called_once()

    def test_header_and_state_management_include_the_account_button(self):
        source = ast.parse((HERE / "CodexSotaManager.py").read_text(encoding="utf-8-sig"))
        app = next(node for node in source.body if isinstance(node, ast.ClassDef)
                   and node.name == "CodexSotaApp")
        methods = {node.name: ast.unparse(node) for node in app.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("command=self._launch_chatgpt_codex", methods["_build_ui"])
        self.assertIn("chatgpt_codex_button", methods["_set_busy"])
        self.assertIn("chatgpt_codex_button", methods["_switch_workspace"])

    def test_account_buttons_fit_the_existing_minimum_window_width(self):
        import tkinter as tk
        source = ast.parse((HERE / "CodexSotaManager.py").read_text(encoding="utf-8-sig"))
        app = next(node for node in source.body if isinstance(node, ast.ClassDef)
                   and node.name == "CodexSotaApp")
        method = next(node for node in app.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_build_ui")
        stop = next(index for index, node in enumerate(method.body)
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "sidebar" for target in node.targets))
        method.body = method.body[:stop]
        space = dict(self.manager.__dict__)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                     "<account-header-only-fixture>", "exec"), space)
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        try:
            root.workspace_var = tk.StringVar(root, value=self.manager.CODEX.name)
            root.header_status_var = tk.StringVar(root, value="GPT 账号版已打开")
            for name in ("_launch_codex", "_launch_chatgpt_codex", "_switch_workspace", "_run_audit"):
                setattr(root, name, mock.Mock())
            self.manager.CodexSotaApp._configure_styles(root)
            space["_build_ui"](root)
            root.update_idletasks()
            self.assertLessEqual(root.winfo_children()[0].winfo_reqwidth(), self.manager.MIN_WINDOW_WIDTH)
        finally:
            root.destroy()


@unittest.skipUnless(os.name == "nt", "Windows launcher behavioral harness")
class AccountLauncherBehaviorTests(unittest.TestCase):
    def test_real_launcher_functions_and_flows_with_fake_processes(self):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(CORE / "check_chatgpt_account_switch.ps1")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=50,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("All account-switch fixtures passed.", result.stdout)


if __name__ == "__main__":
    unittest.main()
