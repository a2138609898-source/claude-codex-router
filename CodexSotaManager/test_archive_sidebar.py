import sys
from pathlib import Path
import tempfile
import unittest
import sqlite3
from contextlib import contextmanager


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CodexHistorySync"))
import sync_codex_histories as sync
import sync_codex_histories_three_way as three_way


class ArchiveSidebarTests(unittest.TestCase):
    def test_remove_archived_nested_project_and_atom_projections(self):
        state = {
            "thread-project-assignments": {"archived": {"projectId": "project"}, "active": {}},
            "app-server-projects-migration-by-host": {
                "local": {"pendingThreadAssignmentIds": ["archived", "active"]}
            },
            "electron-persisted-atom-state": {
                "thread-project-membership-host-ids": {"archived": "local", "active": "local"},
                "client-thread-bindings-v1": {"client": "archived", "client2": "active"},
                "app-server-pinned-thread-order-v1": ["archived", "active"],
            },
        }
        sync.remove_archived_sidebar_entries(state, {"archived"})
        self.assertNotIn("archived", state["thread-project-assignments"])
        self.assertEqual(
            state["app-server-projects-migration-by-host"]["local"]["pendingThreadAssignmentIds"],
            ["active"],
        )
        atom = state["electron-persisted-atom-state"]
        self.assertNotIn("archived", atom["thread-project-membership-host-ids"])
        self.assertNotIn("archived", atom["client-thread-bindings-v1"].values())
        self.assertEqual(atom["app-server-pinned-thread-order-v1"], ["active"])

    def test_sync_index_never_keeps_archived_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshots = []
            for name in ("left", "right"):
                root = Path(temporary) / name
                root.mkdir()
                (root / ".codex-global-state.json").write_text("{}", encoding="utf-8")
                with database(root / "state_5.sqlite") as db:
                    db.execute("CREATE TABLE threads (id TEXT, archived INTEGER)")
                    db.executemany("INSERT INTO threads VALUES (?,?)", [("archived", 1), ("active", 0)])
                snapshots.append(sync.RootSnapshot(
                    root=root,
                    threads={"archived": {"id": "archived", "archived": 1, "source": "cli"},
                             "active": {"id": "active", "archived": 0, "source": "cli"}},
                    thread_columns=[], dynamic_tools={}, spawn_edges=[],
                    global_state={"projectless-thread-ids": ["active"]},
                    session_index={"archived": {"id": "archived"}, "active": {"id": "active"}},
                ))
            sync.sync_global_state_and_index(*snapshots, [])
            for snapshot in snapshots:
                self.assertEqual(set(sync.read_session_index(snapshot.root / "session_index.jsonl")), {"active"})

    def test_archive_timestamp_wins_stale_replica_but_later_unarchive_wins(self):
        from types import SimpleNamespace
        active = {"archived": 0, "updated_at_ms": 20000, "recency_at_ms": 10000}
        archived = {"archived": 1, "archived_at": 30, "updated_at_ms": 10000, "recency_at_ms": 10000}
        left = SimpleNamespace(threads={"thread": active}, thread_columns=[])
        right = SimpleNamespace(threads={"thread": archived}, thread_columns=[])
        self.assertIs(sync.choose_thread_source("thread", left, right)[0], right)
        active["updated_at_ms"] = 40000
        self.assertIs(sync.choose_thread_source("thread", left, right)[0], left)

    def test_sync_removes_archived_visibility_but_keeps_content_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshots = []
            for name in ("left", "right"):
                root = Path(temporary) / name
                root.mkdir()
                with database(root / "state_5.sqlite") as db:
                    db.execute("CREATE TABLE threads (id TEXT, archived INTEGER)")
                    db.executemany("INSERT INTO threads VALUES (?,?)", [("archived", 1), ("active", 0)])
                snapshots.append(sync.RootSnapshot(
                    root=root,
                    threads={"archived": {"id": "archived", "archived": 1, "source": "cli"},
                             "active": {"id": "active", "archived": 0, "source": "cli"}},
                    thread_columns=[], dynamic_tools={}, spawn_edges=[],
                    global_state={"thread-project-assignments": {"archived": "project", "active": "project"},
                                  "projectless-thread-ids": ["archived"],
                                  "pinned-thread-ids": ["archived", "active"],
                                  "local-projects": {"project": {"name": "unchanged"}},
                                  "thread-workspace-root-hints": {"archived": "keep"}},
                    session_index={"archived": {"id": "archived"}, "active": {"id": "active"}},
                ))
            sync.sync_global_state_and_index(*snapshots, [])
            for snapshot in snapshots:
                state = sync.read_json_retry(snapshot.root / ".codex-global-state.json")
                self.assertNotIn("archived", state["thread-project-assignments"])
                self.assertNotIn("archived", state["projectless-thread-ids"])
                self.assertNotIn("archived", state["pinned-thread-ids"])
                self.assertIn("active", state["thread-project-assignments"])
                self.assertEqual(state["local-projects"]["project"]["name"], "unchanged")
                self.assertEqual(state["thread-workspace-root-hints"]["archived"], "keep")
                self.assertEqual(set(sync.read_session_index(snapshot.root / "session_index.jsonl")), {"active"})
                self.assertEqual(snapshot.threads["archived"]["archived"], 1)

    def test_three_profiles_keep_archive_paths_and_support_unarchive(self):
        import json
        from test_sync_build_regressions import SyncAndBuildRegressionTests
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            roots = [base / name for name in ("personal", "plus", "sota")]
            for root in roots:
                SyncAndBuildRegressionTests._make_history_root(root, "test", ("archived", "active"))
                with database(root / "state_5.sqlite") as db:
                    db.execute("ALTER TABLE threads ADD COLUMN archived_at INTEGER")
                    db.execute("ALTER TABLE threads ADD COLUMN updated_at_ms INTEGER DEFAULT 1000")
                state_path = root / ".codex-global-state.json"
                state = json.loads(state_path.read_text())
                state["app-server-projects-migration-by-host"] = {"host": {"pendingThreadAssignmentIds": ["archived", "active"], "version": 1}}
                state["sidebar-project-thread-orders"] = {"project": {"threadIds": ["archived", "active"]}}
                state_path.write_text(json.dumps(state))
            source = roots[2] / "sessions" / "rollout-archived.jsonl"
            target = roots[2] / "archived_sessions" / source.name
            source.rename(target)
            with database(roots[2] / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET archived=1, archived_at=10, rollout_path=? WHERE id='archived'", (str(target),))
            # A background migration in other profiles may have a much newer
            # timestamp. It must not undo SOTA's explicit archive decision.
            for root in roots[:2]:
                with database(root / "state_5.sqlite") as db:
                    db.execute("UPDATE threads SET updated_at_ms=999999 WHERE id='archived'")
            for _ in range(2):
                result = three_way.run_three_way_sync(*roots, base / "backups", "test", "test", "test")
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["verification"]["three_way_same_thread_ids"])
            for root in roots:
                with database(root / "state_5.sqlite") as db:
                    archived, path = db.execute("SELECT archived,rollout_path FROM threads WHERE id='archived'").fetchone()
                    self.assertEqual(db.execute("SELECT count(*) FROM threads").fetchone()[0], 2)
                self.assertEqual(archived, 1)
                self.assertIn("archived_sessions", Path(path).parts)
                self.assertTrue(Path(path).is_file())

                self.assertFalse(list((root / "sessions").glob("*archived*")))
                state = json.loads((root / ".codex-global-state.json").read_text())
                self.assertEqual(state["app-server-projects-migration-by-host"]["host"]["pendingThreadAssignmentIds"], ["active"])
                self.assertEqual(state["sidebar-project-thread-orders"]["project"]["threadIds"], ["active"])
            with database(roots[2] / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET archived=0,archived_at=NULL,updated_at_ms=20000 WHERE id='archived'")
            three_way.run_three_way_sync(*roots, base / "backups", "test", "test", "test")
            for root in (roots[0], roots[2]):
                with database(root / "state_5.sqlite") as db:
                    archived, path = db.execute("SELECT archived,rollout_path FROM threads WHERE id='archived'").fetchone()
                self.assertEqual(archived, 0)
                self.assertEqual(Path(path).parent, root / "sessions")
                self.assertTrue(Path(path).is_file())

    def test_missing_rollouts_fail_before_snapshot_or_rollback(self):
        from unittest import mock
        from test_sync_build_regressions import SyncAndBuildRegressionTests
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            roots = [base / name for name in ("a", "b", "c")]
            for root in roots:
                SyncAndBuildRegressionTests._make_history_root(root, "test", ("missing",))
                (root / "sessions" / "rollout-missing.jsonl").unlink()
            with mock.patch.object(three_way, "create_outer_snapshot") as snapshot, mock.patch.object(three_way, "restore_outer_snapshot") as rollback:
                with self.assertRaisesRegex(sync.SyncError, "missing"):
                    three_way.run_three_way_sync(*roots, base / "backups", "test", "test", "test")
                snapshot.assert_not_called()
                rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
