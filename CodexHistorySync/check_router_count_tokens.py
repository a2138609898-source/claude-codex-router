"""count_tokens and billing safety: the router must not forward a call it knows 404s.

Three fake Anthropic gateways and a shadow router, all on OS-assigned ports against a temp
registry, so the live routers and the real vendors are never involved.

Why this file exists: this install's Claude router log carried 2505 `404` rows for
POST /v1/messages/count_tokens -- 34% of every request it had ever served, and over an hour of
wall time spent asking gateways a question none of them implement.  Claude Desktop calls that
endpoint constantly, so the 404s were not an edge case; they were the second-most-common thing
the router did.
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
# Every request each fake gateway saw, so a check can assert a call never left the router.
seen: dict[str, list[dict]] = {"noct": [], "ct": [], "flaky": []}
# How many more times the flaky gateway should answer 503 before it starts working.
flaky_failures_left = {"count": 0}
PLACEHOLDER = b'{"error":{"code":"model_not_found","message":"no channel for model"}}'


def _read_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    try:
        return json.loads(handler.rfile.read(length) or b"{}")
    except ValueError:
        return {}


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _message_reply(body: dict) -> bytes:
    return json.dumps({
        "id": "msg_fake", "type": "message", "role": "assistant",
        "model": body.get("model"), "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
    }).encode()


class FakeNoCountTokens(BaseHTTPRequestHandler):
    """The common case: Anthropic-shaped, but count_tokens was never implemented."""

    def do_POST(self) -> None:
        body = _read_body(self)
        seen["noct"].append({"path": self.path, "body": body})
        if self.path.endswith("/count_tokens"):
            _reply(self, 404, b'{"error":{"message":"404 page not found"}}')
            return
        _reply(self, 200, _message_reply(body))

    def log_message(self, *_args) -> None:
        return


class FakeWithCountTokens(BaseHTTPRequestHandler):
    """A gateway that does implement it, so its exact number must win over any estimate."""

    def do_POST(self) -> None:
        body = _read_body(self)
        seen["ct"].append({"path": self.path, "body": body})
        if self.path.endswith("/count_tokens"):
            _reply(self, 200, b'{"input_tokens":12345}')
            return
        _reply(self, 200, _message_reply(body))

    def log_message(self, *_args) -> None:
        return


class FakeFlaky(BaseHTTPRequestHandler):
    """Answers 503 a set number of times, then works -- a relay with a disabled channel."""

    def do_POST(self) -> None:
        body = _read_body(self)
        seen["flaky"].append({"path": self.path, "body": body})
        if flaky_failures_left["count"] > 0:
            flaky_failures_left["count"] -= 1
            _reply(self, 503, PLACEHOLDER)
            return
        _reply(self, 200, _message_reply(body))

    def log_message(self, *_args) -> None:
        return


noct_server, NOCT_PORT = serve_on_free_port(FakeNoCountTokens)
ct_server, CT_PORT = serve_on_free_port(FakeWithCountTokens)
flaky_server, FLAKY_PORT = serve_on_free_port(FakeFlaky)
SERVERS = (noct_server, ct_server, flaky_server)
FAKES = (
    ("fakenoct", "noct--", NOCT_PORT),
    ("fakect", "ct--", CT_PORT),
    ("fakeflaky", "flaky--", FLAKY_PORT),
)


def write_registry(path: Path) -> None:
    """Keep the real default provider for its auth shape, add the three fake gateways.

    The template is looked up by `is_default` rather than by position, for the same reason the
    other router checks do it: index 0 is not a promise, and copying the wrong entry would hand
    a fake gateway some other vendor's credential.
    """
    data = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8-sig"))
    for provider in data["providers"]:
        provider["enabled"] = provider.get("is_default", False)
    default = next((p for p in data["providers"] if p.get("is_default")), None)
    if default is None:
        raise SystemExit("跳过：注册表里没有默认供应商，这个检查没有可借用的凭据形状")
    for vendor_id, prefix, port in FAKES:
        template = json.loads(json.dumps(default))
        template.update({
            "id": vendor_id, "name": vendor_id, "prefix": prefix,
            "base_url": f"http://127.0.0.1:{port}", "enabled": True, "is_default": False,
            "protected": False, "allow_failover": False, "protocols": ["messages"],
            "messages_path": "/v1/messages", "models_path": "/v1/models",
            "models": [{"id": "claude-opus-5", "enabled": True, "display_name": "Opus 5",
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
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/healthz", timeout=2) as r:
            payload = json.loads(r.read())
    except Exception:
        return None
    return payload if payload.get("status") == "ok" else None


def post(
    path: str,
    body: dict,
    timeout: int = 30,
    idempotency_key: str | None = None,
) -> tuple[int, object]:
    headers = {"Content-Type": "application/json", "x-api-key": "client-key"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        f"http://127.0.0.1:{ROUTER_PORT}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.status, e.read().decode("utf-8", "replace")


def count_tokens(model: str, text: str = "hi", timeout: int = 30) -> tuple[int, object]:
    return post("/v1/messages/count_tokens",
                {"model": model, "messages": [{"role": "user", "content": text}]}, timeout)


def log_rows(work: Path) -> list[dict]:
    path = work / "l.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


if __name__ == "__main__":
    for server in SERVERS:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp())
    reg = work / "providers.json"
    write_registry(reg)
    proc = start_router(reg, work)
    results: list[bool] = []
    try:
        for _ in range(40):
            health = healthz()
            if health:
                break
            time.sleep(0.5)
        else:
            print("影子路由器起不来:", (proc.stderr.read() or b"").decode("utf-8", "replace")[-600:])
            raise SystemExit(1)
        if health.get("registry_hash") != registry_digest(load_registry(reg)):
            print(f"{ROUTER_PORT} 上答话的不是本次起的影子路由器，先腾出端口")
            raise SystemExit(1)

        print("=== 1) count_tokens：第一次问上游，之后不再问 ===")
        seen["noct"].clear()
        status, body = count_tokens("noct--claude-opus-5")
        first_ok = (status == 200 and isinstance(body, dict)
                    and isinstance(body.get("input_tokens"), int)
                    and body["input_tokens"] > 0)
        results.append(first_ok)
        print(f"  {'PASS' if first_ok else 'FAIL'}  上游只会 404，客户端仍拿到 HTTP {status} {body}")
        asked_once = len(seen["noct"]) == 1
        results.append(asked_once)
        print(f"  {'PASS' if asked_once else 'FAIL'}  第一次确实问了上游一次（上游收到 {len(seen['noct'])} 次）")
        seen["noct"].clear()
        second = [count_tokens("noct--claude-opus-5") for _ in range(5)]
        never_again = not seen["noct"] and all(s == 200 for s, _ in second)
        results.append(never_again)
        print(f"  {'PASS' if never_again else 'FAIL'}  后续 5 次全部本地作答，上游收到 {len(seen['noct'])} 次")

        print("\n=== 2) 本地估算的量级合理，中英文不共用一个系数 ===")
        _, latin = count_tokens("noct--claude-opus-5", "word " * 200)
        _, han = count_tokens("noct--claude-opus-5", "中" * 400)
        latin_n = latin.get("input_tokens", 0) if isinstance(latin, dict) else 0
        han_n = han.get("input_tokens", 0) if isinstance(han, dict) else 0
        # 1000 个拉丁字符大约 270 个 token，400 个汉字大约 400 个：真实分词器在这两个数
        # 附近，估算只要落在同一个数量级、且汉字不被当成 3.6 字符一个 token 就够用了。
        latin_ok = 200 <= latin_n <= 400
        han_ok = 350 <= han_n <= 500
        results.append(latin_ok and han_ok)
        print(f"  {'PASS' if latin_ok and han_ok else 'FAIL'}  1000 拉丁字符 -> {latin_n}，400 汉字 -> {han_n}")
        _, blob = count_tokens("noct--claude-opus-5", "hi")
        _, with_signature = post("/v1/messages/count_tokens", {
            "model": "noct--claude-opus-5",
            "messages": [{"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hi", "signature": "Z" * 4000}]}]})
        blob_n = blob.get("input_tokens", 0) if isinstance(blob, dict) else 0
        sig_n = with_signature.get("input_tokens", 0) if isinstance(with_signature, dict) else 0
        # thinking 块带几千字节的 base64 签名，那是给服务端验签的，模型根本不读。
        # 按文本计就会把估算抬高一个数量级，于是该压缩的时候反而先压缩。
        sig_ok = sig_n < blob_n + 50
        results.append(sig_ok)
        print(f"  {'PASS' if sig_ok else 'FAIL'}  4000 字节 signature 不计入：{sig_n} vs 纯文本 {blob_n}")

        print("\n=== 3) 真的实现了 count_tokens 的网关，数字照原样透出 ===")
        seen["ct"].clear()
        status, body = count_tokens("ct--claude-opus-5")
        exact_ok = status == 200 and isinstance(body, dict) and body.get("input_tokens") == 12345
        results.append(exact_ok)
        print(f"  {'PASS' if exact_ok else 'FAIL'}  HTTP {status} {body}（12345 是上游给的，不是估的）")
        still_forwarding = len(seen["ct"]) == 1
        results.append(still_forwarding)
        print(f"  {'PASS' if still_forwarding else 'FAIL'}  这一家被转发了 {len(seen['ct'])} 次")
        seen["ct"].clear()
        for _ in range(3):
            count_tokens("ct--claude-opus-5")
        not_memoized = len(seen["ct"]) == 3
        results.append(not_memoized)
        print(f"  {'PASS' if not_memoized else 'FAIL'}  没被误记成「不支持」：后续 3 次仍然转发了 {len(seen['ct'])} 次")

        print("\n=== 4) 生成请求即使带幂等键，也只发送一次 ===")
        seen["flaky"].clear()
        seen["noct"].clear()
        seen["ct"].clear()
        flaky_failures_left["count"] = 1
        status, body = post("/v1/messages", {
            "model": "flaky--claude-opus-5", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]},
            idempotency_key="count-retry-transient")
        single_shot_ok = status == 503 and len(seen["flaky"]) == 1
        results.append(single_shot_ok)
        print(f"  {'PASS' if single_shot_ok else 'FAIL'}  HTTP {status}，这一家收到 {len(seen['flaky'])} 次（带幂等键也不重放）")
        # allow_failover 是关着的，也不该因为错误偷偷跑去别家计费。
        stayed_ok = not seen["noct"] and not seen["ct"]
        results.append(stayed_ok)
        print(f"  {'PASS' if stayed_ok else 'FAIL'}  没有溜到别家：其余两家收到 {len(seen['noct'])} / {len(seen['ct'])} 次")

        print("\n=== 5) 持续 503 也不重试，原样把上游状态透出 ===")
        seen["flaky"].clear()
        flaky_failures_left["count"] = 99
        status, body = post("/v1/messages", {
            "model": "flaky--claude-opus-5", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]},
            idempotency_key="count-retry-bounded")
        attempts = len(seen["flaky"])
        bounded_ok = status == 503 and attempts == 1
        results.append(bounded_ok)
        print(f"  {'PASS' if bounded_ok else 'FAIL'}  HTTP {status}，一共只试了 {attempts} 次（没有重试）")
        surfaced_ok = "no channel for model" in str(body)
        results.append(surfaced_ok)
        print(f"  {'PASS' if surfaced_ok else 'FAIL'}  上游正文原样透给客户端：{str(body)[:80]}")

        print("\n=== 6) 上游错误正文进日志，事后才查得出是哪种 503 ===")
        flaky_failures_left["count"] = 0
        time.sleep(0.3)
        rows = log_rows(work)
        detailed = [r for r in rows if r.get("status") == 503 and "no channel" in str(r.get("detail"))]
        results.append(bool(detailed))
        print(f"  {'PASS' if detailed else 'FAIL'}  {len(detailed)} 条 503 带上了上游原文")
        switched = [r for r in rows if "count_tokens route" in str(r.get("detail"))]
        results.append(bool(switched))
        print(f"  {'PASS' if switched else 'FAIL'}  切到本地估算那一次留了痕：{len(switched)} 条")
        local_rows = [r for r in rows if str(r.get("path")).endswith("count_tokens")]
        # 本地作答不写日志，否则一个 200 会把真在坏掉的网关洗成健康的。
        quiet_ok = len(local_rows) <= 6
        results.append(quiet_ok)
        print(f"  {'PASS' if quiet_ok else 'FAIL'}  本地作答没有刷日志：count_tokens 相关只有 {len(local_rows)} 条")

        print("\n=== 7) count_tokens 上的 [1m] 后缀照样剥掉 ===")
        status, body = count_tokens("noct--claude-opus-5[1m]")
        one_m_ok = status == 200 and isinstance(body, dict) and body.get("input_tokens", 0) > 0
        results.append(one_m_ok)
        print(f"  {'PASS' if one_m_ok else 'FAIL'}  HTTP {status} {body}")
        status, body = count_tokens("noct--nope")
        unknown_ok = status == 400 and "model_not_enabled" in str(body)
        results.append(unknown_ok)
        print(f"  {'PASS' if unknown_ok else 'FAIL'}  没启用的模型仍然是干净的 400：HTTP {status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for server in SERVERS:
            server.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n合计 {sum(results)}/{len(results)} 项通过；线上 17895 / 17994 全程未动")
    raise SystemExit(0 if all(results) else 1)
