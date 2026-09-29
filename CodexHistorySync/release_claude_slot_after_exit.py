"""Wait for Claude Desktop to exit, then hand the shared 3P config slot back.

`appliedId` in Claude's config library is a single global slot that cc-switch, Foreign Vendor and
codex-sota all write.  codex-sota takes it only for the duration of a Claude it launched itself
(claude_desktop.claim_slot) and this watcher gives it back afterwards, so launching Claude from
cc-switch keeps using cc-switch's own profile.

The wait is handle-based rather than a polling loop: Claude is an Electron app with roughly a
dozen processes, so this opens every current claude.exe and blocks in WaitForMultipleObjects
until all of them are gone.  A user session can last days, and a watcher that woke up every few
seconds to shell out to tasklist for that long would be a poor tenant.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

INSTALL_DIR = Path(__file__).resolve().parent
if str(INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALL_DIR))

import claude_desktop  # noqa: E402

LOG_DIR = INSTALL_DIR / "logs"
LOG_PATH = LOG_DIR / "claude-slot-watcher.log"
CLAUDE_IMAGE = "claude.exe"
SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
CREATE_NO_WINDOW = 0x08000000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
MAXIMUM_WAIT_OBJECTS = 64
# Claude restarts itself during an update, and close_running_claude leaves dying processes
# behind for a moment.  Requiring the image to stay absent for this long keeps the slot from
# being handed back in the gap between two halves of one restart.
SETTLE_SECONDS = 6.0


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=250_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def claude_pids() -> set[int]:
    """PIDs of every running Claude Desktop process.

    Failing closed matters: if the query breaks, reporting "no Claude" would release the slot
    under a live Claude, which is the exact behaviour this watcher exists to prevent.
    """
    tasklist = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tasklist.exe"
    completed = subprocess.run(
        [str(tasklist), "/FI", f"IMAGENAME eq {CLAUDE_IMAGE}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise RuntimeError(f"无法查询 Claude 进程状态（退出码 {completed.returncode}）：{detail}")
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        fields = [field.strip('"') for field in line.strip().split('","')]
        if len(fields) < 2 or CLAUDE_IMAGE not in fields[0].lower():
            continue
        try:
            pids.add(int(fields[1]))
        except ValueError:
            continue
    return pids


def _kernel32() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def wait_for_all_to_exit(pids: list[int], timeout_ms: int = INFINITE) -> None:
    """Block until every given process has exited, without polling.

    Explicit ctypes signatures are deliberate: on 64-bit Windows an implicitly-typed HANDLE can
    be truncated to 32 bits, and the wait would then be on an unrelated object.
    """
    if os.name != "nt":  # pragma: no cover - Windows-only helper
        return
    kernel32 = _kernel32()
    handles: list[int] = []
    try:
        for pid in pids[:MAXIMUM_WAIT_OBJECTS]:
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if handle:
                handles.append(handle)
            # A failed open means the process is already gone or is not ours to wait on;
            # either way the settle check below is the authority, not this handle.
        if not handles:
            return
        array = (wintypes.HANDLE * len(handles))(*handles)
        result = kernel32.WaitForMultipleObjects(len(handles), array, True, timeout_ms)
        if result == WAIT_FAILED:
            raise OSError(ctypes.get_last_error(), "WaitForMultipleObjects failed")
    finally:
        for handle in handles:
            kernel32.CloseHandle(handle)


def claude_stayed_gone(settle_seconds: float = SETTLE_SECONDS) -> bool:
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        if claude_pids():
            return False
        time.sleep(0.5)
    return True


def wait_until_claude_exits(max_seconds: float | None = None) -> bool:
    """Return True once Claude Desktop is gone for good, False if max_seconds ran out."""
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    while True:
        pids = sorted(claude_pids())
        if pids:
            remaining = INFINITE
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                remaining = int(left * 1000)
            wait_for_all_to_exit(pids, remaining)
            continue
        if claude_stayed_gone():
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Give Claude Desktop's shared 3P config slot back to its previous owner "
        "once the Claude that codex-sota launched has exited."
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="Give up waiting after this long instead of blocking indefinitely.",
    )
    parser.add_argument("--audit-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.audit_only:
        # Read-only: proves the watcher can see its inputs without touching the slot.
        print(
            json.dumps(
                {
                    "status": "ok",
                    "claim_path": str(claude_desktop._slot_claim_path()),
                    "claim_present": bool(claude_desktop.read_slot_claim()),
                    "claude_pids": sorted(claude_pids()),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 0
    setup_logging()
    try:
        claim = claude_desktop.read_slot_claim()
        if not claim:
            logging.info("no slot claim recorded; nothing to give back")
            return 0
        logging.info(
            "waiting for Claude Desktop to exit; slot will go back to %s",
            claim.get("previous_applied_name") or claim.get("previous_applied_id") or "<none>",
        )
        if not wait_until_claude_exits(args.max_seconds):
            logging.info("gave up waiting after %s seconds; slot left claimed", args.max_seconds)
            return 0
        result = claude_desktop.release_slot()
        logging.info("release result: %s", json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception:
        logging.exception("releasing the Claude config slot failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
