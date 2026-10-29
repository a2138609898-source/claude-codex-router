"""Focused regression for Windows package filesystem redirection."""
import unittest
from pathlib import Path
from unittest import mock
import claude_desktop as desktop


class RedirectedClaudeInstallTests(unittest.TestCase):
    local = r"C:\Fixture\Local"
    physical = r"D:\WpSystem\FixtureUser\AppData\Local\Packages\OpenAI.Codex_fixture\LocalCache\Local\AnthropicClaude"

    def test_physical_desktop_path_matches_resolved_known_installation(self):
        with mock.patch.object(desktop.Path, "resolve", return_value=Path(self.physical)):
            self.assertTrue(desktop.is_claude_desktop_executable(
                self.physical + r"\app-2.3\Claude.exe", local_appdata=self.local))

    def test_other_package_account_and_cli_paths_remain_excluded(self):
        with mock.patch.object(desktop.Path, "resolve", return_value=Path(self.physical)):
            for path in (self.physical.replace("FixtureUser", "OtherUser") + r"\Claude.exe",
                         self.physical.replace("OpenAI.Codex_fixture", "OtherPackage") + r"\Claude.exe",
                         self.physical.replace("AnthropicClaude", "AnthropicClaudeCode") + r"\Claude.exe"):
                with self.subTest(path=path):
                    self.assertFalse(desktop.is_claude_desktop_executable(path, local_appdata=self.local))

    def test_native_pid_scan_keeps_redirected_desktop_and_excludes_cli(self):
        paths = {101: self.physical + r"\app-2.3\Claude.exe",
                 102: r"C:\Fixture\ClaudeCode\claude.exe"}
        with mock.patch.object(desktop.Path, "resolve", return_value=Path(self.physical)):
            self.assertEqual(desktop.claude_desktop_pids(candidate_pids=paths, image_path=paths.get), {101})


if __name__ == "__main__":
    unittest.main()
