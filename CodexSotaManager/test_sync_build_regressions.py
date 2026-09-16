from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock


MANAGER_ROOT = Path(__file__).resolve().parent
CORE_ROOT = MANAGER_ROOT.parent / "CodexHistorySync"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

import sync_codex_histories_three_way as three_way  # noqa: E402
import sync_after_codex_exit as post_exit  # noqa: E402
import claude_desktop as claude  # noqa: E402
import codex_sota_router as router  # noqa: E402
import sota_registry as registry  # noqa: E402
import validate_codex_profile as profile_validator  # noqa: E402
from test_codex_sota_regressions import (  # noqa: E402
    LocalUpstream,
    RouterHarness,
    TEST_TOKEN,
    isolated_claude_library,
    provider_config,
    write_auth,
    write_registry,
)


class SyncAndBuildRegressionTests(unittest.TestCase):
    @staticmethod
    def _make_history_root(
        root: Path,
        provider: str,
        thread_ids: tuple[str, ...],
        *,
        orphan_id: str | None = None,
    ) -> None:
        sessions = root / "sessions"
        archived = root / "archived_sessions"
        sessions.mkdir(parents=True)
        archived.mkdir(parents=True)
        connection = sqlite3.connect(root / "state_5.sqlite")
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, archived INTEGER, "
            "model_provider TEXT)"
        )
        for thread_id in thread_ids:
            rollout = sessions / f"rollout-{thread_id}.jsonl"
            rollout.write_text(
                json.dumps(
                    {"payload": {"id": thread_id, "model_provider": provider}},
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                (thread_id, str(rollout), "cli", 0, provider),
            )
        connection.commit()
        connection.close()
        if orphan_id:
            (archived / f"rollout-{orphan_id}.jsonl").write_text(
                json.dumps(
                    {"payload": {"id": orphan_id, "model_provider": provider}},
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
        (root / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "projectless-thread-ids": list(thread_ids),
                    "pinned-thread-ids": [],
                    "local-projects": {},
                    "thread-project-assignments": {},
                    "electron-persisted-atom-state": {
                        "flat-project-sidebar-preferences-v1": {
                            "mode": "project",
                            "chatSortMode": "updated_at",
                            "projectSortMode": "updated_at",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (root / "session_index.jsonl").write_text("", encoding="utf-8")

    @staticmethod
    def _make_rollback_root(root: Path, marker: str) -> None:
        root.mkdir(parents=True)
        connection = sqlite3.connect(root / "state_5.sqlite")
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES (?)", (marker,))
        connection.commit()
        connection.close()
        for directory in ("sessions", "archived_sessions"):
            target = root / directory
            target.mkdir()
            (target / f"{directory}.jsonl").write_text(
                marker + "\n", encoding="utf-8"
            )
        (root / ".codex-global-state.json").write_text(marker, encoding="utf-8")
        (root / "session_index.jsonl").write_text(marker, encoding="utf-8")
        (root / "config.toml").write_text("preserve-config", encoding="utf-8")

    @staticmethod
    def _rollback_fingerprint(root: Path) -> dict[str, object]:
        connection = sqlite3.connect(root / "state_5.sqlite")
        marker = connection.execute("SELECT value FROM marker").fetchone()[0]
        connection.close()
        files: dict[str, bytes] = {}
        for name in (
            ".codex-global-state.json",
            "session_index.jsonl",
            "config.toml",
        ):
            files[name] = (root / name).read_bytes()
        for directory in ("sessions", "archived_sessions"):
            for path in sorted((root / directory).rglob("*")):
                if path.is_file():
                    files[str(path.relative_to(root))] = path.read_bytes()
        return {"marker": marker, "files": files}

    def test_router_routes_json_without_content_type_by_model_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(
            200, {"vendor": "default"}
        ) as default_upstream, LocalUpstream(200, {"vendor": "selected"}) as selected_upstream:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_registry(
                registry_path,
                [
                    provider_config(
                        "default_vendor",
                        default_upstream.url,
                        prefix="",
                        is_default=True,
                    ),
                    provider_config(
                        "selected_vendor",
                        selected_upstream.url,
                        prefix="selected--",
                        is_default=False,
                    ),
                ],
            )
            write_auth(auth_path)
            payload = json.dumps({"model": "selected--shared-model", "input": "test"}).encode()
            with RouterHarness(registry_path, auth_path, root / "router.log") as local_router:
                # urllib assigns application/x-www-form-urlencoded when data has no explicit
                # Content-Type. The router must still parse JSON and honour the model slug.
                request = urllib.request.Request(
                    local_router.url + "/responses", data=payload, method="POST"
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read())

            self.assertEqual(body["vendor"], "selected")
            self.assertEqual(default_upstream.requests, [])
            self.assertEqual(len(selected_upstream.requests), 1)
            forwarded = json.loads(selected_upstream.requests[0]["body"])
            self.assertEqual(forwarded["model"], "shared-model")

    def test_router_rejects_unknown_and_traversal_paths_without_contacting_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(
            200, {"unexpected": True}
        ) as upstream:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_registry(
                registry_path,
                [
                    provider_config(
                        "default_vendor", upstream.url, prefix="", is_default=True
                    )
                ],
            )
            write_auth(auth_path)
            with RouterHarness(registry_path, auth_path, root / "router.log") as local_router:
                for path in ("/admin", "/v1/%2e%2e/admin"):
                    request = urllib.request.Request(
                        local_router.url + path,
                        data=json.dumps({"model": "shared-model"}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(raised.exception.code, 404)
                    error = json.loads(raised.exception.read())
                    self.assertEqual(error["error"]["type"], "unsupported_router_path")

            self.assertEqual(upstream.requests, [])

    def test_router_rejects_invalid_json_before_contacting_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, LocalUpstream(
            200, {"unexpected": True}
        ) as upstream:
            root = Path(temporary)
            registry_path = root / "providers.json"
            auth_path = root / "auth.json"
            write_registry(
                registry_path,
                [
                    provider_config(
                        "default_vendor", upstream.url, prefix="", is_default=True
                    )
                ],
            )
            write_auth(auth_path)
            with RouterHarness(registry_path, auth_path, root / "router.log") as local_router:
                request = urllib.request.Request(
                    local_router.url + "/responses", data=b"not-json", method="POST"
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 400)
                error = json.loads(raised.exception.read())
                self.assertEqual(error["error"]["type"], "invalid_json")

            self.assertEqual(upstream.requests, [])

    def test_router_only_appends_known_inference_suffixes(self) -> None:
        provider = {
            "base_url": "https://example.invalid/v1",
            "responses_path": "/responses",
            "messages_path": "/messages",
        }
        self.assertEqual(
            router.SotaRouterHandler._upstream_url(
                provider, "/v1/responses/compact?mode=test"
            ),
            "https://example.invalid/v1/responses/compact?mode=test",
        )
        with self.assertRaisesRegex(ValueError, "Unsupported router path"):
            router.SotaRouterHandler._upstream_url(provider, "/v1/files")

    def test_secret_file_names_reject_traversal_ads_and_windows_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = registry.Workspace(
                name="security_test",
                label="Security test",
                root=root,
                router_port=1,
                router_starter=root / "start.ps1",
                protocol="responses",
            )
            provider = {"id": "vendor", "workspace": workspace.name}
            with mock.patch.dict(registry.WORKSPACES, {workspace.name: workspace}, clear=False):
                for file_name in ("..", "foo:bar.dpapi", "CON.dpapi", "../key.dpapi"):
                    with self.subTest(file_name=file_name):
                        with self.assertRaisesRegex(ValueError, "invalid secret_file"):
                            registry.secret_path(provider | {"secret_file": file_name})
                safe = registry.secret_path(
                    provider | {"secret_file": "vendor-api-key.dpapi"}
                )
                self.assertEqual(safe, root / "secrets" / "vendor-api-key.dpapi")

    def test_claude_entry_names_reject_ads_traversal_and_windows_devices(self) -> None:
        for entry_id in ("foo:bar", "../entry", "CON", "name/entry"):
            with self.subTest(entry_id=entry_id):
                with self.assertRaisesRegex(ValueError, "Invalid config library entry id"):
                    claude.entry_path(entry_id)
        self.assertEqual(claude.entry_path("theirs").name, "theirs.json")

    def test_claude_meta_with_non_list_entries_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (library, _local, _roaming):
                before = json.dumps({"appliedId": None, "entries": {"bad": True}}).encode()
                claude.META_PATH.write_bytes(before)
                with self.assertRaisesRegex(RuntimeError, "entries .*JSON"):
                    claude.read_meta()
                self.assertEqual(claude.META_PATH.read_bytes(), before)
                self.assertEqual(list(library.glob("*.json")), [claude.META_PATH])

    def test_claude_concurrent_meta_change_is_preserved_and_operation_is_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with isolated_claude_library(root) as (library, _local, _roaming):
                external_id = "79ae43da-be0a-4eb1-bf7c-2aee8fb6db95"
                initial = {
                    "appliedId": external_id,
                    "entries": [{"id": external_id, "name": "CC Switch"}],
                }
                claude.META_PATH.write_text(json.dumps(initial), encoding="utf-8")
                concurrent = {
                    "appliedId": external_id,
                    "entries": [
                        {"id": external_id, "name": "CC Switch"},
                        {"id": "new-external-entry", "name": "Added concurrently"},
                    ],
                }
                concurrent_bytes = json.dumps(concurrent).encode()
                mine = claude.entry_path(claude.SOTA_ENTRY_ID)
                original_atomic = claude._atomic_write_json

                def inject_external_update(path: Path, payload: object) -> None:
                    original_atomic(path, payload)
                    if path == mine:
                        claude.META_PATH.write_bytes(concurrent_bytes)

                with mock.patch.object(
                    claude, "_atomic_write_json", side_effect=inject_external_update
                ):
                    with self.assertRaises(claude.ConcurrentClaudeConfigUpdate):
                        claude.write_profile(
                            [{"name": "vendor--model", "labelOverride": "Vendor model"}],
                            "http://127.0.0.1:17994",
                            api_key=TEST_TOKEN,
                        )

                self.assertEqual(claude.META_PATH.read_bytes(), concurrent_bytes)
                self.assertFalse(mine.exists())
                self.assertFalse((library / f"{claude.SOTA_ENTRY_ID}.json").exists())

    def test_claude_restore_reports_failed_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "new-file.json"
            path.write_text("new", encoding="utf-8")
            with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
                with self.assertRaisesRegex(RuntimeError, "回滚不完整.*locked"):
                    claude._restore({path: None})

    def test_post_exit_process_query_fails_closed_on_nonzero_tasklist(self) -> None:
        completed = SimpleNamespace(returncode=1, stdout="", stderr="tasklist failed")
        with mock.patch.object(post_exit.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "tasklist"):
                post_exit.chatgpt_processes_running()

    def test_post_exit_process_query_fails_closed_on_spawn_error(self) -> None:
        with mock.patch.object(
            post_exit.subprocess, "run", side_effect=OSError("unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "无法查询"):
                post_exit.chatgpt_processes_running()

    def test_post_exit_winapi_wait_declares_pointer_sized_signatures(self) -> None:
        source = (CORE_ROOT / "sync_after_codex_exit.py").read_text(encoding="utf-8")
        self.assertIn("OpenProcess.restype = wintypes.HANDLE", source)
        self.assertIn(
            "WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]",
            source,
        )
        self.assertIn("CloseHandle.argtypes = [wintypes.HANDLE]", source)

    def test_sqlite_snapshot_leaves_no_scratch_file_when_a_connection_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "state_5.sqlite"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE threads (id TEXT)")
            connection.commit()
            connection.close()
            destination = root / "out" / "state_5.sqlite"

            three_way.sqlite_snapshot(source, destination)

            self.assertTrue(destination.is_file())
            self.assertEqual(list(destination.parent.glob(".*bootstrap-*")), [])

            opened: list[sqlite3.Connection] = []
            real_connect = sqlite3.connect

            def flaky_connect(target, *args, **kwargs):
                # The read-only source opens; the scratch target fails, which is the window
                # that used to leak both the handle and the file.
                if isinstance(target, str) and target.startswith("file:"):
                    handle = real_connect(target, *args, **kwargs)
                    opened.append(handle)
                    return handle
                raise sqlite3.OperationalError("unable to open database file")

            with mock.patch.object(three_way.sqlite3, "connect", flaky_connect):
                with self.assertRaises(sqlite3.OperationalError):
                    three_way.sqlite_snapshot(source, destination)

            # The scratch file is created inside the user's Codex home, so a failed snapshot
            # must not leave one behind on every run -- nor an open handle on the live history.
            self.assertEqual(list(destination.parent.glob(".*bootstrap-*")), [])
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].execute("SELECT 1")

    def test_reused_sync_requires_the_exact_provider_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = (root / "cockpit", root / "plus", root / "sota")
            providers = ("cockpit-custom", "openai-custom", "sota-custom")
            resolved = tuple(path.resolve() for path in roots)
            result = {
                "status": "ok",
                "mode": "three-way",
                "cockpit_root": str(resolved[0]),
                "plus_root": str(resolved[1]),
                "sota_root": str(resolved[2]),
                "providers": {
                    str(path): provider for path, provider in zip(resolved, providers)
                },
                "verification": {"three_way_same_thread_ids": True},
            }

            self.assertTrue(
                three_way.reusable_result_matches_request(result, roots, providers)
            )
            self.assertFalse(
                three_way.reusable_result_matches_request(
                    result, roots, (providers[0], providers[1], "wrong-provider")
                )
            )

    def test_audit_reports_the_requested_provider_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = (root / "cockpit", root / "plus", root / "sota")
            providers = ("one", "two", "three")
            result = three_way.audit_roots(*roots, *providers)
            expected = {
                str(path.resolve()): provider
                for path, provider in zip(roots, providers)
            }
            self.assertEqual(result["providers"], expected)

    def test_audit_reports_pending_when_thread_sets_differ(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = (root / "cockpit", root / "plus", root / "sota")
            providers = ("cockpit-provider", "plus-provider", "sota-provider")
            shared = "11111111-1111-4111-8111-111111111111"
            extra = "22222222-2222-4222-8222-222222222222"
            self._make_history_root(roots[0], providers[0], (shared,))
            self._make_history_root(roots[1], providers[1], (shared,))
            self._make_history_root(roots[2], providers[2], (shared, extra))

            result = three_way.audit_roots(*roots, *providers)

            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["pending_reason"], "thread_ids_differ")
            self.assertFalse(result["thread_sets_equal"])
            self.assertEqual(result["thread_union_count"], 2)
            self.assertEqual(
                result["thread_differences"][str(roots[0].resolve())][
                    "missing_from_union"
                ],
                1,
            )

    def test_audit_reports_orphans_without_deleting_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = (root / "cockpit", root / "plus", root / "sota")
            providers = ("cockpit-provider", "plus-provider", "sota-provider")
            shared = "11111111-1111-4111-8111-111111111111"
            orphan = "33333333-3333-4333-8333-333333333333"
            self._make_history_root(
                roots[0], providers[0], (shared,), orphan_id=orphan
            )
            self._make_history_root(roots[1], providers[1], (shared,))
            self._make_history_root(roots[2], providers[2], (shared,))
            orphan_path = roots[0] / "archived_sessions" / f"rollout-{orphan}.jsonl"

            result = three_way.audit_roots(*roots, *providers)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                result["roots"][str(roots[0].resolve())]["orphan_session_files"],
                1,
            )
            self.assertTrue(orphan_path.is_file())

    def test_three_way_failure_restores_all_managed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            roots = (root / "cockpit", root / "plus", root / "sota")
            for index, history_root in enumerate(roots, start=1):
                self._make_rollback_root(history_root, f"original-{index}")
            before = {
                history_root: self._rollback_fingerprint(history_root)
                for history_root in roots
            }
            calls = 0

            def failing_sync(
                left: Path,
                right: Path,
                *_args: object,
                **_kwargs: object,
            ) -> dict[str, object]:
                nonlocal calls
                calls += 1
                for history_root in (left, right):
                    connection = sqlite3.connect(history_root / "state_5.sqlite")
                    connection.execute(
                        "UPDATE marker SET value = ?", (f"mutated-{calls}",)
                    )
                    connection.commit()
                    connection.close()
                    (history_root / ".codex-global-state.json").write_text(
                        f"mutated-{calls}", encoding="utf-8"
                    )
                    (history_root / "session_index.jsonl").write_text(
                        f"mutated-{calls}", encoding="utf-8"
                    )
                    (history_root / "sessions" / "new.jsonl").write_text(
                        f"mutated-{calls}\n", encoding="utf-8"
                    )
                if calls == 2:
                    raise RuntimeError("forced pass-two failure")
                return {
                    "new_files": 1,
                    "updated_files": 1,
                    "unchanged_files": 0,
                    "conflicts_preserved": 0,
                    "exact_duplicates_removed": 0,
                    "duplicate_main_threads_removed": 0,
                    "duplicate_auxiliary_threads_removed": 0,
                    "duplicate_session_files_removed": 0,
                    "warnings": [],
                    "backup_dir": "fake",
                }

            backup_base = root / "backups"
            with mock.patch.object(three_way.core, "run_sync", failing_sync):
                with self.assertRaisesRegex(RuntimeError, "forced pass-two failure"):
                    three_way.run_three_way_sync(
                        roots[0], roots[1], roots[2], backup_base
                    )

            for history_root in roots:
                self.assertEqual(
                    self._rollback_fingerprint(history_root), before[history_root]
                )
            run_backups = [path for path in backup_base.iterdir() if path.is_dir()]
            self.assertEqual(len(run_backups), 1)
            self.assertTrue((run_backups[0] / "outer-snapshot").is_dir())
            self.assertTrue((run_backups[0] / "failed-mutated-state").is_dir())
            rollback = json.loads(
                (run_backups[0] / "rollback-result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rollback["status"], "restored")

    def test_launchers_only_stop_owned_codex_app_processes(self) -> None:
        launcher_paths = (
            CORE_ROOT / "Switch-CodexProfile.ps1",
            CORE_ROOT / "Switch-CodexSota.ps1",
        )
        for launcher_path in launcher_paths:
            with self.subTest(launcher=launcher_path.name):
                source = launcher_path.read_text(encoding="utf-8-sig")
                self.assertIn("function Get-OwnedCodexAppProcesses", source)
                self.assertIn("Get-CimInstance Win32_Process", source)
                self.assertIn("ExecutablePath", source)
                self.assertIn(
                    "[System.StringComparison]::OrdinalIgnoreCase", source
                )
                self.assertIn(
                    "Refusing to stop unowned ChatGPT.exe process IDs", source
                )
                stop_start = source.index("function Stop-CodexApp")
                stop_end = source.find("\nfunction ", stop_start + 1)
                if stop_end < 0:
                    stop_end = len(source)
                stop_source = source[stop_start:stop_end]
                self.assertIn(
                    "$processes = @(Get-OwnedCodexAppProcesses)", stop_source
                )
                self.assertNotIn("Get-Process -Name 'ChatGPT'", stop_source)

    def test_staged_build_swap_preserves_old_and_installs_new(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "Apply-StagedBuild.ps1"
            shutil.copy2(MANAGER_ROOT / script.name, script)
            live = root / "dist" / "codex-sota"
            staged = root / "dist-staging" / "codex-sota"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "old.txt").write_text("old", encoding="utf-8")
            (staged / "new.txt").write_text("new", encoding="utf-8")

            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-File", str(script)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=20,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((live / "new.txt").read_text(encoding="utf-8"), "new")
            retired = list((root / "dist").glob("codex-sota.old-*"))
            self.assertEqual(len(retired), 1)
            self.assertEqual(
                (retired[0] / "old.txt").read_text(encoding="utf-8"), "old"
            )

    def test_failed_final_move_restores_live_and_staged_trees(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "Apply-StagedBuild.ps1"
            script_text = (MANAGER_ROOT / script.name).read_text(encoding="utf-8-sig")
            final_move = "Move-Item -LiteralPath $incoming -Destination $live -ErrorAction Stop"
            self.assertIn(final_move, script_text)
            script.write_text(
                script_text.replace(
                    final_move,
                    "throw 'forced final move failure for isolated regression test'",
                    1,
                ),
                encoding="utf-8",
            )
            live = root / "dist" / "codex-sota"
            staged = root / "dist-staging" / "codex-sota"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "old.txt").write_text("old", encoding="utf-8")
            (staged / "new.txt").write_text("new", encoding="utf-8")

            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-File", str(script)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=20,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual((live / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((staged / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(list((root / "dist").glob("codex-sota.incoming-*")), [])

    def test_log_failure_after_commit_does_not_report_swap_failure(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "Apply-StagedBuild.ps1"
            shutil.copy2(MANAGER_ROOT / script.name, script)
            live = root / "dist" / "codex-sota"
            staged = root / "dist-staging" / "codex-sota"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "old.txt").write_text("old", encoding="utf-8")
            (staged / "new.txt").write_text("new", encoding="utf-8")
            # Add-Content cannot write through a directory with the log file's name.
            (root / "apply-staged-build.log").mkdir()

            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-File", str(script)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=20,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((live / "new.txt").read_text(encoding="utf-8"), "new")
            retired = list((root / "dist").glob("codex-sota.old-*"))
            self.assertEqual(len(retired), 1)
            self.assertEqual((retired[0] / "old.txt").read_text(encoding="utf-8"), "old")

    def test_post_commit_cleanup_preserves_unrelated_staging_files(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "Apply-StagedBuild.ps1"
            shutil.copy2(MANAGER_ROOT / script.name, script)
            live = root / "dist" / "codex-sota"
            staging_root = root / "dist-staging"
            staged = staging_root / "codex-sota"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "old.txt").write_text("old", encoding="utf-8")
            (staged / "new.txt").write_text("new", encoding="utf-8")
            (staging_root / "keep.txt").write_text("keep", encoding="utf-8")

            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-File", str(script)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=20,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((live / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(
                (staging_root / "keep.txt").read_text(encoding="utf-8"), "keep"
            )

    def test_failed_live_retirement_restores_staged_tree(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "Apply-StagedBuild.ps1"
            script_text = (MANAGER_ROOT / script.name).read_text(encoding="utf-8-sig")
            retirement = "Move-Item -LiteralPath $live -Destination $retired -ErrorAction Stop"
            self.assertIn(retirement, script_text)
            script.write_text(
                script_text.replace(
                    retirement,
                    "throw 'forced live retirement failure for isolated regression test'",
                    1,
                ),
                encoding="utf-8",
            )
            live = root / "dist" / "codex-sota"
            staged = root / "dist-staging" / "codex-sota"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "old.txt").write_text("old", encoding="utf-8")
            (staged / "new.txt").write_text("new", encoding="utf-8")

            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-File", str(script)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=20,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual((live / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((staged / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(list((root / "dist").glob("codex-sota.incoming-*")), [])
            self.assertEqual(list((root / "dist").glob("codex-sota.old-*")), [])

    def test_incomplete_rollback_fails_closed_and_preserves_recovery_trees(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "Apply-StagedBuild.ps1"
            script_text = (MANAGER_ROOT / script.name).read_text(encoding="utf-8-sig")
            final_move = "Move-Item -LiteralPath $incoming -Destination $live -ErrorAction Stop"
            restore_live = "Move-Item -LiteralPath $retired -Destination $live -ErrorAction Stop"
            self.assertIn(final_move, script_text)
            self.assertIn(restore_live, script_text)
            script.write_text(
                script_text.replace(
                    final_move,
                    "throw 'forced final move failure for isolated regression test'",
                    1,
                ).replace(
                    restore_live,
                    "throw 'forced live rollback failure for isolated regression test'",
                    1,
                ),
                encoding="utf-8",
            )
            live = root / "dist" / "codex-sota"
            staged = root / "dist-staging" / "codex-sota"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "old.txt").write_text("old", encoding="utf-8")
            (staged / "new.txt").write_text("new", encoding="utf-8")

            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-File", str(script)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=20,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("rollback was incomplete", completed.stderr)
            self.assertFalse(live.exists())
            retired = list((root / "dist").glob("codex-sota.old-*"))
            self.assertEqual(len(retired), 1)
            self.assertEqual((retired[0] / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((staged / "new.txt").read_text(encoding="utf-8"), "new")

    def test_build_and_launcher_sources_are_portable(self) -> None:
        paths = (
            MANAGER_ROOT / "codex-sota.spec",
            MANAGER_ROOT / "Run-ThreeRoundValidation.ps1",
            CORE_ROOT / "Run-CodexHistorySync.ps1",
            CORE_ROOT / "Switch-CodexProfile.ps1",
            CORE_ROOT / "Switch-CodexSota.ps1",
            CORE_ROOT / "Login-CodexSota.ps1",
        )
        for path in paths:
            text = path.read_text(encoding="utf-8-sig")
            self.assertNotIn(str(Path.home()), text, str(path))
            self.assertNotIn(r"D:\CodexCLI", text, str(path))
        spec = (MANAGER_ROOT / "codex-sota.spec").read_text(encoding="utf-8")
        self.assertIn("SPECPATH", spec)

    def test_validation_covers_sync_sources_and_launchers(self) -> None:
        script = (MANAGER_ROOT / "Run-ThreeRoundValidation.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("test_sync_build_regressions.py", script)
        self.assertIn("Get-ChildItem -LiteralPath $coreRoot", script)
        self.assertIn("-Filter '*.py'", script)
        self.assertIn("-Filter '*.ps1'", script)

    def test_final_validation_round_runs_every_regression_class(self) -> None:
        """The clean-repeat round names its classes by hand, so the list silently rots.

        Rounds 1 and 2 collect files by glob and cannot miss anything. Round 3 enumerates
        classes instead -- it runs them with CODEX_SOTA_ARTIFACT_ROOT pointing at the packaged
        build, in a fixed order -- so a class added later just never gets a second pass. Two
        had already dropped out this way, which is invisible: the round still reports OK.
        """
        script = (MANAGER_ROOT / "Run-ThreeRoundValidation.ps1").read_text(encoding="utf-8-sig")
        listed = set(re.findall(r"'(test_[a-z_0-9]+\.[A-Za-z]\w+)'", script))
        for module in ("test_codex_sota_regressions", "test_sync_build_regressions"):
            source = (MANAGER_ROOT / f"{module}.py").read_text(encoding="utf-8")
            for name in re.findall(
                r"^class (\w+)\(unittest\.TestCase\)", source, re.MULTILINE
            ):
                with self.subTest(test_class=f"{module}.{name}"):
                    self.assertIn(f"{module}.{name}", listed)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class StalePinnedModelRepairTests(unittest.TestCase):
    """Deleting a provider (or remapping a model) must never block a launch again.

    The Codex App pins whatever model was selected into config.toml.  Providers and
    mappings change; when the pinned slug falls out of the catalog the launcher repairs
    config.toml instead of refusing to start.
    """

    def _fixture(self, root: Path, catalog_path: Path, model: str) -> None:
        (root / "config.toml").write_text(
            'model_provider = "tango_relay"\n'
            f'model = "{model}"\n'
            'review_model = "provider_a--gpt-5.6-terra"\n'
            'cli_auth_credentials_store = "file"\n'
            'forced_login_method = "api"\n'
            f'model_catalog_json = "{catalog_path.as_posix()}"\n'
            '\n[model_providers.tango_relay]\n'
            'base_url = "http://127.0.0.1:17895"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n',
            encoding="utf-8",
        )
        slugs = [
            "tango-relay--gpt-5.6-sol",
            "tango-relay--gpt-6-astra",
            "provider_a--gpt-5.6-sol",
            "provider_a--gpt-5.6-terra",
        ]
        catalog_path.write_text(
            json.dumps({"models": [{"slug": slug} for slug in slugs]}),
            encoding="utf-8",
        )

    def test_a_deleted_providers_pinned_model_is_repaired_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path = root / "sota-multi-vendor-model-catalog.json"
            self._fixture(root, catalog_path, "golf--gpt-6-astra")

            valid, reason = profile_validator.validate_profile("Sota", root, catalog_path)
            self.assertFalse(valid)
            self.assertEqual(reason, "model_not_in_catalog")

            report = profile_validator.repair_pinned_models(root, catalog_path)
            self.assertEqual(
                report["repaired"]["model"]["to"], "tango-relay--gpt-5.6-sol"
            )
            valid, reason = profile_validator.validate_profile("Sota", root, catalog_path)
            self.assertTrue(valid, reason)

    def test_remapping_keeps_the_same_provider_when_still_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path = root / "sota-multi-vendor-model-catalog.json"
            self._fixture(root, catalog_path, "tango-relay--old-name")

            profile_validator.repair_pinned_models(root, catalog_path)

            config = (root / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "tango-relay--gpt-5.6-sol"', config)
            # The untouched review_model stays exactly as it was.
            self.assertIn('review_model = "provider_a--gpt-5.6-terra"', config)

    def test_a_healthy_config_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path = root / "sota-multi-vendor-model-catalog.json"
            self._fixture(root, catalog_path, "tango-relay--gpt-6-astra")
            before = (root / "config.toml").read_text(encoding="utf-8")

            report = profile_validator.repair_pinned_models(root, catalog_path)

            self.assertEqual(report["repaired"], {})
            self.assertEqual((root / "config.toml").read_text(encoding="utf-8"), before)

