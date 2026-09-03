"""Failover checks: does a failing vendor hand the same model to the next one?

Two fake upstreams and a shadow router, all on OS-assigned loopback ports against a temp
registry, so the live router on 17895 and the real vendors are never involved -- and so a
leftover fixture from an earlier run cannot quietly answer in their place.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import os.path  # noqa: E402 - keeps this script runnable from any checkout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixture_ports import reserve_port, serve_on_free_port  # noqa: E402
from sota_registry import (  # noqa: E402
    AUTH_PATH,
    INSTALL_ROOT,
    REGISTRY_PATH,
    load_registry,
    registry_digest,
)

# The router port is handed to a child process, so it has to be reserved and released; the
# scenario then checks the healthz it gets back really is that child. Nothing ever listens on
# the idle port -- the default家 must stay unreachable, so that MODEL has exactly one fallback.
ROUTER_PORT = reserve_port()
IDLE_PORT = reserve_port()
MODEL = "gpt-5.6-sol"
OTHER_MODEL = "gpt-5.6-terra"


def pick_roles() -> tuple[str, str, str]:
    """Choose default / primary / backup from whatever the live registry actually has.

    Hard-coding vendor ids made this fixture silently useless the moment a vendor was
    removed: it wrote a registry without the primary and every scenario failed for the wrong
    reason. Roles are derived instead, so the check follows the user's config.
    """
    registry = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8"))
    providers = [p for p in registry["providers"] if p.get("enabled")]
    default = next((p["id"] for p in providers if not p.get("prefix")), None)
    serving = [
        p["id"]
        for p in providers
        if p["id"] != default
        and any(m.get("enabled") and m["id"] == MODEL for m in p.get("models") or [])
    ]
    if default is None or len(serving) < 2:
        raise SystemExit(
            f"跳过：需要一个空前缀的默认家外加至少两家提供 {MODEL} 的供应商，"
            f"当前 default={default} 可用={serving}"
        )
    return default, serving[0], serving[1]


DEFAULT_PROVIDER, PRIMARY, BACKUP = pick_roles()
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer local",
    "User-Agent": "codex_cli_rs/0.144.1 (Windows 11.0.26200; x86_64) WindowsTerminal",
    "originator": "codex_cli_rs",
}
hits: list[str] = []
primary_status = [503]


def make_handler(name: str, status_source) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            hits.append(name)
            status = status_source() if callable(status_source) else status_source
            payload = json.dumps({"served_by": name, "status": status}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args) -> None:
            return

    return Handler


# Bound at import so their real ports are known before the first temp registry is written.
primary_server, PRIMARY_PORT = serve_on_free_port(
    make_handler("primary", lambda: primary_status[0])
)
backup_server, BACKUP_PORT = serve_on_free_port(make_handler("backup", 200))


def write_registry(path: Path, failover: bool) -> None:
    """Three enabled providers: the required empty-prefix default plus a primary and a backup.

    The default keeps a different model enabled so it never becomes a candidate for MODEL,
    which leaves exactly one fallback and makes the attempt order unambiguous.
    """
    data = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8"))
    urls = {DEFAULT_PROVIDER: IDLE_PORT, PRIMARY: PRIMARY_PORT, BACKUP: BACKUP_PORT}
    for provider in data["providers"]:
        pid = provider["id"]
        keep = pid in urls
        provider["enabled"] = keep
        provider["is_default"] = pid == DEFAULT_PROVIDER
        provider["allow_failover"] = failover and pid == PRIMARY
        if keep:
            provider["base_url"] = f"http://127.0.0.1:{urls[pid]}"
        wanted = OTHER_MODEL if pid == DEFAULT_PROVIDER else MODEL
        for model in provider["models"]:
            model["enabled"] = keep and model["id"] == wanted
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def start_router(reg_path: Path, work: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(INSTALL_ROOT / "codex_sota_router.py"),
         "--host", "127.0.0.1", "--port", str(ROUTER_PORT),
         "--registry", str(reg_path), "--auth", str(AUTH_PATH),
         "--pid-file", str(work / "r.pid"), "--log", str(work / "r.jsonl")],
        cwd=str(INSTALL_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def wait_ready() -> dict | None:
    """Poll healthz until it answers ok, returning the payload so the caller can identify it."""
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/healthz", timeout=2) as r:
                payload = json.loads(r.read())
                if payload.get("status") == "ok":
                    return payload
        except Exception:
            time.sleep(0.5)
    return None


def ask(slug: str) -> tuple[int, str]:
    body = json.dumps({"model": slug, "input": "hi", "stream": False}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{ROUTER_PORT}/responses", data=body, headers=HEADERS, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.status, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def scenario(label: str, failover: bool, upstream_status: int, expect_status: int, expect_hits: list[str]) -> bool:
    work = Path(tempfile.mkdtemp())
    reg = work / "providers.json"
    write_registry(reg, failover)
    primary_status[0] = upstream_status
    hits.clear()
    proc = start_router(reg, work)
    try:
        health = wait_ready()
        if health is None:
            detail = (proc.stderr.read() or b"").decode("utf-8", "replace")
            print(f"  FAIL  {label}\n        影子路由器起不来：\n{detail[-900:]}", flush=True)
            return False
        # A stranger answering on 17896 looks exactly like success here, and only shows up much
        # later as an empty `hits`. The hash is taken over the validated registry, so it matches
        # only the child we just started against our own temp copy -- the router computes it the
        # same way, from the same file, with the same flags.
        want = registry_digest(load_registry(reg))
        if health.get("registry_hash") != want:
            got = str(health.get("registry_hash"))[:12]
            print(
                f"  FAIL  {label}\n        {ROUTER_PORT} 上答话的不是本次起的影子路由器"
                f"（registry_hash {got} != {want[:12]}），先腾出端口",
                flush=True,
            )
            return False
        status, raw = ask(f"{PRIMARY}--{MODEL}")
        try:
            parsed = json.loads(raw)
            served = parsed.get("served_by") or parsed.get("error", {}).get("type", "?")
        except ValueError:
            served = raw[:40]
        log_path = work / "r.jsonl"
        logged = log_path.read_text(encoding="utf-8").strip().splitlines() if log_path.exists() else []
        vendors = [json.loads(line)["vendor"] for line in logged if line.strip()]
        ok = status == expect_status and hits == expect_hits
        print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
        print(f"        上游被打到 {hits}（期望 {expect_hits}）", flush=True)
        print(f"        客户端 HTTP {status}（期望 {expect_status}），由 {served} 提供", flush=True)
        print(f"        日志记录了 {len(vendors)} 次尝试 {vendors}", flush=True)
        return ok
    finally:
        stop(proc)
        # One temp tree per scenario, six per run, all of them left behind until now.
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    threading.Thread(target=primary_server.serve_forever, daemon=True).start()
    threading.Thread(target=backup_server.serve_forever, daemon=True).start()

    results = []
    for args in (
        ("主上游 503 + 开启换家 → 换到备用", True, 503, 200, ["primary", "backup"]),
        # 关掉换家不等于「一次都不重试」。同一家原地再试两次是安全的：这时还没有一个字节
        # 发给客户端，换家的承诺（绝不把请求悄悄记到别家账上）也没有被破坏。以前这里只打
        # 一次就把 503 抛给客户端，中转随便抖一下，Claude Desktop 的探测就会把整个网关判成
        # 坏的——而那条探测永远钉在默认供应商上。
        ("主上游 503 + 关闭换家 → 原地重试同一家，仍然暴露 503", False, 503, 503,
         ["primary", "primary", "primary"]),
        ("主上游 403（分组停用那类）+ 开启换家 → 换到备用", True, 403, 200, ["primary", "backup"]),
        ("主上游 429 限流 + 开启换家 → 换到备用", True, 429, 200, ["primary", "backup"]),
        ("主上游 400（请求本身错）+ 开启换家 → 不该换", True, 400, 400, ["primary"]),
        ("主上游正常 + 开启换家 → 一次就成，不碰备用", True, 200, 200, ["primary"]),
    ):
        print(f"=== {args[0]} ===", flush=True)
        results.append(scenario(*args))
        print(flush=True)

    primary_server.shutdown()
    backup_server.shutdown()
    print(f"合计 {sum(results)}/{len(results)} 个场景通过；线上 17895 全程未动", flush=True)
    raise SystemExit(0 if all(results) else 1)

