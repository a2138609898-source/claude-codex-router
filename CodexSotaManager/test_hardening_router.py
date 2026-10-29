"""Router hardening regressions: synthetic payloads, sockets, and credentials only."""
from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request

import CodexSotaManager  # Sets the shared core import path.
import codex_sota_router as router
from test_codex_sota_regressions import RouterHarness, provider_config, write_auth, write_registry


def sse(data: dict, event: str = "") -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return (prefix + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")


def anthropic_event(kind: str, **values) -> bytes:
    return sse({"type": kind, **values}, kind)


def decoded_events(chunks) -> list[dict]:
    result = []
    for line in b"".join(chunks).decode("utf-8").splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            result.append(json.loads(line[5:]))
    return result


def terminal(events: list[dict]) -> dict:
    return next(event["response"] for event in reversed(events)
                if event.get("type") in {"response.completed", "response.incomplete", "response.failed"})


def chat_tool_stream(parts, *, finish="tool_calls", done=True) -> bytes:
    data = b""
    for index, part in enumerate(parts):
        call = {"index": 0, "function": {"arguments": part}}
        if index == 0:
            call.update(id="call_test", type="function")
            call["function"]["name"] = "patch"
        data += sse({"choices": [{"delta": {"tool_calls": [call]}}]})
    if finish is not None:
        data += sse({"choices": [{"delta": {}, "finish_reason": finish}]})
    return data + (b"data: [DONE]\n\n" if done else b"")


def anthropic_tool_stream(parts, *, stop="tool_use", initial=None, close=True, done=True) -> bytes:
    data = anthropic_event("message_start", message={"id": "msg_fixture", "usage": {}})
    data += anthropic_event("content_block_start", index=0, content_block={
        "type": "tool_use", "id": "tool_test", "name": "patch",
        "input": {} if initial is None else initial,
    })
    for part in parts:
        data += anthropic_event("content_block_delta", index=0,
                                delta={"type": "input_json_delta", "partial_json": part})
    if close:
        data += anthropic_event("content_block_stop", index=0)
    if stop is not None:
        data += anthropic_event("message_delta", delta={"stop_reason": stop}, usage={})
    if done:
        data += anthropic_event("message_stop")
    return data


class ContentCompatibilityTests(unittest.TestCase):
    def test_chat_keeps_images_in_order_and_does_not_modify_input(self):
        source = {"input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "before"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "high"},
            {"type": "input_text", "text": "after"},
            {"type": "input_image", "image_url": "https://image.invalid/example.png"},
        ]}]}
        original = deepcopy(source)
        content = router.responses_to_chat_payload(source, "fixture")["messages"][0]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url", "text", "image_url"])
        self.assertEqual(content[1]["image_url"], {"url": "data:image/png;base64,AA==", "detail": "high"})
        self.assertEqual(source, original)

    def test_chat_untyped_messages_keep_images(self):
        content = [{"type": "input_image", "image_url": {"url": "https://image.invalid/a", "detail": "low"}}]
        result = router.responses_to_chat_payload({"input": [{"role": "user", "content": content}]}, "fixture")
        self.assertEqual(result["messages"][0]["content"][0]["image_url"]["detail"], "low")

    def test_chat_text_only_request_keeps_legacy_string_shape(self):
        result = router.responses_to_chat_payload({"input": [{"type": "message", "content": [
            {"type": "input_text", "text": "a"}, {"type": "input_text", "text": "b"},
        ]}]}, "fixture")
        self.assertEqual(result["messages"], [{"role": "user", "content": "ab"}])

    def test_anthropic_tool_result_keeps_real_image_blocks(self):
        source = {"input": [{"type": "function_call_output", "call_id": "call_a", "is_error": False,
                             "output": [{"type": "input_text", "text": "screenshot"},
                                        {"type": "input_image", "image_url": "data:image/png;base64,AA=="}]}]}
        result = router.responses_to_anthropic_payload(source, "fixture")
        block = result["messages"][0]["content"][0]
        self.assertEqual(block["tool_use_id"], "call_a")
        self.assertEqual(block["content"][1], {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "AA==",
        }})
        self.assertFalse(block["is_error"])

    def test_anthropic_native_tool_result_image_survives_normalization(self):
        image = {"type": "image", "source": {"type": "url", "url": "https://image.invalid/a"}}
        result = router._anthropic_content_blocks([{"type": "tool_result", "tool_use_id": "c",
                                                   "content": [image], "is_error": True}])
        self.assertEqual(result[0]["content"], [image])
        self.assertTrue(result[0]["is_error"])

    def test_arbitrary_tool_result_json_is_not_discarded(self):
        result = router._anthropic_content_blocks([{"type": "tool_result", "tool_use_id": "c",
                                                   "content": {"count": 5, "ok": True}}])
        self.assertEqual(json.loads(result[0]["content"]), {"count": 5, "ok": True})

    def test_nonstream_length_stop_is_not_promoted_to_tool_use(self):
        result = router.chat_completion_to_anthropic_message({"choices": [{"finish_reason": "length",
            "message": {"tool_calls": [{"id": "c", "function": {"name": "patch", "arguments": '{"partial":'}}]}}]})
        self.assertEqual(result["stop_reason"], "max_tokens")

    def test_nonstandard_normal_tool_stop_remains_compatible(self):
        result = router.chat_completion_to_anthropic_message({"choices": [{"finish_reason": "stop",
            "message": {"tool_calls": [{"id": "c", "function": {"name": "patch", "arguments": "{}"}}]}}]})
        self.assertEqual(result["stop_reason"], "tool_use")


class StreamToolIntegrityTests(unittest.TestCase):
    def test_chat_custom_json_can_split_at_every_character_boundary(self):
        expected = '*** Begin Patch\n汉字 "quoted" \\path\n*** End Patch'
        arguments = json.dumps({"input": expected}, ensure_ascii=True)
        for split in range(1, len(arguments)):
            with self.subTest(split=split):
                data = chat_tool_stream([arguments[:split], arguments[split:]])
                events = decoded_events(router.chat_sse_to_responses(BytesIO(data), "fixture", tool_kinds={"patch": "custom"}))
                self.assertEqual(terminal(events)["output"][0]["input"], expected)
                self.assertEqual("".join(e["delta"] for e in events if e.get("type") == "response.custom_tool_call_input.delta"), expected)

    def test_anthropic_custom_json_can_split_at_every_character_boundary(self):
        expected = 'echo "a\\b"\n汉字'
        arguments = json.dumps({"input": expected}, ensure_ascii=True)
        for split in range(1, len(arguments)):
            with self.subTest(split=split):
                data = anthropic_tool_stream([arguments[:split], arguments[split:]])
                events = decoded_events(router.anthropic_sse_to_responses(BytesIO(data), "fixture", tool_kinds={"patch": "custom"}))
                self.assertEqual(terminal(events)["output"][0]["input"], expected)
                self.assertEqual("".join(e["delta"] for e in events if e.get("type") == "response.custom_tool_call_input.delta"), expected)

    def test_anthropic_custom_initial_input_without_deltas(self):
        data = anthropic_tool_stream([], initial={"input": "complete"})
        events = decoded_events(router.anthropic_sse_to_responses(BytesIO(data), "fixture", tool_kinds={"patch": "custom"}))
        self.assertEqual(terminal(events)["output"][0]["input"], "complete")

    def test_empty_custom_text_is_valid(self):
        for convert, data in ((router.chat_sse_to_responses, chat_tool_stream(['{"input":""}'])),
                              (router.anthropic_sse_to_responses, anthropic_tool_stream(['{"input":""}']))):
            with self.subTest(convert=convert.__name__):
                events = decoded_events(convert(BytesIO(data), "fixture", tool_kinds={"patch": "custom"}))
                self.assertEqual(terminal(events)["status"], "completed")
                self.assertEqual(terminal(events)["output"][0]["input"], "")

    def test_incomplete_custom_wrapper_fails_instead_of_executing(self):
        for convert, data in ((router.chat_sse_to_responses, chat_tool_stream(['{"input":"half'])),
                              (router.anthropic_sse_to_responses, anthropic_tool_stream(['{"input":"half']))):
            with self.subTest(convert=convert.__name__):
                outcome = {}
                events = decoded_events(convert(BytesIO(data), "fixture", tool_kinds={"patch": "custom"}, outcome=outcome))
                self.assertTrue(outcome.get("failed"))
                self.assertEqual(terminal(events)["status"], "failed")
                self.assertFalse(any(e.get("type") == "response.custom_tool_call_input.done" for e in events))

    def test_chat_eof_does_not_complete_function_or_custom_tool(self):
        for kind in ("function", "custom"):
            with self.subTest(kind=kind):
                outcome = {}
                events = decoded_events(router.chat_sse_to_responses(BytesIO(chat_tool_stream(['{"input":"x'], finish=None, done=False)),
                                         "fixture", tool_kinds={"patch": kind}, outcome=outcome))
                self.assertTrue(outcome.get("truncated"))
                self.assertEqual(terminal(events)["status"], "failed")
                self.assertFalse(any(e.get("type", "").endswith(".done") for e in events))

    def test_anthropic_block_stop_without_message_stop_does_not_complete_tool(self):
        for kind in ("function", "custom"):
            data = anthropic_tool_stream(['{"input":"x"}'], done=False)
            events = decoded_events(router.anthropic_sse_to_responses(BytesIO(data), "fixture", tool_kinds={"patch": kind}))
            self.assertEqual(terminal(events)["status"], "failed")
            self.assertFalse(any(e.get("type", "").endswith(".done") for e in events))

    def test_output_limit_does_not_publish_executable_tool_done(self):
        for convert, data in ((router.chat_sse_to_responses, chat_tool_stream(['{"input":"x'], finish="length")),
                              (router.anthropic_sse_to_responses, anthropic_tool_stream(['{"input":"x'], stop="max_tokens"))):
            for kind in ("function", "custom"):
                with self.subTest(convert=convert.__name__, kind=kind):
                    events = decoded_events(convert(BytesIO(data), "fixture", tool_kinds={"patch": kind}))
                    response = terminal(events)
                    self.assertEqual(response["status"], "incomplete")
                    self.assertEqual(response["output"][0]["status"], "incomplete")
                    self.assertFalse(any(e.get("type") in {"response.function_call_arguments.done", "response.custom_tool_call_input.done"} for e in events))

    def test_successful_function_argument_stream_is_unchanged(self):
        for convert, data in ((router.chat_sse_to_responses, chat_tool_stream(['{"a":', '1}'])),
                              (router.anthropic_sse_to_responses, anthropic_tool_stream(['{"a":', '1}']))):
            events = decoded_events(convert(BytesIO(data), "fixture"))
            self.assertEqual(terminal(events)["output"][0]["arguments"], '{"a":1}')
            self.assertEqual("".join(e["delta"] for e in events if e.get("type") == "response.function_call_arguments.delta"), '{"a":1}')

    def test_missing_anthropic_tool_block_stop_fails(self):
        events = decoded_events(router.anthropic_sse_to_responses(BytesIO(anthropic_tool_stream(['{}'], close=False)), "fixture"))
        self.assertEqual(terminal(events)["status"], "failed")


class StreamErrorAndTextTests(unittest.TestCase):
    def test_chat_error_followed_by_done_is_not_a_success(self):
        data = sse({"error": {"code": "overloaded", "message": "fixture overloaded"}}) + b"data: [DONE]\n\n"
        for convert in (router.chat_sse_to_responses, router.chat_sse_to_anthropic_sse):
            outcome = {}
            events = decoded_events(convert(BytesIO(data), "fixture", outcome=outcome))
            self.assertTrue(outcome.get("failed"))
            self.assertFalse(any(e.get("type") in {"response.completed", "message_stop"} for e in events))

    def test_error_after_content_still_terminates_as_failure(self):
        data = sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": "stop"}]})
        data += sse({"error": {"message": "late failure"}}) + b"data: [DONE]\n\n"
        events = decoded_events(router.chat_sse_to_responses(BytesIO(data), "fixture"))
        self.assertEqual(terminal(events)["status"], "failed")
        self.assertFalse(any(e.get("type", "").endswith(".done") for e in events))

    def test_anthropic_error_cannot_be_overridden_by_message_stop(self):
        data = anthropic_event("error", error={"type": "api_error", "message": "fixture error"})
        data += anthropic_event("message_stop")
        outcome = {}
        events = decoded_events(router.anthropic_sse_to_responses(BytesIO(data), "fixture", outcome=outcome))
        self.assertEqual(terminal(events)["status"], "failed")
        self.assertTrue(outcome.get("failed"))

    def test_chat_output_text_excludes_reasoning_but_keeps_reasoning_events(self):
        data = sse({"choices": [{"delta": {"reasoning_content": "thinking"}}]})
        data += sse({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]})
        data += b"data: [DONE]\n\n"
        events = decoded_events(router.chat_sse_to_responses(BytesIO(data), "fixture"))
        self.assertEqual(terminal(events)["output_text"], "answer")
        self.assertTrue(any(e.get("type") == "response.reasoning_summary_text.delta" for e in events))

    def test_anthropic_multiple_text_blocks_are_unique_and_exclude_reasoning(self):
        data = anthropic_event("message_start", message={"id": "msg_test", "usage": {}})
        for index, kind, value in ((0, "thinking", "thought"), (1, "text", "one"), (2, "text", "two")):
            data += anthropic_event("content_block_start", index=index, content_block={"type": kind})
            data += anthropic_event("content_block_delta", index=index,
                                    delta={"type": kind + "_delta", kind: value})
            data += anthropic_event("content_block_stop", index=index)
        data += anthropic_event("message_stop")
        response = terminal(decoded_events(router.anthropic_sse_to_responses(BytesIO(data), "fixture")))
        self.assertEqual(response["output_text"], "onetwo")
        self.assertEqual(len({item["id"] for item in response["output"]}), 3)
        self.assertEqual(response["output"][0]["summary"][0]["text"], "thought")

    def test_empty_text_block_does_not_steal_a_tool_output_index(self):
        data = anthropic_event("content_block_start", index=7, content_block={"type": "text", "text": ""})
        data += anthropic_tool_stream(['{}'], done=False)
        data += anthropic_event("content_block_stop", index=7)
        data += anthropic_event("message_stop")
        events = decoded_events(router.anthropic_sse_to_responses(BytesIO(data), "fixture"))
        self.assertEqual(len(terminal(events)["output"]), 1)
        self.assertEqual(sum(e.get("type") == "response.output_item.done" for e in events), 1)

    def test_initial_anthropic_text_is_not_lost(self):
        data = anthropic_event("content_block_start", index=3, content_block={"type": "text", "text": "initial"})
        data += anthropic_event("content_block_stop", index=3) + anthropic_event("message_stop")
        response = terminal(decoded_events(router.anthropic_sse_to_responses(BytesIO(data), "fixture")))
        self.assertEqual(response["output_text"], "initial")


class TransportSafetyTests(unittest.TestCase):
    @staticmethod
    def handler(path="/responses"):
        handler = object.__new__(router.SotaRouterHandler)
        handler.path, handler.command, handler.headers = path, "POST", {}
        handler.server = SimpleNamespace(router_state=mock.Mock())
        return handler

    @staticmethod
    def provider():
        return provider_config("fixture", "https://gateway.invalid", prefix="fixture--", is_default=True)

    def test_billable_dns_then_http_failure_stops_before_third_attempt(self):
        handler = self.handler()
        failed, success = mock.Mock(), mock.Mock()
        handler._open_upstream = mock.Mock(side_effect=[(None, 502, "DNS", True), (failed, 502, "HTTP 502", False), (success, 200, "", False)])
        with mock.patch.object(router.time, "sleep"):
            result = handler._attempt_upstream(self.provider(), "fixture-key", b"{}", "fixture", "model", True, allow_retry=False)
        self.assertIs(result[0], failed)
        self.assertEqual(handler._open_upstream.call_count, 2)
        failed.close.assert_not_called()

    def test_billable_override_cannot_enable_http_replay(self):
        handler = self.handler()
        handler._open_upstream = mock.Mock(return_value=(mock.Mock(), 502, "HTTP 502", False))
        handler._attempt_upstream(self.provider(), "fixture-key", b"{}", "fixture", "model", True, allow_retry=True)
        self.assertEqual(handler._open_upstream.call_count, 1)

    def test_metadata_retries_remain_available(self):
        handler = self.handler("/messages/count_tokens")
        failed, success = mock.Mock(), mock.Mock()
        handler._open_upstream = mock.Mock(side_effect=[(None, 502, "DNS", True), (failed, 502, "HTTP 502", False), (success, 200, "", False)])
        with mock.patch.object(router.time, "sleep"):
            result = handler._attempt_upstream(self.provider(), "fixture-key", b"{}", "fixture", "model", True, allow_retry=True)
        self.assertIs(result[0], success)
        self.assertEqual(handler._open_upstream.call_count, 3)
        failed.close.assert_called_once()

    def test_fast_tls_error_is_not_proof_of_a_never_sent_request(self):
        handler = self.handler()
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLError("fixture TLS failure"))
        with mock.patch.object(router.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(handler._open_upstream(self.provider(), "fixture-key", b"{}")[3])

    def test_dns_and_connection_refusal_remain_safe_to_retry(self):
        for error in (socket.gaierror("fixture DNS"), ConnectionRefusedError("fixture refused")):
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.URLError(error)
            with mock.patch.object(router.urllib.request, "build_opener", return_value=opener):
                self.assertTrue(self.handler()._open_upstream(self.provider(), "fixture-key", b"{}")[3])

    def test_inbound_identity_headers_are_replaced_not_forwarded(self):
        handler = self.handler()
        handler.headers = {"Authorization": "Bearer inbound", "X-API-Key": "inbound", "api-key": "inbound",
                           "Cookie": "session=private", "Cookie2": "private", "X-Goog-Api-Key": "inbound",
                           "X-Fixture-Option": "keep"}
        opener = mock.Mock()
        opener.open.return_value.status = 200
        with mock.patch.object(router.urllib.request, "build_opener", return_value=opener):
            handler._open_upstream(self.provider(), "fixture-vendor-key", b"{}")
        headers = {key.lower(): value for key, value in opener.open.call_args.args[0].header_items()}
        self.assertEqual(headers["authorization"], "Bearer fixture-vendor-key")
        self.assertEqual(headers["x-fixture-option"], "keep")
        self.assertFalse(set(headers) & {"x-api-key", "api-key", "cookie", "cookie2", "x-goog-api-key"})

    def test_explicit_provider_api_key_header_still_works(self):
        handler = self.handler()
        handler.headers = {"x-api-key": "inbound"}
        provider = self.provider()
        provider.update(auth_header="x-api-key", auth_prefix="")
        opener = mock.Mock()
        opener.open.return_value.status = 200
        with mock.patch.object(router.urllib.request, "build_opener", return_value=opener):
            handler._open_upstream(provider, "vendor-key", b"{}")
        headers = {key.lower(): value for key, value in opener.open.call_args.args[0].header_items()}
        self.assertEqual(headers["x-api-key"], "vendor-key")

    def test_backlog_is_set_before_server_activation(self):
        seen = []
        with mock.patch.object(router.SotaThreadingHTTPServer, "server_activate", lambda server: seen.append(server.request_queue_size)):
            server = router.SotaThreadingHTTPServer(("127.0.0.1", 0), router.SotaRouterHandler)
            server.server_close()
        self.assertEqual(seen, [128])

    def test_empty_billable_stream_is_not_replayed_in_any_adapter(self):
        requests = []
        class EmptyUpstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), EmptyUpstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for adapter in ("", router.JUNO_ADAPTER, router.CHAT_COMPLETIONS_ADAPTER, router.MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER):
                with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    messages = adapter == router.MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER
                    provider = provider_config("fixture", f"http://127.0.0.1:{server.server_port}", prefix="fixture--",
                                               is_default=True, protocols=("messages",) if messages else ("responses",))
                    provider["request_adapter"] = adapter
                    write_registry(root / "providers.json", [provider])
                    write_auth(root / "auth.json")
                    before = len(requests)
                    with RouterHarness(root / "providers.json", root / "auth.json", root / "router.log") as local:
                        payload = {"model": "fixture--shared-model", "stream": True, "input": "test"}
                        if messages:
                            payload.update(messages=[{"role": "user", "content": "test"}], max_tokens=20)
                        request = urllib.request.Request(local.url + ("/messages" if messages else "/responses"),
                                                         data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                        try:
                            response = urllib.request.urlopen(request, timeout=3)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            output = response.read()
                    self.assertEqual(len(requests) - before, 1)
                    self.assertNotIn(b"response.completed", output)
                    self.assertNotIn(b"message_stop", output)
                    self.assertIn(b"error", output)
                    if adapter:
                        self.assertIn(b"event: error" if messages else b"event: response.failed", output)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
