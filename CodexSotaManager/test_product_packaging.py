"""Runtime and release checks that exercise clean-machine behavior."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

MANAGER = Path(__file__).resolve().parent
ROOT = MANAGER.parent
ARTIFACT = os.environ.get("CODEX_SOTA_ARTIFACT_ROOT", "")
sys.path.insert(0, str(ROOT / "CodexHistorySync"))


class RuntimeCommandTests(unittest.TestCase):
    def invocation(self) -> list[str]:
        if ARTIFACT:
            return [str(Path(ARTIFACT) / "codex-sota-runtime.exe")]
        return [sys.executable, "-B", str(MANAGER / "runtime_entrypoint.py")]

    def call_runtime(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        for name in ("PYTHONPATH", "CODEX_SOTA_CORE_ROOT", "CODEX_SOTA_PYTHON", "CODEX_PYTHON"):
            environment.pop(name, None)
        environment["PATH"] = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32"
        return subprocess.run(self.invocation() + list(arguments), capture_output=True,
                              text=True, encoding="utf-8", env=environment, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_probe_matches_launcher_contract_without_system_python(self) -> None:
        result = self.call_runtime("-I", "-S", "-B", "-c", "import sys; print(193731 if sys.version_info >= (3,11) else 0)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "193731")

    def test_script_arguments_and_exit_code_propagate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sota runtime 中文 ") as directory:
            script = Path(directory) / "helper.py"
            script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\nraise SystemExit(7)\n", encoding="utf-8")
            result = self.call_runtime(str(script), "argument with spaces", "中文")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["argument with spaces", "中文"])

    def test_module_mode_and_version(self) -> None:
        result = self.call_runtime("-m", "json.tool", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        version = self.call_runtime("--version")
        self.assertEqual(version.returncode, 0)
        self.assertTrue(version.stdout.startswith("Python 3."))

    def test_runtime_has_control_chain_and_tls_sqlite(self) -> None:
        result = self.call_runtime("-c", "import sqlite3,ssl,tomllib,tkinter,sota_registry,sota_network,sota_compaction,codex_sota_router,claude_desktop,codex_app_lifecycle,sync_codex_histories_three_way; print('ready')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ready")

    def test_missing_script_fails_clearly(self) -> None:
        result = self.call_runtime("does-not-exist.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not found", result.stderr)

    def test_tcl_tk_data_is_usable_by_the_bundled_runtime(self) -> None:
        result = self.call_runtime("-c", "import tkinter; root=tkinter.Tk(); root.withdraw(); root.update_idletasks(); root.destroy(); print('tk-ready')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "tk-ready")


@unittest.skipUnless(ARTIFACT, "Requires an explicitly selected packaged artifact")
class PackagedReleaseTests(unittest.TestCase):
    def test_every_control_helper_is_shipped(self) -> None:
        bundle = Path(ARTIFACT)
        self.assertTrue((bundle / "codex-sota.exe").is_file())
        self.assertTrue((bundle / "codex-sota-runtime.exe").is_file())
        self.assertTrue((bundle / "codex-sota.cmd").is_file())
        core = bundle / "_internal" / "CodexHistorySync"
        for name in ("Start-CodexSotaRouter.ps1", "Switch-CodexSota.ps1", "Switch-CodexProfile.ps1",
                     "Start-CodexStoreProcess.ps1",
                     "Run-CodexHistorySync.ps1", "release_claude_slot_after_exit.py",
                     "sync_after_codex_exit.py", "sota_network.py", "sota_compaction.py"):
            with self.subTest(name=name):
                self.assertTrue((core / name).is_file(), name)

    def test_bundled_helpers_include_their_literal_local_dependencies(self) -> None:
        core = Path(ARTIFACT) / "_internal" / "CodexHistorySync"
        for source in core.iterdir():
            if source.suffix.lower() not in {".ps1", ".py"}:
                continue
            for name in re.findall(r"['\"]([A-Za-z_][A-Za-z0-9_-]*\.(?:ps1|py))['\"]", source.read_text(encoding="utf-8-sig")):
                # Examples in comments may refer to developer-only checks. A literal
                # dependency that exists in the core source tree must ship as well.
                if (ROOT / "CodexHistorySync" / name).is_file():
                    with self.subTest(source=source.name, dependency=name):
                        self.assertTrue((core / name).is_file(), f"{source.name} requires {name}")

    def test_claude_dpapi_workspace_audits_without_a_codex_auth_file(self) -> None:
        from sota_registry import dpapi_protect
        with tempfile.TemporaryDirectory(prefix="sota claude dpapi fixture ") as directory:
            root = Path(directory)
            (root / "secrets").mkdir()
            dpapi_protect("packaging-fixture-key", root / "secrets" / "fixture.dpapi", "CodexSota.PackagingFixture.v1")
            provider = {"id": "fixture", "name": "Fixture", "workspace": "claude", "enabled": True, "is_default": True,
                        "base_url": "http://127.0.0.1:1/v1", "prefix": "",
                        "auth_type": "dpapi", "secret_file": "fixture.dpapi", "entropy": "CodexSota.PackagingFixture.v1",
                        "auth_header": "x-api-key", "auth_prefix": "", "protocols": ["messages"],
                        "models": [{"id": "fixture-model", "enabled": True}]}
            (root / "providers.json").write_text(json.dumps({"version": 1, "providers": [provider]}), encoding="utf-8")
            environment = os.environ.copy()
            environment["CODEX_SOTA_CLAUDE_ROOT"] = str(root)
            environment["CODEX_SOTA_CODEX_ROOT"] = str(root / "unused-codex")
            environment.pop("CODEX_SOTA_CORE_ROOT", None)
            environment.pop("CODEX_SOTA_PYTHON", None)
            environment.pop("CODEX_PYTHON", None)
            powershell = str(Path(environment.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            script = Path(ARTIFACT) / "_internal" / "CodexHistorySync" / "Start-ClaudeSotaRouter.ps1"
            runtime = Path(ARTIFACT) / "codex-sota-runtime.exe"
            check = subprocess.run([str(runtime), str(script.with_name("codex_sota_router.py")), "--check",
                                    "--registry", str(root / "providers.json"), "--auth", str(root / "auth.json")],
                                   capture_output=True, text=True, encoding="utf-8", env=environment, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(script), "-AuditOnly", "-SotaRootOverride", str(root), "-RouterPortOverride", "1"],
                                    capture_output=True, text=True, encoding="utf-8-sig", errors="replace", env=environment,
                                    timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["status"], "ready")
            self.assertFalse((root / "auth.json").exists())

    def test_frozen_router_forwards_to_a_local_fixture_with_its_own_credentials(self) -> None:
        requests = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_arguments):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "model": payload.get("model"), "authorization": self.headers.get("Authorization")})
                data = json.dumps({"id": "resp_fixture", "object": "response", "model": payload["model"], "output": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="sota frozen fixture ") as directory:
                root = Path(directory)
                provider = {"id": "fixture", "name": "Fixture", "enabled": True, "is_default": True,
                            "base_url": f"http://127.0.0.1:{server.server_port}/v1", "prefix": "fixture--",
                            "auth_type": "codex_auth", "auth_header": "Authorization", "auth_prefix": "Bearer ",
                            "responses_path": "/responses", "models_path": "/models", "timeout_seconds": 5,
                            "models": [{"id": "fixture-model", "enabled": True}]}
                registry = root / "providers.json"
                registry.write_text(json.dumps({"version": 1, "providers": [provider]}), encoding="utf-8")
                auth = root / "auth.json"
                auth.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "packaging-fixture-key"}), encoding="utf-8")
                import socket
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                bundle = Path(ARTIFACT)
                arguments = [str(bundle / "codex-sota-runtime.exe"), str(bundle / "_internal" / "CodexHistorySync" / "codex_sota_router.py"),
                             "--host", "127.0.0.1", "--port", str(port), "--registry", str(registry), "--auth", str(auth),
                             "--pid-file", str(root / "router.pid"), "--log", str(root / "router.jsonl")]
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with (root / "stderr.txt").open("wb") as stderr:
                    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=stderr,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    try:
                        deadline = time.monotonic() + 12
                        while True:
                            try:
                                with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                                    self.assertEqual(json.load(response)["status"], "ok")
                                break
                            except OSError:
                                if process.poll() is not None or time.monotonic() >= deadline:
                                    self.fail("The isolated frozen router did not become healthy")
                                time.sleep(0.05)
                        request = urllib.request.Request(f"http://127.0.0.1:{port}/responses",
                            data=json.dumps({"model": "fixture--fixture-model", "input": "fixture", "stream": False}).encode(),
                            headers={"Content-Type": "application/json", "Authorization": "Bearer client-fixture-key"})
                        with opener.open(request, timeout=8) as response:
                            self.assertEqual(json.load(response)["id"], "resp_fixture")
                        self.assertEqual(requests, [{"path": "/v1/responses", "model": "fixture-model", "authorization": "Bearer packaging-fixture-key"}])
                    finally:
                        if process.poll() is None:
                            process.terminate()
                        process.wait(timeout=10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_bundle_does_not_contain_personal_data(self) -> None:
        bundle = Path(ARTIFACT)
        for path in bundle.rglob("*"):
            if path.is_file():
                self.assertNotIn(path.name.lower(), {"providers.json", "auth.json", "config.toml", "active-profile.json"})
                self.assertNotIn(path.suffix.lower(), {".dpapi", ".sqlite", ".jsonl", ".log"})


if __name__ == "__main__":
    unittest.main()
