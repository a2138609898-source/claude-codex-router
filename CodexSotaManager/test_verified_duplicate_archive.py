"""Explicit duplicate archival is tested only against disposable profiles."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "CodexHistorySync"))
import archive_verified_duplicates as archive
import sync_codex_histories as core


class VerifiedDuplicateArchiveTests(unittest.TestCase):
    keep = "00000000-0000-4000-8000-000000000001"
    duplicate = "00000000-0000-4000-8000-000000000002"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "profile"
        (self.root / "sessions").mkdir(parents=True)
        self.source = self.root / "sessions" / ("rollout-" + self.keep + ".jsonl")
        records = [{"type": "session_meta", "ordinal": 0,
                    "payload": {"id": self.keep, "model_provider": "test"}},
                   {"type": "response_item", "ordinal": 1, "payload": {"text": "fixture"}}]
        self.source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        self.clone = self.source.with_name("rollout-" + self.duplicate + ".jsonl")
        core.make_conflict_clone(self.source, self.clone, self.keep, self.duplicate)
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT, archived INTEGER, archived_at INTEGER, is_pinned INTEGER, thread_section_id TEXT, section_position INTEGER)")
            for sid, path, title in ((self.keep, self.source, "fixture"),
                                     (self.duplicate, self.clone, "fixture" + core.CONFLICT_CLONE_SUFFIX)):
                db.execute("INSERT INTO threads VALUES (?,?,?,0,NULL,1,'pinned',7)", (sid, str(path), title))
        self.state = self.root / ".codex-global-state.json"
        self.state.write_text(json.dumps({"projectless-thread-ids": [self.keep, self.duplicate],
                                          "pinned-thread-ids": [self.keep, self.duplicate],
                                          "user-setting": "preserved"}), encoding="utf-8")
        self.index = self.root / "session_index.jsonl"
        self.index.write_text("".join(json.dumps({"id": sid}) + "\n" for sid in (self.keep, self.duplicate)), encoding="utf-8")

    def plan(self):
        return archive.plan_root(self.root, [(self.keep, self.duplicate)])

    def row(self, sid):
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db:
            return db.execute("SELECT archived,is_pinned,thread_section_id,section_position FROM threads WHERE id=?", (sid,)).fetchone()

    def test_archive_only_changes_selected_visibility_and_preserves_history(self):
        before = {path: path.read_bytes() for path in (self.source, self.clone)}
        backup = self.base / "backup"
        archive.archive(self.root, self.plan(), backup)
        self.assertEqual(self.row(self.keep), (0, 1, "pinned", 7))
        self.assertEqual(self.row(self.duplicate), (1, 0, None, None))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        state = json.loads(self.state.read_bytes())
        self.assertEqual(state["projectless-thread-ids"], [self.keep])
        self.assertEqual(state["user-setting"], "preserved")
        self.assertNotIn(self.duplicate, self.index.read_text())
        self.assertEqual(json.loads((backup / "manifest.json").read_bytes())["status"], "committed")
        with closing(sqlite3.connect(backup / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT archived FROM threads WHERE id=?", (self.duplicate,)).fetchone(), (0,))

    def test_database_and_prior_cache_write_roll_back_on_cache_failure(self):
        before = {path: path.read_bytes() for path in (self.source, self.clone, self.state, self.index)}
        original_write = archive.repair.atomic_bytes
        def fail_global_cache(path, content):
            if path == self.state:
                raise OSError("simulated cache write failure")
            return original_write(path, content)
        with patch.object(archive.repair, "atomic_bytes", side_effect=fail_global_cache):
            with self.assertRaisesRegex(OSError, "simulated cache"):
                archive.archive(self.root, self.plan(), self.base / "backup")
        self.assertEqual(self.row(self.duplicate), (0, 1, "pinned", 7))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(json.loads((self.base / "backup" / "manifest.json").read_bytes())["status"], "rolled-back")

    def test_changed_pair_is_rejected_without_writes_or_backup(self):
        plans = self.plan()
        with self.clone.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"text": "unique"}}) + "\n")
        with self.assertRaisesRegex(core.SyncError, "contents differ"):
            archive.archive(self.root, plans, self.base / "backup")
        self.assertFalse((self.base / "backup").exists())
        self.assertEqual(self.row(self.duplicate)[0], 0)

    def test_rejects_chains_and_agent_dependencies(self):
        with self.assertRaisesRegex(core.SyncError, "chains"):
            archive.plan_root(self.root, [(self.keep, self.duplicate), (self.duplicate, self.keep)])
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT)")
            db.execute("INSERT INTO thread_spawn_edges VALUES (?,?)", (self.keep, self.duplicate))
        with self.assertRaisesRegex(core.SyncError, "active or referenced"):
            self.plan()

    def test_refuses_backup_inside_profile_or_existing_directory(self):
        plans = self.plan()
        for target in (self.root / "backup", self.base):
            with self.assertRaisesRegex(core.SyncError, "unused backup"):
                archive.archive(self.root, plans, target)
        self.assertFalse((self.root / "backup").exists())

    def test_catalog_only_removes_local_duplicate_projection(self):
        directory = self.root / "sqlite"
        directory.mkdir()
        catalog = directory / "codex-dev.db"
        with closing(sqlite3.connect(catalog)) as db, db:
            db.execute("CREATE TABLE local_thread_catalog(host_id TEXT,thread_id TEXT)")
            db.executemany("INSERT INTO local_thread_catalog VALUES (?,?)",
                           [("local", self.keep), ("local", self.duplicate), ("remote", self.duplicate)])
            db.execute("CREATE TABLE local_thread_catalog_metadata(id INTEGER,catalog_revision INTEGER)")
            db.execute("INSERT INTO local_thread_catalog_metadata VALUES (1,4)")
        archive.archive(self.root, self.plan(), self.base / "backup")
        with closing(sqlite3.connect(catalog)) as db:
            self.assertEqual(db.execute("SELECT host_id,thread_id FROM local_thread_catalog ORDER BY host_id").fetchall(),
                             [("local", self.keep), ("remote", self.duplicate)])
            self.assertEqual(db.execute("SELECT catalog_revision FROM local_thread_catalog_metadata").fetchone(), (5,))


if __name__ == "__main__":
    unittest.main()
