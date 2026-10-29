"""Offline probe and installer-receipt regressions; executable launches are mocked."""
from __future__ import annotations

from copy import deepcopy
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import artifact_runtime_probe as hook
from test_hardening_deployment import (
    DeploymentFixture, DeploymentScriptTests, MANAGER_ROOT, create_bundle_fixture,
    fixture_runtime_ready, verifier,
)


class RuntimeProbeTests(DeploymentFixture):
    def test_hook_invalid_arguments_cannot_fall_through_to_manager(self) -> None:
        with patch.object(hook, "runtime_checks") as checks:
            for arguments in ([], [hook.PROBE_FLAG], [hook.PROBE_FLAG, str(self.root / "ready.json"), "bad"]):
                self.assertEqual(hook.main(arguments), 2)
            checks.assert_not_called()

    def test_hook_produces_ready_record_for_exact_invocation_without_reusing_report(self) -> None:
        executable = self.root / "fixture.exe"
        executable.write_bytes(b"MZnot-executable")
        report = self.root / "ready.json"
        with patch.object(sys, "executable", str(executable)), \
             patch.object(hook, "runtime_checks", return_value={name: "passed" for name in verifier.RUNTIME_PROBE_CHECKS}) as checks:
            arguments = [hook.PROBE_FLAG, str(report), "a" * 32]
            self.assertEqual(hook.main(arguments), 0)
            before = report.read_bytes()
            self.assertEqual(hook.main(arguments), 2)
            self.assertEqual(report.read_bytes(), before)
            self.assertEqual(checks.call_count, 1)
        result = json.loads(before)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["nonce"], "a" * 32)
        self.assertFalse(result["profiles_loaded"])
        self.assertFalse(result["network_used"])

    def test_hook_dependency_error_is_failed_not_ready(self) -> None:
        report = self.root / "failed.json"
        with patch.object(hook, "runtime_checks", side_effect=ImportError("broken DLL")):
            self.assertEqual(hook.main([hook.PROBE_FLAG, str(report), "b" * 32]), 1)
        self.assertEqual(json.loads(report.read_text())["status"], "failed")

    def _archive_modules(self, source: bytes, scripts=None):
        modules = {name: ModuleType(name) for name in ("PyInstaller", "PyInstaller.archive", "PyInstaller.archive.readers")}
        scripts = scripts or ["pyiboot01_bootstrap", "artifact_runtime_probe", "CodexSotaManager"]
        archive = SimpleNamespace(toc={name: (0, 0, 0, 0, "s") for name in scripts}, extract=lambda _name: source)
        modules["PyInstaller.archive.readers"].CArchiveReader = lambda _path: archive
        return modules

    def test_static_hook_check_accepts_only_reviewed_code_before_entrypoint(self) -> None:
        source = (MANAGER_ROOT / "artifact_runtime_probe.py").read_bytes()
        code = marshal.dumps(compile(source, "a/different/build/path.py", "exec", dont_inherit=True, optimize=0))
        with patch.dict(sys.modules, self._archive_modules(code)):
            verifier.assert_reviewed_runtime_hook(self.root / "not-executed.exe")
        with patch.dict(sys.modules, self._archive_modules(code, ["pyiboot01_bootstrap", "artifact_runtime_probe", "manager_entrypoint"])):
            verifier.assert_reviewed_runtime_hook(self.root / "not-executed.exe")
        changed = marshal.dumps(compile("raise SystemExit(0)", "different.py", "exec"))
        with patch.dict(sys.modules, self._archive_modules(changed)):
            with self.assertRaisesRegex(ValueError, "differs"):
                verifier.assert_reviewed_runtime_hook(self.root / "not-executed.exe")
        with patch.dict(sys.modules, self._archive_modules(code, ["pyiboot01_bootstrap", "CodexSotaManager", "artifact_runtime_probe"])):
            with self.assertRaisesRegex(ValueError, "ahead"):
                verifier.assert_reviewed_runtime_hook(self.root / "not-executed.exe")

    def test_old_executable_is_never_started_even_if_it_would_ignore_flag(self) -> None:
        create_bundle_fixture(self.root)
        with patch.object(verifier, "assert_reviewed_runtime_hook", side_effect=ValueError("missing hook")), \
             patch.object(verifier.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "missing hook"):
                verifier.probe_artifact_runtime(self.root, self.root)
        run.assert_not_called()

    def _probe(self, *, mutate=None, returncode=0, write_report=True):
        artifact, output = self.root / "bundle", self.root / "results"
        if not artifact.exists():
            create_bundle_fixture(artifact)
        output.mkdir(exist_ok=True)
        def run(arguments, **options):
            self.assertEqual(arguments[1], hook.PROBE_FLAG)
            self.assertEqual(options["timeout"], 30)
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            report = fixture_runtime_ready(verifier._sha256(artifact / "codex-sota.exe"))
            report["nonce"] = arguments[3]
            if mutate:
                mutate(report)
            if write_report:
                Path(arguments[2]).write_text(json.dumps(report), encoding="utf-8")
            return SimpleNamespace(returncode=returncode)
        with patch.object(verifier, "assert_reviewed_runtime_hook"), \
             patch.object(verifier.subprocess, "run", side_effect=run):
            return verifier.probe_artifact_runtime(artifact, output)

    def test_runtime_verifier_requires_nonce_binary_hash_and_every_dependency(self) -> None:
        self.assertEqual(self._probe()["status"], "ready")
        invalid = (
            lambda item: item.update(nonce="old"),
            lambda item: item.update(executable_sha256="0" * 64),
            lambda item: item.update(status="starting"),
            lambda item: item.update(network_used=True),
            lambda item: item.update(profiles_loaded=True),
            lambda item: item["checks"].pop("sqlite"),
            lambda item: item.update(probe_protocol=True),
        )
        for mutation in invalid:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    self._probe(mutate=mutation)

    def test_runtime_verifier_does_not_accept_zero_exit_without_ready_or_failed_exit(self) -> None:
        with self.assertRaisesRegex(ValueError, "ready record"):
            self._probe(write_report=False)
        with self.assertRaisesRegex(ValueError, "exit code"):
            self._probe(returncode=2)

    def test_full_verifier_stops_before_regressions_if_runtime_is_broken(self) -> None:
        artifact, output = self.root / "broken-runtime", self.root / "verification"
        create_bundle_fixture(artifact)
        with patch.object(sys, "argv", ["Verify-StartupFix.py", "--output", str(output), "--artifact-root", str(artifact)]), \
             patch.object(sys, "path", list(sys.path)), patch.dict(os.environ), \
             patch.object(verifier, "source_hashes", return_value={}), \
             patch.object(verifier, "probe_artifact_runtime", side_effect=ValueError("failed runtime")), \
             patch.object(unittest.defaultTestLoader, "discover") as discover:
            self.assertEqual(verifier.main(), 1)
        discover.assert_not_called()
        self.assertEqual(json.loads((output / "result.json").read_text())["status"], "failed")

    def test_runtime_only_post_probe_drift_preserves_failure_receipt(self) -> None:
        artifact, output = self.root / "drifting-runtime", self.root / "runtime-verification"
        create_bundle_fixture(artifact)
        initial = verifier.artifact_hashes(artifact)
        with patch.object(sys, "argv", ["Verify-StartupFix.py", "--output", str(output), "--artifact-root", str(artifact), "--runtime-only"]), \
             patch.object(sys, "path", list(sys.path)), patch.dict(os.environ), \
             patch.object(verifier, "source_hashes", return_value={}), \
             patch.object(verifier, "artifact_hashes", side_effect=[initial, ValueError("fixture DLL disappeared")]), \
             patch.object(verifier, "probe_artifact_runtime", return_value=fixture_runtime_ready(initial["codex-sota.exe"])), \
             patch.object(unittest.defaultTestLoader, "discover") as discover:
            self.assertEqual(verifier.main(), 1)
        discover.assert_not_called()
        result = json.loads((output / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["validation_scope"], "runtime-only")
        self.assertIn("DLL disappeared", result["reason"])


class RuntimeReceiptTests(DeploymentScriptTests):
    # Reuse only the synthetic fixture helpers, not the parent's test cases.
    def test_installer_rejects_missing_failed_or_wrong_runtime_evidence(self) -> None:
        script, staged, live, report = self._tree("Install-StartupFix.ps1")
        original = json.loads(report.read_text())
        mutations = (
            lambda item: item.pop("runtime_probe"),
            lambda item: item.update(validation_scope="runtime-only"),
            lambda item: item["runtime_probe"].update(status="failed"),
            lambda item: item["runtime_probe"].update(executable_sha256="0" * 64),
            lambda item: item["runtime_probe"]["checks"].pop("ssl"),
        )
        for mutate in mutations:
            changed = deepcopy(original)
            mutate(changed)
            report.write_text(json.dumps(changed), encoding="utf-8")
            self._assert_not_installed(self._run(script, "-CandidateRoot", staged, "-VerificationReport", report), staged, live)


# unittest also discovers imported TestCase classes. Keep their fixture methods
# without multiplying all of the legacy deployment tests in this module.
for _name in dir(DeploymentScriptTests):
    if _name.startswith("test_") and _name not in RuntimeReceiptTests.__dict__:
        setattr(RuntimeReceiptTests, _name, None)
del DeploymentScriptTests
del DeploymentFixture


if __name__ == "__main__":
    unittest.main()
