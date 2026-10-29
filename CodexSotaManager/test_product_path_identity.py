"""Windows 8.3 aliases must not change head selection, ownership, or boundaries."""
from contextlib import closing
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CodexHistorySync"))
import archive_verified_duplicates as archive
import sync_codex_histories as core


@unittest.skipUnless(os.name == "nt", "Windows short-name aliases")
class WindowsPathIdentityTests(unittest.TestCase):
    owner = "00000000-0000-4000-8000-000000000001"
    duplicate = "00000000-0000-4000-8000-000000000002"
    page = "00000000-0000-4000-8000-000000000003"

    def setUp(self):
        # The normal TMP may be on a volume with 8.3 disabled. Prefer the Windows
        # user's disposable Temp directory, which exposes the CI runner's alias bug.
        directory = Path(os.environ["LOCALAPPDATA"]) / "Temp"
        self.temporary = tempfile.TemporaryDirectory(prefix="sota identity long directory ", dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "profile with spaces"
        (self.root / "sessions").mkdir(parents=True)
        get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_short.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        if not get_short(str(self.root), buffer, len(buffer)):
            self.skipTest("Windows did not provide an 8.3 alias")
        self.short = Path(buffer.value)
        if os.path.normcase(str(self.short)) == os.path.normcase(str(self.root)):
            self.skipTest("8.3 aliases are disabled on this volume")
        self.assertEqual(self.short.resolve(), self.root.resolve())

    def write(self, path, owner=None, text="fixture"):
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session_meta", "ordinal": 0, "payload": {"id": owner or self.owner, "model_provider": "fixture"}},
            {"type": "response_item", "ordinal": 1, "payload": {"text": text}},
        ]
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def test_database_long_head_wins_when_scanning_short_alias(self):
        old = self.root / "sessions" / ("rollout-" + self.owner + ".jsonl")
        head = old.with_name("rollout-" + self.owner + "_" + self.page + ".jsonl")
        self.write(old)
        self.write(head, text="latest head")
        chosen = core.scan_sessions(self.short, {self.owner: "\\\\?\\" + str(head.resolve())})[self.owner]
        self.assertEqual(chosen.path, self.short / "sessions" / head.name)
        self.assertEqual(chosen.relative_path, Path("sessions") / head.name)

    def test_database_short_head_wins_when_scanning_long_spelling(self):
        old = self.root / "sessions" / ("rollout-" + self.owner + ".jsonl")
        head = old.with_name("rollout-" + self.owner + "_" + self.page + ".jsonl")
        self.write(old)
        self.write(head)
        preferred = self.short / "sessions" / head.name
        self.assertEqual(core.scan_sessions(self.root, {self.owner: str(preferred)})[self.owner].path, head)

    def test_provider_mapping_matches_both_spellings_and_rejects_ambiguity(self):
        self.assertEqual(core.provider_for_root(self.root, {self.short: "fixture"}), "fixture")
        self.assertEqual(core.provider_for_root(self.short, {self.root: "fixture"}), "fixture")
        with self.assertRaises(core.SyncError):
            core.provider_for_root(self.root, {self.root: "left", self.short: "right"})

    def test_relative_path_and_future_destination_expand_existing_parent(self):
        long = self.root / "sessions" / "future" / "rollout.jsonl"
        short = self.short / "sessions" / "future" / "rollout.jsonl"
        self.assertEqual(core.canonical_storage_path(short), long)
        self.assertEqual(core.relative_storage_path(short, self.root), Path("sessions/future/rollout.jsonl"))
        self.assertEqual(core.relative_storage_path(long, self.short), Path("sessions/future/rollout.jsonl"))

    def test_short_name_live_mutation_still_invokes_lifecycle_guard(self):
        guarded = []
        with core.history_checkpoint_scope(None, lambda: guarded.append(True), managed_roots=[self.root]) as work:
            core.before_history_mutation(self.short / "sessions" / "new.jsonl")
            self.assertTrue(work.mutation_started)
            self.assertEqual(work.mutated_paths, {self.root / "sessions" / "new.jsonl"})
        self.assertEqual(guarded, [True])

    def test_short_name_dotdot_escape_is_still_rejected(self):
        with core.history_checkpoint_scope(None, managed_roots=[self.root]):
            with self.assertRaisesRegex(core.SyncError, "outside its managed root"):
                core.before_history_mutation(self.short / ".." / "escaped.jsonl")

    def test_existing_parent_does_not_hide_long_name_dotdot_escape(self):
        escaped = self.root / ".." / "escaped.jsonl"
        self.assertIn("..", core.canonical_storage_path(escaped).parts)
        with core.history_checkpoint_scope(None, managed_roots=[self.root]):
            with self.assertRaisesRegex(core.SyncError, "outside its managed root"):
                core.atomic_write_bytes(escaped, b"must not write")
        self.assertFalse((self.base / "escaped.jsonl").exists())

    def seed_duplicates(self):
        keep = self.root / "sessions" / ("rollout-" + self.owner + ".jsonl")
        duplicate = keep.with_name("rollout-" + self.duplicate + ".jsonl")
        self.write(keep)
        core.make_conflict_clone(keep, duplicate, self.owner, self.duplicate)
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT, archived INTEGER)")
            for thread_id, path, title in (
                (self.owner, keep, "fixture"),
                (self.duplicate, duplicate, "fixture" + core.CONFLICT_CLONE_SUFFIX),
            ):
                alias = self.short / "sessions" / path.name
                db.execute("INSERT INTO threads VALUES(?,?,?,0)", (thread_id, str(alias), title))
        return keep, duplicate

    def test_archive_proof_and_apply_accept_short_database_paths(self):
        keep, duplicate = self.seed_duplicates()
        before = {path: path.read_bytes() for path in (keep, duplicate)}
        plans = archive.plan_root(self.short, [(self.owner, self.duplicate)])
        self.assertEqual(len(plans), 1)
        archive.archive(self.short, plans, self.base / "backup")
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT archived FROM threads WHERE id=?", (self.duplicate,)).fetchone(), (1,))

    def test_normalization_keeps_symlink_evidence_for_boundary_rejection(self):
        keep, _ = self.seed_duplicates()
        alias = self.root / "sessions" / "linked-head.jsonl"
        try:
            alias.symlink_to(keep)
        except OSError:
            self.skipTest("Creating test symlinks requires Windows developer mode")
        self.assertEqual(core.canonical_storage_path(alias), alias)
        with self.assertRaises(archive.repair.RepairError):
            archive.repair.inside(self.root.resolve(), core.canonical_storage_path(alias))
        with self.assertRaises(core.SyncError):
            archive.plan_root(self.short, [(self.owner, self.duplicate)])


if __name__ == "__main__":
    unittest.main()
