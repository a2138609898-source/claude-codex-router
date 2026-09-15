"""Client-disconnect handling: a reader that hangs up mid-stream must not look like an upstream 502."""

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
CORE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE_ROOT))

from fixture_ports import reserve_port, serve_on_free_port  # noqa: E402
from sota_registry import load_registry, registry_digest  # noqa: E402

PROVIDER, MODEL = "abort_test", "gpt-5.6-sol"
MODEL_SLUG = "abort-test--" + MODEL


class SlowStream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for index in range(60):
                # Big blocks on purpose: the socket buffer has to actually fill for the
                # write to a departed client to fail, otherwise the OS swallows it silently.
                block = (f"event: chunk.{index}\ndata: " + "x" * 65536 + "\n\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(block), block))
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    def log_message(self, *_args) -> None:
        return


def write_registry(path: Path, upstream_port: int) -> None:
    data = {
        "version": 1,
        "providers": [
            {
                "id": PROVIDER,
                "name": "Abort test",
                "base_url": f"http://127.0.0.1:{upstream_port}",
                "prefix": "abort-test--",
                "enabled": True,
                "protected": False,
                "is_default": True,
                "allow_failover": False,
                "auth_type": "codex_auth",
                "auth_header": "Authorization",
                "auth_prefix": "Bearer ",
                "models_path": "/models",
                "responses_path": "/responses",
                "messages_path": "/messages",
                "timeout_seconds": 15,
                "protocols": ["responses"],
                "extra_headers": {},
                "models": [{"id": MODEL, "enabled": True}],
            }
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


temporary = tempfile.TemporaryDirectory()
work = Path(temporary.name)
reg = work / "providers.json"
auth = work / "auth.json"
log = work / "r.jsonl"

# Both ports come from the OS. Windows lets a second listener bind on top of an existing one
# (ThreadingHTTPServer sets SO_REUSEADDR), so a pinned port would let a leftover process answer
# in this run's place -- and the only symptom would be an empty log at the very end.
upstream, upstream_port = serve_on_free_port(SlowStream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
router_port = reserve_port()
write_registry(reg, upstream_port)
auth.write_text(
    json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "isolated-abort-test-token"}),
    encoding="utf-8",
)

def wait_router() -> dict | None:
    """The healthz payload once it reports ok, or None if it never did."""
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{router_port}/healthz", timeout=2) as r:
                payload = json.loads(r.read())
        except Exception:
            payload = {}
        if payload.get("status") == "ok":
            return payload
        time.sleep(0.5)
    return None


def shut_down() -> str:
    """Stop the router and the fake upstream; hand back whatever the router said on stderr."""
    proc.terminate()
    try:
        _out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        _out, err = proc.communicate()
    upstream.shutdown()
    upstream.server_close()
    return err or ""


proc = subprocess.Popen(
    [sys.executable, str(CORE_ROOT / "codex_sota_router.py"), "--host", "127.0.0.1",
     "--port", str(router_port), "--registry", str(reg), "--auth", str(auth),
     "--pid-file", str(work / "p.pid"), "--log", str(log)],
    cwd=str(CORE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace",
)
health = wait_router()
# A router that never started leaves nothing listening, and a stranger on this port answers
# healthz just as happily. The loop used to fall through in both cases: the first walked into
# the socket below and died on a traceback, the second ended in an empty log -- a FAIL that
# says nothing about client aborts. The hash is computed the way the router computes it, from
# the same file, so it matches only the child this run started.
if health is None or health.get("registry_hash") != registry_digest(load_registry(reg)):
    reason = ("影子路由器起不来" if health is None
              else f"{router_port} 上答话的不是本次起的影子路由器，先腾出端口")
    print(f"  FAIL {reason}\n{shut_down().strip()[-600:]}")
    temporary.cleanup()
    raise SystemExit(1)

body = json.dumps({"model": MODEL_SLUG, "input": "hi", "stream": True}).encode()
request = (
    f"POST /responses HTTP/1.1\r\nHost: 127.0.0.1:{router_port}\r\n"
    f"Content-Type: application/json\r\nAccept: text/event-stream\r\n"
    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
).encode() + body

print("客户端读几个事件就直接拔线…", flush=True)
sock = socket.create_connection(("127.0.0.1", router_port), timeout=30)
sock.sendall(request)
received = b""
while b"chunk.3" not in received:
    piece = sock.recv(4096)
    if not piece:
        break
    received += piece
sock.close()          # 中途拔线
print(f"  收到 {len(received)} 字节后关闭连接", flush=True)

deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if log.exists() and '"path":"/responses"' in log.read_text(encoding="utf-8"):
        break
    time.sleep(0.1)
err = shut_down()

entries = [
    json.loads(line)
    for line in (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])
    if line.strip()
]
routed = [e for e in entries if e["path"] == "/responses"]
statuses = [e["status"] for e in routed]
print(f"\n路由器记录: {[(e['vendor'], e['status']) for e in routed]}")
# 499 when the write to the departed client actually failed, 200 when the OS quietly
# absorbed it and upstream finished anyway. Both are fine; 502 would be a lie about
# the upstream, and a traceback would mean the handler thread died.
print(f"  {'PASS' if 502 not in statuses else 'FAIL'} 没有把客户端断开谎报成上游 502")
print(f"  {'PASS' if statuses else 'FAIL'} 请求被记录了（{statuses}）")
print(f"  路由器 stderr: {(err or '(空)').strip()[-300:] or '(空)'}")
print(f"  {'PASS' if not (err or '').strip() else 'FAIL'} 处理线程没有抛栈")
passed = 502 not in statuses and bool(statuses) and not (err or "").strip()
temporary.cleanup()
raise SystemExit(0 if passed else 1)
