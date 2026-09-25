"""Focused regressions for draft preservation; no live services or credentials."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest import mock

import CodexSotaManager as manager
from test_codex_sota_regressions import Variable, WidgetStub, new_provider_form, provider_config


class EditorSafetyTests(unittest.TestCase):
    def test_real_tk_filter_events_preserve_draft_and_busy_reload_updates_headers(self):
        provider = manager.validate_provider(
            provider_config("vendor", "https://example.invalid", prefix="vendor--", is_default=True),
            allow_missing_secret=True,
        )
        provider["extra_headers"] = {"X-Custom": "updated"}
        with mock.patch.object(manager.CodexSotaApp, "_initial_load"), \
             mock.patch.object(manager.CodexSotaApp, "_refresh_claude_status"), \
             mock.patch.object(manager.CodexSotaApp, "_run_lint"), \
             mock.patch.object(manager, "load_registry", return_value={"providers": [provider]}), \
             mock.patch.object(manager.messagebox, "askyesnocancel") as prompt:
            app = manager.CodexSotaApp()
            app.withdraw()
            try:
                app._load_registry()
                app.update()
                app.api_key_var.set("unsaved-key")
                app.name_var.set("Unsaved name")
                app.provider_filter_var.set("vendor")
                app.update()
                self.assertEqual(app.name_var.get(), "Unsaved name")
                self.assertEqual(app.api_key_var.get(), "unsaved-key")
                prompt.assert_not_called()
                app._set_busy(True, "test")
                app._load_provider("vendor")
                self.assertIn("updated", app.headers_text.get("1.0", "end"))
                self.assertEqual(str(app.headers_text.cget("state")), "disabled")
            finally:
                app.destroy()

    def test_filter_reselection_does_not_reload_dirty_form(self):
        app = SimpleNamespace(_busy=False, current_id="vendor", provider_tree=mock.Mock(),
                              _load_provider=mock.Mock(), _confirm_discarding_edits=mock.Mock())
        app.provider_tree.selection.return_value = ("vendor",)
        manager.CodexSotaApp._provider_selected(app)
        app._load_provider.assert_not_called()
        app._confirm_discarding_edits.assert_not_called()

    def test_cancel_navigation_preserves_provider_and_selection(self):
        app = SimpleNamespace(_busy=False, current_id="vendor", provider_tree=mock.Mock(),
                              _load_provider=mock.Mock(),
                              _confirm_discarding_edits=mock.Mock(return_value=False))
        app.provider_tree.selection.return_value = ("other",)
        manager.CodexSotaApp._provider_selected(app)
        app._load_provider.assert_not_called()
        app.provider_tree.selection_set.assert_called_once_with("vendor")

    def test_cancel_workspace_switch_preserves_workspace(self):
        app = SimpleNamespace(_busy=False, workspace=manager.CODEX,
                              workspace_var=Variable(manager.CLAUDE.name),
                              _confirm_discarding_edits=lambda _: False)
        manager.CodexSotaApp._switch_workspace(app)
        self.assertIs(app.workspace, manager.CODEX)
        self.assertEqual(app.workspace_var.get(), manager.CODEX.name)

    def test_key_only_and_model_only_drafts_are_dirty(self):
        app = new_provider_form(manager.CODEX)
        app.current_id = None
        app.api_key_var.set("replacement-key")
        self.assertTrue(manager.CodexSotaApp._has_unsaved_changes(app))
        app.api_key_var.set("")
        app.draft_models = [{"id": "new-model"}]
        self.assertTrue(manager.CodexSotaApp._has_unsaved_changes(app))

    def test_close_cannot_kill_active_transaction(self):
        app = SimpleNamespace(_busy=True, _closing=False, destroy=mock.Mock())
        with mock.patch.object(manager.messagebox, "showinfo"):
            manager.CodexSotaApp._on_close(app)
        self.assertFalse(app._closing)
        app.destroy.assert_not_called()

    def test_empty_registry_clears_deleted_provider_editor(self):
        app = SimpleNamespace(workspace=manager.CODEX, current_id="removed",
                              loaded_provider={"id": "removed"}, draft_models=[{"id": "model"}],
                              _render_providers=lambda: [], _reset_editor=mock.Mock(),
                              _set_editor_protected=mock.Mock(), _run_lint=lambda: None,
                              _refresh_claude_status=lambda: None)
        with mock.patch.object(manager, "load_registry", return_value={"providers": []}):
            manager.CodexSotaApp._load_registry(app)
        self.assertIsNone(app.current_id)
        self.assertIsNone(app.loaded_provider)
        self.assertEqual(app.draft_models, [])
        app._reset_editor.assert_called_once()

    def test_model_probe_never_commits_existing_provider_draft(self):
        provider = provider_config("vendor", "https://example.invalid", prefix="vendor--", is_default=True)
        edited = deepcopy(provider)
        edited["name"] = "Unsaved name"
        app = SimpleNamespace(registry={"providers": [provider]}, workspace=manager.CODEX,
                              draft_models=edited["models"], reasoning_var=Variable("low"),
                              _blank_editor=lambda: False, _provider_from_form=lambda: edited,
                              _probe_key=lambda: None,
                              _run_task=lambda _label, worker, _success, _keys: worker())
        with mock.patch.object(manager, "auto_repair_active_inference_path", return_value={"changed": True}), \
             mock.patch.object(manager, "test_model", return_value={"ok": False}), \
             mock.patch.object(manager, "apply_provider") as apply:
            manager.CodexSotaApp._test_selected_models(app)
        apply.assert_not_called()

    def test_loading_provider_unlocks_headers_before_replacing_text(self):
        provider = provider_config("vendor", "https://example.invalid", prefix="vendor--", is_default=True)
        provider["extra_headers"] = {"X-Custom": "new-value"}
        provider = manager.validate_provider(provider, allow_missing_secret=True)
        app = new_provider_form(manager.CODEX)
        app.registry = {"providers": [provider]}
        app.model_filter_var = Variable()
        app.headers_text = mock.Mock()
        manager.CodexSotaApp._load_provider(app, "vendor")
        self.assertEqual(app.headers_text.mock_calls[0], mock.call.configure(state="normal"))
        self.assertIn("new-value", app.headers_text.insert.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
