"""Focused regressions from the three-round source review; no live data or providers."""
import json
from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import CodexSotaManager  # Sets the shared core import path.
import validate_codex_profile as profile
import sync_codex_histories_three_way as sync
import codex_sota_router as router


class ConfigurationReviewTests(unittest.TestCase):
    def test_relative_catalog_is_resolved_from_config_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalogs" / "models.json"
            catalog.parent.mkdir()
            catalog.write_text(json.dumps({"models": [{"slug": "vendor--model"}]}))
            config = (
                'model_provider = "true_sota"\nmodel = "vendor--model"\n'
                'cli_auth_credentials_store = "file"\nforced_login_method = "api"\n'
                'model_catalog_json = "catalogs/models.json"\n'
                '[model_providers.true_sota]\n'
                'base_url = "http://127.0.0.1:17895"\n'
                'wire_api = "responses"\nrequires_openai_auth = true\n'
            )
            (root / "config.toml").write_text(config)
            self.assertEqual(profile.validate_profile("Sota", root, catalog), (True, "ok"))
            (root / "config.toml").write_text(config.replace("catalogs/models.json", "models.json"))
            self.assertEqual(profile.validate_profile("Sota", root, catalog), (False, "model_catalog_mismatch"))


class StreamBoundaryReviewTests(unittest.TestCase):
    def relay(self, data, *, final=True):
        stream = BytesIO(data)
        stream.headers = {"Content-Type": "text/event-stream"}
        handler = object.__new__(router.SotaRouterHandler)
        handler.command, handler.path = "POST", "/responses"
        handler.wfile = BytesIO()
        handler.server = SimpleNamespace(router_state=mock.Mock())
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        retry = handler._relay(stream, "vendor", 200, time.monotonic(), "model",
                               request_payload={"stream": True}, is_final_attempt=final)
        return handler.wfile.getvalue(), retry

    def test_oversized_sse_line_is_rejected_before_forwarding(self):
        source = b"data: " + b"x" * 256 + b"\n\n"
        with mock.patch.object(router, "MAX_REQUEST_BODY_BYTES", 128):
            output, retry = self.relay(source, final=False)
        self.assertFalse(retry)
        self.assertNotIn(b"x" * 256, output)
        self.assertIn(b"event: response.failed", output)

    def test_adapters_bound_both_lines_and_multiline_events(self):
        sources = (b"data: " + b"x" * 256 + b"\n\n", (b"data: " + b"x" * 24 + b"\n") * 8 + b"\n")
        for adapter in (router.chat_sse_to_responses, router.chat_sse_to_anthropic_sse,
                        router.anthropic_sse_to_responses):
            for source in sources:
                with self.subTest(adapter=adapter.__name__, multiline=source.count(b"\n") > 2):
                    with mock.patch.object(router, "MAX_REQUEST_BODY_BYTES", 128):
                        with self.assertRaises(router.UpstreamReadError):
                            list(adapter(BytesIO(source), "model"))

    def test_telemetry_tail_is_bounded_without_changing_output(self):
        source = b'data: {"type":"response.completed","text":"' + b"x" * 512 + b'"}\n\n'
        with mock.patch.object(router, "USAGE_TAIL_BYTES", 64):
            with mock.patch.object(router, "extract_token_usage", return_value={}) as usage:
                output, _ = self.relay(source)
        self.assertEqual(output, source)
        self.assertEqual(usage.call_args.args[1], source[-64:])

    def test_unterminated_state_event_is_not_replayed(self):
        source = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"visible"}\n'
        output, retry = self.relay(source, final=False)
        self.assertFalse(retry)
        self.assertIn(b"event: response.failed", output)


class RollbackSafetyReviewTests(unittest.TestCase):
    def fixture(self, base):
        root = base / "live"
        (root / "sessions").mkdir(parents=True)
        (root / "sessions" / "first.jsonl").write_text("original first")
        (root / "sessions" / "second.jsonl").write_text("original second")
        (root / ".codex-global-state.json").write_text('{"original":true}')
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE marker(value TEXT)")
            db.execute("INSERT INTO marker VALUES ('original')")
            db.commit()
        snapshot = sync.create_outer_snapshot([root], base / "backups" / "1")
        (root / "sessions" / "first.jsonl").write_text("current first")
        (root / "sessions" / "second.jsonl").write_text("current second")
        (root / ".codex-global-state.json").write_text('{"current":true}')
        return root, snapshot

    def test_partial_directory_backup_cannot_replace_intact_current_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, snapshot = self.fixture(Path(temporary))
            original_copytree = sync.shutil.copytree

            def partial_copy(source, destination, *args, **kwargs):
                if Path(source) == root / "sessions":
                    Path(destination).mkdir(parents=True, exist_ok=True)
                    (Path(destination) / "first.jsonl").write_text("partial")
                    raise OSError("injected disk-full during recovery backup")
                return original_copytree(source, destination, *args, **kwargs)

            with mock.patch.object(sync.shutil, "copytree", side_effect=partial_copy):
                result = sync.restore_outer_snapshot(snapshot)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual((root / "sessions" / "first.jsonl").read_text(), "current first")
            self.assertEqual((root / "sessions" / "second.jsonl").read_text(), "current second")

    def test_partial_file_backup_cannot_replace_intact_current_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, snapshot = self.fixture(Path(temporary))
            original_copy = sync.shutil.copy2

            def partial_copy(source, destination, *args, **kwargs):
                if Path(source) == root / ".codex-global-state.json":
                    Path(destination).write_text("partial")
                    raise OSError("injected partial metadata backup")
                return original_copy(source, destination, *args, **kwargs)

            with mock.patch.object(sync.shutil, "copy2", side_effect=partial_copy):
                result = sync.restore_outer_snapshot(snapshot)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual((root / ".codex-global-state.json").read_text(), '{"current":true}')

    def test_database_rollback_preserves_committed_wal_in_recovery_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, snapshot = self.fixture(Path(temporary))
            db = sqlite3.connect(root / "state_5.sqlite")
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("UPDATE marker SET value='committed in WAL'")
                db.commit()
                result = sync.restore_outer_snapshot(snapshot)
                recovery = Path(result["failed_state_root"]) / "cockpit" / "state_5.sqlite"
                with closing(sqlite3.connect(recovery)) as saved:
                    self.assertEqual(saved.execute("SELECT value FROM marker").fetchone()[0], "committed in WAL")
                self.assertEqual(result["status"], "restored", result["errors"])
                self.assertEqual(db.execute("SELECT value FROM marker").fetchone()[0], "original")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
