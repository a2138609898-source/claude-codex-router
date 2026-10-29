"""Isolated Manager/launcher/Claude regressions. Run through run_guarded_tests.py."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import CodexSotaManager as manager
import claude_desktop as cd
from codex_launch_controller import LaunchAttempt
import release_claude_slot_after_exit as watcher


def provider(name="fixture", *, workspace="codex", adapter="", auth_type="dpapi"):
    return {
        "id": name, "name": name, "workspace": workspace,
        "base_url": "https://fixture.invalid", "prefix": name + "--",
        "enabled": True, "protected": False, "is_default": True,
        "auth_type": auth_type, "secret_file": name + ".dpapi",
        "entropy": "synthetic-fixture", "request_adapter": adapter,
        "protocols": ["messages"] if workspace == "claude" else ["responses"],
        "responses_path": "/responses", "messages_path": "/v1/messages",
        "chat_path": "/wrong-chat", "extra_headers": {},
        "models": [{"id": "fixture-model", "enabled": True}],
    }


class ManagerHardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="manager-hardening-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = SimpleNamespace(
            root=self.root, registry_path=self.root / "providers.json",
            catalog_path=self.root / "catalog.json",
        )

    def test_restore_rolls_back_registry_and_partial_catalog(self):
        self.workspace.registry_path.write_bytes(b"old registry")
        self.workspace.catalog_path.write_bytes(b"old catalog")

        def rebuild(_workspace):
            self.workspace.catalog_path.write_bytes(b"partial catalog")
            raise RuntimeError("synthetic catalog failure")

        with mock.patch.object(manager, "registry_write_lock", return_value=nullcontext()), \
             mock.patch.object(manager, "write_registry", side_effect=lambda *_a, **_k:
                               self.workspace.registry_path.write_bytes(b"new registry")), \
             mock.patch.object(manager, "rebuild_catalog", side_effect=rebuild):
            with self.assertRaisesRegex(RuntimeError, "synthetic catalog failure"):
                manager.restore_registry_transaction({"providers": []}, self.workspace)
        self.assertEqual(self.workspace.registry_path.read_bytes(), b"old registry")
        self.assertEqual(self.workspace.catalog_path.read_bytes(), b"old catalog")
        backups = list((self.root / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "providers.json").read_bytes(), b"old registry")

    def test_restore_failure_removes_only_new_catalog(self):
        self.workspace.registry_path.write_bytes(b"old registry")

        def rebuild(_workspace):
            self.workspace.catalog_path.write_bytes(b"new catalog")
            raise OSError("synthetic write failure")

        with mock.patch.object(manager, "registry_write_lock", return_value=nullcontext()), \
             mock.patch.object(manager, "write_registry", side_effect=lambda *_a, **_k:
                               self.workspace.registry_path.write_bytes(b"new registry")), \
             mock.patch.object(manager, "rebuild_catalog", side_effect=rebuild):
            with self.assertRaises(OSError):
                manager.restore_registry_transaction({}, self.workspace)
        self.assertFalse(self.workspace.catalog_path.exists())
        self.assertEqual(self.workspace.registry_path.read_bytes(), b"old registry")

    def test_restore_success_keeps_backup_and_new_files(self):
        self.workspace.registry_path.write_bytes(b"old registry")
        self.workspace.catalog_path.write_bytes(b"old catalog")
        with mock.patch.object(manager, "registry_write_lock", return_value=nullcontext()), \
             mock.patch.object(manager, "write_registry", side_effect=lambda *_a, **_k:
                               self.workspace.registry_path.write_bytes(b"new registry")), \
             mock.patch.object(manager, "rebuild_catalog", return_value={"models": ["fixture"]}):
            result = manager.restore_registry_transaction({}, self.workspace)
        self.assertEqual(self.workspace.registry_path.read_bytes(), b"new registry")
        self.assertEqual((result["backup"] / "providers.json").read_bytes(), b"old registry")

    def test_restore_rollback_failure_identifies_recovery_backup(self):
        self.workspace.registry_path.write_bytes(b"old registry")
        with mock.patch.object(manager, "registry_write_lock", return_value=nullcontext()), \
             mock.patch.object(manager, "write_registry", side_effect=lambda *_a, **_k:
                               self.workspace.registry_path.write_bytes(b"new registry")), \
             mock.patch.object(manager, "rebuild_catalog", side_effect=OSError("catalog failed")), \
             mock.patch.object(manager, "restore_config_snapshot", side_effect=OSError("rollback failed")):
            with self.assertRaises(RuntimeError) as raised:
                manager.restore_registry_transaction({}, self.workspace)
        backup = next((self.root / "backups").iterdir())
        self.assertIn(str(backup), str(raised.exception))
        self.assertEqual((backup / "providers.json").read_bytes(), b"old registry")

    def test_import_new_codex_auth_requires_independent_key(self):
        incoming = provider(auth_type="codex_auth")
        incoming["protected"] = True
        incoming["secret_file"] = "another-provider.dpapi"
        with mock.patch.object(manager, "secret_path", return_value=self.root / "missing-key"):
            candidate, report = manager.merge_restored_registry(
                {"version": 1, "providers": [incoming]}, {"providers": []}, "codex"
            )
        actual = candidate["providers"][0]
        self.assertEqual(actual["auth_type"], "dpapi")
        self.assertFalse(actual["protected"])
        self.assertNotEqual(actual["secret_file"], incoming["secret_file"])
        self.assertIn("-import-", actual["secret_file"])
        self.assertEqual(report["needs_key"], ["fixture"])
        self.assertEqual(report["independent_key_required"], ["fixture"])
        self.assertEqual(incoming["auth_type"], "codex_auth")

    def test_import_cannot_upgrade_existing_dpapi_to_shared_login(self):
        old = provider()
        incoming = {**old, "auth_type": "codex_auth", "base_url": "https://other.invalid"}
        with mock.patch.object(manager, "secret_path", return_value=self.root / "missing"):
            candidate, report = manager.merge_restored_registry(
                {"providers": [incoming]}, {"providers": [old]}, "codex"
            )
        self.assertEqual(candidate["providers"][0]["auth_type"], "dpapi")
        self.assertNotEqual(candidate["providers"][0]["secret_file"], old["secret_file"])
        self.assertEqual(report["needs_key"], ["fixture"])

    def test_existing_borrowed_identity_survives_restore_with_or_without_protected(self):
        for protected in (False, True):
            with self.subTest(protected=protected):
                old = provider(auth_type="codex_auth")
                old["protected"] = protected
                incoming = {**deepcopy(old), "base_url": "https://other.invalid",
                            "responses_path": "/changed", "name": "new label"}
                candidate, report = manager.merge_restored_registry(
                    {"providers": [incoming]}, {"providers": [old]}, "codex"
                )
                actual = candidate["providers"][0]
                self.assertEqual(actual["base_url"], old["base_url"])
                self.assertEqual(actual["responses_path"], old["responses_path"])
                self.assertEqual(actual["auth_type"], "codex_auth")
                self.assertEqual(actual["name"], "new label")
                self.assertEqual(report["needs_key"], [])

    def test_request_adapter_external_edit_is_visible(self):
        old = provider()
        new = {**old, "request_adapter": "responses_to_chat_completions"}
        app = SimpleNamespace(loaded_provider=old, _disk_provider=lambda _id: new)
        self.assertIn("协议适配器", manager.CodexSotaApp._outside_edits(app, "fixture"))

    def test_chat_bridges_repair_chat_protocol_only(self):
        for workspace, adapter in (
            ("codex", "responses_to_chat_completions"),
            ("claude", "messages_to_chat_completions"),
        ):
            with self.subTest(workspace=workspace), \
                 mock.patch.object(manager, "auto_repair_chat_path",
                                   return_value={"changed": False}) as chat, \
                 mock.patch.object(manager, "auto_repair_messages_path") as messages, \
                 mock.patch.object(manager, "auto_repair_responses_path") as responses:
                actual = manager.auto_repair_active_inference_path(
                    provider(workspace=workspace, adapter=adapter), "synthetic-key"
                )
                self.assertEqual(actual["protocol"], "chat")
                chat.assert_called_once()
                messages.assert_not_called()
                responses.assert_not_called()

    def test_chat_repair_updates_only_chat_draft(self):
        app = SimpleNamespace(chat_path_var=mock.Mock(), responses_path_var=mock.Mock(),
                              messages_path_var=mock.Mock())
        label = manager.CodexSotaApp._apply_inference_path_repair(
            app, {"protocol": "chat", "after": "/chat/completions"}
        )
        self.assertEqual(label, "Chat Completions")
        app.chat_path_var.set.assert_called_once_with("/chat/completions")
        app.responses_path_var.set.assert_not_called()
        app.messages_path_var.set.assert_not_called()

    def run_checkup(self, *, conflict=False):
        source = provider(adapter="responses_to_chat_completions")
        outputs = []
        app = SimpleNamespace(
            _busy=False, workspace=self.workspace, _confirm_discarding_edits=lambda _: True,
            _show_error=mock.Mock(), _checkup_finished=mock.Mock(),
            _run_task=lambda _label, worker, _success: outputs.append(worker()),
        )

        def repair(value):
            value["chat_path"] = "/chat/completions"
            return {"changed": True, "protocol": "chat", "reason": "fixture repaired"}

        def save(values, workspace, *, endpoint_baselines):
            self.assertIs(workspace, self.workspace)
            self.assertEqual(endpoint_baselines["fixture"]["chat_path"], "/wrong-chat")
            self.assertEqual(values[0]["chat_path"], "/chat/completions")
            return {"providers": ["fixture"],
                    "endpoint_updates": [] if conflict else ["fixture"],
                    "endpoint_conflicts": ["fixture"] if conflict else []}

        with mock.patch.object(manager, "load_registry", return_value={"providers": [source]}), \
             mock.patch.object(manager.messagebox, "askokcancel", return_value=True), \
             mock.patch.object(manager, "auto_repair_active_inference_path", side_effect=repair), \
             mock.patch.object(manager, "test_model", return_value={"ok": False, "status": 500}), \
             mock.patch.object(manager, "save_provider_bookkeeping", side_effect=save):
            manager.CodexSotaApp._run_full_checkup(app)
        return outputs[0]

    def test_checkup_persists_endpoint_baseline_separately(self):
        result = self.run_checkup()
        self.assertEqual(result["repairs"], ["fixture：fixture repaired"])
        self.assertEqual(result["failed_saves"], [])

    def test_checkup_does_not_claim_conflicting_endpoint_was_saved(self):
        result = self.run_checkup(conflict=True)
        self.assertEqual(result["repairs"], [])
        self.assertEqual(result["repair_conflicts"], ["fixture"])

    def test_claude_panel_counts_claude_registry_from_codex_workspace(self):
        claude = provider(workspace="claude")
        claude["models"] = [{"id": f"model-{index}", "enabled": True} for index in range(21)]
        app = SimpleNamespace(
            workspace=manager.CODEX, registry={"providers": [provider()]},
            claude_status_var=mock.Mock(), claude_entry_combo=mock.Mock(),
            _reconcile_claude_slot=lambda: None, _claude_slot_note=lambda: "",
        )
        with mock.patch.object(manager, "migrate_legacy_entry"), \
             mock.patch.object(manager, "claude_library_status", return_value={
                 "available": True, "entries": [], "applied_name": "fixture"}), \
             mock.patch.object(manager, "load_registry",
                               return_value={"providers": [claude]}) as load:
            manager.CodexSotaApp._refresh_claude_status(app)
        load.assert_called_once_with(manager.CLAUDE.registry_path, allow_missing_secrets=True)
        self.assertIn("Messages 模型：21 个", app.claude_status_var.set.call_args.args[0])

    def test_watcher_command_carries_claim_generation(self):
        (self.root / "release_claude_slot_after_exit.py").write_text("# fixture", encoding="utf-8")
        with mock.patch.object(manager, "CORE_ROOT", self.root), \
             mock.patch.object(manager, "resolve_helper_python", return_value=self.root / "python.exe"), \
             mock.patch.object(manager.subprocess, "Popen", return_value=SimpleNamespace(pid=7)) as start:
            result = manager.arm_claude_slot_release("fixture-generation")
        self.assertEqual(result["status"], "armed")
        self.assertEqual(start.call_args.args[0][-2:], ["--claim-id", "fixture-generation"])


class LaunchVerificationHardeningTests(unittest.TestCase):
    def attempt(self, inspect):
        now = [100.0]
        attempt = LaunchAttempt(["fixture-launcher"], Path(tempfile.gettempdir()),
                                Path(tempfile.gettempdir()), inspect,
                                request_id="abcd", clock=lambda: now[0], popen=mock.Mock())
        attempt.completed_result = {"status": "ready", "window_owned": True,
                                    "app_process_id": 42, "app_creation_ticks": "123"}
        attempt.completed_at = now[0]
        return attempt, now

    def test_persistent_verification_exception_is_bounded_without_respawn(self):
        attempt, now = self.attempt(mock.Mock(side_effect=OSError("fixture inspection failed")))
        self.assertEqual(attempt.poll()["phase"], "verifying_window")
        now[0] += 15.1
        self.assertEqual(attempt.poll()["status"], "error")
        self.assertIn("可能仍在运行", attempt.poll()["error"])
        attempt.popen.assert_not_called()

    def test_transient_verification_failure_can_recover(self):
        inspect = mock.Mock(side_effect=[
            RuntimeError("transient"), [{"pid": 42, "role": "desktop",
                                        "window_handle": 1, "creation_ticks": "123"}],
        ])
        attempt, now = self.attempt(inspect)
        self.assertEqual(attempt.poll()["status"], "starting")
        now[0] += 1
        self.assertEqual(attempt.poll()["status"], "ready")
        attempt.popen.assert_not_called()

    def test_malformed_reported_pid_also_expires(self):
        attempt, now = self.attempt(mock.Mock())
        attempt.inspect_process = mock.Mock()
        attempt.completed_result["app_process_id"] = "invalid-pid"
        self.assertEqual(attempt.poll()["status"], "starting")
        now[0] += 16
        self.assertEqual(attempt.poll()["status"], "error")
        attempt.inspect_process.assert_not_called()


class ClaudeSlotHardeningTests(unittest.TestCase):
    OWNER = "00000000-0000-4000-8000-000000000001"
    OTHER = "00000000-0000-4000-8000-000000000002"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-slot-hardening-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "Claude-3p" / "configLibrary"
        self.library.mkdir(parents=True)
        self.meta_path = self.library / "_meta.json"
        self.claim_path = self.root / "claim.json"
        values = {
            "CLAUDE_3P_ROOT": self.library.parent, "CONFIG_LIBRARY": self.library,
            "META_PATH": self.meta_path, "BACKUP_ROOT": self.root / "backups",
            "LIBRARY_LOCK_PATH": self.root / "library.lock", "SLOT_CLAIM_PATH": self.claim_path,
        }
        self.addCleanup(mock.patch.stopall)
        for name, value in values.items():
            mock.patch.object(cd, name, value).start()
        mock.patch.object(cd, "_migrate_legacy_3p_root",
                          return_value={"status": "not-needed"}).start()
        self.write(self.library.parent / "claude_desktop_config.json", {"deploymentMode": "3p"})
        self.write(self.meta_path, {"appliedId": self.OWNER, "entries": [
            {"id": self.OWNER, "name": "Fixture owner"},
            {"id": self.OTHER, "name": "Fixture other"},
            {"id": cd.SOTA_ENTRY_ID, "name": cd.SOTA_ENTRY_NAME},
        ]})
        for entry in (self.OWNER, self.OTHER, cd.SOTA_ENTRY_ID):
            self.write(self.library / (entry + ".json"), {"fixture": entry})

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def applied(self):
        return json.loads(self.meta_path.read_text(encoding="utf-8"))["appliedId"]

    def fail_claim_write(self):
        original = cd._atomic_write_json

        def write(path, data):
            if path == self.claim_path:
                raise OSError("fixture claim write failure")
            return original(path, data)
        return mock.patch.object(cd, "_atomic_write_json", side_effect=write)

    def test_claim_record_precedes_applied_id_commit(self):
        original = cd._atomic_write_json

        def write(path, data):
            if path == self.meta_path and data.get("appliedId") == cd.SOTA_ENTRY_ID:
                self.assertEqual(cd.read_slot_claim()["previous_applied_id"], self.OWNER)
            return original(path, data)

        with mock.patch.object(cd, "_atomic_write_json", side_effect=write):
            result = cd.claim_slot()
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertTrue(result["claim"]["claim_id"])

    def test_failed_claim_never_changes_applied_profile(self):
        before = self.meta_path.read_bytes()
        with self.fail_claim_write(), self.assertRaises(OSError):
            cd.claim_slot()
        self.assertEqual(self.meta_path.read_bytes(), before)
        self.assertFalse(self.claim_path.exists())

    def test_apply_entry_claim_failure_rolls_back(self):
        with self.fail_claim_write(), self.assertRaises(OSError):
            cd.apply_entry(cd.SOTA_ENTRY_ID)
        self.assertEqual(self.applied(), self.OWNER)
        self.assertFalse(self.claim_path.exists())

    def test_profile_apply_claim_failure_restores_profile_too(self):
        profile_path = self.library / (cd.SOTA_ENTRY_ID + ".json")
        before = profile_path.read_bytes()
        with self.fail_claim_write(), self.assertRaises(OSError):
            cd.write_profile([{"name": "fixture-model"}], apply=True)
        self.assertEqual(self.applied(), self.OWNER)
        self.assertEqual(profile_path.read_bytes(), before)
        self.assertFalse(self.claim_path.exists())

    def test_normal_publish_does_not_claim_slot(self):
        cd.write_profile([{"name": "fixture-model"}])
        self.assertEqual(self.applied(), self.OWNER)
        self.assertFalse(self.claim_path.exists())

    def test_failed_index_commit_restores_prior_claim(self):
        old_claim = {"previous_applied_id": self.OTHER, "fixture": "old"}
        self.write(self.claim_path, old_claim)
        original = cd._atomic_write_json

        def write(path, data):
            if path == self.meta_path:
                raise OSError("fixture index failure")
            return original(path, data)

        with mock.patch.object(cd, "_atomic_write_json", side_effect=write), self.assertRaises(OSError):
            cd.claim_slot()
        self.assertEqual(self.applied(), self.OWNER)
        self.assertEqual(cd.read_slot_claim(), old_claim)

    def test_stale_watcher_cannot_release_new_generation(self):
        first = cd.claim_slot()["claim"]["claim_id"]
        second = cd.claim_slot()["claim"]["claim_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(cd.release_slot(expected_claim_id=first)["status"], "superseded")
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertEqual(cd.read_slot_claim()["claim_id"], second)

    def test_release_rereads_claim_only_inside_lock(self):
        token = cd.claim_slot()["claim"]["claim_id"]
        original_lock, original_read = cd._library_lock, cd.read_slot_claim
        locked = [False]

        @contextmanager
        def lock(*args, **kwargs):
            with original_lock(*args, **kwargs):
                locked[0] = True
                try:
                    yield
                finally:
                    locked[0] = False

        def read():
            self.assertTrue(locked[0])
            return original_read()

        with mock.patch.object(cd, "_library_lock", lock), \
             mock.patch.object(cd, "read_slot_claim", side_effect=read):
            result = cd.release_slot(expected_claim_id=token)
        self.assertEqual(result["status"], "released")
        self.assertEqual(self.applied(), self.OWNER)

    def test_release_claim_unlink_failure_rolls_index_back(self):
        cd.claim_slot()
        before = self.claim_path.read_bytes()
        with mock.patch.object(cd, "_clear_slot_claim", side_effect=OSError("fixture unlink failure")), \
             self.assertRaises(OSError):
            cd.release_slot()
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertEqual(self.claim_path.read_bytes(), before)

    def test_legacy_claim_without_token_is_still_recoverable(self):
        meta = cd.read_meta()
        meta["appliedId"] = cd.SOTA_ENTRY_ID
        self.write(self.meta_path, meta)
        legacy = {"version": 1, "owner": "codex-sota", "previous_applied_id": self.OWNER,
                  "claimed_at": "synthetic-old-claim", "claimed_pids": []}
        self.write(self.claim_path, legacy)
        result = cd.release_slot(expected_claim_id=cd.slot_claim_token(legacy))
        self.assertEqual(result["status"], "released")
        self.assertEqual(self.applied(), self.OWNER)

    def test_pending_launch_is_not_reconciled_before_process_exists(self):
        claim = cd.claim_slot(launch_pending=True)["claim"]
        self.assertEqual(cd.reconcile_slot(claude_running=False)["status"], "launch-pending")
        self.assertEqual(cd.read_slot_claim()["claim_id"], claim["claim_id"])

    def test_pending_launch_remains_protected_for_full_manager_wait(self):
        with mock.patch.object(cd.time, "time", return_value=1000.0):
            claim = cd.claim_slot(
                launch_pending=True,
                launch_timeout_seconds=manager.LAUNCH_WINDOW_WAIT_SECONDS + 120.0,
            )["claim"]
        self.assertEqual(claim["launch_deadline"], 1420.0)
        with mock.patch.object(cd.time, "time", return_value=1350.0):
            self.assertEqual(cd.reconcile_slot(claude_running=False)["status"], "launch-pending")
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)

    def test_invalid_pending_timeout_does_not_mutate_deployment_or_claim(self):
        for value in (None, 0, -1, float("nan"), float("inf"), 3601, "bad"):
            with self.subTest(value=value), \
                 mock.patch.object(cd, "ensure_deployment_mode") as deployment, \
                 self.assertRaises(ValueError):
                cd.claim_slot(launch_pending=True, launch_timeout_seconds=value)
            deployment.assert_not_called()
            self.assertFalse(self.claim_path.exists())
            self.assertEqual(self.applied(), self.OWNER)

    def test_none_and_omitted_optional_claim_arguments_keep_legacy_behavior(self):
        for arguments in ({}, {"pids": None}):
            with self.subTest(arguments=arguments):
                claim = cd.claim_slot(**arguments)["claim"]
                self.assertEqual(claim["claimed_pids"], [])
                self.assertNotIn("launch_deadline", claim)
                self.assertEqual(claim["previous_applied_id"], self.OWNER)
        for arguments in ({}, {"expected_claim_id": None}):
            with self.subTest(arguments=arguments):
                cd.claim_slot()
                self.assertEqual(cd.release_slot(**arguments)["status"], "released")
                self.assertEqual(self.applied(), self.OWNER)

    def test_empty_expected_generation_is_not_an_unscoped_release(self):
        claim = cd.claim_slot()["claim"]
        self.assertEqual(cd.release_slot(expected_claim_id="")["status"], "superseded")
        self.assertEqual(cd.read_slot_claim()["claim_id"], claim["claim_id"])

    def test_crashed_pending_launch_can_be_recovered_after_deadline(self):
        claim = cd.claim_slot(launch_pending=True)["claim"]
        with mock.patch.object(cd.time, "time", return_value=claim["launch_deadline"] + 1):
            self.assertEqual(cd.reconcile_slot(claude_running=False)["status"], "released")
        self.assertEqual(self.applied(), self.OWNER)

    def test_confirmation_keeps_generation_and_records_desktop_pids(self):
        claim = cd.claim_slot(launch_pending=True)["claim"]
        result = cd.confirm_slot_claim(claim["claim_id"], pids=[9, 8, 9])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["claim"]["claimed_pids"], [8, 9])
        self.assertNotIn("launch_deadline", result["claim"])
        self.assertEqual(result["claim"]["claim_id"], claim["claim_id"])

    def test_failed_confirmation_keeps_the_recoverable_pending_claim(self):
        claim = cd.claim_slot(launch_pending=True)["claim"]
        with self.fail_claim_write(), self.assertRaises(OSError):
            cd.confirm_slot_claim(claim["claim_id"], pids=[9])
        self.assertEqual(cd.read_slot_claim(), claim)
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)

    def test_final_running_check_blocks_release(self):
        cd.claim_slot()
        self.assertEqual(cd.release_slot(is_claude_running=lambda: True)["status"], "claude-running")
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)

    def test_manual_force_release_remains_available(self):
        cd.claim_slot(launch_pending=True)
        meta = cd.read_meta()
        meta["appliedId"] = self.OTHER
        self.write(self.meta_path, meta)
        self.assertEqual(cd.release_slot(force=True)["status"], "released")
        self.assertEqual(self.applied(), self.OWNER)

    def test_manual_force_still_respects_explicit_generation(self):
        first = cd.claim_slot()["claim"]["claim_id"]
        second = cd.claim_slot()["claim"]["claim_id"]
        self.assertEqual(cd.release_slot(force=True, expected_claim_id=first)["status"], "superseded")
        self.assertEqual(cd.slot_state()["claim_id"], second)
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)

    def test_manager_manual_dialog_cannot_release_a_newer_claim(self):
        first = cd.claim_slot()["claim"]["claim_id"]
        app = SimpleNamespace(
            _busy=False, _show_error=mock.Mock(), _append_log=mock.Mock(),
            _refresh_claude_status=mock.Mock(),
            _run_task=lambda _label, worker, success: success(worker()),
        )

        def confirm(*_args, **_kwargs):
            cd.claim_slot()
            return True

        with mock.patch.object(manager.messagebox, "askokcancel", side_effect=confirm), \
             mock.patch.object(manager.messagebox, "showinfo") as notice:
            manager.CodexSotaApp._release_claude_slot(app)
        self.assertNotEqual(cd.read_slot_claim()["claim_id"], first)
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertIn("确认期间", notice.call_args.args[1])

    def test_failed_start_can_release_pending_generation_immediately(self):
        claim = cd.claim_slot(launch_pending=True)["claim"]
        result = cd.release_slot(expected_claim_id=claim["claim_id"], allow_pending=True)
        self.assertEqual(result["status"], "released")

    def test_external_slot_change_during_claim_is_not_overwritten(self):
        original = cd._set_applied_locked

        def change(entry_id, **kwargs):
            meta = cd.read_meta()
            meta["appliedId"] = self.OTHER
            self.write(self.meta_path, meta)
            return original(entry_id, **kwargs)

        with mock.patch.object(cd, "_set_applied_locked", side_effect=change), \
             self.assertRaises(cd.ConcurrentClaudeConfigUpdate):
            cd.claim_slot()
        self.assertEqual(self.applied(), self.OTHER)
        self.assertFalse(self.claim_path.exists())

    def test_watcher_keeps_initial_generation_after_wait(self):
        initial = cd.claim_slot()["claim"]["claim_id"]

        def wait(_seconds):
            cd.claim_slot()
            return True

        with mock.patch.object(watcher.sys, "argv", ["watcher", "--claim-id", initial]), \
             mock.patch.object(watcher, "setup_logging"), \
             mock.patch.object(watcher, "wait_until_claude_exits", side_effect=wait), \
             mock.patch.object(watcher, "claude_pids", return_value=set()):
            self.assertEqual(watcher.main(), 0)
        self.assertNotEqual(cd.read_slot_claim()["claim_id"], initial)
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)

    def test_watcher_stale_at_start_does_not_wait_or_release(self):
        cd.claim_slot()
        with mock.patch.object(watcher.sys, "argv", ["watcher", "--claim-id", "old-generation"]), \
             mock.patch.object(watcher, "setup_logging"), \
             mock.patch.object(watcher, "wait_until_claude_exits") as wait, \
             mock.patch.object(cd, "release_slot") as release:
            self.assertEqual(watcher.main(), 0)
        wait.assert_not_called()
        release.assert_not_called()

    def run_manager_launch(
        self, *, failing=False, window=True, pids=None, query_failure=None,
        show_success=False, before_window=None,
    ):
        def run_task(_label, worker, success):
            self.launch_result = worker()
            if show_success:
                success(self.launch_result)

        def inspect_window():
            if before_window is not None:
                before_window()
            if query_failure is not None:
                raise query_failure
            return window

        checks = iter([False, inspect_window])

        def next_window():
            result = next(checks)
            return result() if callable(result) else result

        app = SimpleNamespace(
            registry={"providers": [provider(workspace="claude")]},
            _ensure_router_running=mock.Mock(return_value=True),
            _run_task=run_task, _append_log=mock.Mock(), _refresh_claude_status=mock.Mock(),
            header_status_var=mock.Mock(),
        )
        target = {"kind": "path", "value": str(self.root / "claude.exe"), "label": "fixture"}
        processes = pids if pids is not None else (set() if failing else {123})
        with mock.patch.object(manager, "claude_window_present", side_effect=next_window), \
             mock.patch.object(manager, "resolve_claude_launch_target", return_value=target), \
             mock.patch.object(manager, "close_running_claude", return_value={}), \
             mock.patch.object(manager.messagebox, "askokcancel", return_value=True), \
             mock.patch.object(manager.messagebox, "showwarning") as warning, \
             mock.patch.object(manager, "LAUNCH_WINDOW_WAIT_SECONDS", 300 if window else 0), \
             mock.patch.object(manager, "claude_pids", return_value=processes,
                               side_effect=processes if isinstance(processes, Exception) else None), \
             mock.patch.object(manager, "start_claude_process",
                               side_effect=OSError("fixture launch failure") if failing else None,
                               return_value={"returncode": 0, "detail": ""}), \
             mock.patch.object(manager, "arm_claude_slot_release",
                               return_value={"status": "armed"}) as arm:
            manager.CodexSotaApp._launch_claude(app)
        self.launch_warnings = warning
        return arm

    def test_manager_confirms_and_arms_exact_claim_generation(self):
        arm = self.run_manager_launch()
        claim = cd.read_slot_claim()
        arm.assert_called_once_with(claim["claim_id"])
        self.assertNotIn("launch_deadline", claim)
        self.assertEqual(claim["claimed_pids"], [123])

    def test_manager_launch_failure_returns_pending_slot(self):
        with self.assertRaisesRegex(OSError, "fixture launch failure"):
            self.run_manager_launch(failing=True)
        self.assertEqual(self.applied(), self.OWNER)
        self.assertFalse(self.claim_path.exists())

    def test_manager_query_failure_after_launch_preserves_running_slot(self):
        with self.assertRaisesRegex(RuntimeError, "未改动当前生效档"):
            self.run_manager_launch(query_failure=PermissionError("fixture window query failed"))
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertTrue(cd.read_slot_claim()["claim_id"])

    def test_manager_launch_error_with_unknown_process_state_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "未能安全确认"):
            self.run_manager_launch(failing=True, pids=PermissionError("fixture identity denied"))
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertTrue(cd.read_slot_claim()["claim_id"])

    def test_manager_no_window_but_running_desktop_is_not_falsely_reported_released(self):
        arm = self.run_manager_launch(window=False, show_success=True)
        self.assertEqual(self.launch_result["watcher"]["detail"]["status"], "claude-running")
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertTrue(cd.read_slot_claim()["claim_id"])
        self.assertNotIn("已经交还", self.launch_warnings.call_args.args[1])
        self.assertIn("仍在运行", self.launch_warnings.call_args.args[1])
        arm.assert_not_called()

    def test_manager_no_window_and_unknown_process_identity_preserves_claim(self):
        self.run_manager_launch(window=False, show_success=True,
                                pids=PermissionError("fixture identity denied"))
        self.assertEqual(self.launch_result["watcher"]["detail"]["status"], "failed")
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)
        self.assertTrue(cd.read_slot_claim()["claim_id"])
        self.assertIn("未能确认自动交还成功", self.launch_warnings.call_args.args[1])

    def test_manager_confirmation_cannot_clear_a_replacement_generation(self):
        replacement = []

        def replace_claim():
            replacement.append(cd.claim_slot(launch_pending=True)["claim"]["claim_id"])

        with self.assertRaisesRegex(RuntimeError, "借用记录已更新"):
            self.run_manager_launch(before_window=replace_claim)
        self.assertEqual(cd.read_slot_claim()["claim_id"], replacement[0])
        self.assertEqual(self.applied(), cd.SOTA_ENTRY_ID)


class ClaudeProcessIdentityTests(unittest.TestCase):
    def test_desktop_paths_accept_supported_installs_but_not_cli(self):
        cases = {
            r"C:\Local\AnthropicClaude\claude.exe": True,
            r"C:\LOCAL\AnthropicClaude\app-1.2.3\Claude.exe": True,
            r"C:\Program Files\WindowsApps\Claude_1.2.3_x64__pzs8sxrjxfjjc\claude.exe": True,
            r"C:\Local\AnthropicClaudeCode\claude.exe": False,
            r"C:\Users\Fixture\.local\bin\claude.exe": False,
            r"C:\Program Files\WindowsApps\ClaudeCode_1.2_x64__pzs8sxrjxfjjc\claude.exe": False,
            r"C:\Local\AnthropicClaude\other.exe": False,
            r"AnthropicClaude\claude.exe": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(cd.is_claude_desktop_executable(
                    path, local_appdata=r"C:\Local", program_files=r"C:\Program Files"
                ), expected)

    def test_cli_does_not_keep_desktop_slot_alive(self):
        paths = {1: r"C:\Local\AnthropicClaude\claude.exe",
                 2: r"C:\Users\Fixture\.local\bin\claude.exe", 3: None}
        with mock.patch.dict(cd.os.environ, {"LOCALAPPDATA": r"C:\Local"}):
            self.assertEqual(cd.claude_desktop_pids(candidate_pids=paths, image_path=paths.get), {1})
            self.assertEqual(cd.claude_desktop_pids(candidate_pids={2, 3}, image_path=paths.get), set())

    def test_unknown_process_identity_fails_closed(self):
        with self.assertRaises(PermissionError):
            cd.claude_desktop_pids(candidate_pids={1},
                                  image_path=mock.Mock(side_effect=PermissionError("fixture denied")))

    def test_process_exit_during_native_path_query_is_distinguished_from_access_denial(self):
        import ctypes

        for exited, query_exit_success in ((True, True), (False, True), (True, False)):
            def exit_status(_handle, result):
                result._obj.value = 0 if exited else 259
                return query_exit_success

            kernel = SimpleNamespace(
                OpenProcess=mock.Mock(return_value=123),
                QueryFullProcessImageNameW=mock.Mock(return_value=False),
                GetExitCodeProcess=mock.Mock(side_effect=exit_status),
                CloseHandle=mock.Mock(),
            )
            with self.subTest(exited=exited, query_exit_success=query_exit_success), \
                 mock.patch.object(ctypes, "WinDLL", return_value=kernel), \
                 mock.patch.object(ctypes, "get_last_error", return_value=5):
                if exited and query_exit_success:
                    self.assertIsNone(cd._process_image_path(42))
                else:
                    with self.assertRaises(OSError):
                        cd._process_image_path(42)
            kernel.CloseHandle.assert_called_once_with(123)

    def test_manager_and_watcher_use_shared_identity_filter(self):
        with mock.patch.object(manager, "image_pids", return_value={1, 2}), \
             mock.patch.object(manager, "claude_desktop_pids", return_value={1}) as desktop:
            self.assertEqual(manager.claude_pids(), {1})
        desktop.assert_called_once_with(candidate_pids={1, 2})
        with mock.patch.object(cd, "claude_desktop_pids", return_value={7}):
            self.assertEqual(watcher.claude_pids(), {7})


if __name__ == "__main__":
    unittest.main()
