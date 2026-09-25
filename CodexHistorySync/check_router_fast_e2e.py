"""Proof that the 快速 switch puts service_tier=priority on the wire, plus what upstreams do with it.

Part 1 points a provider at a local echo server, so the forwarded body can be read directly.
Part 2 sends the same thing to that provider for real, and needs its API key -- pass --skip-live
to stay entirely local. Both parts run on OS-assigned ports against a temp registry, so the live
router on 17895 serving the running Codex task is never touched.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_ports import reserve_port, serve_on_free_port  # noqa: E402
from sota_registry import (  # noqa: E402
    AUTH_PATH,
    INSTALL_ROOT,
    REGISTRY_PATH,
    load_registry,
    registry_digest,
)

ROUTER_PORT = reserve_port()
FORCED_MODEL = "gpt-5.6-sol"
CONTROL_MODEL = "gpt-5.6-terra"
SKIP_LIVE = "--skip-live" in sys.argv[1:]
CODEX_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer local",
    "User-Agent": "codex_cli_rs/0.144.1 (Windows 11.0.26200; x86_64) WindowsTerminal",
    "originator": "codex_cli_rs",
}


def pick_provider() -> tuple[str, str]:
    """Follow the live registry instead of naming a vendor.

    This fixture was pinned to a provider id that has since been removed, and that failure is
    invisible rather than loud: nothing ends up with fast_tier_forced set, the slug it asks for
    routes nowhere, the echo server is never reached, and both rows then print "没有这个字段" --
    which reads like a finding about the router instead of a dead fixture. The prefix comes from
    the registry too, because it is not always the id plus "--" (xlinks_gateway is not).
    """
    registry = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8-sig"))
    for provider in registry["providers"]:
        if not provider.get("enabled") or not provider.get("prefix"):
            continue
        serves = {m["id"] for m in provider.get("models") or [] if m.get("enabled")}
        if {FORCED_MODEL, CONTROL_MODEL} <= serves:
            return provider["id"], provider["prefix"]
    raise SystemExit(
        f"跳过：没有哪家非默认供应商同时开着 {FORCED_MODEL} 和 {CONTROL_MODEL}，这个检查无从下手"
    )


PROVIDER, PREFIX = pick_provider()

received: list[dict] = []


class Echo(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            received.append(json.loads(self.rfile.read(length)))
        except ValueError:
            received.append({})
        payload = json.dumps({"service_tier": "echo", "ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


def write_registry(path: Path, base_url: str | None) -> None:
    data = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8"))
    for provider in data["providers"]:
        for model in provider["models"]:
            model["fast_tier_forced"] = (
                provider["id"] == PROVIDER and model["id"] == FORCED_MODEL
            )
        if provider["id"] == PROVIDER and base_url:
            provider["base_url"] = base_url
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


def ready(proc: subprocess.Popen, reg_path: Path) -> bool:
    """Wait for the shadow router, then prove the thing that answered is our own child.

    A stranger on this port -- a leftover fixture, another check -- answers healthz just fine,
    and the only symptom downstream is an echo server that never records anything.
    """
    health = None
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/healthz", timeout=2) as r:
                payload = json.loads(r.read())
                if payload.get("status") == "ok":
                    health = payload
                    break
        except Exception:
            time.sleep(0.5)
    if health is None:
        print("  影子路由器起不来:", (proc.stderr.read() or b"").decode("utf-8", "replace")[:400])
        return False
    if health.get("registry_hash") != registry_digest(load_registry(reg_path)):
        print(f"  {ROUTER_PORT} 上答话的不是本次起的影子路由器，先腾出端口")
        return False
    return True


def ask(slug: str) -> tuple[int, str]:
    body = json.dumps({
        "model": slug, "input": "Reply exactly OK", "stream": False,
        "max_output_tokens": 64, "reasoning": {"effort": "low"},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{ROUTER_PORT}/responses", data=body, headers=CODEX_HEADERS, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
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


print(f"=== 第一部分：把 {PROVIDER} 指向本地回显服务器，直接读转发出去的 body ===")
echo, echo_port = serve_on_free_port(Echo)
threading.Thread(target=echo.serve_forever, daemon=True).start()
work = Path(tempfile.mkdtemp())
reg_path = work / "providers.json"
write_registry(reg_path, f"http://127.0.0.1:{echo_port}")
proc = start_router(reg_path, work)
try:
    if not ready(proc, reg_path):
        raise SystemExit(1)
    for label, model in (("开关=快速", FORCED_MODEL), ("开关=标准（对照）", CONTROL_MODEL)):
        received.clear()
        status, _raw = ask(f"{PREFIX}{model}")
        got = received[0] if received else {}
        tier = got.get("service_tier", "<没有这个字段>")
        print(f"  {label:<18} 上游收到 model={got.get('model')!r:<16} service_tier={tier!r}")
finally:
    stop(proc)
    echo.shutdown()
    shutil.rmtree(work, ignore_errors=True)

print()
if SKIP_LIVE:
    print("=== 第二部分：跳过（--skip-live），没有向真实上游发过任何请求 ===")
    raise SystemExit(0)
print(f"=== 第二部分：同样的请求发给真实的 {PROVIDER}，看它怎么回 ===")
work2 = Path(tempfile.mkdtemp())
reg2 = work2 / "providers.json"
write_registry(reg2, None)
proc = start_router(reg2, work2)
try:
    if not ready(proc, reg2):
        raise SystemExit(1)
    for label, model in (("开关=快速", FORCED_MODEL), ("开关=标准（对照）", CONTROL_MODEL)):
        status, raw = ask(f"{PREFIX}{model}")
        try:
            echoed = json.loads(raw).get("service_tier", "<响应里没有>")
        except ValueError:
            echoed = f"<非 JSON> {raw[:80]}"
        print(f"  {label:<18} HTTP {status}  上游回报的 service_tier = {echoed!r}")
finally:
    stop(proc)
    shutil.rmtree(work2, ignore_errors=True)
print("\n影子路由器与回显服务器都已关闭；线上 17895 全程未动")
