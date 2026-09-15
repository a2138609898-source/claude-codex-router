"""Pin down extended_path and the deep-backup copy it exists for."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sync_codex_histories as core  # noqa: E402


class ExtendedPathTests(unittest.TestCase):
    def test_plain_path_gets_the_prefix(self) -> None:
        self.assertEqual(core.extended_path(Path("C:\\a\\b")), "\\\\?\\C:\\a\\b")

    def test_an_already_extended_path_is_left_alone(self) -> None:
        """abspath mangles it into \\\\?\\C:\\?\\C:\\... , so the check must come first."""
        original = "\\\\?\\C:\\a\\b"
        self.assertEqual(core.extended_path(original), original)

    def test_unc_uses_the_unc_form(self) -> None:
        self.assertEqual(
            core.extended_path("\\\\server\\share\\x"), "\\\\?\\UNC\\server\\share\\x"
        )

    def test_relative_is_resolved_before_prefixing(self) -> None:
        result = core.extended_path("b")
        self.assertTrue(result.startswith("\\\\?\\"))
        self.assertNotIn("\\.\\", result)

    def test_backup_conflict_file_survives_past_max_path(self) -> None:
        """The real failure: the conflict backup lands 254-262 characters deep and copy2 died.

        Built to exceed 260 on purpose so it fails without the fix and passes with it.
        """
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "root"
            relative = Path("sessions") / "2026" / "09" / "09" / (
                "rollout-2026-09-09T11-33-25-01a080b0-1e83-7e62-9709-"
                "bfe7d373b237_01a0843a-4119-7b82-b4c5-1a68bcc588a2.jsonl"
            )
            source = root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('{"id":"x"}\n', encoding="utf-8")

            # Pad the backup dir so the destination clears 260 the way the real tree does.
            backup_dir = base / ("pad" + "x" * 60) / ("run" + "y" * 60)
            session_file = core.SessionFile(
                session_id="01a080b0-1e83-7e62-9709-bfe7d373b237",
                path=source,
                relative_path=relative,
                size=source.stat().st_size,
                mtime_ns=source.stat().st_mtime_ns,
            )
            destination = backup_dir / "conflicts" / "plus" / relative
            self.assertGreater(
                len(str(destination)), 260, "fixture must exceed MAX_PATH to be meaningful"
            )

            core.backup_conflict_file(backup_dir, root, "plus", session_file)

            self.assertTrue(os.path.exists(core.extended_path(destination)))
            self.assertEqual(
                Path(core.extended_path(destination)).read_text(encoding="utf-8"),
                '{"id":"x"}\n',
            )
            shutil.rmtree(core.extended_path(base), ignore_errors=True)

    def test_a_genuinely_missing_source_names_both_paths(self) -> None:
        """The old bare WinError named neither file and read like a missing destination."""
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            missing = base / "root" / "sessions" / "gone.jsonl"
            session_file = core.SessionFile(
                session_id="s", path=missing, relative_path=Path("sessions/gone.jsonl"),
                size=0, mtime_ns=0,
            )
            with self.assertRaises(core.SyncError) as caught:
                core.backup_conflict_file(base / "bk", base / "root", "plus", session_file)
            message = str(caught.exception)
            self.assertIn("gone.jsonl", message)
            self.assertIn("存在=False", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
