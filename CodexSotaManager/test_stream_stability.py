"""Stream boundaries and latency regressions without paid upstream calls."""
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import time
import unittest
from unittest import mock
import urllib.request

import CodexSotaManager  # Sets the shared core import path.
import codex_sota_router as router
from test_codex_sota_regressions import RouterHarness, provider_config, write_auth, write_registry


def frame(kind, **extra):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **extra}) + "\n\n").encode()


class HeldOpenStream(BytesIO):
    headers = {"Content-Type": "text/event-stream"}

    def readline(self, *args):
        line = super().readline(*args)
        if not line:
            raise TimeoutError("upstream kept HTTP open after terminal event")
        return line


class StreamStabilityTests(unittest.TestCase):
    def test_real_chunked_relay_flushes_delta_and_finishes_before_upstream_closes(self):
        allow_terminal = threading.Event()
        allow_close = threading.Event()

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def send(data):
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()

                try:
                    send(frame("response.output_text.delta", delta="first token"))
                    if not allow_terminal.wait(3):
                        return
                    send(frame("response.completed"))
                    allow_close.wait(3)
                finally:
                    self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_auth(root / "auth.json")
                write_registry(root / "providers.json", [provider_config(
                    "vendor", f"http://127.0.0.1:{server.server_port}",
                    prefix="vendor--", is_default=True,
                )])
                with RouterHarness(root / "providers.json", root / "auth.json", root / "router.log") as local:
                    request = urllib.request.Request(local.url + "/responses", data=json.dumps(
                        {"model": "vendor--shared-model", "stream": True, "input": "test"}
                    ).encode(), headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertIn(b"response.output_text.delta", response.readline())
                        self.assertIn(b"first token", response.readline())
                        self.assertEqual(response.readline(), b"\n")
                        allow_terminal.set()
                        self.assertIn(b"response.completed", response.read())
                        self.assertFalse(allow_close.is_set())
        finally:
            allow_terminal.set()
            allow_close.set()
            server.shutdown()
            server.server_close()
            thread.join(3)

    def relay(self, stream):
        handler = object.__new__(router.SotaRouterHandler)
        handler.command, handler.path = "POST", "/responses"
        handler.wfile = BytesIO()
        handler.server = SimpleNamespace(router_state=mock.Mock())
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        router.SotaRouterHandler._relay(
            handler, stream, "test-vendor", 200, time.monotonic(), "test-model",
            request_payload={"stream": True}, is_final_attempt=True,
        )
        return handler.wfile.getvalue(), handler.state.record.call_args

    def test_success_does_not_wait_for_http_eof(self):
        source = frame("response.created", response={"id": "resp_same"}) + frame("response.completed")
        output, record = self.relay(HeldOpenStream(source))
        self.assertEqual(output, source)
        self.assertEqual(record.args[3], 200)

    def test_upstream_failure_and_incomplete_are_not_duplicated(self):
        for kind in ("response.failed", "response.incomplete", "error"):
            with self.subTest(kind=kind):
                source = frame(kind)
                output, _ = self.relay(HeldOpenStream(source))
                self.assertEqual(output, source)

    def test_completion_words_in_answer_do_not_hide_truncation(self):
        source = frame("response.created", response={"id": "resp_original"})
        source += frame("response.output_text.delta", delta="response.completed message_stop [DONE]")
        output, record = self.relay(self.sse(source))
        self.assertEqual(record.args[3], 502)
        self.assertIn(b'"id":"resp_original"', output)
        self.assertEqual(output.count(b"event: response.failed"), 1)

    @staticmethod
    def sse(data):
        stream = BytesIO(data)
        stream.headers = {"Content-Type": "text/event-stream"}
        return stream

    def test_partial_event_does_not_count_as_completion(self):
        progress = router.SSEProgress()
        progress.feed(b"event: response.completed\n")
        self.assertFalse(progress.terminal)
        progress.feed(b'data: {"type":"response.completed"}\n')
        self.assertFalse(progress.terminal)
        progress.feed(b"\n")
        self.assertEqual(progress.terminal, "response.completed")

    def test_chat_adapters_finish_at_done_including_usage(self):
        data = b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n'
        data += b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
        data += b'data: [DONE]\n\n'
        for adapter in (router.chat_sse_to_responses, router.chat_sse_to_anthropic_sse):
            with self.subTest(adapter=adapter.__name__):
                output = b"".join(adapter(HeldOpenStream(data), "model"))
                self.assertIn(b"hello", output)
                self.assertNotIn(b"response.failed", output)
                self.assertIn(b'"input_tokens":4', output)

    def test_anthropic_adapter_finishes_at_message_stop(self):
        data = frame("message_start", message={"id": "msg_test", "usage": {}})
        data += frame("message_stop")
        output = b"".join(router.anthropic_sse_to_responses(HeldOpenStream(data), "model"))
        self.assertIn(b"event: response.completed", output)


if __name__ == "__main__":
    unittest.main()
