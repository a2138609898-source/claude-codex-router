"""Native compressed history compatibility, always in disposable profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

import test_chatgpt_history_sync as fixtures
import test_chatgpt_return_sync as returning

account, core = fixtures.account, fixtures.core


class CompressedHistoryTests(unittest.TestCase):
    setUp = returning.AccountReturnTests.setUp
    thread = returning.AccountReturnTests.thread
    paginated = returning.AccountReturnTests.paginated
    rows = returning.AccountReturnTests.rows
    head = returning.AccountReturnTests.head
    append = returning.AccountReturnTests.append
    sync = returning.AccountReturnTests.sync

    def compress(self, path, *, keep_plain=False, concatenate=False):
        zstd = account.zstandard_module()
        original_stat = path.stat()
        data = path.read_bytes()
        compressor = zstd.ZstdCompressor(write_checksum=True)
        if concatenate:
            first, rest = data.split(b"\n", 1)
            packed = compressor.compress(first + b"\n") + compressor.compress(rest)
        else:
            packed = compressor.compress(data)
        compressed = path.with_name(path.name + ".zst")
        compressed.write_bytes(packed)
        os.utime(compressed, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        if not keep_plain:
            path.unlink()
        return compressed

    def test_new_compressed_chats_import_in_both_directions(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                source, target = (self.target, self.source) if reverse else (self.source, self.target)
                thread_id, path = self.thread(source, "compressed new " + str(reverse))
                zipped = self.compress(path)
                result = self.sync(reverse=reverse)
                self.assertEqual(result["new_threads"], 1)
                self.assertGreaterEqual(result["compressed_source_pages"], 1)
                self.assertTrue(zipped.exists())
                self.assertFalse(path.exists())
                self.assertIn(b'hello', self.head(target, thread_id).read_bytes())
                self.assertEqual(self.sync(reverse=reverse)["installed_files"], 0)

    def test_compression_only_is_not_an_account_delta(self):
        thread_id, _ = self.thread(self.source, "compress existing copy")
        self.sync()
        self.compress(self.head(self.target, thread_id))
        before = fixtures.fingerprints(self.source)
        result = self.sync(reverse=True)
        self.assertEqual(result["source_threads"], 0)
        after = fixtures.fingerprints(self.source)
        after.pop(account.RETURN_LEDGER, None)
        self.assertEqual(after, before)

    def test_pre_digest_ledger_compression_does_not_echo_into_a_longer_original(self):
        thread_id, _ = self.thread(self.source, "old compressed copy")
        self.sync()
        ledger_path = self.target / account.LEDGER
        ledger = account.read_object(ledger_path)
        for entry in ledger["threads"].values():
            entry.pop("source_content_digest", None)
            entry.pop("target_content_digest", None)
        ledger_path.write_bytes(account.json_bytes(ledger))
        self.compress(self.head(self.target, thread_id))
        self.append(self.source, thread_id, "SOTA is ahead")
        before = self.head(self.source, thread_id).read_bytes()
        self.assertEqual(self.sync(reverse=True)["source_threads"], 0)
        self.assertEqual(self.head(self.source, thread_id).read_bytes(), before)

    def test_native_compression_keeps_legacy_signature_even_if_original_is_unavailable(self):
        thread_id, missing = self.thread(self.source, "legacy unavailable original")
        self.thread(self.target, "legacy unavailable original")
        missing.unlink()
        self.sync()
        ledger_path = self.target / account.LEDGER
        ledger = account.read_object(ledger_path)
        entry = ledger["threads"][thread_id]
        signature = entry["target_signature"]
        entry.pop("source_content_digest", None)
        entry.pop("target_content_digest", None)
        ledger_path.write_bytes(account.json_bytes(ledger))
        path = self.head(self.target, thread_id)
        self.compress(path)
        store = account.TranscriptStore(self.base / "private-view")
        view = account.HistoryView(account.account_inventory(self.target, store), store)
        self.assertEqual(view.signature(path), signature)
        before = fixtures.fingerprints(self.source)
        result = self.sync(reverse=True)
        self.assertEqual(result["source_threads"], 0)
        self.assertFalse(missing.exists())
        after = fixtures.fingerprints(self.source)
        after.pop(account.RETURN_LEDGER, None)
        self.assertEqual(after, before)

    def migrate_account_copy(self, thread_id):
        path = self.head(self.target, thread_id)
        before = path.stat()
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        records[0]["payload"]["subagent_history_start_ordinal"] = 1
        records.append({"type": "turn_context", "ordinal": len(records),
                        "payload": {"current_date": "2026-10-30", "timezone": "UTC"}})
        path.write_bytes(b"".join(account.json_bytes(record) for record in records))
        packed = self.compress(path)
        os.utime(packed, ns=(before.st_atime_ns, before.st_mtime_ns))
        return packed

    def test_native_schema_reencoding_is_not_a_reverse_delta_or_forward_conflict(self):
        thread_id, original = self.thread(self.source, "native schema migration")
        self.sync()
        packed = self.migrate_account_copy(thread_id)
        original_bytes, packed_bytes = original.read_bytes(), packed.read_bytes()
        self.assertEqual(self.sync(reverse=True)["source_threads"], 0)
        self.assertEqual(self.sync()["conflicts_preserved"], 0)
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(packed.read_bytes(), packed_bytes)
        self.assertEqual(len(self.rows(self.target)), 1)

    def test_account_append_after_schema_migration_uses_original_identity(self):
        thread_id, _ = self.thread(self.source, "append after migration")
        self.sync()
        packed = self.migrate_account_copy(thread_id)
        self.sync(reverse=True)
        self.sync()
        logical = packed.with_suffix("")
        store = account.TranscriptStore(self.base / "unpack-fixture")
        store.physical[logical] = packed
        logical.write_bytes(b"".join(store.blocks(logical)))
        self.append(self.target, thread_id, "new real account message")
        result = self.sync(reverse=True)
        self.assertEqual(result["updated_threads"], 1)
        self.assertEqual(result["conflicts_preserved"], 0)
        self.assertEqual(len(self.rows(self.source)), 1)
        self.assertIn(b'new real account message', self.head(self.source, thread_id).read_bytes())

    def test_independent_appends_after_schema_migration_still_preserve_branches(self):
        thread_id, _ = self.thread(self.source, "diverge after migration")
        self.sync()
        packed = self.migrate_account_copy(thread_id)
        self.sync()
        logical = packed.with_suffix("")
        store = account.TranscriptStore(self.base / "unpack-fixture")
        store.physical[logical] = packed
        logical.write_bytes(b"".join(store.blocks(logical)))
        self.append(self.target, thread_id, "account fork")
        self.append(self.source, thread_id, "SOTA fork")
        original = self.head(self.source, thread_id).read_bytes()
        result = self.sync(reverse=True)
        self.assertEqual(result["conflicts_preserved"], 1)
        self.assertEqual(len(self.rows(self.source)), 2)
        self.assertEqual(self.head(self.source, thread_id).read_bytes(), original)

    def test_compressed_continuation_replaces_compressed_target_with_verified_backup(self):
        thread_id, old = self.thread(self.source, "both compressed")
        self.sync()
        self.append(self.target, thread_id, "new account continuation")
        self.compress(self.head(self.target, thread_id))
        old_zipped = self.compress(old)
        original = old_zipped.read_bytes()
        result = self.sync(reverse=True)
        self.assertEqual(result["updated_threads"], 1)
        self.assertIn(b'new account continuation', self.head(self.source, thread_id).read_bytes())
        backup = Path(result["backup_dir"]) / "before" / old_zipped.relative_to(self.source)
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(old_zipped.exists())
        self.assertEqual(self.sync(reverse=True)["installed_files"], 0)

    def test_compressed_paginated_head_and_ancestor_keep_all_records_and_offsets(self):
        thread_id, head, ancestor = self.paginated(self.target, "compressed page", "compressed ancestor", "new tail")
        ancestor_bytes = ancestor.read_bytes()
        self.compress(head)
        self.compress(ancestor)
        result = self.sync(reverse=True)
        self.assertEqual(result["compressed_source_pages"], 2)
        copied_head = self.head(self.source, thread_id)
        inventory = core.lineage_inventory(self.source)
        copied_ancestor = inventory[0][account.read_header(copied_head)["history_base"]["thread_id"]]
        self.assertEqual(copied_ancestor.read_bytes(), ancestor_bytes)
        core.validate_lineage([self.source], check_offsets=True)
        self.assertEqual(self.sync(reverse=True)["installed_files"], 0)

    def test_existing_compressed_ancestor_is_reused_without_profile_decompression(self):
        thread_id, _, ancestor = self.paginated(self.target, "reuse page", "reuse ancestor", "new tail")
        existing = self.source / "archived_sessions" / ancestor.name
        existing.write_bytes(ancestor.read_bytes())
        packed = self.compress(existing)
        original = packed.read_bytes()
        self.compress(ancestor)
        self.sync(reverse=True)
        head = self.head(self.source, thread_id)
        self.assertEqual(account.read_header(head)["history_base"]["thread_id"], core.physical_page_id(ancestor))
        self.assertFalse(existing.exists())
        self.assertEqual(packed.read_bytes(), original)
        self.assertEqual(self.sync(reverse=True)["installed_files"], 0)

    def test_distinct_compressed_ancestor_collision_preserves_both_branches(self):
        thread_id, _, ancestor = self.paginated(self.target, "collision page", "collision ancestor", "new tail")
        existing = self.source / "archived_sessions" / ancestor.name
        existing.write_bytes(ancestor.read_bytes().replace(b'ancestor', b'SOTA ancestor'))
        packed = self.compress(existing)
        original = packed.read_bytes()
        self.compress(ancestor)
        self.sync(reverse=True)
        head = self.head(self.source, thread_id)
        self.assertNotEqual(account.read_header(head)["history_base"]["thread_id"], core.physical_page_id(ancestor))
        self.assertEqual(packed.read_bytes(), original)
        core.validate_lineage([self.source], check_offsets=True)

    def test_uncompressed_current_head_wins_over_an_older_compressed_twin(self):
        thread_id, path = self.thread(self.target, "native append")
        packed = self.compress(path, keep_plain=True)
        original = packed.read_bytes()
        self.append(self.target, thread_id, "native newest message")
        self.sync(reverse=True)
        self.assertIn(b'native newest message', self.head(self.source, thread_id).read_bytes())
        self.assertEqual(packed.read_bytes(), original)

    def test_superseded_compressed_twin_is_backed_up_together_with_plain_head(self):
        thread_id, path = self.thread(self.source, "target twin")
        self.sync()
        packed = self.compress(path, keep_plain=True)
        contents = {item.relative_to(self.source): item.read_bytes() for item in (path, packed)}
        self.append(self.target, thread_id, "account update")
        result = self.sync(reverse=True)
        for relative, original in contents.items():
            self.assertEqual((Path(result["backup_dir"]) / "before" / relative).read_bytes(), original)
            self.assertFalse((self.source / relative).exists())

    def test_archive_and_unarchive_compressed_current_head(self):
        thread_id, path = self.thread(self.source, "compressed archive")
        self.sync()
        self.compress(path)
        for archived in (1, 0):
            with fixtures.database(self.target / account.STATE) as db:
                db.execute("UPDATE threads SET archived=?,name=? WHERE id=?",
                           (archived, "archive name " + str(archived), thread_id))
            result = self.sync(reverse=True)
            self.assertEqual(result["new_threads"], 0)
            expected = "archived_sessions" if archived else "sessions"
            self.assertEqual(self.head(self.source, thread_id).relative_to(self.source).parts[0], expected)
            self.compress(self.head(self.source, thread_id))

    def test_concatenated_zstandard_frames_preserve_complete_transcript(self):
        thread_id, path = self.thread(self.target, "multiple frames", ("first", "second"))
        self.compress(path, concatenate=True)
        self.sync(reverse=True)
        self.assertIn(b'second', self.head(self.source, thread_id).read_bytes())

    def test_truncated_zstandard_frame_is_rejected_even_with_complete_jsonl_output(self):
        _, path = self.thread(self.target, "truncated frame")
        packed = self.compress(path)
        packed.write_bytes(packed.read_bytes()[:-1])
        before = fixtures.fingerprints(self.source)
        with self.assertRaisesRegex(account.AccountSyncError, "Truncated compressed"):
            self.sync(reverse=True)
        self.assertEqual(fixtures.fingerprints(self.source), before)

    def test_compressed_preview_never_changes_either_profile(self):
        _, path, ancestor = self.paginated(self.target, "preview compressed", "preview ancestor", "tail")
        self.compress(path)
        self.compress(ancestor)
        before = fixtures.fingerprints(self.source)
        result = self.sync(reverse=True, dry_run=True)
        self.assertEqual(result["new_threads"], 1)
        self.assertEqual(result["installed_files"], 0)
        self.assertEqual(fixtures.fingerprints(self.source), before)

    def test_compressed_files_participate_in_drift_detection(self):
        _, path = self.thread(self.target, "compressed drift")
        packed = self.compress(path)
        state = account.tree_state(self.target)
        relative = str(packed.relative_to(self.target))
        self.assertIn(relative, state)
        packed.write_bytes(packed.read_bytes() + b"unexpected frame bytes")
        self.assertNotEqual(account.tree_state(self.target), state)


if __name__ == "__main__":
    unittest.main()
