"""Reproduce pagination corruption using disposable files only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'CodexHistorySync'))
import sync_codex_histories as core


class PaginationIntegrityTests(unittest.TestCase):
    owner = '00000000-0000-4000-8000-000000000001'
    page = '00000000-0000-4000-8000-000000000002'
    clone = '00000000-0000-4000-8000-000000000003'

    def write(self, path, *, base=None, ordinal=0, text='hello'):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'id': self.owner, 'model_provider': 'test'}
        if base:
            payload['history_base'] = base
        records = [{'type': 'session_meta', 'ordinal': ordinal, 'payload': payload},
                   {'type': 'response_item', 'ordinal': ordinal+1, 'payload': {'text': text}}]
        path.write_text(''.join(json.dumps(x)+'\n' for x in records), encoding='utf-8')

    def session(self, root, path):
        stat = path.stat()
        return core.SessionFile(self.owner, path, path.relative_to(root), stat.st_size, stat.st_mtime_ns)

    def test_clone_preserves_history_source_id_not_self_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'page.jsonl'
            base = {'thread_id': self.owner, 'end_ordinal_exclusive': 2, 'end_byte_offset': 99}
            self.write(source, base=base, ordinal=2)
            target = root / 'clone.jsonl'
            core.make_conflict_clone(source, target, self.owner, self.clone)
            meta = json.loads(target.read_text().splitlines()[0])['payload']
            self.assertEqual(meta['id'], self.clone)
            self.assertEqual(meta['history_base'], base)

    def test_extended_windows_path_selects_exact_database_head(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / 'sessions' / ('rollout-'+self.owner+'.jsonl')
            head = root / 'sessions' / ('rollout-'+self.owner+'_'+self.page+'.jsonl')
            self.write(old)
            self.write(head, ordinal=2)
            preferred = '\\\\?\\' + str(head.resolve())
            self.assertEqual(core.scan_sessions(root, {self.owner: preferred})[self.owner].path, head)

    def test_paginated_clone_compares_equal_to_its_preserved_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ancestor = root/'sessions'/('rollout-'+self.owner+'.jsonl')
            self.write(ancestor)
            head = ancestor.with_name('rollout-'+self.owner+'_'+self.page+'.jsonl')
            self.write(head, base={'thread_id':self.owner, 'end_ordinal_exclusive':2,
                                  'end_byte_offset':ancestor.stat().st_size}, ordinal=2)
            clone = head.with_name('rollout-conflict-'+self.clone+'.jsonl')
            core.make_conflict_clone(head, clone, self.owner, self.clone)
            stat = clone.stat()
            clone_session = core.SessionFile(self.clone, clone, clone.relative_to(root), stat.st_size, stat.st_mtime_ns)
            self.assertEqual(core.normalized_session_digest(self.session(root, head)),
                             core.normalized_session_digest(clone_session))

    def test_sync_never_overwrites_ancestor_with_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            roots = [base/'left', base/'right']
            snapshots = []
            originals = []
            for root in roots:
                old = root/'sessions'/('rollout-'+self.owner+'.jsonl')
                self.write(old)
                originals.append(old.read_bytes())
                chosen = old
                if root == roots[1]:
                    chosen = root/'sessions'/('rollout-'+self.owner+'_'+self.page+'.jsonl')
                    self.write(chosen, base={'thread_id': self.owner, 'end_ordinal_exclusive': 2,
                                            'end_byte_offset': len(originals[-1])}, ordinal=2, text='new')
                snapshots.append(core.RootSnapshot(root, {self.owner: {'id':self.owner, 'title':'same',
                    'rollout_path':str(chosen), 'updated_at':10 if root==roots[1] else 1,
                    'archived':0, 'model_provider':'test'}}, [], {}, [], {}, {}))
            scratch = base/'scratch'; scratch.mkdir()
            paths, clones, _, _ = core.sync_session_files(*snapshots, base/'backup', scratch,
                                                        {root:'test' for root in roots})
            for root, original in zip(roots, originals):
                self.assertEqual((root/'sessions'/('rollout-'+self.owner+'.jsonl')).read_bytes(), original)
                head = paths[root][self.owner]
                self.assertTrue(head.name.endswith(self.page+'.jsonl'))
            self.assertEqual(clones, [], 'A paginated continuation is not a conflicting branch')

    def test_cycle_rejected_before_sync_creates_any_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.write(root/'sessions'/('rollout-'+self.owner+'.jsonl'),
                       base={'thread_id':self.owner,'end_ordinal_exclusive':2,'end_byte_offset':99})
            with self.assertRaisesRegex(core.SyncError, 'cycle'):
                core.validate_lineage([root])

    def test_normal_pagination_byte_cursors_are_repaired_after_provider_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'root'
            old = root/'sessions'/('rollout-'+self.owner+'.jsonl')
            self.write(old)
            head = old.with_name('rollout-'+self.owner+'_'+self.page+'.jsonl')
            self.write(head, base={'thread_id':self.owner, 'end_ordinal_exclusive':2,
                                  'end_byte_offset':1}, ordinal=2)
            before = head.read_bytes()
            backup = Path(temp)/'backup'
            with self.assertRaisesRegex(core.SyncError, 'byte boundary'):
                core.validate_lineage([root], check_offsets=True)
            core.refresh_recovered_lineage(root, backup)
            self.assertEqual(json.loads(head.read_text().splitlines()[0])['payload']['history_base']['end_byte_offset'], old.stat().st_size)
            self.assertEqual((backup/'lineage-headers'/root.name/head.relative_to(root)).read_bytes(), before)
            core.validate_lineage([root], check_offsets=True)

    def duplicate_root(self, root):
        from test_sync_build_regressions import SyncAndBuildRegressionTests
        SyncAndBuildRegressionTests._make_history_root(root, 'test', (self.owner,self.clone))
        original = root/'sessions'/('rollout-'+self.owner+'.jsonl')
        duplicate = root/'sessions'/('rollout-'+self.clone+'.jsonl')
        self.write(original)
        core.make_conflict_clone(original, duplicate, self.owner, self.clone)
        with closing(sqlite3.connect(root/'state_5.sqlite')) as db, db:
            db.execute('ALTER TABLE threads ADD COLUMN title TEXT')
            db.execute('UPDATE threads SET title=? WHERE id=?', ('chat',self.owner))
            db.execute('UPDATE threads SET title=? WHERE id=?', ('chat'+core.CONFLICT_CLONE_SUFFIX,self.clone))
        return duplicate

    def test_duplicate_archive_preserves_history_and_backup(self):
        import archive_verified_duplicates as cleanup
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'root'
            duplicate = self.duplicate_root(root)
            original_bytes = duplicate.read_bytes()
            plans = cleanup.plan_root(root, [(self.owner,self.clone)])
            saved = Path(temp)/'backup'
            self.assertEqual(cleanup.archive(root,plans,saved), [])
            snapshot = core.load_root_snapshot(root)
            self.assertEqual(snapshot.threads[self.owner]['archived'], 0)
            self.assertEqual(snapshot.threads[self.clone]['archived'], 1)
            self.assertEqual(Path(snapshot.threads[self.clone]['rollout_path']).read_bytes(), original_bytes)
            self.assertTrue((saved/'state_5.sqlite').is_file())

    def test_duplicate_archive_rejects_active_or_divergent_chat(self):
        import archive_verified_duplicates as cleanup
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'root'
            duplicate = self.duplicate_root(root)
            with patch.dict('os.environ', {'CODEX_THREAD_ID':self.clone}):
                with self.assertRaisesRegex(core.SyncError, 'active or referenced'):
                    cleanup.plan_root(root, [(self.owner,self.clone)])
            with duplicate.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'type':'response_item','ordinal':2,'payload':{'text':'important unique content'}})+'\n')
            with self.assertRaisesRegex(core.SyncError, 'contents differ'):
                cleanup.plan_root(root, [(self.owner,self.clone)])

    def test_duplicate_archive_rejects_referenced_source(self):
        import archive_verified_duplicates as cleanup
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'root'
            duplicate = self.duplicate_root(root)
            self.write(duplicate.with_name('rollout-'+self.page+'.jsonl'),
                       base={'thread_id':self.clone,'end_ordinal_exclusive':2,
                             'end_byte_offset':duplicate.stat().st_size}, ordinal=2)
            with self.assertRaisesRegex(core.SyncError, 'active or referenced'):
                cleanup.plan_root(root, [(self.owner,self.clone)])

    def test_atomic_backup_does_not_extend_near_limit_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'source.jsonl'
            source.write_bytes(b'history\n')
            destination = root/('a'*(245-len(str(root))-1-6)+'.jsonl')
            core.atomic_copy_file(source,destination)
            self.assertEqual(destination.read_bytes(),b'history\n')
            core.atomic_write_bytes(destination,b'updated\n')
            self.assertEqual(destination.read_bytes(),b'updated\n')

    def test_three_way_pagination_converges_across_three_repeated_runs(self):
        import sync_codex_histories_three_way as three
        from test_sync_build_regressions import SyncAndBuildRegressionTests
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            roots=[base/name for name in ('personal','plus','sota')]
            for root in roots:
                SyncAndBuildRegressionTests._make_history_root(root,'test',(self.owner,))
                old=root/'sessions'/('rollout-'+self.owner+'.jsonl')
                self.write(old)
            old=roots[2]/'sessions'/('rollout-'+self.owner+'.jsonl')
            head=old.with_name('rollout-'+self.owner+'_'+self.page+'.jsonl')
            self.write(head,base={'thread_id':self.owner,'end_ordinal_exclusive':2,
                                  'end_byte_offset':old.stat().st_size},ordinal=2,text='continuation')
            with closing(sqlite3.connect(roots[2]/'state_5.sqlite')) as db, db:
                db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(head),self.owner))
            first_paths=None
            for run in range(3):
                result=three.run_three_way_sync(*roots,base/'backups')
                self.assertEqual(result['conflicts_preserved'],0)
                paths=[]
                for root in roots:
                    snapshot=core.load_root_snapshot(root)
                    self.assertEqual(set(snapshot.threads),{self.owner})
                    paths.append(Path(snapshot.threads[self.owner]['rollout_path']).relative_to(root))
                    core.validate_lineage([root])
                if first_paths is None:
                    first_paths=paths
                self.assertEqual(paths,first_paths)

    def test_distinct_suffixes_after_same_cutoff_are_preserved_as_real_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            ancestor=root/'sessions'/('rollout-'+self.owner+'.jsonl')
            self.write(ancestor)
            ancestor.write_text(ancestor.read_text()+json.dumps({'type':'response_item','ordinal':2,'payload':{'text':'other branch'}})+'\n')
            child=ancestor.with_name('rollout-'+self.owner+'_'+self.page+'.jsonl')
            self.write(child,base={'thread_id':self.owner,'end_ordinal_exclusive':2,'end_byte_offset':1},ordinal=2,text='new branch')
            self.assertEqual(core.compare_files(self.session(root,ancestor),self.session(root,child)),'divergent')

    def test_real_paginated_conflict_is_preserved_only_once_across_three_runs(self):
        import sync_codex_histories_three_way as three
        from test_sync_build_regressions import SyncAndBuildRegressionTests
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            roots = [base/name for name in ('personal','plus','sota')]
            for index, root in enumerate(roots):
                SyncAndBuildRegressionTests._make_history_root(root,'test',(self.owner,))
                old = root/'sessions'/('rollout-'+self.owner+'.jsonl')
                self.write(old)
                if index == 1:
                    continue
                page = self.page if index == 0 else self.clone
                head = old.with_name('rollout-'+self.owner+'_'+page+'.jsonl')
                self.write(head,base={'thread_id':self.owner,'end_ordinal_exclusive':2,
                                      'end_byte_offset':old.stat().st_size},ordinal=2,text=f'branch-{index}')
                with closing(sqlite3.connect(root/'state_5.sqlite')) as db, db:
                    db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(head),self.owner))
            ids = None
            for run in range(3):
                result = three.run_three_way_sync(*roots,base/'backups')
                current = set(core.load_root_snapshot(roots[0]).threads)
                self.assertEqual(len(current),2)
                if ids is None:
                    ids = current
                self.assertEqual(current,ids)
                if run:
                    self.assertEqual(result['conflicts_preserved'],0)


if __name__ == '__main__':
    unittest.main()
