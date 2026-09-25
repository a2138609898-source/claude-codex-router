from __future__ import annotations

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

from codex_app_lifecycle import (
    FileLock, LifecycleBusy, WATCHER_LOCK_PATH, app_running, launch_pending,
    process_identity, _role_for_path,
)


INSTALL_DIR = Path(__file__).resolve().parent
SYNC_SCRIPT = INSTALL_DIR / "sync_codex_histories_three_way.py"
REPAIR_SCRIPT = INSTALL_DIR / "repair_sota_launch_history.py"
ARCHIVE_SIDEBAR_REPAIR_SCRIPT = INSTALL_DIR / "repair_archived_sidebar.py"
REPAIR_PLAN = INSTALL_DIR / "work" / "pending-sota-history-repair.json"
LOG_DIR = INSTALL_DIR / "logs"
LOG_PATH = LOG_DIR / "codex-history-watcher.log"
SYNC_PYTHON = Path(sys.executable).with_name("python.exe")
if not SYNC_PYTHON.is_file():
    SYNC_PYTHON = Path(sys.executable)
SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
CREATE_NO_WINDOW = 0x08000000

# Win32 wait constants/types.  Explicit ctypes signatures are important on 64-bit
# Windows: without them a HANDLE can be truncated and the watcher may wait on an
# unrelated object (or report that the app exited before it did).
WAIT_OBJECT_0 = 0x00000000
WAIT_FAILED = 0xFFFFFFFF
ERROR_INVALID_PARAMETER = 87
DEFAULT_QUIET_SECONDS = 30.0


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=500_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def wait_for_process(process_id: int, expected_executable: str | None = None,
                     expected_creation_ticks: str | None = None) -> bool:
    """Wait for the exact desktop process, or report that its identity is stale.

    A launcher retry can hand the watcher a PID that is still present but no longer
    inspectable (for example while an old Electron instance is shutting down).  That
    PID must not keep the watcher alive or make the next launch wait on it forever.
    """
    if os.name != "nt":  # pragma: no cover - the watcher is a Windows helper
        return True
    try:
        identity = process_identity(process_id)
    except PermissionError as error:
        logging.warning("stale Codex watcher pid=%s: process identity is inaccessible (%s)",
                        process_id, error)
        return False
    except OSError as error:
        winerror = getattr(error, "winerror", None)
        if winerror == 5 or getattr(error, "errno", None) == 5:
            logging.warning("stale Codex watcher pid=%s: process identity is inaccessible (%s)",
                            process_id, error)
            return False
        raise
    if identity is None:
        logging.info("stale Codex watcher pid=%s: process no longer exists", process_id)
        return False
    if not _role_for_path(identity["exe"]):
        logging.info("not waiting on recycled non-Codex pid=%s", process_id)
        return False
    if expected_executable and os.path.normcase(os.path.realpath(identity["exe"])) != os.path.normcase(os.path.realpath(expected_executable)):
        logging.info("not waiting on recycled pid=%s (image changed)", process_id)
        return False
    if expected_creation_ticks and identity["creation_ticks"] != str(expected_creation_ticks):
        logging.info("not waiting on recycled pid=%s (creation time changed)", process_id)
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes

    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(SYNCHRONIZE | 0x1000, False, int(process_id))
    if not handle:
        error = ctypes.get_last_error()
        # ERROR_INVALID_PARAMETER means the process has already gone away.  An
        # access or system error must stop the watcher rather than proceeding as
        # if the app were closed.
        if error == ERROR_INVALID_PARAMETER:
            logging.info("stale Codex watcher pid=%s: process exited before wait", process_id)
            return False
        if error == 5:
            logging.warning("stale Codex watcher pid=%s: OpenProcess denied access", process_id)
            return False
        raise OSError(error, f"OpenProcess({process_id}) failed")
    try:
        # Re-query creation time through THIS handle to close the PID-reuse race
        # between process_identity and OpenProcess. Holding the handle then pins
        # the object even when Windows recycles its numeric PID.
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        actual_ticks = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        if actual_ticks != identity["creation_ticks"]:
            logging.info("pid=%s was recycled before the wait handle opened", process_id)
            return False
        result = kernel32.WaitForSingleObject(handle, INFINITE)
        if result == WAIT_FAILED:
            error = ctypes.get_last_error()
            raise OSError(error, f"WaitForSingleObject({process_id}) failed")
        if result != WAIT_OBJECT_0:
            raise RuntimeError(
                f"WaitForSingleObject({process_id}) returned unexpected result {result}"
            )
    finally:
        if not kernel32.CloseHandle(handle):
            error = ctypes.get_last_error()
            raise OSError(error, f"CloseHandle({process_id}) failed")
    return True


def chatgpt_processes_running() -> bool:
    # Keep the old function name for callers; ownership is now based on paths,
    # not the shared ChatGPT.exe basename. App-server children also count.
    return app_running()


def wait_until_app_is_fully_closed(quiet_seconds: float = DEFAULT_QUIET_SECONDS) -> None:
    quiet_since = None
    while True:
        if chatgpt_processes_running() or launch_pending():
            quiet_since = None
        else:
            now = time.monotonic()
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= quiet_seconds:
                return
        time.sleep(1.0)


def run_sync_with_retry(attempts: int = 1) -> dict[str, object]:
    last_error = ""
    for attempt in range(attempts):
        if chatgpt_processes_running() or launch_pending():
            return {"status": "deferred", "reason": "app_running_or_launch_pending"}
        completed = subprocess.run(
            [str(SYNC_PYTHON), str(SYNC_SCRIPT), "--json", "--defer-if-app-running",
             "--lock-wait-seconds", "0"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        raw_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if completed.returncode == 0 and raw_lines:
            try:
                result = json.loads(raw_lines[-1])
            except json.JSONDecodeError:
                result = None
            if isinstance(result, dict) and result.get("status") in ("ok", "deferred"):
                return result
        last_error = (completed.stderr or completed.stdout or "unknown error").strip()
        if (
            "已经在运行" not in last_error
            and "already running" not in last_error.lower()
        ):
            break
        # Compatibility with an older core during an atomic deployment. Busy is
        # not failed; never queue another 15-minute lock wait behind a winner.
        return {"status": "deferred", "reason": "sync_busy"}
    raise RuntimeError(last_error or "post-exit sync failed")


def run_pending_repair() -> dict[str, object]:
    """Execute an already-reviewed exact plan once, never rescan on startup.

    The repair tool owns lifecycle + sync locks itself, validates all target
    fingerprints and records a receipt before retiring its queue file.
    """
    if not REPAIR_PLAN.is_file():
        return {"status": "not-needed"}
    if not REPAIR_SCRIPT.is_file():
        raise RuntimeError(f"Queued history repair tool is missing: {REPAIR_SCRIPT}")
    if chatgpt_processes_running() or launch_pending():
        return {"status": "deferred", "reason": "app_running_or_launch_pending"}
    completed = subprocess.run(
        [str(SYNC_PYTHON), str(REPAIR_SCRIPT), "--execute-plan", str(REPAIR_PLAN),
         "--apply", "--defer-if-app-running", "--refresh-on-drift"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW, check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        result = None
    if isinstance(result, dict):
        if completed.returncode == 0 and result.get("status") in ("applied", "already-applied"):
            logging.info("queued history repair completed: plan_id=%s status=%s", result.get("plan_id"), result["status"])
            return result
        if completed.returncode in (0, 75) and result.get("status") == "deferred":
            return result
    detail = completed.stderr or completed.stdout or "no structured result"
    # On drift/failure preserve the exact queue and evidence. Do not mutate the
    # histories again via a normal sync before a fresh review of the plan.
    raise RuntimeError(f"Queued history repair did not complete safely (exit {completed.returncode}); normal sync was not started: {detail.strip()}")


def run_archived_sidebar_repair() -> dict[str, object]:
    """Remove archived IDs from sidebar projections before any heavyweight sync.

    This is intentionally independent of the conflict-repair queue.  A stale
    queue must not leave a chat visible after the user archived it, and this
    operation only rewrites two small JSON projections after the App is fully
    closed.
    """
    if not ARCHIVE_SIDEBAR_REPAIR_SCRIPT.is_file():
        raise RuntimeError(f"Archived sidebar repair tool is missing: {ARCHIVE_SIDEBAR_REPAIR_SCRIPT}")
    completed = subprocess.run(
        [str(SYNC_PYTHON), str(ARCHIVE_SIDEBAR_REPAIR_SCRIPT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW, check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no structured result").strip()
        raise RuntimeError(f"Archived sidebar repair failed (exit {completed.returncode}): {detail}")
    return {"status": "ok", "detail": completed.stdout.strip()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wait for ChatGPT App to exit, then sync all three Codex histories."
    )
    parser.add_argument("--pid", type=int)
    parser.add_argument("--app-executable")
    parser.add_argument("--creation-ticks")
    parser.add_argument("--quiet-seconds", type=float, default=DEFAULT_QUIET_SECONDS)
    parser.add_argument("--audit-only", action="store_true")
    return parser


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    if args.audit_only:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "sync_script_exists": SYNC_SCRIPT.is_file(),
                    "python_executable": str(SYNC_PYTHON),
                    "chatgpt_running": chatgpt_processes_running(),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 0
    if args.pid is None:
        raise SystemExit("--pid is required unless --audit-only is used")
    try:
        with FileLock(WATCHER_LOCK_PATH):
            logging.info("watching Codex App pid=%s (singleton)", args.pid)
            if not wait_for_process(args.pid, args.app_executable, args.creation_ticks):
                logging.info("stale/unverifiable Codex watcher pid=%s; exiting without app wait",
                             args.pid)
                return 0
            while True:
                wait_until_app_is_fully_closed(max(1.0, args.quiet_seconds))
                archive_repair = run_archived_sidebar_repair()
                if archive_repair.get("detail"):
                    logging.info("archived sidebar repair complete: %s", archive_repair["detail"])
                repair = run_pending_repair()
                if repair.get("status") == "deferred":
                    logging.info("queued history repair deferred: %s", repair.get("reason"))
                    time.sleep(2.0)
                    continue
                result = run_sync_with_retry()
                if result.get("status") == "deferred":
                    logging.info("post-exit sync deferred: %s", result.get("reason"))
                    # Keep the singleton alive to observe the NEXT clean exit.
                    # A relaunch must not spawn duplicate queued synchronizers.
                    time.sleep(2.0)
                    continue
                logging.info(
                    "post-exit sync complete: main=%s total=%s conflicts=%s exact_duplicates_removed=%s",
                    result.get("visible_top_level_threads"), result.get("index_entries"),
                    result.get("conflicts_preserved"), result.get("exact_duplicates_removed"),
                )
                return 0
    except LifecycleBusy:
        logging.info("another history watcher already owns this installation; no duplicate created")
        return 0
    except Exception:
        logging.exception("post-exit sync failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
