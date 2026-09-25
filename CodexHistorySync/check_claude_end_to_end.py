"""End-to-end dry run of the Claude side, with no real vendor and no real credentials.

Seeds a synthetic Anthropic-shaped provider into a throwaway Claude workspace, starts the real
router on the Claude port against it, and drives /v1/models and /v1/messages through it. Proves
the whole chain works so that adding a real vendor is the only remaining step. The real
configLibrary is never touched — profile writing is exercised against a temp library.
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
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_desktop as cd  # noqa: E402
import sota_registry as R  # noqa: E402
from fixture_ports import reserve_port, serve_on_free_port  # noqa: E402

# Both ports come from the OS. The fixture router is not the live Claude one on 17994, and
# pinning a number only creates a way for a leftover run to answer in its place.
ROUTER_PORT = reserve_port()
results: list[bool] = []
seen: list[dict] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))


class FakeAnthropic(BaseHTTPRequestHandler):
    """Minimal Anthropic Messages upstream: records what it got, answers a valid message."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = {}
        seen.append({"path": self.path, "body": body,
                     "auth": self.headers.get("Authorization"),
                     "x_api_key": self.headers.get("x-api-key")})
        payload = json.dumps({
            "id": "msg_fake", "type": "message", "role": "assistant",
            "model": body.get("model", "?"),
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


# Bound at import so the seeded provider can point at wherever it landed.
upstream, UPSTREAM_PORT = serve_on_free_port(FakeAnthropic)


def seed_claude_workspace(ws: R.Workspace) -> None:
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.secrets_root.mkdir(parents=True, exist_ok=True)
    ws.auth_path.write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "unused-by-messages"}),
        encoding="utf-8",
    )
    shutil.copy(R.CODEX.source_catalog_path, ws.source_catalog_path)
    default = {
        "workspace": ws.name, "id": "fake_default", "name": "Fake Default",
        "base_url": f"http://127.0.0.1:{UPSTREAM_PORT}", "prefix": "",
        "enabled": True, "protected": False, "is_default": True,
        "auth_type": "codex_auth", "auth_header": "x-api-key", "auth_prefix": "",
        "models_path": "/v1/models", "responses_path": "/v1/responses",
        "messages_path": "/v1/messages", "timeout_seconds": 60, "extra_headers": {},
        "protocols": ["messages"],
        "models": [{"id": "claude-opus-5", "enabled": True, "display_name": "", "description": ""}],
    }
    ws.registry_path.write_text(
        json.dumps({"version": 1, "providers": [default]}, ensure_ascii=False), encoding="utf-8"
    )
    R.build_model_catalog(
        R.load_registry(ws.registry_path), ws.source_catalog_path, ws.catalog_path
    )


def wait_router() -> dict | None:
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/healthz", timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.5)
    return None


def post(path: str, payload: dict) -> tuple[int, dict | str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{ROUTER_PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local-gateway"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.status, json.loads(raw)
        except ValueError:
            return error.status, raw
    except Exception as error:
        return 0, f"{type(error).__name__}: {error}"


upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
upstream_thread.start()

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary) / "claude-sota"
    ws = R.Workspace(
        name="claude", label="Claude Desktop", root=root, router_port=ROUTER_PORT,
        router_starter=root / "starter.ps1", protocol="messages",
    )
    with mock.patch.dict(R.WORKSPACES, {"claude": ws}):
        print("=== 1) 用假上游种一个 Anthropic 供应商，起真实路由器 ===")
        seed_claude_workspace(ws)
        proc = subprocess.Popen(
            [sys.executable, "codex_sota_router.py", "--host", "127.0.0.1",
             "--port", str(ROUTER_PORT), "--registry", str(ws.registry_path),
             "--auth", str(ws.auth_path), "--pid-file", str(root / "r.pid"),
             "--log", str(ws.log_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=str(R.INSTALL_ROOT),
        )
        try:
            health = wait_router()
            # The hash ties the answer to this run's synthetic registry: a leftover router from
            # an earlier run would otherwise answer healthz and quietly serve its own providers.
            mine = bool(health) and health.get("registry_hash") == R.registry_digest(
                R.load_registry(ws.registry_path)
            )
            check("路由器在 Claude 端口起来了，而且答话的就是它", mine,
                  f"models={health.get('models') if health else (proc.stderr.read() or b'').decode('utf-8','replace')[-200:]}")

            print()
            print("=== 2) Claude Desktop 会去拉的 /v1/models ===")
            with urllib.request.urlopen(f"http://127.0.0.1:{ROUTER_PORT}/v1/models", timeout=10) as r:
                listed = json.loads(r.read())
            entries = listed.get("data") or listed.get("models") or []
            ids = [str(e.get("id") or e.get("slug") or e.get("name")) for e in entries]
            check("列出了 messages 协议的模型", "claude-opus-5" in ids, f"{ids}")

            print()
            print("=== 3) /v1/messages 端到端 ===")
            seen.clear()
            status, body = post("/v1/messages",
                                {"model": "claude-opus-5", "max_tokens": 16,
                                 "messages": [{"role": "user", "content": "hi"}]})
            check("客户端拿到 200", status == 200, f"HTTP {status}")
            check("响应是 Anthropic message 形状",
                  isinstance(body, dict) and body.get("type") == "message",
                  str(body)[:80])
            check("上游收到的是 /v1/messages", bool(seen) and seen[0]["path"] == "/v1/messages",
                  seen[0]["path"] if seen else "上游没收到请求")
            check("上游收到的 model 已去掉前缀", bool(seen) and seen[0]["body"].get("model") == "claude-opus-5",
                  seen[0]["body"].get("model") if seen else "-")
            # 弱断言（只看"不是客户端的 token"）会漏掉"根本没带凭据"这种情况 —— 那在真实
            # 上游就是 401。所以要确认凭据确实进了供应商配置的那个头。
            got = seen[0] if seen else {}
            check("客户端的 token 没有被转发",
                  got.get("auth") != "Bearer local-gateway",
                  f"上游看到 Authorization={got.get('auth')!r}")
            check("供应商自己的凭据进了配置的 x-api-key 头",
                  bool(got.get("x_api_key")),
                  f"x-api-key={'<有值>' if got.get('x_api_key') else '空'}")

            print()
            print("=== 4) 不存在的模型要干净地 400 ===")
            status, body = post("/v1/messages",
                                {"model": "nope--nope", "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "x"}]})
            check("未启用模型返回 400 model_not_enabled",
                  status == 400 and isinstance(body, dict)
                  and body.get("error", {}).get("type") == "model_not_enabled",
                  f"HTTP {status} {str(body)[:60]}")

            print()
            print("=== 5) 写 Claude Desktop 档（临时配置库，真实的不碰）===")
            lib = Path(temporary) / "configLibrary"
            lib.mkdir()
            (lib / "_meta.json").write_text(
                json.dumps({"appliedId": "theirs", "entries": [{"id": "theirs", "name": "CC Switch"}]}),
                encoding="utf-8")
            (lib / "theirs.json").write_text('{"inferenceProvider":"gateway"}', encoding="utf-8")
            with mock.patch.object(cd, "CONFIG_LIBRARY", lib), \
                 mock.patch.object(cd, "META_PATH", lib / "_meta.json"), \
                 mock.patch.object(cd, "CLAUDE_3P_ROOT", lib.parent), \
                 mock.patch.object(cd, "LIBRARY_LOCK_PATH", Path(temporary) / "claude.lock"), \
                 mock.patch.object(cd, "SLOT_CLAIM_PATH", Path(temporary) / "claim.json"), \
                 mock.patch.object(cd, "BACKUP_ROOT", Path(temporary) / "bk"):
                models = cd.build_inference_models(R.load_registry(ws.registry_path))
                check("从注册表生成了 inferenceModels", len(models) == 1, str(models))
                out = cd.write_profile(models, gateway_url=f"http://127.0.0.1:{ROUTER_PORT}")
                check("我的档已写入，但没抢走 ccswitch 的生效槽",
                      out["models"] == 1 and out["applied"] is False
                      and cd.library_status()["applied_id"] == "theirs",
                      f"applied={out['applied']} 生效档={cd.library_status()['applied_name']}")
                meta = json.loads((lib / "_meta.json").read_text(encoding="utf-8"))
                check("ccswitch 的档还在库里",
                      any(e["id"] == "theirs" for e in meta["entries"]),
                      f"entries={[e['name'] for e in meta['entries']]}")
                check("ccswitch 的档文件一个字节没动",
                      (lib / "theirs.json").read_text(encoding="utf-8") == '{"inferenceProvider":"gateway"}')

                print()
                print("=== 6) 只有启动才借生效槽，退出后归还 ===")
                claimed = cd.claim_slot(pids=[4242])
                check("启动时借到了生效槽，并记下前任是 ccswitch",
                      cd.library_status()["applied_id"] == cd.SOTA_ENTRY_ID
                      and claimed["displaced_id"] == "theirs",
                      f"生效档={cd.library_status()['applied_name']} 前任={claimed['displaced_name']!r}")
                check("Claude 还在跑时不动生效槽",
                      cd.reconcile_slot(claude_running=True)["status"] == "claude-running"
                      and cd.library_status()["applied_id"] == cd.SOTA_ENTRY_ID)
                released = cd.reconcile_slot(claude_running=False)
                check("Claude 退出后自动交还给 ccswitch",
                      released["status"] == "released"
                      and cd.library_status()["applied_id"] == "theirs"
                      and cd.read_slot_claim() == {},
                      f"status={released['status']} 生效档={cd.library_status()['applied_name']}")
                back = cd.apply_entry("theirs")
                check("能手动切回 ccswitch", back["applied_id"] == "theirs")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

upstream.shutdown()
print()
print(f"合计 {sum(results)}/{len(results)} 项通过")
raise SystemExit(0 if all(results) else 1)
