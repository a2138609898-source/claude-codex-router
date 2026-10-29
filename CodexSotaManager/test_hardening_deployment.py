"""Synthetic bundle integrity tests; no packaged executable is ever launched."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


MANAGER_ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_artifact_verifier_for_tests", MANAGER_ROOT / "Verify-StartupFix.py")
verifier = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(verifier)
RUNTIME_FILES = tuple(verifier.REQUIRED_ARTIFACT_FILES) + (
    "_internal/python311.dll", "_internal/tcl86t.dll", "_internal/tk86t.dll",
    "_internal/libssl-3.dll", "_internal/libcrypto-3.dll",
    "_internal/_tcl_data/init.tcl", "_internal/_tk_data/tk.tcl",
)


def create_bundle_fixture(artifact: Path) -> None:
    artifact.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        path = artifact / name
        path.parent.mkdir(parents=True, exist_ok=True)
        binary = path.suffix.lower() in {".exe", ".dll", ".pyd"}
        path.write_bytes((b"MZ" if binary else b"") + b"synthetic-not-an-executable-fixture")
    verifier.write_artifact_manifest(artifact)


def fixture_runtime_ready(executable_hash: str) -> dict:
    return {
        "probe_protocol": 1, "status": "ready", "nonce": "0" * 32,
        "executable_sha256": executable_hash, "network_used": False, "profiles_loaded": False,
        "checks": {name: "passed" for name in verifier.RUNTIME_PROBE_CHECKS},
    }


def prepare_staged_bundle_fixture(root: Path, staged: Path) -> Path:
    """Upgrade the old swap tests' marker directories without changing their assertions."""
    shutil.copy2(MANAGER_ROOT / "Artifact-Integrity.ps1", root / "Artifact-Integrity.ps1")
    create_bundle_fixture(staged)
    source = root / "fixture-source.py"
    source.write_text("# Synthetic source identity; never executed.\n", encoding="utf-8")
    hashes = verifier.artifact_hashes(staged)
    report = {
        "verification_schema": 2, "status": "passed",
        "validation_scope": "full-bundle-and-regressions",
        "runtime_probe": fixture_runtime_ready(hashes["codex-sota.exe"]),
        "sources_unchanged_during_tests": True, "artifact_unchanged_during_tests": True,
        "artifact_root": str(staged.resolve()), "artifact_sha256": hashes["codex-sota.exe"],
        "artifact_files_sha256": hashes,
        "source_sha256": {str(source.resolve()): hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    report_path = staged.parent / "codex-sota-verification.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path


class DeploymentFixture(unittest.TestCase):
    def setUp(self) -> None:
        allowed = MANAGER_ROOT.parents[1] / "test-tmp"
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if not temporary_root.is_relative_to(allowed.resolve()):
            allowed.mkdir(parents=True, exist_ok=True)
            temporary_root = allowed.resolve()
        temporary = tempfile.TemporaryDirectory(prefix="deploy-", dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class DeploymentManifestTests(DeploymentFixture):
    def test_complete_manifest_binds_every_payload_file_and_itself(self) -> None:
        create_bundle_fixture(self.root)
        hashes = verifier.artifact_hashes(self.root)
        self.assertEqual(set(hashes), set(RUNTIME_FILES) | {verifier.ARTIFACT_MANIFEST})
        self.assertEqual(hashes["_internal/python311.dll"], hashlib.sha256((self.root / "_internal/python311.dll").read_bytes()).hexdigest())

    def test_every_required_runtime_role_is_checked_before_stamping_a_manifest(self) -> None:
        create_bundle_fixture(self.root)
        for name in RUNTIME_FILES:
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(ValueError):
                        verifier.write_artifact_manifest(self.root)
                finally:
                    path.write_bytes(original)

    def test_dll_mutation_is_detected_even_when_executable_is_unchanged(self) -> None:
        create_bundle_fixture(self.root)
        executable = (self.root / "codex-sota.exe").read_bytes()
        (self.root / "_internal/python311.dll").write_bytes(b"MZchanged-fixture")
        with self.assertRaisesRegex(ValueError, "changed"):
            verifier.artifact_hashes(self.root)
        self.assertEqual((self.root / "codex-sota.exe").read_bytes(), executable)

    def test_deleting_optional_payload_or_adding_unlisted_file_is_detected(self) -> None:
        create_bundle_fixture(self.root)
        optional = self.root / "optional-fixture.dat"
        optional.write_bytes(b"fixture-data")
        verifier.write_artifact_manifest(self.root)
        optional.unlink()
        with self.assertRaisesRegex(ValueError, "changed"):
            verifier.artifact_hashes(self.root)
        optional.write_bytes(b"fixture-data")
        (self.root / "injected-fixture.dat").write_bytes(b"new-fixture-data")
        with self.assertRaisesRegex(ValueError, "changed"):
            verifier.artifact_hashes(self.root)

    def test_empty_or_non_pe_runtime_cannot_receive_a_valid_manifest(self) -> None:
        for content in (b"", b"not-a-PE-fixture"):
            with self.subTest(content=content):
                create_bundle_fixture(self.root)
                (self.root / "_internal/python311.dll").write_bytes(content)
                with self.assertRaises(ValueError):
                    verifier.write_artifact_manifest(self.root)

    def test_python_310_runtime_cannot_satisfy_the_python_311_minimum(self) -> None:
        create_bundle_fixture(self.root)
        runtime = self.root / "_internal/python311.dll"
        destination = self.root / "_internal/python310.dll"
        self.assertTrue(runtime.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(destination.resolve().is_relative_to(self.root.resolve()))
        runtime.rename(destination)
        with self.assertRaisesRegex(ValueError, "runtime role"):
            verifier.write_artifact_manifest(self.root)

    def test_manifest_rewrite_changes_the_report_snapshot(self) -> None:
        create_bundle_fixture(self.root)
        before = verifier.artifact_hashes(self.root)
        (self.root / "_internal/python311.dll").write_bytes(b"MZrestamped-fixture")
        verifier.write_artifact_manifest(self.root)
        after = verifier.artifact_hashes(self.root)
        self.assertEqual(before["codex-sota.exe"], after["codex-sota.exe"])
        self.assertNotEqual(before, after)
        self.assertNotEqual(before[verifier.ARTIFACT_MANIFEST], after[verifier.ARTIFACT_MANIFEST])

    def test_manifest_rejects_traversal_duplicates_bad_hash_and_wrong_schema(self) -> None:
        create_bundle_fixture(self.root)
        path = self.root / verifier.ARTIFACT_MANIFEST
        valid = json.loads(path.read_text(encoding="utf-8"))
        cases = []
        for bad_name in ("../outside.dll", "/absolute.dll", "_internal\\evil.dll", "a//b", "C:evil.dll"):
            candidate = deepcopy(valid)
            candidate["files"][bad_name] = "0" * 64
            cases.append(json.dumps(candidate))
        bad_hash = deepcopy(valid)
        bad_hash["files"]["codex-sota.exe"] = "not-a-sha256"
        cases.append(json.dumps(bad_hash))
        cases.append(json.dumps(dict(valid, schema_version=2)))
        cases.append('{"schema_version":1,"files":{"one":"' + "0" * 64 + '","ONE":"' + "0" * 64 + '"}}')
        for source in cases:
            with self.subTest(source=source[:90]):
                path.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    verifier.artifact_hashes(self.root)

    def test_windows_junction_attribute_is_rejected_without_following_it(self) -> None:
        path = Mock()
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with self.assertRaisesRegex(ValueError, "Linked"):
            verifier._reject_link(path)

    def test_source_fingerprint_includes_registry_helpers_and_installers(self) -> None:
        manager, core = self.root / "manager", self.root / "core"
        manager.mkdir()
        core.mkdir()
        expected = [manager / "CodexSotaManager.py", manager / "Artifact-Integrity.ps1",
                    manager / "codex-sota.spec", core / "sota_registry.py", core / "launcher.cmd"]
        for path in expected:
            path.write_text("fixture source", encoding="utf-8")
        (manager / "private-settings.json").write_text("{}", encoding="utf-8")
        with patch.object(verifier, "MANAGER", manager), patch.object(verifier, "CORE", core):
            hashes = verifier.source_hashes()
        self.assertEqual(set(hashes), {str(path) for path in expected})

    def _run_verifier(self, mutate=None, subprocess_error=None):
        artifact = self.root / "bundle"
        output = self.root / "verification"
        create_bundle_fixture(artifact)
        suite = unittest.TestSuite([unittest.FunctionTestCase(mutate or (lambda: None))])
        with patch.object(sys, "argv", ["Verify-StartupFix.py", "--output", str(output), "--artifact-root", str(artifact)]), \
             patch.object(sys, "path", list(sys.path)), patch.dict(os.environ), \
             patch.object(verifier, "source_hashes", return_value={"fixture-source": "0" * 64}), \
             patch.object(verifier, "probe_artifact_runtime", return_value=fixture_runtime_ready("0" * 64)), \
             patch.object(unittest.defaultTestLoader, "discover", return_value=suite), \
             patch.object(unittest.defaultTestLoader, "loadTestsFromName", side_effect=lambda _name: unittest.TestSuite()), \
             patch.object(verifier.subprocess, "run", return_value=SimpleNamespace(returncode=0), side_effect=subprocess_error) as subprocess_mock:
            code = verifier.main()
        self.assertEqual(subprocess_mock.call_count, 2)
        return code, json.loads((output / "result.json").read_text(encoding="utf-8"))

    def test_verification_report_snapshots_full_bundle_before_and_after_tests(self) -> None:
        code, report = self._run_verifier()
        self.assertEqual(code, 0)
        self.assertEqual(report["verification_schema"], 2)
        self.assertTrue(report["artifact_unchanged_during_tests"])
        self.assertIn("_internal/python311.dll", report["artifact_files_sha256"])
        self.assertIn(verifier.ARTIFACT_MANIFEST, report["artifact_files_sha256"])

    def test_verification_fails_if_tests_leave_changed_dll_even_with_same_exe(self) -> None:
        def mutate():
            (self.root / "bundle/_internal/python311.dll").write_bytes(b"MZchanged-during-tests")
        code, report = self._run_verifier(mutate)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["artifact_unchanged_during_tests"])
        self.assertTrue(report["sources_unchanged_during_tests"])

    def test_invalid_artifact_gets_failure_report_before_any_test_or_subprocess(self) -> None:
        artifact, output = self.root / "broken", self.root / "verification"
        artifact.mkdir()
        with patch.object(sys, "argv", ["Verify-StartupFix.py", "--output", str(output), "--artifact-root", str(artifact)]), \
             patch.object(sys, "path", list(sys.path)), patch.dict(os.environ), \
             patch.object(verifier, "source_hashes", return_value={}), \
             patch.object(unittest.defaultTestLoader, "discover") as discover, \
             patch.object(verifier.subprocess, "run") as subprocess_mock:
            self.assertEqual(verifier.main(), 1)
        discover.assert_not_called()
        subprocess_mock.assert_not_called()
        self.assertEqual(json.loads((output / "result.json").read_text())["status"], "failed")

    def test_powershell_timeout_preserves_a_structured_failed_report(self) -> None:
        code, report = self._run_verifier(subprocess_error=subprocess.TimeoutExpired("fixture-check", 90))
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["powershell"]), 2)
        self.assertTrue(all(item["exit_code"] is None and item["error"] for item in report["powershell"]))


class DeploymentScriptTests(DeploymentFixture):
    def setUp(self) -> None:
        super().setUp()
        self.powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not self.powershell:
            self.skipTest("Windows PowerShell is unavailable")

    def _tree(self, script_name="Apply-StagedBuild.ps1"):
        script = self.root / script_name
        shutil.copy2(MANAGER_ROOT / script_name, script)
        staged = self.root / "dist-staging/codex-sota"
        staged.mkdir(parents=True)
        (staged / "new.txt").write_text("new", encoding="utf-8")
        report = prepare_staged_bundle_fixture(self.root, staged)
        live = self.root / "dist/codex-sota"
        live.mkdir(parents=True)
        (live / "old.txt").write_text("old", encoding="utf-8")
        return script, staged, live, report

    def _run(self, script, *arguments):
        return subprocess.run(
            [self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), *map(str, arguments)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _assert_not_installed(self, result, staged, live):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((live / "old.txt").read_text(), "old")
        self.assertEqual((staged / "new.txt").read_text(), "new")
        self.assertEqual(list((self.root / "dist").glob("codex-sota.old-*")), [])
        self.assertEqual(list((self.root / "dist").glob("codex-sota.incoming-*")), [])

    def test_apply_accepts_the_exact_verified_complete_bundle(self) -> None:
        script, staged, live, _report = self._tree()
        result = self._run(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((live / "new.txt").read_text(), "new")
        self.assertFalse(staged.exists())
        self.assertEqual(len(list((self.root / "dist").glob("codex-sota.old-*"))), 1)

    def test_apply_refuses_missing_python_dll_before_retiring_live(self) -> None:
        script, staged, live, _report = self._tree()
        (staged / "_internal/python311.dll").unlink()
        self._assert_not_installed(self._run(script), staged, live)

    def test_apply_refuses_mutated_dll_with_unchanged_exe(self) -> None:
        script, staged, live, _report = self._tree()
        (staged / "_internal/python311.dll").write_bytes(b"MZchanged")
        self._assert_not_installed(self._run(script), staged, live)

    def test_apply_refuses_dll_and_manifest_rewrite_against_original_receipt(self) -> None:
        script, staged, live, _report = self._tree()
        (staged / "_internal/python311.dll").write_bytes(b"MZchanged")
        verifier.write_artifact_manifest(staged)
        verifier.artifact_hashes(staged)  # Self-consistency alone deliberately passes.
        self._assert_not_installed(self._run(script), staged, live)

    def test_apply_requires_an_independent_verification_receipt(self) -> None:
        script, staged, live, report = self._tree()
        report.unlink()
        self._assert_not_installed(self._run(script), staged, live)

    def test_apply_refuses_failed_or_unstable_artifact_receipt(self) -> None:
        script, staged, live, report = self._tree()
        original = json.loads(report.read_text())
        for field, value in (("status", "failed"), ("artifact_unchanged_during_tests", False),
                             ("artifact_unchanged_during_tests", "true"), ("source_sha256", {})):
            with self.subTest(field=field, value=value):
                changed = deepcopy(original)
                changed[field] = value
                report.write_text(json.dumps(changed), encoding="utf-8")
                self._assert_not_installed(self._run(script), staged, live)

    def test_apply_refuses_receipt_bound_to_another_candidate_path(self) -> None:
        script, staged, live, report = self._tree()
        changed = json.loads(report.read_text())
        changed["artifact_root"] = str(self.root / "another-candidate")
        report.write_text(json.dumps(changed), encoding="utf-8")
        self._assert_not_installed(self._run(script), staged, live)

    def test_apply_refuses_source_changes_since_verification(self) -> None:
        script, staged, live, _report = self._tree()
        (self.root / "fixture-source.py").write_text("# changed fixture source", encoding="utf-8")
        self._assert_not_installed(self._run(script), staged, live)

    def test_install_and_repeated_install_verify_the_complete_live_tree(self) -> None:
        script, staged, live, report = self._tree("Install-StartupFix.ps1")
        arguments = ("-CandidateRoot", staged, "-VerificationReport", report)
        installed = self._run(script, *arguments)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(json.loads(installed.stdout)["status"], "installed")
        repeated = self._run(script, *arguments)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["status"], "already-installed")
        (live / "_internal/python311.dll").write_bytes(b"MZchanged-after-install")
        failed = self._run(script, *arguments)
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn('"already-installed"', failed.stdout)

    def test_install_refuses_rewritten_manifest_and_dll_after_verification(self) -> None:
        script, staged, live, report = self._tree("Install-StartupFix.ps1")
        (staged / "_internal/python311.dll").write_bytes(b"MZchanged")
        verifier.write_artifact_manifest(staged)
        result = self._run(script, "-CandidateRoot", staged, "-VerificationReport", report)
        self._assert_not_installed(result, staged, live)

    def test_install_does_not_treat_missing_runtime_as_already_installed(self) -> None:
        script, staged, live, report = self._tree("Install-StartupFix.ps1")
        arguments = ("-CandidateRoot", staged, "-VerificationReport", report)
        installed = self._run(script, *arguments)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        (live / "_internal/python311.dll").unlink()
        repeated = self._run(script, *arguments)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertNotIn('"already-installed"', repeated.stdout)

    def test_install_requires_schema_two_and_stable_artifact_flags(self) -> None:
        script, staged, live, report = self._tree("Install-StartupFix.ps1")
        value = json.loads(report.read_text())
        value["verification_schema"] = 1
        report.write_text(json.dumps(value), encoding="utf-8")
        self._assert_not_installed(self._run(script, "-CandidateRoot", staged, "-VerificationReport", report), staged, live)


class DeploymentSourceContractTests(unittest.TestCase):
    def test_build_validates_before_and_after_tests_then_writes_bound_receipt(self) -> None:
        source = (MANAGER_ROOT / "Build-Staged.ps1").read_text(encoding="utf-8-sig")
        manifest_at = source.index("New-ArtifactManifest $candidateApp")
        validation_at = source.index("& $validation -ArtifactRoot $candidateApp")
        final_check_at = source.index("Assert-ArtifactBundle $candidateApp $validatedFiles")
        receipt_at = source.index("artifact_files_sha256 = $validatedFiles")
        self.assertLess(manifest_at, validation_at)
        self.assertLess(validation_at, final_check_at)
        self.assertLess(final_check_at, receipt_at)
        self.assertNotIn("Remove-Item -LiteralPath $stagedRoot -Recurse", source)
        self.assertNotIn("--runtime-only", source)
        full_gate_at = source.index("--output $verificationOutput --artifact-root $candidateApp")
        self.assertLess(validation_at, full_gate_at)
        self.assertLess(full_gate_at, receipt_at)
        self.assertIn("validation_scope = $verificationResult.validation_scope", source)

    def test_deferred_installer_revalidates_after_wait_and_mutex(self) -> None:
        source = (MANAGER_ROOT / "Install-StartupFix-AfterManagerExit.ps1").read_text(encoding="utf-8-sig")
        self.assertLess(source.index("$mutex.WaitOne()"), source.index("-File $installScript"))
        self.assertIn("-CandidateRoot $CandidateRoot -VerificationReport $VerificationReport", source)
        install = (MANAGER_ROOT / "Install-StartupFix.ps1").read_text(encoding="utf-8-sig")
        self.assertLess(install.index("Read-ArtifactVerification"), install.index("Move-Item -LiteralPath $live"))
        self.assertIn("Assert-ArtifactBundle $live $expectedFiles", install)


if __name__ == "__main__":
    unittest.main()
