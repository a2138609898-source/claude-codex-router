"""Pin down clear_archived_sidebar_placement: archived chats must leave every sidebar section.

Regression guard for "archived conversations still appear in the sidebar". An archived thread that
kept thread_section_id / section_position (e.g. the Pinned section) was rendered by the desktop by
section membership regardless of the archive bit. The sync now detaches archived threads from any
section in state_5.sqlite. This also checks the reduced-schema path (older/fixture DBs) does not
crash on a missing column.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_codex_histories as core  # noqa: E402
import repair_archived_sidebar as repair  # noqa: E402


def _make_full_threads(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER, "
        "thread_section_id TEXT, section_position INTEGER, is_pinned INTEGER)"
    )


class ClearArchivedSidebarPlacementTests(unittest.TestCase):
    def test_archived_threads_are_detached_active_untouched(self) -> None:
        con = sqlite3.connect(":memory:")
        _make_full_threads(con)
        con.executemany(
            "INSERT INTO threads VALUES (?,?,?,?,?)",
            [
                ("arch_pinned", 1, "sec-pinned", 8000000, 0),
                ("arch_ispinned", 1, None, None, 1),
                ("arch_clean", 1, None, None, 0),
                ("active_pinned", 0, "sec-pinned", 5, 1),
            ],
        )
        con.commit()

        changed = core.clear_archived_sidebar_placement(con)
        self.assertEqual(changed, 2, "只应改动两个仍带分区/置顶的归档线程")

        rows = {r[0]: r for r in con.execute(
            "SELECT id, thread_section_id, section_position, is_pinned FROM threads")}
        # Every archived thread ends section-free and unpinned.
        for tid in ("arch_pinned", "arch_ispinned", "arch_clean"):
            self.assertEqual(rows[tid][1], None, f"{tid} 仍带 thread_section_id")
            self.assertEqual(rows[tid][2], None, f"{tid} 仍带 section_position")
            self.assertEqual(rows[tid][3], 0, f"{tid} 仍置顶")
        # The active pinned chat is left exactly as it was.
        self.assertEqual(rows["active_pinned"], ("active_pinned", "sec-pinned", 5, 1))
        con.close()

    def test_reduced_schema_without_section_columns_does_not_crash(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER)")
        con.execute("INSERT INTO threads VALUES ('a', 1)")
        con.commit()
        # No thread_section_id/section_position/is_pinned columns: must be a safe no-op.
        self.assertEqual(core.clear_archived_sidebar_placement(con), 0)
        con.close()

    def test_missing_archived_column_is_a_safe_noop(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, thread_section_id TEXT)")
        con.commit()
        self.assertEqual(core.clear_archived_sidebar_placement(con), 0)
        con.close()

    def test_top_level_membership_cache_filters_only_archived_threads(self) -> None:
        state = {
            core.THREAD_PROJECT_MEMBERSHIP_KEY: {
                "archived-thread": "local",
                "active-thread": "local",
            },
            core.ATOM_STATE_KEY: {
                core.THREAD_PROJECT_MEMBERSHIP_KEY: {
                    "archived-thread": "legacy",
                    "active-thread": "legacy",
                },
            },
        }

        self.assertEqual(
            core.sidebar_thread_ids(state),
            {"archived-thread", "active-thread"},
        )

        core.remove_archived_sidebar_entries(state, {"archived-thread"})

        self.assertEqual(
            state[core.THREAD_PROJECT_MEMBERSHIP_KEY],
            {"active-thread": "local"},
        )
        self.assertEqual(
            state[core.ATOM_STATE_KEY][core.THREAD_PROJECT_MEMBERSHIP_KEY],
            {"active-thread": "legacy"},
        )
        self.assertEqual(core.sidebar_thread_ids(state), {"active-thread"})

    def test_archived_local_catalog_rows_are_removed_by_id_only(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE local_thread_catalog ("
            "host_id TEXT, thread_id TEXT, display_title TEXT, source_kind TEXT, "
            "PRIMARY KEY (host_id, thread_id))"
        )
        con.execute(
            "CREATE TABLE local_thread_catalog_metadata ("
            "id INTEGER PRIMARY KEY, catalog_revision INTEGER NOT NULL)"
        )
        con.execute("INSERT INTO local_thread_catalog_metadata VALUES (1, 4)")
        con.executemany(
            "INSERT INTO local_thread_catalog VALUES (?, ?, ?, ?)",
            [
                ("local", "archived", "same title", "vscode"),
                ("local", "active", "same title", "vscode"),
                ("remote", "archived", "same title", "vscode"),
            ],
        )

        removed = repair.clear_archived_local_catalog_entries(con, {"archived"})

        self.assertEqual(removed, 1)
        self.assertEqual(
            con.execute(
                "SELECT host_id, thread_id FROM local_thread_catalog ORDER BY host_id"
            ).fetchall(),
            [("local", "active"), ("remote", "archived")],
        )
        self.assertEqual(
            con.execute(
                "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
            ).fetchone()[0],
            5,
        )
        con.close()

    def test_missing_local_catalog_table_is_a_safe_noop(self) -> None:
        con = sqlite3.connect(":memory:")
        self.assertEqual(
            repair.clear_archived_local_catalog_entries(con, {"archived"}), 0
        )
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
