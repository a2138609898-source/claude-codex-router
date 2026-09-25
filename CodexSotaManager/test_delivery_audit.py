import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'CodexHistorySync'))
import sync_codex_histories_three_way as sync
import validate_codex_profile as profile
from test_sync_build_regressions import SyncAndBuildRegressionTests


class DeliveryAuditTests(unittest.TestCase):
    def test_model_repair_preserves_nested_settings_and_string_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = root / 'catalog.json'
            catalog.write_text(json.dumps({'models': [{'slug': 'provider--new'}]}))
            config = root / 'config.toml'
            original = "description = '''\nmodel = 'old'\n'''\nmodel = 'old'\n[profiles.custom]\nmodel = 'old'\n"
            config.write_text(original)
            result = profile.repair_pinned_models(root, catalog)
            self.assertEqual(result['repaired']['model']['to'], 'provider--new')
            self.assertEqual(config.read_text(), "description = '''\nmodel = 'old'\n'''\nmodel = \"provider--new\"\n[profiles.custom]\nmodel = 'old'\n")
            config.write_text('[profiles.custom]\nmodel = "old"\n')
            self.assertEqual(profile.repair_pinned_models(root, catalog)['repaired'], {})
            self.assertEqual(config.read_text(), '[profiles.custom]\nmodel = "old"\n')

    def test_empty_third_database_bootstraps_but_unknown_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            roots = [base / name for name in ('personal', 'plus', 'sota')]
            for root in roots[:2]:
                SyncAndBuildRegressionTests._make_history_root(root, 'test', ())
            roots[2].mkdir()
            sqlite3.connect(roots[2] / 'state_5.sqlite').close()
            sync.preflight_rollout_availability(roots)
            self.assertTrue(sync.ensure_sota_initialized(roots[0], roots[2], base / 'backup'))
            db = sqlite3.connect(roots[2] / 'state_5.sqlite')
            db.execute('DROP TABLE threads')
            db.execute('CREATE TABLE unrelated (id TEXT)')
            db.commit()
            db.close()
            with self.assertRaises(sync.core.SyncError):
                sync.preflight_rollout_availability(roots)

    def test_reused_snapshot_never_links_to_live_history(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            roots = [base / name for name in ('personal', 'plus', 'sota')]
            for root in roots:
                SyncAndBuildRegressionTests._make_history_root(root, 'test', ('one',))
            first = sync.create_outer_snapshot(roots, base / 'backups' / '1')
            second = sync.create_outer_snapshot(roots, base / 'backups' / '2')
            original = roots[0] / 'sessions' / 'rollout-one.jsonl'
            copied = second['root'] / 'cockpit' / 'sessions' / original.name
            expected = copied.read_bytes()
            self.assertGreater(second['manifest']['roots']['cockpit']['reused_bytes'], 0)
            original.write_text('live mutation')
            self.assertEqual(copied.read_bytes(), expected)
            sync.prune_outer_snapshots(base / 'backups')
            self.assertFalse(first['root'].exists())
            self.assertEqual(copied.read_bytes(), expected)

    def test_incomplete_rollback_survives_both_retention_policies(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old = base / '1'
            (old / 'outer-snapshot').mkdir(parents=True)
            (old / 'rollback-result.json').write_text('{"status":"incomplete"}')
            (base / '2').mkdir()
            sync.prune_outer_snapshots(base, keep=1)
            sync.rotate_three_way_backups(base, keep=1)
            self.assertTrue((old / 'outer-snapshot').exists())


if __name__ == '__main__':
    unittest.main()
