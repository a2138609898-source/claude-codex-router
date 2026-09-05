from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import gc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock


MANAGER_ROOT = Path(__file__).resolve().parent
CORE_ROOT = MANAGER_ROOT.parent / "CodexHistorySync"
for source_root in (MANAGER_ROOT, CORE_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

import claude_desktop as claude  # noqa: E402
import codex_sota_router as router  # noqa: E402
import sota_registry as registry  # noqa: E402
import CodexSotaManager as manager  # noqa: E402


TEST_TOKEN = "local-regression-token"


def provider_config(
    provider_id: str,
    base_url: str,
    *,
    prefix: str,
    is_default: bool,
    protocols: tuple[str, ...] = ("responses",),
    allow_failover: bool = False,
    model_id: str = "shared-model",
) -> dict[str, object]:
    return {
        "id": provider_id,
        "name": provider_id.replace("_", " ").title(),
        "base_url": base_url,
        "prefix": prefix,
        "enabled": True,
        "protected": False,
        "is_default": is_default,
        "allow_failover": allow_failover,
        "auth_type": "codex_auth",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "models_path": "/models",
        "responses_path": "/responses",
        "messages_path": "/messages",
        "timeout_seconds": 5,
        "protocols": list(protocols),
        "extra_headers": {},
        "models": [{"id": model_id, "enabled": True}],
    }


def write_registry(path: Path, providers: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "providers": providers}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def write_auth(path: Path) -> None:
    path.write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": TEST_TOKEN}) + "\n",
        encoding="utf-8",
    )


class LocalUpstream:
    def __init__(self, status: int, payload: dict[str, object]):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _answer
            do_POST = _answer

        self.requests: list[dict[str, object]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "LocalUpstream":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ScriptedUpstream:
    """An upstream whose answer depends on the path and on how many times it has been asked.

    `LocalUpstream` returns one fixed status for everything, which cannot express either of the
    two behaviours the real gateways have: a count_tokens route that is permanently missing while
    /messages works, and a 5xx that clears on the next attempt.
    """

    def __init__(
        self,
        *,
        count_tokens_status: int = 200,
        messages_failures: int = 0,
        failure_status: int = 503,
    ):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                owner.requests.append({"path": self.path, "body": raw})
                if self.path.endswith("/count_tokens"):
                    status = count_tokens_status
                    payload: dict[str, object] = (
                        {"input_tokens": 4242}
                        if status == 200
                        else {"error": {"message": "404 page not found"}}
                    )
                elif owner.remaining_failures > 0:
                    owner.remaining_failures -= 1
                    status = failure_status
                    payload = {"error": {"message": "no channel for model"}}
                else:
                    status = 200
                    payload = {"id": "msg", "type": "message", "content": []}
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.requests: list[dict[str, object]] = []
        self.remaining_failures = messages_failures
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def paths(self) -> list[str]:
        return [str(entry["path"]) for entry in self.requests]

    def __enter__(self) -> "ScriptedUpstream":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RouterHarness:
    def __init__(self, registry_path: Path, auth_path: Path, log_path: Path):
        self.state = router.RouterState(registry_path, auth_path, log_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), router.SotaRouterHandler)
        self.server.daemon_threads = True
        self.server.router_state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "RouterHarness":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.state.clear_keys()


@contextmanager
def isolated_claude_library(root: Path):
    local = root / "Local"
    roaming = root / "Roaming"
    third_party_root = local / "Claude-3p"
    library = third_party_root / "configLibrary"
    library.mkdir(parents=True)
    patches = (
        mock.patch.object(claude, "CLAUDE_3P_ROOT", third_party_root),
        mock.patch.object(claude, "CONFIG_LIBRARY", library),
        mock.patch.object(claude, "META_PATH", library / "_meta.json"),
        mock.patch.object(claude, "BACKUP_ROOT", root / "backups"),
        mock.patch.object(claude, "LIBRARY_LOCK_PATH", root / "claude-library.lock"),
        mock.patch.object(claude, "SLOT_CLAIM_PATH", root / "claude-slot-claim.json"),
        mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)},
            clear=False,
        ),
    )
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield library, local, roaming


class ClaudeLibraryRegressionTests(unittest.TestCase):
    def test_writing_the_profile_does_not_take_the_shared_applied_slot(self) -> None:
        """appliedId is one global slot shared with cc-switch.

        Publishing used to set it, so launching Claude from cc-switch afterwards silently used
        codex-sota's gateway.  Writing a profile must leave the neighbour's choice alone.
        """
        self.assertIs(
            inspect.signature(claude.write_profile).parameters["apply"].default, False
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (library, _local, _roaming):
                external_id = "79ae43da-be0a-4eb1-bf7c-2aee8fb6db95"
                (library / f"{external_id}.json").write_text("{}", encoding="utf-8")
                claude.META_PATH.write_text(
                    json.dumps(
                        {
                            "appliedId": external_id,
                            "entries": [{"id": external_id, "name": "CC Switch"}],
                        }
                    ),
                    encoding="utf-8",
                )
                result = claude.write_profile(
                    [{"name": "vendor--model", "labelOverride": "Vendor model"}],
                    "http://127.0.0.1:17994",
                    api_key=TEST_TOKEN,
                )
                self.assertFalse(result["applied"])
                self.assertEqual(claude.library_status()["applied_id"], external_id)
                self.assertEqual(claude.read_slot_claim(), {})

    def test_launch_claim_is_handed_back_to_the_displaced_neighbour(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (library, _local, _roaming):
                external_id = "79ae43da-be0a-4eb1-bf7c-2aee8fb6db95"
                (library / f"{external_id}.json").write_text("{}", encoding="utf-8")
                claude.META_PATH.write_text(
                    json.dumps(
                        {
                            "appliedId": external_id,
                            "entries": [{"id": external_id, "name": "CC Switch"}],
                        }
                    ),
                    encoding="utf-8",
                )
                claude.write_profile(
                    [{"name": "vendor--model", "labelOverride": "Vendor model"}],
                    "http://127.0.0.1:17994",
                    api_key=TEST_TOKEN,
                )
                claimed = claude.claim_slot(pids=[11, 12])
                self.assertEqual(claimed["displaced_id"], external_id)
                self.assertEqual(claude.library_status()["applied_id"], claude.SOTA_ENTRY_ID)
                # A Claude still running must keep the profile it started with.
                self.assertEqual(
                    claude.reconcile_slot(claude_running=True)["status"], "claude-running"
                )
                self.assertEqual(claude.library_status()["applied_id"], claude.SOTA_ENTRY_ID)
                self.assertEqual(
                    claude.reconcile_slot(claude_running=False)["status"], "released"
                )
                self.assertEqual(claude.library_status()["applied_id"], external_id)
                self.assertEqual(claude.read_slot_claim(), {})

    def test_legacy_uuid_migration_preserves_every_external_profile_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (library, _local, _roaming):
                external_ids = (
                    "79ae43da-be0a-4eb1-bf7c-2aee8fb6db95",
                    "00000000-0000-4000-8000-000000157210",
                )
                external_bytes = {
                    external_ids[0]: b'{\r\n  "owner": "cc-switch", "keep": 1\r\n}\r\n',
                    external_ids[1]: b'{"owner":"other-tool","opaque":[3,2,1]}',
                }
                for entry_id, data in external_bytes.items():
                    (library / f"{entry_id}.json").write_bytes(data)
                legacy_id = claude.LEGACY_ENTRY_IDS[0]
                legacy_payload = b'{"inferenceProvider":"gateway","custom":"keep-me"}\n'
                (library / f"{legacy_id}.json").write_bytes(legacy_payload)
                external_entries = [
                    {"id": external_ids[0], "name": "CC Switch", "opaque": {"a": 1}},
                    {"id": external_ids[1], "name": "Another Tool", "rank": 7},
                ]
                meta = {
                    "appliedId": legacy_id,
                    "entries": external_entries
                    + [{"id": legacy_id, "name": claude.SOTA_ENTRY_NAME}],
                    "unknownTopLevel": ["must", "stay"],
                }
                claude.META_PATH.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )

                result = claude.migrate_legacy_entry()

                self.assertEqual(result["status"], "migrated")
                self.assertFalse((library / f"{legacy_id}.json").exists())
                self.assertEqual(claude.entry_path(claude.SOTA_ENTRY_ID).read_bytes(), legacy_payload)
                for entry_id, before in external_bytes.items():
                    self.assertEqual((library / f"{entry_id}.json").read_bytes(), before)
                saved = json.loads(claude.META_PATH.read_text(encoding="utf-8"))
                self.assertEqual(saved["appliedId"], claude.SOTA_ENTRY_ID)
                self.assertEqual(saved["unknownTopLevel"], ["must", "stay"])
                self.assertEqual(saved["entries"][:2], external_entries)
                self.assertTrue(all(claude.UUID_RE.fullmatch(item["id"]) for item in saved["entries"]))

    def test_write_profile_failure_restores_index_and_profile_as_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (library, _local, _roaming):
                external_id = "79ae43da-be0a-4eb1-bf7c-2aee8fb6db95"
                external_path = library / f"{external_id}.json"
                external_path.write_bytes(b'{"external":true}\r\n')
                meta_bytes = (
                    json.dumps(
                        {
                            "appliedId": external_id,
                            "entries": [{"id": external_id, "name": "CC Switch"}],
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8")
                claude.META_PATH.write_bytes(meta_bytes)
                original_atomic = claude._atomic_write_json
                calls = 0

                def fail_on_index(path: Path, payload: object) -> None:
                    nonlocal calls
                    calls += 1
                    if path == claude.META_PATH:
                        raise OSError("simulated index failure")
                    original_atomic(path, payload)

                with mock.patch.object(claude, "_atomic_write_json", side_effect=fail_on_index):
                    with self.assertRaisesRegex(OSError, "simulated index failure"):
                        claude.write_profile(
                            [{"name": "vendor--model", "labelOverride": "Vendor model"}],
                            "http://127.0.0.1:17994",
                            api_key=TEST_TOKEN,
                        )

                self.assertGreaterEqual(calls, 2)
                self.assertEqual(claude.META_PATH.read_bytes(), meta_bytes)
                self.assertFalse(claude.entry_path(claude.SOTA_ENTRY_ID).exists())
                self.assertEqual(external_path.read_bytes(), b'{"external":true}\r\n')

    def test_migration_and_profile_update_roll_back_as_one_outer_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (library, _local, _roaming):
                legacy_id = claude.LEGACY_ENTRY_IDS[0]
                legacy_path = library / f"{legacy_id}.json"
                legacy_bytes = b'{"inferenceProvider":"gateway","legacy":true}\n'
                legacy_path.write_bytes(legacy_bytes)
                meta_bytes = (
                    json.dumps(
                        {
                            "appliedId": legacy_id,
                            "entries": [{"id": legacy_id, "name": claude.SOTA_ENTRY_NAME}],
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8")
                claude.META_PATH.write_bytes(meta_bytes)
                original_atomic = claude._atomic_write_json
                meta_writes = 0

                def fail_final_index(path: Path, payload: object) -> None:
                    nonlocal meta_writes
                    if path == claude.META_PATH:
                        meta_writes += 1
                        if meta_writes == 2:
                            raise OSError("simulated final profile index failure")
                    original_atomic(path, payload)

                with mock.patch.object(claude, "_atomic_write_json", side_effect=fail_final_index):
                    with self.assertRaisesRegex(OSError, "simulated final profile index failure"):
                        claude.write_profile(
                            [{"name": "vendor--model", "labelOverride": "Vendor model"}],
                            "http://127.0.0.1:17994",
                            api_key=TEST_TOKEN,
                        )

                self.assertEqual(claude.META_PATH.read_bytes(), meta_bytes)
                self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
                self.assertFalse(claude.entry_path(claude.SOTA_ENTRY_ID).exists())

    def test_deployment_mode_failure_restores_all_existing_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (_library, local, roaming):
                paths = (
                    claude.CLAUDE_3P_ROOT / "claude_desktop_config.json",
                    local / "Claude" / "claude_desktop_config.json",
                    roaming / "Claude" / "claude_desktop_config.json",
                )
                before: dict[Path, bytes] = {}
                for index, path in enumerate(paths):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    data = (json.dumps({"deploymentMode": "1p", "marker": index}) + "\n").encode()
                    path.write_bytes(data)
                    before[path] = data
                original_atomic = claude._atomic_write_json
                calls = 0

                def fail_second(path: Path, payload: object) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("simulated mode failure")
                    original_atomic(path, payload)

                with mock.patch.object(claude, "_atomic_write_json", side_effect=fail_second):
                    with self.assertRaisesRegex(OSError, "simulated mode failure"):
                        claude.ensure_deployment_mode()

                self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_deployment_snapshot_is_taken_after_the_library_lock_is_acquired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (_library, _local, _roaming):
                path = claude.CLAUDE_3P_ROOT / "claude_desktop_config.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'{"deploymentMode":"1p","generation":"before-lock"}\n')
                locked_bytes = b'{"deploymentMode":"1p","generation":"inside-lock"}\n'

                @contextmanager
                def simulated_lock(_timeout_seconds: float = 20.0):
                    path.write_bytes(locked_bytes)
                    yield

                with mock.patch.object(claude, "_library_lock", simulated_lock), mock.patch.object(
                    claude, "_atomic_write_json", side_effect=OSError("simulated write failure")
                ):
                    with self.assertRaisesRegex(OSError, "simulated write failure"):
                        claude.ensure_deployment_mode()

                self.assertEqual(path.read_bytes(), locked_bytes)


class RouterRegressionTests(unittest.TestCase):
    def test_health_alias_and_models_hot_reload_before_answering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(
            200, {"id": "unused"}
        ) as upstream:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_auth(auth_path)
            first = provider_config(
                "first_vendor", upstream.url, prefix="", is_default=True, model_id="old-model"
            )
            write_registry(registry_path, [first])
            with RouterHarness(registry_path, auth_path, root / "router.log") as local_router:
                with urllib.request.urlopen(local_router.url + "/health", timeout=5) as response:
                    health_alias = json.loads(response.read())
                self.assertEqual(health_alias["status"], "ok")
                self.assertEqual(health_alias["upstreams"], ["first_vendor"])

                second = provider_config(
                    "second_vendor_longer",
                    upstream.url,
                    prefix="",
                    is_default=True,
                    model_id="new-model-longer",
                )
                write_registry(registry_path, [second])
                with urllib.request.urlopen(local_router.url + "/healthz", timeout=5) as response:
                    health = json.loads(response.read())
                with urllib.request.urlopen(local_router.url + "/models", timeout=5) as response:
                    models = json.loads(response.read())

                self.assertEqual(health["upstreams"], ["second_vendor_longer"])
                self.assertEqual(health["version"], router.ROUTER_VERSION)
                self.assertEqual([item["id"] for item in models["data"]], ["new-model-longer"])
                self.assertEqual(upstream.requests, [], "local health/model endpoints reached upstream")

    def test_a_published_slug_is_the_only_name_the_router_answers_and_advertises(self) -> None:
        """The list, the single-model lookup and the dispatch table must agree on one name.

        Claude Desktop discovers models from GET /v1/models and decides whether to show a
        thinking slider from the id it finds there, so a model using publish_as has to be
        advertised under the override -- while the upstream still has to receive its own id or
        the vendor 404s. Three separate places compose that slug, and when only some of them
        knew about the override the app was offered a name that then failed to resolve.
        """
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(
            200, {"id": "answered"}
        ) as upstream:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_auth(auth_path)
            provider = provider_config(
                "juno",
                upstream.url,
                prefix="",
                is_default=True,
                protocols=("messages",),
                model_id="claude-opus-5-thinking",
            )
            provider["models"][0]["publish_as"] = "claude-opus-5"
            write_registry(registry_path, [provider])
            with RouterHarness(registry_path, auth_path, root / "router.log") as local_router:
                with urllib.request.urlopen(
                    local_router.url + "/v1/models", timeout=5
                ) as response:
                    listed = json.loads(response.read())
                self.assertEqual([item["id"] for item in listed["data"]], ["claude-opus-5"])

                with urllib.request.urlopen(
                    local_router.url + "/v1/models/claude-opus-5", timeout=5
                ) as response:
                    self.assertEqual(json.loads(response.read())["id"], "claude-opus-5")
                # The vendor id is not a second name for the same model; advertising both
                # would put an id with no thinking record back in front of the app.
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        local_router.url + "/v1/models/claude-opus-5-thinking", timeout=5
                    )
                self.assertEqual(caught.exception.code, 404)

                request = urllib.request.Request(
                    local_router.url + "/v1/messages",
                    data=json.dumps({"model": "claude-opus-5"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            self.assertEqual([entry["path"] for entry in upstream.requests], ["/messages"])
            self.assertEqual(
                json.loads(upstream.requests[0]["body"])["model"], "claude-opus-5-thinking"
            )

    def test_a_refused_reload_keeps_serving_and_says_why_on_healthz(self) -> None:
        """A bad providers.json must not take the router down -- and must not be silent either.

        Keeping the last known-good table is what stops a half-written file from killing
        in-flight requests. But the manager compares registry_hash to decide whether the
        listener is serving this workspace, so a refusal looked exactly like "router not
        running" for a process that was answering every request. /healthz now carries the
        reason, and the manager can tell the two apart.
        """
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(
            200, {"id": "unused"}
        ) as upstream:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_auth(auth_path)
            write_registry(
                registry_path,
                [
                    provider_config(
                        "good_vendor",
                        upstream.url,
                        prefix="",
                        is_default=True,
                        model_id="good-model",
                    )
                ],
            )
            with RouterHarness(registry_path, auth_path, root / "router.log") as local_router:
                with urllib.request.urlopen(local_router.url + "/healthz", timeout=5) as response:
                    before = json.loads(response.read())
                self.assertEqual(before["config_error"], "")
                self.assertEqual(before["config_error_age_seconds"], 0)

                registry_path.write_text("{ this is not json", encoding="utf-8")
                with urllib.request.urlopen(local_router.url + "/healthz", timeout=5) as response:
                    refused = json.loads(response.read())
                self.assertEqual(refused["status"], "ok", "a refused reload is not a dead router")
                self.assertEqual(refused["upstreams"], ["good_vendor"])
                self.assertEqual(refused["registry_hash"], before["registry_hash"])
                self.assertTrue(refused["config_error"], "the refusal was silent again")

                # Requests keep flowing on the old table while the file on disk is broken.
                request = urllib.request.Request(
                    local_router.url + "/v1/responses",
                    data=json.dumps({"model": "good-model"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)

                write_registry(
                    registry_path,
                    [
                        provider_config(
                            "fixed_vendor",
                            upstream.url,
                            prefix="",
                            is_default=True,
                            model_id="fixed-model",
                        )
                    ],
                )
                with urllib.request.urlopen(local_router.url + "/healthz", timeout=5) as response:
                    healed = json.loads(response.read())
                self.assertEqual(healed["upstreams"], ["fixed_vendor"])
                self.assertEqual(
                    healed["config_error"], "", "a fixed file must clear the old complaint"
                )

    def test_responses_failover_skips_messages_only_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(
            503, {"error": "primary unavailable"}
        ) as primary, LocalUpstream(200, {"winner": "messages-only"}) as messages_only, LocalUpstream(
            200, {"winner": "responses"}
        ) as responses:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_auth(auth_path)
            providers = [
                provider_config(
                    "primary_vendor",
                    primary.url,
                    prefix="",
                    is_default=True,
                    protocols=("responses",),
                    allow_failover=True,
                ),
                provider_config(
                    "messages_vendor",
                    messages_only.url,
                    prefix="messages--",
                    is_default=False,
                    protocols=("messages",),
                ),
                provider_config(
                    "responses_vendor",
                    responses.url,
                    prefix="responses--",
                    is_default=False,
                    protocols=("responses",),
                ),
            ]
            write_registry(registry_path, providers)
            with RouterHarness(registry_path, auth_path, root / "router.log") as local_router:
                request = urllib.request.Request(
                    local_router.url + "/v1/responses",
                    data=json.dumps({"model": "shared-model", "input": "local"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read())

            self.assertEqual(payload["winner"], "responses")
            self.assertEqual(len(primary.requests), 1)
            self.assertEqual(messages_only.requests, [])
            self.assertEqual(len(responses.requests), 1)

    def test_upstream_read_timeout_is_recorded_as_gateway_failure_not_client_abort(self) -> None:
        class TimedOutUpstream:
            headers: dict[str, str] = {}

            def __enter__(self) -> "TimedOutUpstream":
                return self

            def __exit__(self, *_args: object) -> None:
                return

            def read(self, _size: int) -> bytes:
                raise TimeoutError("upstream stalled")

        recorded: list[tuple[object, ...]] = []
        keywords: list[dict[str, object]] = []
        handler = object.__new__(router.SotaRouterHandler)
        handler.command = "POST"
        handler.path = "/v1/responses"
        handler.wfile = BytesIO()
        handler.close_connection = False
        handler.server = SimpleNamespace(
            router_state=SimpleNamespace(
                record=lambda *args, **kwargs: (
                    recorded.append(args), keywords.append(kwargs)
                )
            )
        )
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()

        handler._relay(TimedOutUpstream(), "test_vendor", 200, 0.0, "gpt-5")

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][3], 502)
        self.assertIn("Timeout", str(recorded[0][5]))
        # A stream that died before any usage arrived logs the model and no token counts.
        self.assertEqual(keywords[0], {"model": "gpt-5", "usage": {}})

    def _starter_workspace(self, root: Path) -> "registry.Workspace":
        return registry.Workspace(
            name="test",
            label="test",
            root=root,
            router_port=19999,
            router_starter=root / "starter.ps1",
            protocol="responses",
        )

    def test_restart_router_leaves_a_healthy_router_alone_unless_force_is_asked_for(self) -> None:
        """The default path may not kill anything -- save-and-apply and both launch buttons use it.

        The starter is an ensure-running script: it answers `started: false` when /healthz already
        matches, and every caller but the manual button depends on that, because a router that is
        already correct is serving requests that must not be dropped.  Passing `-Stop` here would
        turn every save into an outage for whatever is mid-turn.
        """
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._starter_workspace(Path(temporary))
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(list(command))
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"status": "ready", "started": False}),
                    stderr="",
                )

            with mock.patch.object(registry.subprocess, "run", side_effect=fake_run):
                result = registry.restart_router(workspace)

            self.assertEqual(len(calls), 1, "the default path ran more than the starter")
            self.assertNotIn("-Stop", calls[0])
            self.assertEqual(result, {"status": "ready", "started": False})
            # No force means no claim about what was replaced, so nothing invents an empty one.
            self.assertNotIn("stopped_process_ids", result)

    def test_forced_restart_stops_first_because_healthz_cannot_see_a_code_change(self) -> None:
        """Editing codex_sota_router.py changes neither ROUTER_VERSION nor the registry hash.

        So the starter's health probe still matches and it declines to start anything.  Without a
        stop the manual restart button would report success while the old code kept serving --
        which is exactly the case the button exists for.
        """
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._starter_workspace(Path(temporary))
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(list(command))
                if "-Stop" in command:
                    payload = {"status": "stopped", "stopped_process_ids": [4242]}
                else:
                    payload = {"status": "ready", "started": True}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            with mock.patch.object(registry.subprocess, "run", side_effect=fake_run):
                result = registry.restart_router(workspace, force=True)

            self.assertEqual(len(calls), 2)
            self.assertIn("-Stop", calls[0], "the stop has to come first, not after")
            self.assertNotIn("-Stop", calls[1])
            self.assertEqual(result["started"], True)
            self.assertEqual(result["stopped_process_ids"], [4242])

    def test_forced_restart_refuses_when_the_port_belongs_to_something_else(self) -> None:
        """A blocked stop must not be followed by a start that then fails for a vaguer reason."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._starter_workspace(Path(temporary))
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(list(command))
                return SimpleNamespace(
                    returncode=2,
                    stdout=json.dumps({"status": "blocked", "unrelated_listeners": [777]}),
                    stderr="",
                )

            with mock.patch.object(registry.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(RuntimeError):
                    registry.restart_router(workspace, force=True)

            self.assertEqual(len(calls), 1, "it started a router after the stop was refused")

    def test_count_tokens_stops_being_forwarded_once_a_gateway_has_404ed_it(self) -> None:
        """2505 of this install's logged requests were 404s for exactly this route.

        Claude Desktop asks for a token count on nearly every edit to the pending turn, and
        almost none of these Anthropic-shaped gateways implement the endpoint. Each refusal cost
        a full round trip and returned no number, so the client got nothing usable either.
        """
        with tempfile.TemporaryDirectory() as temporary, ScriptedUpstream(
            count_tokens_status=404
        ) as upstream:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_auth(auth_path)
            write_registry(
                registry_path,
                [
                    provider_config(
                        "counting_vendor",
                        upstream.url,
                        prefix="",
                        is_default=True,
                        protocols=("messages",),
                        model_id="claude-opus-5",
                    )
                ],
            )
            with RouterHarness(registry_path, auth_path, root / "router.log") as local:
                body = json.dumps(
                    {
                        "model": "claude-opus-5",
                        "messages": [{"role": "user", "content": "hello there"}],
                    }
                ).encode("utf-8")

                def count() -> dict[str, object]:
                    request = urllib.request.Request(
                        local.url + "/v1/messages/count_tokens",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                        return json.loads(response.read())

                first = count()
                self.assertIsInstance(first.get("input_tokens"), int)
                self.assertGreater(first["input_tokens"], 0)
                self.assertEqual(
                    upstream.paths(),
                    ["/messages/count_tokens"],
                    "the first call must still ask, so a gateway that does implement it wins",
                )

                upstream.requests.clear()
                for _ in range(4):
                    self.assertGreater(count()["input_tokens"], 0)
                self.assertEqual(
                    upstream.paths(), [], "it kept asking a gateway that already said no"
                )

    def test_a_fast_transient_5xx_is_retried_on_the_same_vendor_and_only_that_one(self) -> None:
        """A single hiccup used to surface as "the provider rejected a test request".

        With `allow_failover: false` there is exactly one candidate, so `is_last` was true on the
        very first attempt and nothing was ever retried. Retrying in place is safe -- no byte has
        reached the client yet -- and it must not become failover: the second vendor here exists
        only to fail the test if the retry wanders off to another account.
        """
        with tempfile.TemporaryDirectory() as temporary, ScriptedUpstream(
            messages_failures=1
        ) as flaky, ScriptedUpstream() as untouched:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_auth(auth_path)
            write_registry(
                registry_path,
                [
                    provider_config(
                        "flaky_vendor",
                        flaky.url,
                        prefix="",
                        is_default=True,
                        protocols=("messages",),
                        model_id="claude-opus-5",
                    ),
                    provider_config(
                        "other_vendor",
                        untouched.url,
                        prefix="other--",
                        is_default=False,
                        protocols=("messages",),
                        model_id="claude-opus-5",
                    ),
                ],
            )
            with RouterHarness(registry_path, auth_path, root / "router.log") as local:
                request = urllib.request.Request(
                    local.url + "/v1/messages",
                    data=json.dumps(
                        {
                            "model": "claude-opus-5",
                            "max_tokens": 8,
                            "messages": [{"role": "user", "content": "hi"}],
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(len(flaky.requests), 2, "the 503 was not retried in place")
                self.assertEqual(
                    untouched.requests, [], "the retry leaked to a second vendor's account"
                )

    def test_retry_and_estimate_limits_are_the_ones_that_make_them_safe(self) -> None:
        """These four numbers are the whole safety argument; a later tweak must trip this.

        Retrying anything below 500 would resend a request the gateway understood and refused,
        and retrying a request that already burned the provider timeout is what turns a busy
        upstream into an overloaded one -- the exact failure mode that made a working profile
        report itself broken.
        """
        self.assertTrue(
            all(status >= 500 for status in router.SAME_VENDOR_RETRY_STATUSES),
            f"a sub-500 status is being retried: {sorted(router.SAME_VENDOR_RETRY_STATUSES)}",
        )
        self.assertNotIn(429, router.SAME_VENDOR_RETRY_STATUSES)
        self.assertLessEqual(len(router.SAME_VENDOR_RETRY_BACKOFF), 2)
        self.assertLess(sum(router.SAME_VENDOR_RETRY_BACKOFF), 2.0)
        self.assertLess(router.SAME_VENDOR_RETRY_MAX_ELAPSED, 120.0)
        self.assertEqual(
            router.COUNT_TOKENS_PATHS,
            frozenset(
                path
                for path, route in router.INFERENCE_PATHS.items()
                if route[1] == "/count_tokens"
            ),
            "a count_tokens alias exists that the local-answer path does not recognise",
        )

    def test_the_token_estimate_separates_cjk_from_latin_and_skips_signatures(self) -> None:
        """The estimate replaces a 404, so it only has to be the right size, not exact.

        Two things would make it the wrong size. Charging CJK at the Latin ratio under-counts a
        Chinese conversation roughly threefold, and counting a thinking block's base64 signature
        as prose inflates it by thousands -- which would compact a conversation that is nowhere
        near full.
        """
        latin = router.estimate_input_tokens(
            {"messages": [{"role": "user", "content": "word " * 200}]}
        )
        han = router.estimate_input_tokens(
            {"messages": [{"role": "user", "content": "中" * 400}]}
        )
        self.assertLess(200, latin)
        self.assertLess(latin, 400)
        self.assertLess(350, han)
        self.assertLess(han, 500)

        plain = router.estimate_input_tokens(
            {"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "hi"}]}]}
        )
        signed = router.estimate_input_tokens(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "hi", "signature": "Z" * 4000}
                        ],
                    }
                ]
            }
        )
        self.assertEqual(plain, signed, "the opaque signature is being counted as prose")
        self.assertGreaterEqual(router.estimate_input_tokens({}), 1)


class HeaderValidationTests(unittest.TestCase):
    def test_request_header_fields_reject_control_characters(self) -> None:
        base = provider_config(
            "header_vendor", "https://example.invalid/v1", prefix="", is_default=True
        )
        mutations = (
            ("auth_header", "Authorization\r\nX-Injected"),
            ("auth_prefix", "Bearer token\nX-Injected: yes"),
            ("extra_header_name", "X-Test\r\nX-Injected"),
            ("extra_header_value", "ok\x00bad"),
        )
        for label, value in mutations:
            with self.subTest(label=label):
                item = deepcopy(base)
                if label == "extra_header_name":
                    item["extra_headers"] = {value: "value"}
                elif label == "extra_header_value":
                    item["extra_headers"] = {"X-Test": value}
                else:
                    item[label] = value
                with self.assertRaises(ValueError):
                    registry.validate_provider(item, allow_missing_secret=True)


class RegistryRecoveryRegressionTests(unittest.TestCase):
    def test_redacted_export_masks_every_extra_header_value(self) -> None:
        provider = provider_config(
            "header_vendor", "https://example.invalid/v1", prefix="", is_default=True
        )
        provider["extra_headers"] = {
            "X-Api-Key": "secondary-secret",
            "Cookie": "session=private",
        }

        redacted = registry.redacted_registry({"version": 1, "providers": [provider]})

        exported = redacted["providers"][0]
        self.assertEqual(
            exported["extra_headers"],
            {"X-Api-Key": "<redacted>", "Cookie": "<redacted>"},
        )
        self.assertNotIn("entropy", exported)

    def test_a_borrowed_login_provider_cannot_carry_a_junk_model_prefix(self) -> None:
        """codex_auth prefixes were the one shape validate_provider never checked.

        A dpapi entry either derives its prefix or has it pattern-checked, but a codex_auth
        entry -- which borrows the Codex App's own login and so is only ever hand-written or
        restored from a file -- took whatever string it was given. That string is not
        decoration: `prefix + model id` is the slug written into the generated catalog and
        matched by the router, so a space in it produces a model the app lists and cannot
        select, with nothing anywhere saying why.
        """
        provider = provider_config(
            "borrowed_login", "https://example.invalid/v1", prefix="", is_default=True
        )
        provider["auth_type"] = "codex_auth"

        # The default's empty prefix stays legal -- that is the live registry's own shape.
        self.assertEqual(
            registry.validate_provider(deepcopy(provider), allow_missing_secret=True)["prefix"],
            "",
        )
        sibling = deepcopy(provider) | {"is_default": False, "prefix": "borrowed-login--"}
        self.assertEqual(
            registry.validate_provider(sibling, allow_missing_secret=True)["prefix"],
            "borrowed-login--",
        )
        for junk in ("borrowed login--", "Borrowed--", "borrowed_login", "--", "x--\r\n"):
            with self.subTest(prefix=junk):
                item = deepcopy(provider) | {"is_default": False, "prefix": junk}
                with self.assertRaises(ValueError):
                    registry.validate_provider(item, allow_missing_secret=True)

    def test_publish_as_reaches_claudes_thinking_table_without_renaming_the_upstream(self) -> None:
        """A vendor id that bakes the tier in cost the user the thinking slider entirely.

        Claude Desktop renders a thinking control only for a model whose canonicalized id is in
        a table compiled into the app. Its canonicalizer strips `<vendor>.anthropic.`, so the
        prefix migration fixes most slugs -- but `claude-opus-5-thinking` reduces to itself and
        misses the table however it is prefixed, which is why the user's own default model had
        no slider while every other model had one. `publish_as` decouples the slug the app
        selects by from the id the upstream is sent, and only the first may change.
        """
        self.assertIsNone(claude.thinking_effort_levels("claude-opus-5-thinking"))
        self.assertTrue(claude.thinking_effort_levels("claude-opus-5"))

        default = provider_config(
            "juno",
            "https://a.invalid/v1",
            prefix="",
            is_default=True,
            protocols=("messages",),
            model_id="claude-opus-5-thinking",
        )
        default["models"][0]["publish_as"] = "claude-opus-5"
        reg = registry.validate_registry(
            {"version": registry.REGISTRY_VERSION, "providers": [default]},
            allow_missing_secrets=True,
        )

        self.assertEqual(registry.selectable_slugs(reg), ["claude-opus-5"])
        self.assertEqual(
            registry.failover_chain(reg, "claude-opus-5", protocol="messages"),
            [("juno", "claude-opus-5-thinking")],
        )
        # One model, one slug: the vendor id is no longer selectable, so nothing can arrive
        # under a name the app has no capability record for.
        self.assertEqual(
            registry.failover_chain(reg, "claude-opus-5-thinking", protocol="messages"), []
        )
        # The picker must still say which model it really is; only `name` is renamed.  The 1M
        # claim rides on the same renamed slug for the same reason the slider does: it is looked
        # up through the canonicalizer, and `claude-opus-5-thinking` reduces to itself and is in
        # no table, so without `publish_as` this model would lose the 1M variant too.
        self.assertEqual(
            claude.build_inference_models(reg),
            [
                {
                    "name": "claude-opus-5",
                    "labelOverride": "Juno · claude-opus-5-thinking",
                    "supports1m": True,
                }
            ],
        )

    def test_publish_as_is_refused_wherever_a_rename_would_do_damage(self) -> None:
        """The override may only ever rename a Claude-side slug, and only to a free name.

        A responses slug is pinned in two places this code does not own -- config.toml's
        `model = ...` and the generated catalog -- so renaming one would silently unpoint the
        Codex App at a model that no longer answers. Refusing the field on any provider that
        speaks responses is what keeps the two apps independent structurally, rather than by
        the user remembering not to set it. Collisions matter for a subtler reason: both of the
        user's overridden models still have a disabled `claude-opus-5` sibling, so switching
        one on would claim a slug the override already owns, and the message has to name both
        claimants or there is no way to tell which side to change.
        """
        dual = provider_config(
            "provider_d",
            "https://b.invalid/v1",
            prefix="provider_d--",
            is_default=False,
            protocols=("responses", "messages"),
            model_id="claude-opus-5-thinking",
        )
        dual["models"][0]["publish_as"] = "claude-opus-5"
        with self.assertRaises(ValueError) as caught:
            registry.validate_provider(deepcopy(dual), allow_missing_secret=True)
        self.assertIn("it also speaks responses", str(caught.exception))

        for bad in ("has space", "slash/name", "semi;colon", "x" * 161):
            with self.subTest(publish_as=bad):
                messages_only = provider_config(
                    "kilo",
                    "https://c.invalid/v1",
                    prefix="kilo.anthropic.",
                    is_default=False,
                    protocols=("messages",),
                    model_id="claude-opus-5-thinking",
                )
                messages_only["models"][0]["publish_as"] = bad
                with self.assertRaises(ValueError):
                    registry.validate_provider(messages_only, allow_missing_secret=True)

        colliding = provider_config(
            "juno",
            "https://a.invalid/v1",
            prefix="",
            is_default=True,
            protocols=("messages",),
            model_id="claude-opus-5-thinking",
        )
        colliding["models"][0]["publish_as"] = "claude-opus-5"
        colliding["models"].append({"id": "claude-opus-5", "enabled": True})
        with self.assertRaises(ValueError) as caught:
            registry.validate_registry(
                {"version": registry.REGISTRY_VERSION, "providers": [colliding]},
                allow_missing_secrets=True,
            )
        message = str(caught.exception)
        # Pinned whole, because the useful part is precisely that both sides are named: a bare
        # "duplicate slug" leaves the user hunting for which of two identical-looking models
        # to change. `(publish_as)` marks the one whose name is an override.
        self.assertEqual(
            message,
            "Duplicate selectable model slug: claude-opus-5 (claimed by "
            "juno/claude-opus-5-thinking (publish_as) and juno/claude-opus-5)",
        )

    def test_absolute_inference_endpoints_are_the_only_repair_candidates(self) -> None:
        provider = provider_config(
            "absolute_vendor", "https://example.invalid/v1", prefix="", is_default=True
        )
        provider["responses_path"] = "https://responses.example.invalid/custom"
        provider["messages_path"] = "https://messages.example.invalid/custom"

        self.assertEqual(
            registry.candidate_responses_paths(provider),
            ["https://responses.example.invalid/custom"],
        )
        self.assertEqual(
            registry.candidate_messages_paths(provider),
            ["https://messages.example.invalid/custom"],
        )
        self.assertEqual(
            registry.endpoint_url(provider, "responses"),
            "https://responses.example.invalid/custom",
        )
        self.assertEqual(
            registry.endpoint_url(provider, "messages"),
            "https://messages.example.invalid/custom",
        )

    def test_missing_dpapi_secret_can_be_loaded_and_linted_but_not_strictly_used(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="test",
                label="test",
                root=root,
                router_port=19997,
                router_starter=root / "starter.ps1",
                protocol="responses",
            )
            provider = provider_config(
                "missing_secret", "https://example.invalid/v1", prefix="", is_default=True
            )
            provider.update(
                {
                    "workspace": "test",
                    "auth_type": "dpapi",
                    "secret_file": "missing-secret.dpapi",
                    "entropy": "test-entropy",
                }
            )
            write_registry(workspace.registry_path, [provider])

            with mock.patch.dict(registry.WORKSPACES, {"test": workspace}, clear=False):
                recovered = registry.load_registry(
                    workspace.registry_path, allow_missing_secrets=True
                )
                findings = registry.lint_registry(recovered, workspace)
                with self.assertRaises(FileNotFoundError):
                    registry.load_registry(workspace.registry_path)

            self.assertEqual(recovered["providers"][0]["id"], "missing_secret")
            self.assertTrue(
                any(
                    finding["level"] == "error"
                    and finding["provider"] == "missing_secret"
                    and "API Key" in finding["message"]
                    for finding in findings
                )
            )

    def test_deleting_the_only_provider_removes_catalog_and_stops_router(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="test",
                label="test",
                root=root,
                router_port=19996,
                router_starter=root / "starter.ps1",
                protocol="responses",
            )
            provider = provider_config(
                "only_vendor", "https://example.invalid/v1", prefix="", is_default=True
            )
            provider["workspace"] = "test"
            write_registry(workspace.registry_path, [provider])
            workspace.catalog_path.write_text('{"models":[{"slug":"stale"}]}', encoding="utf-8")

            with mock.patch.dict(registry.WORKSPACES, {"test": workspace}, clear=False), \
                mock.patch.object(registry, "stop_router", return_value={"status": "stopped"}) as stop, \
                mock.patch.object(registry, "restart_router") as restart:
                result = registry.delete_provider("only_vendor", workspace)

            saved = json.loads(workspace.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["providers"], [])
            self.assertFalse(workspace.catalog_path.exists())
            self.assertEqual(result["catalog"]["status"], "empty")
            stop.assert_called_once_with(workspace)
            restart.assert_not_called()

    def protected_workspace(self, temporary: str, port: int = 19995):
        """A workspace whose single default provider is protected, written to disk."""
        root = Path(temporary)
        workspace = registry.Workspace(
            name="test",
            label="test",
            root=root,
            router_starter=root / "starter.ps1",
            router_port=port,
            protocol="responses",
        )
        provider = provider_config(
            "borrowed_login", "https://example.invalid/v1", prefix="", is_default=True
        )
        provider.update({"workspace": "test", "protected": True, "auth_type": "codex_auth"})
        write_registry(workspace.registry_path, [provider])
        return workspace, provider

    def test_a_protected_provider_accepts_model_edits_but_pins_its_identity(self) -> None:
        """Refusing every write to a protected provider left its model list unreachable.

        `protected` exists so the codex_auth entry's borrowed identity -- the Codex App's own
        login, base URL and model prefix -- cannot be rewritten from the manager.  But the guard
        was a blanket refusal, and on the Codex side the protected entry is also the *default*
        provider, so the models a user most needs to switch on were the ones they could never
        touch: the editor was fully disabled and save_provider rejected the write anyway.  Now the
        identity fields are pinned back to whatever is on disk and everything else goes through.
        """
        with tempfile.TemporaryDirectory() as temporary:
            workspace, on_disk = self.protected_workspace(temporary)
            candidate = deepcopy(on_disk)
            candidate["models"] = [
                {"id": "shared-model", "enabled": True},
                {"id": "newly-discovered", "enabled": True},
            ]
            candidate["name"] = "Renamed"
            candidate["timeout_seconds"] = 42
            # Every one of these is an identity field and must not survive the write.
            candidate["base_url"] = "https://hijacked.invalid/v1"
            candidate["auth_header"] = "X-Hijack"
            candidate["auth_prefix"] = "Token "
            candidate["models_path"] = "/hijacked-models"
            candidate["responses_path"] = "/hijacked-responses"
            candidate["messages_path"] = "/hijacked-messages"
            candidate["protocols"] = ["responses", "messages"]

            # The catalog rebuild wants a source template this fixture has no reason to carry; what
            # is under test is which fields reach providers.json.
            with mock.patch.dict(registry.WORKSPACES, {"test": workspace}, clear=False), \
                mock.patch.object(
                    registry, "rebuild_catalog", return_value={"status": "ok"}
                ):
                result = registry.apply_provider(
                    candidate, api_key=None, workspace=workspace, restart=False
                )

            self.assertEqual(result["status"], "ready")
            saved = json.loads(workspace.registry_path.read_text(encoding="utf-8"))
            written = saved["providers"][0]
            self.assertEqual(
                [model["id"] for model in written["models"]],
                ["shared-model", "newly-discovered"],
            )
            self.assertEqual(written["name"], "Renamed")
            self.assertEqual(written["timeout_seconds"], 42)
            self.assertTrue(written["protected"])
            for key in registry.PROTECTED_PINNED_PROVIDER_KEYS:
                with self.subTest(pinned=key):
                    self.assertEqual(written[key], on_disk[key])

    def test_a_protected_provider_still_cannot_be_deleted(self) -> None:
        """Editing one is now allowed; removing one is still not.

        The entry is recreated from the Codex App's own login, so a delete is never what someone
        means by it -- and unlike a field edit there is nothing to pin it back to.
        """
        with tempfile.TemporaryDirectory() as temporary:
            workspace, _on_disk = self.protected_workspace(temporary, port=19994)
            before = workspace.registry_path.read_text(encoding="utf-8")

            with mock.patch.dict(registry.WORKSPACES, {"test": workspace}, clear=False), \
                mock.patch.object(registry, "stop_router") as stop, \
                mock.patch.object(registry, "restart_router") as restart:
                with self.assertRaises(ValueError):
                    registry.delete_provider("borrowed_login", workspace)

            self.assertEqual(workspace.registry_path.read_text(encoding="utf-8"), before)
            stop.assert_not_called()
            restart.assert_not_called()


class Variable:
    def __init__(self, value: object = ""):
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class WidgetStub:
    def __init__(self) -> None:
        self.value = "{}"

    def configure(self, **_kwargs: object) -> None:
        return

    def selection(self) -> tuple[()]:
        return ()

    def selection_remove(self, *_args: object) -> None:
        return

    def delete(self, *_args: object) -> None:
        self.value = ""

    def insert(self, _index: object, value: str) -> None:
        self.value = value

    def get(self, *_args: object) -> str:
        return self.value

    def focus_set(self) -> None:
        return

    def yview_moveto(self, _value: float) -> None:
        return

    def select(self, _value: object) -> None:
        return


def new_provider_form(workspace: registry.Workspace) -> SimpleNamespace:
    form = SimpleNamespace(
        _busy=False,
        current_id="previous",
        workspace=workspace,
        registry={"version": 1, "providers": []},
        provider_tree=WidgetStub(),
        name_var=Variable(),
        id_var=Variable(),
        base_url_var=Variable(),
        prefix_var=Variable(),
        api_key_var=Variable(),
        models_path_var=Variable(),
        responses_path_var=Variable(),
        messages_path_var=Variable(),
        auth_header_var=Variable(),
        auth_prefix_var=Variable(),
        timeout_var=Variable(),
        enabled_var=Variable(),
        failover_var=Variable(),
        proto_responses_var=Variable(),
        proto_messages_var=Variable(),
        headers_text=WidgetStub(),
        draft_models=[],
        id_entry=WidgetStub(),
        config_canvas=WidgetStub(),
        name_entry=WidgetStub(),
        status_var=Variable(),
        notebook=WidgetStub(),
        config_tab=object(),
    )
    form._set_key_revealed = lambda _revealed: None
    form._render_models = lambda: None
    form._set_editor_protected = lambda _protected: None
    # Saving now re-reads the provider off disk instead of trusting the editor's snapshot, so
    # bind the real helpers rather than stubbing them out -- a stub here would hide the very
    # merge behaviour these tests exist to pin down.
    form.loaded_provider = None
    form._disk_provider = lambda provider_id: manager.CodexSotaApp._disk_provider(
        form, provider_id
    )
    form._outside_edits = lambda provider_id: manager.CodexSotaApp._outside_edits(
        form, provider_id
    )
    return form


class ManagerRegressionTests(unittest.TestCase):
    def test_claude_endpoint_repair_uses_messages_not_responses(self) -> None:
        provider = provider_config(
            "claude_gateway",
            "https://example.invalid/v1",
            prefix="",
            is_default=True,
            protocols=("responses", "messages"),
        )
        provider["workspace"] = "claude"
        expected = {
            "changed": True,
            "before": "/wrong",
            "after": "/v1/messages",
            "reason": "fixed",
        }
        with mock.patch.object(
            manager, "auto_repair_messages_path", return_value=deepcopy(expected)
        ) as messages, mock.patch.object(
            manager, "auto_repair_responses_path"
        ) as responses:
            result = manager.auto_repair_active_inference_path(provider, TEST_TOKEN)

        messages.assert_called_once_with(provider, TEST_TOKEN)
        responses.assert_not_called()
        self.assertEqual(result["protocol"], "messages")

    def test_claude_close_waits_for_every_signalled_desktop_process(self) -> None:
        with mock.patch.object(
            manager, "claude_pids", side_effect=[{101, 202}, {202}, set()]
        ), mock.patch.object(
            manager, "close_visible_process_windows", return_value={101, 202}
        ), mock.patch.object(manager.time, "sleep"):
            result = manager.close_running_claude(timeout_seconds=2)

        self.assertEqual(result["requested"], [101, 202])
        self.assertEqual(result["closed"], [101, 202])

    def test_claude_close_refuses_to_launch_over_a_stuck_old_instance(self) -> None:
        with mock.patch.object(manager, "claude_pids", return_value={101}), mock.patch.object(
            manager, "close_visible_process_windows", return_value={101}
        ):
            with self.assertRaisesRegex(RuntimeError, "退出"):
                manager.close_running_claude(timeout_seconds=0)

    def test_new_provider_defaults_to_the_selected_workspace_protocol(self) -> None:
        claude_form = new_provider_form(registry.CLAUDE)
        manager.CodexSotaApp._new_provider(claude_form)
        self.assertFalse(claude_form.proto_responses_var.get())
        self.assertTrue(claude_form.proto_messages_var.get())
        self.assertEqual(claude_form.messages_path_var.get(), "/v1/messages")

        codex_form = new_provider_form(registry.CODEX)
        manager.CodexSotaApp._new_provider(codex_form)
        self.assertTrue(codex_form.proto_responses_var.get())
        self.assertFalse(codex_form.proto_messages_var.get())

    def test_form_round_trip_keeps_messages_path(self) -> None:
        form = new_provider_form(registry.CLAUDE)
        form.current_id = None
        form.name_var.set("Claude Gateway")
        form.id_var.set("claude_gateway")
        form.base_url_var.set("https://example.invalid/v1")
        form.prefix_var.set("ignored--")
        form.models_path_var.set("/v1/models")
        form.responses_path_var.set("/v1/responses")
        form.messages_path_var.set("/v1/messages-custom")
        form.auth_header_var.set("x-api-key")
        form.auth_prefix_var.set("")
        form.timeout_var.set("120")
        form.enabled_var.set(True)
        form.failover_var.set(False)
        form.proto_responses_var.set(False)
        form.proto_messages_var.set(True)
        form.draft_models = [{"id": "claude-model", "enabled": True}]
        form._enabled_default_exists = lambda: False

        saved = manager.CodexSotaApp._provider_from_form(form)

        self.assertEqual(saved["workspace"], "claude")
        self.assertEqual(saved["protocols"], ["messages"])
        self.assertEqual(saved["messages_path"], "/v1/messages-custom")

    def test_save_merges_onto_disk_instead_of_the_editors_stale_snapshot(self) -> None:
        """A save must not revert what changed on disk while the editor sat open.

        This is the "自动换家 turns itself back off" report: the candidate used to be built by
        merging the form onto the in-memory registry snapshot, so any provider key changed
        elsewhere -- by a second manager window, a hand edit, or a newer version of this app --
        was quietly written back to its stale value.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="codex",
                label="Codex CLI",
                root=root / "workspace",
                router_port=19997,
                router_starter=root / "starter.ps1",
                protocol="responses",
                needs_catalog=False,
            )
            provider = provider_config(
                "vendor_a", "https://example.invalid/v1", prefix="", is_default=True
            )
            write_registry(workspace.registry_path, [provider])
            loaded = registry.load_registry(workspace.registry_path, allow_missing_secrets=True)

            form = new_provider_form(workspace)
            form.current_id = "vendor_a"
            form.registry = loaded
            form.loaded_provider = deepcopy(registry.find_provider(loaded, "vendor_a"))
            form.name_var.set("Vendor A")
            form.id_var.set("vendor_a")
            form.base_url_var.set("https://example.invalid/v1")
            form.prefix_var.set("")
            form.models_path_var.set("/models")
            form.responses_path_var.set("/responses")
            form.messages_path_var.set("/messages")
            form.auth_header_var.set("Authorization")
            form.auth_prefix_var.set("Bearer ")
            form.timeout_var.set("5")
            form.enabled_var.set(True)
            form.failover_var.set(False)
            form.proto_responses_var.set(True)
            form.proto_messages_var.set(False)
            form.draft_models = [{"id": "shared-model", "enabled": True}]
            form._enabled_default_exists = lambda: True

            # Nothing has moved yet, so an untouched editor must not accuse anyone.
            self.assertEqual(form._outside_edits("vendor_a"), [])

            # Now somebody else edits the same provider on disk: one field this editor owns,
            # one key this version of the editor has never heard of, and one probe result the
            # app itself writes.
            outside = deepcopy(provider)
            outside["allow_failover"] = True
            outside["rate_limit_rpm"] = 60
            outside["models"] = [
                {"id": "shared-model", "enabled": True, "last_test_status": "ready"}
            ]
            write_registry(workspace.registry_path, [outside])

            drifted = form._outside_edits("vendor_a")
            saved = manager.CodexSotaApp._provider_from_form(form)

            # The form owns allow_failover, so the save still writes what the user sees -- but
            # only after _save_current has shown this list and had the overwrite confirmed.
            self.assertEqual(drifted, ["自动换家"])
            self.assertIs(saved["allow_failover"], False)
            # The key the editor does not own is carried across from disk, not dropped.
            self.assertEqual(saved["rate_limit_rpm"], 60)
            # Probe bookkeeping is written by this app between load and save; counting it as
            # someone else's edit would put a warning in front of every ordinary save.
            self.assertNotIn("模型清单", drifted)

    def test_a_protected_editor_locks_identity_but_not_the_model_list(self) -> None:
        """The protected editor used to be disabled wholesale, model list and save button included.

        On the Codex side the protected entry is the default provider, so that made the one model
        list the user most needs to curate permanently read-only -- a freshly discovered model
        could be listed but never switched on, and 保存并应用 stayed greyed out because no edit
        could be made in the first place. Only the borrowed identity needs locking.
        """

        class Widget:
            def __init__(self) -> None:
                self.state_value: object = None
                self.tree_state: object = None

            def configure(self, **kwargs: object) -> None:
                if "state" in kwargs:
                    self.state_value = kwargs["state"]

            def state(self, spec: object = None) -> tuple[()]:
                if spec is not None:
                    self.tree_state = spec
                return ()

        locked = (
            "id_entry",
            "base_url_entry",
            "prefix_entry",
            "api_key_entry",
            "models_path_entry",
            "responses_path_entry",
            "messages_path_entry",
            "auth_header_entry",
            "auth_prefix_entry",
            "proto_responses_check",
            "proto_messages_check",
            "show_key_button",
            "delete_button",
        )
        editable = (
            "name_entry",
            "timeout_spin",
            "enabled_check",
            "failover_check",
            "headers_text",
            "add_model_button",
            "select_models_button",
            "clear_models_button",
            "remove_models_button",
            "save_button",
        )
        probes = (
            "fetch_button",
            "connection_button",
            "test_models_button",
            "measure_fast_button",
            "speed_button",
            "rank_button",
        )
        names = locked + editable + probes + ("model_tree", "reasoning_combo")
        form = SimpleNamespace(_busy=False, **{name: Widget() for name in names})

        manager.CodexSotaApp._set_editor_protected(form, True)

        self.assertTrue(form._protected)
        for name in locked:
            with self.subTest(locked=name):
                self.assertEqual(getattr(form, name).state_value, "disabled")
        for name in editable + probes:
            with self.subTest(editable=name):
                self.assertEqual(getattr(form, name).state_value, "normal")
        # The rows themselves have to stay clickable; toggling a model is a tree click.
        self.assertEqual(form.model_tree.tree_state, ["!disabled"])

        # An unprotected provider locks nothing, and busy still locks everything.
        form = SimpleNamespace(_busy=False, **{name: Widget() for name in names})
        manager.CodexSotaApp._set_editor_protected(form, False)
        for name in locked + editable + probes:
            with self.subTest(unprotected=name):
                self.assertEqual(getattr(form, name).state_value, "normal")
        form = SimpleNamespace(_busy=True, **{name: Widget() for name in names})
        manager.CodexSotaApp._set_editor_protected(form, False)
        for name in locked + editable + probes:
            with self.subTest(busy=name):
                self.assertEqual(getattr(form, name).state_value, "disabled")
        self.assertEqual(form.model_tree.tree_state, ["disabled"])

    def test_a_protected_providers_identity_survives_whatever_the_form_shows(self) -> None:
        """Widget states are a UI affordance, not the guarantee.

        The identity fields are read-only for a protected provider, so in practice the form shows
        the on-disk values. _provider_from_form pins them anyway, so a stale editor -- or a future
        change to which widgets get disabled -- cannot quietly rewrite the borrowed identity.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="codex",
                label="Codex CLI",
                root=root / "workspace",
                router_port=19992,
                router_starter=root / "starter.ps1",
                protocol="responses",
                needs_catalog=False,
            )
            provider = provider_config(
                "borrowed_login", "https://example.invalid/v1", prefix="", is_default=True
            )
            provider.update({"protected": True, "auth_type": "codex_auth"})
            write_registry(workspace.registry_path, [provider])
            loaded = registry.load_registry(workspace.registry_path, allow_missing_secrets=True)

            form = new_provider_form(workspace)
            form.current_id = "borrowed_login"
            form.registry = loaded
            form.loaded_provider = deepcopy(
                registry.find_provider(loaded, "borrowed_login")
            )
            form.id_var.set("borrowed_login")
            # What the user is allowed to change.
            form.name_var.set("Renamed")
            form.timeout_var.set("42")
            form.enabled_var.set(True)
            form.failover_var.set(False)
            form.draft_models = [
                {"id": "shared-model", "enabled": True},
                {"id": "newly-discovered", "enabled": True},
            ]
            # What it must not matter that the form says.
            form.base_url_var.set("https://hijacked.invalid/v1")
            form.models_path_var.set("/hijacked-models")
            form.responses_path_var.set("/hijacked-responses")
            form.messages_path_var.set("/hijacked-messages")
            form.auth_header_var.set("X-Hijack")
            form.auth_prefix_var.set("Token ")
            form.prefix_var.set("hijack--")
            form.proto_responses_var.set(True)
            form.proto_messages_var.set(True)
            form._enabled_default_exists = lambda: True

            saved = manager.CodexSotaApp._provider_from_form(form)

            self.assertEqual(saved["name"], "Renamed")
            self.assertEqual(saved["timeout_seconds"], 42)
            self.assertEqual(
                [model["id"] for model in saved["models"]],
                ["shared-model", "newly-discovered"],
            )
            for key in registry.PROTECTED_PINNED_PROVIDER_KEYS:
                with self.subTest(pinned=key):
                    self.assertEqual(saved[key], provider[key])
            # And the drift warning must not claim a save would overwrite a pinned field.
            outside = deepcopy(provider)
            outside["base_url"] = "https://moved.invalid/v1"
            write_registry(workspace.registry_path, [outside])
            self.assertEqual(form._outside_edits("borrowed_login"), [])

    def test_save_treats_a_provider_deleted_elsewhere_as_a_fresh_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="codex",
                label="Codex CLI",
                root=root / "workspace",
                router_port=19996,
                router_starter=root / "starter.ps1",
                protocol="responses",
                needs_catalog=False,
            )
            write_registry(workspace.registry_path, [])

            form = new_provider_form(workspace)
            form.current_id = "vendor_gone"
            form.registry = {"version": 1, "providers": []}
            form.loaded_provider = None
            form.name_var.set("Vendor Gone")
            form.id_var.set("vendor_gone")
            form.base_url_var.set("https://example.invalid/v1")
            form.prefix_var.set("")
            form.models_path_var.set("/models")
            form.responses_path_var.set("/responses")
            form.messages_path_var.set("/messages")
            form.auth_header_var.set("Authorization")
            form.auth_prefix_var.set("Bearer ")
            form.timeout_var.set("5")
            form.enabled_var.set(True)
            form.failover_var.set(False)
            form.proto_responses_var.set(True)
            form.proto_messages_var.set(False)
            form.draft_models = [{"id": "shared-model", "enabled": True}]
            form._enabled_default_exists = lambda: False

            self.assertEqual(form._outside_edits("vendor_gone"), [])
            saved = manager.CodexSotaApp._provider_from_form(form)

            # Rebuilt from scratch rather than crashing on the missing entry, and it takes the
            # default role because the workspace has no enabled default left.
            self.assertEqual(saved["id"], "vendor_gone")
            self.assertIs(saved["is_default"], True)
            self.assertEqual(saved["secret_file"], "vendor_gone-api-key.dpapi")

    def test_export_uses_the_current_workspace_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "export.json"
            workspace = registry.Workspace(
                name="claude",
                label="Claude Desktop",
                root=root / "workspace",
                router_port=19998,
                router_starter=root / "starter.ps1",
                protocol="messages",
                needs_catalog=False,
            )
            provider = provider_config(
                "claude_only",
                "https://example.invalid/v1",
                prefix="",
                is_default=True,
                protocols=("messages",),
            )
            write_registry(workspace.registry_path, [provider])
            claude_registry = registry.load_registry(
                workspace.registry_path, allow_missing_secrets=True
            )
            calls: list[dict[str, object]] = []

            def redact(value: dict[str, object]) -> dict[str, object]:
                calls.append(value)
                return deepcopy(value)

            app = SimpleNamespace(
                workspace=workspace,
                registry=claude_registry,
                _append_log=lambda _line: None,
                _show_error=lambda _title, error: self.fail(str(error)),
            )
            with mock.patch.object(manager.filedialog, "asksaveasfilename", return_value=str(output)), \
                mock.patch.object(manager.messagebox, "showinfo"), \
                mock.patch.object(manager, "redacted_registry", side_effect=redact):
                manager.CodexSotaApp._export_config(app)

            self.assertEqual([value["providers"][0]["id"] for value in calls], ["claude_only"])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["providers"][0]["id"], "claude_only")

    def test_stale_router_health_forces_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="test",
                label="test",
                root=root,
                router_port=19999,
                router_starter=root / "starter.ps1",
                protocol="responses",
            )
            provider = provider_config(
                "test_vendor", "http://127.0.0.1:1", prefix="", is_default=True
            )
            write_registry(workspace.registry_path, [provider])

            class Response:
                def __enter__(self) -> "Response":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return

                def read(self) -> bytes:
                    return json.dumps(
                        {
                            "status": "ok",
                            "version": "stale-version",
                            "registry_hash": "stale-hash",
                            "upstreams": ["test_vendor"],
                        }
                    ).encode()

            app = SimpleNamespace(workspace=workspace)
            with mock.patch.object(manager.urllib.request, "urlopen", return_value=Response()), \
                mock.patch.object(manager, "restart_router", return_value={"status": "ready"}) as restart:
                started = manager.CodexSotaApp._ensure_router_running(app, workspace)

            self.assertTrue(started)
            restart.assert_called_once_with(workspace)

    def test_claude_launch_forces_a_new_router_after_it_has_closed_claude(self) -> None:
        """The forced restart has to land in the gap between the close and the launch.

        The profile written during a launch can assert capabilities the router has to implement --
        `supports1m` publishes a `<slug>[1m]` picker entry that only the current routing code
        strips -- and /healthz cannot distinguish a stale router from a current one when only that
        code changed.  Forcing is therefore required, and it is safe exactly once per launch:
        after Claude is down, before the new one starts, when nothing is mid-request.
        """
        order: list[str] = []
        app = SimpleNamespace(
            _busy=False,
            registry={"version": 1, "providers": []},
            _ensure_router_running=lambda _workspace, **kwargs: (
                order.append(f"router(force={kwargs.get('force', False)})") or True
            ),
        )

        def run_task(_label: str, worker: object, _success: object) -> None:
            worker()  # type: ignore[operator]

        app._run_task = run_task
        target = {"kind": "aumid", "value": manager.CLAUDE_SQUIRREL_AUMID, "label": "Claude Squirrel 3P"}
        with mock.patch.object(
            manager, "build_inference_models", return_value=[{"name": "vendor--model", "labelOverride": "Model"}]
        ), mock.patch.object(manager.messagebox, "askokcancel", return_value=True), mock.patch.object(
            manager, "claude_pids", return_value=set()
        ), mock.patch.object(manager, "resolve_claude_launch_target", return_value=target), mock.patch.object(
            manager, "close_running_claude", side_effect=lambda: order.append("close") or {"status": "not-running"}
        ), mock.patch.object(
            manager, "migrate_legacy_entry", return_value={"status": "not-needed"}
        ), mock.patch.object(
            manager, "ensure_deployment_mode", return_value={"status": "ready"}
        ), mock.patch.object(
            manager, "write_claude_profile", side_effect=lambda *_a, **_k: order.append("profile") or {"models": 1}
        ), mock.patch.object(
            manager, "claim_claude_slot", return_value={"status": "claimed", "displaced_name": "CC Switch"}
        ), mock.patch.object(
            manager, "arm_claude_slot_release", return_value={"status": "armed"}
        ), mock.patch.object(
            manager,
            "start_claude_process",
            side_effect=lambda _t: order.append("launch") or {"returncode": 0, "detail": ""},
        ), mock.patch.object(
            manager, "claude_window_present", return_value=True
        ):
            manager.CodexSotaApp._launch_claude(app)

        self.assertEqual(order, ["close", "router(force=True)", "profile", "launch"])

    def test_router_health_requires_version_hash_and_exact_upstream_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="test",
                label="test",
                root=root,
                router_port=19999,
                router_starter=root / "starter.ps1",
                protocol="responses",
            )
            provider = provider_config(
                "test_vendor", "http://127.0.0.1:1", prefix="", is_default=True
            )
            provider["workspace"] = "test"
            write_registry(workspace.registry_path, [provider])

            with mock.patch.dict(registry.WORKSPACES, {"test": workspace}, clear=False):
                loaded = registry.load_registry(
                    workspace.registry_path, allow_missing_secrets=True
                )
                base = {
                    "status": "ok",
                    "version": router.ROUTER_VERSION,
                    "registry_hash": registry.registry_digest(loaded),
                    "upstreams": ["test_vendor"],
                }
                self.assertTrue(manager.router_health_matches_workspace(base, workspace))
                for changed in (
                    base | {"version": "old"},
                    base | {"registry_hash": "stale"},
                    base | {"upstreams": ["test_vendor", "foreign_vendor"]},
                ):
                    self.assertFalse(
                        manager.router_health_matches_workspace(changed, workspace)
                    )

    def test_unsaved_provider_model_test_does_not_attempt_to_persist_path_repair(self) -> None:
        provider = provider_config(
            "new_vendor", "https://example.invalid/v1", prefix="", is_default=True
        )
        app = SimpleNamespace(
            _busy=False,
            registry={"version": 1, "providers": []},
            draft_models=provider["models"],
            reasoning_var=Variable("low"),
            model_tree=WidgetStub(),
            workspace=registry.CODEX,
            _blank_editor=lambda: False,
            _provider_from_form=lambda: deepcopy(provider),
            _probe_key=lambda: TEST_TOKEN,
            _model_index_from_item=lambda _item: None,
        )
        captured: dict[str, object] = {}

        def run_task(_label: str, worker: object, _success: object, _secrets: object) -> None:
            captured["result"] = worker()  # type: ignore[operator]

        app._run_task = run_task
        with mock.patch.object(
            manager,
            "auto_repair_active_inference_path",
            side_effect=lambda item, _key: {
                "changed": True,
                "before": item["responses_path"],
                "after": "/v1/responses",
                "reason": "fixed",
                "protocol": "responses",
            },
        ), mock.patch.object(manager, "apply_provider") as apply, mock.patch.object(
            manager, "test_model", return_value={"ok": False}
        ):
            manager.CodexSotaApp._test_selected_models(app)

        apply.assert_not_called()
        self.assertFalse(captured["result"]["persisted"])  # type: ignore[index]

    def test_claude_launch_reports_process_query_failure_before_mutating_configuration(self) -> None:
        app = SimpleNamespace(
            _busy=False,
            registry={"version": 1, "providers": []},
            _show_error=mock.Mock(),
            _run_task=mock.Mock(),
        )
        with mock.patch.object(
            manager, "build_inference_models", return_value=[{"id": "claude-model"}]
        ), mock.patch.object(
            manager, "claude_window_present", side_effect=RuntimeError("tasklist failed")
        ):
            manager.CodexSotaApp._launch_claude(app)

        app._show_error.assert_called_once()
        self.assertEqual(app._show_error.call_args.args[0], "无法检查 Claude Desktop 状态")
        app._run_task.assert_not_called()

    def test_claude_launch_enables_3p_mode_and_uses_the_resolved_squirrel_target(self) -> None:
        app = SimpleNamespace(
            _busy=False,
            registry={"version": 1, "providers": []},
            _ensure_router_running=lambda _workspace, **_kwargs: False,
        )
        captured: dict[str, object] = {}

        def run_task(_label: str, worker: object, _success: object) -> None:
            captured["result"] = worker()  # type: ignore[operator]

        app._run_task = run_task
        process_calls: list[list[str]] = []

        def run_process(command: list[str], **_kwargs: object) -> SimpleNamespace:
            process_calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        target = {
            "kind": "aumid",
            "value": manager.CLAUDE_SQUIRREL_AUMID,
            "label": "Claude Squirrel 3P",
        }
        with mock.patch.object(
            manager, "build_inference_models", return_value=[{"name": "vendor--model", "labelOverride": "Model"}]
        ), mock.patch.object(manager.messagebox, "askokcancel", return_value=True), mock.patch.object(
            manager, "claude_pids", return_value=set()
        ), mock.patch.object(manager, "resolve_claude_launch_target", return_value=target), mock.patch.object(
            manager, "migrate_legacy_entry", return_value={"status": "not-needed"}
        ), mock.patch.object(manager, "ensure_deployment_mode", return_value={"status": "ready"}) as ensure_mode, mock.patch.object(
            manager, "write_claude_profile", return_value={"models": 1}
        ), mock.patch.object(
            # Unmocked, these two reach the user's real Claude config library and spawn a real
            # detached watcher.  A unit test must not touch either.
            manager, "claim_claude_slot", return_value={"status": "claimed", "displaced_name": "CC Switch"}
        ) as claim_slot, mock.patch.object(
            manager, "arm_claude_slot_release", return_value={"status": "armed"}
        ) as arm_watcher, mock.patch.object(
            manager, "release_claude_slot", return_value={"status": "released"}
        ) as release_slot, mock.patch.object(
            manager.subprocess, "run", side_effect=run_process
        ), mock.patch.object(
            manager, "claude_window_present", return_value=True
        ):
            manager.CodexSotaApp._launch_claude(app)

        ensure_mode.assert_called_once_with()
        expected = f"shell:AppsFolder\\{manager.CLAUDE_SQUIRREL_AUMID}"
        self.assertTrue(
            any(call == [manager.EXPLORER_EXE, expected] for call in process_calls), process_calls
        )
        # The slot is borrowed for the launch and the give-it-back watcher is armed, so
        # cc-switch gets its own profile back when this Claude exits.
        claim_slot.assert_called_once_with()
        arm_watcher.assert_called_once_with()
        release_slot.assert_not_called()

    def test_claude_launch_succeeds_even_though_explorer_reports_a_nonzero_exit_code(self) -> None:
        """explorer.exe is a shell dispatcher, not a launcher.

        Measured on this machine (check_explorer_launch_exitcode.py): a successful AppsFolder
        activation exits 1, and so does a nonexistent AUMID.  Treating non-zero as failure made
        every Claude launch report failure *after* closing the user's running Claude.
        """
        app = SimpleNamespace(
            _busy=False,
            registry={"providers": []},
            header_status_var=SimpleNamespace(set=lambda _value: None),
            _append_log=lambda _message: None,
            _refresh_claude_status=lambda: None,
            _ensure_router_running=lambda _workspace, **_kwargs: False,
            _show_error=lambda *args: self.fail(f"unexpected error dialog: {args}"),
        )
        captured: dict[str, object] = {}

        def run_task(_label: str, worker: object, success: object) -> None:
            captured["result"] = worker()  # type: ignore[operator]
            success(captured["result"])  # type: ignore[operator]

        app._run_task = run_task
        target = {"kind": "aumid", "value": manager.CLAUDE_SQUIRREL_AUMID, "label": "Claude Squirrel 3P"}
        warnings: list[tuple] = []
        with mock.patch.object(
            manager, "build_inference_models", return_value=[{"name": "vendor--model", "labelOverride": "Model"}]
        ), mock.patch.object(manager.messagebox, "askokcancel", return_value=True), mock.patch.object(
            manager.messagebox, "showwarning", side_effect=lambda *a, **k: warnings.append(a)
        ), mock.patch.object(manager, "claude_pids", return_value=set()), mock.patch.object(
            manager, "resolve_claude_launch_target", return_value=target
        ), mock.patch.object(manager, "migrate_legacy_entry", return_value={"status": "not-needed"}), mock.patch.object(
            manager, "ensure_deployment_mode", return_value={"status": "ready", "changed": False}
        ), mock.patch.object(manager, "write_claude_profile", return_value={"models": 1}), mock.patch.object(
            manager, "claim_claude_slot", return_value={"status": "claimed", "displaced_name": "CC Switch"}
        ), mock.patch.object(
            manager, "arm_claude_slot_release", return_value={"status": "armed"}
        ) as arm_watcher, mock.patch.object(
            manager, "release_claude_slot", return_value={"status": "released"}
        ) as release_slot, mock.patch.object(
            manager, "close_running_claude", return_value={"closed": 0}
        ), mock.patch.object(
            manager.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
        ), mock.patch.object(manager, "claude_window_present", return_value=True):
            manager.CodexSotaApp._launch_claude(app)

        result = captured["result"]
        self.assertTrue(result["window"], result)
        self.assertEqual(result["launch_code"], 1)
        self.assertEqual(warnings, [], "a launch that produced a window must not warn")
        arm_watcher.assert_called_once_with()
        release_slot.assert_not_called()

    def test_claude_launch_hands_the_slot_back_when_no_window_appears(self) -> None:
        """A launch that produces no window must not leave cc-switch pointed at our gateway."""
        app = SimpleNamespace(
            _busy=False,
            registry={"providers": []},
            header_status_var=SimpleNamespace(set=lambda _value: None),
            _append_log=lambda _message: None,
            _refresh_claude_status=lambda: None,
            _ensure_router_running=lambda _workspace, **_kwargs: False,
            _show_error=lambda *args: self.fail(f"unexpected error dialog: {args}"),
        )
        captured: dict[str, object] = {}

        def run_task(_label: str, worker: object, success: object) -> None:
            captured["result"] = worker()  # type: ignore[operator]
            success(captured["result"])  # type: ignore[operator]

        app._run_task = run_task
        target = {"kind": "aumid", "value": manager.CLAUDE_SQUIRREL_AUMID, "label": "Claude Squirrel 3P"}
        with mock.patch.object(
            manager, "build_inference_models", return_value=[{"name": "vendor--model", "labelOverride": "Model"}]
        ), mock.patch.object(manager.messagebox, "askokcancel", return_value=True), mock.patch.object(
            manager.messagebox, "showwarning", return_value=None
        ), mock.patch.object(manager, "claude_pids", return_value=set()), mock.patch.object(
            manager, "resolve_claude_launch_target", return_value=target
        ), mock.patch.object(manager, "migrate_legacy_entry", return_value={"status": "not-needed"}), mock.patch.object(
            manager, "ensure_deployment_mode", return_value={"status": "ready", "changed": False}
        ), mock.patch.object(manager, "write_claude_profile", return_value={"models": 1}), mock.patch.object(
            manager, "claim_claude_slot", return_value={"status": "claimed", "displaced_name": "CC Switch"}
        ), mock.patch.object(
            manager, "arm_claude_slot_release", return_value={"status": "armed"}
        ) as arm_watcher, mock.patch.object(
            manager, "release_claude_slot", return_value={"status": "released"}
        ) as release_slot, mock.patch.object(
            manager, "close_running_claude", return_value={"closed": 0}
        ), mock.patch.object(
            manager.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        ), mock.patch.object(manager, "claude_window_present", return_value=False), mock.patch.object(
            manager, "LAUNCH_WINDOW_WAIT_SECONDS", 0.05
        ), mock.patch.object(manager, "LAUNCH_WINDOW_POLL_SECONDS", 0.01):
            manager.CodexSotaApp._launch_claude(app)

        self.assertFalse(captured["result"]["window"])
        release_slot.assert_called_once_with()
        arm_watcher.assert_not_called()


    def test_claude_target_resolver_never_falls_back_to_msix_without_3p_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "Local"
            local.mkdir()
            with mock.patch.object(
                manager,
                "registered_claude_app_ids",
                return_value={manager.CLAUDE_MSIX_AUMID},
            ), mock.patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=False
            ), mock.patch.object(manager, "CLAUDE_3P_ROOT", local / "Claude-3p"):
                self.assertIsNone(manager.resolve_claude_launch_target())

    def test_claude_target_resolver_never_guesses_msix_even_when_3p_data_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "Local"
            three_p = local / "Claude-3p"
            (three_p / "configLibrary").mkdir(parents=True)
            with mock.patch.object(
                manager,
                "registered_claude_app_ids",
                return_value={manager.CLAUDE_MSIX_AUMID},
            ), mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(local), manager.CLAUDE_AUMID_ENV: ""},
                clear=False,
            ), mock.patch.object(manager, "CLAUDE_3P_ROOT", three_p):
                self.assertIsNone(manager.resolve_claude_launch_target())

            with mock.patch.object(
                manager,
                "registered_claude_app_ids",
                return_value={manager.CLAUDE_MSIX_AUMID},
            ), mock.patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(local),
                    manager.CLAUDE_AUMID_ENV: manager.CLAUDE_MSIX_AUMID,
                },
                clear=False,
            ), mock.patch.object(manager, "CLAUDE_3P_ROOT", three_p):
                target = manager.resolve_claude_launch_target()
                self.assertEqual(target["kind"], "aumid")
                self.assertEqual(target["value"], manager.CLAUDE_MSIX_AUMID)

    def test_claude_launch_command_supports_squirrel_update_and_aumid(self) -> None:
        command, cwd = manager.claude_launch_command(
            {"kind": "update", "value": r"C:\Claude\Update.exe", "cwd": r"C:\Claude"}
        )
        self.assertEqual(
            command, [r"C:\Claude\Update.exe", "--processStart", "Claude.exe"]
        )
        self.assertEqual(cwd, Path(r"C:\Claude"))
        command, cwd = manager.claude_launch_command(
            {"kind": "aumid", "value": manager.CLAUDE_SQUIRREL_AUMID}
        )
        self.assertEqual(
            command,
            [manager.EXPLORER_EXE, f"shell:AppsFolder\\{manager.CLAUDE_SQUIRREL_AUMID}"],
        )
        self.assertIsNone(cwd)
        # Absolute, because CreateProcess searches the working directory before PATH and the
        # manager can be started from anywhere.
        self.assertTrue(Path(manager.EXPLORER_EXE).is_absolute(), manager.EXPLORER_EXE)
        self.assertEqual(Path(manager.EXPLORER_EXE).name.lower(), "explorer.exe")

    def test_direct_exe_claude_launch_is_detached_instead_of_pipe_captured(self) -> None:
        """Claude holds its stdout pipe open for its whole lifetime.

        subprocess.run(capture_output=True) on the update/path kinds therefore blocks until the
        timeout expires instead of returning once the app is up.
        """
        popen_calls: list[dict[str, object]] = []

        def fake_popen(command: list[str], **kwargs: object) -> SimpleNamespace:
            popen_calls.append({"command": command, **kwargs})
            return SimpleNamespace(pid=4242)

        with mock.patch.object(manager.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
            manager.subprocess, "run", side_effect=AssertionError("must not capture pipes")
        ):
            result = manager.start_claude_process(
                {"kind": "update", "value": r"C:\Claude\Update.exe", "cwd": r"C:\Claude"}
            )

        self.assertEqual(result["pid"], 4242)
        self.assertIsNone(result["returncode"])
        self.assertEqual(len(popen_calls), 1)
        call = popen_calls[0]
        self.assertEqual(call["stdout"], manager.subprocess.DEVNULL)
        self.assertEqual(call["stderr"], manager.subprocess.DEVNULL)
        self.assertEqual(call["stdin"], manager.subprocess.DEVNULL)
        self.assertTrue(
            call["creationflags"] & getattr(manager.subprocess, "DETACHED_PROCESS", 0x00000008)
        )



class LauncherAndArtifactTests(unittest.TestCase):
    def test_launcher_resolution_prefers_override_then_adjacent_then_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "app" / "CodexSotaManager.py"
            executable = root / "dist" / "codex-sota.exe"
            override = root / "custom" / "launch.cmd"
            adjacent = module.parent / "codex-sota.cmd"
            path_command = root / "path" / "codex-sota.cmd"
            for path in (module, executable, override, adjacent, path_command):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test", encoding="utf-8")

            self.assertEqual(
                manager.resolve_codex_sota_command(
                    {"CODEX_SOTA_COMMAND": str(override)}, module, executable
                ),
                override.resolve(),
            )
            override.unlink()
            self.assertEqual(
                manager.resolve_codex_sota_command({}, module, executable),
                adjacent.resolve(),
            )
            adjacent.unlink()
            self.assertEqual(
                manager.resolve_codex_sota_command(
                    {},
                    module,
                    executable,
                    which=lambda _name: str(path_command),
                    powershell_lookup=lambda _name: None,
                ),
                path_command.resolve(),
            )

    def test_core_root_resolution_uses_environment_or_adjacent_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = root / "configured"
            adjacent = root / "CodexHistorySync"
            manager_module = root / "CodexSotaManager" / "CodexSotaManager.py"
            executable = root / "dist" / "codex-sota.exe"
            for core in (configured, adjacent):
                core.mkdir(parents=True)
                (core / "sota_registry.py").write_text("test", encoding="utf-8")
            manager_module.parent.mkdir()
            manager_module.write_text("test", encoding="utf-8")
            executable.parent.mkdir()
            executable.write_text("test", encoding="utf-8")

            self.assertEqual(
                manager.resolve_core_root(
                    {"CODEX_SOTA_CORE_ROOT": str(configured)}, manager_module, executable
                ),
                configured.resolve(),
            )
            self.assertEqual(
                manager.resolve_core_root({}, manager_module, executable),
                adjacent.resolve(),
            )

    def test_router_starter_reports_the_selected_workspace_port(self) -> None:
        script = (CORE_ROOT / "Start-CodexSotaRouter.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("healthy on 127.0.0.1:17895", script)
        self.assertRegex(script, r"healthy on 127\.0\.0\.1:\$\{?routerPort\}?")

    def test_packaged_artifact_is_complete_and_not_older_than_sources(self) -> None:
        raw_root = os.environ.get("CODEX_SOTA_ARTIFACT_ROOT")
        if not raw_root:
            self.skipTest("CODEX_SOTA_ARTIFACT_ROOT is not set")
        root = Path(raw_root)
        exe = root / "codex-sota.exe"
        internal = root / "_internal"
        required = (
            exe,
            internal / "base_library.zip",
            internal / "_tkinter.pyd",
            internal / "tcl86t.dll",
            internal / "tk86t.dll",
        )
        self.assertEqual([str(path) for path in required if not path.exists()], [])
        self.assertEqual(exe.read_bytes()[:2], b"MZ")
        self.assertGreater(exe.stat().st_size, 1_000_000)
        newest_source = max(
            path.stat().st_mtime
            for path in (
                MANAGER_ROOT / "CodexSotaManager.py",
                CORE_ROOT / "claude_desktop.py",
                CORE_ROOT / "codex_sota_router.py",
                CORE_ROOT / "sota_registry.py",
            )
        )
        self.assertGreaterEqual(
            exe.stat().st_mtime,
            newest_source,
            "packaged executable is stale relative to the fixed sources",
        )


class UsageAccountingTests(unittest.TestCase):
    """The usage/cost layer: what the router logs, and what the panel makes of it."""

    @staticmethod
    def sse(*events: dict) -> bytes:
        return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)

    def test_anthropic_usage_is_read_from_both_ends_of_the_stream(self) -> None:
        """input_tokens arrive first, output_tokens last, so one window is never enough."""
        head = self.sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1200, "output_tokens": 1}}}
        )
        tail = self.sse(
            {"type": "message_delta", "usage": {"output_tokens": 340}},
            {"type": "message_stop"},
        )
        self.assertEqual(
            router.extract_token_usage(head, tail),
            {"tokens_in": 1200, "tokens_out": 340},
        )

    def test_responses_usage_is_read_from_the_closing_event(self) -> None:
        body = self.sse(
            {"type": "response.created", "response": {"id": "resp_1"}},
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 88, "output_tokens": 9}},
            },
        )
        self.assertEqual(
            router.extract_token_usage(body[:16], body),
            {"tokens_in": 88, "tokens_out": 9},
        )

    def test_chat_completions_spelling_is_understood(self) -> None:
        body = json.dumps(
            {"usage": {"prompt_tokens": 7, "completion_tokens": 11}}
        ).encode()
        self.assertEqual(
            router.extract_token_usage(body, body), {"tokens_in": 7, "tokens_out": 11}
        )

    def test_a_vendor_that_reports_nothing_produces_no_zero_keys(self) -> None:
        """A missing count must stay missing: a logged 0 would read as a free request."""
        self.assertEqual(router.extract_token_usage(b"", b""), {})
        self.assertEqual(router.extract_token_usage(b"data: [DONE]\n\n", b"\x00\xff{"), {})

    def test_accounting_never_raises_on_junk(self) -> None:
        for head, tail in (
            (b"{", b"}"),
            (b"data: {\"usage\":", b"truncated"),
            (os.urandom(64), os.urandom(64)),
            (b"data: null\n\n", b"data: 3\n\n"),
        ):
            with self.subTest(head=head[:8]):
                self.assertIsInstance(router.extract_token_usage(head, tail), dict)

    @staticmethod
    def router_state(root: Path, log: Path) -> "router.RouterState":
        """A RouterState wired to a throwaway registry — enough to exercise the log writer."""
        registry_path = root / "providers.json"
        auth_path = root / "auth.json"
        write_auth(auth_path)
        write_registry(
            registry_path,
            [provider_config("vendor_a", "http://127.0.0.1:9", prefix="", is_default=True)],
        )
        return router.RouterState(registry_path, auth_path, log)

    def test_record_logs_model_and_usage_without_breaking_old_readers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self.router_state(root, root / "sota-router.jsonl")
            state.record("vendor_a", "POST", "/responses", 200, 1.25)
            state.record(
                "vendor_a", "POST", "/responses", 200, 0.5,
                model="gpt-5", usage={"tokens_in": 10, "tokens_out": 20},
            )
            # A negative or non-int count is a bug upstream, not something to log.
            state.record(
                "vendor_a", "POST", "/responses", 500, 0.1,
                model="gpt-5", usage={"tokens_in": -5, "tokens_out": None},  # type: ignore[dict-item]
            )
            lines = [
                json.loads(line)
                for line in (root / "sota-router.jsonl").read_text(encoding="utf-8").splitlines()
            ]
        self.assertNotIn("model", lines[0])
        self.assertNotIn("tokens_in", lines[0])
        self.assertEqual((lines[1]["model"], lines[1]["tokens_in"], lines[1]["tokens_out"]), ("gpt-5", 10, 20))
        self.assertEqual(lines[2]["model"], "gpt-5")
        self.assertNotIn("tokens_in", lines[2])
        self.assertNotIn("tokens_out", lines[2])

    def test_log_rotation_keeps_one_generation_and_never_loses_the_new_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "sota-router.jsonl"
            state = self.router_state(root, log)
            log.write_bytes(b"x" * (router.LOG_MAX_BYTES + 1))
            state.record("vendor_a", "POST", "/responses", 200, 0.2, model="gpt-5")
            rotated = log.with_suffix(log.suffix + ".1")
            self.assertTrue(rotated.exists())
            self.assertEqual(rotated.stat().st_size, router.LOG_MAX_BYTES + 1)
            fresh = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(fresh), 1)
            self.assertEqual(json.loads(fresh[0])["vendor"], "vendor_a")

            # A second rotation replaces .1 rather than piling up .2, .3, ...
            log.write_bytes(b"y" * (router.LOG_MAX_BYTES + 1))
            state.record("vendor_b", "POST", "/responses", 200, 0.2, model="gpt-5")
            self.assertEqual(rotated.read_bytes()[:1], b"y")
            self.assertEqual(sorted(p.name for p in root.glob("sota-router.jsonl*")),
                             ["sota-router.jsonl", "sota-router.jsonl.1"])

    @staticmethod
    def write_usage_log(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def test_a_vendor_is_only_billed_for_the_tokens_it_served(self) -> None:
        """The bug this pins: fanning a model's whole cost out to every vendor that answered."""
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "sota-router.jsonl"
            self.write_usage_log(log, [
                {"time": "2026-01-01T00:00:00Z", "vendor": "paid", "status": 200,
                 "duration_ms": 900, "model": "gpt-5", "tokens_in": 1_000_000,
                 "tokens_out": 1_000_000},
                # Same model, different vendor, and it failed with no tokens at all.
                {"time": "2026-01-01T00:00:01Z", "vendor": "broken", "status": 502,
                 "duration_ms": 40, "model": "gpt-5"},
            ])
            summary = manager.read_router_usage(
                log, {"currency": "USD", "models": {"gpt-5": {"in": 1.25, "out": 10.0}}}
            )
        self.assertAlmostEqual(summary["vendors"]["paid"]["cost"], 11.25)
        self.assertEqual(summary["vendors"]["broken"]["cost"], 0.0)
        self.assertAlmostEqual(summary["totals"]["cost"], 11.25)
        self.assertAlmostEqual(
            summary["totals"]["cost"],
            sum(row["cost"] for row in summary["vendors"].values()),
        )

    def test_legacy_lines_are_the_ones_written_before_models_were_logged(self) -> None:
        """A failed request legitimately has no tokens; that is not the same as an old line."""
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "sota-router.jsonl"
            self.write_usage_log(log, [
                {"time": "2026-01-01T00:00:00Z", "vendor": "paid", "status": 200,
                 "duration_ms": 10},
                {"time": "2026-01-01T00:00:01Z", "vendor": "paid", "status": 502,
                 "duration_ms": 10, "model": "gpt-5"},
                {"not": "a log line"},
                b"\xff".decode("utf-8", "replace"),
            ])
            summary = manager.read_router_usage(log)
        self.assertEqual(summary["lines"], 2)
        self.assertEqual(summary["legacy_lines"], 1)
        self.assertEqual(summary["totals"]["requests"], 2)

    def test_today_is_counted_and_priced_on_the_users_own_calendar_day(self) -> None:
        start, end = manager.utc_day_boundaries()
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "sota-router.jsonl"
            self.write_usage_log(log, [
                {"time": start, "vendor": "paid", "status": 200, "duration_ms": 10,
                 "model": "gpt-5", "tokens_in": 1_000_000, "tokens_out": 0},
                {"time": "2020-01-01T00:00:00Z", "vendor": "paid", "status": 200,
                 "duration_ms": 10, "model": "gpt-5", "tokens_in": 1_000_000, "tokens_out": 0},
            ])
            summary = manager.read_router_usage(
                log, {"currency": "USD", "models": {"gpt-5": {"in": 2.0, "out": 0.0}}}
            )
        self.assertLess(start, end)
        self.assertEqual(summary["today"]["requests"], 1)
        self.assertAlmostEqual(summary["today"]["cost"], 2.0)
        self.assertAlmostEqual(summary["totals"]["cost"], 4.0)

    def test_prices_survive_a_round_trip_and_a_corrupt_file_is_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "usage-prices.json"
            manager.save_usage_prices(path, {
                "currency": "CNY",
                "models": {"gpt-5": {"in": 1.5, "out": 12.0}, "junk": {"in": "free"},
                           "negative": {"in": -1}},
            })
            table = manager.load_usage_prices(path)
            self.assertEqual(table["currency"], "CNY")
            self.assertEqual(table["models"], {"gpt-5": {"in": 1.5, "out": 12.0}})
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(manager.load_usage_prices(path)["models"], {})
            self.assertEqual(
                manager.load_usage_prices(path / "missing")["currency"], "USD"
            )

    def test_prefixed_price_keys_match_the_plain_model_id_in_the_log(self) -> None:
        table = {"models": {"openai/gpt-5": {"in": 1.0, "out": 2.0}}}
        self.assertEqual(manager.price_for_model(table, "gpt-5"), {"in": 1.0, "out": 2.0})
        self.assertIsNone(manager.price_for_model(table, "gpt-4o"))

    def test_both_router_prefix_shapes_are_tolerated_in_a_hand_written_price_table(self) -> None:
        """The log always carries the bare upstream id, so a decorated key must still match.

        The price dialog keys rows by model id and the router logs `model=upstream_model`, so
        the exact-hit branch covers everything the app writes itself. These two shapes only
        turn up in a file someone edited by hand, or one an older build left behind -- and the
        dotted one is what the messages-side migration made possible, so it gets pinned here.
        """
        table = {
            "models": {
                "tango--claude-opus-5": {"in": 3.0, "out": 15.0},
                "sierra.anthropic.claude-opus-4-6": {"in": 1.5, "out": 7.5},
                "claude-sonnet-5": {"in": 0.5, "out": 2.5},
            }
        }
        self.assertEqual(
            manager.price_for_model(table, "claude-opus-5"), {"in": 3.0, "out": 15.0}
        )
        self.assertEqual(
            manager.price_for_model(table, "claude-opus-4-6"), {"in": 1.5, "out": 7.5}
        )
        # An undecorated key still wins outright, and a prefix is never invented to force a hit.
        self.assertEqual(
            manager.price_for_model(table, "claude-sonnet-5"), {"in": 0.5, "out": 2.5}
        )
        self.assertIsNone(manager.price_for_model(table, "claude-opus-5-thinking"))
        self.assertIsNone(manager.price_for_model(table, "tango"))

    def test_a_missing_log_reports_zeroes_rather_than_raising(self) -> None:
        summary = manager.read_router_usage(Path("nowhere") / "sota-router.jsonl")
        self.assertEqual(summary["lines"], 0)
        self.assertEqual(summary["totals"]["requests"], 0)
        self.assertEqual(summary["totals"]["p95_ms"], 0)
        self.assertEqual(summary["vendors"], {})


class UsageUiTests(unittest.TestCase):
    """The usage tab itself, built against a real Tk root but never shown.

    Worth doing as a test rather than by eye: every widget here is created from data, so a
    renamed summary key or a missing style silently produces an empty panel at runtime.
    """

    def setUp(self) -> None:
        try:
            self.root = manager.tk.Tk()
        except manager.tk.TclError as error:  # pragma: no cover - headless CI
            self.skipTest(f"no Tk display available: {error}")
        self.root.withdraw()
        manager.CodexSotaApp._configure_styles(self.root)

    def tearDown(self) -> None:
        # Order matters here, and getting it wrong crashes a *later* test file rather than this
        # one.  Tk.destroy() only tears down widgets; the Tcl interpreter itself is freed when
        # the Python Tk object is deallocated, and Tcl insists that happen on the thread that
        # created it.  Left to the cycle collector, the dead root can instead be reaped by a
        # worker thread in a subsequent test, and Tcl aborts the whole process with
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread" -- no traceback, no
        # results summary.  So: collect the dialogs' Variables while the interpreter is still
        # alive, then destroy it, then collect the root here on the main thread.
        gc.collect()
        self.root.destroy()
        self.root = None
        gc.collect()

    def build_tab(self) -> SimpleNamespace:
        stub = SimpleNamespace(
            usage_tab=manager.ttk.Frame(self.root, style="Surface.TFrame"),
            workspace=manager.CODEX,
            _busy=False,
            _closing=False,
            _usage_summary={},
            _usage_tree=manager.CodexSotaApp._usage_tree,
            _open_path=lambda _path: None,
            _edit_usage_prices=lambda: None,
            _export_usage_csv=lambda: None,
            _refresh_usage=lambda *_a: None,
        )
        manager.CodexSotaApp._build_usage_tab(stub)
        return stub

    @staticmethod
    def summary() -> dict:
        return {
            "currency": "USD",
            "lines": 3,
            "legacy_lines": 1,
            "totals": {"requests": 3, "ok": 2, "tokens_in": 1500, "tokens_out": 500,
                       "tokens": 2000, "success_rate": 2 / 3, "p50_ms": 900, "p95_ms": 1200,
                       "cost": 1.5},
            "today": {"requests": 1, "ok": 1, "tokens_in": 1000, "tokens_out": 0,
                      "tokens": 1000, "success_rate": 1.0, "p50_ms": 900, "p95_ms": 900,
                      "cost": 0.5},
            "vendors": {
                "paid": {"requests": 2, "ok": 2, "tokens_in": 1500, "tokens_out": 500,
                         "tokens": 2000, "success_rate": 1.0, "p50_ms": 900, "p95_ms": 1200,
                         "cost": 1.5},
                "broken": {"requests": 1, "ok": 0, "tokens_in": 0, "tokens_out": 0,
                           "tokens": 0, "success_rate": 0.0, "p50_ms": 0, "p95_ms": 0,
                           "cost": 0.0},
            },
            "models": {
                "gpt-5": {"requests": 2, "ok": 2, "tokens_in": 1500, "tokens_out": 500,
                          "tokens": 2000, "success_rate": 1.0, "p50_ms": 900, "p95_ms": 1200,
                          "cost": 1.5, "priced": True, "vendors": ["paid"]},
                "mystery": {"requests": 1, "ok": 1, "tokens_in": 10, "tokens_out": 0,
                            "tokens": 10, "success_rate": 1.0, "p50_ms": 5, "p95_ms": 5,
                            "cost": 0.0, "priced": False, "vendors": ["paid", "broken"]},
            },
            "first_time": "2026-01-01T00:00:00Z",
            "last_time": "2026-01-02T00:00:00Z",
            "log_bytes": 4096,
            "truncated": False,
            "unpriced": ["mystery"],
        }

    def test_the_tab_reports_spend_traffic_and_what_it_cannot_price(self) -> None:
        stub = self.build_tab()
        manager.CodexSotaApp._render_usage(stub, self.summary())
        self.assertIn("$1.50", stub.usage_total_var.get())
        self.assertIn("$0.50", stub.usage_today_var.get())
        self.assertIn("2.0K token", stub.usage_total_var.get())
        models = [
            stub.usage_model_tree.item(row, "values")
            for row in stub.usage_model_tree.get_children()
        ]
        # Heaviest model first, and the unpriced one says so instead of claiming it was free.
        self.assertEqual([row[0] for row in models], ["gpt-5", "mystery"])
        self.assertEqual(models[0][6], "是")
        self.assertEqual(models[1][6], "未定价")
        self.assertEqual(models[1][5], "—")
        vendors = [
            stub.usage_vendor_tree.item(row, "values")
            for row in stub.usage_vendor_tree.get_children()
        ]
        self.assertEqual([row[0] for row in vendors], ["paid", "broken"])
        self.assertEqual(vendors[1][2], "0%")
        note = stub.usage_note_var.get()
        self.assertIn("旧记录", note)
        self.assertIn("mystery", note)

    def test_an_empty_log_says_so_instead_of_showing_a_blank_panel(self) -> None:
        stub = self.build_tab()
        empty = manager.read_router_usage(Path("nowhere") / "sota-router.jsonl")
        manager.CodexSotaApp._render_usage(stub, empty)
        self.assertIn("还没有路由记录", stub.usage_span_var.get())
        self.assertEqual(stub.usage_model_tree.get_children(), ())
        self.assertIn("0 次", stub.usage_today_var.get())

    def test_the_price_dialog_round_trips_numbers_and_rejects_junk(self) -> None:
        prices = {"currency": "USD", "models": {"openai/gpt-5": {"in": 1.25, "out": 10.0}}}
        dialog = manager.PriceDialog(self.root, ["gpt-5", "gpt-4o"], prices)
        try:
            # The prefixed table row still populates the plain model id's fields.
            self.assertEqual(dialog.rows["gpt-5"][0].get(), "1.25")
            self.assertEqual(dialog.rows["gpt-4o"][0].get(), "")
            # A decimal comma is rejected, not silently read as a thousands separator.
            dialog.rows["gpt-4o"][0].set("2,5")
            with mock.patch.object(manager.messagebox, "showerror") as complained:
                dialog._accept()
            self.assertTrue(complained.called)
            self.assertIsNone(dialog.result)
            # Full-width digits from a Chinese IME are folded to ASCII rather than rejected.
            dialog.rows["gpt-4o"][0].set("３．５")
            dialog.currency.set("CNY")
            dialog._accept()
        finally:
            if dialog.winfo_exists():
                dialog.destroy()
        self.assertEqual(dialog.result["currency"], "CNY")
        self.assertEqual(
            dialog.result["models"],
            {"gpt-5": {"in": 1.25, "out": 10.0}, "gpt-4o": {"in": 3.5, "out": 0.0}},
        )

    def test_a_cleared_price_removes_the_model_from_the_table(self) -> None:
        dialog = manager.PriceDialog(
            self.root, ["gpt-5"], {"currency": "USD", "models": {"gpt-5": {"in": 1.0, "out": 2.0}}}
        )
        try:
            dialog.rows["gpt-5"][0].set("")
            dialog.rows["gpt-5"][1].set("")
            dialog._accept()
        finally:
            if dialog.winfo_exists():
                dialog.destroy()
        self.assertEqual(dialog.result["models"], {})


class ConfigRestoreTests(unittest.TestCase):
    """Restoring a backup must not write placeholders, lose secrets, or cross workspaces."""

    @contextmanager
    def workspace(self, name: str = "test", port: int = 19993):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            space = registry.Workspace(
                name=name,
                label=name,
                root=root,
                router_port=port,
                router_starter=root / "starter.ps1",
                protocol="responses",
            )
            with mock.patch.dict(registry.WORKSPACES, {name: space}, clear=False):
                yield space

    @staticmethod
    def dpapi_provider(provider_id: str, workspace_name: str, **extra: object) -> dict[str, object]:
        provider = provider_config(
            provider_id, "https://example.invalid/v1", prefix="", is_default=True
        )
        provider.update(
            {
                "workspace": workspace_name,
                "auth_type": "dpapi",
                "secret_file": f"{provider_id}.dpapi",
                "entropy": "entropy-on-disk",
            }
        )
        provider.update(extra)
        return provider

    def test_export_then_restore_puts_back_the_header_values_on_disk(self) -> None:
        with self.workspace() as space:
            provider = self.dpapi_provider("header_vendor", space.name)
            provider["extra_headers"] = {"X-Api-Key": "secondary-secret"}
            current = {"version": 1, "providers": [deepcopy(provider)]}
            exported = registry.redacted_registry(deepcopy(current))
            # The exporter's own output is the realistic restore input, so go through it rather
            # than hand-writing what we think a backup looks like.
            self.assertEqual(
                exported["providers"][0]["extra_headers"], {"X-Api-Key": manager.REDACTED_HEADER}
            )

            candidate, report = manager.merge_restored_registry(exported, current, space.name)

            restored = candidate["providers"][0]
            self.assertEqual(restored["extra_headers"], {"X-Api-Key": "secondary-secret"})
            self.assertEqual(restored["entropy"], "entropy-on-disk")
            self.assertNotIn("key_present", restored)
            self.assertEqual(report["kept_headers"], ["header_vendor.X-Api-Key"])
            self.assertNotIn(
                manager.REDACTED_HEADER, json.dumps(candidate, ensure_ascii=False)
            )

    def test_a_redacted_header_with_no_disk_value_is_dropped_not_written(self) -> None:
        with self.workspace() as space:
            incoming = {
                "version": 1,
                "providers": [
                    self.dpapi_provider(
                        "brand_new",
                        space.name,
                        extra_headers={"X-Api-Key": manager.REDACTED_HEADER},
                    )
                ],
            }

            candidate, report = manager.merge_restored_registry(
                incoming, {"version": 1, "providers": []}, space.name
            )

            # Writing the placeholder verbatim would send the literal "<redacted>" as a
            # credential -- a 401 with no explanation. Dropping it surfaces as "missing header".
            self.assertEqual(candidate["providers"][0]["extra_headers"], {})
            self.assertEqual(report["added"], ["brand_new"])
            self.assertEqual(report["kept_headers"], [])

    def test_a_backup_from_the_other_workspace_is_reported_not_merged(self) -> None:
        with self.workspace(name="claude_test", port=19992) as space:
            incoming = {
                "version": 1,
                "providers": [self.dpapi_provider("codex_vendor", "codex")],
            }

            _candidate, report = manager.merge_restored_registry(
                incoming, {"version": 1, "providers": []}, space.name
            )

            self.assertEqual(report["foreign_workspace"], ["codex_vendor（codex）"])

    def test_a_restore_that_would_drop_a_protected_provider_is_flagged(self) -> None:
        with self.workspace() as space:
            keeper = self.dpapi_provider("builtin_vendor", space.name, protected=True)
            current = {"version": 1, "providers": [keeper, self.dpapi_provider("spare", space.name)]}
            incoming = {"version": 1, "providers": [self.dpapi_provider("spare", space.name)]}

            _candidate, report = manager.merge_restored_registry(incoming, current, space.name)

            self.assertEqual(report["removed"], ["builtin_vendor"])
            self.assertEqual(report["protected_removed"], ["builtin_vendor"])

    def test_only_providers_whose_key_file_is_absent_are_reported_as_needing_one(self) -> None:
        with self.workspace() as space:
            space.secrets_root.mkdir(parents=True, exist_ok=True)
            (space.secrets_root / "has_key.dpapi").write_bytes(b"ciphertext")
            incoming = {
                "version": 1,
                "providers": [
                    self.dpapi_provider("has_key", space.name),
                    self.dpapi_provider("no_key", space.name, is_default=False, prefix="nk/"),
                    provider_config(
                        "codex_login", "https://example.invalid/v1", prefix="cl/", is_default=False
                    )
                    | {"workspace": space.name},
                ],
            }

            _candidate, report = manager.merge_restored_registry(
                incoming, {"version": 1, "providers": []}, space.name
            )

            # codex_auth borrows the Codex App's own login, so it has no key file to miss.
            self.assertEqual(report["needs_key"], ["no_key"])

    def test_restore_writes_the_registry_and_backs_up_the_old_one_first(self) -> None:
        with self.workspace() as space:
            space.secrets_root.mkdir(parents=True, exist_ok=True)
            (space.secrets_root / "kept.dpapi").write_bytes(b"ciphertext")
            live = self.dpapi_provider("kept", space.name)
            live["extra_headers"] = {"X-Api-Key": "secondary-secret"}
            write_registry(space.registry_path, [live])
            space.catalog_path.write_text('{"models":[]}', encoding="utf-8")
            backup_file = space.root / "backup.json"
            snapshot = registry.redacted_registry(
                registry.load_registry(space.registry_path, allow_missing_secrets=True)
            )
            snapshot["providers"][0]["timeout_seconds"] = 42
            backup_file.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            app = self.restore_app(space)
            with mock.patch.object(
                manager.filedialog, "askopenfilename", return_value=str(backup_file)
            ), mock.patch.object(manager.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(manager.messagebox, "showinfo"), \
                mock.patch.object(manager, "rebuild_catalog", return_value={"models": []}):
                manager.CodexSotaApp._restore_config(app)

            saved = registry.load_registry(space.registry_path, allow_missing_secrets=True)
            self.assertEqual(saved["providers"][0]["timeout_seconds"], 42)
            self.assertEqual(
                saved["providers"][0]["extra_headers"], {"X-Api-Key": "secondary-secret"}
            )
            backups = sorted((space.root / "backups").glob("pre-restore-*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "providers.json").exists())
            self.assertTrue((backups[0] / space.catalog_path.name).exists())

    def test_a_foreign_workspace_backup_is_refused_without_touching_the_registry(self) -> None:
        with self.workspace() as space:
            space.secrets_root.mkdir(parents=True, exist_ok=True)
            (space.secrets_root / "kept.dpapi").write_bytes(b"ciphertext")
            write_registry(space.registry_path, [self.dpapi_provider("kept", space.name)])
            before = space.registry_path.read_text(encoding="utf-8")
            intruder = self.dpapi_provider("claude_vendor", "claude")
            backup_file = space.root / "claude-backup.json"
            backup_file.write_text(
                json.dumps({"version": 1, "providers": [intruder]}, ensure_ascii=False),
                encoding="utf-8",
            )

            app = self.restore_app(space)
            with mock.patch.object(
                manager.filedialog, "askopenfilename", return_value=str(backup_file)
            ), mock.patch.object(manager.messagebox, "showerror") as showerror, \
                mock.patch.object(manager.messagebox, "askokcancel") as askokcancel:
                manager.CodexSotaApp._restore_config(app)

            askokcancel.assert_not_called()
            self.assertEqual(space.registry_path.read_text(encoding="utf-8"), before)
            self.assertIn("工作区", showerror.call_args.args[0])

    def test_a_file_without_providers_is_rejected_before_anything_is_written(self) -> None:
        with self.workspace() as space:
            space.secrets_root.mkdir(parents=True, exist_ok=True)
            (space.secrets_root / "kept.dpapi").write_bytes(b"ciphertext")
            write_registry(space.registry_path, [self.dpapi_provider("kept", space.name)])
            before = space.registry_path.read_text(encoding="utf-8")
            junk = space.root / "notes.json"
            junk.write_text('{"hello":"world"}', encoding="utf-8")

            app = self.restore_app(space)
            errors: list[str] = []
            app._show_error = lambda title, error: errors.append(f"{title}: {error}")
            with mock.patch.object(
                manager.filedialog, "askopenfilename", return_value=str(junk)
            ), mock.patch.object(manager.messagebox, "askokcancel") as askokcancel:
                manager.CodexSotaApp._restore_config(app)

            askokcancel.assert_not_called()
            self.assertEqual(space.registry_path.read_text(encoding="utf-8"), before)
            self.assertIn("providers", errors[0])

    def restore_app(self, space: registry.Workspace) -> SimpleNamespace:
        """The slice of CodexSotaApp that _restore_config actually touches.

        _run_task normally hands the worker to a thread and reports back through the Tk event
        queue; here it runs inline so the assertions see the finished write.
        """
        app = SimpleNamespace(_busy=False, current_id=None, workspace=space, log=[])
        app._confirm_discarding_edits = lambda _action: True
        app._show_error = lambda title, error: self.fail(f"{title}: {error}")
        app._load_registry = lambda *_args: None
        app._run_audit = lambda: None
        app._append_log = app.log.append
        app._restore_summary = lambda source, candidate, report: manager.CodexSotaApp._restore_summary(
            app, source, candidate, report
        )
        app._confirm_and_restore = lambda source, candidate, report: manager.CodexSotaApp._confirm_and_restore(
            app, source, candidate, report
        )
        app._run_task = lambda _label, worker, success: success(worker())
        return app


if __name__ == "__main__":
    unittest.main(verbosity=2)
