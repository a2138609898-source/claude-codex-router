"""Account-only history catch-up: disposable profiles, never real app processes."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

CORE = Path(__file__).resolve().parent.parent / "CodexHistorySync"
sys.path.insert(0, str(CORE))
import sync_chatgpt_account_history as account
import sync_codex_histories as core


@contextlib.contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def sid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "account-sync-fixture:" + name))


def fingerprints(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        # Leave room for the deliberately deep paginated staging paths on
        # Windows, including when TEMP itself is an 8.3 alias.
        self.temp = tempfile.TemporaryDirectory(prefix="acct-")
        self.addCleanup(self.temp.cleanup)
        # Match the synchronizer's physical spelling, including hosted Windows
        # runners whose TEMP uses an 8.3 alias. Fault injection must hit writes.
        self.base = core.canonical_storage_path(self.temp.name)
        self.source, self.target = self.base / "sota", self.base / "account"
        self.work = self.base / "work"
        for root in (self.source, self.target):
            root.mkdir()
            (root / "sessions").mkdir()
            (root / "archived_sessions").mkdir()
            (root / "config.toml").write_text('model="fixture-account-model"\n', encoding="utf-8")
            (root / "auth.json").write_text('{"fixture":"never-copy-credentials"}', encoding="utf-8")
            (root / "providers.json").write_text('{"fixture":"never-copy-provider-settings"}')
            with database(root / account.STATE) as db:
                db.executescript('''
                    CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, checksum TEXT);
                    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT DEFAULT '{}',
                        position INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER);
                    CREATE TABLE project_roots (project_id TEXT REFERENCES projects(id), position INTEGER, path TEXT,
                        PRIMARY KEY(project_id,position));
                    CREATE TABLE thread_sections (id TEXT PRIMARY KEY, name TEXT, appearance TEXT);
                    CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER,
                        updated_at INTEGER, source TEXT, model_provider TEXT, cwd TEXT, title TEXT,
                        sandbox_policy TEXT, approval_mode TEXT, tokens_used INTEGER DEFAULT 0,
                        has_user_event INTEGER DEFAULT 1, archived INTEGER DEFAULT 0, archived_at INTEGER,
                        model TEXT, reasoning_effort TEXT, name TEXT, preview TEXT, updated_at_ms INTEGER,
                        recency_at_ms INTEGER, is_pinned INTEGER DEFAULT 0,
                        project_id TEXT REFERENCES projects(id), thread_section_id TEXT REFERENCES thread_sections(id),
                        section_position INTEGER, section_entered_at_ms INTEGER);
                    CREATE TABLE thread_dynamic_tools (thread_id TEXT REFERENCES threads(id), position INTEGER,
                        name TEXT, description TEXT, input_schema TEXT, PRIMARY KEY(thread_id,position));
                    CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY,status TEXT);
                ''')
                db.execute("INSERT INTO _sqlx_migrations VALUES (?,?)", (1, root.name))
            with database(root / account.HISTORY) as db:
                db.executescript('''
                    CREATE TABLE thread_items(thread_id TEXT,item_json TEXT);
                    CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_byte_offset INTEGER);
                    CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER);
                    CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY,checksum TEXT);
                    INSERT INTO _sqlx_migrations VALUES(1,'keep-account-migration');
                ''')

    def thread(self, root, name, messages=("hello",), *, archived=0, **fields):
        thread_id = sid(name)
        provider = "tango_relay" if root == self.source else "openai"
        path = root / ("archived_sessions" if archived else "sessions") / f"rollout-2026-10-29T00-00-00-{thread_id}.jsonl"
        records = [{"type": "session_meta", "ordinal": 0, "timestamp": "2026-10-29T00:00:00Z",
                    "payload": {"id": thread_id, "model_provider": provider}}]
        records += [{"type": "event_msg", "ordinal": number, "payload": {"type": "user_message", "message": message}}
                    for number, message in enumerate(messages, 1)]
        path.write_bytes(b"".join(account.json_bytes(item) for item in records))
        row = dict(id=thread_id, rollout_path=str(path), created_at=1, updated_at=10, source="cli",
                   model_provider=provider, cwd=str(self.base), title="raw initial prompt " + name,
                   name="界面名称 " + name, sandbox_policy="read-only", approval_mode="never",
                   model="vendor--fixture-model" if root == self.source else "account-choice", archived=archived,
                   has_user_event=1, is_pinned=0, project_id=None, thread_section_id=None, **{})
        row.update(fields)
        with database(root / account.STATE) as db:
            core.upsert_thread(db, row, path)
        return thread_id, path

    def rows(self):
        with database(self.target / account.STATE) as db:
            return {r["id"]: r for r in core.rows_as_dicts(db, "threads")}

    def run_sync(self, **kwargs):
        before = fingerprints(self.source)
        config_before = {name: (self.target / name).read_bytes() for name in ("config.toml", "auth.json", "providers.json")}
        result = account.sync_account(self.source, self.target, self.work,
                                      guard=lambda: None, rollback_guard=lambda: None, **kwargs)
        self.assertEqual(fingerprints(self.source), before)
        self.assertEqual({name: (self.target / name).read_bytes() for name in config_before}, config_before)
        return result

    def test_new_history_preserves_id_name_provider_and_account_credentials(self):
        thread_id, path = self.thread(self.source, "new")
        result = self.run_sync()
        row = self.rows()[thread_id]
        self.assertEqual(result["new_threads"], 1)
        self.assertEqual(row["name"], "界面名称 new")
        self.assertEqual(row["model_provider"], "openai")
        self.assertEqual(row["model"], "fixture-account-model")
        self.assertEqual(account.read_header(Path(row["rollout_path"]))["model_provider"], "openai")
        index = account.read_index(self.target / account.INDEX)
        self.assertEqual(index[thread_id]["thread_name"], row["name"])
        self.assertNotEqual(path, Path(row["rollout_path"]))

    def test_prefix_extension_updates_without_losing_original_backup(self):
        thread_id, source = self.thread(self.source, "extended", ("one", "two"))
        _, original = self.thread(self.target, "extended", ("one",))
        before = original.read_bytes()
        result = self.run_sync()
        self.assertEqual(result["updated_threads"], 1)
        self.assertIn(b'two', Path(self.rows()[thread_id]["rollout_path"]).read_bytes())
        self.assertEqual((Path(result["backup_dir"]) / "before" / original.relative_to(self.target)).read_bytes(), before)
        self.assertFalse(original.exists())

    def test_new_account_import_without_pins_uses_official_defaults(self):
        (self.target / "config.toml").write_text('forced_login_method="chatgpt"\n')
        thread_id, source = self.thread(self.source, "official-default", reasoning_effort="ultra")
        self.run_sync()
        row = self.rows()[thread_id]
        self.assertEqual(row["model_provider"], "openai")
        self.assertIsNone(row["model"])
        self.assertIsNone(row["reasoning_effort"])
        self.assertEqual(source.read_bytes().splitlines()[1:],
                         Path(row["rollout_path"]).read_bytes().splitlines()[1:])

    def test_account_effort_preference_survives_without_a_pinned_model(self):
        (self.target / "config.toml").write_text('model_reasoning_effort="high"\n')
        thread_id, _ = self.thread(self.source, "official-effort", reasoning_effort="ultra")
        self.run_sync()
        row = self.rows()[thread_id]
        self.assertIsNone(row["model"])
        self.assertEqual(row["reasoning_effort"], "high")

    def test_existing_account_choice_survives_when_global_default_is_unset(self):
        (self.target / "config.toml").write_text('forced_login_method="chatgpt"\n')
        thread_id, _ = self.thread(self.source, "existing-official", ("one", "two"))
        self.thread(self.target, "existing-official", ("one",), model="chosen-official-model", reasoning_effort="high")
        self.run_sync()
        row = self.rows()[thread_id]
        self.assertEqual(row["model"], "chosen-official-model")
        self.assertEqual(row["reasoning_effort"], "high")

    def test_account_ahead_and_account_only_history_are_kept_exactly(self):
        self.thread(self.source, "ahead", ("one",))
        _, ahead = self.thread(self.target, "ahead", ("one", "account continued"))
        only, only_path = self.thread(self.target, "only", ("private account chat",))
        before = {p: p.read_bytes() for p in (ahead, only_path)}
        result = self.run_sync()
        self.assertEqual(result["account_ahead"], 1)
        self.assertEqual(result["account_only_threads_preserved"], 1)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertIn(only, self.rows())

    def test_divergence_keeps_account_under_original_id_and_source_visible_once(self):
        original_id, _ = self.thread(self.source, "branch", ("common", "SOTA continued"))
        _, account_path = self.thread(self.target, "branch", ("common", "account continued"))
        before = account_path.read_bytes()
        result = self.run_sync()
        self.assertEqual(result["conflicts_preserved"], 1)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[original_id]["rollout_path"], str(account_path))
        self.assertEqual(account_path.read_bytes(), before)
        clone = next(row for key, row in self.rows().items() if key != original_id)
        self.assertTrue(clone["name"].endswith(account.BRANCH_SUFFIX))
        self.assertIn(b'SOTA continued', Path(clone["rollout_path"]).read_bytes())
        again = self.run_sync()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(again["installed_files"], 0)

    def test_repeated_sync_is_noop_even_with_newer_source_schema(self):
        with database(self.source / account.STATE) as db:
            db.execute("ALTER TABLE threads ADD COLUMN originator TEXT")
        self.thread(self.source, "new-schema", originator="source-only")
        self.run_sync()
        before = fingerprints(self.target)
        with mock.patch.object(account, "relation", side_effect=AssertionError("unchanged history must use ledger")):
            result = self.run_sync()
        self.assertEqual(result["installed_files"], 0)
        self.assertEqual(fingerprints(self.target), before)
        with database(self.target / account.STATE) as db:
            self.assertEqual(db.execute("SELECT checksum FROM _sqlx_migrations").fetchone()[0], "account")
            self.assertNotIn("originator", core.table_columns(db, "threads"))

    def test_account_rename_and_unpin_remain_protected_across_three_syncs(self):
        thread_id, _ = self.thread(self.source, "renamed", is_pinned=1)
        self.run_sync()
        with database(self.target / account.STATE) as db:
            db.execute("UPDATE threads SET name='account rename',is_pinned=0 WHERE id=?", (thread_id,))
        for _ in range(3):
            self.run_sync()
            self.assertEqual(self.rows()[thread_id]["name"], "account rename")
            self.assertEqual(self.rows()[thread_id]["is_pinned"], 0)
            state = account.read_object(self.target / account.GLOBAL)
            self.assertNotIn(thread_id, state["pinned-thread-ids"])

    def test_source_rename_still_flows_when_account_has_not_changed_it(self):
        thread_id, _ = self.thread(self.source, "source rename")
        self.run_sync()
        with database(self.source / account.STATE) as db:
            db.execute("UPDATE threads SET name='new SOTA name' WHERE id=?", (thread_id,))
        self.run_sync()
        self.assertEqual(self.rows()[thread_id]["name"], "new SOTA name")

    def test_archived_history_is_copied_but_not_resurrected_in_sidebar(self):
        thread_id, _ = self.thread(self.source, "archived", archived=1)
        self.thread(self.target, "archived", archived=0)
        self.run_sync()
        row = self.rows()[thread_id]
        self.assertEqual(row["archived"], 1)
        self.assertIn("archived_sessions", Path(row["rollout_path"]).parts)
        self.assertNotIn(thread_id, account.read_index(self.target / account.INDEX))
        self.assertNotIn(thread_id, account.read_object(self.target / account.GLOBAL)["projectless-thread-ids"])
        self.assertFalse(any((self.target / "sessions").rglob("*.jsonl")))
        self.assertEqual(self.run_sync()["installed_files"], 0)

    def test_projects_sections_tools_edges_and_host_identity_follow_threads(self):
        project_id, section_id = sid("project"), sid("section")
        with database(self.source / account.STATE) as db:
            db.execute("INSERT INTO projects VALUES (?,?,?,0,1,2)", (project_id, "Project", "{}"))
            db.execute("INSERT INTO project_roots VALUES (?,0,?)", (project_id, str(self.base)))
            db.execute("INSERT INTO thread_sections VALUES (?,?,'{}')", (section_id, "Section"))
        parent, _ = self.thread(self.source, "project-parent", project_id=project_id, thread_section_id=section_id, is_pinned=1)
        child, _ = self.thread(self.source, "project-child", source="subagent")
        with database(self.source / account.STATE) as db:
            db.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'completed')", (parent, child))
            db.execute("INSERT INTO thread_dynamic_tools VALUES (?,0,'fixture','desc','{}')", (parent,))
        state = {"thread-project-assignments": {parent: {"projectId": project_id}},
                 "thread-project-membership-host-ids": {parent: "local:" + str(self.source)},
                 "app-server-project-id-by-legacy-project-id-by-host": {"local:" + str(self.source): {"legacy": project_id}},
                 "queued-follow-ups": {"do-not-copy": "source work"},
                 core.ATOM_STATE_KEY: {"composer-prompt-drafts-v1": {"private": "source draft"}}}
        (self.source / account.GLOBAL).write_bytes(account.json_bytes(state))
        target_state = {"queued-follow-ups": {"account": "preserve"}, core.ATOM_STATE_KEY: {"theme": "keep", "composer-prompt-drafts-v1": {"account": "keep"}}}
        (self.target / account.GLOBAL).write_bytes(account.json_bytes(target_state))
        self.run_sync()
        result = account.read_object(self.target / account.GLOBAL)
        self.assertEqual(result["queued-follow-ups"], target_state["queued-follow-ups"])
        self.assertEqual(result[core.ATOM_STATE_KEY]["composer-prompt-drafts-v1"], {"account": "keep"})
        self.assertEqual(result["thread-project-membership-host-ids"][parent], "local:" + str(self.target))
        self.assertIn("local:" + str(self.target), result["app-server-project-id-by-legacy-project-id-by-host"])
        self.assertEqual(self.rows()[parent]["project_id"], project_id)
        self.assertEqual(self.rows()[parent]["thread_section_id"], section_id)
        self.assertNotIn(child, account.read_index(self.target / account.INDEX))
        with database(self.target / account.STATE) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_roots").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM thread_dynamic_tools").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT parent_thread_id FROM thread_spawn_edges").fetchone()[0], parent)

    def test_old_projection_offsets_are_invalidated_only_for_changed_threads(self):
        changed, _ = self.thread(self.source, "cached", ("one", "two"))
        self.thread(self.target, "cached", ("one",))
        keep, _ = self.thread(self.target, "other cached")
        with database(self.target / account.HISTORY) as db:
            for thread_id in (changed, keep):
                db.execute("INSERT INTO thread_items VALUES (?, 'cache')", (thread_id,))
                db.execute("INSERT INTO thread_history_projection_state VALUES (?,123)", (thread_id,))
        result = self.run_sync()
        with database(self.target / account.HISTORY) as db:
            self.assertEqual(db.execute("SELECT thread_id FROM thread_items").fetchall(), [(keep,)])
            self.assertEqual(db.execute("SELECT thread_id FROM thread_history_projection_state").fetchall(), [(keep,)])
            self.assertEqual(db.execute("SELECT checksum FROM _sqlx_migrations").fetchone()[0], "keep-account-migration")
        self.assertTrue((Path(result["backup_dir"]) / "before" / account.HISTORY).exists())

    def paginated(self, root, name, ancestor_name, tail):
        thread_id, path = self.thread(root, name, ())
        ancestor = root / "archived_sessions" / f"rollout-2026-10-29T00-00-00-{sid(ancestor_name)}.jsonl"
        owner = sid(ancestor_name)
        lines = [dict(type="session_meta", ordinal=0, timestamp="2026-10-29T00:00:00Z", payload={"id": owner,"model_provider":"tango_relay"}),
                 dict(type="event_msg", ordinal=1, payload={"type":"user_message","message":"ancestor"})]
        ancestor.write_bytes(b"".join(account.json_bytes(item) for item in lines))
        header = dict(type="session_meta", ordinal=2, payload={"id":thread_id,"model_provider":"tango_relay",
                      "history_base":{"thread_id":sid(ancestor_name),"end_ordinal_exclusive":2,"end_byte_offset":ancestor.stat().st_size}})
        record = dict(type="event_msg", ordinal=3, payload={"type":"user_message","message":tail})
        path.write_bytes(account.json_bytes(header) + account.json_bytes(record))
        return thread_id, path, ancestor

    def test_paginated_ancestor_is_preserved_and_correctly_bound(self):
        thread_id, _, ancestor = self.paginated(self.source, "page head", "ancestor", "tail")
        self.run_sync()
        head = Path(self.rows()[thread_id]["rollout_path"])
        inventory = core.lineage_inventory(self.target)
        page = inventory[0][account.read_header(head)["history_base"]["thread_id"]]
        self.assertEqual(page.read_bytes(), ancestor.read_bytes())
        core.validate_lineage([self.target], check_offsets=True)
        self.assertEqual(self.run_sync()["installed_files"], 0)

    def test_same_physical_uuid_with_different_target_content_is_not_overwritten(self):
        thread_id, _, ancestor = self.paginated(self.source, "page collision", "ancestor collision", "tail")
        other = self.target / "archived_sessions" / ancestor.name
        other.write_bytes(ancestor.read_bytes().replace(b'ancestor', b'different account history'))
        original = other.read_bytes()
        self.run_sync()
        self.assertEqual(other.read_bytes(), original)
        head = Path(self.rows()[thread_id]["rollout_path"])
        self.assertNotEqual(account.read_header(head)["history_base"]["thread_id"], core.physical_page_id(ancestor))
        core.validate_lineage([self.target], check_offsets=True)

    def test_missing_ancestor_aborts_without_account_writes(self):
        _, _, ancestor = self.paginated(self.source, "broken", "missing ancestor", "tail")
        ancestor.unlink()
        before = fingerprints(self.target)
        with self.assertRaises((account.AccountSyncError, core.SyncError)):
            self.run_sync()
        self.assertEqual(fingerprints(self.target), before)

    def test_missing_source_head_keeps_intact_account_and_still_imports_new_chats(self):
        thread_id, missing = self.thread(self.source, "old missing")
        with database(self.source / account.STATE) as db:
            db.execute("UPDATE threads SET name='source sidebar name' WHERE id=?", (thread_id,))
        missing.unlink()
        _, intact = self.thread(self.target, "old missing", ("account keeps full conversation",))
        self.thread(self.source, "new beside missing")
        original = intact.read_bytes()
        result = self.run_sync()
        self.assertEqual(result["new_threads"], 1)
        self.assertEqual(result["source_unavailable_kept_account"], 1)
        self.assertEqual(self.rows()[thread_id]["name"], "source sidebar name")
        self.assertEqual(intact.read_bytes(), original)
        self.assertEqual(self.run_sync()["installed_files"], 0)

    def test_missing_source_ancestor_keeps_valid_account_copy_without_source_repair(self):
        thread_id, _, ancestor = self.paginated(self.source, "old paginated missing", "lost page", "source tail")
        ancestor.unlink()
        _, intact = self.thread(self.target, "old paginated missing", ("all account content",))
        original = intact.read_bytes()
        self.run_sync()
        self.assertEqual(intact.read_bytes(), original)
        self.assertFalse(ancestor.exists())
        self.assertEqual(len(self.rows()), 1)

    def test_account_continued_source_branch_creates_new_branch_without_overwrite(self):
        thread_id, source = self.thread(self.source, "rebranch", ("common", "SOTA"))
        self.thread(self.target, "rebranch", ("common", "account"))
        self.run_sync()
        clone_id = next(key for key in self.rows() if key != thread_id)
        clone = Path(self.rows()[clone_id]["rollout_path"])
        extra = account.json_bytes({"type":"event_msg","ordinal":3,"payload":{"type":"user_message","message":"account edited clone"}})
        clone.write_bytes(clone.read_bytes() + extra)
        source.write_bytes(source.read_bytes() + extra.replace(b'account edited clone', b'SOTA continued again'))
        original = clone.read_bytes()
        self.run_sync()
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(clone.read_bytes(), original)
        self.assertEqual(self.run_sync()["installed_files"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows process/lease interoperability")
    def test_real_powershell_parent_and_venv_redirector_lend_only_lifecycle_lock(self):
        # Exercise the real borrowed guard *inside* a complete sync, not only
        # before the checkpoint scope is installed. All profiles/locks are fixtures.
        new_id, _ = self.thread(self.source, "borrowed new")
        updated_id, _ = self.thread(self.source, "borrowed extended", ("one", "two"))
        self.thread(self.target, "borrowed extended", ("one",))
        only_id, only_path = self.thread(self.target, "borrowed account only")
        page_id, _, _ = self.paginated(self.source, "borrowed paginated", "borrowed ancestor", "tail")
        source_before = fingerprints(self.source)
        only_before = only_path.read_bytes()
        config_before = {name: (self.target / name).read_bytes()
                         for name in ("config.toml", "auth.json", "providers.json")}
        probe = self.base / "probe.py"
        probe.write_text('''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import sync_chatgpt_account_history as a
import codex_app_lifecycle as life
base=Path(sys.argv[2])
life.LIFECYCLE_LOCK_PATH=base/'lifecycle.lock'
life.LAUNCH_REQUEST_DIR=base/'requests'
life.app_running=lambda:False
a.core.LOCK_PATH=base/'sync.lock'
guard=a.borrowed_launcher_guard(int(sys.argv[3]),sys.argv[4],'fixture')
with life.FileLock(base/'sync.lock'):
    with a.core.history_checkpoint_scope(guard):
        a.core.history_checkpoint(force=True)
arguments=['--source',str(base/'sota'),'--target',str(base/'account'),
           '--work-base',str(base/'work'),'--launcher-pid',sys.argv[3],
           '--launcher-creation-ticks',sys.argv[4],'--launch-request-id','fixture']
for _ in range(2):
    code=a.main(arguments)
    if code:
        raise SystemExit(code)
print('BORROWED_WITH_OWN_HISTORY_LEASE')
''', encoding="utf-8")
        ps = r'''
param($root,$python,$probe,$core)
$ErrorActionPreference='Stop'
[IO.Directory]::CreateDirectory((Join-Path $root 'requests')) | Out-Null
$ticks=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
$request=@{request_id='fixture';launcher_pid=$PID;launcher_creation_ticks=$ticks}|ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $root ('requests\shell-fixture-'+$PID+'.json')),$request)
$stream=[IO.File]::Open((Join-Path $root 'lifecycle.lock'),'OpenOrCreate','ReadWrite','ReadWrite')
$stream.Lock(0,1)
try { & $python -B $probe $core $root ([string]$PID) $ticks; $code=$LASTEXITCODE }
finally { $stream.Unlock(0,1);$stream.Dispose() }
exit $code
'''
        script = self.base / "parent.ps1"
        script.write_text(ps, encoding="utf-8")
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell),"-NoLogo","-NoProfile","-NonInteractive","-File",str(script),
                                 str(self.base),sys.executable,str(probe),str(CORE)], capture_output=True,
                                text=True,timeout=30,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BORROWED_WITH_OWN_HISTORY_LEASE", result.stdout)
        reports = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertEqual(len(reports), 2, result.stdout)
        self.assertEqual(reports[0]["new_threads"], 2)
        self.assertEqual(reports[0]["updated_threads"], 1)
        self.assertEqual(reports[1]["installed_files"], 0)
        self.assertEqual(set(self.rows()), {new_id, updated_id, only_id, page_id})
        self.assertEqual(fingerprints(self.source), source_before)
        self.assertEqual(only_path.read_bytes(), only_before)
        self.assertEqual({name: (self.target / name).read_bytes() for name in config_before}, config_before)
        core.validate_lineage([self.target], check_offsets=True)

    @contextlib.contextmanager
    def borrowed_guard_fixture(self):
        requests = self.base / "requests"
        requests.mkdir(exist_ok=True)
        marker = requests / "shell-fixture-123.json"
        request = {"request_id": "fixture", "launcher_pid": 123, "launcher_creation_ticks": "birth"}
        marker.write_bytes(account.json_bytes(request))
        # Keep the real marker reader and app guard: mocking read_object hides
        # checkpoint re-entry, which caused the production launcher failure.
        with mock.patch.object(account, "launcher_is_ancestor", return_value=True), \
                mock.patch.object(account.lifecycle, "LAUNCH_REQUEST_DIR", requests), \
                mock.patch.object(account.lifecycle, "process_identity", return_value={"creation_ticks": "birth"}) as identity, \
                mock.patch.object(account.lifecycle, "app_running", return_value=False) as app_running, \
                mock.patch.object(account.lifecycle, "lifecycle_lock", side_effect=account.lifecycle.LifecycleBusy("fixture lease")):
            guard = account.borrowed_launcher_guard(123, "birth", "fixture")
            yield guard, marker, identity, app_running

    def test_borrowed_guard_checkpoints_do_not_reenter_and_keep_rechecking(self):
        with self.borrowed_guard_fixture() as (guard, marker, identity, app_running):
            before = marker.read_bytes()
            identity.reset_mock()
            app_running.reset_mock()
            with core.history_checkpoint_scope(guard) as work:
                for _ in range(3):
                    core.history_checkpoint(force=True)
                    self.assertEqual(work.suspended, 0)
                self.assertEqual(identity.call_count, 3)
                self.assertEqual(app_running.call_count, 3)
                self.assertGreater(work.last_check, 0)
            self.assertEqual(marker.read_bytes(), before)

    def test_borrowed_guard_checkpoint_failures_restore_checks(self):
        for failure in ("missing-marker", "request-id", "marker-pid", "marker-birth",
                        "process-gone", "process-birth", "invalid-json", "app-running"):
            with self.subTest(failure=failure), self.borrowed_guard_fixture() as fixture:
                guard, marker, identity, app_running = fixture
                original = marker.read_bytes()
                request = json.loads(original)
                expected = account.AccountSyncError
                if failure == "missing-marker":
                    marker.unlink()
                elif failure in ("request-id", "marker-pid", "marker-birth"):
                    field = {"request-id": "request_id", "marker-pid": "launcher_pid",
                             "marker-birth": "launcher_creation_ticks"}[failure]
                    request[field] = "changed"
                    marker.write_bytes(account.json_bytes(request))
                elif failure == "process-gone":
                    identity.return_value = None
                elif failure == "process-birth":
                    identity.return_value = {"creation_ticks": "another-process"}
                elif failure == "invalid-json":
                    marker.write_bytes(b'{"incomplete":')
                    expected = core.SyncError
                else:
                    app_running.return_value = True
                    expected = account.lifecycle.AppNotQuiescent
                with core.history_checkpoint_scope(guard) as work:
                    with self.assertRaises(expected):
                        core.history_checkpoint(force=True)
                    self.assertEqual(work.suspended, 0)
                    # Even after an exception, a later checkpoint must really run.
                    marker.write_bytes(original)
                    identity.return_value = {"creation_ticks": "birth"}
                    app_running.return_value = False
                    identity.reset_mock()
                    app_running.reset_mock()
                    core.history_checkpoint(force=True)
                    self.assertEqual(identity.call_count, 1)
                    self.assertEqual(app_running.call_count, 1)

    def test_borrowed_launcher_expiry_during_staging_leaves_profiles_untouched(self):
        self.thread(self.source, "launcher expires during staging")
        source_before, target_before = fingerprints(self.source), fingerprints(self.target)
        original_snapshot = core.stable_snapshot
        with self.borrowed_guard_fixture() as (guard, marker, _, _):
            def expire_after_snapshot(*args, **kwargs):
                snapshot = original_snapshot(*args, **kwargs)
                marker.unlink()
                return snapshot

            with mock.patch.object(core, "_HISTORY_CHECK_INTERVAL", 0), \
                    mock.patch.object(core, "stable_snapshot", side_effect=expire_after_snapshot), \
                    self.assertRaisesRegex(account.AccountSyncError, "identity/lease expired"):
                account.sync_account(self.source, self.target, self.work, guard=guard)
        self.assertEqual(fingerprints(self.source), source_before)
        self.assertEqual(fingerprints(self.target), target_before)
        self.assertFalse(any(self.work.rglob("pending.json")))

    def test_invalid_jsonl_aborts_before_account_writes(self):
        _, path = self.thread(self.source, "invalid")
        path.write_bytes(path.read_bytes() + b'{"incomplete":')
        before = fingerprints(self.target)
        with self.assertRaises((account.AccountSyncError, core.SyncError)):
            self.run_sync()
        self.assertEqual(fingerprints(self.target), before)

    def test_project_identity_collision_is_fail_closed(self):
        project_id = sid("conflicting project")
        for root, created in ((self.source, 1), (self.target, 2)):
            with database(root / account.STATE) as db:
                db.execute("INSERT INTO projects VALUES (?,?,?,0,?,2)", (project_id,"Project","{}",created))
        self.thread(self.source, "conflicting project chat", project_id=project_id)
        before = fingerprints(self.target)
        with self.assertRaises(core.SyncError):
            self.run_sync()
        self.assertEqual(fingerprints(self.target), before)

    def test_dry_run_leaves_both_profiles_identical(self):
        self.thread(self.source, "preview")
        before = fingerprints(self.target)
        result = self.run_sync(dry_run=True)
        self.assertEqual(result["installed_files"], 0)
        self.assertGreater(result["planned_files"], 0)
        self.assertEqual(fingerprints(self.target), before)

    def test_install_failure_rolls_back_every_account_byte(self):
        self.thread(self.source, "rollback")
        before = fingerprints(self.target)
        original_replace = os.replace
        injected = False
        def fail_once(src, dst):
            nonlocal injected
            if core.plain_storage_path(dst) == self.target / account.STATE and not injected:
                injected = True
                raise OSError("fixture installation failure")
            return original_replace(src, dst)
        with mock.patch.object(account.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "fixture installation failure"):
                self.run_sync()
        self.assertTrue(injected)
        self.assertEqual(fingerprints(self.target), before)
        self.assertFalse(list(self.work.rglob("pending.json")))
        self.assertFalse(list(self.work.rglob("scratch")))
        self.assertFalse(list(self.work.rglob("stage")))
        self.run_sync()
        self.assertEqual(len(self.rows()), 1)

    def test_interrupted_install_retains_journal_and_recovers_on_next_attempt(self):
        self.thread(self.source, "interrupted")
        before = fingerprints(self.target)
        original_replace = os.replace
        def fail(src, dst):
            if core.plain_storage_path(dst) == self.target / account.STATE:
                raise OSError("fixture process failure")
            return original_replace(src, dst)
        with mock.patch.object(account.os, "replace", side_effect=fail):
            with self.assertRaises(OSError):
                self.run_sync()
        pending = list(self.work.rglob("pending.json"))
        self.assertEqual(len(pending), 1)
        self.assertTrue(account.recover_pending(pending[0].parent, self.target, lambda: None))
        self.assertEqual(fingerprints(self.target), before)
        self.run_sync()

    def test_recovery_refuses_to_overwrite_external_edits(self):
        self.thread(self.source, "external edit")
        original_replace = os.replace
        def fail(src, dst):
            if core.plain_storage_path(dst) == self.target / account.STATE:
                raise OSError("fixture process failure")
            return original_replace(src, dst)
        with mock.patch.object(account.os, "replace", side_effect=fail):
            with self.assertRaises(OSError):
                self.run_sync()
        (self.target / account.GLOBAL).write_text('{"new-account-edit":true}')
        before = fingerprints(self.target)
        pending = next(self.work.rglob("pending.json"))
        with self.assertRaises(account.AccountSyncError):
            account.recover_pending(pending.parent, self.target, lambda: None)
        self.assertEqual(fingerprints(self.target), before)

    def test_overlapping_roots_and_linked_history_are_rejected(self):
        with self.assertRaises(account.AccountSyncError):
            account.sync_account(self.source, self.source, self.work, guard=lambda:None)
        _, source = self.thread(self.source, "hardlinked")
        linked = self.target / "sessions" / source.name
        try:
            os.link(source, linked)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaises(account.AccountSyncError):
            self.run_sync()

    def test_source_wal_is_read_without_checkpointing_or_losing_recent_rows(self):
        thread_id, _ = self.thread(self.source, "wal source")
        # A read-only backup must include committed WAL data, not just the
        # sqlite main file; the fixture writer makes no changes during sync.
        with contextlib.closing(sqlite3.connect(self.source / account.STATE)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            with writer:
                writer.execute("UPDATE threads SET name='WAL-only source rename' WHERE id=?", (thread_id,))
            self.assertGreater((self.source / (account.STATE + "-wal")).stat().st_size, 0)
            self.run_sync()
            self.assertEqual(self.rows()[thread_id]["name"], "WAL-only source rename")

    def test_uncheckpointed_account_wal_is_preserved_in_backup_and_retired_safely(self):
        self.thread(self.source, "new after unclean account exit")
        account_id, _ = self.thread(self.target, "account WAL")
        program = '''import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1]);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
db.execute("UPDATE threads SET name='Account WAL name' WHERE id=?",(sys.argv[2],));db.commit();os._exit(0)
'''
        subprocess.run([sys.executable,"-B","-c",program,str(self.target / account.STATE),account_id],
                       check=True,timeout=10,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        wal = self.target / (account.STATE + "-wal")
        self.assertTrue(wal.exists())
        result = self.run_sync()
        self.assertEqual(self.rows()[account_id]["name"], "Account WAL name")
        self.assertTrue((Path(result["backup_dir"]) / "before" / wal.name).exists())
        self.assertFalse(wal.exists())

    def test_account_sidebar_edit_is_not_overwritten_on_following_runs(self):
        thread_id, _ = self.thread(self.source, "sidebar custom")
        (self.source / account.GLOBAL).write_bytes(account.json_bytes({
            "thread-workspace-root-hints": {thread_id: "source-hint"}}))
        self.run_sync()
        state = account.read_object(self.target / account.GLOBAL)
        state["thread-workspace-root-hints"][thread_id] = "account-hint"
        (self.target / account.GLOBAL).write_bytes(account.json_bytes(state))
        for _ in range(3):
            self.run_sync()
            self.assertEqual(account.read_object(self.target / account.GLOBAL)["thread-workspace-root-hints"][thread_id], "account-hint")

    def test_copied_archived_thread_can_be_unarchived_on_source_without_duplicate_heads(self):
        thread_id, _ = self.thread(self.source, "unarchive", archived=1)
        self.run_sync()
        with database(self.source / account.STATE) as db:
            db.execute("UPDATE threads SET archived=0,archived_at=NULL WHERE id=?", (thread_id,))
        self.run_sync()
        self.assertEqual(self.rows()[thread_id]["archived"], 0)
        self.assertIn("sessions", Path(self.rows()[thread_id]["rollout_path"]).parts)
        self.assertEqual(self.run_sync()["installed_files"], 0)

    def test_borrowed_lifecycle_rejects_bad_birth_identity_and_unheld_lock(self):
        request = {"launcher_pid":123,"launcher_creation_ticks":"birth","request_id":"fixture"}
        with mock.patch.object(account, "launcher_is_ancestor", return_value=True), \
                mock.patch.object(account, "read_object", return_value=request), \
                mock.patch.object(account.lifecycle, "process_identity", return_value={"creation_ticks":"other-birth"}):
            with self.assertRaises(account.AccountSyncError):
                account.borrowed_launcher_guard(123,"birth","fixture")
        with mock.patch.object(account, "launcher_is_ancestor", return_value=True), \
                mock.patch.object(account, "read_object", return_value=request), \
                mock.patch.object(account.lifecycle, "process_identity", return_value={"creation_ticks":"birth"}), \
                mock.patch.object(account, "no_app_guard"), \
                mock.patch.object(account.lifecycle,"lifecycle_lock",return_value=contextlib.nullcontext()):
            with self.assertRaisesRegex(account.AccountSyncError,"hold the lifecycle lease"):
                account.borrowed_launcher_guard(123,"birth","fixture")

    def test_active_app_stops_default_entry_before_work_or_profile_mutation(self):
        self.thread(self.source, "live app")
        before = fingerprints(self.base)
        with mock.patch.object(account.lifecycle, "assert_quiescent", side_effect=account.lifecycle.AppNotQuiescent("fixture app running")):
            code = account.main(["--source",str(self.source),"--target",str(self.target),"--work-base",str(self.work)])
        self.assertEqual(code, 2)
        self.assertEqual(fingerprints(self.base), before)

    def test_journal_allowlist_cannot_touch_auth_or_config(self):
        for path in ("auth.json", "config.toml", "../sota/auth.json", "sessions/../../auth.json", "C:/outside.jsonl"):
            with self.subTest(path=path), self.assertRaises(account.AccountSyncError):
                account.history_relative(path)


if __name__ == "__main__":
    unittest.main()
