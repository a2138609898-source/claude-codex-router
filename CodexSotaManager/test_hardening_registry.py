"""Isolated regressions for registry ownership, catalog aliases and endpoint repair.

Run only through test-support/run_guarded_tests.py. Every credential below is synthetic;
real DPAPI, router starts/stops and HTTP are independently mocked as an extra safeguard.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request


CORE_ROOT = Path(__file__).resolve().parent.parent / "CodexHistorySync"
sys.path.insert(0, str(CORE_ROOT))
import sota_registry as registry


class RegistryFixture(unittest.TestCase):
    def setUp(self) -> None:
        allowed_root = Path(__file__).resolve().parents[2] / "test-tmp"
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if not temporary_root.is_relative_to(allowed_root.resolve()):
            # Normal Build-Staged validation also discovers this suite. Keep that path
            # usable while still confining its synthetic fixtures to this source tree.
            allowed_root.mkdir(parents=True, exist_ok=True)
            temporary_root = allowed_root.resolve()
        temporary = tempfile.TemporaryDirectory(prefix="registry-", dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.codex = registry.Workspace(
            "codex", "Fixture Codex", self.root / "codex", 0,
            self.root / "never-start-codex.ps1", "responses",
        )
        self.claude = registry.Workspace(
            "claude", "Fixture Claude", self.root / "claude", 0,
            self.root / "never-start-claude.ps1", "messages", needs_catalog=False,
        )
        workspace_patch = patch.dict(registry.WORKSPACES, {"codex": self.codex, "claude": self.claude})
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
        for name in ("dpapi_unprotect", "dpapi_protect", "read_codex_auth_key", "restart_router", "stop_router", "_http_json"):
            safeguard = patch.object(registry, name, side_effect=AssertionError(f"Unexpected real {name}"))
            safeguard.start()
            self.addCleanup(safeguard.stop)
        self.codex.root.mkdir(parents=True)
        self.codex.source_catalog_path.write_text(json.dumps({"models": [
            {"slug": "gpt-5.6-sol", "context_window": 256000,
             "supported_reasoning_levels": [{"effort": "low"}], "fixture_template": "default"},
            {"slug": "fixture-alias", "context_window": 123456,
             "supported_reasoning_levels": [{"effort": "max"}], "fixture_template": "alias"},
        ]}), encoding="utf-8")

    def provider(self, provider_id: str = "alpha", **changes: object) -> dict:
        value = {
            "id": provider_id, "name": provider_id.title(), "workspace": "codex",
            "base_url": "https://fixture.invalid", "enabled": True,
            "auth_type": "dpapi", "is_default": provider_id == "alpha",
            "models": [{"id": "fixture-model", "enabled": True}],
        }
        value.update(changes)
        return registry.validate_provider(value, allow_missing_secret=True)

    def seed(self, providers: list[dict]) -> dict:
        value = registry.validate_registry(
            {"version": 1, "providers": deepcopy(providers)},
            allow_missing_secrets=True, allow_shared_secrets=True,
        )
        for provider in value["providers"]:
            if provider["auth_type"] == "dpapi":
                secret = registry.secret_path(provider)
                secret.parent.mkdir(parents=True, exist_ok=True)
                if not secret.exists():
                    secret.write_bytes(b"synthetic-encrypted-fixture-only")
        self.codex.registry_path.write_text(json.dumps(value), encoding="utf-8")
        registry.build_model_catalog(value, self.codex.source_catalog_path, self.codex.catalog_path)
        return deepcopy(value)

    def stored(self, provider_id: str = "alpha") -> dict:
        value = registry.load_registry(self.codex.registry_path, allow_missing_secrets=True)
        return next(provider for provider in value["providers"] if provider["id"] == provider_id)


class CatalogAliasTests(RegistryFixture):
    def test_alias_template_uses_each_provider_prefix_not_the_last_provider(self) -> None:
        first = self.provider(models=[{"id": "vendor-model", "enabled": True,
                                       "publish_as": "alpha--fixture-alias"}])
        last = self.provider("omega")
        for providers in ([first, last], [last, first]):
            with self.subTest(order=[provider["id"] for provider in providers]):
                self.seed(providers)
                models = json.loads(self.codex.catalog_path.read_text(encoding="utf-8"))["models"]
                by_slug = {model["slug"]: model for model in models}
                alias = by_slug["alpha--fixture-alias"]
                self.assertEqual(alias["fixture_template"], "alias")
                self.assertEqual(alias["context_window"], 123456)
                self.assertEqual(alias["supported_reasoning_levels"], [{"effort": "max"}])
                self.assertEqual(by_slug["omega--fixture-model"]["fixture_template"], "default")

    def test_internal_builder_does_not_reacquire_registry_lock(self) -> None:
        with patch.object(registry, "registry_write_lock", side_effect=AssertionError("nested lock")):
            self.seed([self.provider()])

    def test_external_builder_holds_lock_across_read_and_build(self) -> None:
        source = ast.parse((CORE_ROOT / "Build-CodexSotaModelCatalog.py").read_text(encoding="utf-8"))
        main = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        events: list[str] = []
        held = False

        @contextmanager
        def lock(*, workspace):
            nonlocal held
            self.assertIs(workspace, self.codex)
            held = True
            events.append("lock")
            try:
                yield
            finally:
                held = False
                events.append("unlock")

        def load(path):
            self.assertTrue(held)
            self.assertEqual(path, self.codex.registry_path)
            events.append("read")
            return {"version": 1, "providers": []}

        def build(value, source_path, destination_path):
            self.assertTrue(held)
            self.assertEqual(source_path, self.codex.source_catalog_path)
            self.assertEqual(destination_path, self.codex.catalog_path)
            events.append("build")
            return {"status": "ok"}

        namespace = {"json": json, "default_workspace": lambda: self.codex,
                     "registry_write_lock": lock, "load_registry": load, "build_model_catalog": build}
        exec(compile(ast.Module(body=[main], type_ignores=[]), "<isolated catalog main>", "exec"), namespace)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(namespace["main"](), 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "ok"})
        self.assertEqual(events, ["lock", "read", "build", "unlock"])


class AuthenticationHeaderTests(RegistryFixture):
    def test_case_variants_cannot_replace_custom_authentication(self) -> None:
        provider = self.provider(auth_header="x-api-key", auth_prefix="", extra_headers={
            "x-api-key": "wrong-first", "X-API-Key": "wrong-later", "Authorization": "wrong-auth",
        })
        for defaults in (True, False):
            with self.subTest(defaults=defaults):
                headers = registry.auth_headers(provider, "fixture-key", include_probe_defaults=defaults)
                request = urllib.request.Request("https://fixture.invalid", headers=headers)
                actual = {name.lower(): value for name, value in request.header_items()}
                self.assertEqual(actual["x-api-key"], "fixture-key")
                self.assertNotIn("authorization", actual)
                self.assertEqual(sum(name.lower() == "x-api-key" for name in headers), 1)

    def test_normal_headers_and_probe_defaults_remain_overridable(self) -> None:
        provider = self.provider(extra_headers={
            "user-agent": "Fixture Client", "ORIGINATOR": "fixture-origin",
            "X-Trace": "first", "x-trace": "last", "AUTHORIZATION": "wrong",
            "Host": "wrong.invalid", "Content-Length": "999",
        })
        headers = registry.auth_headers(provider, "fixture-key")
        lowered = {name.lower(): value for name, value in headers.items()}
        self.assertEqual(lowered["user-agent"], "Fixture Client")
        self.assertEqual(lowered["originator"], "fixture-origin")
        self.assertEqual(lowered["x-trace"], "last")
        self.assertEqual(lowered["authorization"], "Bearer fixture-key")
        self.assertNotIn("host", lowered)
        self.assertNotIn("content-length", lowered)
        self.assertEqual(len(lowered), len(headers))

    def test_authentication_header_matching_a_probe_default_has_only_one_spelling(self) -> None:
        provider = self.provider(auth_header="user-agent", auth_prefix="")
        headers = registry.auth_headers(provider, "fixture-key")
        self.assertEqual(sum(name.lower() == "user-agent" for name in headers), 1)
        self.assertEqual(headers["user-agent"], "fixture-key")


class SecretOwnershipTests(RegistryFixture):
    def test_new_registry_rejects_shared_paths_including_case_variants(self) -> None:
        for filename in ("shared.dpapi", "SHARED.DPAPI"):
            with self.subTest(filename=filename):
                providers = [self.provider(secret_file="shared.dpapi"), self.provider("bravo", secret_file=filename)]
                with self.assertRaisesRegex(registry.CredentialOwnershipError, "alpha.*bravo"):
                    registry.validate_registry({"version": 1, "providers": providers}, allow_missing_secrets=True)

    def test_separate_workspace_files_are_independent(self) -> None:
        providers = [self.provider(secret_file="shared.dpapi"), self.provider(
            "bravo", workspace="claude", protocols=["messages"], secret_file="shared.dpapi",
        )]
        clean = registry.validate_registry({"version": 1, "providers": providers}, allow_missing_secrets=True)
        self.assertEqual(len(clean["providers"]), 2)

    def test_add_or_edit_collision_is_rejected_before_encrypting(self) -> None:
        for editing in (False, True):
            for api_key in (None, "fixture-key"):
                with self.subTest(editing=editing, has_key=bool(api_key)):
                    first = self.provider(secret_file="shared.dpapi")
                    providers = [first, self.provider("bravo")] if editing else [first]
                    self.seed(providers)
                    original_registry = self.codex.registry_path.read_bytes()
                    original_secret = registry.secret_path(first).read_bytes()
                    candidate = self.provider("bravo", secret_file="SHARED.DPAPI")
                    with patch.object(registry, "dpapi_protect") as protect:
                        with self.assertRaises(registry.CredentialOwnershipError):
                            registry.apply_provider(candidate, api_key=api_key, workspace=self.codex, restart=False)
                    protect.assert_not_called()
                    self.assertEqual(self.codex.registry_path.read_bytes(), original_registry)
                    self.assertEqual(registry.secret_path(first).read_bytes(), original_secret)

    def test_legacy_shared_registry_remains_readable_and_metadata_editable(self) -> None:
        first, second = self.provider(secret_file="shared.dpapi"), self.provider("bravo", secret_file="shared.dpapi")
        self.seed([first, second, self.provider("charlie")])
        self.assertEqual(len(registry.load_registry(self.codex.registry_path)["providers"]), 3)
        for provider_id in ("alpha", "charlie"):
            changed = self.stored(provider_id)
            changed["name"] += " Edited"
            registry.apply_provider(changed, workspace=self.codex, restart=False)
            self.assertEqual(self.stored(provider_id)["name"], changed["name"])

    def test_legacy_shared_key_cannot_be_overwritten_in_place(self) -> None:
        first = self.provider(secret_file="shared.dpapi")
        self.seed([first, self.provider("bravo", secret_file="shared.dpapi")])
        with patch.object(registry, "dpapi_protect") as protect:
            with self.assertRaisesRegex(registry.CredentialOwnershipError, "still used"):
                registry.apply_provider(first, api_key="replacement", workspace=self.codex, restart=False)
        protect.assert_not_called()

    def test_legacy_shared_provider_can_move_to_its_own_new_key(self) -> None:
        first = self.provider(secret_file="shared.dpapi")
        self.seed([first, self.provider("bravo", secret_file="shared.dpapi")])
        original = registry.secret_path(first).read_bytes()
        first["secret_file"] = "independent.dpapi"

        def protect(_key, path, _entropy):
            path.write_bytes(b"synthetic-replacement-fixture")

        with patch.object(registry, "dpapi_protect", side_effect=protect):
            registry.apply_provider(first, api_key="fixture-key", workspace=self.codex, restart=False)
        self.assertEqual((self.codex.secrets_root / "shared.dpapi").read_bytes(), original)
        self.assertEqual(self.stored()["secret_file"], "independent.dpapi")

    def test_registry_write_allows_existing_group_but_not_added_claimants(self) -> None:
        value = self.seed([self.provider(secret_file="shared.dpapi"), self.provider("bravo", secret_file="shared.dpapi")])
        registry.write_registry(value, self.codex.registry_path)
        value["providers"].append(self.provider("charlie", secret_file="shared.dpapi"))
        with self.assertRaises(registry.CredentialOwnershipError):
            registry.write_registry(value, self.codex.registry_path)
        self.assertEqual(len(registry.load_registry(self.codex.registry_path)["providers"]), 2)

    def test_delete_one_of_three_shared_references_keeps_survivor_key(self) -> None:
        first = self.provider(secret_file="shared.dpapi")
        self.seed([first, self.provider("bravo", secret_file="SHARED.DPAPI"), self.provider("charlie", secret_file="shared.dpapi")])
        original = registry.secret_path(first).read_bytes()
        with patch.object(registry, "restart_router", return_value={"status": "ready"}):
            result = registry.delete_provider("BRAVO", workspace=self.codex, restart=True)
        self.assertEqual(result["secret_retained_for"], ["alpha", "charlie"])
        self.assertEqual(registry.secret_path(first).read_bytes(), original)
        self.assertFalse((Path(result["archive"]) / "SHARED.DPAPI").exists())
        self.assertEqual(len(registry.load_registry(self.codex.registry_path)["providers"]), 2)

    def test_delete_unique_reference_still_archives_its_key(self) -> None:
        second = self.provider("bravo")
        self.seed([self.provider(), second])
        secret = registry.secret_path(second)
        original = secret.read_bytes()
        result = registry.delete_provider("bravo", workspace=self.codex, restart=False)
        self.assertEqual(result["secret_retained_for"], [])
        self.assertFalse(secret.exists())
        self.assertEqual((Path(result["archive"]) / second["secret_file"]).read_bytes(), original)

    def test_unrelated_delete_does_not_disrupt_existing_shared_group(self) -> None:
        first = self.provider(secret_file="shared.dpapi")
        self.seed([first, self.provider("bravo", secret_file="shared.dpapi"), self.provider("charlie")])
        result = registry.delete_provider("charlie", workspace=self.codex, restart=False)
        self.assertEqual(result["secret_retained_for"], [])
        self.assertTrue(registry.secret_path(first).exists())
        self.assertEqual(len(registry.load_registry(self.codex.registry_path)["providers"]), 2)

    def test_unique_key_replacement_still_succeeds_and_rolls_back_on_catalog_failure(self) -> None:
        provider = self.provider()
        self.seed([provider])
        secret = registry.secret_path(provider)
        original_registry = self.codex.registry_path.read_bytes()
        original_secret = secret.read_bytes()

        def protect(_key, path, _entropy):
            path.write_bytes(b"synthetic-replacement-fixture")

        with patch.object(registry, "dpapi_protect", side_effect=protect):
            with patch.object(registry, "rebuild_catalog", side_effect=RuntimeError("fixture failure")):
                with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                    registry.apply_provider(provider, api_key="fixture-key", workspace=self.codex, restart=False)
            self.assertEqual(secret.read_bytes(), original_secret)
            self.assertEqual(self.codex.registry_path.read_bytes(), original_registry)
            registry.apply_provider(provider, api_key="fixture-key", workspace=self.codex, restart=False)
        self.assertEqual(secret.read_bytes(), b"synthetic-replacement-fixture")


class EndpointBookkeepingTests(RegistryFixture):
    def test_legacy_call_still_merges_only_volatile_model_fields(self) -> None:
        before = self.seed([self.provider()])["providers"][0]
        after = deepcopy(before)
        after["responses_path"] = "/v1/responses"
        after["models"][0].update(last_test_status="ready", enabled=False)
        result = registry.save_provider_bookkeeping([after], self.codex)
        stored = self.stored()
        self.assertEqual(stored["responses_path"], before["responses_path"])
        self.assertTrue(stored["models"][0]["enabled"])
        self.assertEqual(stored["models"][0]["last_test_status"], "ready")
        self.assertEqual(result["endpoint_updates"], [])

    def test_all_three_paths_persist_without_changing_other_provider_fields(self) -> None:
        before = self.seed([self.provider()])["providers"][0]
        after = deepcopy(before)
        after.update(responses_path="/v1/responses", messages_path="/messages", chat_path="/chat/completions", name="Ignored rename")
        after["models"][0]["last_test_status"] = "ready"
        result = registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"alpha": before})
        stored = self.stored()
        self.assertEqual(result["endpoint_updates"], ["alpha"])
        self.assertEqual(result["endpoint_conflicts"], [])
        for field in ("responses_path", "messages_path", "chat_path"):
            self.assertEqual(stored[field], after[field])
        self.assertEqual(stored["name"], before["name"])
        self.assertEqual(stored["base_url"], before["base_url"])
        self.assertEqual(stored["models"][0]["last_test_status"], "ready")

    def test_protected_provider_gets_bookkeeping_but_not_endpoint_changes(self) -> None:
        before = self.seed([self.provider(protected=True)])["providers"][0]
        after = deepcopy(before)
        after["responses_path"] = "/v1/responses"
        after["models"][0]["last_test_status"] = "ready"
        result = registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"alpha": before})
        self.assertEqual(result["endpoint_conflicts"], ["alpha"])
        self.assertEqual(result["endpoint_updates"], [])
        self.assertEqual(self.stored()["responses_path"], before["responses_path"])
        self.assertEqual(self.stored()["models"][0]["last_test_status"], "ready")

    def test_concurrent_routing_or_credential_identity_changes_are_not_overwritten(self) -> None:
        mutations = {
            "base_url": "https://new-fixture.invalid", "responses_path": "/manual/responses",
            "messages_path": "/manual/messages", "chat_path": "/manual/chat/completions",
            "auth_header": "X-Api-Key", "auth_prefix": "", "secret_file": "new-key.dpapi",
            "entropy": "fixture-new-entropy", "extra_headers": {"X-Fixture": "new"},
            "request_adapter": registry.RESPONSES_TO_CHAT_COMPLETIONS_ADAPTER,
            "models_path": "/v1/models", "prefix": "new-bravo--", "enabled": False,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                before = self.provider("bravo")
                current = deepcopy(before)
                current[field] = value
                self.seed([self.provider(), current])
                after = deepcopy(before)
                after["responses_path"] = "/v1/responses"
                result = registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"bravo": before})
                self.assertEqual(result["endpoint_conflicts"], ["bravo"])
                self.assertEqual(result["endpoint_updates"], [])
                self.assertEqual(self.stored("bravo")[field], value)

    def test_concurrent_cosmetic_or_model_changes_are_preserved_without_blocking_repair(self) -> None:
        before = self.provider()
        current = deepcopy(before)
        current["name"] = "New user label"
        current["models"][0]["display_name"] = "New model label"
        current["models"][0]["fast_tier_forced"] = True
        self.seed([current])
        after = deepcopy(before)
        after["responses_path"] = "/v1/responses"
        result = registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"alpha": before})
        self.assertEqual(result["endpoint_updates"], ["alpha"])
        stored = self.stored()
        self.assertEqual(stored["name"], current["name"])
        self.assertEqual(stored["models"][0]["display_name"], "New model label")
        self.assertTrue(stored["models"][0]["fast_tier_forced"])

    def test_source_probed_against_changed_base_url_cannot_apply_its_path(self) -> None:
        before = self.seed([self.provider()])["providers"][0]
        after = deepcopy(before)
        after.update(base_url="https://different-fixture.invalid", responses_path="/v1/responses")
        result = registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"alpha": before})
        self.assertEqual(result["endpoint_conflicts"], ["alpha"])
        self.assertEqual(self.stored()["base_url"], before["base_url"])
        self.assertEqual(self.stored()["responses_path"], before["responses_path"])

    def test_deleted_provider_is_reported_as_conflict_not_recreated(self) -> None:
        before = self.provider("bravo")
        self.seed([self.provider()])
        after = deepcopy(before)
        after["responses_path"] = "/v1/responses"
        result = registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"bravo": before})
        self.assertEqual(result["endpoint_conflicts"], ["bravo"])
        self.assertEqual(len(registry.load_registry(self.codex.registry_path)["providers"]), 1)

    def test_catalog_failure_rolls_back_registry_and_catalog_bytes(self) -> None:
        before = self.seed([self.provider()])["providers"][0]
        originals = {path: path.read_bytes() for path in (self.codex.registry_path, self.codex.catalog_path)}
        after = deepcopy(before)
        after["responses_path"] = "/v1/responses"

        def fail(workspace):
            workspace.catalog_path.write_text("fixture-partial-catalog", encoding="utf-8")
            raise RuntimeError("fixture catalog failure")

        with patch.object(registry, "rebuild_catalog", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "fixture catalog failure"):
                registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"alpha": before})
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_endpoint_transaction_reads_writes_and_rebuilds_under_same_lock(self) -> None:
        before = self.seed([self.provider()])["providers"][0]
        after = deepcopy(before)
        after["responses_path"] = "/v1/responses"
        held = False
        events: list[str] = []
        original_load, original_write, original_rebuild = registry.load_registry, registry.write_registry, registry.rebuild_catalog

        @contextmanager
        def lock(*, workspace):
            nonlocal held
            self.assertIs(workspace, self.codex)
            held = True
            events.append("lock")
            try:
                yield
            finally:
                held = False
                events.append("unlock")

        def checked(name, function):
            def invoke(*args, **kwargs):
                self.assertTrue(held, name)
                events.append(name)
                return function(*args, **kwargs)
            return invoke

        with patch.object(registry, "registry_write_lock", lock), \
             patch.object(registry, "load_registry", side_effect=checked("load", original_load)), \
             patch.object(registry, "write_registry", side_effect=checked("write", original_write)), \
             patch.object(registry, "rebuild_catalog", side_effect=checked("rebuild", original_rebuild)):
            registry.save_provider_bookkeeping([after], self.codex, endpoint_baselines={"alpha": before})
        self.assertEqual(events[0], "lock")
        self.assertEqual(events[-1], "unlock")
        self.assertLess(events.index("load"), events.index("write"))
        self.assertLess(events.index("write"), events.index("rebuild"))

    def test_bookkeeping_preserves_legacy_shared_credential_group(self) -> None:
        first = self.provider(secret_file="shared.dpapi")
        self.seed([first, self.provider("bravo", secret_file="shared.dpapi")])
        changed = self.stored()
        changed["models"][0]["last_test_status"] = "ready"
        registry.save_provider_bookkeeping([changed], self.codex)
        self.assertEqual(self.stored()["models"][0]["last_test_status"], "ready")
        self.assertEqual(self.stored("bravo")["secret_file"], "shared.dpapi")


class ChatPathTests(RegistryFixture):
    @staticmethod
    def chat_result() -> tuple:
        body = {"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        return 200, body, json.dumps(body)

    def test_candidate_paths_respect_base_v1_and_absolute_endpoints(self) -> None:
        provider = self.provider(base_url="https://fixture.invalid/v1", chat_path="/chat/completions", models_path="/v1/models")
        self.assertEqual(registry.candidate_chat_paths(provider), ["/chat/completions"])
        provider["chat_path"] = "/v1/chat/completions"
        self.assertEqual(registry.candidate_chat_paths(provider), ["/v1/chat/completions", "/chat/completions"])
        provider["chat_path"] = "https://explicit-fixture.invalid/custom/chat"
        self.assertEqual(registry.candidate_chat_paths(provider), [provider["chat_path"]])

    def test_chat_repair_uses_chat_payload_and_mutates_only_chat_path(self) -> None:
        provider = self.provider(chat_path="/chat/completions", request_adapter=registry.RESPONSES_TO_CHAT_COMPLETIONS_ADAPTER)
        before = deepcopy(provider)
        with patch.object(registry, "_http_json", side_effect=[(200, None, "<html>fixture</html>"), self.chat_result()]) as http:
            result = registry.auto_repair_chat_path(provider, temporary_key="fixture-key")
        self.assertTrue(result["changed"])
        self.assertEqual(provider["chat_path"], "/v1/chat/completions")
        self.assertEqual({key: value for key, value in provider.items() if key != "chat_path"},
                         {key: value for key, value in before.items() if key != "chat_path"})
        self.assertEqual([call.args[0] for call in http.call_args_list], [
            "https://fixture.invalid/chat/completions", "https://fixture.invalid/v1/chat/completions",
        ])
        for call in http.call_args_list:
            _url, method, headers, payload, timeout = call.args
            self.assertEqual(method, "POST")
            self.assertEqual(payload["messages"], [{"role": "user", "content": "Reply exactly OK"}])
            self.assertEqual(payload["model"], "fixture-model")
            self.assertEqual(payload["max_tokens"], 64)
            self.assertNotIn("input", payload)
            self.assertNotIn("max_output_tokens", payload)
            self.assertNotIn("anthropic-version", headers)
            self.assertLessEqual(timeout, 120)

    def test_base_v1_doubled_path_is_repaired_to_single_v1(self) -> None:
        provider = self.provider(base_url="https://fixture.invalid/v1")
        with patch.object(registry, "_http_json", side_effect=[(404, {}, "missing"), self.chat_result()]) as http:
            result = registry.auto_repair_chat_path(provider, temporary_key="fixture-key")
        self.assertTrue(result["changed"])
        self.assertEqual(http.call_args.args[0], "https://fixture.invalid/v1/chat/completions")
        self.assertEqual(provider["chat_path"], "/chat/completions")

    def test_claude_chat_adapter_selects_enabled_model_and_uses_chat_payload(self) -> None:
        provider = self.provider(
            "bravo", workspace="claude", protocols=["messages"],
            request_adapter=registry.MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER,
            timeout_seconds=900,
            models=[{"id": "disabled-fixture", "enabled": False}, {"id": "enabled-fixture", "enabled": True}],
        )
        with patch.object(registry, "_http_json", return_value=self.chat_result()) as http:
            result = registry.discover_chat_path(provider, temporary_key="fixture-key")
        self.assertTrue(result["ok"])
        self.assertEqual(http.call_args.args[3]["model"], "enabled-fixture")
        self.assertNotIn("anthropic-version", http.call_args.args[2])
        self.assertEqual(http.call_args.args[4], 120)

    def test_false_shaped_successes_do_not_select_an_endpoint(self) -> None:
        for body in ({"object": "response", "output": []}, {"type": "message", "content": []},
                     {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}):
            with self.subTest(body=body):
                provider = self.provider()
                with patch.object(registry, "_http_json", return_value=(200, body, json.dumps(body))):
                    result = registry.auto_repair_chat_path(provider, temporary_key="fixture-key")
                self.assertFalse(result["changed"])
                self.assertIsNone(result["after"])
                self.assertEqual(provider["chat_path"], "/v1/chat/completions")

    def test_absolute_failure_does_not_send_key_to_guessed_origins(self) -> None:
        provider = self.provider(chat_path="https://explicit-fixture.invalid/api/chat")
        with patch.object(registry, "_http_json", return_value=(404, {}, "missing")) as http:
            result = registry.discover_chat_path(provider, temporary_key="fixture-key")
        self.assertFalse(result["ok"])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[0], provider["chat_path"])

    def test_no_enabled_model_does_not_resolve_key_or_send_request(self) -> None:
        provider = self.provider(models=[{"id": "disabled-fixture", "enabled": False}])
        with patch.object(registry, "provider_key") as key, patch.object(registry, "_http_json") as http:
            result = registry.discover_chat_path(provider)
        self.assertFalse(result["ok"])
        key.assert_not_called()
        http.assert_not_called()

    def test_already_correct_path_is_unchanged(self) -> None:
        provider = self.provider()
        before = deepcopy(provider)
        with patch.object(registry, "_http_json", return_value=self.chat_result()):
            result = registry.auto_repair_chat_path(provider, temporary_key="fixture-key")
        self.assertFalse(result["changed"])
        self.assertEqual(result["after"], provider["chat_path"])
        self.assertEqual(provider, before)

    def test_failure_details_redact_synthetic_probe_key(self) -> None:
        provider = self.provider()
        with patch.object(registry, "_http_json", side_effect=RuntimeError("bad fixture-secret-key value")):
            result = registry.discover_chat_path(provider, temporary_key="fixture-secret-key")
        self.assertFalse(result["ok"])
        self.assertNotIn("fixture-secret-key", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
