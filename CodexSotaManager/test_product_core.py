"""First-install and upgrade regressions; all files and credentials are synthetic."""
from __future__ import annotations

from copy import deepcopy
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import tomllib
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CodexHistorySync"))
import sota_registry as registry
import codex_sota_router as router


class FirstInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sota-first-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = registry.Workspace(
            "codex", "Fixture", self.root, 17895, self.root / "unused.ps1", "responses"
        )
        patch = mock.patch.dict(registry.WORKSPACES, {"codex": self.workspace})
        patch.start()
        self.addCleanup(patch.stop)
        self.provider = {
            "id": "fixture", "name": "Fixture", "workspace": "codex",
            "base_url": "https://relay.invalid", "auth_type": "dpapi",
            "models": [{"id": "fixture-model", "enabled": True}],
        }

    def test_opening_and_auditing_missing_workspace_writes_no_profile(self) -> None:
        self.assertEqual(registry.load_registry(self.workspace.registry_path)["providers"], [])
        self.assertEqual(registry.audit_registry(self.workspace)["status"], "empty")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupted_existing_registry_is_not_treated_as_empty(self) -> None:
        self.workspace.registry_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            registry.load_registry(self.workspace.registry_path)

    def test_first_provider_saves_without_private_template_or_installed_python(self) -> None:
        # Windows DPAPI is exercised on a disposable key under this test directory.
        with mock.patch.object(registry, "restart_router", side_effect=AssertionError("unexpected restart")):
            result = registry.apply_provider(
                deepcopy(self.provider), "synthetic-upstream-credential", restart=False,
                workspace=self.workspace,
            )
        self.assertEqual(result["status"], "ready")
        stored = registry.load_registry(self.workspace.registry_path)
        self.assertTrue(stored["providers"][0]["is_default"])
        self.assertEqual(registry.provider_key(stored["providers"][0]), "synthetic-upstream-credential")
        self.assertFalse(self.workspace.source_catalog_path.exists())
        catalog = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        self.assertEqual([model["slug"] for model in catalog["models"]], ["fixture--fixture-model"])
        self.assertNotIn("model_messages", catalog["models"][0])
        config = tomllib.loads((self.root / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "fixture--fixture-model")
        self.assertEqual(config["model_providers"]["codex_sota"]["base_url"], "http://127.0.0.1:17895")
        auth = json.loads(self.workspace.auth_path.read_text(encoding="utf-8"))
        self.assertTrue(auth["OPENAI_API_KEY"].startswith("sota-local-"))
        self.assertNotEqual(auth["OPENAI_API_KEY"], "synthetic-upstream-credential")

    def test_failed_first_install_rolls_back_client_and_credential_files(self) -> None:
        with mock.patch.object(registry, "restart_router", side_effect=RuntimeError("fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                registry.apply_provider(deepcopy(self.provider), "synthetic-credential", workspace=self.workspace)
        for path in (
            self.workspace.registry_path, self.workspace.catalog_path,
            self.workspace.auth_path, self.root / "config.toml",
            self.workspace.secrets_root / "fixture-api-key.dpapi",
        ):
            self.assertFalse(path.exists(), str(path))

    def test_bootstrap_preserves_existing_configuration_and_login_exactly(self) -> None:
        config = b'# user settings\nmodel_provider = "custom"\n'
        auth = b'{"OPENAI_API_KEY":"synthetic-existing-login","auth_mode":"apikey"}\n'
        (self.root / "config.toml").write_bytes(config)
        self.workspace.auth_path.write_bytes(auth)
        value = registry.validate_registry(
            {"version": 1, "providers": [dict(self.provider, is_default=True)]},
            allow_missing_secrets=True,
        )
        self.assertEqual(registry.ensure_codex_profile(value, self.workspace)["created"], [])
        self.assertEqual((self.root / "config.toml").read_bytes(), config)
        self.assertEqual(self.workspace.auth_path.read_bytes(), auth)

    def test_missing_borrowed_upstream_login_never_receives_a_fake_local_key(self) -> None:
        provider = dict(self.provider, auth_type="codex_auth", is_default=True)
        value = registry.validate_registry({"version": 1, "providers": [provider]})
        result = registry.ensure_codex_profile(value, self.workspace)
        self.assertEqual(result["status"], "authentication-required")
        self.assertFalse(self.workspace.auth_path.exists())
        self.assertFalse((self.root / "config.toml").exists())

    def test_legacy_source_filename_is_discovered_without_loading_generated_catalog(self) -> None:
        self.workspace.catalog_path.write_text('{"models":[]}', encoding="utf-8")
        legacy = self.root / "legacy-provider-model-catalog.json"
        legacy.write_text('{"models":[{"slug":"fixture-model","context_window":32100}]}', encoding="utf-8")
        self.assertEqual(self.workspace.source_catalog_path, legacy)
        registry.apply_provider(deepcopy(self.provider), "synthetic-credential", restart=False, workspace=self.workspace)
        generated = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        self.assertEqual(generated["models"][0]["context_window"], 32100)

    def test_one_missing_key_does_not_prevent_other_providers_starting(self) -> None:
        healthy = dict(self.provider, is_default=True)
        missing = dict(self.provider, id="missing", name="Missing", is_default=False)
        value = registry.validate_registry(
            {"version": 1, "providers": [healthy, missing]}, allow_missing_secrets=True,
        )
        registry.write_registry(value, self.workspace.registry_path, allow_missing_secrets=True)
        registry.dpapi_protect("synthetic-credential", registry.secret_path(value["providers"][0]), value["providers"][0]["entropy"])
        result = router.check_configuration(self.workspace.registry_path, self.workspace.auth_path)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["keys_present"], {"fixture": True, "missing": False})
        self.assertEqual(result["credential_errors"], {"missing": "missing_or_unreadable"})

    def test_no_usable_credentials_is_still_a_clear_startup_error(self) -> None:
        value = registry.validate_registry(
            {"version": 1, "providers": [dict(self.provider, is_default=True)]}, allow_missing_secrets=True,
        )
        registry.write_registry(value, self.workspace.registry_path, allow_missing_secrets=True)
        with self.assertRaisesRegex(RuntimeError, "No enabled provider has a usable credential"):
            router.check_configuration(self.workspace.registry_path, self.workspace.auth_path)


class BrowserIsolationTests(unittest.TestCase):
    def test_remote_pages_cannot_start_generations_or_read_health(self) -> None:
        from test_codex_sota_regressions import LocalUpstream, RouterHarness, provider_config, write_auth, write_registry
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(200, {"id": "fixture", "output": []}) as upstream:
            root = Path(temporary)
            write_auth(root / "auth.json")
            write_registry(root / "providers.json", [provider_config(
                "fixture", upstream.url, prefix="fixture--", is_default=True,
            )])
            with RouterHarness(root / "providers.json", root / "auth.json", root / "router.log") as local:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for headers in (
                    {"Origin": "https://untrusted.invalid"},
                    {"Origin": "null"},
                    {"Sec-Fetch-Site": "cross-site"},
                    {"Host": "rebound.invalid"},
                ):
                    body = b'{"model":"fixture--shared-model","input":"hi"}'
                    for method, path, payload, send_body in (
                        ("POST", "/responses", body, True),
                        ("POST", "/responses", body, False),
                        ("GET", "/healthz", b"", True),
                    ):
                        with self.subTest(headers=headers, method=method, send_body=send_body):
                            request_headers = {
                                "Host": f"127.0.0.1:{local.server.server_port}",
                                "Content-Type": "text/plain", "Content-Length": str(len(payload)),
                                "Connection": "close", **headers,
                            }
                            request = (f"{method} {path} HTTP/1.1\r\n" + "".join(
                                f"{name}: {value}\r\n" for name, value in request_headers.items()
                            ) + "\r\n").encode("ascii")
                            # Send the tiny fixture in one write: urllib splits headers/body,
                            # so an immediate denial can race its second send and reset the
                            # socket on Windows. The headers-only case separately proves
                            # denial happens without waiting for or routing the POST body.
                            with socket.create_connection(("127.0.0.1", local.server.server_port), timeout=5) as connection:
                                connection.sendall(request + (payload if send_body else b""))
                                with http.client.HTTPResponse(connection) as response:
                                    response.begin()
                                    self.assertEqual(response.status, 403)
                                    self.assertEqual(json.loads(response.read())["error"]["type"], "cross_origin_request_denied")
                                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                with opener.open(local.url + "/healthz", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                same_origin = urllib.request.Request(local.url + "/healthz", headers={"Origin": local.url})
                with opener.open(same_origin, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            self.assertEqual(upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
