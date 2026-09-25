"""Shared, side-effect-free process discovery and launch/offline-sync coordination.

Importing this module never starts/stops an app or opens a database. Locks are
Windows byte-range locks, interoperable with FileStream.Lock(0, 1) in PowerShell.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time

INSTALL_DIR = Path(__file__).resolve().parent
WORK_DIR = INSTALL_DIR / "work"
LIFECYCLE_LOCK_PATH = WORK_DIR / "codex-app-lifecycle.lock"
WATCHER_LOCK_PATH = WORK_DIR / "codex-history-watcher.lock"
LAUNCH_REQUEST_PATH = WORK_DIR / "codex-launch-request.json"


class LifecycleBusy(RuntimeError):
    """Another launcher or offline synchronization owns the lifecycle lock."""


class AppNotQuiescent(RuntimeError):
    """An app is using history or a launch has asked an offline sync to yield."""


class FileLock:
    def __init__(self, path: Path, wait_timeout: float = 0.0):
        self.path = Path(path)
        self.wait_timeout = max(0.0, wait_timeout)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        deadline = time.monotonic() + self.wait_timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
                self.handle = handle
                return self
            except OSError as error:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise LifecycleBusy(f"Lifecycle lock is busy: {self.path}") from error
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def __exit__(self, *_):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(self.handle.fileno(), fcntl.LOCK_UN, 1)
            finally:
                self.handle.close()
                self.handle = None


def lifecycle_lock(wait_timeout: float = 0.0) -> FileLock:
    return FileLock(LIFECYCLE_LOCK_PATH, wait_timeout)


def _role_for_path(executable: str) -> str | None:
    normalized = executable.replace("/", "\\").lower()
    if re.search(r"\\windowsapps\\openai\.codex_[^\\]+\\app\\(?:chatgpt|codex)\.exe$", normalized):
        return "desktop"
    if re.search(r"\\appdata\\local\\openai\\codexclibundled\\(?:[^\\]+\\)*codex\.exe$", normalized):
        return "backend_candidate"
    if re.search(r"\\(?:appdata\\local|program files)\\openai\\codex\\(?:[^\\]+\\)*(?:chatgpt|codex)\.exe$", normalized):
        return "app_server" if "\\bin\\" in normalized or "\\resources\\" in normalized else "desktop"
    if re.search(r"\\windowsapps\\openai\.codex_[^\\]+\\app\\resources\\(?:[^\\]+\\)*codex\.exe$", normalized):
        return "app_server"
    return None


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    return kernel


def process_identity(pid: int) -> dict | None:
    """PID + image + creation time; a recycled PID is never an app identity."""
    if os.name != "nt":
        return None
    kernel = _kernel32()
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        if error in (87, 1168):
            return None
        raise OSError(error, f"Cannot inspect process {pid}")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        stamp = (ticks - 116444736000000000) / 10_000_000
        return {"pid": int(pid), "exe": buffer.value, "creation_time": stamp,
                "creation_time_utc": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                "creation_ticks": str(ticks)}
    finally:
        kernel.CloseHandle(handle)


def _unknown_process(pid: int, image_name: str, reason: str) -> dict:
    """Represent an uninspectable candidate so callers fail closed.

    Toolhelp gives us the image basename even when a protected/elevated process
    rejects ``OpenProcess``. Returning an explicit unknown row keeps the
    lifecycle detector usable: ``app_running`` stays true, while launch
    verification never treats the row as a desktop window it owns.
    """
    return {
        "pid": int(pid),
        "exe": image_name,
        "creation_time": None,
        "creation_time_utc": None,
        "creation_ticks": None,
        "role": "unknown",
        "window_handle": 0,
        "probe_error": reason,
    }


def _process_command_line(pid: int) -> str:
    """Query a backend candidate without PowerShell/WMI or exposing its argv."""
    kernel = _kernel32()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.NtQueryInformationProcess
        query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                          wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        query.restype = wintypes.LONG
        size = wintypes.ULONG()
        query(handle, 60, None, 0, ctypes.byref(size))
        if size.value == 0 or size.value > 1_048_576:
            raise OSError(f"Cannot establish backend role for PID {pid}")
        data = ctypes.create_string_buffer(size.value)
        status = query(handle, 60, data, len(data), ctypes.byref(size))
        if status != 0:
            raise OSError(f"Cannot establish backend role for PID {pid}: {status}")

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        value = UNICODE_STRING.from_buffer(data)
        if not value.Buffer:
            return ""
        begin = ctypes.addressof(data)
        if not begin <= value.Buffer <= begin + len(data) - value.Length:
            raise OSError(f"Invalid command-line buffer for PID {pid}")
        return ctypes.wstring_at(value.Buffer, value.Length // 2)
    finally:
        kernel.CloseHandle(handle)


def _is_app_server_command_line(command_line: str) -> bool:
    # Windows permits quoting individual arguments even when they contain no
    # spaces. A quoted subcommand is still the same history-writing backend.
    return bool(re.search(r'(?:^|\s)"?app-server"?(?:\s|$)', command_line))


def inspect_codex_app_process(pid: int) -> dict | None:
    """Read one requested identity/window, without inspecting other processes.

    GUI launch verification must not fail because an unrelated elevated
    ChatGPT process is uninspectable. Offline sync still uses the global,
    fail-closed detector below.
    """
    if os.name != "nt":
        return None
    identity = process_identity(int(pid))
    if identity is None:
        return None
    role = _role_for_path(identity["exe"])
    if role == "backend_candidate":
        role = "app_server" if _is_app_server_command_line(_process_command_line(int(pid))) else None
    if role is None:
        return None
    identity.update(role=role, window_handle=0)
    return _attach_visible_windows([identity])[0]


def detect_codex_app_processes() -> list[dict]:
    """Read only; includes Electron children/backend, excludes the unrelated ChatGPT app."""
    if os.name != "nt":
        return []
    kernel = _kernel32()

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        item = PROCESSENTRY32W()
        item.dwSize = ctypes.sizeof(item)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(item))
        while more:
            if item.szExeFile.lower() in ("chatgpt.exe", "codex.exe"):
                try:
                    identity = process_identity(item.th32ProcessID)
                except OSError as error:
                    # Failure to establish ownership cannot mean "safe to rewrite history".
                    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
                    # A process that exited during enumeration is gone. An
                    # access-denied process is different: keep it as an
                    # explicit unknown so offline sync remains busy instead of
                    # crashing and accidentally proceeding later.
                    if code in (6, 87, 1168):
                        identity = None
                    elif code == 5:
                        result.append(_unknown_process(
                            item.th32ProcessID, item.szExeFile, "access_denied"
                        ))
                        identity = None
                    else:
                        raise
                if identity:
                    role = _role_for_path(identity["exe"])
                    if role == "backend_candidate":
                        try:
                            command_line = _process_command_line(identity["pid"])
                        except OSError as error:
                            code = getattr(error, "winerror", None) or getattr(error, "errno", None)
                            if code == 5:
                                result.append(_unknown_process(
                                    identity["pid"], item.szExeFile, "access_denied"
                                ))
                                identity = None
                                command_line = ""
                            else:
                                raise
                        role = "app_server" if _is_app_server_command_line(command_line) else None
                    if role:
                        identity.update(parent_pid=int(item.th32ParentProcessID), role=role, window_handle=0)
                        result.append(identity)
            more = kernel.Process32NextW(snapshot, ctypes.byref(item))
    finally:
        kernel.CloseHandle(snapshot)

    return _attach_visible_windows(result)


def _attach_visible_windows(result: list[dict]) -> list[dict]:
    """EnumWindows reads window ownership only; it does not open other PIDs."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    by_pid = {item["pid"]: item for item in result}

    @callback_type
    def visit(window, _):
        if user32.IsWindowVisible(window):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value in by_pid:
                by_pid[owner.value]["window_handle"] = int(window)
        return True

    if not user32.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def app_running() -> bool:
    return bool(detect_codex_app_processes())


def _retire_launch_request(raw: bytes) -> None:
    """Remove a request only when the file has not been replaced meanwhile.

    Launchers publish requests with an atomic rename.  Re-reading the bytes before
    unlinking prevents a stale observer from deleting a newer request written by a
    concurrently starting launcher.
    """
    try:
        if LAUNCH_REQUEST_PATH.read_bytes() != raw:
            return
        LAUNCH_REQUEST_PATH.unlink()
    except OSError:
        return


def _request_launcher_is_live(payload: dict) -> bool:
    """Fail closed unless the launcher PID and (when present) birth identity agree."""
    try:
        pid = int(payload.get("launcher_pid"))
    except (TypeError, ValueError):
        # Legacy request files did not carry a PID; their short expiry is the only
        # available guard and is handled by launch_pending().
        return True
    if pid <= 0 or os.name != "nt":
        return False
    try:
        identity = process_identity(pid)
    except OSError:
        # Access denied is not proof that the launcher exited. Keep the request
        # pending until its explicit expiry rather than racing a live launch.
        return True
    if identity is None:
        return False
    expected_ticks = str(payload.get("launcher_creation_ticks") or "").strip()
    if expected_ticks and expected_ticks != str(identity.get("creation_ticks") or ""):
        return False
    return True


def launch_pending() -> bool:
    raw = None
    try:
        raw = LAUNCH_REQUEST_PATH.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            _retire_launch_request(raw)
            return False
        if float(payload.get("expires_at", 0)) <= time.time():
            _retire_launch_request(raw)
            return False
        if not _request_launcher_is_live(payload):
            _retire_launch_request(raw)
            return False
        return True
    except (OSError, ValueError, TypeError):
        if raw is not None:
            _retire_launch_request(raw)
        return False


def assert_quiescent() -> None:
    if launch_pending():
        raise AppNotQuiescent("launch_requested")
    if app_running():
        raise AppNotQuiescent("app_running")
