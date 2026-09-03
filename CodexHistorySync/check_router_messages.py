"""Anthropic Messages side of the router: /v1/messages dispatch and /v1/models discovery.

A fake Anthropic upstream and a shadow router, both on OS-assigned ports against a temp
registry, so the live router on 17895 and the real vendors are never involved.
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

ROUTER_PORT = reserve_port()
CLAUDE_ID = "fakeanthropic"
received: list[dict] = []


class FakeAnthropic(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        received.append({"path": self.path, "body": body, "auth_header_seen": bool(
            self.headers.get("Authorization") or self.headers.get("x-api-key")),
            "anthropic_version": self.headers.get("anthropic-version")})
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
        # Model lookups must be answered locally.  If the router ever forwards one, this teapot
        # turns up in the check instead of a registry-derived 200 -- an assertion that the
        # request never left the router would otherwise pass simply because nothing recorded it.
        received.append({"path": self.path, "body": {}, "auth_header_seen": False,
                         "anthropic_version": None})
        self.send_response(418)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


# Bound at import so write_registry knows where to point the fake gateway.
fake_server, FAKE_PORT = serve_on_free_port(FakeAnthropic)


def write_registry(path: Path) -> None:
    """Keep the real default provider, add one fake Anthropic-speaking gateway.

    The template is the default provider by name rather than by position: it is only there to
    donate a valid auth shape, and copying whichever entry happens to sit at index 0 would hand
    the fake gateway some other vendor's credential the day the list is reordered.
    """
    data = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8-sig"))
    for provider in data["providers"]:
        provider["enabled"] = provider.get("is_default", False)
    default = next((p for p in data["providers"] if p.get("is_default")), None)
    if default is None:
        raise SystemExit("跳过：注册表里没有默认供应商，这个检查没有可借用的凭据形状")
    template = json.loads(json.dumps(default))
    template.update({
        "id": CLAUDE_ID, "name": "Fake Anthropic", "prefix": "fake--",
        "base_url": f"http://127.0.0.1:{FAKE_PORT}", "enabled": True, "is_default": False,
        "protected": False, "allow_failover": False, "protocols": ["messages"],
        "messages_path": "/v1/messages", "models_path": "/v1/models",
        # Two models on purpose: with one entry first_id and last_id are the same string, so the
        # envelope check below could not tell "the ends of the page" from "the only id there is".
        "models": [{"id": "claude-opus-5", "enabled": True, "display_name": "Opus 5 (fake)",
                    "description": "", "last_test_status": "untested"},
                   {"id": "claude-haiku-4-5", "enabled": True, "display_name": "Haiku 4.5 (fake)",
                    "description": "", "last_test_status": "untested"}],
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
    """Tolerant probe for the startup race: get() below deliberately does not swallow errors.

    The readiness loop used to call get(), which only handles HTTPError -- so the connection
    refused while the router was still starting propagated out and killed the run before the
    first check ever ran.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/healthz", timeout=2) as r:
            payload = json.loads(r.read())
    except Exception:
        return None
    return payload if payload.get("status") == "ok" else None


def get(path: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}{path}", timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.status, e.read().decode("utf-8", "replace")


def post_messages(model: str, extra_headers: dict[str, str] | None = None) -> tuple[int, object]:
    body = json.dumps({"model": model, "max_tokens": 32,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    headers = {"Content-Type": "application/json", "x-api-key": "client-key"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(
        f"http://127.0.0.1:{ROUTER_PORT}/v1/messages", data=body,
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.status, e.read().decode("utf-8", "replace")


if __name__ == "__main__":
    threading.Thread(target=fake_server.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp())
    reg = work / "providers.json"
    write_registry(reg)
    proc = start_router(reg, work)
    results = []
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
        if health.get("registry_hash") != registry_digest(load_registry(reg)):
            print(f"{ROUTER_PORT} 上答话的不是本次起的影子路由器，先腾出端口")
            raise SystemExit(1)

        print("=== 1) /v1/models 只列 messages 协议的供应商 ===")
        status, body = get("/v1/models")
        ids = [entry["id"] for entry in body["data"]]
        ok = ids == ["fake--claude-opus-5", "fake--claude-haiku-4-5"]
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  HTTP {status}  data = {ids}")
        entry = body["data"][0]
        shape_ok = entry.get("type") == "model" and "display_name" in entry and "id" in entry
        results.append(shape_ok)
        print(f"  {'PASS' if shape_ok else 'FAIL'}  条目同时带 Anthropic 字段: type={entry.get('type')!r} display_name={entry.get('display_name')!r}")
        # Anthropic 的列表信封是 data + has_more + first_id + last_id，客户端就拿后两个去翻页。
        # 整份注册表一次发完，所以 has_more 恒为 false，两个 id 只是这一页的两端。
        envelope_ok = (body.get("has_more") is False
                       and body.get("first_id") == ids[0] and body.get("last_id") == ids[-1])
        results.append(envelope_ok)
        print(f"  {'PASS' if envelope_ok else 'FAIL'}  信封带分页字段: has_more={body.get('has_more')!r} "
              f"first_id={body.get('first_id')!r} last_id={body.get('last_id')!r}")
        # created_at 在官方 SDK 里是必填字段。第三方网关的模型没有发布日期可报，就用 Anthropic
        # 自己的哨兵（Unix 纪元 = 日期未知），而不是编一个看起来像真的日期。
        created_ok = all(e.get("created_at") for e in body["data"])
        results.append(created_ok)
        print(f"  {'PASS' if created_ok else 'FAIL'}  每条都带 created_at: {entry.get('created_at')!r}")

        print("\n=== 2) /models 仍然是 responses 那一侧（不回归）===")
        status, body = get("/models")
        legacy = [e["id"] for e in body["data"]]
        ok = bool(legacy) and "fake--claude-opus-5" not in legacy
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  HTTP {status}  {len(legacy)} 个模型，不含 fake--")

        print("\n=== 3) POST /v1/messages 按前缀派发并剥掉前缀 ===")
        received.clear()
        status, body = post_messages("fake--claude-opus-5")
        got = received[0] if received else {}
        ok = (status == 200 and got.get("path") == "/v1/messages"
              and got.get("body", {}).get("model") == "claude-opus-5")
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  HTTP {status}  上游收到 path={got.get('path')!r} model={got.get('body', {}).get('model')!r}")
        auth_ok = got.get("auth_header_seen") is True
        results.append(auth_ok)
        print(f"  {'PASS' if auth_ok else 'FAIL'}  上游收到了路由器注入的凭据（不是客户端那把）")

        print("\n=== 4) messages 路径不注入 service_tier ===")
        tier = got.get("body", {}).get("service_tier", "<没有>")
        ok = tier == "<没有>"
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  上游收到的 service_tier = {tier!r}")

        print("\n=== 5) anthropic-version：客户端没带就补默认，带了就照原样转发 ===")
        # 转发用的是黑名单，客户端带了什么就过什么；这里验证的是「没带时补上」不会变成
        # 「带了也覆盖」。故意用不同的大小写发，顺带验证判断不区分大小写。
        received.clear()
        post_messages("fake--claude-opus-5")
        default_seen = (received[0] if received else {}).get("anthropic_version")
        ok = default_seen == "2023-06-01"
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  客户端没带时上游收到 {default_seen!r}（官方 SDK 每次请求都钉这个值）")
        received.clear()
        post_messages("fake--claude-opus-5", {"Anthropic-Version": "2026-01-01"})
        forwarded = (received[0] if received else {}).get("anthropic_version")
        ok = forwarded == "2026-01-01"
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  客户端自己带了就不覆盖：上游收到 {forwarded!r}")

        print("\n=== 6) 未启用的模型给干净的 400 ===")
        status, body = post_messages("fake--nope")
        ok = status == 400 and "model_not_enabled" in str(body)
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  HTTP {status} {str(body)[:70]}")

        print("\n=== 7) GET /v1/models/{id}：SDK 用它把别名解析成具体 id ===")
        # 以前这个路径会掉进「未知路径」那个 404。它必须在本地答：转发就等于把供应商凭据
        # 发到 INFERENCE_PATHS 之外的路径上，而那个白名单存在的意义就是拦住这件事。
        received.clear()
        status, body = get("/v1/models/fake--claude-opus-5")
        ok = (status == 200 and isinstance(body, dict)
              and body.get("id") == "fake--claude-opus-5" and body.get("type") == "model"
              and body.get("display_name") == "Opus 5 (fake)")
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  HTTP {status}  单个模型 = {str(body)[:90]}")
        ok = not received
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  没有转发给上游（上游收到 {len(received)} 次；转发了就会是 418）")
        status, body = get("/v1/models/fake--nope")
        ok = status == 404 and "model_not_found" in str(body)
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  不存在的 id 给 404 model_not_found：HTTP {status} {str(body)[:60]}")
        # 解码只是为了让带百分号转义的 id 也能查到；解出来的东西只跟注册表已有的 id 逐字比对，
        # 既不拼路径也不拼 URL，所以穿越串在这里就只是一个查不到的 id。
        received.clear()
        status, body = get("/v1/models/%2e%2e%2fadmin")
        ok = status == 404 and "model_not_found" in str(body) and not received
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  编码过的穿越串只当成查不到的 id：HTTP {status}，上游收到 {len(received)} 次")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        fake_server.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n合计 {sum(results)}/{len(results)} 项通过；线上 17895 全程未动")
    raise SystemExit(0 if all(results) else 1)
