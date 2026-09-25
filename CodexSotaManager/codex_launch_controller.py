"""Non-blocking, request-scoped supervision of the Codex desktop launcher.

There is deliberately no subprocess.run(timeout=...) here: that used to kill the
shell while leaving its descendants starting Codex, then invite a second launch.
The launcher bounds its own operations. This controller follows the same request
until it reports a verified window, a queued sync, or a concrete failure.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from typing import Callable
import uuid


PHASE_LABELS = {
    "preflight": "正在检查本地启动配置",
    "waiting_for_history_sync": "正在等待历史写入完成，随后自动打开 Codex",
    "waiting_for_launch": "另一个启动请求正在处理，正在等待",
    "waiting_for_lifecycle": "正在等待启动锁释放",
    "router": "正在检查本地路由器",
    "preparing_router": "正在检查本地路由器",
    "closing_previous_app": "正在等待 Codex 正常退出",
    "opening_desktop": "正在打开 Codex App",
    "explicit_history_sync": "正在执行手动请求的历史同步",
    "starting_app": "正在打开 Codex App",
    "waiting_for_window": "正在等待 Codex 窗口",
    "ready": "Codex 窗口已确认",
    "window_ready": "Codex 窗口已确认",
    "verifying_window": "启动器已返回，正在核验本次 Codex 窗口",
    "waiting_for_previous_app": "正在等待上一个 Codex 后台正常退出",
}

# A deferred launcher is a request that is still valid, not a failure. Keep the
# first retry quick, then back off when an offline history sync or app-server
# shutdown takes a while. This prevents a long-lived sync from spawning a new
# PowerShell process every couple of seconds.
DEFERRED_RETRY_INITIAL_SECONDS = 2.0
DEFERRED_RETRY_MAX_SECONDS = 30.0
DEFERRED_RETRY_PHASES = {
    "waiting_for_history_sync",
    "waiting_for_previous_app",
    "preparing_router",
    "router",
    "waiting_for_launch",
    "waiting_for_lifecycle",
}
VERIFY_WINDOW_GRACE_SECONDS = 15.0


def parse_result(output: str, request_id: str) -> dict | None:
    """Only accept this request's JSON, never another launch's last-result file."""
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line.lstrip("\ufeff"))
        except (ValueError, TypeError):
            continue
        if (isinstance(value, dict) and value.get("request_id") == request_id
                and value.get("status") in {"starting", "ready", "deferred", "error"}):
            return value
    return None


def owns_reported_window(result: dict, processes: list[dict]) -> bool:
    """PID, birth identity and visible window must all agree with the launcher."""
    if result.get("window_owned") is not True:
        return False
    try:
        pid = int(result["app_process_id"])
    except (KeyError, TypeError, ValueError):
        return False
    for process in processes:
        if (process.get("pid") != pid or process.get("role") != "desktop"
                or not process.get("window_handle")):
            continue
        ticks = result.get("app_creation_ticks") or result.get("creation_ticks")
        if ticks is not None:
            return str(ticks) == str(process.get("creation_ticks"))
        # Older compatible launchers may provide an ISO timestamp instead of FILETIME.
        stamp = result.get("app_started_utc")
        if stamp:
            from datetime import datetime
            try:
                expected = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
                return abs(expected - float(process["creation_time"])) < 0.01
            except (KeyError, ValueError, TypeError, OverflowError):
                return False
        return False
    return False


class LaunchAttempt:
    """One click, one identity; deferred retries never overlap a live launcher."""

    def __init__(self, invocation: list[str], cwd: Path, core_root: Path,
                 inspect_processes: Callable[[], list[dict]], *,
                 inspect_process: Callable[[int], dict | None] | None = None,
                 request_id: str | None = None, restart: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 popen: Callable = subprocess.Popen,
                 verification_grace_seconds: float = VERIFY_WINDOW_GRACE_SECONDS):
        self.request_id = request_id or uuid.uuid4().hex
        if not self.request_id or any(c not in "0123456789abcdef-" for c in self.request_id.lower()):
            raise ValueError("Invalid launch request id")
        self.invocation = [*invocation, "-NoGui", "-LaunchRequestId", self.request_id]
        if restart:
            self.invocation.append("-Restart")
        self.cwd = Path(cwd)
        self.core_root = Path(core_root)
        self.status_path = self.core_root / "work" / f"launch-status-{self.request_id}.json"
        self.log_path = self.core_root / "logs" / f"launch-{self.request_id}.log"
        self.inspect_processes = inspect_processes
        self.inspect_process = inspect_process
        self.clock = clock
        self.popen = popen
        self.started = clock()
        self.process = None
        self.sink = None
        self.next_retry = 0.0
        self.retry_delay = DEFERRED_RETRY_INITIAL_SECONDS
        self.verification_grace_seconds = max(0.0, float(verification_grace_seconds))
        self.last_result: dict = {"status": "starting", "phase": "preflight"}
        self.output_offset = 0
        self.status_before: int | None = None
        self.terminal: dict | None = None
        self.cancelled = False
        self.detached = False
        self.completed_result: dict | None = None
        self.completed_at: float | None = None

    @property
    def can_cancel_queued(self) -> bool:
        return self.process is None and self.last_result.get("status") == "deferred"

    def cancel_queued(self) -> bool:
        if not self.can_cancel_queued:
            return False
        self.cancelled = True
        return True

    def detach(self) -> None:
        """Stop UI supervision, not the launcher or the app it owns.

        The child has its own copy of the log handle. Closing the manager's
        copy is safe and does not cancel an in-progress configuration write.
        """
        self.detached = True
        if self.sink is not None:
            self.sink.close()
            self.sink = None

    def _verify_completed(self) -> dict:
        result = self.completed_result
        assert result is not None
        completed_at = self.completed_at if self.completed_at is not None else self.clock()
        try:
            if self.inspect_process is not None:
                process = self.inspect_process(int(result["app_process_id"]))
                processes = [process] if process is not None else []
            else:
                processes = self.inspect_processes()
        except (OSError, RuntimeError) as error:
            # Verification trouble is not launch failure. Keep this exact
            # completed attempt; polling again must never spawn a second one.
            self.last_result = {**result, "status": "starting", "phase": "verifying_window",
                                "verification_error": str(error)}
            return self._snapshot(self.last_result)
        if owns_reported_window(result, processes):
            self.terminal = {**result, "window_shown": True}
        else:
            # The launcher writes its ready marker immediately after discovering
            # the window. Electron can briefly disappear from EnumWindows while
            # ownership moves to the renderer, and a targeted process query can
            # briefly return no object. Keep this exact request alive for a
            # bounded grace period instead of showing a false launch failure.
            if self.clock() - completed_at < self.verification_grace_seconds:
                self.last_result = {**result, "status": "starting", "phase": "waiting_for_window"}
                return self._snapshot(self.last_result)
            self.terminal = {**result, "status": "error", "window_shown": False,
                             "error": "启动器报告的 Codex 进程已退出或身份发生变化；请查看本次启动日志。"}
        return self._snapshot(self.terminal)

    def _start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.status_before = self.status_path.stat().st_mtime_ns
        except OSError:
            self.status_before = None
        # A real file, not PIPE: Electron descendants must not hold communicate() open.
        self.sink = self.log_path.open("ab", buffering=0)
        self.output_offset = self.sink.tell()
        try:
            self.process = self.popen(
                self.invocation, cwd=str(self.cwd), stdin=subprocess.DEVNULL,
                stdout=self.sink, stderr=subprocess.STDOUT, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self.sink.close()
            self.sink = None
            raise

    def _output(self) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(max(self.output_offset, self.log_path.stat().st_size - 131072))
                return handle.read(131072).decode("utf-8-sig", errors="replace")
        except OSError:
            return ""

    def _status(self) -> dict | None:
        try:
            if self.status_path.stat().st_mtime_ns == self.status_before:
                return None
            with self.status_path.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                return None
            value = json.loads(raw.decode("utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            return None
        return value if isinstance(value, dict) and value.get("request_id") == self.request_id else None

    def _snapshot(self, value: dict) -> dict:
        return {**value, "elapsed": self.clock() - self.started,
                "request_id": self.request_id, "log_path": str(self.log_path)}

    def poll(self) -> dict:
        if self.detached:
            return self._snapshot({"status": "detached"})
        if self.terminal is not None:
            return self._snapshot(self.terminal)
        if self.cancelled:
            return self._snapshot({"status": "cancelled"})
        if self.completed_result is not None:
            return self._verify_completed()
        if self.process is None:
            if self.clock() < self.next_retry:
                return self._snapshot(self.last_result)
            self._start()
        code = self.process.poll()
        if code is None:
            status = self._status()
            if status:
                self.last_result = status
                if status.get("status") != "deferred":
                    self.retry_delay = DEFERRED_RETRY_INITIAL_SECONDS
            # A terminal status file can precede process teardown. Never launch again yet.
            return self._snapshot({**self.last_result, "status": "starting",
                                   "launcher_pid": self.process.pid})
        if self.sink is not None:
            self.sink.close()
            self.sink = None
        self.process = None
        output = self._output()
        result = parse_result(output, self.request_id) or self._status()
        if result is None:
            detail = next((line.strip() for line in reversed(output.splitlines()) if line.strip()),
                          "启动器没有返回本次请求的结构化状态")
            result = {"status": "error", "error": f"启动器退出码 {code}：{detail}"}
        if result.get("status") == "deferred" and code in (0, 75):
            self.last_result = result
            phase = str(result.get("phase") or "")
            delay = self.retry_delay
            self.next_retry = self.clock() + delay
            if phase in DEFERRED_RETRY_PHASES:
                self.retry_delay = min(
                    DEFERRED_RETRY_MAX_SECONDS,
                    max(DEFERRED_RETRY_INITIAL_SECONDS, delay * 2.0),
                )
            else:
                # Waiting for a window is an active GUI transition. Poll it at
                # the short cadence rather than delaying a ready window by 30s.
                self.retry_delay = DEFERRED_RETRY_INITIAL_SECONDS
            return self._snapshot(result)
        if result.get("status") == "ready" and code == 0:
            self.completed_result = result
            self.completed_at = self.clock()
            return self._verify_completed()
        elif result.get("status") != "error":
            result = {**result, "status": "error",
                      "error": f"启动器未完成打开窗口（退出码 {code}，阶段 {result.get('phase', 'unknown')}）。"}
        self.terminal = result
        return self._snapshot(result)
