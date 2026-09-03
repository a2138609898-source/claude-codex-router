"""Isolated PowerShell launcher checks on a random port and throwaway workspace."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parent
STARTER = ROOT / "Start-CodexSotaRouter.ps1"
ROUTER = ROOT / "codex_sota_router.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "synthetic-test-key"}),
        encoding="utf-8",
    )
    registry = {
        "version": 1,
        "providers": [
            {
                "workspace": "codex",
                "id": "testdefault",
                "name": "Synthetic Default",
                "base_url": "http://127.0.0.1:9",
                "prefix": "",
                "enabled": True,
                "protected": False,
                "is_default": True,
                "allow_failover": False,
                "auth_type": "codex_auth",
                "auth_header": "Authorization",
                "auth_prefix": "Bearer ",
                "models_path": "/models",
                "responses_path": "/responses",
                "messages_path": "/v1/messages",
                "protocols": ["responses"],
                "timeout_seconds": 5,
                "extra_headers": {},
                "models": [{"id": "test-model", "enabled": True}],
            }
        ],
    }
    (root / "providers.json").write_text(json.dumps(registry), encoding="utf-8")


def invoke(root: Path, port: int, *extra: str) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(STARTER),
        "-Workspace",
        "codex",
        "-SotaRootOverride",
        str(root),
        "-RouterPortOverride",
        str(port),
        "-RouterScriptOverride",
        str(ROUTER),
        "-PythonExecutableOverride",
        sys.executable,
        *extra,
    ]
    # errors="replace" is not optional here.  PowerShell writes its error text in the console
    # codepage (GBK on this machine), and an undecodable byte kills subprocess's reader thread
    # rather than raising here -- the call then returns with stderr set to None, which is how
    # this check used to die with "NoneType + str" three assertions later.
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def text_of(result: subprocess.CompletedProcess[str]) -> str:
    """Both streams as one string, tolerating a stream that never arrived."""
    return (result.stdout or "") + (result.stderr or "")



def payload(result: subprocess.CompletedProcess[str]) -> dict:
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return json.loads(response.read()).get("status") == "ok"
    except Exception:
        return False


def accepts_connection(port: int, listener: socket.socket) -> bool:
    """True while `listener` is still alive and completing handshakes.

    The launcher's own health probe connects and then gives up without the test ever calling
    accept(), so that connection stays parked in the backlog.  Draining it first is what makes
    this measure "was the owner killed" instead of "is the queue full".
    """
    listener.setblocking(False)
    while True:
        try:
            listener.accept()[0].close()
        except OSError:
            break
    listener.setblocking(True)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'} {label}" + (f": {detail}" if detail else ""))
    return condition


def main() -> int:
    results: list[bool] = []
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary) / "workspace"
        write_workspace(workspace)
        port = free_port()
        try:
            first = invoke(workspace, port)
            first_data = payload(first)
            pid_path = workspace / "sota-router.pid"
            original_pid = pid_path.read_text(encoding="ascii").strip() if pid_path.exists() else ""
            results.append(
                check(
                    "launcher starts a healthy isolated router",
                    first.returncode == 0 and first_data.get("status") == "ready" and healthy(port),
                    text_of(first).strip()[-200:],
                )
            )

            pid_path.unlink(missing_ok=True)
            second = invoke(workspace, port)
            repaired_pid = pid_path.read_text(encoding="ascii").strip() if pid_path.exists() else ""
            results.append(
                check(
                    "missing PID file is repaired without starting a duplicate",
                    second.returncode == 0
                    and payload(second).get("started") is False
                    and repaired_pid == original_pid,
                    text_of(second).strip()[-200:],
                )
            )

            pid_path.unlink(missing_ok=True)
            stopped = invoke(workspace, port, "-Stop")
            for _ in range(20):
                if not healthy(port):
                    break
                time.sleep(0.1)
            results.append(
                check(
                    "stop finds its router by the listening port when PID is missing",
                    stopped.returncode == 0 and not healthy(port),
                    text_of(stopped).strip()[-200:],
                )
            )

            unrelated = socket.socket()
            unrelated.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            unrelated.bind(("127.0.0.1", port))
            unrelated.listen(8)
            try:
                blocked = invoke(workspace, port)
                report = text_of(blocked)
                # PowerShell hard-wraps error text at the console width, mid-token, so the
                # reported PID is only reliably findable with the whitespace taken out.
                dense = "".join(report.split())
                results.append(
                    check(
                        "an unrelated listener is reported and never killed",
                        blocked.returncode != 0
                        and "unrelated process" in report
                        and f"PID{os.getpid()}" in dense
                        and accepts_connection(port, unrelated)
                        and not healthy(port),
                        report.strip()[-240:],
                    )
                )
            finally:
                unrelated.close()
        finally:
            invoke(workspace, port, "-Stop")

    print(f"\n{sum(results)}/{len(results)} checks passed; no live port or config was used")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
