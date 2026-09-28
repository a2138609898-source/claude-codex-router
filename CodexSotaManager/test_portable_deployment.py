"""Deployment regressions using temporary roots; never launch installed apps."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "CodexHistorySync"
sys.path.insert(0, str(CORE))
import codex_app_lifecycle as lifecycle
import repair_archived_sidebar as sidebar


class PortableDeploymentTests(unittest.TestCase):
    def test_bundle_uses_directories_and_an_explicit_runtime_allowlist(self):
        captured = {}

        def analysis(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

        namespace = {
            "SPECPATH": str(REPO / "CodexSotaManager"),
            "Analysis": analysis,
            "PYZ": lambda *a, **k: None,
            "EXE": lambda *a, **k: None,
            "COLLECT": lambda *a, **k: None,
        }
        spec = REPO / "CodexSotaManager" / "codex-sota.spec"
        exec(compile(spec.read_text(encoding="utf-8-sig"), str(spec), "exec"), namespace)
        data = captured["datas"]
        core_data = [(Path(path), destination) for path, destination in data if Path(path).parent == CORE]
        self.assertTrue(core_data)
        for path, destination in core_data:
            self.assertEqual(destination, "CodexHistorySync")
            self.assertTrue(path.is_file(), path.name)
        names = {path.name for path, _ in core_data}
        self.assertTrue({"sota_registry.py", "Switch-CodexSota.ps1", "messages.zh-CN.json"} <= names)
        self.assertEqual({name for name in names if name.endswith(".json")}, {"messages.zh-CN.json"})
        self.assertTrue(names.isdisjoint({"auth.json", "providers.json", "last-result.json", "config.toml"}))

    def test_router_root_is_absolute_once_for_defaults_and_overrides(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell unavailable")
        source = (CORE / "Start-CodexSotaRouter.ps1").read_text(encoding="utf-8-sig")
        # Execute only path resolution, before router/process/config access.
        prefix = source[:source.index("$routerPort =")]
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "user home"
            custom = Path(temporary) / "portable workspace"
            for workspace, variable, default in (
                ("codex", "CODEX_SOTA_CODEX_ROOT", ".codex-sota"),
                ("claude", "CODEX_SOTA_CLAUDE_ROOT", ".claude-sota"),
            ):
                for override in (None, custom):
                    with self.subTest(workspace=workspace, override=override):
                        env = dict(os.environ, USERPROFILE=str(home))
                        env.pop(variable, None)
                        if override is not None:
                            env[variable] = str(override)
                        script = "& {\n" + prefix + "\nWrite-Output $sotaRoot\n} -Workspace " + workspace
                        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                        result = subprocess.run(
                            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, encoding="utf-8-sig", errors="replace", env=env, timeout=15,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(Path(result.stdout.strip()), override or home / default)
            self.assertFalse(custom.exists())
            self.assertFalse((home / ".codex-sota").exists())
            self.assertFalse((home / ".claude-sota").exists())

    def test_sidebar_repair_respects_all_profile_overrides_without_creating_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {
                "CODEX_SOTA_COCKPIT_ROOT": str(Path(temporary) / "cockpit"),
                "CODEX_SOTA_PLUS_ROOT": str(Path(temporary) / "plus"),
                "CODEX_SOTA_CODEX_ROOT": str(Path(temporary) / "sota"),
            }
            with mock.patch.dict(os.environ, env):
                self.assertEqual(
                    [root for _, root in sidebar.configured_profiles()],
                    [Path(value).resolve() for value in env.values()],
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_history_defaults_follow_the_same_roots_in_a_new_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = [str(Path(temporary) / name) for name in ("cockpit", "plus", "sota")]
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            env.update(zip(("CODEX_SOTA_COCKPIT_ROOT", "CODEX_SOTA_PLUS_ROOT", "CODEX_SOTA_CODEX_ROOT"), roots))
            result = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import json; import sync_codex_histories_three_way as s; print(json.dumps([str(s.DEFAULT_COCKPIT_ROOT),str(s.DEFAULT_PLUS_ROOT),str(s.DEFAULT_SOTA_ROOT)]))"],
                cwd=CORE, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), roots)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_custom_app_path_remains_visible_to_offline_sync_guard(self):
        with mock.patch.dict(os.environ, {"CODEX_APP_EXE": "D:/Portable Codex/app/ChatGPT.exe"}):
            self.assertEqual(lifecycle._role_for_path("d:/portable codex/app/CHATGPT.exe"), "desktop")
            self.assertEqual(lifecycle._role_for_path("D:/Portable Codex/app/resources/bin/codex.exe"), "app_server")
            self.assertIsNone(lifecycle._role_for_path("D:/Unrelated/ChatGPT.exe"))
            self.assertIsNone(lifecycle._role_for_path("D:/Portable Codex/app/resources-other/codex.exe"))

    def test_app_overrides_fail_closed_before_any_launcher_actions(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("Codex.exe", "Other.exe"):
                (Path(temporary) / name).touch()
            env = dict(os.environ, CODEX_PORTABLE_TEST_CORE=str(CORE), CODEX_PORTABLE_TEST_FIXTURE=temporary)
            script = r'''
$ErrorActionPreference = 'Stop'
$valid = Join-Path $env:CODEX_PORTABLE_TEST_FIXTURE 'Codex.exe'
foreach ($name in @('Switch-CodexSota.ps1', 'Switch-CodexProfile.ps1', 'Run-CodexHistorySync.ps1')) {
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:CODEX_PORTABLE_TEST_CORE $name), [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors) { throw 'Launcher parsing failed' }
    $function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-CodexAppExecutable' }, $true)
    . ([scriptblock]::Create($function.Extent.Text))
    foreach ($invalid in @('relative/Codex.exe', (Join-Path $env:CODEX_PORTABLE_TEST_FIXTURE 'Other.exe'), (Join-Path $env:CODEX_PORTABLE_TEST_FIXTURE 'missing/Codex.exe'))) {
        $env:CODEX_APP_EXE = $invalid
        $script:resolvedAppExecutable = $null
        $rejected = $false
        try { $null = Get-CodexAppExecutable }
        catch {
            if ($_.Exception.Message -notlike 'CODEX_APP_EXE*') { throw }
            $rejected = $true
        }
        if (-not $rejected) { throw ('Unsafe app override accepted by ' + $name) }
    }
    $env:CODEX_APP_EXE = $valid
    $script:resolvedAppExecutable = $null
    if ((Get-CodexAppExecutable) -ne $valid) { throw ('Valid app override rejected by ' + $name) }
}
Write-Output 'validated 3 launcher resolvers only'
'''
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            result = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True, text=True, env=env, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("validated 3 launcher resolvers only", result.stdout)


if __name__ == "__main__":
    unittest.main()
