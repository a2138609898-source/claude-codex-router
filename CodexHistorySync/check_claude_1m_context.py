"""The 1M-context variant: who claims it, that the claim survives the write, and that it routes.

Claude Desktop turns `supports1m` on a 3P profile entry into a *second* picker row whose id is
the slug with a literal `[1m]` glued on.  Two things have to hold for that to be usable: the flag
has to reach the profile at all (the writer used to strip every field but name/labelOverride), and
whichever of the app's code paths sends the turn, the router has to answer to the suffixed id
without ever passing it upstream.

Part A is in-process against a throwaway config library.  Part B starts a shadow router and a fake
Anthropic upstream on OS-assigned ports against a temp registry, so the live router on 17895, the
real vendors, and the real config library are never involved.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
import os.path  # noqa: E402 - keeps this script runnable from any checkout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_desktop as cd  # noqa: E402
import codex_sota_router as router  # noqa: E402
from fixture_ports import reserve_port, serve_on_free_port  # noqa: E402
from sota_registry import (  # noqa: E402
    AUTH_PATH,
    INSTALL_ROOT,
    REGISTRY_PATH,
    load_registry,
    registry_digest,
)

ROUTER_PORT = reserve_port()
FAKE_ID = "fake1m"
received: list[dict] = []
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


def provider(pid: str, protocols: list[str], models: list[str], **extra: object) -> dict:
    return {
        "id": pid,
        "name": pid.title(),
        "base_url": "https://gateway.example.com",
        "protocols": protocols,
        "models": [{"id": m, "enabled": True} for m in models],
        **extra,
    }


def point_module_at(root: Path) -> None:
    """Redirect claude_desktop at a throwaway library; the real one is never opened."""
    cd.CLAUDE_3P_ROOT = root
    cd.CONFIG_LIBRARY = root / "configLibrary"
    cd.META_PATH = cd.CONFIG_LIBRARY / "_meta.json"
    cd.BACKUP_ROOT = root / "backups"
    cd.LIBRARY_LOCK_PATH = root.parent / "claude-library.lock"
    cd.SLOT_CLAIM_PATH = root.parent / "claude-slot-claim.json"


class FakeAnthropic(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        received.append({"path": self.path, "model": body.get("model")})
        payload = json.dumps({
            "id": "msg_fake", "type": "message", "role": "assistant",
            "model": body.get("model"), "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        # A model lookup must be answered out of the registry.  If the router ever forwards one
        # this teapot shows up in the check, instead of the assertion passing merely because
        # nothing was recorded.
        received.append({"path": self.path, "model": None})
        self.send_response(418)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


fake_server, FAKE_PORT = serve_on_free_port(FakeAnthropic)


def write_registry(path: Path) -> None:
    """One fake Anthropic-speaking gateway, borrowing the real default provider's auth shape.

    Two models on purpose, and chosen for what the 1M table says about them: opus-5 is in it and
    haiku-4-5 is not, so a single registry proves both the claim and the abstention.
    """
    data = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8-sig"))
    for entry in data["providers"]:
        entry["enabled"] = entry.get("is_default", False)
    default = next((p for p in data["providers"] if p.get("is_default")), None)
    if default is None:
        raise SystemExit("跳过：注册表里没有默认供应商，这个检查没有可借用的凭据形状")
    template = json.loads(json.dumps(default))
    template.update({
        "id": FAKE_ID, "name": "Fake 1M", "prefix": f"{FAKE_ID}.anthropic.",
        "base_url": f"http://127.0.0.1:{FAKE_PORT}", "enabled": True, "is_default": False,
        "protected": False, "allow_failover": False, "protocols": ["messages"],
        "messages_path": "/v1/messages", "models_path": "/v1/models",
        "models": [
            {"id": "claude-opus-5", "enabled": True, "display_name": "Opus 5 (fake)",
             "description": "", "last_test_status": "untested"},
            {"id": "claude-haiku-4-5", "enabled": True, "display_name": "Haiku 4.5 (fake)",
             "description": "", "last_test_status": "untested"},
        ],
    })
    data["providers"].append(template)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def start_router(reg: Path, work: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(INSTALL_ROOT / "codex_sota_router.py"),
         "--host", "127.0.0.1", "--port", str(ROUTER_PORT),
         "--registry", str(reg), "--auth", str(AUTH_PATH),
         "--pid-file", str(work / "p.pid"), "--log", str(work / "l.jsonl")],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=str(INSTALL_ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def healthz() -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/healthz", timeout=2) as r:
            payload = json.loads(r.read())
    except Exception:  # noqa: BLE001 - the startup race is expected, not reportable
        return None
    return payload if payload.get("status") == "ok" else None


def get(path: str) -> tuple[int, object]:
    url = f"http://127.0.0.1:{ROUTER_PORT}{urllib.parse.quote(path, safe='/')}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.status, e.read().decode("utf-8", "replace")


def post_messages(model: str) -> tuple[int, object]:
    body = json.dumps({"model": model, "max_tokens": 32,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{ROUTER_PORT}/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": "client-key"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.status, e.read().decode("utf-8", "replace")


if __name__ == "__main__":
    print("=== 1) 谁配得上 1M：走的是跟思考档同一套归一化 ===")
    for slug, want in [
        ("claude-opus-5", True),
        ("tango.anthropic.claude-opus-5", True),
        ("alfa-relay.anthropic.claude-opus-4-8", True),
        ("sierra.anthropic.claude-opus-4-6", True),
        ("anthropic.claude-sonnet-4-5-v2:0", True),
        ("Tango.Anthropic.Claude-Opus-5", True),
        ("x.anthropic.claude-fable-5", True),
        ("claude-haiku-4-5", False),
        ("kilo.anthropic.claude-opus-5-thinking", False),
        ("tango--claude-opus-5", False),
        ("gpt-5.6-sol", False),
    ]:
        got = cd.supports_1m_context(slug)
        check(f"{slug} -> {want}", got is want, f"实际 {got}")

    print("\n=== 2) build_inference_models：该给的给，不该给的一个都不给 ===")
    reg = {"providers": [
        provider("juno", ["messages"], ["claude-opus-5"], enabled=True, is_default=True,
                 prefix=""),
        provider("sierra", ["messages"], ["claude-opus-4-6", "claude-haiku-4-5"], enabled=True,
                 prefix="sierra.anthropic."),
        provider("mike", ["responses"], ["gpt-5.6-sol"], enabled=True, prefix="mike--"),
        provider("offline", ["messages"], ["claude-opus-5"], enabled=False, prefix="off.anthropic."),
    ]}
    built = cd.build_inference_models(reg)
    by_name = {m["name"]: m for m in built}
    check("只收 messages 且启用的供应商", sorted(by_name) == sorted([
        "claude-opus-5", "sierra.anthropic.claude-opus-4-6", "sierra.anthropic.claude-haiku-4-5"]),
        str(sorted(by_name)))
    check("opus 带上了 supports1m", by_name["claude-opus-5"].get("supports1m") is True)
    check("4-6 也带上了", by_name["sierra.anthropic.claude-opus-4-6"].get("supports1m") is True)
    check("haiku 没有这个键（不是 False，是没有）",
          "supports1m" not in by_name["sierra.anthropic.claude-haiku-4-5"],
          str(by_name["sierra.anthropic.claude-haiku-4-5"]))
    check("谁都没有被塞 prefer1m：默认档位由用户自己挑",
          all("prefer1m" not in m for m in built))
    check("labelOverride 照旧带着厂商和真实上游 id",
          by_name["sierra.anthropic.claude-opus-4-6"]["labelOverride"] == "Sierra · claude-opus-4-6",
          by_name["sierra.anthropic.claude-opus-4-6"]["labelOverride"])

    print("\n=== 3) write_profile 不再把能力位刮掉（一次性档库，真实档库没碰）===")
    library_root = Path(tempfile.mkdtemp())
    local, roaming = library_root / "Local", library_root / "Roaming"
    point_module_at(local / "Claude-3p")
    env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)})
    env.start()
    try:
        cd.write_profile(built + [
            {"name": "manual.anthropic.claude-sonnet-5", "labelOverride": "手写",
             "supports1m": True, "prefer1m": True, "anthropicFamilyTier": "sonnet",
             "isFamilyDefault": True},
            {"name": "lonely.anthropic.claude-sonnet-5", "labelOverride": "只有 prefer1m",
             "prefer1m": True},
            {"name": "junk.anthropic.claude-opus-5", "labelOverride": "坏值",
             "supports1m": "yes", "anthropicFamilyTier": "bad\nvalue"},
        ], "http://127.0.0.1:17994")
        written = json.loads(
            (cd.CONFIG_LIBRARY / f"{cd.SOTA_ENTRY_ID}.json").read_text(encoding="utf-8"))
        saved = {m["name"]: m for m in written["inferenceModels"]}
        check("supports1m 活着写进了档", saved["claude-opus-5"].get("supports1m") is True,
              str(saved["claude-opus-5"]))
        check("haiku 那条还是干净的两个键",
              set(saved["sierra.anthropic.claude-haiku-4-5"]) == {"name", "labelOverride"},
              str(saved["sierra.anthropic.claude-haiku-4-5"]))
        check("四个能力位一起写时全都留下",
              saved["manual.anthropic.claude-sonnet-5"] == {
                  "name": "manual.anthropic.claude-sonnet-5", "labelOverride": "手写",
                  "supports1m": True, "prefer1m": True, "anthropicFamilyTier": "sonnet",
                  "isFamilyDefault": True},
              str(saved["manual.anthropic.claude-sonnet-5"]))
        check("prefer1m 不许单独上路：没有 supports1m 时 app 根本不看它",
              "prefer1m" not in saved["lonely.anthropic.claude-sonnet-5"],
              str(saved["lonely.anthropic.claude-sonnet-5"]))
        check("非 True 的 supports1m 当没写，带换行的 tier 直接丢",
              set(saved["junk.anthropic.claude-opus-5"]) == {"name", "labelOverride"},
              str(saved["junk.anthropic.claude-opus-5"]))
        check("六个顶层键跟官方/cc-switch 对得上",
              set(written) == {"coworkEgressAllowedHosts", "disableDeploymentModeChooser",
                               "inferenceGatewayApiKey", "inferenceGatewayAuthScheme",
                               "inferenceGatewayBaseUrl", "inferenceModels", "inferenceProvider"},
              str(sorted(written)))
    finally:
        env.stop()
        shutil.rmtree(library_root, ignore_errors=True)

    print("\n=== 4) 后缀剥离：只剥末尾那一个，大小写不挑 ===")
    for raw, want in [
        ("claude-opus-5[1m]", "claude-opus-5"),
        ("claude-opus-5[1M]", "claude-opus-5"),
        ("tango.anthropic.claude-opus-5[1m]", "tango.anthropic.claude-opus-5"),
        ("claude-opus-5", "claude-opus-5"),
        ("claude-opus-5[preview]", "claude-opus-5[preview]"),
        ("claude-opus-5[1m]x", "claude-opus-5[1m]x"),
        ("[1m]", ""),
        ("", ""),
    ]:
        got = router.strip_context_1m_suffix(raw)
        check(f"{raw!r} -> {want!r}", got == want, f"实际 {got!r}")

    print("\n=== 5) 影子路由器：带后缀能派发，上游拿到的永远是裸 id ===")
    threading.Thread(target=fake_server.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp())
    registry_file = work / "providers.json"
    write_registry(registry_file)
    proc = start_router(registry_file, work)
    base = f"{FAKE_ID}.anthropic.claude-opus-5"
    try:
        for _ in range(40):
            health = healthz()
            if health:
                break
            time.sleep(0.5)
        else:
            print("影子路由器起不来:", (proc.stderr.read() or b"").decode("utf-8", "replace")[-600:])
            raise SystemExit(1)
        # A stranger on this port answers healthz just as happily; the only later symptom would
        # be a fake upstream that never records anything.
        if health.get("registry_hash") != registry_digest(load_registry(registry_file)):
            print(f"{ROUTER_PORT} 上答话的不是本次起的影子路由器，先腾出端口")
            raise SystemExit(1)

        received.clear()
        status, body = post_messages(f"{base}[1m]")
        sent = received[0]["model"] if received else None
        check(f"POST {base}[1m] 成功派发", status == 200, f"HTTP {status} {str(body)[:80]}")
        check("上游收到的是裸的 claude-opus-5，既没有前缀也没有 [1m]",
              sent == "claude-opus-5", f"上游看到 {sent!r}")
        received.clear()
        status, _ = post_messages(f"{base}[1M]")
        sent = received[0]["model"] if received else None
        check("大写后缀 [1M] 同样认，上游还是裸 id",
              status == 200 and sent == "claude-opus-5", f"HTTP {status} 上游看到 {sent!r}")

        received.clear()
        status, body = post_messages(f"{FAKE_ID}.anthropic.claude-nope[1m]")
        check("不存在的模型照旧 400，而且报的是客户端原话（带 [1m]）",
              status == 400 and "claude-nope[1m]" in str(body), f"HTTP {status} {str(body)[:110]}")
        check("被拒的请求没有碰上游", not received, f"上游收到 {len(received)} 次")

        print("\n=== 6) [1m] 只是别名：目录里不出现，查询能查到本体 ===")
        received.clear()
        status, listing = get("/v1/models")
        ids = [entry["id"] for entry in listing["data"]]
        check("/v1/models 只列真 id，没有一条带 [1m]",
              status == 200 and ids == [base, f"{FAKE_ID}.anthropic.claude-haiku-4-5"], str(ids))
        status, single = get(f"/v1/models/{base}[1m]")
        check("按别名查得到本体，回的 id 是不带后缀的那个",
              status == 200 and isinstance(single, dict) and single.get("id") == base,
              f"HTTP {status} {str(single)[:110]}")
        check("模型查询没有被转发给上游（转发了就会是 418）",
              not received, f"上游收到 {len(received)} 次")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        fake_server.shutdown()
        shutil.rmtree(work, ignore_errors=True)

    total, passed = len(results), sum(results)
    print(f"\n合计 {passed}/{total} 项通过；线上 17895/17994 与真实档库全程未动。")
    raise SystemExit(0 if passed == total else 1)







