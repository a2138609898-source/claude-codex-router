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


INSTALL_DIR = Path(__file__).resolve().parent
SYNC_SCRIPT = INSTALL_DIR / "sync_codex_histories_three_way.py"
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


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=500_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def wait_for_process(process_id: int) -> None:
    if os.name != "nt":  # pragma: no cover - the watcher is a Windows helper
        return
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

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(process_id))
    if not handle:
        error = ctypes.get_last_error()
        # ERROR_INVALID_PARAMETER means the process has already gone away.  An
        # access or system error must stop the watcher rather than proceeding as
        # if the app were closed.
        if error == ERROR_INVALID_PARAMETER:
            return
        raise OSError(error, f"OpenProcess({process_id}) failed")
    try:
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


def chatgpt_processes_running() -> bool:
    try:
        completed = subprocess.run(
            [
                os.path.join(
                    os.environ.get("SystemRoot", r"C:\Windows"),
                    "System32",
                    "tasklist.exe",
                ),
                "/FI",
                "IMAGENAME eq ChatGPT.exe",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法查询 ChatGPT 进程状态：{error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise RuntimeError(
            f"tasklist 查询 ChatGPT 进程失败（退出码 {completed.returncode}）：{detail}"
        )
    return '"ChatGPT.exe"' in completed.stdout


def wait_until_app_is_fully_closed() -> None:
    while chatgpt_processes_running():
        time.sleep(0.25)


def run_sync_with_retry(attempts: int = 240) -> dict[str, object]:
    last_error = ""
    for attempt in range(attempts):
        if chatgpt_processes_running():
            time.sleep(0.5)
            continue
        completed = subprocess.run(
            [str(SYNC_PYTHON), str(SYNC_SCRIPT), "--json", "--wait-for-existing"],
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
            if isinstance(result, dict) and result.get("status") == "ok":
                return result
        last_error = (completed.stderr or completed.stdout or "unknown error").strip()
        if (
            "已经在运行" not in last_error
            and "already running" not in last_error.lower()
        ) or attempt == attempts - 1:
            break
        time.sleep(0.5)
    raise RuntimeError(last_error or "post-exit sync failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wait for ChatGPT App to exit, then sync all three Codex histories."
    )
    parser.add_argument("--pid", type=int)
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
        logging.info("watching ChatGPT App pid=%s", args.pid)
        wait_for_process(args.pid)
        wait_until_app_is_fully_closed()
        result = run_sync_with_retry()
        logging.info(
            "post-exit sync complete: main=%s total=%s conflicts=%s exact_duplicates_removed=%s",
            result.get("visible_top_level_threads"),
            result.get("index_entries"),
            result.get("conflicts_preserved"),
            result.get("exact_duplicates_removed"),
        )
        return 0
    except Exception:
        logging.exception("post-exit sync failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
