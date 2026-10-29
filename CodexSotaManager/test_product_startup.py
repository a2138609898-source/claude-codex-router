"""Standalone installs and explicit portable apps never require companion products."""
from contextlib import nullcontext, redirect_stdout
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parent.parent / "CodexHistorySync"
sys.path.insert(0, str(CORE))
import sync_after_codex_exit as watcher
import sync_codex_histories as history
import sync_codex_histories_three_way as three_way
import validate_codex_profile as validator


class ProductStartupTests(unittest.TestCase):
    def environment(self, root):
        env = dict(os.environ)
        for name, suffix in (("CODEX_SOTA_COCKPIT_ROOT", "cockpit"),
                             ("CODEX_SOTA_PLUS_ROOT", "plus"),
                             ("CODEX_SOTA_CODEX_ROOT", "sota")):
            env[name] = str(root / suffix)
        env["CODEX_SOTA_PYTHON"] = sys.executable
        return env

    def test_default_standalone_sync_and_audit_are_read_only_and_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for arguments in (("--json",), ("--json", "--audit-only")):
                result = subprocess.run(
                    [sys.executable, "-B", str(CORE / "sync_codex_histories_three_way.py"), *arguments],
                    env=self.environment(root), capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                status = json.loads(result.stdout)
                self.assertEqual(status["status"], "skipped")
                self.assertEqual(status["reason"], "optional_profiles_absent")
                self.assertFalse(status["sync_performed"])
                self.assertEqual(list(root.iterdir()), [])

    def test_corrupt_config_emits_structured_error_before_any_history_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text("model_provider = [", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["sync", "--json", "--sota-root", str(root)]), \
                 mock.patch.object(history, "write_last_result") as write_result, \
                 mock.patch.object(three_way, "run_three_way_sync") as run_sync, \
                 mock.patch.object(history.logging, "error"), redirect_stdout(output):
                code = three_way.main()
            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["recovery_status"], "not_needed")
            write_result.assert_not_called()
            run_sync.assert_not_called()
            self.assertEqual((root / "config.toml").read_text(encoding="utf-8"), "model_provider = [")

    def test_profile_validator_reports_the_selected_provider_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            for provider_id in ("codex_sota", "existing_provider"):
                before = f"model_provider = '{provider_id}' # user comment\n".encode("utf-8")
                config.write_bytes(before)
                output = io.StringIO()
                with mock.patch.object(sys, "argv", ["validator", "--profile", "Sota", "--root", str(root)]), \
                     mock.patch.object(validator, "validate_profile", return_value=(False, "catalog_missing")), \
                     redirect_stdout(output):
                    self.assertEqual(validator.main(), 1)
                self.assertEqual(json.loads(output.getvalue())["model_provider"], provider_id)
                self.assertEqual(config.read_bytes(), before)

    def test_complete_default_stores_keep_the_existing_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = [Path(temporary) / name for name in ("cockpit", "plus", "sota")]
            for root in roots:
                root.mkdir()
                (root / "state_5.sqlite").touch()
            output = io.StringIO()
            with mock.patch.object(three_way, "DEFAULT_COCKPIT_ROOT", roots[0]), \
                 mock.patch.object(three_way, "DEFAULT_PLUS_ROOT", roots[1]), \
                 mock.patch.object(three_way, "DEFAULT_SOTA_ROOT", roots[2]), \
                 mock.patch.object(sys, "argv", ["sync", "--audit-only"]), \
                 mock.patch.object(three_way, "audit_roots", return_value={"status": "ok"}) as audit, \
                 redirect_stdout(output):
                self.assertEqual(three_way.main(), 0)
            audit.assert_called_once()
            self.assertEqual(json.loads(output.getvalue())["status"], "ok")

    def test_explicit_missing_roots_remain_an_audit_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run(
                [sys.executable, "-B", str(CORE / "sync_codex_histories_three_way.py"), "--json", "--audit-only",
                 "--cockpit-root", str(root / "custom-cockpit"), "--plus-root", str(root / "custom-plus"),
                 "--sota-root", str(root / "custom-sota")],
                env=self.environment(root), capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            self.assertEqual(list(root.iterdir()), [])

    def test_post_exit_watcher_accepts_standalone_skip(self):
        payload = {"status": "skipped", "reason": "optional_profiles_absent", "sync_performed": False}
        with mock.patch.object(watcher, "chatgpt_processes_running", return_value=False), \
             mock.patch.object(watcher, "launch_pending", return_value=False), \
             mock.patch.object(watcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
            self.assertEqual(watcher.run_sync_with_retry(), payload)

    def test_post_exit_watcher_finishes_after_standalone_skip_without_retry(self):
        payload = {"status": "skipped", "reason": "optional_profiles_absent", "sync_performed": False}
        with mock.patch.object(sys, "argv", ["watcher", "--pid", "42"]), \
             mock.patch.object(watcher, "setup_logging"), \
             mock.patch.object(watcher, "FileLock", return_value=nullcontext()), \
             mock.patch.object(watcher, "wait_for_process", return_value=True), \
             mock.patch.object(watcher, "wait_until_app_is_fully_closed"), \
             mock.patch.object(watcher, "run_archived_sidebar_repair", return_value={"status": "ok"}), \
             mock.patch.object(watcher, "run_pending_repair", return_value={"status": "not-needed"}), \
             mock.patch.object(watcher, "run_sync_with_retry", return_value=payload) as sync, \
             mock.patch.object(watcher.time, "sleep", side_effect=AssertionError("Skip must finish")):
            self.assertEqual(watcher.main(), 0)
            sync.assert_called_once()

    def powershell(self, script, env):
        executable = shutil.which("powershell.exe")
        if not executable:
            self.skipTest("Windows PowerShell unavailable")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                              env=env, capture_output=True, encoding="utf-8-sig", errors="replace", timeout=20)

    def test_portable_mode_requires_exact_explicit_override_and_never_bypasses_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, PRODUCT_TEST_CORE=str(CORE), PRODUCT_TEST_ROOT=temporary)
            result = self.powershell(r'''
$ErrorActionPreference = 'Stop'
function Invoke-WithComRetry { param([scriptblock]$Action) & $Action }
function Get-AppxPackage { return @([pscustomobject]@{ InstallLocation = (Join-Path $env:PRODUCT_TEST_ROOT 'store') }) }
foreach ($name in @('Switch-CodexSota.ps1', 'Switch-CodexProfile.ps1')) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:PRODUCT_TEST_CORE $name), [ref]$tokens, [ref]$errors)
    $function = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-ExplicitPortableCodexApp'}, $true)
    . ([scriptblock]::Create($function.Extent.Text))
    $portable = Join-Path $env:PRODUCT_TEST_ROOT 'portable\Codex.exe'
    $env:CODEX_APP_EXE = $portable
    if (-not (Test-ExplicitPortableCodexApp $portable)) { throw ('Portable app rejected: ' + $name) }
    if (Test-ExplicitPortableCodexApp (Join-Path $env:PRODUCT_TEST_ROOT 'other\Codex.exe')) { throw 'A different override was accepted' }
    $store = Join-Path $env:PRODUCT_TEST_ROOT 'store\app\Codex.exe'
    $env:CODEX_APP_EXE = $store
    if (Test-ExplicitPortableCodexApp $store) { throw 'Registered Store path bypassed package launch' }
    $store = Join-Path $env:PRODUCT_TEST_ROOT 'WindowsApps\old-package\app\Codex.exe'
    $env:CODEX_APP_EXE = $store
    if (Test-ExplicitPortableCodexApp $store) { throw 'Unregistered Store path bypassed package launch' }
    $env:CODEX_APP_EXE = ''
    if (Test-ExplicitPortableCodexApp $portable) { throw 'Implicit portable fallback accepted' }
}
Write-Output 'Both explicit portable selectors passed without launching a process'
''', env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Both explicit portable selectors passed", result.stdout)

    def test_runner_standalone_skip_never_requires_a_codex_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = self.environment(Path(temporary))
            env["PRODUCT_TEST_CORE"] = str(CORE)
            env["CODEX_APP_EXE"] = "invalid-relative-Codex.exe"
            result = self.powershell("& (Join-Path $env:PRODUCT_TEST_CORE 'Run-CodexHistorySync.ps1') -NoGui -AuditOnly", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "skipped")
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_both_launchers_accept_only_the_expected_standalone_skip_receipt(self):
        result = self.powershell(r'''
$ErrorActionPreference = 'Stop'
foreach ($name in @('Switch-CodexSota.ps1', 'Switch-CodexProfile.ps1')) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:PRODUCT_TEST_CORE $name), [ref]$tokens, [ref]$errors)
    $function = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-StructuredSyncSuccess'}, $true)
    . ([scriptblock]::Create($function.Extent.Text))
    $receipt = [pscustomobject]@{status='skipped'; reason='optional_profiles_absent'; sync_performed=$false; mode='three-way-audit'; missing_optional_profiles=@('fixture')}
    if (-not (Test-StructuredSyncSuccess -Result $receipt -AuditOnly)) { throw ('Standalone audit rejected: '+$name) }
    if (Test-StructuredSyncSuccess -Result $receipt) { throw 'Audit receipt accepted as actual sync' }
    $receipt.mode = 'three-way'
    if (-not (Test-StructuredSyncSuccess -Result $receipt)) { throw ('Standalone sync rejected: '+$name) }
    $receipt.reason = 'unrelated_skip'
    if (Test-StructuredSyncSuccess -Result $receipt) { throw 'Unrelated skip accepted' }
}
Write-Output 'Both launchers accepted only expected standalone receipts'
''', dict(os.environ, PRODUCT_TEST_CORE=str(CORE)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Both launchers accepted only expected standalone receipts", result.stdout)

    def test_interactive_runner_detects_standalone_skip_before_closing_any_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = self.environment(Path(temporary))
            env["PRODUCT_TEST_CORE"] = str(CORE)
            result = self.powershell(r'''
$ErrorActionPreference = 'Stop'
$python = $env:CODEX_SOTA_PYTHON
$scriptPath = Join-Path $env:PRODUCT_TEST_CORE 'sync_codex_histories_three_way.py'
$profileRoots = @{Cockpit=$env:CODEX_SOTA_COCKPIT_ROOT; Plus=$env:CODEX_SOTA_PLUS_ROOT; Sota=$env:CODEX_SOTA_CODEX_ROOT}
$restartAppWhenDone = $false
$NoGui = $false
function Get-CodexAppExecutable { throw 'App resolver must not run for standalone skip' }
function Stop-CodexApp { throw 'A standalone sync must never close an app' }
function Show-SyncMessage { param($Message,$Title,$Icon) Write-Output ('skip-message:'+$Title) }
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:PRODUCT_TEST_CORE 'Run-CodexHistorySync.ps1'), [ref]$tokens, [ref]$errors)
$main = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] -and $_.Extent.Text.Contains('if (-not $python)') })
if ($main.Count -ne 1) { throw 'Runner main try block not uniquely located' }
& ([scriptblock]::Create($main[0].Extent.Text))
''', env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skip-message:Cross-profile history sync inactive", result.stdout)
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
