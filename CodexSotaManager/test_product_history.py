"""Portable profile identities and absent optional stores use disposable roots."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parent.parent / "CodexHistorySync"
sys.path.insert(0, str(CORE))
import repair_archived_sidebar as sidebar
import sync_codex_histories as history
import sync_codex_histories_three_way as three_way
import validate_codex_profile as validator


class ProductHistoryTests(unittest.TestCase):
    def test_destination_provider_uses_its_config_and_preserves_every_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            before = b'model_provider = "existing_local_provider"\n# keep user settings\n'
            config.write_bytes(before)
            self.assertEqual(history.configured_model_provider(root, "codex_sota"), "existing_local_provider")
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["config.toml"])

    def test_absent_profile_read_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "absent"
            self.assertEqual(history.configured_model_provider(root, "fallback"), "fallback")
            self.assertFalse(root.exists())

    def test_malformed_config_never_silently_retargets_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text("model_provider = [", encoding="utf-8")
            with self.assertRaises(history.SyncError):
                history.configured_model_provider(root, "fallback")
            self.assertEqual(len(list(root.iterdir())), 1)

    def test_sidebar_repair_skips_profiles_that_have_never_been_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "absent"
            with mock.patch.object(sidebar, "app_running", return_value=False), \
                    mock.patch.object(sidebar, "configured_profiles", return_value=(("test", root),)), \
                    mock.patch("builtins.print"):
                sidebar.main()
            self.assertFalse(root.exists())

    def test_new_and_existing_provider_ids_validate_by_the_local_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"models": [{"slug": "vendor--model"}]}), encoding="utf-8")
            for provider_id in ("codex_sota", "existing_local_provider"):
                with self.subTest(provider=provider_id):
                    (root / "config.toml").write_text(
                        'cli_auth_credentials_store = "file"\nforced_login_method = "api"\n'
                        f'model_provider = "{provider_id}"\nmodel = "vendor--model"\n'
                        + 'model_catalog_json = ' + json.dumps(catalog.as_posix()) + '\n'
                        + f'[model_providers.{provider_id}]\n'
                        + 'base_url = "http://127.0.0.1:17895"\nwire_api = "responses"\nrequires_openai_auth = true\n',
                        encoding="utf-8",
                    )
                    self.assertEqual(validator.validate_profile("Sota", root, catalog), (True, "ok"))

    def test_cli_keeps_explicit_provider_overrides(self):
        args = three_way.build_parser().parse_args(["--sota-provider", "explicit_provider"])
        self.assertEqual(args.sota_provider, "explicit_provider")


if __name__ == "__main__":
    unittest.main()
