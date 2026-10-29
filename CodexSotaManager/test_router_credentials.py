"""Credential regressions: temporary files, synthetic keys, loopback HTTP only."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test_codex_sota_regressions import (
    RouterHarness, TEST_TOKEN, provider_config, registry, write_auth, write_registry,
)


class CredentialUpstream:
    def __init__(self, invalid_keys=()):
        owner = self
        self.invalid_keys = set(invalid_keys)
        self.force_status = 0
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                authorization = self.headers.get("Authorization", "")
                owner.requests.append(authorization)
                key = authorization.removeprefix("Bearer ")
                status = owner.force_status or (401 if key in owner.invalid_keys else 200)
                data = json.dumps(
                    {"error": {"code": "INVALID_API_KEY"}} if status != 200
                    else {"id": "local-response", "object": "response", "output": []}
                ).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class CredentialRegressionTests(unittest.TestCase):
    @contextmanager
    def workspace(self, *, rejected=True):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            primary = stack.enter_context(CredentialUpstream([TEST_TOKEN] if rejected else []))
            working = stack.enter_context(CredentialUpstream())
            space = registry.Workspace(
                name="credential_test", label="test", root=root, router_port=19997,
                router_starter=root / "unused.ps1", protocol="responses",
            )
            stack.enter_context(mock.patch.dict(registry.WORKSPACES, {space.name: space}))
            first = provider_config(
                "primary", primary.url, prefix="primary--", is_default=True,
                allow_failover=True,
            )
            second = provider_config(
                "working", working.url, prefix="working--", is_default=False,
            )
            second.update(workspace=space.name, auth_type="dpapi",
                          secret_file="working.dpapi", entropy="credential-test")
            write_auth(space.auth_path)
            registry.dpapi_protect("working-test-key", space.secrets_root / "working.dpapi",
                                   "credential-test")
            providers = [first, second]
            write_registry(space.registry_path, providers)
            local = stack.enter_context(RouterHarness(
                space.registry_path, space.auth_path, space.log_path,
            ))
            yield space, local, primary, working, providers

    def post(self, local, model="primary--shared-model"):
        request = urllib.request.Request(
            local.url + "/responses", data=json.dumps({"model": model, "input": "local"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read())

    def health(self, local):
        with urllib.request.urlopen(local.url + "/healthz", timeout=5) as response:
            return json.loads(response.read())

    def replace_auth(self, space, key):
        space.auth_path.write_text(json.dumps({
            "auth_mode": "apikey", "OPENAI_API_KEY": key, "test_note": "rewritten",
        }), encoding="utf-8")

    def test_401_stops_later_upstream_calls_without_failover_or_log_noise(self):
        with self.workspace() as (space, local, primary, working, _providers):
            self.assertEqual(self.post(local)[0], 401)
            for _ in range(6):
                status, body = self.post(local)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["type"], "provider_auth_unavailable")
            self.assertEqual(len(primary.requests), 1)
            self.assertEqual(working.requests, [], "a rejected key must not bill another vendor")
            self.assertEqual(len(space.log_path.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(self.health(local)["credential_errors"], {"primary": "rejected"})
            self.assertEqual(self.post(local, "working--shared-model")[0], 200)

    def test_touching_same_key_stays_blocked_and_new_key_recovers(self):
        with self.workspace() as (space, local, primary, _working, _providers):
            self.assertEqual(self.post(local)[0], 401)
            self.replace_auth(space, TEST_TOKEN)
            self.assertEqual(self.post(local)[0], 400)
            self.assertEqual(len(primary.requests), 1)
            self.replace_auth(space, "replacement-test-key")
            self.assertEqual(self.post(local)[0], 200)
            self.assertEqual(primary.requests[-1], "Bearer replacement-test-key")
            self.assertEqual(self.health(local)["credential_errors"], {})

    def test_removing_then_restoring_same_rejected_key_stays_blocked(self):
        with self.workspace() as (space, local, primary, _working, _providers):
            self.assertEqual(self.post(local)[0], 401)
            space.auth_path.unlink()
            self.assertEqual(self.post(local)[0], 400)
            self.replace_auth(space, TEST_TOKEN)
            self.assertEqual(self.post(local)[0], 400)
            self.assertEqual(len(primary.requests), 1)

    def test_deleted_empty_and_corrupt_auth_stop_old_key_and_recover(self):
        for mode in ("deleted", "empty", "corrupt"):
            with self.subTest(mode=mode), self.workspace(rejected=False) as fixture:
                space, local, primary, _working, _providers = fixture
                self.assertEqual(self.post(local)[0], 200)
                if mode == "deleted":
                    space.auth_path.unlink()
                elif mode == "empty":
                    self.replace_auth(space, "")
                else:
                    space.auth_path.write_text("{ incomplete", encoding="utf-8")
                self.assertEqual(self.post(local)[0], 400)
                self.assertEqual(len(primary.requests), 1, "old cached key was sent after removal")
                self.assertEqual(self.post(local, "working--shared-model")[0], 200)
                self.assertEqual(self.health(local)["credential_errors"], {
                    "primary": "missing_or_unreadable",
                })
                self.replace_auth(space, "restored-test-key")
                self.assertEqual(self.post(local)[0], 200)

    def test_dpapi_deletion_does_not_block_unrelated_routing_edits(self):
        with self.workspace(rejected=False) as (space, local, primary, working, providers):
            self.assertEqual(self.post(local, "working--shared-model")[0], 200)
            (space.secrets_root / "working.dpapi").unlink()
            providers[0]["models"][0]["id"] = "renamed-model"
            write_registry(space.registry_path, providers)
            self.assertEqual(self.post(local, "working--shared-model")[0], 400)
            self.assertEqual(len(working.requests), 1)
            self.assertEqual(self.post(local, "primary--renamed-model")[0], 200)
            self.assertEqual(len(primary.requests), 1)
            self.assertEqual(self.health(local)["config_error"], "")
            registry.dpapi_protect("replacement-dpapi-key", space.secrets_root / "working.dpapi",
                                   "credential-test")
            self.assertEqual(self.post(local, "working--shared-model")[0], 200)
            self.assertEqual(working.requests[-1], "Bearer replacement-dpapi-key")

    def test_bad_registry_retains_routes_but_not_deleted_credential(self):
        with self.workspace(rejected=False) as (space, local, primary, _working, providers):
            before = self.health(local)["registry_hash"]
            self.assertEqual(self.post(local)[0], 200)
            space.registry_path.write_text("{ incomplete", encoding="utf-8")
            space.auth_path.unlink()
            self.assertEqual(self.post(local)[0], 400)
            self.assertEqual(len(primary.requests), 1)
            self.assertEqual(self.post(local, "working--shared-model")[0], 200)
            health = self.health(local)
            self.assertEqual(health["registry_hash"], before)
            self.assertTrue(health["config_error"])
            self.replace_auth(space, "restored-with-bad-registry")
            # Do not pair a new key with potentially obsolete routes from a bad registry.
            self.assertEqual(self.post(local)[0], 400)
            self.assertEqual(len(primary.requests), 1)
            write_registry(space.registry_path, providers)
            self.assertEqual(self.post(local)[0], 200)
            self.assertEqual(primary.requests[-1], "Bearer restored-with-bad-registry")

    def test_unrelated_registry_reload_does_not_revive_rejected_key(self):
        with self.workspace() as (space, local, primary, _working, providers):
            self.assertEqual(self.post(local)[0], 401)
            providers[1]["models"][0]["id"] = "different-model"
            write_registry(space.registry_path, providers)
            self.assertEqual(self.post(local)[0], 400)
            self.assertEqual(len(primary.requests), 1)
            self.assertEqual(self.post(local, "working--different-model")[0], 200)

    def test_late_401_from_old_key_does_not_block_new_key(self):
        with self.workspace(rejected=False) as (space, local, _primary, _working, _providers):
            old = local.state.routing_snapshot()
            self.replace_auth(space, "new-key-before-old-401-arrives")
            local.state.refresh()
            local.state.reject_credential("primary", old.keys["primary"])
            self.assertTrue(local.state.credential_problem("primary", old.keys["primary"]))
            self.assertEqual(self.post(local)[0], 200)
            self.assertEqual(self.health(local)["credential_errors"], {})

    def test_non_401_errors_do_not_quarantine_or_replay_generation(self):
        with self.workspace(rejected=False) as (_space, local, primary, working, _providers):
            for status in (403, 429, 503):
                primary.force_status = status
                before = len(primary.requests)
                self.assertEqual(self.post(local)[0], status)
                self.assertEqual(self.post(local)[0], status)
                self.assertEqual(len(primary.requests), before + 2)
                self.assertEqual(self.health(local)["credential_errors"], {})
            primary.force_status = 0
            self.assertEqual(self.post(local)[0], 200)
            self.assertEqual(working.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
