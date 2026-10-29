"""Final router adapter regressions using only in-memory synthetic payloads."""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CodexHistorySync"))
import codex_sota_router as router


def sse(payload, event="", *, multiline=False, terminated=True):
    encoded = json.dumps(payload, ensure_ascii=False, indent=2 if multiline else None)
    frame = (f"event: {event}\n" if event else "")
    frame += "\n".join("data: " + line for line in encoded.splitlines())
    return (frame + ("\n\n" if terminated else "")).encode("utf-8")


def decoded(chunks):
    return [json.loads(line[5:]) for line in b"".join(chunks).decode("utf-8").splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


def chat_tool_frame(arguments, *, finish=None):
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_fixture",
        "type": "function", "function": {"name": "patch", "arguments": arguments}}]},
        "finish_reason": finish}]}


class MessagesJsonErrorTests(unittest.TestCase):
    def test_explicit_upstream_errors_are_not_empty_messages(self):
        for payload in ({"error": {"code": "fixture_busy", "message": "fixture overloaded"}},
                        {"type": "error", "message": "fixture overloaded"},
                        {"error": "fixture overloaded"}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "fixture overloaded"):
                router.chat_completion_to_anthropic_message(payload, "fixture")

    def test_non_object_response_is_rejected_consistently(self):
        for payload in ([], None, "fixture"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                router.chat_completion_to_anthropic_message(payload, "fixture")

    def test_valid_message_keeps_tools_thinking_and_usage_without_mutating_input(self):
        payload = {"id": "chatcmpl-fixture", "usage": {"prompt_tokens": 5, "completion_tokens": 8},
                   "choices": [{"finish_reason": "tool_calls", "message": {
                       "content": "answer", "reasoning_content": "thought", "tool_calls": [
                           {"id": "call_fixture", "function": {"name": "patch", "arguments": '{"a":1}'}}
                       ]}}]}
        original = deepcopy(payload)
        result = router.chat_completion_to_anthropic_message(payload, "fixture", thinking_enabled=True)
        self.assertEqual([block["type"] for block in result["content"]], ["thinking", "text", "tool_use"])
        self.assertEqual(result["content"][-1]["input"], {"a": 1})
        self.assertEqual(result["stop_reason"], "tool_use")
        self.assertEqual(result["usage"], {"input_tokens": 5, "output_tokens": 8})
        self.assertEqual(payload, original)

    def test_json_error_relay_returns_and_records_502_without_retry(self):
        for stream in (False, True):
            for payload in ({"error": {"message": "fixture overloaded"}}, {"type": "error", "message": "busy"}):
                with self.subTest(stream=stream, payload=payload):
                    handler = object.__new__(router.SotaRouterHandler)
                    handler.command, handler.path = "POST", "/messages"
                    handler.wfile = BytesIO()
                    handler._relay_headers_sent = False
                    handler.close_connection = False
                    handler.server = SimpleNamespace(router_state=mock.Mock())
                    handler.send_response = mock.Mock()
                    handler.send_header = mock.Mock()
                    handler.end_headers = mock.Mock()
                    upstream = BytesIO(json.dumps(payload).encode("utf-8"))
                    upstream.headers = {"Content-Type": "application/json"}
                    retry = handler._relay_messages_chat(upstream, "fixture", 200, time.monotonic(),
                                                         "fixture", {"stream": stream}, is_final_attempt=True)
                    self.assertFalse(retry)
                    handler.send_response.assert_called_once_with(502)
                    self.assertEqual(handler.server.router_state.record.call_args.args[3], 502)
                    self.assertIn("error", json.loads(handler.wfile.getvalue()))
                    self.assertNotIn(b"message_stop", handler.wfile.getvalue())


class MessagesStreamTerminationTests(unittest.TestCase):
    def test_eof_does_not_close_or_publish_a_tool_as_complete(self):
        for arguments in ('{"a":', '{"a":1}'):
            with self.subTest(arguments=arguments):
                outcome = {}
                events = decoded(router.chat_sse_to_anthropic_sse(
                    BytesIO(sse(chat_tool_frame(arguments))), "fixture", outcome=outcome))
                self.assertTrue(outcome.get("truncated"))
                self.assertEqual(events[-1]["type"], "error")
                self.assertFalse(any(event["type"] in {"content_block_stop", "message_delta", "message_stop"}
                                     for event in events))

    def test_explicit_completion_and_done_only_gateways_remain_compatible(self):
        for finish, done in (("tool_calls", False), ("stop", False), (None, True)):
            with self.subTest(finish=finish, done=done):
                wire = sse(chat_tool_frame('{"a":1}', finish=finish))
                wire += b"data: [DONE]\n\n" if done else b""
                outcome = {}
                events = decoded(router.chat_sse_to_anthropic_sse(BytesIO(wire), "fixture", outcome=outcome))
                self.assertFalse(outcome)
                self.assertEqual(events[-1]["type"], "message_stop")
                self.assertEqual([event["index"] for event in events if event["type"] == "content_block_stop"], [0])
                self.assertEqual(next(event["delta"]["stop_reason"] for event in events
                                      if event["type"] == "message_delta"), "tool_use")

    def test_output_limit_still_uses_max_tokens_not_tool_use(self):
        for finish in ("length", "max_tokens", "max_output_tokens"):
            with self.subTest(finish=finish):
                events = decoded(router.chat_sse_to_anthropic_sse(
                    BytesIO(sse(chat_tool_frame('{"a":', finish=finish))), "fixture"))
                self.assertEqual(next(event["delta"]["stop_reason"] for event in events
                                      if event["type"] == "message_delta"), "max_tokens")


class MessagesJsonStreamFallbackTests(unittest.TestCase):
    def relay(self, payload, *, stream=True, thinking=False):
        handler = object.__new__(router.SotaRouterHandler)
        handler.command, handler.path = "POST", "/messages"
        handler.wfile = BytesIO()
        handler._relay_headers_sent = False
        handler.close_connection = False
        handler.server = SimpleNamespace(router_state=mock.Mock())
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._open_upstream = mock.Mock(side_effect=AssertionError("a received response must not be sent again"))
        upstream = BytesIO(json.dumps(payload).encode("utf-8"))
        upstream.headers = {"Content-Type": "application/json"}
        retry = handler._relay_messages_chat(upstream, "fixture", 200, time.monotonic(), "fixture", {
            "stream": stream, "thinking": {"type": "enabled" if thinking else "disabled"},
        }, is_final_attempt=True)
        self.assertFalse(retry)
        handler._open_upstream.assert_not_called()
        return handler

    @staticmethod
    def payload(*, finish="tool_calls"):
        return {"id": "chatcmpl-fixture", "usage": {"prompt_tokens": 7, "completion_tokens": 11},
                "choices": [{"finish_reason": finish, "message": {
                    "content": "answer", "reasoning_content": "thought", "tool_calls": [
                        {"id": "call_first", "type": "function", "function": {
                            "name": "first", "arguments": '{"a":1}'}},
                        {"id": "call_second", "type": "function", "function": {
                            "name": "second", "arguments": '{"b":"汉字"}'}},
                    ],
                }}]}

    def test_stream_requested_json_emits_messages_sse_with_parallel_tools_and_usage(self):
        payload = self.payload()
        original = deepcopy(payload)
        handler = self.relay(payload, thinking=True)
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call("Content-Type", "text/event-stream; charset=utf-8")
        events = decoded([handler.wfile.getvalue()])
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "message_stop")
        starts = [event for event in events if event["type"] == "content_block_start"]
        self.assertEqual([event["content_block"]["type"] for event in starts],
                         ["thinking", "text", "tool_use", "tool_use"])
        self.assertEqual([event["content_block"]["id"] for event in starts[2:]], ["call_first", "call_second"])
        self.assertEqual([event["content_block"]["name"] for event in starts[2:]], ["first", "second"])
        self.assertEqual(len({event["index"] for event in starts}), 4)
        arguments = [json.loads(event["delta"]["partial_json"]) for event in events
                     if event["type"] == "content_block_delta" and event["delta"]["type"] == "input_json_delta"]
        self.assertEqual(arguments, [{"a": 1}, {"b": "汉字"}])
        final_delta = next(event for event in events if event["type"] == "message_delta")
        self.assertEqual(final_delta["delta"]["stop_reason"], "tool_use")
        self.assertEqual(final_delta["usage"], {"input_tokens": 7, "output_tokens": 11})
        self.assertEqual(sum(event["type"] == "message_stop" for event in events), 1)
        self.assertEqual(handler.server.router_state.record.call_args.args[3], 200)
        self.assertEqual(payload, original)

    def test_json_fallback_still_honors_disabled_thinking(self):
        handler = self.relay(self.payload(), thinking=False)
        events = decoded([handler.wfile.getvalue()])
        self.assertFalse(any(event.get("content_block", {}).get("type") == "thinking" for event in events))
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertIn("answer", handler.wfile.getvalue().decode("utf-8"))

    def test_output_limit_preserves_raw_partial_tool_arguments_and_max_tokens(self):
        for finish in ("length", "max_tokens", "max_output_tokens"):
            with self.subTest(finish=finish):
                payload = self.payload(finish=finish)
                payload["choices"][0]["message"]["tool_calls"] = [{"id": "call_partial", "function": {
                    "name": "patch", "arguments": '{"input":"half',
                }}]
                handler = self.relay(payload)
                events = decoded([handler.wfile.getvalue()])
                self.assertEqual(next(event["delta"]["stop_reason"] for event in events
                                      if event["type"] == "message_delta"), "max_tokens")
                self.assertEqual([event["delta"]["partial_json"] for event in events
                                  if event["type"] == "content_block_delta"
                                  and event["delta"]["type"] == "input_json_delta"], ['{"input":"half'])
                self.assertNotIn(b"raw_arguments", handler.wfile.getvalue())

    def test_nonstandard_missing_and_stop_finish_reasons_keep_tools(self):
        for finish in (None, "stop"):
            with self.subTest(finish=finish):
                handler = self.relay(self.payload(finish=finish))
                events = decoded([handler.wfile.getvalue()])
                self.assertEqual(next(event["delta"]["stop_reason"] for event in events
                                      if event["type"] == "message_delta"), "tool_use")

    def test_nonstream_success_is_unchanged(self):
        payload = self.payload()
        handler = self.relay(payload, stream=False, thinking=True)
        handler.send_header.assert_any_call("Content-Type", "application/json; charset=utf-8")
        self.assertEqual(json.loads(handler.wfile.getvalue()),
                         router.chat_completion_to_anthropic_message(payload, "fixture", thinking_enabled=True))


class SseEnvelopeErrorTests(unittest.TestCase):
    def test_named_chat_error_is_terminal_even_after_a_finish_reason(self):
        for convert in (router.chat_sse_to_responses, router.chat_sse_to_anthropic_sse):
            for prefix in (b"", sse(chat_tool_frame('{"a":1}', finish="tool_calls"))):
                with self.subTest(convert=convert.__name__, after_content=bool(prefix)):
                    outcome = {}
                    wire = prefix + sse({"code": "fixture_error", "message": "fixture busy"}, "error")
                    wire += b"data: [DONE]\n\n"
                    events = decoded(convert(BytesIO(wire), "fixture", outcome=outcome))
                    self.assertTrue(outcome.get("failed"))
                    self.assertFalse(any(event["type"] in {"response.completed", "message_stop", "content_block_stop"}
                                         or event["type"].endswith(".done") for event in events))
                    self.assertIn("fixture busy", json.dumps(events))

    def test_multiline_error_frame_is_not_replaced_by_done_success(self):
        for convert in (router.chat_sse_to_responses, router.chat_sse_to_anthropic_sse):
            with self.subTest(convert=convert.__name__):
                outcome = {}
                wire = sse({"error": {"code": "fixture", "message": "fixture busy"}}, multiline=True)
                wire += b"data: [DONE]\n\n"
                events = decoded(convert(BytesIO(wire), "fixture", outcome=outcome))
                self.assertTrue(outcome.get("failed"))
                self.assertFalse(any(event["type"] in {"response.completed", "message_stop"} for event in events))
                self.assertIn("fixture busy", json.dumps(events))

    def test_final_error_frame_at_eof_is_still_processed(self):
        for convert in (router.chat_sse_to_responses, router.chat_sse_to_anthropic_sse):
            with self.subTest(convert=convert.__name__):
                outcome = {}
                wire = sse({"message": "fixture busy"}, "error", multiline=True, terminated=False)
                events = decoded(convert(BytesIO(wire), "fixture", outcome=outcome))
                self.assertTrue(outcome.get("failed"))
                self.assertIn("fixture busy", json.dumps(events))

    def test_anthropic_untyped_error_cannot_be_overridden_by_message_stop(self):
        outcome = {}
        wire = sse({"error": {"type": "fixture_error", "message": "fixture busy"}})
        wire += sse({"type": "message_stop"}, "message_stop")
        events = decoded(router.anthropic_sse_to_responses(BytesIO(wire), "fixture", outcome=outcome))
        self.assertTrue(outcome.get("failed"))
        self.assertFalse(any(event["type"] == "response.completed" for event in events))
        self.assertIn("fixture busy", json.dumps(events))

    def test_multiline_tool_frame_preserves_complete_json_and_unicode(self):
        expected = {"a": "汉字", "b": "line\nquote\"slash\\"}
        arguments = json.dumps(expected, ensure_ascii=False)
        outcome = {}
        wire = sse(chat_tool_frame(arguments, finish="tool_calls"), "chat.completion.chunk", multiline=True)
        wire += b"data: [DONE]\n\n"
        events = decoded(router.chat_sse_to_anthropic_sse(BytesIO(wire), "fixture", outcome=outcome))
        starts = [event for event in events if event["type"] == "content_block_start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["content_block"]["name"], "patch")
        actual = "".join(event["delta"]["partial_json"] for event in events
                         if event["type"] == "content_block_delta")
        self.assertEqual(json.loads(actual), expected)
        self.assertFalse(outcome)
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_advisory_sse_event_name_does_not_leak_to_the_next_record(self):
        wire = b"event: ping\ndata: {}\n\n"
        wire += sse(chat_tool_frame('{}', finish="tool_calls")) + b"data: [DONE]\n\n"
        for convert in (router.chat_sse_to_responses, router.chat_sse_to_anthropic_sse):
            with self.subTest(convert=convert.__name__):
                outcome = {}
                events = decoded(convert(BytesIO(wire), "fixture", outcome=outcome))
                self.assertFalse(outcome)
                self.assertIn(events[-1]["type"], {"response.completed", "message_stop"})


if __name__ == "__main__":
    unittest.main()
