"""Synthetic history hardening regressions; run only with run_guarded_tests.py.

All files live in the guarded tempfile root. No launcher, router, supplier, real
profile, process termination, or business entrypoint is executed.
"""
from contextlib import closing, nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import codex_app_lifecycle as lifecycle
import repair_archived_sidebar as sidebar
import repair_sota_launch_history as repair
import sync_after_codex_exit as watcher
import sync_codex_histories as core
import sync_codex_histories_three_way as three_way


class HistoryHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        (self.base / "work").mkdir()
        for module, name, value in (
            (core, "INSTALL_DIR", self.base),
            (core, "LOG_DIR", self.base / "logs"),
            (core, "LOCK_PATH", self.base / "sync.lock"),
            (core, "LAST_RESULT_PATH", self.base / "last.json"),
            (lifecycle, "LIFECYCLE_LOCK_PATH", self.base / "life.lock"),
            (watcher, "LOG_DIR", self.base / "logs"),
            (watcher, "LOG_PATH", self.base / "logs" / "watch.log"),
            (watcher, "WATCHER_LOCK_PATH", self.base / "watch.lock"),
        ):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def history(self, name="a"):
        root = self.base / name
        (root / "sessions").mkdir(parents=True)
        state = {core.ATOM_STATE_KEY: {core.SIDEBAR_PREFERENCES_KEY: {
            "mode": "project", "chatSortMode": "updated_at", "projectSortMode": "updated_at"}}}
        (root / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
        (root / "session_index.jsonl").write_text("", encoding="utf-8")
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT, "
                       "source TEXT, archived INTEGER, archived_at INTEGER, model_provider TEXT, "
                       "thread_section_id TEXT, section_position REAL, is_pinned INTEGER)")
        return root

    def rollout(self, root, owner, *, page=None, content="same", title="fixture", archived=0):
        path = root / "sessions" / ("rollout-2026-10-14T00-00-00-" + (page or owner) + ".jsonl")
        records = [{"type": "session_meta", "ordinal": 0, "timestamp": "2026-10-14T00:00:00Z",
                    "payload": {"id": owner, "model_provider": "fixture", "timestamp": "2026-10-14T00:00:00Z"}},
                   {"type": "event_msg", "ordinal": 1, "payload": {"message": content}}]
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        if title is not None:
            with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,NULL,NULL,0)",
                           (owner, str(path), title, "cli", archived, None, "fixture"))
        return path

    def reference_schema(self, root):
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
            db.executescript("ALTER TABLE threads ADD COLUMN project_id TEXT;"
                             "CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',position INTEGER NOT NULL,created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL);"
                             "CREATE TABLE project_roots(project_id TEXT NOT NULL REFERENCES projects(id),position INTEGER NOT NULL,path TEXT NOT NULL,PRIMARY KEY(project_id,position));"
                             "CREATE TABLE project_idempotency_keys(key TEXT PRIMARY KEY,project_id TEXT NOT NULL,created_at_ms INTEGER NOT NULL);"
                             "CREATE TABLE thread_sections(id TEXT PRIMARY KEY,name TEXT NOT NULL,appearance TEXT);")

    def add_project(self, root, project_id, *, name="Project", created=10, path="C:/fixture/project"):
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
            db.execute("INSERT INTO projects VALUES(?,?,?,0,?,20)", (project_id, name, "{}", created))
            db.execute("INSERT INTO project_roots VALUES(?,0,?)", (project_id, path))
            db.execute("INSERT INTO project_idempotency_keys VALUES(?,?,10)", ("key-" + project_id, project_id))

    def assign_refs(self, root, owner, project_id, section_id="section", section_name="Section"):
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
            db.execute("INSERT INTO thread_sections VALUES(?,?,NULL)", (section_id, section_name))
            db.execute("UPDATE threads SET project_id=?,thread_section_id=? WHERE id=?", (project_id, section_id, owner))

    def sidebar_backup(self, suffix, *, status="completed"):
        backup = self.base / "backups" / (sidebar.BACKUP_PREFIX + suffix)
        backup.mkdir(parents=True)
        (backup / "fixture.sqlite").write_bytes(b"synthetic saved state")
        if status is not None:
            (backup / sidebar.BACKUP_STATE_FILE).write_text(json.dumps({
                "format": sidebar.BACKUP_FORMAT, "status": status,
                "backup_name": backup.name,
            }), encoding="utf-8")
        return backup

    def test_disconnected_unique_clone_branch_is_not_deduplicated(self):
        root = self.history()
        canonical, duplicate = str(uuid.uuid4()), str(uuid.uuid4())
        self.rollout(root, canonical)
        self.rollout(root, duplicate, title="fixture" + core.CONFLICT_CLONE_SUFFIX)
        branch = self.rollout(root, duplicate, page=str(uuid.uuid4()), content="unique branch", title=None)
        snapshot = core.load_root_snapshot(root)
        plan, warnings = core.find_legacy_exact_clones(snapshot, snapshot)
        self.assertEqual(plan, [])
        self.assertTrue(warnings)
        self.assertIn("unique branch", branch.read_text(encoding="utf-8"))

    def test_single_file_exact_clone_still_deduplicates(self):
        root = self.history()
        canonical, duplicate = str(uuid.uuid4()), str(uuid.uuid4())
        self.rollout(root, canonical)
        self.rollout(root, duplicate, title="fixture" + core.CONFLICT_CLONE_SUFFIX)
        snapshot = core.load_root_snapshot(root)
        plan, _ = core.find_legacy_exact_clones(snapshot, snapshot)
        self.assertEqual([(item.canonical_id, item.duplicate_id) for item in plan], [(canonical, duplicate)])

    def test_purge_rechecks_unproved_physical_branches_before_writing(self):
        root = self.history()
        canonical, duplicate = str(uuid.uuid4()), str(uuid.uuid4())
        self.rollout(root, canonical)
        head = self.rollout(root, duplicate, title="fixture" + core.CONFLICT_CLONE_SUFFIX)
        branch = self.rollout(root, duplicate, page=str(uuid.uuid4()), content="unique", title=None)
        with self.assertRaises(core.SyncError), mock.patch.object(core, "atomic_write_json") as write:
            core.purge_legacy_exact_clones([root], [core.DuplicateCloneSpec(canonical, duplicate, False)], self.base / "backup")
        write.assert_not_called()
        self.assertTrue(head.exists() and branch.exists())

    def test_real_jsonl_index_is_checked_for_archived_residue(self):
        root = self.history()
        owner = str(uuid.uuid4())
        self.rollout(root, owner, archived=1)
        index = root / "session_index.jsonl"
        index.write_text(json.dumps({"id": owner}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(core.SyncError, "archived_index_threads.*1"):
            core.verify_roots([root], root_providers={root: "fixture"}, model_guards={})
        index.write_text("", encoding="utf-8")
        self.assertEqual(core.verify_roots([root], root_providers={root: "fixture"}, model_guards={})[str(root)]["archived_index_threads"], 0)

    def test_base_exceptions_roll_back_and_record_resolved_state(self):
        roots = [self.history(name) for name in ("a", "b", "c")]
        for failure in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(exception=type(failure).__name__), \
                    mock.patch.object(three_way, "preflight_rollout_availability"), \
                    mock.patch.object(three_way, "create_outer_snapshot", return_value={"root": "memory-snapshot"}), \
                    mock.patch.object(three_way, "execute_three_way_mutations", side_effect=failure), \
                    mock.patch.object(three_way, "restore_outer_snapshot", return_value={"status": "restored"}) as restore, \
                    mock.patch.object(three_way, "prune_outer_snapshots"), \
                    mock.patch.object(three_way, "rotate_three_way_backups"):
                with self.assertRaises(type(failure)):
                    three_way.run_three_way_sync(*roots, self.base / "backups")
                restore.assert_called_once()
        states = [json.loads(path.read_text()) for path in (self.base / "backups").glob("*/run-state.json")]
        self.assertEqual([state["status"] for state in states], ["rolled_back", "rolled_back"])

    def test_snapshot_rotation_preserves_in_progress_unknown_and_failed_runs(self):
        backup = self.base / "bk"
        for name, status in (("1", None), ("2", "applying"), ("3", "recovery_required"), ("4", "completed"), ("5", "rolled_back")):
            run = backup / name
            (run / "outer-snapshot").mkdir(parents=True)
            if status:
                (run / "run-state.json").write_text(json.dumps({"status": status}))
        three_way.prune_outer_snapshots(backup, keep=0)
        three_way.rotate_three_way_backups(backup, keep=0)
        self.assertEqual({path.name for path in backup.iterdir()}, {"1", "2", "3"})

    def test_interrupted_real_fixture_mutation_restores_files_and_database_contents(self):
        roots = [self.history(name) for name in ("a", "b", "c")]
        before = {root: {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
                         if path.is_file() and path.suffix != ".sqlite"} for root in roots}
        databases_before = {}
        for root in roots:
            with closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
                databases_before[root] = list(db.iterdump())

        def partial_mutation(*_args):
            (roots[0] / ".codex-global-state.json").write_text('{"partial":true}')
            (roots[1] / "sessions" / "partial.jsonl").write_text('{"partial":true}\n')
            with closing(sqlite3.connect(roots[2] / "state_5.sqlite")) as db, db:
                db.execute("INSERT INTO threads(id,title) VALUES('partial','partial')")
            raise KeyboardInterrupt()

        with mock.patch.object(three_way, "execute_three_way_mutations", side_effect=partial_mutation):
            with self.assertRaises(KeyboardInterrupt):
                three_way.run_three_way_sync(*roots, self.base / "backup")
        for root in roots:
            self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*")
                              if path.is_file() and path.suffix != ".sqlite"}, before[root])
            # SQLite's backup API legitimately updates the destination header's
            # transaction counter. Verify every schema/row plus integrity, not
            # storage-header byte identity.
            with closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
                self.assertEqual(list(db.iterdump()), databases_before[root])
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        run = next((self.base / "backup").iterdir())
        self.assertEqual(json.loads((run / "run-state.json").read_text())["status"], "rolled_back")
        self.assertTrue((run / "failed-mutated-state" / "plus" / "sessions" / "partial.jsonl").is_file())

    def test_failed_recovery_marks_snapshot_nonprunable(self):
        roots = [self.history(name) for name in ("a", "b", "c")]
        with mock.patch.object(three_way, "execute_three_way_mutations", side_effect=RuntimeError("synthetic mutation failure")), \
                mock.patch.object(three_way, "restore_outer_snapshot", return_value={"status": "incomplete", "errors": ["fixture locked"], "failed_state_root": "fixture"}):
            with self.assertRaisesRegex(core.SyncError, "rollback was incomplete"):
                three_way.run_three_way_sync(*roots, self.base / "backup")
        run = next((self.base / "backup").iterdir())
        self.assertEqual(json.loads((run / "run-state.json").read_text())["status"], "recovery_required")
        three_way.prune_outer_snapshots(self.base / "backup", keep=0)
        three_way.rotate_three_way_backups(self.base / "backup", keep=0)
        self.assertTrue((run / "outer-snapshot").is_dir())

    def test_interrupted_preparation_retries_with_fresh_attempt(self):
        root = self.history()
        owner = str(uuid.uuid4())
        canonical = self.rollout(root, owner)
        old = root / "sessions" / ("rollout-conflict-" + owner + ".jsonl")
        canonical.rename(old)
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
            db.execute("UPDATE threads SET rollout_path=?", (str(old),))
        plan = repair.build_repair_plan([root])
        backup = self.base / "rb"
        with mock.patch.object(repair, "require_codex_closed"), \
                mock.patch.object(repair, "sqlite_backup", side_effect=OSError("synthetic copy failure")):
            with self.assertRaisesRegex(OSError, "synthetic copy failure"):
                repair.apply_plan(plan, backup)
        self.assertEqual(json.loads((backup / "manifest.json").read_text())["status"], "preparing")
        with mock.patch.object(repair, "require_codex_closed"):
            result = repair.apply_plan(plan, backup)
        self.assertEqual(result["status"], "applied")
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertTrue(manifest["attempt"].startswith("attempts"))
        self.assertTrue((backup / root.name).is_dir())

    def test_noop_sidebar_repair_does_not_create_database_backups(self):
        for name in (".codex-personal", ".codex-plus", ".codex-sota"):
            root = self.history(name)
            self.rollout(root, str(uuid.uuid4()), archived=1)
        with mock.patch.object(sidebar.Path, "home", return_value=self.base), \
                mock.patch.object(sidebar, "__file__", str(self.base / "repair.py")), \
                mock.patch.object(sidebar, "app_running", return_value=False), \
                mock.patch.object(sidebar, "backup_sqlite_database") as backup, redirect_stdout(io.StringIO()):
            sidebar.main()
        backup.assert_not_called()
        self.assertFalse((self.base / "backups").exists())

    def test_sidebar_completed_backups_are_bounded_but_unknown_backups_are_preserved(self):
        completed = [self.sidebar_backup(str(index)) for index in range(4)]
        protected = [self.sidebar_backup("legacy", status=None),
                     self.sidebar_backup("interrupted", status="applying"),
                     self.sidebar_backup("failed", status="recovery_required")]
        malformed = self.sidebar_backup("bad-json")
        (malformed / sidebar.BACKUP_STATE_FILE).write_text("{broken", encoding="utf-8")
        protected.append(malformed)
        foreign = self.sidebar_backup("foreign-format")
        (foreign / sidebar.BACKUP_STATE_FILE).write_text(json.dumps({
            "format": "unknown", "status": "completed", "backup_name": foreign.name,
        }), encoding="utf-8")
        protected.append(foreign)
        removed = sidebar.prune_completed_backups(self.base / "backups", keep=2)
        self.assertEqual(set(removed), {str(path) for path in completed[:2]})
        self.assertTrue(all(path.is_dir() for path in completed[2:] + protected))
        self.assertTrue(all((path / "fixture.sqlite").read_bytes() == b"synthetic saved state"
                            for path in protected))

    def test_sidebar_success_writes_completion_receipt_before_bounded_rotation(self):
        old = [self.sidebar_backup("20000101-000000-" + str(index).zfill(6))
               for index in range(sidebar.MAX_COMPLETED_BACKUPS + 1)]
        legacy = self.sidebar_backup("19990101-000000-000000", status=None)
        for name in (".codex-personal", ".codex-plus", ".codex-sota"):
            root = self.history(name)
            self.rollout(root, str(uuid.uuid4()), archived=1)
            if name == ".codex-personal":
                with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
                    db.execute("UPDATE threads SET thread_section_id='old-section',is_pinned=1")
        with mock.patch.object(sidebar.Path, "home", return_value=self.base), \
                mock.patch.object(sidebar, "__file__", str(self.base / "repair.py")), \
                mock.patch.object(sidebar, "app_running", return_value=False), redirect_stdout(io.StringIO()):
            sidebar.main()
        completed = [path for path in (self.base / "backups").iterdir()
                     if (path / sidebar.BACKUP_STATE_FILE).is_file()]
        self.assertEqual(len(completed), sidebar.MAX_COMPLETED_BACKUPS)
        self.assertFalse(old[0].exists())
        self.assertTrue(legacy.is_dir())
        newest = max(completed, key=lambda path: path.name)
        self.assertEqual(json.loads((newest / sidebar.BACKUP_STATE_FILE).read_text(encoding="utf-8"))["status"], "completed")
        with closing(sqlite3.connect(newest / ".codex-personal" / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT is_pinned FROM threads").fetchone()[0], 1)
        with closing(sqlite3.connect(self.base / ".codex-personal" / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT is_pinned FROM threads").fetchone()[0], 0)

    def test_sidebar_partial_failure_never_marks_backup_safe_to_prune(self):
        for name in (".codex-personal", ".codex-plus", ".codex-sota"):
            root = self.history(name)
            self.rollout(root, str(uuid.uuid4()), archived=1)
        personal = self.base / ".codex-personal"
        with closing(sqlite3.connect(personal / "state_5.sqlite")) as db, db:
            db.execute("UPDATE threads SET is_pinned=1")
        (personal / ".codex-global-state.json").write_bytes(b"{invalid-json")
        with mock.patch.object(sidebar.Path, "home", return_value=self.base), \
                mock.patch.object(sidebar, "__file__", str(self.base / "repair.py")), \
                mock.patch.object(sidebar, "app_running", return_value=False), redirect_stdout(io.StringIO()):
            with self.assertRaises(json.JSONDecodeError):
                sidebar.main()
        backup = next((self.base / "backups").iterdir())
        self.assertFalse((backup / sidebar.BACKUP_STATE_FILE).exists())
        self.assertEqual(sidebar.prune_completed_backups(backup.parent, keep=0), [])
        with closing(sqlite3.connect(backup / ".codex-personal" / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT is_pinned FROM threads").fetchone()[0], 1)

    def test_sidebar_retention_rechecks_completion_before_deleting(self):
        backup = self.sidebar_backup("receipt-race")
        checks = 0

        def check_tree(_path):
            nonlocal checks
            checks += 1
            if checks == 2:
                (backup / sidebar.BACKUP_STATE_FILE).write_text(json.dumps({
                    "format": sidebar.BACKUP_FORMAT, "status": "recovery_required",
                    "backup_name": backup.name,
                }), encoding="utf-8")
            return True

        with mock.patch.object(sidebar, "_plain_backup_tree", side_effect=check_tree):
            self.assertEqual(sidebar.prune_completed_backups(backup.parent, keep=0), [])
        self.assertTrue((backup / "fixture.sqlite").is_file())

    def test_sidebar_backup_validation_rejects_windows_junction_without_traversal(self):
        path = mock.Mock()
        path.lstat.return_value = SimpleNamespace(st_mode=sidebar.stat.S_IFDIR,
                                                  st_file_attributes=0x400)
        self.assertFalse(sidebar._plain_backup_tree(path))
        path.iterdir.assert_not_called()

    def test_archived_placement_probe_is_read_only_and_uses_same_predicate(self):
        root = self.history()
        owner = str(uuid.uuid4())
        self.rollout(root, owner, archived=1)
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
            db.execute("UPDATE threads SET thread_section_id='section', is_pinned=1")
            self.assertEqual(core.clear_archived_sidebar_placement(db, dry_run=True), 1)
            self.assertEqual(db.execute("SELECT is_pinned FROM threads").fetchone()[0], 1)
            self.assertEqual(core.clear_archived_sidebar_placement(db), 1)
            self.assertEqual(core.clear_archived_sidebar_placement(db, dry_run=True), 0)

    def test_watcher_fast_exit_still_waits_for_global_quiet_and_syncs(self):
        args = SimpleNamespace(audit_only=False, pid=123, app_executable=None, creation_ticks=None, quiet_seconds=1)
        with mock.patch.object(watcher, "build_parser") as parser, \
                mock.patch.object(watcher, "setup_logging"), \
                mock.patch.object(watcher, "FileLock", return_value=nullcontext()), \
                mock.patch.object(watcher, "wait_for_process", return_value=False), \
                mock.patch.object(watcher, "wait_until_app_is_fully_closed") as quiet, \
                mock.patch.object(watcher, "run_archived_sidebar_repair", return_value={}), \
                mock.patch.object(watcher, "run_pending_repair", return_value={}), \
                mock.patch.object(watcher, "run_sync_with_retry", return_value={"status": "ok"}) as sync:
            parser.return_value.parse_args.return_value = args
            self.assertEqual(watcher.main(), 0)
        quiet.assert_called_once_with(1.0)
        sync.assert_called_once()

    def test_watcher_audit_never_initializes_file_logging(self):
        args = SimpleNamespace(audit_only=True)
        with mock.patch.object(watcher, "build_parser") as parser, \
                mock.patch.object(watcher, "setup_logging") as logging_setup, \
                mock.patch.object(watcher, "chatgpt_processes_running", return_value=False), redirect_stdout(io.StringIO()):
            parser.return_value.parse_args.return_value = args
            self.assertEqual(watcher.main(), 0)
        logging_setup.assert_not_called()

    def test_two_way_entrypoint_checks_lifecycle_before_sync(self):
        args = SimpleNamespace(left=self.base / "a", right=self.base / "b", backup_base=self.base / "backup",
                               left_provider="left", right_provider="right", json=True)
        with mock.patch.object(core, "build_parser") as parser, \
                mock.patch.object(core, "setup_logging", return_value=self.base / "log"), \
                mock.patch.object(core, "write_last_result"), \
                mock.patch.object(lifecycle, "lifecycle_lock", return_value=nullcontext()) as lock, \
                mock.patch.object(lifecycle, "assert_quiescent", side_effect=lifecycle.AppNotQuiescent("app_running")), \
                mock.patch.object(core, "run_sync") as sync, redirect_stdout(io.StringIO()):
            parser.return_value.parse_args.return_value = args
            self.assertEqual(core.main(), 1)
        lock.assert_called_once()
        sync.assert_not_called()

    def test_audit_explicitly_reports_content_not_compared(self):
        roots = [self.history(name) for name in ("a", "b", "c")]
        with mock.patch.object(three_way, "account_ids_for_roots", return_value=set()), \
                mock.patch.object(core, "verify_roots", side_effect=lambda roots, *_args, **_kwargs: {str(roots[0]): {}}):
            result = three_way.audit_roots(*roots)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["content_verified"])
        self.assertIn("not compared", result["validation_scope"])

    def test_powershell_mutation_guards_remain_before_side_effects(self):
        # Behavioral PS fixtures live in check_sota_launch_lifecycle.ps1; this
        # guard deliberately reads sources instead of launching PowerShell.
        root = Path(core.__file__).parent
        router = (root / "Start-CodexSotaRouter.ps1").read_text(encoding="utf-8-sig")
        profile = (root / "Switch-CodexProfile.ps1").read_text(encoding="utf-8-sig")
        runner = (root / "Run-CodexHistorySync.ps1").read_text(encoding="utf-8-sig")
        sota = (root / "Switch-CodexSota.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("if ($Stop -and -not $AuditOnly)", router)
        self.assertIn("if ($PrepareOnly -and -not $AuditOnly)", profile)
        self.assertIn("if (-not $NoGui -and -not $AuditOnly -and -not $DeferIfAppRunning)", runner)
        self.assertIn("if ($restartAppWhenDone -and $syncSafeToRestart)", runner)
        status = sota.split("function Write-LaunchStatus {", 1)[1].split("function Set-LaunchRequest", 1)[0]
        self.assertLess(status.index("if ($AuditOnly)"), status.index("[IO.Directory]::CreateDirectory"))
        stop = sota.split("function Stop-CodexApp {", 1)[1].split("function Start-SotaApp", 1)[0]
        self.assertNotIn("Stop-Process -", stop)
        self.assertNotIn("Stop-CodexApp -ForceHidden", sota)

    def test_reference_entities_are_copied_as_a_closure_without_overwriting_unrelated_projects(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        path = self.rollout(left, owner)
        for root in (left, right):
            self.reference_schema(root)
        self.add_project(left, "referenced")
        self.add_project(left, "unreferenced")
        self.add_project(right, "target-only", name="Keep me")
        self.assign_refs(left, owner, "referenced")
        snapshots = [core.load_root_snapshot(root) for root in (left, right)]
        core.sync_databases(*snapshots, {left: {owner: path}, right: {owner: right / "sessions" / "copy.jsonl"}}, [], {left: "fixture", right: "fixture"})
        with closing(sqlite3.connect(right / "state_5.sqlite")) as db:
            self.assertEqual(set(db.execute("SELECT id,name FROM projects")), {("referenced", "Project"), ("target-only", "Keep me")})
            self.assertEqual(db.execute("SELECT project_id,thread_section_id FROM threads").fetchone(), ("referenced", "section"))
            self.assertEqual(db.execute("SELECT path FROM project_roots WHERE project_id='referenced'").fetchone(), ("C:/fixture/project",))
            self.assertEqual(db.execute("SELECT project_id FROM project_idempotency_keys WHERE key='key-referenced'").fetchone(), ("referenced",))
            self.assertEqual(db.execute("SELECT name FROM thread_sections WHERE id='section'").fetchone(), ("Section",))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_project_and_section_renames_keep_target_display_fields(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        for root, label in ((left, "Old name"), (right, "Renamed")):
            self.rollout(root, owner)
            self.reference_schema(root)
            self.add_project(root, "shared", name=label)
            self.assign_refs(root, owner, "shared", section_name=label)
        source, destination = core.load_root_snapshot(left), core.load_root_snapshot(right)
        plan = core.plan_thread_reference_rows(destination, [(source, source.threads[owner])])
        self.assertFalse(any(plan.values()))
        with closing(sqlite3.connect(right / "state_5.sqlite")) as db:
            self.assertEqual(db.execute("SELECT name FROM projects").fetchone()[0], "Renamed")
            self.assertEqual(db.execute("SELECT name FROM thread_sections").fetchone()[0], "Renamed")

    def test_conflicting_project_identity_stops_before_either_database_changes(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        path = self.rollout(left, owner)
        for root, created in ((left, 10), (right, 99)):
            self.reference_schema(root)
            self.add_project(root, "collision", created=created)
        self.assign_refs(left, owner, "collision")
        snapshots = [core.load_root_snapshot(root) for root in (left, right)]
        before = {root: (root / "state_5.sqlite").read_bytes() for root in (left, right)}
        with self.assertRaisesRegex(core.SyncError, "身份冲突"):
            core.sync_databases(*snapshots, {left: {owner: path}, right: {owner: path}}, [], {left: "fixture", right: "fixture"})
        self.assertEqual(before, {root: (root / "state_5.sqlite").read_bytes() for root in (left, right)})

    def test_conflicting_root_slot_is_not_overwritten(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        self.rollout(left, owner)
        for root, path in ((left, "C:/source-project"), (right, "C:/unrelated-project")):
            self.reference_schema(root)
            self.add_project(root, "collision", path=path)
        self.assign_refs(left, owner, "collision")
        source, destination = core.load_root_snapshot(left), core.load_root_snapshot(right)
        with self.assertRaisesRegex(core.SyncError, "project_roots"):
            core.plan_thread_reference_rows(destination, [(source, source.threads[owner])])

    def test_section_collision_without_shared_membership_is_not_silently_bound(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        for root, thread_id, name in ((left, owner, "Source section"), (right, str(uuid.uuid4()), "Other section")):
            self.rollout(root, thread_id)
            self.reference_schema(root)
            self.add_project(root, "shared")
            self.assign_refs(root, thread_id, "shared", section_name=name)
        source, destination = core.load_root_snapshot(left), core.load_root_snapshot(right)
        with self.assertRaisesRegex(core.SyncError, "thread_sections"):
            core.plan_thread_reference_rows(destination, [(source, source.threads[owner])])

    def test_duplicate_references_are_planned_once(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        self.rollout(left, owner)
        for root in (left, right):
            self.reference_schema(root)
        self.add_project(left, "shared")
        self.assign_refs(left, owner, "shared")
        source, destination = core.load_root_snapshot(left), core.load_root_snapshot(right)
        selected = (source, source.threads[owner])
        plan = core.plan_thread_reference_rows(destination, [selected, selected])
        self.assertEqual({name: len(rows) for name, rows in plan.items()}, {"projects": 1, "project_roots": 1, "project_idempotency_keys": 1, "thread_sections": 1})

    def test_old_destination_schema_without_reference_columns_stays_supported(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        self.rollout(left, owner)
        self.reference_schema(left)
        self.add_project(left, "project")
        source, destination = core.load_root_snapshot(left), core.load_root_snapshot(right)
        source.threads[owner]["project_id"] = "project"
        self.assertFalse(any(core.plan_thread_reference_rows(destination, [(source, source.threads[owner])]).values()))

    def test_conflicting_idempotency_key_is_not_rebound(self):
        left, right = self.history("a"), self.history("b")
        owner = str(uuid.uuid4())
        self.rollout(left, owner)
        for root in (left, right):
            self.reference_schema(root)
        self.add_project(left, "source")
        self.add_project(right, "target")
        self.assign_refs(left, owner, "source")
        with closing(sqlite3.connect(right / "state_5.sqlite")) as db, db:
            db.execute("INSERT INTO project_idempotency_keys VALUES('key-source','target',10)")
        source, destination = core.load_root_snapshot(left), core.load_root_snapshot(right)
        with self.assertRaisesRegex(core.SyncError, "project_idempotency_keys"):
            core.plan_thread_reference_rows(destination, [(source, source.threads[owner])])


if __name__ == "__main__":
    unittest.main()
