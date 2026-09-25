import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'CodexHistorySync'))
import sync_codex_histories as core
import sync_codex_histories_three_way as three_way


class PaginatedHistoryTests(unittest.TestCase):
    def test_physical_pages_are_copied_without_rewriting_or_creating_visible_threads(self):
        owner = '00000000-0000-0000-0000-000000000001'
        page = '00000000-0000-0000-0000-000000000002'
        missing = '00000000-0000-0000-0000-000000000003'
        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp) / name for name in ('left', 'right')]
            for root in roots:
                (root / 'sessions').mkdir(parents=True)
                main = {'type': 'session_meta', 'payload': {'id': owner, 'history_base': {'thread_id': page}}}
                (root / 'sessions' / f'rollout-{owner}.jsonl').write_text(json.dumps(main) + '\n')
            segment = roots[0] / 'sessions' / f'rollout-{owner}_{page}.jsonl'
            segment.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': owner, 'history_base': {'thread_id': missing}}}) + '\n')
            before = (roots[1] / 'sessions' / f'rollout-{owner}.jsonl').read_bytes()
            warnings = core.sync_lineage_dependencies(roots)
            self.assertEqual((roots[1] / 'archived_sessions' / segment.name).read_bytes(), segment.read_bytes())
            self.assertEqual((roots[1] / 'sessions' / f'rollout-{owner}.jsonl').read_bytes(), before)
            self.assertTrue(any(missing in warning for warning in warnings))
            protected, unavailable = core.lineage_dependencies(roots)
            self.assertIn(owner, protected)
            self.assertIn(page, protected)
            self.assertEqual(unavailable, {missing})
            self.assertEqual(core.sync_lineage_dependencies(roots), warnings)

    def test_full_snapshot_restores_a_large_rollout_after_archive_move(self):
        from test_sync_build_regressions import SyncAndBuildRegressionTests
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            roots = [base / name for name in ('personal', 'plus', 'sota')]
            for root in roots:
                SyncAndBuildRegressionTests._make_history_root(root, 'test', ('large',))
            original = roots[2] / 'sessions' / 'rollout-large.jsonl'
            expected = original.read_bytes()
            snapshot = three_way.create_outer_snapshot(roots, base / 'backup')
            original.rename(roots[2] / 'archived_sessions' / original.name)
            result = three_way.restore_outer_snapshot(snapshot)
            self.assertEqual(result['status'], 'restored')
            self.assertEqual(original.read_bytes(), expected)
            self.assertEqual((snapshot['root'] / 'sota' / 'sessions' / original.name).read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()
