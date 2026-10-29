"""JSON fallback regressions using only in-memory payloads and mocked HTTP sinks."""
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


def chat_payload(arguments='{"input":"ok"}', *, finish="tool_calls"):
    return {"id": "chatcmpl_fixture", "choices": [{"finish_reason": finish, "message": {
        "content": "answer", "tool_calls": [{"id": "call_fixture", "function": {
            "name": "patch", "arguments": arguments,
        }}],
    }}]}


def anthropic_payload(tool_input=None, *, stop="tool_use"):
    return {"id": "msg_fixture", "stop_reason": stop, "content": [
        {"type": "text", "text": "answer"},
        {"type": "tool_use", "id": "call_fixture", "name": "patch",
         "input": {"input": "ok"} if tool_input is None else tool_input},
    ]}


def events_from_bytes(data):
    return [json.loads(line[5:]) for line in data.decode("utf-8").splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


def terminal(events):
    return next(event["response"] for event in reversed(events)
                if event.get("type") in {"response.completed", "response.incomplete", "response.failed"})


EXECUTABLE_EVENTS = {
    "response.function_call_arguments.delta", "response.function_call_arguments.done",
    "response.custom_tool_call_input.delta", "response.custom_tool_call_input.done",
}


class JsonFallbackConversionTests(unittest.TestCase):
    def test_valid_custom_inputs_preserve_text_namespace_and_source(self):
        for text in ("", '*** Begin Patch\n汉字 "quoted" \\path\n*** End Patch'):
            for convert, payload in (
                (router.chat_message_to_response, chat_payload(json.dumps({"input": text}))),
                (router.anthropic_message_to_response, anthropic_payload({"input": text})),
            ):
                with self.subTest(convert=convert.__name__, text=text):
                    original = deepcopy(payload)
                    response = convert(payload, "fixture", {"patch": "functions"}, {"patch": "custom"})
                    self.assertEqual(response["output"][-1]["input"], text)
                    self.assertEqual(response["output"][-1]["namespace"], "functions")
                    self.assertEqual(response["output"][-1]["status"], "completed")
                    events = events_from_bytes(b"".join(router.response_to_sse(response)))
                    done = [event for event in events if event.get("type") == "response.custom_tool_call_input.done"]
                    self.assertEqual([event["input"] for event in done], [text])
                    self.assertEqual(payload, original)

    def test_invalid_completed_chat_custom_wrapper_is_not_raw_tool_input(self):
        for arguments in ('{"input":"partial', '{}', '{"input":17}', 'null', 'true', 'not json'):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                router.chat_message_to_response(chat_payload(arguments), tool_kinds={"patch": "custom"})

    def test_invalid_completed_anthropic_custom_wrapper_is_not_raw_tool_input(self):
        for value in ({}, {"input": 17}, {"other": "field"}, '{"input":"partial', 'not json', []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                router.anthropic_message_to_response(anthropic_payload(value), tool_kinds={"patch": "custom"})

    def test_completed_json_string_wrapper_remains_supported(self):
        for convert, payload in (
            (router.chat_message_to_response, chat_payload(json.dumps("plain text"))),
            (router.anthropic_message_to_response, anthropic_payload(json.dumps("plain text"))),
        ):
            self.assertEqual(convert(payload, tool_kinds={"patch": "custom"})["output"][-1]["input"], "plain text")

    def test_output_limit_never_completes_or_streams_tool_input(self):
        for convert, payload in (
            (router.chat_message_to_response, chat_payload('{"input":"half', finish="length")),
            (router.anthropic_message_to_response, anthropic_payload('{"input":"half', stop="max_tokens")),
        ):
            for kind in ("function", "custom"):
                with self.subTest(convert=convert.__name__, kind=kind):
                    response = convert(payload, tool_kinds={"patch": kind})
                    self.assertEqual(response["status"], "incomplete")
                    self.assertEqual(response["output"][-1]["status"], "incomplete")
                    events = events_from_bytes(b"".join(router.response_to_sse(response)))
                    self.assertEqual(terminal(events)["status"], "incomplete")
                    self.assertFalse(any(event.get("type") in EXECUTABLE_EVENTS for event in events))
                    tools = [event["item"] for event in events if event.get("type") == "response.output_item.done"
                             and event["item"].get("type") in {"function_call", "custom_tool_call"}]
                    self.assertEqual([item["status"] for item in tools], ["incomplete"])

    def test_valid_function_arguments_and_text_are_unchanged(self):
        for convert, payload in (
            (router.chat_message_to_response, chat_payload('{"a":1}')),
            (router.anthropic_message_to_response, anthropic_payload({"a": 1})),
        ):
            response = convert(payload)
            events = events_from_bytes(b"".join(router.response_to_sse(response)))
            self.assertEqual(terminal(events)["output_text"], "answer")
            self.assertEqual(terminal(events)["output"][-1]["arguments"], '{"a":1}')
            self.assertEqual([event["arguments"] for event in events
                              if event.get("type") == "response.function_call_arguments.done"], ['{"a":1}'])

    def test_explicit_json_errors_are_never_empty_successes(self):
        for convert in (router.chat_message_to_response, router.anthropic_message_to_response):
            for payload in ({"error": {"type": "overloaded_error", "message": "fixture busy"}},
                            {"type": "error", "message": "fixture busy"}, {"error": "fixture busy"}):
                with self.subTest(convert=convert.__name__, payload=payload), self.assertRaisesRegex(ValueError, "fixture busy"):
                    convert(payload)

    def test_non_object_json_responses_are_refused(self):
        for convert in (router.chat_message_to_response, router.anthropic_message_to_response):
            with self.assertRaises(ValueError):
                convert([])

    def test_failed_response_cannot_promote_existing_tool_and_does_not_mutate_source(self):
        source = {"id": "resp_fixture", "status": "failed", "error": {"code": "fixture"}, "output": [
            {"id": "fc_fixture", "type": "function_call", "status": "completed", "arguments": '{"a":1}'},
            {"id": "ctc_fixture", "type": "custom_tool_call", "status": "completed", "input": "partial"},
        ]}
        original = deepcopy(source)
        events = events_from_bytes(b"".join(router.response_to_sse(source)))
        self.assertEqual(source, original)
        self.assertEqual(terminal(events)["status"], "failed")
        self.assertEqual([item["status"] for item in terminal(events)["output"]], ["incomplete", "incomplete"])
        self.assertFalse(any(event.get("type") in EXECUTABLE_EVENTS for event in events))

    def test_incomplete_item_is_not_promoted_by_completed_envelope(self):
        response = {"status": "completed", "output": [{"type": "custom_tool_call", "status": "incomplete", "input": "half"}]}
        events = events_from_bytes(b"".join(router.response_to_sse(response)))
        self.assertFalse(any(event.get("type") in EXECUTABLE_EVENTS for event in events))
        self.assertEqual(terminal(events)["output"][0]["status"], "incomplete")


class JsonFallbackRelayTests(unittest.TestCase):
    def relay(self, method, payload, *, stream=True, kind="custom"):
        handler = object.__new__(router.SotaRouterHandler)
        handler.command, handler.path = "POST", "/responses"
        handler.wfile = BytesIO()
        handler._relay_headers_sent = False
        handler.close_connection = False
        handler.server = SimpleNamespace(router_state=mock.Mock())
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        upstream = BytesIO(json.dumps(payload).encode("utf-8"))
        upstream.headers = {"Content-Type": "application/json"}
        retry = getattr(handler, method)(upstream, "fixture", 200, time.monotonic(), "fixture",
                                         {"stream": stream}, tool_kinds={"patch": kind}, is_final_attempt=True)
        self.assertFalse(retry)
        return handler

    def test_stream_requested_json_success_uses_real_relay_and_decodes_custom_text(self):
        for method, payload in (("_relay_chat", chat_payload()), ("_relay_juno", anthropic_payload())):
            handler = self.relay(method, payload)
            handler.send_response.assert_called_once_with(200)
            self.assertEqual(handler.server.router_state.record.call_args.args[3], 200)
            response = terminal(events_from_bytes(handler.wfile.getvalue()))
            self.assertEqual(response["status"], "completed")
            self.assertEqual(response["output"][-1]["input"], "ok")

    def test_stream_requested_json_limit_never_emits_executable_tool_done(self):
        for method, payload in (("_relay_chat", chat_payload('{"input":"half', finish="length")),
                                ("_relay_juno", anthropic_payload('{"input":"half', stop="max_tokens"))):
            for kind in ("function", "custom"):
                handler = self.relay(method, payload, kind=kind)
                events = events_from_bytes(handler.wfile.getvalue())
                self.assertEqual(terminal(events)["status"], "incomplete")
                self.assertFalse(any(event.get("type") in EXECUTABLE_EVENTS for event in events))

    def test_json_error_and_bad_custom_input_return_and_record_502_before_streaming(self):
        for stream in (True, False):
            for method, invalid in (("_relay_chat", chat_payload('{"input":"half')),
                                    ("_relay_juno", anthropic_payload({"input": 12}))):
                for payload in (invalid, {"error": {"code": "fixture_error", "message": "fixture busy"}}):
                    with self.subTest(stream=stream, method=method, payload=payload):
                        handler = self.relay(method, payload, stream=stream)
                        handler.send_response.assert_called_once_with(502)
                        self.assertEqual(handler.server.router_state.record.call_args.args[3], 502)
                        self.assertIn("error", json.loads(handler.wfile.getvalue()))
                        self.assertNotIn(b"response.completed", handler.wfile.getvalue())


if __name__ == "__main__":
    unittest.main()
