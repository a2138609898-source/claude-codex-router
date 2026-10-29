"""Final ownership/restore regressions; synthetic fixtures only, via the guarded runner."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import mock

import CodexSotaManager as manager
import sota_registry as registry
from test_hardening_registry import RegistryFixture


class CredentialReassignmentTests(RegistryFixture):
    def test_replacement_identity_cannot_inherit_a_removed_owners_key(self):
        previous = self.provider(secret_file="owned.dpapi")
        self.seed([previous])
        paths = [self.codex.registry_path, self.codex.catalog_path, registry.secret_path(previous)]
        before = {path: path.read_bytes() for path in paths}
        replacement = self.provider("bravo", is_default=True, secret_file="OWNED.DPAPI",
                                    entropy=previous["entropy"])
        with self.assertRaises(registry.CredentialOwnershipError):
            registry.write_registry({"version": 1, "providers": [replacement]},
                                    self.codex.registry_path)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_replacing_a_shared_group_with_one_new_owner_is_not_a_safe_subset(self):
        first = self.provider(secret_file="owned.dpapi")
        self.seed([first, self.provider("bravo", secret_file="owned.dpapi")])
        before = self.codex.registry_path.read_bytes()
        replacement = self.provider("charlie", is_default=True, secret_file="owned.dpapi")
        with self.assertRaises(registry.CredentialOwnershipError):
            registry.write_registry({"version": 1, "providers": [replacement]},
                                    self.codex.registry_path)
        self.assertEqual(self.codex.registry_path.read_bytes(), before)

    def test_existing_identities_cannot_exchange_each_others_key_files(self):
        original = self.seed([self.provider(), self.provider("bravo")])
        changed = deepcopy(original)
        left, right = changed["providers"]
        for field in ("secret_file", "entropy"):
            left[field], right[field] = right[field], left[field]
        before = self.codex.registry_path.read_bytes()
        with self.assertRaises(registry.CredentialOwnershipError):
            registry.write_registry(changed, self.codex.registry_path)
        self.assertEqual(self.codex.registry_path.read_bytes(), before)

    def test_removing_other_shared_claimants_keeps_the_original_owner_valid(self):
        first = self.provider(secret_file="shared.dpapi")
        self.seed([first, self.provider("bravo", secret_file="shared.dpapi")])
        secret = registry.secret_path(first)
        before = secret.read_bytes()
        registry.write_registry({"version": 1, "providers": [first]}, self.codex.registry_path)
        self.assertEqual(self.stored()["secret_file"], "shared.dpapi")
        self.assertEqual(secret.read_bytes(), before)

    def test_restoring_an_unowned_independent_key_remains_supported(self):
        self.seed([self.provider()])
        restored = self.provider("bravo", is_default=True)
        secret = registry.secret_path(restored)
        secret.write_bytes(b"synthetic-independent-restored-key")
        registry.write_registry({"version": 1, "providers": [restored]}, self.codex.registry_path)
        self.assertEqual(self.stored("bravo")["secret_file"], restored["secret_file"])
        self.assertEqual(secret.read_bytes(), b"synthetic-independent-restored-key")

    def test_existing_owner_can_move_to_a_preexisting_unowned_key(self):
        current = self.seed([self.provider()])
        original_secret = registry.secret_path(current["providers"][0])
        changed = deepcopy(current)
        changed["providers"][0]["secret_file"] = "independent.dpapi"
        independent = registry.secret_path(changed["providers"][0])
        independent.write_bytes(b"synthetic-independent-rotated-key")
        registry.write_registry(changed, self.codex.registry_path)
        self.assertEqual(self.stored()["secret_file"], "independent.dpapi")
        self.assertTrue(original_secret.exists())

    def test_unique_first_write_remains_supported(self):
        first = self.provider()
        registry.write_registry({"version": 1, "providers": [first]},
                                self.codex.registry_path, allow_missing_secrets=True)
        self.assertEqual(self.stored()["id"], "alpha")


class RestorePreviewConcurrencyTests(RegistryFixture):
    def restore_app(self):
        app = SimpleNamespace(workspace=self.codex, current_id="alpha", log=[])
        app._restore_summary = lambda *args: manager.CodexSotaApp._restore_summary(app, *args)
        app._run_task = lambda _label, worker, success: success(worker())
        app._load_registry = mock.Mock()
        app._run_audit = mock.Mock()
        app._append_log = app.log.append
        return app

    def preview(self, providers):
        self.seed(providers)
        current = registry.load_registry(self.codex.registry_path, allow_missing_secrets=True)
        incoming = registry.redacted_registry(current)
        incoming["providers"][0]["timeout_seconds"] = 42
        candidate, report = manager.merge_restored_registry(incoming, current, self.codex.name)
        candidate = registry.validate_registry(candidate, allow_missing_secrets=True,
                                               allow_shared_secrets=True)
        return candidate, report

    def confirm(self, candidate, report, change=None):
        app = self.restore_app()

        def accept(*_args, **_kwargs):
            if change is not None:
                change()
            return True

        with mock.patch.object(manager.messagebox, "askokcancel", side_effect=accept), \
             mock.patch.object(manager.messagebox, "showinfo"):
            manager.CodexSotaApp._confirm_and_restore(app, "synthetic-backup.json", candidate, report)
        return app

    def test_protected_identity_changed_during_confirmation_is_not_overwritten(self):
        candidate, report = self.preview([self.provider(protected=True, auth_type="codex_auth")])
        concurrent = []

        def change():
            latest = registry.load_registry(self.codex.registry_path, allow_missing_secrets=True)
            latest["providers"][0]["base_url"] = "https://new-protected-fixture.invalid"
            latest["providers"][0]["chat_path"] = "/new-protected/chat/completions"
            registry.write_registry(latest, self.codex.registry_path)
            concurrent.append(self.codex.registry_path.read_bytes())

        with self.assertRaisesRegex(RuntimeError, r"changed|\u53d8\u5316"):
            self.confirm(candidate, report, change)
        self.assertEqual(self.codex.registry_path.read_bytes(), concurrent[0])
        self.assertFalse((self.codex.root / "backups").exists())

    def test_new_protected_provider_added_during_confirmation_is_not_deleted(self):
        candidate, report = self.preview([self.provider(protected=True, auth_type="codex_auth")])

        def change():
            latest = registry.load_registry(self.codex.registry_path, allow_missing_secrets=True)
            latest["providers"].append(self.provider("bravo", protected=True, auth_type="codex_auth"))
            registry.write_registry(latest, self.codex.registry_path)

        with self.assertRaisesRegex(RuntimeError, r"changed|\u53d8\u5316"):
            self.confirm(candidate, report, change)
        self.assertTrue(self.stored("bravo")["protected"])

    def test_redacted_header_rotated_during_confirmation_is_not_reverted(self):
        candidate, report = self.preview([self.provider(extra_headers={"X-Fixture-Key": "old-synthetic"})])

        def change():
            latest = registry.load_registry(self.codex.registry_path, allow_missing_secrets=True)
            latest["providers"][0]["extra_headers"]["X-Fixture-Key"] = "new-synthetic"
            registry.write_registry(latest, self.codex.registry_path)

        with self.assertRaisesRegex(RuntimeError, r"changed|\u53d8\u5316"):
            self.confirm(candidate, report, change)
        self.assertEqual(self.stored()["extra_headers"]["X-Fixture-Key"], "new-synthetic")

    def test_change_after_confirmation_is_checked_only_after_taking_lock(self):
        candidate, report = self.preview([self.provider(protected=True, auth_type="codex_auth")])
        original_lock = registry.registry_write_lock

        @contextmanager
        def concurrent_lock(*, workspace):
            with original_lock(workspace=workspace):
                latest = registry.load_registry(self.codex.registry_path, allow_missing_secrets=True)
                latest["providers"][0]["responses_path"] = "/concurrent/responses"
                registry.write_registry(latest, self.codex.registry_path)
                yield

        with mock.patch.object(manager, "registry_write_lock", concurrent_lock), \
             self.assertRaisesRegex(RuntimeError, r"changed|\u53d8\u5316"):
            self.confirm(candidate, report)
        self.assertEqual(self.stored()["responses_path"], "/concurrent/responses")

    def test_unchanged_protected_restore_still_applies_user_settings_and_backs_up(self):
        candidate, report = self.preview([self.provider(protected=True, auth_type="codex_auth")])
        before = self.codex.registry_path.read_bytes()
        app = self.confirm(candidate, report)
        self.assertEqual(self.stored()["timeout_seconds"], 42)
        self.assertTrue(self.stored()["protected"])
        self.assertEqual(self.stored()["base_url"], "https://fixture.invalid")
        backup = next((self.codex.root / "backups").iterdir())
        self.assertEqual((backup / "providers.json").read_bytes(), before)
        self.assertIsNone(app.current_id)

    def test_timestamp_only_rewrite_does_not_invalidate_the_preview(self):
        candidate, report = self.preview([self.provider()])

        def change():
            latest = json.loads(self.codex.registry_path.read_text(encoding="utf-8"))
            latest["updated_at"] = "synthetic-timestamp-only"
            self.codex.registry_path.write_text(json.dumps(latest, indent=4), encoding="utf-8")

        self.confirm(candidate, report, change)
        self.assertEqual(self.stored()["timeout_seconds"], 42)

    def test_legacy_shared_group_restore_preserves_both_working_identities(self):
        first = self.provider(secret_file="shared.dpapi")
        candidate, report = self.preview([first, self.provider("bravo", secret_file="shared.dpapi")])
        secret = registry.secret_path(first)
        before = secret.read_bytes()
        self.confirm(candidate, report)
        self.assertEqual(self.stored()["timeout_seconds"], 42)
        self.assertEqual(self.stored("bravo")["secret_file"], "shared.dpapi")
        self.assertEqual(secret.read_bytes(), before)


class ManagerTimerLifecycleTests(RegistryFixture):
    @contextmanager
    def timer_app(self):
        # Build only a hidden synthetic root. UI layout, disk reads, worker launches
        # and live workspace inspection are outside this timer-lifecycle regression.
        with mock.patch.object(manager, "CODEX", self.codex), \
             mock.patch.object(manager.CodexSotaApp, "_build_ui"), \
             mock.patch.object(manager.CodexSotaApp, "_configure_styles"), \
             mock.patch.object(manager.CodexSotaApp, "_place_in_work_area"), \
             mock.patch.object(manager.CodexSotaApp, "deiconify"), \
             mock.patch.object(manager.CodexSotaApp, "_load_registry"), \
             mock.patch.object(manager.CodexSotaApp, "_run_audit"), \
             mock.patch.object(manager.CodexSotaApp, "_append_log"), \
             mock.patch.object(manager.CodexSotaApp, "_render_health"), \
             mock.patch.object(manager, "read_router_vendor_health", return_value={}), \
             mock.patch.object(manager.messagebox, "showinfo"):
            app = manager.CodexSotaApp()
            background_errors = []
            callback_errors = []
            # Register directly on this interpreter so the recorder survives Tk.destroy.
            # Tcl background errors otherwise print to stderr while unittest reports OK.
            app.tk.createcommand("bgerror", background_errors.append)
            app.report_callback_exception = lambda *error: callback_errors.append(error)
            try:
                app.update_idletasks()
                yield app, background_errors, callback_errors
            finally:
                if app._tclCommands is not None:
                    app.destroy()
                # Failed pre-fix assertions must not leak this fixture's timers into
                # later tests. Assertions inspect them BEFORE this fallback cleanup.
                for timer_id in app.tk.splitlist(app.tk.call("after", "info")):
                    app.tk.call("after", "cancel", timer_id)
                app.tk.deletecommand("bgerror")

    @staticmethod
    def pending_callbacks(app):
        return {
            timer_id: str(app.tk.splitlist(app.tk.call("after", "info", timer_id))[0])
            for timer_id in app.tk.splitlist(app.tk.call("after", "info"))
        }

    @staticmethod
    def drain_after_destroy(app):
        # Exercise both already-due callbacks and the native progress timer due
        # shortly after destroy; a plain update would miss those future failures.
        app.tk.call("after", 60, "set fixture_timer_wait_complete 1")
        app.tk.call("vwait", "fixture_timer_wait_complete")
        app.tk.call("update")

    def test_destroy_cancels_pending_callbacks_and_records_no_tcl_background_errors(self):
        with self.timer_app() as (app, background_errors, callback_errors):
            # Native ttk timers can return an unhashable Tcl_Obj script, unlike
            # the Python callback names; active progress must still close safely.
            app.progress = manager.ttk.Progressbar(app, mode="indeterminate")
            app.progress.start(12)
            app.after(0, app._drain_task_events)
            app.after(0, app._tick_health)
            app.after(0, app._initial_load)
            app.after_idle(lambda: self.fail("idle callback ran after destruction"))
            registered_commands = set(app._tclCommands)
            self.assertGreaterEqual(len(self.pending_callbacks(app)), 6)
            # A Tcl-native timer is not owned by the manager and must not be removed.
            app.tk.call("after", 0, "set fixture_foreign_timer_ran 1")
            app.destroy()
            remaining = self.pending_callbacks(app)
            self.drain_after_destroy(app)
            self.assertEqual(background_errors, [], "uncancelled Tcl after scripts failed")
            self.assertEqual(callback_errors, [])
            self.assertTrue(app._closing)
            self.assertFalse(registered_commands.intersection(remaining.values()))
            self.assertEqual(int(app.tk.getvar("fixture_foreign_timer_ran")), 1)

    def test_normal_polling_and_refused_close_preserve_timers_until_accepted_close(self):
        with self.timer_app() as (app, background_errors, callback_errors):
            app._initial_load()
            app._tick_health()
            app._render_health.assert_called_once_with({})
            delivered = []
            with mock.patch.object(app, "_task_succeeded", side_effect=lambda cb, result: cb(result)):
                app._task_events.put(("success", delivered.append, "synthetic-result"))
                app._drain_task_events()
            self.assertEqual(delivered, ["synthetic-result"])
            before = self.pending_callbacks(app)
            self.assertTrue(any(name.endswith("_tick_health") for name in before.values()))
            self.assertTrue(any(name.endswith("_drain_task_events") for name in before.values()))
            app._busy = True
            app._on_close()
            self.assertFalse(app._closing)
            self.assertEqual(self.pending_callbacks(app), before)
            app._busy = False
            with mock.patch.object(app, "_confirm_discarding_edits", return_value=False) as confirm:
                app._on_close()
                self.assertFalse(app._closing)
                self.assertTrue(app.winfo_exists())
                self.assertEqual(self.pending_callbacks(app), before)
                confirm.return_value = True
                app._on_close()
            self.drain_after_destroy(app)
            self.assertEqual(background_errors, [])
            self.assertEqual(callback_errors, [])
            self.assertTrue(app._closing)
            self.assertFalse(set(before.values()).intersection(self.pending_callbacks(app).values()))
