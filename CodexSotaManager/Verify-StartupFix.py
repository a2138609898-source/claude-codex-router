"""Offline regression gate for the startup repair; never launches real Codex.

Persist results so interrupting a terminal or switching tasks cannot lose the
evidence. All behavioral tests use their own temporary history/router fixtures.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import marshal
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from types import CodeType
import unittest
import uuid

MANAGER = Path(__file__).resolve().parent
CORE = MANAGER.parent / "CodexHistorySync"
ARTIFACT_MANIFEST = "codex-sota-artifact-manifest.json"
REQUIRED_ARTIFACT_FILES = (
    "codex-sota.exe", "_internal/base_library.zip", "_internal/_tkinter.pyd",
    "_internal/_ctypes.pyd", "_internal/_ssl.pyd", "_internal/_socket.pyd",
    "_internal/_sqlite3.pyd", "_internal/sqlite3.dll", "_internal/vcruntime140.dll",
)
REQUIRED_ARTIFACT_ROLES = (
    r"_internal/python3(1[1-9]|[2-9][0-9])t?(_d)?\.dll",
    r"_internal/tcl[89][0-9a-z]*\.dll", r"_internal/tk[89][0-9a-z]*\.dll",
    r"_internal/libssl[^/]*\.dll", r"_internal/libcrypto[^/]*\.dll",
    r"_internal/(_tcl_data|tcl[89][^/]*)/init\.tcl",
    r"_internal/(_tk_data|tk[89][^/]*)/tk\.tcl",
)
RUNTIME_PROBE_CHECKS = ("python", "ctypes", "ssl", "sqlite", "tcl", "socket")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_link(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Linked artifact entries are not supported: {path}")
    return info


def _artifact_root(root: Path) -> Path:
    absolute = Path(os.path.abspath(root))
    for item in (absolute, *absolute.parents):
        _reject_link(item)
    if not absolute.is_dir():
        raise ValueError(f"Artifact directory is missing: {absolute}")
    return absolute


def artifact_payload_hashes(root: Path) -> dict[str, str]:
    """Snapshot every payload file without following junctions or loading any executable."""
    artifact = _artifact_root(root)
    pending = [artifact]
    files: dict[str, str] = {}
    names: dict[str, Path] = {}
    while pending:
        for path in pending.pop().iterdir():
            info = _reject_link(path)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Artifact entry is not a regular file: {path}")
            relative = path.relative_to(artifact).as_posix()
            if relative.casefold() == ARTIFACT_MANIFEST:
                continue
            if relative.casefold() in names:
                raise ValueError(f"Duplicate artifact filename: {relative}")
            names[relative.casefold()] = path
            files[relative] = _sha256(path)
    required = list(REQUIRED_ARTIFACT_FILES)
    for pattern in REQUIRED_ARTIFACT_ROLES:
        role_files = [name for name in names if re.fullmatch(pattern, name)]
        if not role_files:
            raise ValueError(f"Required artifact runtime role is missing: {pattern}")
        required.extend(role_files)
    for name in required:
        path = names.get(name)
        if path is None:
            raise ValueError(f"Required artifact runtime file is missing: {name}")
        if path.stat().st_size == 0:
            raise ValueError(f"Required artifact runtime file is empty: {name}")
        if path.suffix.lower() in {".exe", ".dll", ".pyd"}:
            with path.open("rb") as handle:
                if handle.read(2) != b"MZ":
                    raise ValueError(f"Required artifact binary has no PE signature: {name}")
    return dict(sorted(files.items()))


def _manifest_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    seen: set[str] = set()
    for key, item in pairs:
        if key.casefold() in seen:
            raise ValueError(f"Duplicate artifact manifest key: {key}")
        seen.add(key.casefold())
        value[key] = item
    return value


def write_artifact_manifest(root: Path) -> Path:
    artifact = _artifact_root(root)
    payload = artifact_payload_hashes(artifact)
    manifest = artifact / ARTIFACT_MANIFEST
    if manifest.exists():
        _reject_link(manifest)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=artifact)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"schema_version": 1, "files": payload}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def artifact_hashes(root: Path) -> dict[str, str]:
    """Verify the manifest and return a snapshot including the manifest's own hash."""
    artifact = _artifact_root(root)
    manifest_path = artifact / ARTIFACT_MANIFEST
    _reject_link(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_manifest_object)
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("Unsupported artifact manifest version")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("Artifact file hashes are missing or empty")
    for name, digest in expected.items():
        if (not name or "\\" in name or ":" in name or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.split("/"))):
            raise ValueError(f"Invalid relative artifact filename: {name}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"Invalid artifact hash: {name}")
    actual = artifact_payload_hashes(artifact)
    if {name.casefold(): digest for name, digest in actual.items()} != {
        name.casefold(): digest.lower() for name, digest in expected.items()
    }:
        raise ValueError("Artifact file inventory or contents changed")
    actual[ARTIFACT_MANIFEST] = _sha256(manifest_path)
    return actual


def source_hashes():
    # Include registry/helpers/build and installer sources, not only the historical handful
    # of startup files. Never recurse into profiles, bundles, caches or virtual environments.
    suffixes = {".py", ".ps1", ".cmd", ".bat", ".spec", ".ico"}
    sources = sorted(path for parent in (MANAGER, CORE) for path in parent.iterdir()
                     if path.is_file() and path.suffix.lower() in suffixes)
    hashes = {}
    for path in sources:
        _reject_link(path)
        hashes[str(path)] = _sha256(path)
    return hashes


def _normalized_code(code: CodeType) -> CodeType:
    return code.replace(
        co_filename="<reviewed-runtime-hook>",
        co_consts=tuple(_normalized_code(value) if isinstance(value, CodeType) else value
                        for value in code.co_consts),
    )


def assert_reviewed_runtime_hook(executable: Path) -> None:
    """Inspect, never execute, the hook before allowing an EXE self-test.

    An old executable may ignore an unknown flag and launch its real GUI. Require
    the exact reviewed hook ahead of the manager entrypoint to avoid that risk.
    Use the build interpreter, whose PyInstaller and bytecode version match the
    bundle, for this additional check.
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as error:
        raise ValueError("Use the PyInstaller build interpreter for runtime verification") from error
    try:
        archive = CArchiveReader(str(executable))
        scripts = [name for name, entry in archive.toc.items() if entry[-1] == "s"]
        hook_index = scripts.index("artifact_runtime_probe")
        entrypoints = {"CodexSotaManager", "manager_entrypoint"}
        if scripts[:hook_index] != ["pyiboot01_bootstrap"] or not entrypoints.intersection(scripts[hook_index + 1:]):
            raise ValueError("Runtime probe is not ahead of the application entrypoint")
        actual = marshal.loads(archive.extract("artifact_runtime_probe"))
        source = MANAGER / "artifact_runtime_probe.py"
        expected = compile(source.read_bytes(), str(source), "exec", dont_inherit=True, optimize=0)
        if not isinstance(actual, CodeType) or _normalized_code(actual) != _normalized_code(expected):
            raise ValueError("The executable's runtime hook differs from reviewed source")
    except (KeyError, IndexError, TypeError, EOFError, OSError) as error:
        raise ValueError("The executable has no compatible reviewed runtime probe") from error


def probe_artifact_runtime(root: Path, output: Path) -> dict:
    artifact = _artifact_root(root)
    executable = artifact / "codex-sota.exe"
    assert_reviewed_runtime_hook(executable)
    nonce = uuid.uuid4().hex
    report_path = output / ("runtime-probe-" + nonce + ".json")
    completed = subprocess.run(
        [str(executable), "--codex-sota-artifact-self-test", str(report_path.resolve()), nonce],
        cwd=artifact, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise ValueError(f"Frozen runtime self-test failed with exit code {completed.returncode}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Frozen runtime did not produce a valid ready record") from error
    if (not isinstance(report, dict) or type(report.get("probe_protocol")) is not int
            or report["probe_protocol"] != 1 or report.get("status") != "ready"
            or report.get("nonce") != nonce or report.get("network_used") is not False
            or report.get("profiles_loaded") is not False
            or report.get("executable_sha256") != _sha256(executable)
            or not isinstance(report.get("checks"), dict)
            or any(report["checks"].get(name) != "passed" for name in RUNTIME_PROBE_CHECKS)):
        raise ValueError("Frozen runtime ready record is incomplete or belongs to another invocation")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--runtime-only", action="store_true",
                        help="Only run the reviewed offline runtime hook; not an installation approval")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["CODEX_SOTA_ARTIFACT_ROOT"] = str(args.artifact_root.resolve())
    sys.path[:0] = [str(MANAGER), str(CORE)]
    try:
        before = source_hashes()
        # Older otherwise complete candidates can be re-verified without rebuilding solely
        # to add the manifest. Never replace an existing manifest to hide a changed bundle.
        if not (args.artifact_root / ARTIFACT_MANIFEST).exists():
            write_artifact_manifest(args.artifact_root)
        artifact_before = artifact_hashes(args.artifact_root)
    except (OSError, ValueError) as error:
        (args.output / "result.json").write_text(json.dumps({
            "verification_schema": 2, "status": "failed", "reason": str(error),
            "artifact_root": str(args.artifact_root.resolve()),
            "artifact_unchanged_during_tests": False,
            "sources_unchanged_during_tests": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1
    started = time.monotonic()
    try:
        runtime_probe = probe_artifact_runtime(args.artifact_root, args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        (args.output / "result.json").write_text(json.dumps({
            "verification_schema": 2, "status": "failed", "reason": str(error),
            "validation_scope": "runtime-only" if args.runtime_only else "full-bundle-and-regressions",
            "artifact_root": str(args.artifact_root.resolve()),
            "artifact_unchanged_during_tests": False, "sources_unchanged_during_tests": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1
    if args.runtime_only:
        problem = ""
        try:
            sources_unchanged = before == source_hashes()
            artifact_unchanged = artifact_before == artifact_hashes(args.artifact_root)
        except (OSError, ValueError) as error:
            sources_unchanged = artifact_unchanged = False
            problem = str(error)
        stable = sources_unchanged and artifact_unchanged
        (args.output / "result.json").write_text(json.dumps({
            "verification_schema": 2, "status": "passed" if stable else "failed",
            "validation_scope": "runtime-only", "runtime_probe": runtime_probe,
            "artifact_root": str(args.artifact_root.resolve()),
            "sources_unchanged_during_tests": sources_unchanged,
            "artifact_unchanged_during_tests": artifact_unchanged, "reason": problem,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0 if stable else 1
    suite = unittest.defaultTestLoader.discover(str(MANAGER), pattern="test_*.py")
    for module in ("check_history_sync_regressions", "check_history_model_guard",
                   "check_codex_app_lifecycle", "check_sota_launch_history_repair"):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(module))
    with (args.output / "regressions.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    scripts = []
    powershell = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                     "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    with (args.output / "powershell.log").open("w", encoding="utf-8") as log:
        for name in ("check_sota_launch_lifecycle.ps1", "check_launcher_process_guard.ps1"):
            try:
                completed = subprocess.run(
                    [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(CORE / name)],
                    cwd=CORE, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                scripts.append({"script": name, "exit_code": completed.returncode})
            except (OSError, subprocess.SubprocessError) as error:
                scripts.append({"script": name, "exit_code": None, "error": str(error)})
    source_problem = ""
    try:
        after = source_hashes()
    except (OSError, ValueError) as error:
        after = {}
        source_problem = str(error)
    artifact_problem = ""
    try:
        artifact_after = artifact_hashes(args.artifact_root)
    except (OSError, ValueError) as error:
        artifact_after = {}
        artifact_problem = str(error)
    artifact_unchanged = artifact_before == artifact_after
    success = (result.wasSuccessful() and all(item["exit_code"] == 0 for item in scripts)
               and before == after and artifact_unchanged and not source_problem and not artifact_problem)
    report = {
        "verification_schema": 2,
        "validation_scope": "full-bundle-and-regressions",
        "runtime_probe": runtime_probe,
        "status": "passed" if success else "failed",
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "tests_run": result.testsRun,
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
        "powershell": scripts, "sources_unchanged_during_tests": before == after,
        "source_sha256": after, "artifact_root": str(args.artifact_root.resolve()),
        "artifact_sha256": next(digest for name, digest in artifact_before.items() if name.casefold() == "codex-sota.exe"),
        "artifact_files_sha256": artifact_before,
        "artifact_unchanged_during_tests": artifact_unchanged,
        "artifact_problem": artifact_problem,
        "source_problem": source_problem,
    }
    (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
