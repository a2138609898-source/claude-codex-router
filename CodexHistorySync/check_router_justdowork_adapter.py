"""Offline end-to-end checks for the juno Responses -> Messages adapter.

The fake upstream speaks Anthropic Messages on a loopback port.  A shadow SOTA router uses a
throwaway registry and auth file, so this check never touches the live listener, credentials, or
the real juno endpoint.
"""

from __future__ import annotations

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
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fixture_ports import reserve_port, serve_on_free_port  # noqa: E402
from sota_registry import registry_digest, load_registry  # noqa: E402


MODEL = "juno--gpt-5.6-sol"
UPSTREAM_MODEL = "gpt-5.6-sol"
TOOL = {
    "type": "namespace",
    "name": "codex_app",
    "tools": [
        {
            "type": "function",
            "name": "edit_file",
            "description": "Edit one file",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                "required": ["path", "text"],
            },
        }
    ],
}
received: list[dict] = []


class FakeAnthropic(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            body = {}
        received.append(
            {
                "path": self.path,
                "body": body,
                "anthropic_version": self.headers.get("anthropic-version"),
                "authorization": self.headers.get("Authorization"),
                "user_agent": self.headers.get("User-Agent"),
                "originator": self.headers.get("originator"),
            }
        )
        stream = bool(body.get("stream"))
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        has_tool_result = any(
            isinstance(message, dict)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in (message.get("content") or [])
            )
            for message in messages
        )
        has_tools = bool(body.get("tools"))
        if has_tools and not has_tool_result:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_edit_1",
                    "name": "edit_file",
                    "input": {"path": "a.txt", "text": "updated"},
                }
            ]
            stop_reason = "tool_use"
        else:
            content = [{"type": "text", "text": "done" if has_tool_result else "hello"}]
            stop_reason = "end_turn"
        if stream:
            self._send_stream(content, stop_reason)
            return
        payload = {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": content,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_stream(self, content: list[dict], stop_reason: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event: str, data: dict) -> None:
            raw = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
            self.wfile.write(raw)
            self.wfile.flush()

        emit(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stream",
                    "type": "message",
                    "role": "assistant",
                    "model": UPSTREAM_MODEL,
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
            },
        )
        block = content[0]
        if block["type"] == "text":
            emit("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block})
            emit(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": block["text"]}},
            )
        else:
            emit("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"]}})
            emit(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], separators=(",", ":"))}},
            )
        emit("content_block_stop", {"type": "content_block_stop", "index": 0})
        emit("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 7}})
        emit("message_stop", {"type": "message_stop"})

    def log_message(self, *_args) -> None:
        return


def write_registry(path: Path, upstream_port: int) -> None:
    registry = {
        "version": 1,
        "providers": [
            {
                "id": "juno",
                "name": "juno fixture",
                "workspace": "codex",
                "enabled": True,
                "protected": False,
                "is_default": True,
                "allow_failover": False,
                "auth_type": "codex_auth",
                "base_url": f"http://127.0.0.1:{upstream_port}",
                "prefix": "juno--",
                "models_path": "/v1/models",
                "responses_path": "/responses",
                "messages_path": "/v1/messages",
                "protocols": ["responses"],
                "timeout_seconds": 10,
                "extra_headers": {},
                "request_adapter": "responses_to_anthropic_messages",
                "models": [{"id": UPSTREAM_MODEL, "enabled": True}],
            }
        ],
    }
    path.write_text(json.dumps(registry), encoding="utf-8")


def start_router(registry: Path, auth: Path, port: int, work: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "codex_sota_router.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--registry",
            str(registry),
            "--auth",
            str(auth),
            "--pid-file",
            str(work / "router.pid"),
            "--log",
            str(work / "router.jsonl"),
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def wait_ready(port: int, registry: Path) -> bool:
    expected = registry_digest(load_registry(registry))
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                body = json.loads(response.read())
                if body.get("status") == "ok" and body.get("registry_hash") == expected:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def ask(port: int, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/responses",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": "Bearer client", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.status, error.read()


def check(label: str, condition: bool) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'} {label}", flush=True)
    return condition


def main() -> int:
    upstream, upstream_port = serve_on_free_port(FakeAnthropic)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    router_port = reserve_port()
    results: list[bool] = []
    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        registry = work / "providers.json"
        auth = work / "auth.json"
        auth.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "fixture-key"}), encoding="utf-8")
        write_registry(registry, upstream_port)
        process = start_router(registry, auth, router_port, work)
        try:
            results.append(check("shadow router starts", wait_ready(router_port, registry)))

            status, raw_body = ask(router_port, {"model": MODEL, "input": "hi", "stream": False})
            body = json.loads(raw_body)
            request = received[-1]
            results.append(check("non-stream text is converted", status == 200 and body.get("output_text") == "hello"))
            results.append(check("Messages endpoint and required headers are used", request["path"] == "/v1/messages" and request["anthropic_version"] == "2023-06-01" and request["authorization"] == "Bearer fixture-key"))
            results.append(check("upstream receives the bare model id", request["body"].get("model") == UPSTREAM_MODEL))

            status, raw_body = ask(router_port, {"model": MODEL, "input": "edit", "tools": [TOOL], "stream": False})
            body = json.loads(raw_body)
            sent_tool = received[-1]["body"].get("tools", [{}])[0]
            call = (body.get("output") or [{}])[0]
            results.append(check("inputSchema survives the conversion", status == 200 and sent_tool.get("input_schema") == TOOL["tools"][0]["inputSchema"]))
            results.append(check("tool_use becomes a namespaced function_call", call.get("type") == "function_call" and call.get("name") == "edit_file" and call.get("namespace") == "codex_app" and call.get("call_id") == "toolu_edit_1"))

            status, raw_body = ask(
                router_port,
                {
                    "model": MODEL,
                    "input": [
                        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
                        {"type": "function_call", "call_id": "toolu_edit_1", "name": "edit_file", "arguments": "{\"path\":\"a.txt\",\"text\":\"updated\"}"},
                        {"type": "function_call_output", "call_id": "toolu_edit_1", "output": "ok"},
                    ],
                    "tools": [TOOL],
                    "stream": False,
                },
            )
            body = json.loads(raw_body)
            sent_messages = received[-1]["body"].get("messages") or []
            results.append(check("tool result round-trip remains usable", status == 200 and body.get("output_text") == "done" and any(any(b.get("type") == "tool_use" for b in (m.get("content") or [])) for m in sent_messages if m.get("role") == "assistant") and any(any(b.get("type") == "tool_result" for b in (m.get("content") or [])) for m in sent_messages if m.get("role") == "user")))

            status, stream_body = ask(router_port, {"model": MODEL, "input": "stream", "stream": True})
            stream_text = stream_body.decode("utf-8", "replace")
            results.append(check("streaming text is translated to Responses events", status == 200 and "response.output_text.delta" in stream_text and '"delta":"hello"' in stream_text and "response.completed" in stream_text))

            status, stream_body = ask(router_port, {"model": MODEL, "input": "stream tool", "tools": [TOOL], "stream": True})
            stream_text = stream_body.decode("utf-8", "replace")
            results.append(check("streaming tool call carries namespace and arguments", status == 200 and '"type":"function_call"' in stream_text and '"namespace":"codex_app"' in stream_text and "response.function_call_arguments.delta" in stream_text and "toolu_edit_1" in stream_text))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            upstream.shutdown()
    print(f"\n{sum(results)}/{len(results)} checks passed; live router and vendors were not touched")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
