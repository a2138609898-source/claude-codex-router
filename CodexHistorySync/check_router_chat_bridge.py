"""Offline regressions for shared bridge fixes; no live profile or vendor is written."""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import codex_sota_router as B
import sota_registry as R
import sota_compaction as C

TOOL = {"type": "function", "name": "echo", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}


def events(raw):
    return [json.loads(line[5:]) for line in raw.decode().splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


def chat_stream(text="summary", finish="stop"):
    chunks = [{"model": "test-model", "choices": [{"delta": {"content": text}, "finish_reason": None}]}]
    if finish:
        chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    raw = b"".join(("data: " + json.dumps(item) + "\n\n").encode() for item in chunks)
    return raw + (b"data: [DONE]\n\n" if finish else b"")


def messages_stream(text="summary", finish="end_turn"):
    chunks = [
        {"type": "message_start", "message": {"id": "msg_fixture", "model": "m", "usage": {"input_tokens": 15}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
    ]
    if finish:
        chunks += [{"type": "message_delta", "delta": {"stop_reason": finish}, "usage": {"output_tokens": 10}},
                   {"type": "message_stop"}]
    return b"".join(("event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n").encode() for item in chunks)


class ConversionTests(unittest.TestCase):
    def test_parallel_history_is_one_assistant_turn(self):
        history = [{"role": "user", "content": "use tools"},
                   {"type": "function_call", "name": "a", "call_id": "a", "arguments": "{}"},
                   {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "checking"}]},
                   {"type": "custom_tool_call", "name": "b", "call_id": "b", "input": "literal"},
                   {"type": "function_call_output", "call_id": "a", "output": "A"},
                   {"type": "custom_tool_call_output", "call_id": "b", "output": "B"},
                   {"role": "assistant", "content": "finished"}]
        messages = B.responses_to_chat_payload({"input": history}, "m")["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "tool", "assistant"])
        self.assertEqual([c["id"] for c in messages[1]["tool_calls"]], ["a", "b"])
        self.assertEqual(messages[1]["content"], "checking")
        self.assertEqual(json.loads(messages[1]["tool_calls"][1]["function"]["arguments"]), {"input": "literal"})

    def test_parallel_controls_all_bridge_directions(self):
        for parallel in (False, True):
            source = {"input": "hello", "tools": [TOOL], "parallel_tool_calls": parallel}
            chat = B.responses_to_chat_payload(source, "m")
            anthropic = B.responses_to_anthropic_payload(source, "m")
            self.assertIs(chat["parallel_tool_calls"], parallel)
            self.assertIs(anthropic["tool_choice"]["disable_parallel_tool_use"], not parallel)
            messages = {"messages": [{"role": "user", "content": "hello"}],
                        "tools": [{"name": "echo", "input_schema": TOOL["parameters"]}],
                        "tool_choice": {"type": "auto", "disable_parallel_tool_use": not parallel}}
            self.assertIs(B.messages_to_chat_payload(messages, "m")["parallel_tool_calls"], parallel)

    def test_missing_or_disabled_tools_keep_old_request_shape(self):
        self.assertNotIn("parallel_tool_calls", B.responses_to_chat_payload({"input": "hi"}, "m"))
        result = B.responses_to_chat_payload({"input": "hi", "tools": [TOOL], "tool_choice": "none", "parallel_tool_calls": False}, "m")
        self.assertNotIn("tools", result)
        self.assertNotIn("parallel_tool_calls", result)

    def test_parallel_metadata_json_and_sse(self):
        chat = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        messages = {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"}
        self.assertFalse(B.chat_message_to_response(chat, parallel_tool_calls=False)["parallel_tool_calls"])
        self.assertFalse(B.anthropic_message_to_response(messages, parallel_tool_calls=False)["parallel_tool_calls"])
        for converter, stream in ((B.chat_sse_to_responses, chat_stream()),
                                  (B.anthropic_sse_to_responses, messages_stream())):
            raw = b"".join(converter(io.BytesIO(stream), "m", parallel_tool_calls=False))
            for event in events(raw):
                if "response" in event:
                    self.assertFalse(event["response"]["parallel_tool_calls"])

    def test_compaction_is_bounded_tool_free_and_preserves_original(self):
        payload = {"input": [{"role": "user", "content": "pending exact request"},
                             {"type": "additional_tools", "tools": [TOOL]}, {"type": "compaction_trigger"}], "instructions": "keep paths",
                   "tools": [TOOL], "tool_choice": "required", "max_output_tokens": 256000,
                   "reasoning": {"effort": "max"}, "stream": True}
        original = copy.deepcopy(payload)
        result = B.responses_compaction_payload(payload)
        self.assertEqual(payload, original)
        self.assertEqual(result["input"][0], payload["input"][0])
        self.assertIn("keep paths", result["instructions"])
        self.assertFalse(result["stream"])
        self.assertEqual(result["max_output_tokens"], 4096)
        self.assertNotIn("tools", result)
        self.assertNotIn("tools", B.responses_to_chat_payload(result, "m"))
        self.assertNotIn("tools", B.responses_to_anthropic_payload(result, "m"))
        self.assertNotIn("reasoning", result)
        self.assertLessEqual(B.responses_to_anthropic_payload(result, "m")["max_tokens"], 4096)
        with self.assertRaises(ValueError):
            B.responses_compaction_payload({"input": []})

    def test_incomplete_empty_or_tool_compaction_never_replaces_history(self):
        for response in ({"status": "incomplete", "output": []}, {"status": "completed", "output": []},
                         {"status": "completed", "output": [{"type": "function_call"}]}):
            with self.assertRaises(ValueError):
                B.response_to_compaction(response)

    def test_local_compaction_is_encrypted_and_replays_in_both_adapters(self):
        response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "exact_file.py pending request"}]}]}
        output = B.response_to_compaction(response)["output"]
        self.assertEqual(output[0]["type"], "compaction")
        self.assertNotIn("exact_file.py", output[0]["encrypted_content"])
        for convert in (B.responses_to_chat_payload, B.responses_to_anthropic_payload):
            result = convert({"input": output}, "m")
            self.assertIn("exact_file.py", json.dumps(result))
        original = {"input": [{"type": "compaction", "encrypted_content": "vendor-owned-opaque"}]}
        self.assertIs(C.restore_payload(original), original)
        with self.assertRaises(ValueError):
            B.responses_to_chat_payload(original, "m")
        with self.assertRaises(ValueError):
            C.restore_summary(C.PREFIX + "broken")

    def test_compaction_survives_a_fresh_process(self):
        token = C.protect_summary("exact_file.py pending request")
        result = subprocess.run([sys.executable, "-B", "-c",
                                 "import sys; from sota_compaction import restore_summary; print(restore_summary(sys.stdin.read()))"],
                                input=token, text=True, capture_output=True, cwd=Path(__file__).parent, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "exact_file.py pending request")

    def test_catalog_ceiling_is_explicit_and_isolated(self):
        levels = [{"effort": x, "description": x} for x in ("low", "medium", "high", "xhigh", "max", "ultra")]
        template = {"models": [{"slug": "gpt-5.6-sol", "supported_reasoning_levels": levels, "context_window": 10000}]}
        registry = {"providers": [{"id": "example", "name": "Example", "enabled": True, "prefix": "example--",
                                  "models": [{"id": "limited", "enabled": True, "reasoning_effort_ceiling": "max", "default_reasoning_level": "ultra"},
                                             {"id": "unchanged", "enabled": True}]}]}
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.json", Path(folder) / "catalog.json"
            source.write_text(json.dumps(template), encoding="utf-8")
            B0 = source.read_bytes()
            R.build_model_catalog(registry, source, output)
            models = json.loads(output.read_text())["models"]
            self.assertEqual([x["effort"] for x in models[0]["supported_reasoning_levels"]], ["low", "medium", "high", "xhigh", "max"])
            self.assertEqual(models[0]["default_reasoning_level"], "max")
            self.assertEqual(models[1]["supported_reasoning_levels"], levels)
            self.assertEqual(source.read_bytes(), B0)
        with self.assertRaises(ValueError):
            R.validate_model({"id": "m", "reasoning_effort_ceiling": "typo"})

    def test_adapter_selection_uses_configuration_for_any_provider(self):
        for provider_id in ("juno", "another-vendor"):
            provider = {"id": provider_id, "request_adapter": "responses_to_anthropic_messages"}
            self.assertTrue(B.is_juno_adapter(provider))
            self.assertTrue(R.uses_responses_to_anthropic_messages(provider))
            provider["request_adapter"] = ""
            self.assertFalse(B.is_juno_adapter(provider))
            self.assertFalse(R.uses_responses_to_anthropic_messages(provider))


class Upstream(BaseHTTPRequestHandler):
    mode = "json"
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append((self.path, body))
        mode = type(self).mode
        status, content_type = 200, "application/json"
        if mode == "billing":
            status, value = 403, {"error": {"message": "insufficient balance", "type": "billing_error"}}
            raw = json.dumps(value).encode()
        elif mode in ("sse", "truncated"):
            content_type = "text/event-stream"
            if self.path == "/messages":
                raw = messages_stream("Remember exact_file.py and pending request", None if mode == "truncated" else "end_turn")
            else:
                raw = chat_stream("Remember exact_file.py and pending request", None if mode == "truncated" else "stop")
        elif self.path == "/messages":
            raw = json.dumps({"content": [{"type": "text", "text": "Remember exact_file.py and pending request"}],
                              "stop_reason": "max_tokens" if mode == "incomplete" else "end_turn",
                              "usage": {"input_tokens": 15, "output_tokens": 10}}).encode()
        else:
            raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Remember exact_file.py and pending request"},
                                           "finish_reason": "length" if mode == "incomplete" else "stop"}],
                              "usage": {"prompt_tokens": 15, "completion_tokens": 10}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        Upstream.mode, Upstream.received = "json", []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.provider = {"id": "fixture", "name": "Fixture", "base_url": f"http://127.0.0.1:{self.upstream.server_port}",
                         "prefix": "fixture--", "enabled": True, "protected": False, "is_default": True, "auth_type": "codex_auth",
                         "protocols": ["responses"], "request_adapter": "responses_to_chat_completions", "chat_path": "/chat/completions",
                         "responses_path": "/responses", "messages_path": "/messages", "models": [{"id": "m", "enabled": True}]}
        self.registry, self.auth = root / "providers.json", root / "auth.json"
        self.auth.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "fixture-only"}), encoding="utf-8")
        self.save_registry()
        state = B.RouterState(self.registry, self.auth, root / "router.jsonl")
        self.router = B.SotaThreadingHTTPServer(("127.0.0.1", 0), B.SotaRouterHandler)
        self.router.router_state = state
        self.thread = threading.Thread(target=self.router.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.router.shutdown()
        self.router.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.folder.cleanup()

    def save_registry(self):
        self.registry.write_text(json.dumps({"version": 1, "providers": [self.provider]}), encoding="utf-8")
        if hasattr(self, "router"):
            # Production reload checks are throttled; fixture edits must be observed now.
            self.router.router_state._signature_checked_at = 0.0

    def request(self, path="/responses/compact", **overrides):
        body = {"model": "fixture--m", "input": [{"role": "user", "content": "Keep exact_file.py and pending request"}]}
        body.update(overrides)
        request = urllib.request.Request(f"http://127.0.0.1:{self.router.server_port}" + path,
                                         json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_chat_compaction_json_and_sse_are_replayable(self):
        for mode in ("json", "sse"):
            Upstream.mode = mode
            status, raw = self.request(tools=[TOOL], max_output_tokens=256000, stream=True)
            value = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertEqual(value["object"], "response.compaction")
            self.assertEqual(value["output"][0]["type"], "compaction")
            self.assertIn("exact_file.py", C.restore_summary(value["output"][0]["encrypted_content"]))
            replay = B.responses_to_chat_payload({"input": value["output"]}, "m")
            self.assertIn("exact_file.py", replay["messages"][0]["content"])
            path, outgoing = Upstream.received[-1]
            self.assertEqual(path, "/chat/completions")
            self.assertFalse(outgoing["stream"])
            self.assertLessEqual(outgoing["max_tokens"], 4096)
            self.assertNotIn("tools", outgoing)

    def test_messages_compaction_uses_dedicated_summary(self):
        self.provider["request_adapter"] = "responses_to_anthropic_messages"
        self.save_registry()
        for mode in ("json", "sse"):
            Upstream.mode = mode
            status, raw = self.request(reasoning={"effort": "max"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["object"], "response.compaction")
            self.assertEqual(Upstream.received[-1][0], "/messages")
            self.assertLessEqual(Upstream.received[-1][1]["max_tokens"], 4096)

    def test_v2_compaction_trigger_returns_a_compaction_response_item(self):
        for adapter in ("responses_to_chat_completions", "responses_to_anthropic_messages"):
            self.provider["request_adapter"] = adapter
            self.save_registry()
            for stream in (False, True):
                history = [{"role": "user", "content": "Keep exact_file.py and pending request"},
                           {"type": "additional_tools", "tools": [TOOL]}, {"type": "compaction_trigger"}]
                status, raw = self.request("/responses", input=history, stream=stream, parallel_tool_calls=False)
                self.assertEqual(status, 200)
                value = next(event["response"] for event in events(raw) if event.get("type") == "response.completed") if stream else json.loads(raw)
                self.assertEqual(value["output"][0]["type"], "compaction")
                self.assertIs(value["parallel_tool_calls"], False)
                self.assertIn("exact_file.py", C.restore_summary(value["output"][0]["encrypted_content"]))
                self.assertNotIn("tools", Upstream.received[-1][1])

    def test_native_v2_trigger_is_untouched(self):
        self.provider["request_adapter"] = ""
        self.save_registry()
        history = [{"role": "user", "content": "original"}, {"type": "compaction_trigger"}]
        status, _raw = self.request("/responses", input=history)
        self.assertEqual(status, 200)
        self.assertEqual(Upstream.received[-1][1]["input"], history)

    def test_native_messages_on_a_mixed_provider_are_not_converted_to_responses(self):
        self.provider["request_adapter"] = "responses_to_anthropic_messages"
        self.provider["protocols"] = ["responses", "messages"]
        self.save_registry()
        history = [{"role": "user", "content": "native Messages history"}]
        status, raw = self.request("/messages", messages=history, max_tokens=100)
        self.assertEqual(status, 200)
        self.assertIn("content", json.loads(raw))
        self.assertNotIn("output", json.loads(raw))
        self.assertEqual(Upstream.received[-1][1]["messages"], history)

    def test_failed_or_truncated_compaction_is_not_success(self):
        for adapter in ("responses_to_chat_completions", "responses_to_anthropic_messages"):
            self.provider["request_adapter"] = adapter
            self.save_registry()
            for mode in ("incomplete", "truncated"):
                Upstream.mode = mode
                status, raw = self.request()
                self.assertEqual(status, 502)
                self.assertNotIn("output", json.loads(raw))

    def test_parallel_controls_survive_actual_http_json_and_stream(self):
        for adapter in ("responses_to_chat_completions", "responses_to_anthropic_messages"):
            self.provider["request_adapter"] = adapter
            self.save_registry()
            for stream in (False, True):
                Upstream.mode = "sse" if stream else "json"
                status, raw = self.request("/responses", tools=[TOOL], parallel_tool_calls=False, stream=stream)
                self.assertEqual(status, 200)
                responses = [event["response"] for event in events(raw) if "response" in event] if stream else [json.loads(raw)]
                self.assertTrue(responses)
                self.assertTrue(all(response["parallel_tool_calls"] is False for response in responses))
                outgoing = Upstream.received[-1][1]
                if adapter == "responses_to_chat_completions":
                    self.assertIs(outgoing["parallel_tool_calls"], False)
                else:
                    self.assertIs(outgoing["tool_choice"]["disable_parallel_tool_use"], True)

    def test_bridged_token_count_stays_local(self):
        self.provider["protocols"] = ["responses", "messages"]
        self.save_registry()
        status, raw = self.request("/v1/messages/count_tokens", messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(raw)["input_tokens"], 0)
        self.assertEqual(Upstream.received, [])

    def test_upstream_billing_error_is_preserved_and_not_retried(self):
        Upstream.mode = "billing"
        status, raw = self.request()
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"]["type"], "billing_error")
        self.assertEqual(len(Upstream.received), 1)

    def test_native_compact_is_untouched(self):
        self.provider["request_adapter"] = ""
        self.save_registry()
        status, _raw = self.request()
        self.assertEqual(status, 200)
        path, body = Upstream.received[-1]
        self.assertEqual(path, "/responses/compact")
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["input"][0]["content"], "Keep exact_file.py and pending request")

    def test_native_provider_can_continue_local_compaction(self):
        self.provider["request_adapter"] = ""
        self.save_registry()
        history = [{"type": "compaction", "encrypted_content": C.protect_summary("exact_file.py pending request")}]
        status, _raw = self.request("/responses", input=history)
        self.assertEqual(status, 200)
        path, body = Upstream.received[-1]
        self.assertEqual(path, "/responses")
        self.assertIn("exact_file.py", body["input"][0]["content"])

    def test_damaged_compaction_never_generates_a_reply_without_history(self):
        history = [{"type": "compaction", "encrypted_content": C.PREFIX + "broken"}]
        status, raw = self.request("/responses", input=history)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["type"], "request_translation_error")
        self.assertEqual(Upstream.received, [])

    def test_empty_compaction_does_not_contact_upstream(self):
        status, _raw = self.request(input=[])
        self.assertEqual(status, 400)
        self.assertEqual(Upstream.received, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
