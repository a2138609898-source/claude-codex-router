#!/usr/bin/env python3
"""Focused, offline checks for history model namespace protection."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


INSTALL_DIR = Path(__file__).resolve().parent
if str(INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALL_DIR))

import sync_codex_histories as core  # noqa: E402
import sync_codex_histories_three_way as three_way  # noqa: E402


THREAD_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    updated_at_ms INTEGER,
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0
)
"""


def write_registry(root: Path, providers: list[dict[str, object]]) -> None:
    (root / "providers.json").write_text(
        json.dumps({"version": 1, "providers": providers}),
        encoding="utf-8",
    )


def make_root(root: Path, provider: str, model: str | None, include_thread: bool = True) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir()
    session_id = "00000000-0000-0000-0000-000000000001"
    rollout = root / "sessions" / "rollout-test.jsonl"
    if include_thread:
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "model_provider": provider},
            },
            {
                "type": "turn_context",
                "payload": {
                    "model": model,
                    "collaboration_mode": {"settings": {"model": model}},
                    "dynamic_tools": [{"model": model}],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "model": model,
                        "model_provider_id": provider,
                    },
                },
            },
        ]
        rollout.write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )

    connection = sqlite3.connect(root / "state_5.sqlite")
    connection.execute(THREAD_SCHEMA)
    if include_thread:
        connection.execute(
            "INSERT INTO threads "
            "(id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, "
            "sandbox_policy, approval_mode, model) VALUES (?, ?, 1, 1, 'cli', ?, '', 'test', '', '', ?)",
            (session_id, str(rollout), provider, model),
        )
    connection.commit()
    connection.close()

    state = {
        "projectless-thread-ids": [session_id] if include_thread else [],
        "thread-project-assignments": {},
        "local-projects": {},
        "pinned-thread-ids": [],
        "electron-persisted-atom-state": {
            "flat-project-sidebar-preferences-v1": {
                "mode": "project",
                "chatSortMode": "updated_at",
                "projectSortMode": "updated_at",
            }
        },
    }
    (root / ".codex-global-state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    (root / "session_index.jsonl").write_text(
        json.dumps({"id": session_id, "thread_name": "test", "updated_at": "2026-01-01T00:00:00Z"})
        + "\n"
        if include_thread
        else "",
        encoding="utf-8",
    )
    return session_id


class HistoryModelGuardTests(unittest.TestCase):
    def test_three_way_uses_automatic_guard_for_each_pair(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            roots = [
                (base / "cockpit").resolve(),
                (base / "plus").resolve(),
                (base / "sota").resolve(),
            ]
            providers = {
                roots[0]: "cockpit_provider",
                roots[1]: "plus_provider",
                roots[2]: "sota_provider",
            }
            sync_calls: list[tuple[Path, Path, object]] = []
            verify_guards: list[object] = []

            def fake_sync(
                left: Path,
                right: Path,
                _backup: Path,
                _left_provider: str,
                _right_provider: str,
                **kwargs: object,
            ) -> dict[str, object]:
                sync_calls.append((left, right, kwargs.get("model_guard_roots")))
                return {
                    "status": "ok",
                    "index_entries": 0,
                    "visible_top_level_threads": 0,
                }

            def fake_verify(
                verify_roots: list[Path],
                _account_ids: set[str],
                _providers: dict[Path, str],
                **kwargs: object,
            ) -> dict[str, object]:
                verify_guards.append(kwargs.get("model_guard_roots"))
                return {
                    **{str(root): {"integrity": "ok"} for root in verify_roots},
                    "same_thread_ids": True,
                }

            with (
                mock.patch.object(three_way, "ensure_sota_initialized", return_value=False),
                mock.patch.object(three_way, "account_ids_for_roots", return_value=set()),
                mock.patch.object(three_way, "rotate_three_way_backups"),
                mock.patch.object(three_way, "prune_outer_snapshots"),
                mock.patch.object(three_way.core, "run_sync", side_effect=fake_sync),
                mock.patch.object(three_way.core, "verify_roots", side_effect=fake_verify),
            ):
                three_way.execute_three_way_mutations(
                    core.utc_now(), roots, providers, base / "run", base / "backups"
                )

            self.assertEqual(sync_calls[0][:2], (roots[0], roots[1]))
            self.assertIsNone(sync_calls[0][2])
            self.assertEqual(sync_calls[1][:2], (roots[0], roots[2]))
            self.assertIsNone(sync_calls[1][2])
            self.assertEqual(sync_calls[2][:2], (roots[1], roots[2]))
            self.assertIsNone(sync_calls[2][2])
            self.assertEqual(verify_guards, [None])

    def test_automatic_guard_discovers_only_codex_responses_roots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            official = base / "official"
            sota = base / "sota"
            messages = base / "messages"
            official.mkdir()
            sota.mkdir()
            messages.mkdir()
            write_registry(
                sota,
                [
                    {
                        "id": "vendor",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    }
                ],
            )
            write_registry(
                messages,
                [
                    {
                        "id": "claude_vendor",
                        "workspace": "claude",
                        "protocols": ["messages"],
                        "enabled": True,
                        "prefix": "claude-vendor.anthropic.",
                        "models": [{"id": "claude-opus-5", "enabled": True}],
                    }
                ],
            )

            contexts = core.build_model_guard_contexts([official, sota, messages])

            self.assertEqual(set(contexts), {sota.resolve()})
            self.assertEqual(contexts[sota.resolve()].registry_error, None)

    def test_malformed_registry_is_guarded_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.mkdir(exist_ok=True)
            (root / "providers.json").write_text("{not-json", encoding="utf-8")

            contexts = core.build_model_guard_contexts([root])

            self.assertEqual(set(contexts), {root.resolve()})
            self.assertEqual(
                contexts[root.resolve()].registry_error,
                "target_registry_unreadable:JSONDecodeError",
            )

    def test_unsupported_registry_version_is_guarded_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_registry(
                root,
                [
                    {
                        "id": "vendor",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    }
                ],
            )
            registry_path = root / "providers.json"
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            payload["version"] = 2
            registry_path.write_text(json.dumps(payload), encoding="utf-8")
            context = core.load_model_guard_context(root)
            self.assertEqual(
                context.registry_error,
                "target_registry_unsupported_version",
            )

    def test_unique_provider_qualifies_db_and_rollout_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "source"
            target = base / "sota"
            make_root(source, "source_vendor", "gpt-5.6-sol")
            make_root(target, "tango_relay", None, include_thread=False)
            write_registry(
                target,
                [
                    {
                        "id": "source_vendor",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "source-vendor--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    }
                ],
            )
            (core.INSTALL_DIR / "work").mkdir(exist_ok=True)
            result = core.run_sync(
                source,
                target,
                base / "backups",
                "source_vendor",
                "tango_relay",
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["unresolved_model_fields"], 0)
            self.assertGreaterEqual(result["normalized_model_fields"], 3)
            connection = sqlite3.connect(target / "state_5.sqlite")
            self.assertEqual(
                connection.execute("SELECT model FROM threads").fetchone()[0],
                "source-vendor--gpt-5.6-sol",
            )
            connection.close()
            text = (target / "sessions" / "rollout-test.jsonl").read_text(encoding="utf-8")
            self.assertIn('"model":"source-vendor--gpt-5.6-sol"', text)
            self.assertIn('"model":"gpt-5.6-sol"', text)  # tool schema is untouched

    def test_ambiguous_model_is_copied_verbatim_instead_of_blocking_the_sync(self) -> None:
        """Ambiguity is a fact about the user's setup, not a reason to refuse to sync.

        Several enabled providers offering the same bare id is the normal shape of a multi-vendor
        router with failover -- one real registry had `gpt-5.6-sol` on nine providers and 7602
        history fields naming it, so aborting here made every launch impossible. What has to hold is
        the narrower thing: the sync must never INVENT attribution. An ambiguous value is copied
        exactly as it was recorded, and the count is reported so it stays visible.
        """
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = base / "source"
            target = base / "sota"
            make_root(source, "source_vendor", "gpt-5.6-sol")
            make_root(target, "tango_relay", None, include_thread=False)
            write_registry(
                target,
                [
                    {
                        "id": "vendor_a",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor-a--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    },
                    {
                        "id": "vendor_b",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor-b--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    },
                ],
            )

            result = core.run_sync(
                source,
                target,
                base / "backups",
                "source_vendor",
                "tango_relay",
            )

            copied = list((target / "sessions").glob("*.jsonl"))
            self.assertEqual(len(copied), 1, "会话应当被同步过去")
            text = copied[0].read_text(encoding="utf-8")
            # The whole point: no provider was guessed onto it.
            self.assertIn('"gpt-5.6-sol"', text)
            self.assertNotIn("vendor-a--", text)
            self.assertNotIn("vendor-b--", text)
            self.assertIn("unresolved", json.dumps(result))

    def test_multiple_candidates_are_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_registry(
                root,
                [
                    {
                        "id": "vendor_a",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor-a--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    },
                    {
                        "id": "vendor_b",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor-b--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    },
                ],
            )
            context = core.load_model_guard_context(root)
            self.assertEqual(
                core.qualify_model_for_target(
                    "gpt-5.6-sol", "vendor_a", context, "fixture"
                ),
                "gpt-5.6-sol",
            )
            self.assertEqual(
                context.unresolved_reasons["model_has_multiple_enabled_providers"],
                1,
            )

    def test_official_root_without_registry_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_root(root, "openai", "gpt-5.6-sol")
            # Official profiles have no SOTA registry and intentionally keep bare first-party IDs.
            result = core.verify_roots([root], set(), {root: "openai"})
            self.assertEqual(result[str(root)]["unqualified_model_values"], 0)

    def test_sota_root_is_guarded_by_default(self) -> None:
        """A bare-but-resolvable value is REPORTED, not fatal.

        It used to raise. That cannot hold for a real archive: the sync only normalizes files it
        writes, so every rollout it had no reason to touch keeps the name it was recorded with --
        one real .codex-sota holds 3250 of them. Demanding zero meant demanding a retroactive
        rewrite of the entire history before any sync could finish. The count is surfaced instead,
        and the router is what stops a bare slug from reaching the wrong account.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_root(root, "vendor", "gpt-5.6-sol")
            write_registry(
                root,
                [
                    {
                        "id": "vendor",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor--",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    }
                ],
            )
            result = core.verify_roots([root], set(), {root: "vendor"})

            report = result[str(root)]
            self.assertGreater(report["unqualified_model_values"], 0)
            self.assertGreater(report["requalifiable_model_sessions"], 0)
            self.assertEqual(report["integrity"], "ok")

    def test_dotted_namespace_is_recognized(self) -> None:
        self.assertTrue(core.model_has_namespace("relay.anthropic.gpt-5.6-sol"))
        item = {
            "type": "turn_context",
            "payload": {"model": "relay.anthropic.gpt-5.6-sol"},
        }
        core.canonicalize_rollout_models(item)
        self.assertEqual(item["payload"]["model"], "gpt-5.6-sol")

    def test_namespace_like_model_id_is_not_treated_as_qualified(self) -> None:
        """A vendor model may contain ``--``; only a registered prefix proves provenance."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_registry(
                root,
                [
                    {
                        "id": "vendor",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "vendor--",
                        "models": [{"id": "foo--bar", "enabled": True}],
                    }
                ],
            )
            context = core.load_model_guard_context(root)
            self.assertFalse(
                core._model_has_namespace_in_context("foo--bar", context)
            )
            self.assertTrue(
                core._model_has_namespace_in_context("vendor--foo--bar", context)
            )
            self.assertEqual(
                core.qualify_model_for_target(
                    "foo--bar", "vendor", context, "fixture"
                ),
                "vendor--foo--bar",
            )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_registry(
                root,
                [
                    {
                        "id": "relay",
                        "workspace": "codex",
                        "protocols": ["responses"],
                        "enabled": True,
                        "prefix": "relay.anthropic.",
                        "models": [{"id": "gpt-5.6-sol", "enabled": True}],
                    }
                ],
            )
            context = core.load_model_guard_context(root)
            self.assertEqual(
                core.qualify_model_for_target(
                    "gpt-5.6-sol", "relay", context, "fixture"
                ),
                "relay.anthropic.gpt-5.6-sol",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
