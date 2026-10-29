"""Account -> SOTA deltas and round trips, using disposable profiles only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import test_chatgpt_history_sync as fixtures

account, core = fixtures.account, fixtures.core


class AccountReturnTests(unittest.TestCase):
    thread = fixtures.AccountHistoryTests.thread
    paginated = fixtures.AccountHistoryTests.paginated

    def setUp(self):
        fixtures.AccountHistoryTests.setUp(self)
        (self.source / "config.toml").write_text(
            'model_provider="tango_relay"\nmodel="vendor--fixture-model"\n', encoding="utf-8")

    def rows(self, root):
        with fixtures.database(root / account.STATE) as db:
            return {row["id"]: row for row in core.rows_as_dicts(db, "threads")}

    def head(self, root, thread_id):
        return Path(self.rows(root)[thread_id]["rollout_path"])

    def append(self, root, thread_id, message):
        path = self.head(root, thread_id)
        contents = path.read_bytes()
        ordinal = max(json.loads(line).get("ordinal", 0) for line in contents.splitlines()) + 1
        path.write_bytes(contents + account.json_bytes({"type": "event_msg", "ordinal": ordinal,
                        "payload": {"type": "user_message", "message": message}}))

    def sync(self, reverse=False, **kwargs):
        source, target = (self.target, self.source) if reverse else (self.source, self.target)
        before = fixtures.fingerprints(source)
        protected = {name: (target / name).read_bytes()
                     for name in ("config.toml", "auth.json", "providers.json")}
        result = account.sync_account(source, target, self.base / ("return-work" if reverse else "work"),
                                      guard=lambda: None, rollback_guard=lambda: None,
                                      return_to_sota=reverse, **kwargs)
        self.assertEqual(fixtures.fingerprints(source), before)
        self.assertEqual({name: (target / name).read_bytes() for name in protected}, protected)
        return result

    def test_new_account_chat_uses_sota_provider_and_configured_model(self):
        thread_id, original = self.thread(self.target, "account created")
        result = self.sync(reverse=True)
        row = self.rows(self.source)[thread_id]
        self.assertEqual(result["new_threads"], 1)
        self.assertEqual(result["mode"], "sota-return-history-one-way")
        self.assertEqual(row["model_provider"], "tango_relay")
        self.assertEqual(row["model"], "vendor--fixture-model")
        self.assertEqual(account.read_header(Path(row["rollout_path"]))["model_provider"], "tango_relay")
        self.assertIn("account-return-sync", str(row["rollout_path"]))
        self.assertTrue(original.exists())
        self.assertEqual(self.sync(reverse=True)["installed_files"], 0)

    def test_unchanged_forward_import_does_not_touch_sota_history(self):
        self.thread(self.source, "already shared")
        self.sync()
        before = fixtures.fingerprints(self.source)
        result = self.sync(reverse=True)
        self.assertEqual(result["unchanged_imports_skipped"], 1)
        self.assertEqual(result["source_threads"], 0)
        after = fixtures.fingerprints(self.source)
        after.pop(account.RETURN_LEDGER, None)
        self.assertEqual(after, before)

    def test_account_continuation_returns_and_keeps_old_sota_backup(self):
        thread_id, original = self.thread(self.source, "continue account")
        original_bytes = original.read_bytes()
        self.sync()
        self.append(self.target, thread_id, "new account answer")
        result = self.sync(reverse=True)
        self.assertEqual(result["updated_threads"], 1)
        self.assertIn(b'new account answer', self.head(self.source, thread_id).read_bytes())
        self.assertEqual((Path(result["backup_dir"]) / "before" / original.relative_to(self.source)).read_bytes(), original_bytes)
        self.assertEqual(self.rows(self.source)[thread_id]["model"], "vendor--fixture-model")

    def test_untouched_account_copy_does_not_overwrite_new_sota_turns(self):
        thread_id, _ = self.thread(self.source, "sota ahead")
        self.sync()
        self.append(self.source, thread_id, "only on SOTA")
        contents = self.head(self.source, thread_id).read_bytes()
        result = self.sync(reverse=True)
        self.assertEqual(result["unchanged_imports_skipped"], 1)
        self.assertEqual(self.head(self.source, thread_id).read_bytes(), contents)
        self.assertEqual(len(self.rows(self.source)), 1)

    def test_alternating_continuations_converge_without_duplicates(self):
        thread_id, _ = self.thread(self.source, "alternate")
        self.sync()
        for number in range(3):
            self.append(self.target, thread_id, f"account-{number}")
            self.sync(reverse=True)
            self.append(self.source, thread_id, f"SOTA-{number}")
            self.sync()
            self.assertEqual(self.sync(reverse=True)["new_threads"], 0)
        for root in (self.source, self.target):
            self.assertEqual(len(self.rows(root)), 1)
            text = self.head(root, thread_id).read_text(encoding="utf-8")
            self.assertIn("account-2", text)
            self.assertIn("SOTA-2", text)

    def test_divergent_branches_round_trip_without_echo_copies(self):
        thread_id, _ = self.thread(self.source, "both continue")
        self.sync()
        self.append(self.source, thread_id, "SOTA branch")
        self.append(self.target, thread_id, "GPT branch")
        original_sota = self.head(self.source, thread_id).read_bytes()
        original_account = self.head(self.target, thread_id).read_bytes()
        result = self.sync(reverse=True)
        self.assertEqual(result["conflicts_preserved"], 1)
        self.assertEqual(self.head(self.source, thread_id).read_bytes(), original_sota)
        clone = next(row for key, row in self.rows(self.source).items() if key != thread_id)
        self.assertTrue(clone["name"].endswith(account.RETURN_BRANCH_SUFFIX))
        for _ in range(3):
            self.sync()
            self.sync(reverse=True)
            self.assertEqual(len(self.rows(self.source)), 2)
            self.assertEqual(len(self.rows(self.target)), 2)
        self.assertEqual(self.head(self.target, thread_id).read_bytes(), original_account)
        self.append(self.target, thread_id, "account branch continues")
        self.sync(reverse=True)
        self.assertIn(b'account branch continues', self.head(self.source, clone["id"]).read_bytes())
        self.sync()
        self.assertEqual(len(self.rows(self.target)), 2)

    def test_account_origin_round_trip_keeps_one_identity(self):
        thread_id, _ = self.thread(self.target, "born in account")
        self.sync(reverse=True)
        self.append(self.source, thread_id, "SOTA follows")
        self.sync()
        self.assertIn(b'SOTA follows', self.head(self.target, thread_id).read_bytes())
        self.assertEqual(self.sync(reverse=True)["new_threads"], 0)
        self.assertEqual(len(self.rows(self.source)), 1)
        self.assertEqual(len(self.rows(self.target)), 1)

    def test_legacy_missing_sota_history_is_not_repaired_by_unchanged_account_copy(self):
        _, missing = self.thread(self.source, "old missing")
        self.thread(self.target, "old missing")
        missing.unlink()
        self.sync()
        new_id, _ = self.thread(self.target, "new independent account")
        result = self.sync(reverse=True)
        self.assertFalse(missing.exists())
        self.assertEqual(result["source_threads"], 1)
        self.assertEqual(result["new_threads"], 1)
        self.assertIn(new_id, self.rows(self.source))

    def test_account_continuation_of_missing_sota_head_is_a_separate_branch(self):
        thread_id, missing = self.thread(self.source, "missing but continued")
        self.thread(self.target, "missing but continued")
        missing.unlink()
        self.sync()
        self.append(self.target, thread_id, "continued on account")
        result = self.sync(reverse=True)
        self.assertEqual(result["conflicts_preserved"], 1)
        self.assertFalse(missing.exists())
        self.assertEqual(self.rows(self.source)[thread_id]["rollout_path"], str(missing))
        self.assertEqual(len(self.rows(self.source)), 2)

    def test_account_rename_unpin_and_archive_return_without_credentials(self):
        thread_id, _ = self.thread(self.source, "sidebar change", is_pinned=1)
        self.sync()
        with fixtures.database(self.target / account.STATE) as db:
            db.execute("UPDATE threads SET name='account rename',is_pinned=0,archived=1 WHERE id=?", (thread_id,))
        self.sync(reverse=True)
        row = self.rows(self.source)[thread_id]
        self.assertEqual(row["name"], "account rename")
        self.assertEqual(row["is_pinned"], 0)
        self.assertEqual(row["archived"], 1)
        self.assertIn("archived_sessions", row["rollout_path"])

    def test_paginated_account_chat_preserves_ancestor_contents(self):
        thread_id, _, ancestor = self.paginated(self.target, "account page", "page ancestor", "new tail")
        self.sync(reverse=True)
        head = self.head(self.source, thread_id)
        inventory = core.lineage_inventory(self.source)
        copied = inventory[0][account.read_header(head)["history_base"]["thread_id"]]
        self.assertEqual(copied.read_bytes(), ancestor.read_bytes())
        core.validate_lineage([self.source], check_offsets=True)

    def test_reverse_preview_leaves_both_profiles_byte_identical(self):
        self.thread(self.target, "preview")
        before = fixtures.fingerprints(self.source)
        result = self.sync(reverse=True, dry_run=True)
        self.assertEqual(result["mode"], "sota-return-history-dry-run")
        self.assertEqual(result["installed_files"], 0)
        self.assertEqual(fixtures.fingerprints(self.source), before)

    def test_unrelated_inbound_ledger_fails_closed(self):
        self.thread(self.target, "foreign ledger")
        (self.target / account.LEDGER).write_bytes(account.json_bytes(
            {"schema": 1, "source": "different-profile", "target": str(self.target), "threads": {}}))
        before = fixtures.fingerprints(self.source)
        with self.assertRaisesRegex(account.AccountSyncError, "different profiles"):
            self.sync(reverse=True)
        self.assertEqual(fixtures.fingerprints(self.source), before)

    def test_target_drift_is_reported_in_preview_and_blocks_real_commit(self):
        self.thread(self.target, "target drift")
        before = fixtures.fingerprints(self.source)
        real_tree_state = account.tree_state
        for dry_run in (True, False):
            calls = 0

            def target_changes(root):
                nonlocal calls
                value = real_tree_state(root)
                if root == self.source:
                    calls += 1
                    if calls > 1:
                        value["fixture-external-change"] = [1, 2]
                return value

            with self.subTest(dry_run=dry_run), mock.patch.object(account, "tree_state", side_effect=target_changes):
                if dry_run:
                    result = self.sync(reverse=True, dry_run=True)
                    self.assertTrue(result["target_changed_during_preview"])
                    self.assertEqual(result["installed_files"], 0)
                else:
                    with self.assertRaisesRegex(account.AccountSyncError, "History changed"):
                        self.sync(reverse=True)
            self.assertEqual(fixtures.fingerprints(self.source), before)

    def test_live_app_blocks_reverse_cli_before_any_writes(self):
        self.thread(self.target, "live account")
        before = fixtures.fingerprints(self.base)
        with mock.patch.object(account.lifecycle, "assert_quiescent", side_effect=account.lifecycle.AppNotQuiescent("fixture")):
            code = account.main(["--to-sota", "--source", str(self.target), "--target", str(self.source),
                                 "--work-base", str(self.work)])
        self.assertEqual(code, 2)
        self.assertEqual(fixtures.fingerprints(self.base), before)

    @unittest.skipUnless(os.name == "nt", "Windows process/lease interoperability")
    def test_real_powershell_parent_runs_reverse_cli_twice(self):
        thread_id, _ = self.thread(self.source, "real parent shared")
        self.sync()
        self.append(self.target, thread_id, "account update")
        new_id, _ = self.thread(self.target, "real parent new")
        before = fixtures.fingerprints(self.target)
        probe = self.base / "probe.py"
        probe.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import sync_chatgpt_account_history as a
base=Path(sys.argv[2])
a.lifecycle.LIFECYCLE_LOCK_PATH=base/'lifecycle.lock'
a.lifecycle.LAUNCH_REQUEST_DIR=base/'requests'
a.lifecycle.app_running=lambda:False
a.core.LOCK_PATH=base/'sync.lock'
args=['--to-sota','--source',str(base/'account'),'--target',str(base/'sota'),
      '--work-base',str(base/'return-work'),'--launcher-pid',sys.argv[3],
      '--launcher-creation-ticks',sys.argv[4],'--launch-request-id','fixture']
for _ in range(2):
    code=a.main(args)
    if code: raise SystemExit(code)
''', encoding="utf-8")
        parent = self.base / "parent.ps1"
        parent.write_text(r'''
param($root,$python,$probe,$core)
$ErrorActionPreference='Stop'
[IO.Directory]::CreateDirectory((Join-Path $root 'requests')) | Out-Null
$ticks=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
$request=@{request_id='fixture';launcher_pid=$PID;launcher_creation_ticks=$ticks}|ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $root ('requests\shell-fixture-'+$PID+'.json')),$request)
$stream=[IO.File]::Open((Join-Path $root 'lifecycle.lock'),'OpenOrCreate','ReadWrite','ReadWrite')
$stream.Lock(0,1)
try { & $python -B $probe $core $root ([string]$PID) $ticks; $code=$LASTEXITCODE }
finally {$stream.Unlock(0,1);$stream.Dispose()}
exit $code
''', encoding="utf-8")
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(parent),
                                 str(self.base), sys.executable, str(probe), str(fixtures.CORE)],
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reports = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertEqual(reports[0]["new_threads"], 1)
        self.assertEqual(reports[0]["updated_threads"], 1)
        self.assertEqual(reports[1]["installed_files"], 0)
        self.assertEqual(set(self.rows(self.source)), {thread_id, new_id})
        self.assertEqual(fixtures.fingerprints(self.target), before)


if __name__ == "__main__":
    unittest.main()
