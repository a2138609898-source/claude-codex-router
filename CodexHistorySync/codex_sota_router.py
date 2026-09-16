#!/usr/bin/env python3
"""Local registry-driven Responses router for the isolated Codex SOTA profile."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from sota_registry import (
    auth_headers,
    endpoint_url,
    effective_model_prefix,
    failover_chain,
    load_registry,
    provider_key,
    published_slug,
    read_codex_auth_key,
    registry_digest,
    selectable_slugs,
)


# Bumped whenever /healthz gains a field the manager reads, so a manager built from this
# source refuses to trust an older listener that cannot answer for itself. 15 added the
# fail-closed model provenance guard; 16 made requests without an idempotency key single-shot;
# 17 makes *all* billable generation requests single-shot. A third-party gateway may ignore an
# Idempotency-Key, and separate vendors never share an idempotency ledger, so the key cannot be
# treated as permission to replay a request that may already have been accepted and billed.
ROUTER_VERSION = "17"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_HEADERS_TO_DROP = HOP_BY_HOP_HEADERS | {
    "authorization",
    "content-length",
    "host",
}
RESPONSE_HEADERS_TO_DROP = HOP_BY_HOP_HEADERS | {"content-length"}
# Upstream answers that say "this gateway cannot serve you right now" rather than "your
# request is wrong" — the same request may well succeed at another vendor. 400 is absent
# on purpose: a malformed body fails identically everywhere.
FAILOVER_RETRY_STATUSES = frozenset({401, 402, 403, 404, 408, 409, 425, 429})
# How many recent outcomes per vendor to keep, and how many consecutive failures within
# that window mark a vendor as one to try last.
# Deliberately NOT capping non-final attempts any tighter than the provider's own timeout.
# A 45s cap was tried and it aborted requests that were merely slow — long reasoning turns
# routinely exceed it — turning working calls into failures. Slow is not the same as broken,
# and only the provider's configured timeout gets to decide when to give up.
HEALTH_WINDOW = 5
UNHEALTHY_STREAK = 3
# Bound request buffering before reading from the socket.  Claude can legitimately send
# image-bearing messages, so keep the ceiling generous while still preventing a forged
# Content-Length from allocating arbitrary memory in every handler thread.
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
# Only these inference endpoints may receive an upstream credential.  Keeping the router as a
# narrow protocol adapter prevents a local client from turning it into an authenticated proxy
# for arbitrary vendor paths (including encoded traversal or account/admin endpoints).
INFERENCE_PATHS: dict[str, tuple[str, str]] = {
    "/responses": ("responses", ""),
    "/v1/responses": ("responses", ""),
    "/responses/compact": ("responses", "/compact"),
    "/v1/responses/compact": ("responses", "/compact"),
    "/messages": ("messages", ""),
    "/v1/messages": ("messages", ""),
    "/messages/count_tokens": ("messages", "/count_tokens"),
    "/v1/messages/count_tokens": ("messages", "/count_tokens"),
}
COUNT_TOKENS_PATHS = frozenset({"/messages/count_tokens", "/v1/messages/count_tokens"})
# Responses compact is a distinct state-changing/metadata operation.  The juno adapter
# only translates ordinary Responses generations to Anthropic Messages; mapping compact onto
# /v1/messages would silently turn a non-generation request into a billable generation.
COMPACT_PATHS = frozenset({"/responses/compact", "/v1/responses/compact"})
# Statuses that mean "this gateway has no count_tokens route" rather than "not right now".
# Every one of them is an answer about the route itself, so it is safe to remember: 404 no
# such path, 405 wrong method for a path that exists, 501 not implemented.
COUNT_TOKENS_MISSING_STATUSES = frozenset({404, 405, 501})
# These statuses are used only for replay-safe metadata/discovery calls. Billable generation
# requests never enter the retry path, regardless of status or Idempotency-Key.
SAME_VENDOR_RETRY_STATUSES = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 529})
SAME_VENDOR_RETRY_BACKOFF = (0.4, 1.2)
# Failures that provably happened before any request bytes left this machine: the port
# refused the connection or DNS failed. No generation was started, so retrying them cannot
# double-bill. An SSLError is NOT unambiguously pre-request: urllib raises it both when the
# TLS handshake dies (nothing sent) and when the gateway closes the connection after
# processing the request while we wait for headers (sent, possibly billed — observed dying
# 16 s in on this install's flakiest gateway). Only a fast SSLError, inside the handshake
# window below, is treated as safe to retry; a slow one is treated as sent.
PRE_REQUEST_FAILURE_CLASSES = (ConnectionRefusedError, socket.gaierror)
PRE_REQUEST_SSL_HANDSHAKE_WINDOW = 3.0

# Events that make a relayed SSE stream "state-bearing" for the client: once any of these
# has been forwarded, silently retrying the request would duplicate visible content or
# double-terminal events, so only streams that carried nothing but advisory traffic
# (keep-alive comments, ping, rate-limit notices) may be retried invisibly.
STREAM_STATE_MARKERS = (
    b"response.created",
    b"response.output",
    b"response.reasoning",
    b"response.function_call",
    b"response.custom_tool",
    b"message_start",
    b"content_block",
    b'"type":"error"',
    b'"type": "error"',
)
# Only retry a *fast* failure. Retrying a 120s read timeout is how a slow gateway becomes a
# hammered one: the retries pile onto an upstream that is merely busy, its relay starts
# answering "no channel available" to everything, and that outage is precisely the symptom
# this path exists to prevent. A rejection that arrived in under this many seconds is a
# decision, not a queue.
SAME_VENDOR_RETRY_MAX_ELAPSED = 20.0

# POST /responses, /messages, and compact are all conservatively considered billable. The
# router cannot know whether a gateway charged a request whose response was lost, and a vendor
# may ignore Idempotency-Key. The only inference POST that is explicitly replay-safe is
# Anthropic count_tokens, which is handled locally or as metadata by the caller below.
BILLABLE_GENERATION_PATHS = frozenset(
    {
        "/responses",
        "/v1/responses",
        "/responses/compact",
        "/v1/responses/compact",
        "/messages",
        "/v1/messages",
    }
)

# Every Anthropic-protocol call carries anthropic-version, and the official SDKs pin this exact
# value as a default header on every request; a gateway that enforces it answers 400 without one.
# Claude Desktop sends its own, which is forwarded untouched -- this default only covers a caller
# that omits it (curl, a script, a client that otherwise only speaks OpenAI).
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
# Anthropic's own model list uses the Unix epoch as its "release date unknown" sentinel.  A
# third-party gateway model has no release date to report, so say that in the field's own
# vocabulary instead of inventing a date or omitting a field its SDKs expect on every entry.
UNKNOWN_MODEL_CREATED_AT = "1970-01-01T00:00:00Z"
# Claude Desktop's 1M-context picker entry is a *second* model whose id is the base slug with a
# literal `[1m]` glued on -- the app builds it as `${id}[1m]` whenever the 3P profile sets
# supports1m, and its renderer carries both the suffixed and the unsuffixed form around.  Which
# form reaches a gateway depends on the code path that sends the turn, so the router accepts
# either: the suffix is stripped at the request boundary and everything after it -- routing,
# failover, forced-fast, telemetry, the id sent upstream -- sees the plain slug.  Nothing has to
# be registered under the alias, and GET /v1/models keeps advertising real ids only.
CONTEXT_1M_SUFFIX = "[1m]"
# The client going away mid-stream is not an upstream fault. Windows reports it as any
# of these, and a stalled reader shows up as a socket timeout on our own write.
CLIENT_GONE_ERRORS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
    TimeoutError,
)
# Token accounting is sniffed out of the response the router is already pumping, never by
# re-reading or buffering the whole body.  Both protocols put what we need at a predictable
# end of the stream, but not the same end: an Anthropic SSE stream reports input_tokens in
# its very first `message_start` event and output_tokens in the last `message_delta`, while a
# Responses stream carries the whole usage block in the closing `response.completed`.  So we
# keep a small head window and a slightly larger tail window and look in both.
USAGE_HEAD_BYTES = 16 * 1024
USAGE_TAIL_BYTES = 64 * 1024
# Keep the request log bounded.  It is append-only telemetry that nothing prunes, so without
# this it grows for the life of the install and every panel that tails it gets slower.
LOG_MAX_BYTES = 8 * 1024 * 1024
# This is deliberately a provider marker instead of a global protocol switch.  Codex still
# talks Responses to the local router and keeps the normal `juno--...` model slugs; only
# that provider's outbound request is translated to the Anthropic Messages API.
JUNO_ADAPTER = "responses_to_anthropic_messages"
CHAT_COMPLETIONS_ADAPTER = "responses_to_chat_completions"
MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER = "messages_to_chat_completions"
JUNO_CODEX_USER_AGENT = (
    "codex_cli_rs/0.144.1 (Windows 11.0.26200; x86_64) WindowsTerminal"
)

# Codex `reasoning.effort` -> Anthropic `thinking.budget_tokens`.  The Messages wire has no
# "effort" concept; budget is the only knob, and a gateway that honours it requires
# max_tokens to exceed the budget, so callers raise max_tokens alongside.
REASONING_EFFORT_BUDGETS = {
    "minimal": 1024,
    "low": 1024,
    "medium": 2048,
    "high": 4096,
    "xhigh": 6144,
    "max": 8192,
    "ultra": 8192,
}


def is_juno_adapter(provider: dict[str, Any]) -> bool:
    return (
        str(provider.get("id") or "").lower() == "juno"
        and provider.get("request_adapter") == JUNO_ADAPTER
    )


def is_messages_to_chat_adapter(provider: dict[str, Any]) -> bool:
    """Whether this provider is an OpenAI-Chat-only gateway bridged for Claude Desktop.

    Mirror of the responses bridge: Anthropic Messages in, Chat Completions out, and the
    gateway's answer translated back into Anthropic SSE.
    """
    return (
        str(provider.get("request_adapter") or "").strip()
        == MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER
    )


def is_chat_completions_adapter(provider: dict[str, Any]) -> bool:
    """Whether this provider is an OpenAI-Chat-only gateway bridged for the Codex App.

    Not id-gated: the whole point is that any such vendor can be configured this way.  The
    bridge translates Responses (the Codex App's protocol) into Chat Completions requests
    and the gateway's answers back into Responses streams.
    """
    return (
        str(provider.get("request_adapter") or "").strip()
        == CHAT_COMPLETIONS_ADAPTER
    )


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_from_content(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("output"), str):
            return value["output"]
        if "content" in value:
            return _text_from_content(value["content"])
    return ""


def _anthropic_image_block(block: dict[str, Any]) -> dict[str, Any] | None:
    url = block.get("image_url") or block.get("url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:") and ";base64," in url:
        header, data = url.split(",", 1)
        media_type = header[5:].split(";", 1)[0] or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _anthropic_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize Responses content items to Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str):
                blocks.append({"type": "text", "text": text})
        elif kind in {"input_image", "image_url", "image"}:
            image = _anthropic_image_block(item)
            if image:
                blocks.append(image)
        elif kind == "tool_use":
            tool_input = item.get("input")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(item.get("id") or "call_unknown"),
                    "name": str(item.get("name") or "tool"),
                    "input": tool_input if isinstance(tool_input, dict) else {},
                }
            )
        elif kind == "tool_result":
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(item.get("tool_use_id") or ""),
                    "content": str(item.get("content") or ""),
                }
            )
        elif kind in {"input_file", "file"}:
            # Anthropic gateways vary in document support.  Keep the useful filename/text
            # rather than sending an OpenAI-only block that makes the whole request invalid.
            text = _text_from_content(item.get("filename") or item.get("file_id"))
            if text:
                blocks.append({"type": "text", "text": f"[file: {text}]"})
    return blocks


def _append_anthropic_message(messages: list[dict[str, Any]], role: str, content: Any) -> None:
    blocks = _anthropic_content_blocks(content)
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    if messages and messages[-1].get("role") == role:
        previous = messages[-1].get("content")
        if isinstance(previous, list):
            previous.extend(blocks)
            return
    messages.append({"role": role, "content": blocks})


def _anthropic_tool_definitions(tools: Any) -> list[dict[str, Any]]:
    """Flatten Responses function/namespace tools into Anthropic tool definitions.

    Codex's dynamic tool registry uses ``inputSchema`` (camel case), while the older
    OpenAI-compatible shape uses ``parameters``.  The namespace is tracked separately by
    ``_response_tool_namespaces`` because Anthropic returns only the tool name in ``tool_use``
    blocks and descriptions are model-visible text, not a reliable correlation channel.
    """
    result: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return result

    def append_function(fn: dict[str, Any], namespace: str = "") -> None:
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return
        schema = fn.get("parameters")
        if not isinstance(schema, dict):
            schema = fn.get("inputSchema")
        if not isinstance(schema, dict):
            schema = fn.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        result.append(
            {
                "name": name,
                "description": str(fn.get("description") or ""),
                "input_schema": schema,
            }
        )

    def append_custom(fn: dict[str, Any], namespace: str = "") -> None:
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return
        description = str(fn.get("description") or "")
        tool_format = fn.get("format")
        if isinstance(tool_format, dict):
            format_type = str(tool_format.get("type") or "")
            if format_type == "grammar":
                syntax = str(tool_format.get("syntax") or "text")
                definition = tool_format.get("definition")
                if isinstance(definition, str) and definition:
                    description = (
                        f"{description}\nInput format: {syntax} grammar.\n{definition}"
                    ).strip()
            elif format_type == "text":
                description = f"{description}\nInput is unconstrained text.".strip()
        description = (
            f"{description}\nPass the custom tool input as the single string field `input`."
        ).strip()
        # Anthropic Messages requires object-shaped tool inputs.  Wrap a Responses custom
        # tool's free-form string in one required property; the response adapter unwraps it.
        result.append(
            {
                "name": name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                    "additionalProperties": False,
                },
            }
        )

    for item in tools:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function":
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            append_function(fn)
        elif kind == "namespace":
            namespace = str(item.get("name") or "namespace")
            nested = item.get("tools")
            if not isinstance(nested, list):
                continue
            for nested_item in nested:
                if not isinstance(nested_item, dict):
                    continue
                nested_kind = nested_item.get("type")
                fn = nested_item.get("function")
                if nested_kind == "custom":
                    append_custom(nested_item, namespace)
                elif nested_kind in {None, "function"}:
                    append_function(fn if isinstance(fn, dict) else nested_item, namespace)
        elif kind == "custom":
            append_custom(item, str(item.get("namespace") or ""))
        elif kind == "function_call":
            # A few Responses clients put a function definition directly under a wrapper.
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            append_function(fn, str(item.get("namespace") or ""))
    return result


def _response_tools(payload: dict[str, Any]) -> list[Any]:
    """Return the complete Responses tool list, including Codex's dynamic tool envelope.

    Codex App sends its runtime tools as an ``input`` item with type
    ``additional_tools``.  They are not placed in the top-level Responses ``tools``
    field, so an adapter that only reads ``payload["tools"]`` silently gives an
    Anthropic provider no tools at all.  Keep this normalization local to the
    juno conversion path; other providers continue to receive the original
    payload unchanged.
    """
    result = list(payload.get("tools") or []) if isinstance(payload.get("tools"), list) else []
    input_value = payload.get("input")
    input_items = input_value if isinstance(input_value, list) else [input_value]
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            continue
        nested = item.get("tools")
        if isinstance(nested, list):
            result.extend(nested)
    return result


def _response_tool_namespaces(tools: Any) -> dict[str, str]:
    """Return leaf tool name -> Codex namespace for the current request.

    Anthropic's ``tool_use`` response has no namespace field.  Codex's namespace tools are
    normally globally unique by leaf name; if a malformed request contains an ambiguous
    duplicate, omit it so we do not claim the call belongs to the wrong namespace.
    """
    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    if not isinstance(tools, list):
        return mapping
    for item in tools:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "namespace":
            namespace = str(item.get("name") or "namespace")
            nested = item.get("tools")
            if not isinstance(nested, list):
                continue
            for nested_item in nested:
                if not isinstance(nested_item, dict):
                    continue
                fn = nested_item.get("function")
                fn = fn if isinstance(fn, dict) else nested_item
                name = fn.get("name")
                if not isinstance(name, str) or not name:
                    continue
                previous = mapping.get(name)
                if previous is not None and previous != namespace:
                    ambiguous.add(name)
                else:
                    mapping[name] = namespace
        elif kind in {"function", "function_call", "custom"}:
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            name = fn.get("name")
            if isinstance(name, str) and name:
                namespace = str(item.get("namespace") or "")
                if namespace:
                    previous = mapping.get(name)
                    if previous is not None and previous != namespace:
                        ambiguous.add(name)
                    else:
                        mapping[name] = namespace
    for name in ambiguous:
        mapping.pop(name, None)
    return mapping


def _response_tool_kinds(tools: Any) -> dict[str, str]:
    """Return leaf tool name -> ``custom``/``function`` for response translation."""
    mapping: dict[str, str] = {}
    if not isinstance(tools, list):
        return mapping

    def add(item: Any, default_kind: str = "function") -> None:
        if not isinstance(item, dict):
            return
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name")
        if isinstance(name, str) and name:
            kind = "custom" if item.get("type") == "custom" else default_kind
            # An ambiguous name is intentionally removed; otherwise a response from
            # Anthropic could be attributed to the wrong Responses tool kind.
            if name in mapping and mapping[name] != kind:
                mapping.pop(name, None)
            elif name not in mapping:
                mapping[name] = kind

    for item in tools:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "namespace":
            nested = item.get("tools")
            if isinstance(nested, list):
                for nested_item in nested:
                    add(nested_item)
        elif item.get("type") in {"function", "custom", "function_call"}:
            add(item)
    return mapping


def _chat_tool_definitions(tools: Any) -> list[dict[str, Any]]:
    """Flatten Responses function/namespace/custom tools into Chat Completions tools.

    The same shapes the Messages adapter handles: namespace tools carry leaf functions,
    custom tools (a free-form string input) are expressed as one required string field
    named ``input`` so the response side can unwrap them again.
    """
    result: list[dict[str, Any]] = []

    def append_function(fn: dict[str, Any]) -> None:
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return
        schema = fn.get("parameters")
        if not isinstance(schema, dict):
            schema = fn.get("inputSchema")
        if not isinstance(schema, dict):
            schema = fn.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(fn.get("description") or ""),
                    "parameters": schema,
                },
            }
        )

    def append_custom(fn: dict[str, Any]) -> None:
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return
        description = str(fn.get("description") or "")
        tool_format = fn.get("format")
        if isinstance(tool_format, dict):
            format_type = str(tool_format.get("type") or "")
            if format_type == "grammar":
                syntax = str(tool_format.get("syntax") or "text")
                definition = tool_format.get("definition")
                if isinstance(definition, str) and definition:
                    description = f"{description}\nInput format: {syntax} grammar.\n{definition}".strip()
            elif format_type == "text":
                description = f"{description}\nInput is unconstrained text.".strip()
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (
                        f"{description}\nPass the custom tool input as the single string field `input`."
                    ).strip(),
                    "parameters": {
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                        "required": ["input"],
                        "additionalProperties": False,
                    },
                },
            }
        )

    if not isinstance(tools, list):
        return result
    for item in tools:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function":
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            append_function(fn)
        elif kind == "namespace":
            nested = item.get("tools")
            if not isinstance(nested, list):
                continue
            for nested_item in nested:
                if not isinstance(nested_item, dict):
                    continue
                nested_kind = nested_item.get("type")
                fn = nested_item.get("function")
                if nested_kind == "custom":
                    append_custom(nested_item)
                elif nested_kind in {None, "function"}:
                    append_function(fn if isinstance(fn, dict) else nested_item)
        elif kind == "custom":
            append_custom(item)
        elif kind == "function_call":
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            append_function(fn)
    return result


def _anthropic_content_to_chat_parts(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """Split one Anthropic message content into (plain text, image parts)."""
    text_parts: list[str] = []
    parts: list[dict[str, Any]] = []
    if isinstance(content, str):
        return content, parts
    items = content if isinstance(content, list) else [content]
    for item in items:
        if isinstance(item, str):
            text_parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            text_parts.append(str(item.get("text") or ""))
        elif kind == "image" and isinstance(item.get("source"), dict):
            source = item["source"]
            if source.get("type") == "base64" and source.get("data"):
                media = str(source.get("media_type") or "image/png")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media};base64,{source['data']}"},
                    }
                )
            elif source.get("type") == "url" and source.get("url"):
                parts.append({"type": "image_url", "image_url": {"url": str(source["url"])}})
    return "\n".join(piece for piece in text_parts if piece), parts


def _chat_tools_from_anthropic(tools: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": schema,
                },
            }
        )
    return result


def messages_to_chat_payload(payload: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    """Translate one Anthropic Messages request into an OpenAI Chat Completions request.

    Thinking budgets are deliberately not forwarded: there is no faithful budget -> effort
    mapping and a wrong guess would either be ignored or rejected; these chat models reason
    by themselves.  Claude Desktop's slider therefore has no effect through this bridge.
    """
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system is not None:
        text, _parts = _anthropic_content_to_chat_parts(system)
        if text:
            messages.append({"role": "system", "content": text})

    source_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    for message in source_messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        items = content if isinstance(content, list) else [content]
        text, image_parts = _anthropic_content_to_chat_parts(content)
        tool_uses = [item for item in items if isinstance(item, dict) and item.get("type") == "tool_use"]
        tool_results = [item for item in items if isinstance(item, dict) and item.get("type") == "tool_result"]

        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_uses:
                entry["tool_calls"] = [
                    {
                        "id": str(use.get("id") or f"call_{index}"),
                        "type": "function",
                        "function": {
                            "name": str(use.get("name") or "tool"),
                            "arguments": json.dumps(
                                use.get("input") if isinstance(use.get("input"), dict) else {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for index, use in enumerate(tool_uses)
                ]
            messages.append(entry)
            continue

        # user role: tool results become their own tool messages, everything else is a
        # normal user turn.  Anthropic requires tool_result blocks to lead the message.
        if tool_results:
            for result in tool_results:
                result_content = result.get("content")
                result_text, _ = _anthropic_content_to_chat_parts(result_content)
                if not result_text and isinstance(result_content, str):
                    result_text = result_content
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(result.get("tool_use_id") or ""),
                        "content": result_text,
                    }
                )
            if text:
                messages.append({"role": "user", "content": text})
            continue
        if image_parts:
            messages.append(
                {"role": "user", "content": ([{"type": "text", "text": text}] if text else []) + image_parts}
            )
        else:
            messages.append({"role": "user", "content": text})

    if not messages:
        messages.append({"role": "user", "content": ""})

    result: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": max(1, int(payload.get("max_tokens") or 4096)),
        "stream": bool(payload.get("stream")),
    }
    if payload.get("stream"):
        result["stream_options"] = {"include_usage": True}
    for key in ("temperature", "top_p"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    stop_sequences = payload.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        result["stop"] = stop_sequences
    tools = _chat_tools_from_anthropic(payload.get("tools"))
    if tools:
        result["tools"] = tools
        choice = payload.get("tool_choice")
        if isinstance(choice, dict):
            kind = str(choice.get("type") or "auto")
            if kind == "any":
                result["tool_choice"] = "required"
            elif kind == "tool" and choice.get("name"):
                result["tool_choice"] = {
                    "type": "function",
                    "function": {"name": str(choice["name"])},
                }
            elif kind == "none":
                result.pop("tools", None)
            else:
                result["tool_choice"] = "auto"
        else:
            result["tool_choice"] = "auto"
    return result


def responses_to_chat_payload(payload: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    """Translate one Codex Responses request into an OpenAI Chat Completions request."""
    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if instructions:
        text = _text_from_content(instructions)
        if text:
            messages.append({"role": "system", "content": text})

    input_value = payload.get("input", "")
    input_items = input_value if isinstance(input_value, list) else [input_value]
    for item in input_items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        role = str(item.get("role") or "user")
        if kind == "message":
            text = _text_from_content(item.get("content"))
            if role in {"developer", "system"}:
                if text:
                    messages.append({"role": "system", "content": text})
            else:
                messages.append(
                    {"role": "assistant" if role == "assistant" else "user", "content": text}
                )
        elif kind in {"input_text", "text"}:
            messages.append({"role": "user", "content": str(item.get("text") or "")})
        elif kind in {"function_call", "custom_tool_call", "tool_use"}:
            name = str(item.get("name") or "tool")
            arguments = item.get("arguments", item.get("input", "{}"))
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            call_id = str(item.get("call_id") or item.get("id") or "call_unknown")
            if kind == "custom_tool_call":
                arguments = json.dumps({"input": arguments}, ensure_ascii=False)
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
        elif kind in {"function_call_output", "custom_tool_call_output", "tool_result"}:
            output = item.get("output", item.get("content", ""))
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(
                        item.get("call_id") or item.get("tool_use_id") or item.get("id") or ""
                    ),
                    "content": output,
                }
            )
        elif kind == "reasoning":
            # Chat Completions has no reasoning replay channel; summaries are advisory and
            # safe to drop (the upstream never sees a plain text echo of its own thoughts).
            continue
        elif "content" in item:
            text = _text_from_content(item["content"])
            messages.append(
                {"role": "assistant" if role == "assistant" else "user", "content": text}
            )

    if not messages:
        messages.append({"role": "user", "content": ""})

    max_tokens = max(
        1, int(payload.get("max_output_tokens") or payload.get("max_tokens") or 4096)
    )
    result: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": bool(payload.get("stream")),
    }
    if payload.get("stream"):
        # Without this most gateways omit usage from the stream entirely; harmless where
        # the field is ignored.
        result["stream_options"] = {"include_usage": True}
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = str(reasoning.get("effort") or "").strip().lower()
        if effort and effort != "none":
            result["reasoning_effort"] = effort
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if key in payload and payload[key] is not None:
            result[key] = payload[key]
    stop = payload.get("stop")
    if stop is not None:
        result["stop"] = stop if isinstance(stop, list) else [stop]
    tools = _chat_tool_definitions(_response_tools(payload))
    if tools:
        result["tools"] = tools
        choice = payload.get("tool_choice")
        if choice == "none":
            result.pop("tools", None)
        elif choice == "required":
            result["tool_choice"] = "required"
        elif isinstance(choice, dict):
            name = choice.get("name")
            if not name and isinstance(choice.get("function"), dict):
                name = choice["function"].get("name")
            result["tool_choice"] = (
                {"type": "function", "function": {"name": str(name)}}
                if name
                else "auto"
            )
        else:
            result["tool_choice"] = "auto"
    return result


def responses_to_anthropic_payload(payload: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    """Translate one Codex Responses request to the provider's Messages shape."""
    messages: list[dict[str, Any]] = []
    system_parts: list[str] = []
    instructions = payload.get("instructions")
    if instructions:
        text = _text_from_content(instructions)
        if text:
            system_parts.append(text)

    input_value = payload.get("input", "")
    input_items = input_value if isinstance(input_value, list) else [input_value]
    for item in input_items:
        if isinstance(item, str):
            _append_anthropic_message(messages, "user", item)
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        role = str(item.get("role") or "user")
        if kind == "message":
            if role in {"developer", "system"}:
                text = _text_from_content(item.get("content"))
                if text:
                    system_parts.append(text)
            else:
                _append_anthropic_message(messages, "assistant" if role == "assistant" else "user", item.get("content"))
        elif kind in {"input_text", "text"}:
            _append_anthropic_message(messages, "user", item.get("text", ""))
        elif kind in {"function_call", "custom_tool_call", "tool_use"}:
            name = str(item.get("name") or "tool")
            arguments = item.get("arguments", item.get("input", "{}"))
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if kind == "custom_tool_call":
                input_data = {"input": arguments}
            else:
                try:
                    input_data = json.loads(arguments) if arguments else {}
                except (ValueError, TypeError):
                    input_data = {"raw_arguments": arguments}
            _append_anthropic_message(
                messages,
                "assistant",
                [{
                    "type": "tool_use",
                    "id": str(item.get("call_id") or item.get("id") or "call_unknown"),
                    "name": name,
                    "input": input_data,
                }],
            )
        elif kind in {"function_call_output", "custom_tool_call_output", "tool_result"}:
            output = item.get("output", item.get("content", ""))
            if not isinstance(output, (str, list, dict)):
                output = str(output)
            _append_anthropic_message(
                messages,
                "user",
                [{
                    "type": "tool_result",
                    "tool_use_id": str(
                        item.get("call_id")
                        or item.get("tool_use_id")
                        or item.get("id")
                        or ""
                    ),
                    "content": (
                        output
                        if isinstance(output, str)
                        else json.dumps(output, ensure_ascii=False, separators=(",", ":"))
                    ),
                }],
            )
        elif kind == "reasoning":
            # Encrypted reasoning blocks are not valid Anthropic input.  The visible summary,
            # when present, is safe to carry as assistant text.
            summary = _text_from_content(item.get("summary"))
            if summary:
                _append_anthropic_message(messages, "assistant", summary)
        elif "content" in item:
            _append_anthropic_message(messages, "assistant" if role == "assistant" else "user", item["content"])

    if not messages:
        messages.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    elif messages[0].get("role") != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    max_tokens = max(
        1, int(payload.get("max_output_tokens") or payload.get("max_tokens") or 4096)
    )
    result: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": bool(payload.get("stream")),
    }
    # Codex asks the model to reason with `reasoning.effort`; Anthropic-shaped gateways spell
    # that `thinking`.  Dropping the field used to mean every request ran at the gateway's
    # default effort -- the model answered with no visible reasoning at all.  Gateways that
    # do not know the field ignore it, and the ones that honour it require
    # max_tokens > budget_tokens, so raise the cap before a strict gateway can reject.
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        budget = REASONING_EFFORT_BUDGETS.get(str(reasoning.get("effort") or "").lower())
        if budget:
            result["max_tokens"] = max(max_tokens, budget + 2048)
            result["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p", "metadata"):
        if key in payload and payload[key] is not None:
            result[key] = payload[key]
    if payload.get("stop") is not None:
        stops = payload["stop"] if isinstance(payload["stop"], list) else [payload["stop"]]
        result["stop_sequences"] = [str(value) for value in stops[:4] if value is not None]
    tools = _anthropic_tool_definitions(_response_tools(payload))
    if tools:
        result["tools"] = tools
        choice = payload.get("tool_choice")
        if choice == "none":
            result.pop("tools", None)
        elif choice == "required":
            result["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict):
            name = choice.get("name")
            if not name and isinstance(choice.get("function"), dict):
                name = choice["function"].get("name")
            result["tool_choice"] = (
                {"type": "tool", "name": str(name)} if name else {"type": "auto"}
            )
        else:
            result["tool_choice"] = {"type": "auto"}
    return result


def _response_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return {
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, input_tokens + output_tokens),
    }


_CHAT_STOP_REASONS_TO_ANTHROPIC = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def chat_completion_to_anthropic_message(
    completion: dict[str, Any], model: str = ""
) -> dict[str, Any]:
    """Translate one Chat Completions response into an Anthropic Messages object.

    Reasoning output is intentionally dropped: Anthropic thinking blocks carry a
    ``signature`` this bridge cannot produce, and an unsigned block risks the client
    rejecting the whole answer.  The visible text is what the user asked for.
    """
    choices = completion.get("choices") if isinstance(completion.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) and arguments else {}
        except (ValueError, TypeError):
            parsed = {"raw_arguments": arguments}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or f"toolu_{index}"),
                "name": str(function.get("name") or "tool"),
                "input": parsed,
            }
        )
    if not content:
        content.append({"type": "text", "text": ""})
    usage = completion.get("usage") if isinstance(completion.get("usage"), dict) else {}
    # Anthropic's contract: a message carrying tool_use blocks stops with reason
    # "tool_use".  Gateways are inconsistent here (many report finish_reason "stop"
    # even when they emitted tool calls), and clients that gate on the stop reason
    # would silently never run the tool.
    has_tool_use = any(block.get("type") == "tool_use" for block in content)
    stop_reason = _CHAT_STOP_REASONS_TO_ANTHROPIC.get(
        str(choice.get("finish_reason") or ""), "end_turn"
    )
    if has_tool_use:
        stop_reason = "tool_use"
    return {
        "id": "msg_" + str(completion.get("id") or int(time.time() * 1000)).removeprefix("chatcmpl-"),
        "type": "message",
        "role": "assistant",
        "model": model or str(completion.get("model") or ""),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def chat_message_to_response(
    completion: dict[str, Any],
    model: str = "",
    tool_namespaces: dict[str, str] | None = None,
    tool_kinds: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Translate one Chat Completions response into a Responses-API object."""
    choices = completion.get("choices") if isinstance(completion.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    finish_reason = str(choice.get("finish_reason") or "")
    response_id = "resp_" + str(
        completion.get("id") or int(time.time() * 1000)
    ).removeprefix("chatcmpl-")
    output: list[dict[str, Any]] = []
    text_parts: list[str] = []

    reasoning = message.get("reasoning_content")
    if not (isinstance(reasoning, str) and reasoning):
        # Gateways disagree on the name; some relays use a plain "reasoning".
        reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        output.append(
            {
                "id": f"rs_{response_id.removeprefix('resp_')}_0",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    content = message.get("content")
    if isinstance(content, str) and content:
        text_parts.append(content)
        output.append(
            {
                "id": f"msg_{response_id.removeprefix('resp_')}_0",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        tool_name = str(function.get("name") or "tool")
        arguments = function.get("arguments")
        arguments = arguments if isinstance(arguments, str) else json.dumps(
            arguments or {}, ensure_ascii=False, separators=(",", ":")
        )
        call_id = str(call.get("id") or f"call_{index}")
        namespace = (tool_namespaces or {}).get(tool_name, "")
        is_custom = (tool_kinds or {}).get(tool_name) == "custom"
        if is_custom:
            try:
                parsed = json.loads(arguments)
                custom_input = parsed.get("input", arguments) if isinstance(parsed, dict) else arguments
            except (ValueError, TypeError):
                custom_input = arguments
            if not isinstance(custom_input, str):
                custom_input = json.dumps(custom_input, ensure_ascii=False, separators=(",", ":"))
            item: dict[str, Any] = {
                "id": f"ctc_{response_id.removeprefix('resp_')}_{index}",
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": tool_name,
                "input": custom_input,
            }
        else:
            item = {
                "id": f"fc_{response_id.removeprefix('resp_')}_{index}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": tool_name,
                "arguments": arguments,
            }
        if namespace:
            item["namespace"] = namespace
        output.append(item)

    usage = _response_usage(completion.get("usage"))
    status = "incomplete" if finish_reason == "length" else "completed"
    result: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": int(completion.get("created") or time.time()),
        "model": model or str(completion.get("model") or ""),
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "output_text": "".join(text_parts),
        "usage": usage,
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


def anthropic_message_to_response(
    message: dict[str, Any],
    model: str = "",
    tool_namespaces: dict[str, str] | None = None,
    tool_kinds: dict[str, str] | None = None,
) -> dict[str, Any]:
    response_id = str(message.get("id") or ("msg_" + str(int(time.time() * 1000))))
    response_id = response_id if response_id.startswith("resp_") else "resp_" + response_id
    output: list[dict[str, Any]] = []
    text_parts: list[str] = []
    content = message.get("content") if isinstance(message.get("content"), list) else []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            # Surface the gateway's extended thinking as a Responses reasoning item so the
            # client shows the model actually reasoning instead of jumping straight to text.
            thinking_text = str(block.get("thinking") or "")
            output.append(
                {
                    "id": f"rs_{response_id.removeprefix('resp_')}_{index}",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": thinking_text}],
                }
            )
        elif kind == "redacted_thinking":
            output.append(
                {
                    "id": f"rs_{response_id.removeprefix('resp_')}_{index}",
                    "type": "reasoning",
                    "summary": [],
                }
            )
        elif kind == "text":
            text = str(block.get("text") or "")
            if not text:
                # Gateways that open with an empty text block would otherwise become an
                # empty assistant message item in the response output.
                continue
            text_parts.append(text)
            output.append(
                {
                    "id": f"msg_{response_id.removeprefix('resp_')}_{index}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        elif kind == "tool_use":
            raw_input = block.get("input", {})
            call_id = str(block.get("id") or f"call_{index}")
            tool_name = str(block.get("name") or "tool")
            namespace = (tool_namespaces or {}).get(tool_name, "")
            is_custom = (tool_kinds or {}).get(tool_name) == "custom"
            if is_custom:
                if isinstance(raw_input, dict) and "input" in raw_input:
                    custom_input = raw_input.get("input")
                else:
                    custom_input = raw_input
                if not isinstance(custom_input, str):
                    custom_input = json.dumps(custom_input, ensure_ascii=False, separators=(",", ":"))
                output_item = {
                    "id": f"ctc_{response_id.removeprefix('resp_')}_{index}",
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": tool_name,
                    "input": custom_input,
                }
            else:
                arguments = raw_input
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                output_item = {
                    "id": f"fc_{response_id.removeprefix('resp_')}_{index}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": tool_name,
                    "arguments": arguments,
                }
            if namespace:
                output_item["namespace"] = namespace
            output.append(
                output_item
            )
    usage = _response_usage(message.get("usage"))
    stop_reason = message.get("stop_reason")
    status = "incomplete" if stop_reason in {"max_tokens", "stop_sequence"} else "completed"
    result: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model or message.get("model") or "",
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "output_text": "".join(text_parts),
        "usage": usage,
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


def friendly_upstream_error(detail: str, vendor: str = "") -> str:
    """A client-facing one-liner for an upstream failure.

    Technical detail (exception class, byte counts) belongs in the router log where it can
    be diagnosed; the apps should show who failed and in what way, never a Python
    traceback.  Gateway-authored messages (an HTML-free JSON error body, an SSE error
    event) are kept verbatim -- they are the vendor's own words, not ours.
    """
    text = str(detail)
    if "IncompleteRead" in text:
        reason = "响应传输不完整（连接中途断开）"
    elif "timed out" in text or "TimeoutError" in text:
        reason = "响应超时"
    elif "SSL" in text:
        reason = "加密连接被中断"
    elif "Remote end closed" in text or "RemoteDisconnected" in text:
        reason = "提前关闭了连接"
    elif "ConnectionReset" in text:
        reason = "重置了连接"
    elif "refused" in text or "gaierror" in text:
        reason = "无法连接"
    elif "truncated" in text or "ended without" in text:
        reason = "截断了响应流"
    else:
        reason = "连接中断"
    return f"上游 {vendor} {reason}" if vendor else f"上游{reason}"


def _sse_frame(
    event: str, payload: dict[str, Any], response_id: str | None = None
) -> bytes:
    """Encode one Responses SSE event, adding the correlation id when known.

    The Responses stream schema puts ``response_id`` on each incremental event (not only
    inside the embedded response object).  A few clients tolerate its absence, but Codex's
    streaming reducer uses it to associate output-item and argument deltas with the response.
    Keep the argument optional so the generic helper remains usable by the non-adapted paths.
    """
    if response_id and event != "[DONE]":
        payload = dict(payload)
        payload.setdefault("response_id", response_id)
    return (
        f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def response_to_sse(response: dict[str, Any]) -> list[bytes]:
    """Turn a completed response into a valid Responses event sequence."""
    events: list[bytes] = []
    response_id = str(response.get("id") or "resp_local")
    created = dict(response)
    created["status"] = "in_progress"
    created["output"] = []
    events.append(
        _sse_frame(
            "response.created",
            {"type": "response.created", "response": created},
            response_id,
        )
    )
    for output_index, item in enumerate(response.get("output") or []):
        item_copy = dict(item)
        item_copy["status"] = "in_progress"
        if item_copy.get("type") == "function_call":
            # Arguments arrive through the dedicated delta events.  Sending them again in
            # output_item.added makes reducers append the same JSON twice on some clients.
            item_copy["arguments"] = ""
        elif item_copy.get("type") == "custom_tool_call":
            # Custom tool input arrives through the dedicated custom-tool delta events.
            item_copy["input"] = ""
        events.append(
            _sse_frame(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": item_copy,
                },
                response_id,
            )
        )
        if item.get("type") == "message":
            for content_index, part in enumerate(item.get("content") or []):
                if part.get("type") != "output_text":
                    continue
                item_id = str(item.get("id") or response_id)
                events.append(
                    _sse_frame(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": content_index,
                            "part": {
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        },
                        response_id,
                    )
                )
                text = str(part.get("text") or "")
                if text:
                    events.append(
                        _sse_frame(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": content_index,
                                "delta": text,
                            },
                            response_id,
                        )
                    )
                events.append(
                    _sse_frame(
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": content_index,
                            "text": text,
                        },
                        response_id,
                    )
                )
                events.append(
                    _sse_frame(
                        "response.content_part.done",
                        {
                            "type": "response.content_part.done",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": content_index,
                            "part": {
                                "type": "output_text",
                                "text": text,
                                "annotations": [],
                            },
                        },
                        response_id,
                    )
                )
        elif item.get("type") == "function_call":
            item_id = str(item.get("id") or f"fc_{output_index}")
            arguments = str(item.get("arguments") or "")
            if arguments:
                events.append(
                    _sse_frame(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "delta": arguments,
                        },
                        response_id,
                    )
                )
            events.append(
                _sse_frame(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "arguments": arguments,
                    },
                    response_id,
                )
            )
        elif item.get("type") == "custom_tool_call":
            item_id = str(item.get("id") or f"ctc_{output_index}")
            custom_input = str(item.get("input") or "")
            if custom_input:
                events.append(
                    _sse_frame(
                        "response.custom_tool_call_input.delta",
                        {
                            "type": "response.custom_tool_call_input.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "delta": custom_input,
                        },
                        response_id,
                    )
                )
            events.append(
                _sse_frame(
                    "response.custom_tool_call_input.done",
                    {
                        "type": "response.custom_tool_call_input.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "input": custom_input,
                    },
                    response_id,
                )
            )
        done = dict(item)
        done["status"] = "completed"
        events.append(
            _sse_frame(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": done,
                },
                response_id,
            )
        )
    events.append(
        _sse_frame(
            "response.completed",
            {"type": "response.completed", "response": response},
            response_id,
        )
    )
    events.append(b"data: [DONE]\n\n")
    return events


def chat_sse_to_anthropic_sse(
    upstream: Any,
    model: str,
    outcome: dict[str, Any] | None = None,
) -> Any:
    """Yield Anthropic Messages SSE events while consuming a Chat Completions stream.

    Completion evidence is ``[DONE]`` or a non-null ``finish_reason``; without either the
    stream is truncated and ``outcome`` says so, so the caller fails the turn instead of
    pretending a half-answer is complete.
    """
    message_id = "msg_" + str(int(time.time() * 1000))
    response_model = model
    started = False
    saw_completion = False
    stop_reason = "end_turn"
    usage: dict[str, int] = {}
    next_block_index = 0
    text_block: int | None = None
    open_tool_blocks: dict[int, int] = {}

    def message_start() -> bytes:
        nonlocal started
        started = True
        return _sse_frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": response_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            None,
        )

    with upstream:
        while True:
            line = upstream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", "replace").rstrip("\r\n")
            if not decoded.startswith("data:"):
                continue
            raw_payload = decoded[5:].strip()
            if not raw_payload:
                continue
            if raw_payload == "[DONE]":
                saw_completion = True
                continue
            try:
                data = json.loads(raw_payload)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("usage"):
                usage.update(data.get("usage") or {})
            if data.get("model"):
                response_model = str(data.get("model"))
            choices = data.get("choices") if isinstance(data.get("choices"), list) else []
            if not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            finish = str(choice.get("finish_reason") or "")
            if finish:
                saw_completion = True
                stop_reason = _CHAT_STOP_REASONS_TO_ANTHROPIC.get(finish, "end_turn")
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                if not started:
                    yield message_start()
                if text_block is None:
                    text_block = next_block_index
                    next_block_index += 1
                    yield _sse_frame(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": text_block,
                            "content_block": {"type": "text", "text": ""},
                        },
                        None,
                    )
                yield _sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": text_block,
                        "delta": {"type": "text_delta", "text": text},
                    },
                    None,
                )
            tool_deltas = delta.get("tool_calls")
            if isinstance(tool_deltas, list):
                for fragment in tool_deltas:
                    if not isinstance(fragment, dict):
                        continue
                    index = int(fragment.get("index") or 0)
                    function = fragment.get("function") if isinstance(fragment.get("function"), dict) else {}
                    if index not in open_tool_blocks:
                        if not started:
                            yield message_start()
                        block_index = next_block_index
                        next_block_index += 1
                        open_tool_blocks[index] = block_index
                        yield _sse_frame(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": block_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": str(fragment.get("id") or f"toolu_{index}"),
                                    "name": str(function.get("name") or "tool"),
                                    "input": {},
                                },
                            },
                            None,
                        )
                    arguments_fragment = function.get("arguments")
                    if isinstance(arguments_fragment, str) and arguments_fragment:
                        yield _sse_frame(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": open_tool_blocks[index],
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": arguments_fragment,
                                },
                            },
                            None,
                        )

        if not started and not saw_completion:
            # Nothing observable and no completion evidence: zero-content death, retryable.
            if outcome is not None:
                outcome["retryable"] = True
            return
        if not started:
            yield message_start()
        # Close every open block, text first, then the terminal delta.
        stop_order = sorted(
            [index for index in [text_block, *open_tool_blocks.values()] if index is not None]
        )
        for index in stop_order:
            yield _sse_frame(
                "content_block_stop", {"type": "content_block_stop", "index": index}, None
            )
        if saw_completion:
            if open_tool_blocks and stop_reason == "end_turn":
                # Same contract as the non-stream path: tool_use blocks mean the turn
                # stopped to run a tool, whatever inconsistent finish_reason the gateway
                # reported.
                stop_reason = "tool_use"
            yield _sse_frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        "input_tokens": int(usage.get("prompt_tokens") or 0),
                        "output_tokens": int(usage.get("completion_tokens") or 0),
                    },
                },
                None,
            )
            yield _sse_frame("message_stop", {"type": "message_stop"}, None)
        else:
            if outcome is not None:
                outcome["truncated"] = True
            yield _sse_frame(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "upstream_stream_truncated",
                        "message": "上游截断了响应流（未收到完成事件）",
                    },
                },
                None,
            )


def chat_sse_to_responses(
    upstream: Any,
    model: str,
    tool_namespaces: dict[str, str] | None = None,
    tool_kinds: dict[str, str] | None = None,
    outcome: dict[str, Any] | None = None,
) -> Any:
    """Yield Responses SSE frames while consuming an OpenAI Chat Completions SSE stream.

    Completion evidence is ``[DONE]`` or a non-null ``finish_reason``; a stream that ends
    with neither is truncated, which ``outcome`` reports so the caller can record a failure
    and the client receives a terminal ``response.failed`` instead of a silent close.
    """
    response_id = "resp_" + str(int(time.time() * 1000))
    response_model = model
    output: list[dict[str, Any]] = []
    text_buffers: dict[str, str] = {}
    usage: dict[str, int] = {}
    created_sent = False
    completed_sent = False
    saw_completion = False
    # Tool calls stream as indexed fragments; the wire format gives the id and the name in
    # the first fragment of each index and argument fragments after it.
    tool_items: dict[int, dict[str, Any]] = {}

    def ensure_created() -> bytes:
        nonlocal created_sent
        if created_sent:
            return b""
        created_sent = True
        response = {
            "id": response_id, "object": "response", "created_at": int(time.time()),
            "model": response_model, "status": "in_progress", "output": [],
            "parallel_tool_calls": True, "tool_choice": "auto", "usage": None,
        }
        return _sse_frame(
            "response.created",
            {"type": "response.created", "response": response},
            response_id,
        )

    def ensure_text_item() -> tuple[dict[str, Any] | None, bytes]:
        """Lazily materialize the assistant message item on the first visible text."""
        nonlocal created_sent
        item = text_items.get("main")
        if item is not None:
            return item, b""
        item = {
            "id": response_id.replace("resp_", "msg_", 1),
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        output.append(item)
        text_items["main"] = item
        text_buffers["main"] = ""
        output_index = len(output) - 1
        item["_output_index"] = output_index
        item["content"].append({"type": "output_text", "text": "", "annotations": []})
        frames = ensure_created()
        frames += _sse_frame(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {k: v for k, v in item.items() if not k.startswith("_")},
            },
            response_id,
        )
        frames += _sse_frame(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": item["id"],
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            response_id,
        )
        return item, frames

    def ensure_reasoning_item() -> tuple[dict[str, Any], bytes]:
        item = reasoning_item.get("main")
        if item is not None:
            return item, b""
        item = {"id": f"rs_{response_id.removeprefix('resp_')}_r", "type": "reasoning", "summary": []}
        output.append(item)
        reasoning_item["main"] = item
        item["_output_index"] = len(output) - 1
        item["summary"].append({"type": "summary_text", "text": ""})
        text_buffers["reasoning"] = ""
        frames = ensure_created()
        frames += _sse_frame(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": item["_output_index"],
                "item": {"id": item["id"], "type": "reasoning", "summary": []},
            },
            response_id,
        )
        frames += _sse_frame(
            "response.reasoning_summary_part.added",
            {
                "type": "response.reasoning_summary_part.added",
                "item_id": item["id"],
                "output_index": item["_output_index"],
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            },
            response_id,
        )
        return item, frames

    text_items: dict[str, dict[str, Any]] = {}
    reasoning_item: dict[str, dict[str, Any]] = {}

    def finish() -> bytes:
        nonlocal completed_sent
        if completed_sent:
            return b""
        completed_sent = True
        clean_output = [
            {k: v for k, v in item.items() if not k.startswith("_")} for item in output
        ]
        response = {
            "id": response_id, "object": "response", "created_at": int(time.time()),
            "model": response_model, "status": "completed", "output": clean_output,
            "parallel_tool_calls": True, "tool_choice": "auto",
            "output_text": "".join(text_buffers.values()),
            "usage": _response_usage(usage),
        }
        return _sse_frame(
            "response.completed",
            {"type": "response.completed", "response": response},
            response_id,
        )

    event_name = ""
    data_lines: list[str] = []
    with upstream:
        while True:
            line = upstream.readline()
            if not line:
                if data_lines:
                    line = b"\n"
                else:
                    break
            decoded = line.decode("utf-8", "replace").rstrip("\r\n")
            if decoded:
                if decoded.startswith("event:"):
                    event_name = decoded[6:].strip()
                elif decoded.startswith("data:"):
                    data_lines.append(decoded[5:].lstrip())
                continue
            if not data_lines:
                event_name = ""
                continue
            raw_payload = "\n".join(data_lines)
            data_lines, event_name = [], ""
            if raw_payload.strip() == "[DONE]":
                saw_completion = True
                continue
            try:
                data = json.loads(raw_payload)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("usage"):
                usage.update(data.get("usage") or {})
            if data.get("model"):
                response_model = str(data.get("model"))
            choices = data.get("choices") if isinstance(data.get("choices"), list) else []
            if not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            if choice.get("finish_reason"):
                saw_completion = True
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            reasoning_text = delta.get("reasoning_content")
            if not (isinstance(reasoning_text, str) and reasoning_text):
                reasoning_text = delta.get("reasoning")
            if isinstance(reasoning_text, str) and reasoning_text:
                item, frames = ensure_reasoning_item()
                text_buffers["reasoning"] = text_buffers.get("reasoning", "") + reasoning_text
                item["summary"][0]["text"] = text_buffers["reasoning"]
                if frames:
                    yield frames
                yield _sse_frame(
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": item["id"],
                        "output_index": item["_output_index"],
                        "summary_index": 0,
                        "delta": reasoning_text,
                    },
                    response_id,
                )
            content_text = delta.get("content")
            if isinstance(content_text, str) and content_text:
                item, frames = ensure_text_item()
                if frames:
                    yield frames
                text_buffers["main"] = text_buffers.get("main", "") + content_text
                item["content"][0]["text"] = text_buffers["main"]
                yield _sse_frame(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": item["_output_index"],
                        "content_index": 0,
                        "delta": content_text,
                    },
                    response_id,
                )
            tool_deltas = delta.get("tool_calls")
            if isinstance(tool_deltas, list):
                for fragment in tool_deltas:
                    if not isinstance(fragment, dict):
                        continue
                    index = int(fragment.get("index") or 0)
                    function = fragment.get("function") if isinstance(fragment.get("function"), dict) else {}
                    record = tool_items.get(index)
                    if record is None:
                        tool_name = str(function.get("name") or "tool")
                        call_id = str(fragment.get("id") or f"call_{response_id.removeprefix('resp_')}_{index}")
                        is_custom = (tool_kinds or {}).get(tool_name) == "custom"
                        item_id = f"{'ctc' if is_custom else 'fc'}_{response_id.removeprefix('resp_')}_{index}"
                        if is_custom:
                            item = {
                                "id": item_id, "type": "custom_tool_call", "status": "in_progress",
                                "call_id": call_id, "name": tool_name, "input": "",
                            }
                        else:
                            item = {
                                "id": item_id, "type": "function_call", "status": "in_progress",
                                "call_id": call_id, "name": tool_name, "arguments": "",
                            }
                        namespace = (tool_namespaces or {}).get(tool_name)
                        if namespace:
                            item["namespace"] = namespace
                        output.append(item)
                        item["_output_index"] = len(output) - 1
                        record = tool_items[index] = {
                            "item": item, "custom": is_custom, "arguments": "",
                            "name_known": bool(function.get("name")),
                        }
                        yield ensure_created()
                        yield _sse_frame(
                            "response.output_item.added",
                            {
                                "type": "response.output_item.added",
                                "output_index": item["_output_index"],
                                "item": {k: v for k, v in item.items() if not k.startswith("_")},
                            },
                            response_id,
                        )
                    item = record["item"]
                    arguments_fragment = function.get("arguments")
                    if not isinstance(arguments_fragment, str) or not arguments_fragment:
                        continue
                    record["arguments"] += arguments_fragment
                    if record["custom"]:
                        # Custom tools are bridged as {"input": <string>}; unwrap on the fly
                        # when a fragment happens to be complete JSON, else pass it through.
                        try:
                            parsed = json.loads(arguments_fragment)
                            piece = parsed.get("input", arguments_fragment) if isinstance(parsed, dict) else arguments_fragment
                        except (ValueError, TypeError):
                            piece = arguments_fragment
                        if not isinstance(piece, str):
                            piece = json.dumps(piece, ensure_ascii=False, separators=(",", ":"))
                        item["input"] = str(item.get("input") or "") + piece
                        yield _sse_frame(
                            "response.custom_tool_call_input.delta",
                            {
                                "type": "response.custom_tool_call_input.delta",
                                "item_id": item["id"],
                                "output_index": item["_output_index"],
                                "delta": piece,
                            },
                            response_id,
                        )
                    else:
                        item["arguments"] = str(item.get("arguments") or "") + arguments_fragment
                        yield _sse_frame(
                            "response.function_call_arguments.delta",
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": item["id"],
                                "output_index": item["_output_index"],
                                "delta": arguments_fragment,
                            },
                            response_id,
                        )

        # Close every open item in order, then emit the terminal events.
        for item in output:
            output_index = item["_output_index"]
            if item["type"] == "message":
                buffer = text_buffers.get("main", "")
                yield _sse_frame(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "content_index": 0,
                        "text": buffer,
                    },
                    response_id,
                )
                yield _sse_frame(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": buffer, "annotations": []},
                    },
                    response_id,
                )
            elif item["type"] == "reasoning" and item.get("summary"):
                buffer = text_buffers.get("reasoning", "")
                yield _sse_frame(
                    "response.reasoning_summary_text.done",
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "summary_index": 0,
                        "text": buffer,
                    },
                    response_id,
                )
                yield _sse_frame(
                    "response.reasoning_summary_part.done",
                    {
                        "type": "response.reasoning_summary_part.done",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": buffer},
                    },
                    response_id,
                )
            elif item["type"] == "function_call":
                yield _sse_frame(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "arguments": item.get("arguments", ""),
                    },
                    response_id,
                )
            elif item["type"] == "custom_tool_call":
                yield _sse_frame(
                    "response.custom_tool_call_input.done",
                    {
                        "type": "response.custom_tool_call_input.done",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "input": item.get("input", ""),
                    },
                    response_id,
                )
            item["status"] = "completed"
            yield _sse_frame(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": {k: v for k, v in item.items() if not k.startswith("_")},
                },
                response_id,
            )

        if not output and not saw_completion:
            # Nothing observable was produced and the stream carries no completion
            # evidence: a zero-content death the caller may retry invisibly.
            if outcome is not None:
                outcome["retryable"] = True
            return
        if not created_sent:
            created = ensure_created()
            if created:
                yield created
        if saw_completion:
            final = finish()
            if final:
                yield final
        else:
            if outcome is not None:
                outcome["truncated"] = True
            yield _sse_frame(
                "response.failed",
                {
                    "type": "response.failed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": int(time.time()),
                        "status": "failed",
                        "error": {
                            "code": "upstream_stream_truncated",
                            "message": "上游截断了响应流（未收到完成事件）",
                        },
                        "output": [],
                    },
                },
                response_id,
            )
        yield b"data: [DONE]\n\n"


def anthropic_sse_to_responses(
    upstream: Any,
    model: str,
    tool_namespaces: dict[str, str] | None = None,
    tool_kinds: dict[str, str] | None = None,
    outcome: dict[str, Any] | None = None,
) -> Any:
    """Yield Responses SSE frames while consuming Anthropic Messages SSE.

    ``outcome`` receives ``{"truncated": True}`` when the upstream stream ended without
    ``message_stop``, so the relaying caller can record the run as a 502 instead of a
    healthy 200 over a half-answer.
    """
    response_id = "resp_" + str(int(time.time() * 1000))
    response_model = model
    output: list[dict[str, Any]] = []
    blocks: dict[int, dict[str, Any]] = {}
    output_indices: dict[int, int] = {}
    text_buffers: dict[int, str] = {}
    usage: dict[str, int] = {}
    created_sent = False
    completed_sent = False
    # Anthropic always closes a healthy stream with message_stop.  Its absence after the
    # upstream ends means the stream was truncated, and the end-of-stream path below must
    # say so instead of synthesising a completed event over a half-answer.
    saw_message_stop = False

    def ensure_created() -> bytes:
        nonlocal created_sent
        if created_sent:
            return b""
        created_sent = True
        response = {
            "id": response_id, "object": "response", "created_at": int(time.time()),
            "model": response_model, "status": "in_progress", "output": [],
            "parallel_tool_calls": True, "tool_choice": "auto", "usage": None,
        }
        return _sse_frame(
            "response.created",
            {"type": "response.created", "response": response},
            response_id,
        )

    def finish() -> bytes:
        nonlocal completed_sent
        if completed_sent:
            return b""
        completed_sent = True
        response = {
            "id": response_id, "object": "response", "created_at": int(time.time()),
            "model": response_model, "status": "completed", "output": output,
            "parallel_tool_calls": True, "tool_choice": "auto", "output_text": "".join(text_buffers.values()),
            "usage": _response_usage(usage),
        }
        return _sse_frame(
            "response.completed",
            {"type": "response.completed", "response": response},
            response_id,
        )

    event_name = ""
    data_lines: list[str] = []
    with upstream:
        while True:
            line = upstream.readline()
            if not line:
                if data_lines:
                    line = b"\n"
                else:
                    break
            decoded = line.decode("utf-8", "replace").rstrip("\r\n")
            if decoded:
                if decoded.startswith("event:"):
                    event_name = decoded[6:].strip()
                elif decoded.startswith("data:"):
                    data_lines.append(decoded[5:].lstrip())
                continue
            if not data_lines:
                event_name = ""
                continue
            try:
                data = json.loads("\n".join(data_lines))
            except (ValueError, TypeError):
                data_lines, event_name = [], ""
                continue
            data_lines, event_name = [], ""
            kind = str(data.get("type") or event_name)
            if kind == "message_start":
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                response_id = "resp_" + str(message.get("id") or response_id.replace("resp_", ""))
                response_model = str(message.get("model") or response_model)
                usage.update(message.get("usage") or {})
                created = ensure_created()
                if created:
                    yield created
            elif kind == "content_block_start":
                block_index = int(data.get("index") or 0)
                block = data.get("content_block") if isinstance(data.get("content_block"), dict) else {}
                block_type = block.get("type")
                output_index = len(output)
                output_indices[block_index] = output_index
                blocks[block_index] = block
                if block_type == "text":
                    # Several gateways open every answer with an empty text block before the
                    # real content (or a tool call).  Creating the Responses message item here
                    # would hand the client an empty assistant bubble, so materialize lazily:
                    # the item only exists once an actual text delta arrives.
                    blocks[block_index] = block
                    text_buffers[block_index] = ""
                elif block_type in {"thinking", "redacted_thinking"}:
                    # Extended thinking arrives as its own block kind.  Translate it to a
                    # Responses reasoning item with one summary part so the client can show
                    # the reasoning instead of receiving a bare text answer out of nowhere.
                    item_id = f"rs_{response_id.removeprefix('resp_')}_{block_index}"
                    item = {"id": item_id, "type": "reasoning", "summary": []}
                    output.append(item)
                    text_buffers[block_index] = ""
                    yield ensure_created()
                    yield _sse_frame(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": output_index,
                            "item": item,
                        },
                        response_id,
                    )
                    if block_type == "thinking":
                        item["summary"].append({"type": "summary_text", "text": ""})
                        yield _sse_frame(
                            "response.reasoning_summary_part.added",
                            {
                                "type": "response.reasoning_summary_part.added",
                                "item_id": item_id,
                                "output_index": output_index,
                                "summary_index": 0,
                                "part": {"type": "summary_text", "text": ""},
                            },
                            response_id,
                        )
                elif block_type == "tool_use":
                    # Anthropic's block id is the tool-use/call id.  Responses has a
                    # separate function-call item id, and Codex uses that id to join the
                    # argument delta stream, so do not reuse the vendor id for both fields.
                    call_id = str(
                        block.get("id")
                        or f"call_{response_id.removeprefix('resp_')}_{output_index}"
                    )
                    tool_name = str(block.get("name") or "tool")
                    is_custom = (tool_kinds or {}).get(tool_name) == "custom"
                    item_id = f"{'ctc' if is_custom else 'fc'}_{response_id.removeprefix('resp_')}_{output_index}"
                    initial_input = block.get("input")
                    if is_custom:
                        initial_value = (
                            initial_input.get("input")
                            if isinstance(initial_input, dict) and "input" in initial_input
                            else initial_input
                        )
                        initial_arguments = initial_value if isinstance(initial_value, str) else (
                            json.dumps(initial_value, ensure_ascii=False, separators=(",", ":"))
                            if initial_value is not None else ""
                        )
                        item = {
                            "id": item_id, "type": "custom_tool_call", "status": "in_progress",
                            "call_id": call_id, "name": tool_name, "input": initial_arguments,
                        }
                    else:
                        initial_arguments = (
                            json.dumps(initial_input, ensure_ascii=False, separators=(",", ":"))
                            if isinstance(initial_input, dict) and initial_input
                            else ""
                        )
                        item = {
                            "id": item_id, "type": "function_call", "status": "in_progress",
                            "call_id": call_id, "name": tool_name, "arguments": initial_arguments,
                        }
                    namespace = (tool_namespaces or {}).get(tool_name)
                    if namespace:
                        item["namespace"] = namespace
                    output.append(item)
                    yield ensure_created()
                    yield _sse_frame(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": output_index,
                            "item": item,
                        },
                        response_id,
                    )
            elif kind == "content_block_delta":
                block_index = int(data.get("index") or 0)
                output_index = output_indices.get(block_index, 0)
                item = output[output_index] if output and output_index < len(output) else None
                delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = str(delta.get("text") or "")
                    text_buffers[block_index] = text_buffers.get(block_index, "") + text
                    if item is None and output_index >= len(output):
                        # First real text on a lazily-tracked block: create the message item
                        # now, so gateways that open with an empty text block never produce
                        # an empty assistant bubble on the client.
                        item = {
                            "id": response_id.replace("resp_", "msg_", 1),
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        }
                        output.append(item)
                        output_indices[block_index] = output_index
                        yield ensure_created()
                        yield _sse_frame(
                            "response.output_item.added",
                            {
                                "type": "response.output_item.added",
                                "output_index": output_index,
                                "item": item,
                            },
                            response_id,
                        )
                        yield _sse_frame(
                            "response.content_part.added",
                            {
                                "type": "response.content_part.added",
                                "item_id": item["id"],
                                "output_index": output_index,
                                "content_index": 0,
                                "part": {
                                    "type": "output_text",
                                    "text": "",
                                    "annotations": [],
                                },
                            },
                            response_id,
                        )
                        item["content"].append({"type": "output_text", "text": "", "annotations": []})
                    if item and item.get("content"):
                        item["content"][0]["text"] = text_buffers[block_index]
                    yield _sse_frame(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": item.get("id") if item else response_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "delta": text,
                        },
                        response_id,
                    )
                elif delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking") or "")
                    text_buffers[block_index] = text_buffers.get(block_index, "") + thinking
                    if item:
                        item_id = item.get("id")
                    else:
                        item_id = response_id
                    yield _sse_frame(
                        "response.reasoning_summary_text.delta",
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "summary_index": 0,
                            "delta": thinking,
                        },
                        response_id,
                    )
                elif delta_type == "input_json_delta":
                    partial = str(delta.get("partial_json") or "")
                    if item:
                        if item.get("type") == "custom_tool_call":
                            try:
                                part_obj = json.loads(partial)
                                partial_value = part_obj.get("input", "") if isinstance(part_obj, dict) else part_obj
                            except (ValueError, TypeError):
                                partial_value = partial
                            partial = partial_value if isinstance(partial_value, str) else str(partial_value)
                            item["input"] = str(item.get("input") or "") + partial
                        else:
                            item["arguments"] = str(item.get("arguments") or "") + partial
                        item_id = item.get("id")
                    else:
                        item_id = response_id
                    event_type = (
                        "response.custom_tool_call_input.delta"
                        if item and item.get("type") == "custom_tool_call"
                        else "response.function_call_arguments.delta"
                    )
                    event_payload = {
                            "type": event_type,
                            "item_id": item_id,
                            "output_index": output_index,
                            "delta": partial,
                        }
                    yield _sse_frame(event_type, event_payload, response_id)
            elif kind == "content_block_stop":
                block_index = int(data.get("index") or 0)
                output_index = output_indices.get(block_index, 0)
                item = output[output_index] if output and output_index < len(output) else None
                if item and item.get("type") == "message":
                    text = str(text_buffers.get(block_index) or "")
                    yield _sse_frame(
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "item_id": item["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "text": text,
                        },
                        response_id,
                    )
                    yield _sse_frame(
                        "response.content_part.done",
                        {
                            "type": "response.content_part.done",
                            "item_id": item["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": text,
                                "annotations": [],
                            },
                        },
                        response_id,
                    )
                elif item and item.get("type") == "reasoning":
                    thinking = str(text_buffers.get(block_index) or "")
                    if item.get("summary"):
                        item["summary"][0]["text"] = thinking
                        yield _sse_frame(
                            "response.reasoning_summary_text.done",
                            {
                                "type": "response.reasoning_summary_text.done",
                                "item_id": item["id"],
                                "output_index": output_index,
                                "summary_index": 0,
                                "text": thinking,
                            },
                            response_id,
                        )
                        yield _sse_frame(
                            "response.reasoning_summary_part.done",
                            {
                                "type": "response.reasoning_summary_part.done",
                                "item_id": item["id"],
                                "output_index": output_index,
                                "summary_index": 0,
                                "part": {"type": "summary_text", "text": thinking},
                            },
                            response_id,
                        )
                elif item and item.get("type") == "function_call":
                    yield _sse_frame(
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": item["id"],
                            "output_index": output_index,
                            "arguments": item.get("arguments", ""),
                        },
                        response_id,
                    )
                elif item and item.get("type") == "custom_tool_call":
                    yield _sse_frame(
                        "response.custom_tool_call_input.done",
                        {
                            "type": "response.custom_tool_call_input.done",
                            "item_id": item["id"],
                            "output_index": output_index,
                            "input": item.get("input", ""),
                        },
                        response_id,
                    )
                if item:
                    item["status"] = "completed"
                    yield _sse_frame(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "output_index": output_index,
                            "item": item,
                        },
                        response_id,
                    )
            elif kind == "message_delta":
                usage.update(data.get("usage") or {})
            elif kind == "message_stop":
                saw_message_stop = True
                final = finish()
                if final:
                    yield final
            elif kind == "error":
                yield _sse_frame(
                    "error",
                    {"type": "error", "error": data.get("error", data)},
                    response_id,
                )
        if not created_sent and not output:
            # The upstream stream produced nothing observable: no created, no blocks, no
            # usage.  Yield not a single byte (not even [DONE]) so the relaying caller can
            # still retry the request invisibly or answer a plain JSON error.
            if outcome is not None:
                outcome["retryable"] = True
            return
        if not created_sent:
            created = ensure_created()
            if created:
                yield created
        if saw_message_stop:
            final = finish()
            if final:
                yield final
        else:
            # The upstream stream ended without message_stop: truncated.  Synthesising a
            # completed event here would tell the client a half-answer is the whole answer.
            if outcome is not None:
                outcome["truncated"] = True
            # The official terminal event, so the client marks the turn failed-and-finished
            # instead of reporting a missing response.completed and retry-looping.
            yield _sse_frame(
                "response.failed",
                {
                    "type": "response.failed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": int(time.time()),
                        "status": "failed",
                        "error": {
                            "code": "upstream_stream_truncated",
                            "message": "上游截断了响应流（未收到完成事件）",
                        },
                        "output": [],
                    },
                },
                response_id,
            )
        yield b"data: [DONE]\n\n"


def strip_context_1m_suffix(model: str) -> str:
    """Return a requested model id without a trailing `[1m]`, matched case-insensitively.

    Only the suffix goes; a slug that merely contains brackets elsewhere is left alone, so an
    upstream id that legitimately ends in something like `[preview]` cannot be truncated here.
    """
    if model.lower().endswith(CONTEXT_1M_SUFFIX):
        return model[: -len(CONTEXT_1M_SUFFIX)]
    return model


def _usage_from_object(node: Any, found: dict[str, int]) -> None:
    """Collect the largest input/output token counts from any `usage` block inside `node`.

    Streaming protocols report usage incrementally, so the same field appears several times
    with growing values; taking the maximum lands on the final figure without having to know
    which event was last.  Anthropic and OpenAI disagree on the key names, hence both spellings.
    """
    if isinstance(node, list):
        for item in node:
            _usage_from_object(item, found)
        return
    if not isinstance(node, dict):
        return
    usage = node.get("usage")
    if isinstance(usage, dict):
        for target, keys in (
            ("tokens_in", ("input_tokens", "prompt_tokens")),
            ("tokens_out", ("output_tokens", "completion_tokens")),
        ):
            for key in keys:
                value = usage.get(key)
                if isinstance(value, int) and value > found.get(target, 0):
                    found[target] = value
    for value in node.values():
        if isinstance(value, (dict, list)):
            _usage_from_object(value, found)


def extract_token_usage(head: bytes, tail: bytes) -> dict[str, int]:
    """Best-effort token counts from the head and tail of a relayed response body.

    Returns only the keys it actually found, so a vendor that reports nothing produces no
    misleading zeros in the log.  Every parse is guarded: telemetry must never be able to
    turn a delivered response into an error.
    """
    found: dict[str, int] = {}
    for window in (head, tail):
        if not window:
            continue
        text = window.decode("utf-8", errors="replace")
        # A short non-streaming body is whole JSON; try that before treating it as SSE.
        try:
            _usage_from_object(json.loads(text), found)
        except ValueError:
            pass
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            payload = stripped[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                _usage_from_object(json.loads(payload), found)
            except ValueError:
                # A window boundary cuts the last event in half; the other window has it.
                continue
    return found


@dataclass(frozen=True)
class RoutingSnapshot:
    """One internally consistent routing generation retained for an entire request."""

    registry: dict[str, Any]
    registry_hash: str
    providers: dict[str, dict[str, Any]]
    default_provider_id: str
    model_routes: dict[str, tuple[str, str]]
    keys: dict[str, str]
    models: tuple[str, ...]
    forced_fast: frozenset[str]
    failover_vendors: frozenset[str]
    # Published slugs with no provider namespace. They are retained for backwards-compatible
    # catalog reads, but inference requests must opt in explicitly before they can reach any
    # upstream. This is what prevents a lost model prefix from selecting the default account.
    unscoped_models: frozenset[str]


class UpstreamReadError(RuntimeError):
    """The upstream stopped producing a body after its response had started."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every upstream redirect into a response instead of another network request.

    A provider endpoint should already be canonical in providers.json. Following a 301/302 can
    silently change POST into GET; following any redirect can also forward credentials to a new
    host. Most importantly for billing safety, it would violate the one-request/one-attempt
    invariant without the router's retry loop ever seeing the second request.
    """

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


# Ratios for the local count_tokens estimate.  Claude's tokenizer averages a little under four
# characters per token on Latin text while CJK runs closer to one token per character, so
# splitting on that boundary keeps a Chinese conversation from being under-counted threefold.
CHARS_PER_TOKEN_LATIN_NUMERATOR = 10
CHARS_PER_TOKEN_LATIN_DENOMINATOR = 36
# Per-message framing (role marker, delimiters) that Anthropic counts on top of the text.
TOKENS_PER_MESSAGE = 4
# Base64 payload bytes per token.  An image costs roughly (width * height) / 750 tokens and the
# byte length is the only size information a request body carries, so this is a calibration
# against screenshot-sized attachments rather than an exact rule.
IMAGE_BYTES_PER_TOKEN = 400
# Keys whose values are structure or opaque blobs, never prose the model reads.  `signature` is
# the load-bearing one: a thinking block carries a few hundred bytes of base64 attestation that
# would inflate the estimate badly if it were counted as text.
NON_PROSE_KEYS = frozenset(
    {"type", "id", "signature", "media_type", "encoding", "cache_control"}
)


def _estimate_text_tokens(text: str) -> int:
    """Character-class-aware token estimate for one string."""
    wide = sum(1 for char in text if ord(char) >= 0x2E80)
    narrow = len(text) - wide
    return wide + (
        narrow * CHARS_PER_TOKEN_LATIN_NUMERATOR
        + CHARS_PER_TOKEN_LATIN_DENOMINATOR
        - 1
    ) // CHARS_PER_TOKEN_LATIN_DENOMINATOR


def _estimate_json_tokens(node: Any) -> int:
    """Walk a request fragment and estimate the tokens its prose costs."""
    if isinstance(node, str):
        return _estimate_text_tokens(node)
    if isinstance(node, list):
        return sum(_estimate_json_tokens(item) for item in node)
    if isinstance(node, dict):
        total = 0
        for key, value in node.items():
            if key == "data" and isinstance(value, str):
                # Base64 image or document bytes: charge by size, not per character.
                total += len(value) // IMAGE_BYTES_PER_TOKEN
            elif key not in NON_PROSE_KEYS:
                total += _estimate_json_tokens(value)
        return total
    return 0


def estimate_input_tokens(payload: dict[str, Any]) -> int:
    """Approximate what Anthropic's count_tokens would report for a /v1/messages body.

    Only reached when the gateway itself has no count_tokens route.  The client spends this
    number on "how full is the context window" decisions, so an estimate a few percent high is
    harmless where the 404 it replaces is not: with no number at all the client either guesses
    or stops managing the window.
    """
    total = 0
    for key in ("system", "messages", "tools", "tool_choice"):
        if key in payload:
            total += _estimate_json_tokens(payload[key])
    messages = payload.get("messages")
    if isinstance(messages, list):
        total += TOKENS_PER_MESSAGE * len(messages)
    return max(1, total)



class RouterState:
    def __init__(self, registry_path: Path, auth_path: Path, log_path: Path):
        self.registry_path = registry_path
        self.auth_path = auth_path
        self.registry: dict[str, Any] = {}
        self.registry_hash = ""
        self.providers: dict[str, Any] = {}
        self.default_provider_id = ""
        self.model_routes: dict[str, tuple[str, str]] = {}
        self.keys: dict[str, str] = {}
        self.models: list[str] = []
        self.log_path = log_path
        self.started_at = time.time()
        self.requests = 0
        self.lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._config_signature: tuple[Any, ...] | None = None
        self._signature_checked_at = 0.0
        # How often the request path may re-stat the config files.  Instance-level so the
        # regression harness can zero it and keep hot reloads deterministic without sleeps.
        self.signature_throttle_seconds = 0.5
        self._routing: RoutingSnapshot | None = None
        # Why the last reload was refused, if it was.  A rejected providers.json leaves the
        # previous table serving, which is the right call for availability but used to be
        # completely invisible: no log line, nothing in /healthz, and a manager that could
        # only report "not running" about a process that was answering fine.
        self.config_error = ""
        self.config_error_at = 0.0
        self.recent: dict[str, deque[bool]] = {}
        self.forced_fast: set[str] = set()
        self.failover_vendors: set[str] = set()
        self.failover_registry: dict[str, Any] = {}
        # Vendors that have already answered "no such route" for count_tokens.  Per process and
        # deliberately not persisted: a gateway that gains the route only has to be asked once
        # more, after a restart, rather than never again.
        self._count_tokens_missing: set[str] = set()
        self.refresh()
        if self._routing is None:
            raise RuntimeError(
                "No usable enabled provider configuration was loaded: "
                + (self.config_error or "reason unknown")
            )

    @staticmethod
    def _path_signature(path: Path) -> tuple[Any, ...]:
        try:
            stat = path.stat()
        except OSError:
            return (str(path), None)
        return (str(path), stat.st_mtime_ns, stat.st_size, getattr(stat, "st_ctime_ns", 0))

    def _configuration_signature(self) -> tuple[Any, ...]:
        """Include credential files as well as providers.json in the hot-reload key."""
        paths = [self.registry_path, self.auth_path]
        secret_root = self.registry_path.parent / "secrets"
        try:
            paths.extend(sorted(secret_root.glob("*.dpapi")))
        except OSError:
            pass
        return tuple(self._path_signature(path) for path in paths)

    def _refuse(self, reason: str) -> None:
        """Record why this reload was rejected and return None for `_rebuild_routing`.

        Deliberately not written to the request log: the request counters count every line
        carrying a vendor and a status as one request, so a diagnostic there would inflate
        the usage totals and invent a vendor row that never served anything.
        """
        with self.lock:
            self.config_error = reason[:400]
            self.config_error_at = time.time()
        return None

    def config_status(self) -> tuple[str, float]:
        with self.lock:
            return self.config_error, self.config_error_at

    def refresh(self) -> None:
        """Pick up providers.json changes without a restart — routing included.

        Restarting the process to apply a config edit kills every in-flight request, which
        shows up in the client as "connection aborted" or a write timeout mid-stream. So the
        whole routing table is swapped in place instead, and only after the new one has been
        built successfully: a bad or half-written file leaves the previous config serving.

        The signature check stats a dozen files; on Windows each stat can cost milliseconds
        once antivirus hooks in, and this runs on every request. Throttle it to one probe
        per 0.5 s: a config edit still lands within half a second of the next request,
        which no user can perceive, while the request path stops paying the stat tax.
        """
        now = time.monotonic()
        if self.signature_throttle_seconds and (
            now - self._signature_checked_at < self.signature_throttle_seconds
        ):
            return
        self._signature_checked_at = now
        signature = self._configuration_signature()
        if signature == self._config_signature:
            return
        # Multiple request threads can notice the same change at once.  Only one performs the
        # decrypt/validation work; the others re-check the signature after acquiring the lock.
        with self._refresh_lock:
            signature = self._configuration_signature()
            if signature == self._config_signature:
                return
            rebuilt = self._rebuild_routing()
            if rebuilt is None:
                # Keep serving the last known-good configuration and retry on the next request.
                return
            registry, providers, routes, keys = rebuilt
            forced: set[str] = set()
            failover: set[str] = set()
            for provider in providers.values():
                if provider.get("allow_failover"):
                    failover.add(str(provider.get("id") or ""))
                for model in provider.get("models") or []:
                    if model.get("enabled") and model.get("fast_tier_forced"):
                        # Keyed by the published slug because that is what an incoming
                        # request carries; a model with publish_as is never asked for by
                        # `prefix + id`, so keying on that would lose its fast-tier flag.
                        forced.add(published_slug(provider, model))
            unscoped = frozenset(
                slug
                for provider in providers.values()
                for model in provider.get("models") or []
                if model.get("enabled")
                for slug in (published_slug(provider, model),)
                # A publish_as alias is a second way to lose provenance.  Normally validation
                # rejects it for Responses providers, but keeping the runtime check based on the
                # actual published slug also protects a hot-reloaded legacy/hand-edited registry.
                if not effective_model_prefix(provider)
                or not slug.startswith(effective_model_prefix(provider))
            )
            # Exactly one default is guaranteed by `_rebuild_routing`, which refuses the file
            # otherwise -- and says so, instead of returning from here without a word.
            default_provider_id = next(
                provider["id"] for provider in providers.values() if provider.get("is_default")
            )
            routing = RoutingSnapshot(
                registry=registry,
                registry_hash=registry_digest(registry),
                providers=providers,
                default_provider_id=default_provider_id,
                model_routes=routes,
                keys=keys,
                models=tuple(selectable_slugs(registry)),
                forced_fast=frozenset(forced),
                failover_vendors=frozenset(failover),
                unscoped_models=unscoped,
            )
            with self.lock:
                self._routing = routing
                # Keep these aliases for the manager's existing diagnostics. Request routing
                # uses `routing_snapshot()` so a hot reload cannot mix generations.
                self.registry = routing.registry
                self.providers = routing.providers
                self.model_routes = routing.model_routes
                self.keys = routing.keys
                self.registry_hash = routing.registry_hash
                self.models = list(routing.models)
                self.default_provider_id = routing.default_provider_id
                self.forced_fast = set(routing.forced_fast)
                self.failover_vendors = set(routing.failover_vendors)
                self.failover_registry = routing.registry
                self._config_signature = signature

    def routing_snapshot(self) -> RoutingSnapshot:
        """Return one live generation without invalidating requests using the old one."""
        self.refresh()
        with self.lock:
            routing = self._routing
            reason = self.config_error
        if routing is None:
            raise RuntimeError(
                "No usable enabled provider configuration was loaded: "
                + (reason or "reason unknown")
            )
        return routing

    def _rebuild_routing(self) -> tuple[Any, Any, Any, Any] | None:
        """Build a fresh routing table, or None when the file cannot be trusted yet."""
        try:
            registry = load_registry(self.registry_path)
            providers = {
                provider["id"]: provider
                for provider in registry["providers"]
                if provider["enabled"]
            }
            if not providers:
                return self._refuse("providers.json has no enabled provider")
            defaults = [p for p in providers.values() if p.get("is_default")]
            if len(defaults) != 1:
                return self._refuse(
                    f"providers.json needs exactly one default provider, found {len(defaults)}"
                )
            routes: dict[str, tuple[str, str]] = {}
            for provider in providers.values():
                for model in provider["models"]:
                    if model["enabled"]:
                        # The key is what a request asks for, the value is what the upstream
                        # is sent -- `publish_as` is exactly the case where they differ.
                        routes[published_slug(provider, model)] = (
                            provider["id"],
                            model["id"],
                        )
            keys: dict[str, str] = {}
            for provider_id, provider in providers.items():
                if provider.get("auth_type") == "codex_auth":
                    value = read_codex_auth_key(self.auth_path)
                else:
                    value = provider_key(provider)
                if not value:
                    return self._refuse(f"provider {provider_id!r} has no usable credential")
                keys[provider_id] = value
        except Exception as error:  # noqa: BLE001 - keep serving with the config we already have
            return self._refuse(f"{type(error).__name__}: {error}")
        with self.lock:
            self.config_error = ""
            self.config_error_at = 0.0
        return registry, providers, routes, keys

    def forced_fast_models(self, routing: RoutingSnapshot | None = None) -> frozenset[str]:
        """Slugs whose requests get service_tier=priority."""
        routing = routing or self.routing_snapshot()
        return routing.forced_fast

    def failover_candidates(
        self,
        requested_model: str,
        protocol: str | None = None,
        routing: RoutingSnapshot | None = None,
    ) -> list[tuple[str, str]]:
        """Ordered attempts for one slug, honouring the live allow_failover switches."""
        routing = routing or self.routing_snapshot()
        primary = routing.model_routes.get(requested_model)
        if primary is None:
            return []
        primary_provider = routing.providers.get(primary[0])
        if primary_provider is None:
            return []
        if protocol and protocol not in primary_provider.get("protocols", ["responses"]):
            return []
        if primary[0] not in routing.failover_vendors:
            return [primary]
        chain = [
            candidate
            for candidate in failover_chain(
                routing.registry,
                requested_model,
                self.unhealthy_vendors(routing),
                protocol=protocol,
            )
            if candidate[0] in routing.providers
        ]
        return chain or [primary]

    def protocol_models(
        self, protocol: str, routing: RoutingSnapshot | None = None
    ) -> list[str]:
        """Enabled slugs served by providers that speak one wire protocol."""
        routing = routing or self.routing_snapshot()
        slugs: list[str] = []
        for provider in routing.registry["providers"]:
            if not provider["enabled"]:
                continue
            if protocol not in provider.get("protocols", ["responses"]):
                continue
            slugs.extend(
                published_slug(provider, model)
                for model in provider["models"]
                if model["enabled"]
            )
        return slugs

    def unhealthy_vendors(self, routing: RoutingSnapshot | None = None) -> set[str]:
        """Vendors to try last: live failure streaks plus ones the saved probes condemned.

        Runtime-only health is forgotten on every restart, which would spend the first
        retry of every request on a vendor already known to be down. The saved
        last_test_status carries that knowledge across restarts.
        """
        with self.lock:
            live = {
                vendor
                for vendor, outcomes in self.recent.items()
                if len(outcomes) >= UNHEALTHY_STREAK and not any(outcomes)
            }
        routing = routing or self.routing_snapshot()
        registry = routing.registry
        recorded: set[str] = set()
        for provider in registry.get("providers") or []:
            if not isinstance(provider, dict) or not provider.get("enabled"):
                continue
            models = [
                model
                for model in provider.get("models") or []
                if isinstance(model, dict) and model.get("enabled")
            ]
            if models and all(model.get("last_test_status") == "failed" for model in models):
                recorded.add(str(provider.get("id") or ""))
        return live | recorded

    def resolve_model(self, model: str) -> tuple[str, str]:
        route = self.routing_snapshot().model_routes.get(model)
        if route is None:
            raise KeyError(f"Model is not enabled in providers.json: {model}")
        return route

    def count_tokens_reachable(self, vendor: str) -> bool:
        """Whether it is still worth forwarding count_tokens to this vendor."""
        with self.lock:
            return vendor not in self._count_tokens_missing

    def mark_count_tokens_missing(self, vendor: str) -> None:
        """Remember that this vendor has no count_tokens route, so stop asking.

        Claude Desktop calls count_tokens constantly -- once per meaningful edit to the pending
        turn -- and most of these Anthropic-shaped gateways never implemented it.  Each refusal
        cost a full round trip to answer nothing, which added up to thousands of 404s and over
        an hour of dead latency in this install's log before the local estimate existed.
        """
        with self.lock:
            self._count_tokens_missing.add(vendor)

    def record(
        self,
        vendor: str,
        method: str,
        path: str,
        status: int,
        duration: float,
        detail: str = "",
        *,
        model: str = "",
        usage: dict[str, int] | None = None,
    ) -> None:
        """Append one attempt to the log.

        `detail` carries the upstream error text for failures. Without it a 502 is just a
        number and a duration, which is not enough to tell a stalled gateway from a refused
        connection from a body we failed to send — every diagnosis needs the message.

        `model` and `usage` are what make the log answer "what did this cost me" rather than
        only "was it up". They are additive: older lines without them still parse, and a
        vendor that reports no usage simply omits the keys.
        """
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "vendor": vendor,
            "method": method,
            "path": path.split("?", 1)[0],
            "status": status,
            "duration_ms": round(duration * 1000),
        }
        if model:
            entry["model"] = model
        for key, value in (usage or {}).items():
            if isinstance(value, int) and value >= 0:
                entry[key] = value
        if detail and not 200 <= status < 300:
            entry["detail"] = detail[:400]
        with self.lock:
            self.requests += 1
            # 499 means our downstream client left. It says nothing about upstream health,
            # so it must not demote an otherwise working vendor in later failover chains.
            if status != 499:
                self.recent.setdefault(vendor, deque(maxlen=HEALTH_WINDOW)).append(
                    200 <= status < 300
                )
        # Telemetry is best-effort. A read-only log directory or a full disk must never abort
        # a failover attempt, turn a successful relay into a 502, or crash a request thread.
        # Serialize and prepare the directory outside the lock so a stalled disk stalls one
        # request thread, not every request thread queued on it.
        try:
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_lock:
                self._rotate_log_if_needed()
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:  # noqa: BLE001 - telemetry must never affect request routing
            pass

    def _rotate_log_if_needed(self) -> None:
        """Move the log aside once it passes the cap, keeping exactly one older generation.

        Caller already holds `_log_lock`.  Rotating on the write path rather than on a timer
        means the size ceiling holds even for a router that is only ever started and stopped,
        and one generation is enough: the panels read the tail, and the point is to stop
        unbounded growth, not to build an archive.
        """
        try:
            if self.log_path.stat().st_size < LOG_MAX_BYTES:
                return
        except OSError:
            return
        previous = self.log_path.with_suffix(self.log_path.suffix + ".1")
        try:
            previous.unlink(missing_ok=True)
            self.log_path.replace(previous)
        except OSError:
            # Another process has the file open on Windows; write on and try again next time.
            return

    def clear_keys(self) -> None:
        with self.lock:
            routing = self._routing
            if routing is None:
                self.keys = {}
                return
            cleared = RoutingSnapshot(
                registry=routing.registry,
                registry_hash=routing.registry_hash,
                providers=routing.providers,
                default_provider_id=routing.default_provider_id,
                model_routes=routing.model_routes,
                keys={},
                models=routing.models,
                forced_fast=routing.forced_fast,
                failover_vendors=routing.failover_vendors,
                unscoped_models=routing.unscoped_models,
            )
            self._routing = cleared
            self.keys = cleared.keys


class SotaRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexSotaRouter/2"
    # Without a socket timeout a client that connects and stalls -- the app is killed
    # mid-write, a laptop sleeps, a proxy half-closes -- parks its handler thread forever.
    # http.server applies this to every socket operation, and converts a firing timeout
    # into a clean close, so a stalled connection costs one log line instead of a leaked
    # thread.  300 s per operation is far beyond any legitimate pause (long streaming turns
    # write continuously; each write gets its own budget).
    timeout = 300
    # Latch shared by the relay paths and the stream-retry loop: once any response line
    # has gone to the client on this request, a retried attempt must not send another.
    _relay_headers_sent = False

    @property
    def state(self) -> RouterState:
        return self.server.router_state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json_response(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except CLIENT_GONE_ERRORS:
            # The client hung up before the answer could be written -- Claude Desktop
            # cancels count_tokens on nearly every edit, so this is routine.  Nobody is
            # left to tell, and letting it bubble wrote a full traceback into the crash
            # log for a healthy router.
            pass
        finally:
            self.close_connection = True

    def _client_protocol(self) -> str:
        """The wire protocol the client below us speaks, from the path it called."""
        route = INFERENCE_PATHS.get(urllib.parse.urlsplit(self.path).path)
        return route[0] if route else "responses"

    def _write_stream_error_frame(
        self, message: str, *, protocol: str = "responses", send_status: bool = False
    ) -> None:
        """Best-effort final SSE frame after an upstream mid-stream death or truncation.

        A raw close surfaces in the apps as "stream disconnected before completion" and a
        retry loop. The official terminal events stop that: a Responses client gets
        ``response.failed`` (the lifecycle event it treats as failed-and-finished), a
        Messages client gets the Anthropic ``error`` event. Best-effort by design: the
        client may already be gone, and a failing write here must never mask the telemetry
        that follows.

        ``send_status`` opens a fresh SSE response first, for the case where the stream
        died before any response line was ever sent and the client still expects a
        stream-shaped answer rather than a bare error status.
        """
        try:
            if send_status and not self._relay_headers_sent:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self._relay_headers_sent = True
            if protocol == "responses":
                event = "response.failed"
                payload = {
                    "type": "response.failed",
                    "response": {
                        "id": "resp_failed_" + str(int(time.time() * 1000)),
                        "object": "response",
                        "created_at": int(time.time()),
                        "status": "failed",
                        "error": {
                            "code": "upstream_stream_error",
                            "message": message[:300],
                        },
                        "output": [],
                    },
                }
            else:
                event = "error"
                payload = {
                    "type": "error",
                    "error": {"type": "upstream_stream_error", "message": message[:300]},
                }
            frame = (
                f"event: {event}\ndata: "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n\n"
            ).encode("utf-8")
            self.wfile.write(frame)
            self.wfile.flush()
        except Exception:  # noqa: BLE001 - nothing left to tell; never mask the outcome
            pass

    def _model_entries(
        self, routing: RoutingSnapshot, protocol: str | None = None
    ) -> list[dict[str, Any]]:
        """Enabled models as response objects, in registry order.

        One builder feeds both the list and the single-model lookup: a client that saw an id in
        GET /v1/models has to be able to retrieve that same id, and two copies of this loop
        would eventually disagree about which models exist.
        """
        entries: list[dict[str, Any]] = []
        for provider in routing.registry["providers"]:
            if not provider["enabled"]:
                continue
            if protocol and protocol not in provider.get("protocols", ["responses"]):
                continue
            for model in provider["models"]:
                if not model["enabled"]:
                    continue
                # The advertised id must be the slug this router dispatches on, not
                # `prefix + id`: a model with publish_as answers under its override and
                # nothing else, so listing the vendor id would advertise a model that then
                # 404s on GET /v1/models/{id} and misses Claude Desktop's thinking table.
                slug = published_slug(provider, model)
                entries.append(
                    {
                        "id": slug,
                        "object": "model",
                        "type": "model",
                        "display_name": model.get("display_name") or slug,
                        "created_at": UNKNOWN_MODEL_CREATED_AT,
                        "owned_by": provider["id"],
                    }
                )
        return entries

    def _combined_models(
        self, routing: RoutingSnapshot, protocol: str | None = None
    ) -> None:
        """Serve the model list. Entries carry both OpenAI and Anthropic field names.

        Claude Desktop on 3P auto-discovers models from GET /v1/models, and the Codex App
        reads its own catalog file, so one response shaped for both keeps a single endpoint
        working for either client.
        """
        data = self._model_entries(routing, protocol)
        # The Anthropic list envelope is data + has_more + first_id + last_id, and its clients
        # page with before_id/after_id taken off those two ids.  The whole registry always fits
        # in one response, so has_more stays false and the ids merely bound this page; null on an
        # empty list is what the SDKs' own page classes fall back to.
        self._json_response(
            200,
            {
                "object": "list",
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            },
        )

    def _single_model(
        self, routing: RoutingSnapshot, slug: str, protocol: str | None = None
    ) -> None:
        """Answer GET /v1/models/{id} out of the registry.

        The Anthropic SDKs call this to resolve an alias to a concrete model id; without it the
        request fell through to the generic 404 for unknown paths.  It is answered locally on
        purpose -- forwarding it would hand a vendor credential to a path outside
        INFERENCE_PATHS, which is exactly what that allowlist exists to prevent.
        """
        # A `[1m]` alias resolves to its base entry, which is what alias resolution is for: the
        # caller asked "what concrete model is this", and the answer is the slug without the
        # context marker.  Kept consistent with the inference path so a client cannot pick a
        # model that answers turns but 404s on lookup.
        wanted_slug = strip_context_1m_suffix(slug)
        for entry in self._model_entries(routing, protocol):
            if entry["id"] == wanted_slug:
                self._json_response(200, entry)
                return
        self._json_response(
            404,
            {
                "error": {
                    "message": f"No such model in providers.json: {slug}",
                    "type": "model_not_found",
                }
            },
        )

    def _read_request_body(self) -> bytes | None:
        """Read one bounded, unambiguous HTTP request body.

        BaseHTTPRequestHandler does not decode chunked request bodies.  Silently treating one
        as empty would forward a different request than the client sent, so reject it along
        with duplicate or malformed Content-Length fields.
        """
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding:
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Transfer-Encoding request bodies are not supported",
                        "type": "unsupported_transfer_encoding",
                    }
                },
            )
            return None

        content_lengths = self.headers.get_all("Content-Length", [])
        if not content_lengths:
            return b""
        if len(content_lengths) != 1:
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Content-Length must appear exactly once",
                        "type": "invalid_content_length",
                    }
                },
            )
            return None

        value = content_lengths[0]
        if not value or not value.isascii() or not value.isdigit():
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Content-Length must be a non-negative decimal integer",
                        "type": "invalid_content_length",
                    }
                },
            )
            return None
        length = int(value)
        if length > MAX_REQUEST_BODY_BYTES:
            self._json_response(
                413,
                {
                    "error": {
                        "message": (
                            f"Request body exceeds the {MAX_REQUEST_BODY_BYTES}-byte limit"
                        ),
                        "type": "request_body_too_large",
                    }
                },
            )
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Request body ended before Content-Length bytes arrived",
                        "type": "incomplete_request_body",
                    }
                },
            )
            return None
        return body

    @staticmethod
    def _upstream_url(provider: dict[str, Any], incoming_path: str) -> str:
        parsed = urllib.parse.urlsplit(incoming_path)
        route = INFERENCE_PATHS.get(parsed.path)
        if route is None:
            raise ValueError(f"Unsupported router path: {parsed.path}")
        protocol, suffix = route
        url = endpoint_url(provider, protocol)
        if suffix:
            url = url.rstrip("/") + suffix
        if parsed.query:
            url += ("&" if "?" in url else "?") + parsed.query
        return url

    def _route_request(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path in {"/health", "/v1/health", "/healthz", "/v1/healthz"}:
            # Health answers drive restart decisions, so they must never be served from
            # inside the signature throttle window: a save followed by an immediate health
            # probe would otherwise read a stale registry hash and trigger a needless
            # restart that drops in-flight requests. Health calls are rare; the stat cost
            # is irrelevant here.
            self.state._signature_checked_at = 0.0
        try:
            # Capture once before every local or proxied request. The returned generation is
            # retained through the final byte, so deleting a provider or rotating its key
            # cannot produce mixed provider/key lookups in this handler thread.
            routing = self.state.routing_snapshot()
        except RuntimeError as error:
            self._json_response(
                503, {"error": {"message": str(error), "type": "router_not_ready"}}
            )
            return
        if clean_path in {"/health", "/v1/health", "/healthz", "/v1/healthz"}:
            config_error, config_error_at = self.state.config_status()
            self._json_response(
                200,
                {
                    "status": "ok",
                    "version": ROUTER_VERSION,
                    "registry_hash": routing.registry_hash,
                    "upstreams": list(routing.providers),
                    "models": len(routing.models),
                    "requests": self.state.requests,
                    "uptime_seconds": round(time.time() - self.state.started_at),
                    # "ok" means this listener is serving; these two say whether what it is
                    # serving is still what providers.json says. A stale table plus an empty
                    # reason means nobody has edited the file -- not that a reload failed.
                    "config_error": config_error,
                    "config_error_age_seconds": (
                        round(time.time() - config_error_at) if config_error_at else 0
                    ),
                },
            )
            return
        if clean_path in {"/models", "/v1/models"}:
            # /v1/models is what Claude Desktop auto-discovers from, so prefer the
            # Anthropic-capable providers there — falling back to everything while no
            # messages provider is configured yet, so the Codex side never regresses.
            wanted = "messages" if clean_path == "/v1/models" else "responses"
            if not self.state.protocol_models(wanted, routing):
                wanted = None
            self._combined_models(routing, wanted)
            return
        # GET /v1/models/{id} is how the Anthropic SDKs resolve an alias to a concrete id.
        # Unquoting is safe here because the result is only ever compared against the ids the
        # registry already produced -- an id that does not match simply 404s.
        for base in ("/v1/models/", "/models/"):
            if not clean_path.startswith(base):
                continue
            wanted = "messages" if base == "/v1/models/" else "responses"
            if not self.state.protocol_models(wanted, routing):
                wanted = None
            self._single_model(
                routing, urllib.parse.unquote(clean_path[len(base):]), wanted
            )
            return

        # Consume one bounded body before closing an unsupported request. On Windows, closing
        # a socket with unread client bytes can emit a TCP reset and hide the intended JSON
        # 404 behind WinError 10053 at the caller.
        raw_body = self._read_request_body()
        if raw_body is None:
            return

        inference_route = INFERENCE_PATHS.get(clean_path)
        if inference_route is None:
            self._json_response(
                404,
                {
                    "error": {
                        "message": f"The local router does not expose this path: {clean_path}",
                        "type": "unsupported_router_path",
                    }
                },
            )
            return
        request_protocol = inference_route[0]

        payload: Any = None
        requested_model: str | None = None
        outgoing_body = raw_body
        if raw_body:
            try:
                payload = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json_response(
                    400,
                    {
                        "error": {
                            "message": "Inference request body must be valid JSON",
                            "type": "invalid_json",
                        }
                    },
                )
                return
            if not isinstance(payload, dict):
                self._json_response(
                    400,
                    {
                        "error": {
                            "message": "Inference request body must be a JSON object",
                            "type": "invalid_request_body",
                        }
                    },
                )
                return
        elif self.command in {"POST", "PUT", "PATCH"}:
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Inference request body must not be empty",
                        "type": "invalid_request_body",
                    }
                },
            )
            return

        # Every inference request must name the model explicitly.  Previously a missing,
        # null, or non-string value fell through to the default provider with an empty upstream
        # model id; that made a model-selection bug bill whichever provider happened to be
        # default.  Keep this check local and fail closed before constructing any candidates or
        # touching an upstream credential.  Discovery and health endpoints return above and are
        # intentionally unaffected.
        if not isinstance(payload, dict) or "model" not in payload or payload.get("model") is None:
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Inference requests must include an explicit non-empty model",
                        "type": "model_required",
                    }
                },
            )
            return
        raw_model = payload.get("model")
        if not isinstance(raw_model, str) or not raw_model.strip():
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Inference request model must be a non-empty string",
                        "type": "invalid_model",
                    }
                },
            )
            return

        # The 1M picker entry asks for `<slug>[1m]`; route it as `<slug>`.  The error below
        # still echoes what the client actually sent, not the rewritten form.
        requested_model = strip_context_1m_suffix(raw_model)
        if not requested_model or not requested_model.strip():
            self._json_response(
                400,
                {
                    "error": {
                        "message": "Inference request model must name a concrete model",
                        "type": "invalid_model",
                    }
                },
            )
            return
        route = routing.model_routes.get(requested_model)
        if route is None:
            self._json_response(
                400,
                {
                    "error": {
                        "message": f"Model is not enabled in providers.json: {raw_model}",
                        "type": "model_not_enabled",
                    }
                },
            )
            return
        # A bare published slug carries no provider identity.  Never let it reach an upstream:
        # if a client loses its provider prefix, routing by the default account would turn a
        # model-selection bug into an unplanned charge.  Use the snapshot's complete set rather
        # than comparing with the upstream id so a Messages-only publish_as alias cannot bypass
        # the same guard.
        route_provider = routing.providers.get(route[0])
        legacy_claude_messages = bool(
            route_provider
            and route_provider.get("workspace") == "claude"
            and route_provider.get("is_default") is True
            and request_protocol == "messages"
            and "messages" in route_provider.get("protocols", [])
            and "responses" not in route_provider.get("protocols", [])
        )
        if requested_model in routing.unscoped_models and not legacy_claude_messages:
            self._json_response(
                400,
                {
                    "error": {
                        "message": (
                            f"Model {raw_model} is unqualified; use the provider-prefixed model slug"
                        ),
                        "type": "unqualified_model",
                    }
                },
            )
            return

        candidates = self.state.failover_candidates(requested_model, request_protocol, routing)
        if not candidates:
            self._json_response(
                400,
                {
                    "error": {
                        "message": f"Model is not enabled in providers.json: {requested_model}",
                        "type": "model_not_enabled",
                    }
                },
            )
            return
        if request_protocol == "responses" and candidates:
            primary_provider = routing.providers.get(candidates[0][0])
            if primary_provider is not None and is_messages_to_chat_adapter(primary_provider):
                # The Messages bridge serves Claude Desktop; a Responses client would need
                # the other bridge.  Refuse locally instead of forwarding a mangled body.
                self._json_response(
                    501,
                    {
                        "error": {
                            "message": (
                                "这家供应商配置的是 Claude 侧（Messages）的 Chat Completions "
                                "桥接，只服务 Claude Desktop；Codex 侧请勾选对应方向的桥接"
                            ),
                            "type": "unsupported_adapter_operation",
                        }
                    },
                )
                return
        if (
            request_protocol == "messages"
            and candidates
            and clean_path not in COUNT_TOKENS_PATHS
        ):
            # count_tokens is exempt: it is answered locally for bridged vendors further
            # down (the loop estimates instead of forwarding), so refusing it here would
            # turn a working local answer into a 501.
            primary_provider = routing.providers.get(candidates[0][0])
            if primary_provider is not None and is_chat_completions_adapter(primary_provider):
                # The bridge translates Responses -> Chat Completions.  A Messages client
                # (Claude Desktop) would need the opposite translation, which does not
                # exist; refuse locally instead of forwarding a mangled body.
                self._json_response(
                    501,
                    {
                        "error": {
                            "message": (
                                "这家供应商配置的是 Chat Completions 桥接，只服务 Codex App；"
                                "Claude 侧暂不支持这种网关"
                            ),
                            "type": "unsupported_adapter_operation",
                        }
                    },
                )
                return
        if clean_path in COMPACT_PATHS:
            primary_provider = routing.providers.get(candidates[0][0]) if candidates else None
            if primary_provider is not None and (
                is_juno_adapter(primary_provider)
                or is_chat_completions_adapter(primary_provider)
            ):
                # There is no semantics-preserving Responses -> Messages mapping for compact.
                # Refuse locally before credentials, retries, or failover can reach an upstream.
                self._json_response(
                    501,
                    {
                        "error": {
                            "message": (
                                "Responses compact is not supported by the juno "
                                "Messages adapter"
                            ),
                            "type": "unsupported_adapter_operation",
                        }
                    },
                )
                return
        counting_tokens = clean_path in COUNT_TOKENS_PATHS and isinstance(payload, dict)
        # Treat every request arriving at a generation route as non-replayable.  POST is the
        # normal wire method, but being conservative for an unusual GET/PUT/PATCH caller is
        # cheaper than discovering later that a gateway assigned side effects to it.
        billable_generation = clean_path in BILLABLE_GENERATION_PATHS
        request_can_replay = not billable_generation
        if billable_generation:
            # A generation may have reached the upstream even when its response did not reach us.
            # This remains true when the caller supplied an Idempotency-Key: third-party gateways
            # are allowed to ignore it, and a different vendor cannot share its deduplication
            # ledger. Therefore a billable request gets one vendor and one network attempt,
            # without exception.
            candidates = candidates[:1]
        forced_fast = requested_model in routing.forced_fast if requested_model else False
        tool_namespaces = (
            _response_tool_namespaces(_response_tools(payload))
            if isinstance(payload, dict)
            else {}
        )
        tool_kinds = (
            _response_tool_kinds(_response_tools(payload))
            if isinstance(payload, dict)
            else {}
        )
        if counting_tokens and not any(
            self.state.count_tokens_reachable(candidate[0]) for candidate in candidates
        ):
            # Every vendor this slug can reach has already answered "no such route" once.  The
            # alternative to estimating locally is not a better number, it is a 404 with no
            # number in it at all -- and the client asks for this on nearly every edit to the
            # pending turn, so the round trip it saves is the difference between a responsive
            # composer and a second of lag per keystroke burst.  Deliberately not recorded: the
            # local answer never touched a vendor, and a synthetic 200 in the health window
            # would make a genuinely failing gateway look like it was serving.
            self._json_response(200, {"input_tokens": estimate_input_tokens(payload)})
            return

        last_status, last_error = 502, "no upstream was attempted"
        for index, (vendor, upstream_model) in enumerate(candidates):
            provider = routing.providers.get(vendor)
            key = routing.keys.get(vendor)
            if provider is None or not key:
                last_status, last_error = 502, "routing generation is incomplete"
                self.state.record(
                    vendor,
                    self.command,
                    self.path,
                    last_status,
                    0.0,
                    last_error,
                    model=upstream_model,
                )
                continue
            body = outgoing_body
            adapter = is_juno_adapter(provider)
            if counting_tokens and (
                adapter
                or is_chat_completions_adapter(provider)
                or is_messages_to_chat_adapter(provider)
            ):
                # This adapter's upstream endpoint is a *generation* Messages endpoint.  It
                # cannot safely forward /messages/count_tokens: the generic adapter URL rewrite
                # would turn that harmless metadata request into a real /v1/messages generation
                # and could bill the account every time the client refreshes its context meter.
                # A local estimate is deliberately preferable to any network attempt here.
                self._json_response(200, {"input_tokens": estimate_input_tokens(payload)})
                return
            if isinstance(payload, dict) and upstream_model:
                payload["model"] = upstream_model
                if forced_fast and clean_path not in {"/messages", "/v1/messages"} and not adapter:
                    payload["service_tier"] = "priority"
                try:
                    if adapter:
                        translated = responses_to_anthropic_payload(payload, upstream_model)
                    elif is_chat_completions_adapter(provider):
                        translated = responses_to_chat_payload(payload, upstream_model)
                    elif is_messages_to_chat_adapter(provider):
                        translated = messages_to_chat_payload(payload, upstream_model)
                    else:
                        translated = payload
                    body = json.dumps(translated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                except (TypeError, ValueError, OverflowError) as error:
                    self._json_response(
                        400,
                        {
                            "error": {
                                "message": f"Could not translate Responses request for juno: {error}",
                                "type": "request_translation_error",
                            }
                        },
                    )
                    return
            started = time.monotonic()
            is_last = index == len(candidates) - 1
            upstream, status, error = self._attempt_upstream(
                provider,
                key,
                body,
                vendor,
                upstream_model,
                is_last,
                allow_retry=request_can_replay,
            )
            if counting_tokens and status in COUNT_TOKENS_MISSING_STATUSES:
                # A permanent answer about the route, not about this request.  Remember it, log
                # it once so the switch is traceable, and still hand the client a number.
                if upstream is not None:
                    upstream.close()
                self.state.mark_count_tokens_missing(vendor)
                self.state.record(
                    vendor,
                    self.command,
                    self.path,
                    status,
                    time.monotonic() - started,
                    f"HTTP {status}: no count_tokens route; estimating locally from now on",
                    model=upstream_model,
                )
                self._json_response(200, {"input_tokens": estimate_input_tokens(payload)})
                return
            retryable = upstream is None or status in FAILOVER_RETRY_STATUSES or 500 <= status < 600
            if retryable and not is_last:
                if upstream is not None:
                    upstream.close()
                self.state.record(
                    vendor, self.command, self.path, status, time.monotonic() - started,
                    error or f"HTTP {status}", model=upstream_model,
                )
                last_status, last_error = status, error or f"HTTP {status}"
                continue
            if upstream is None:
                self.state.record(
                    vendor, self.command, self.path, status, time.monotonic() - started,
                    error or f"HTTP {status}", model=upstream_model,
                )
                last_status, last_error = status, error or f"HTTP {status}"
                break
            # One invisible same-vendor retry when the stream dies before any
            # state-bearing event has reached the client.  The apps retry a visible
            # failure themselves (and re-bill), so absorbing the early death here costs
            # the same and the user never sees it.  A stream that already showed content
            # is never retried -- duplicating visible output is strictly worse.
            self._relay_headers_sent = False
            for attempt_round in range(2):
                retry_stream = self._relay(
                    upstream,
                    vendor,
                    status,
                    started,
                    upstream_model,
                    provider=provider,
                    request_payload=payload if isinstance(payload, dict) else None,
                    tool_namespaces=tool_namespaces,
                    tool_kinds=tool_kinds,
                    is_final_attempt=attempt_round == 1,
                )
                if not retry_stream:
                    return
                self.state.record(
                    vendor,
                    self.command,
                    self.path,
                    502,
                    0.0,
                    "stream died before any content; retrying the same vendor",
                    model=upstream_model,
                )
                started = time.monotonic()
                upstream, status, error, _pre = self._open_upstream(provider, key, body)
                if upstream is None:
                    # The retry never connected and nothing was ever relayed.
                    self.state.record(
                        vendor,
                        self.command,
                        self.path,
                        status,
                        time.monotonic() - started,
                        error or f"HTTP {status}",
                        model=upstream_model,
                    )
                    self.close_connection = True
                    if not self._relay_headers_sent and not self.wfile.closed:
                        try:
                            self._json_response(
                                502,
                                {
                                    "error": {
                                        "message": friendly_upstream_error(
                                            error or "connection failed", vendor
                                        ),
                                        "type": "sota_router_error",
                                    }
                                },
                            )
                        except CLIENT_GONE_ERRORS:
                            pass
                    return
            return

        self.close_connection = True
        if not self.wfile.closed:
            try:
                self._json_response(
                    last_status if last_status >= 400 else 502,
                    {
                        "error": {
                            "message": friendly_upstream_error(last_error),
                            "type": "sota_router_error",
                        }
                    },
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _relay_juno(
        self,
        upstream: Any,
        vendor: str,
        status: int,
        started: float,
        model: str,
        request_payload: dict[str, Any] | None,
        tool_namespaces: dict[str, str] | None = None,
        tool_kinds: dict[str, str] | None = None,
        is_final_attempt: bool = True,
    ) -> bool:
        """Convert one juno Messages response back to the Responses wire shape.

        Returns True when the stream died before a single frame was written -- the caller
        may then retry the same vendor invisibly.  Response headers are held back until
        the first frame for exactly that reason.
        """
        headers_sent = False
        relay_error = ""
        head = bytearray()
        tail = deque(maxlen=64)
        tail_bytes = 0
        frames_written = 0

        def write_chunk(chunk: bytes) -> None:
            nonlocal tail_bytes, headers_sent, frames_written
            if not chunk:
                return
            if not headers_sent and not self._relay_headers_sent:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                self._relay_headers_sent = True
            self.wfile.write(chunk)
            self.wfile.flush()
            frames_written += 1
            if len(head) < USAGE_HEAD_BYTES:
                head.extend(chunk[: USAGE_HEAD_BYTES - len(head)])
            tail.append(chunk)
            tail_bytes += len(chunk)
            while tail_bytes > USAGE_TAIL_BYTES and len(tail) > 1:
                tail_bytes -= len(tail.popleft())

        try:
            with upstream:
                content_type = str(upstream.headers.get("Content-Type") or "").lower()
                upstream_is_sse = "text/event-stream" in content_type
                stream_requested = bool((request_payload or {}).get("stream"))
                if stream_requested or upstream_is_sse:
                    if self.command != "HEAD":
                        if upstream_is_sse:
                            outcome: dict[str, Any] = {}
                            for chunk in anthropic_sse_to_responses(
                                upstream, model, tool_namespaces, tool_kinds, outcome
                            ):
                                write_chunk(chunk)
                            if outcome.get("truncated"):
                                # The client already received the protocol-level terminal
                                # event; record the run as a failure so the health board
                                # and the log can see the gateway truncating.
                                status = 502
                                relay_error = "truncated SSE stream: no message_stop"
                            elif outcome.get("retryable") and not is_final_attempt:
                                # Zero frames reached the client: invisible retry.
                                return True
                        else:
                            raw = upstream.read(MAX_REQUEST_BODY_BYTES + 1)
                            if len(raw) > MAX_REQUEST_BODY_BYTES:
                                raise ValueError("upstream response exceeded the adapter limit")
                            response = anthropic_message_to_response(
                                json.loads(raw), model, tool_namespaces, tool_kinds
                            )
                            for chunk in response_to_sse(response):
                                write_chunk(chunk)
                    else:
                        self.send_response(status)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        headers_sent = True
                else:
                    raw = upstream.read(MAX_REQUEST_BODY_BYTES + 1)
                    if len(raw) > MAX_REQUEST_BODY_BYTES:
                        raise ValueError("upstream response exceeded the adapter limit")
                    response = anthropic_message_to_response(
                        json.loads(raw), model, tool_namespaces, tool_kinds
                    )
                    data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    headers_sent = True
                    self._relay_headers_sent = True
                    if self.command != "HEAD":
                        write_chunk(data)
        except CLIENT_GONE_ERRORS as error:
            status = 499
            relay_error = f"client gone: {type(error).__name__}"
        except Exception as error:  # noqa: BLE001 - report, never crash the handler thread
            status = 502
            relay_error = f"{type(error).__name__}: {error}"
            if frames_written == 0 and not is_final_attempt:
                # Nothing reached the client: hand the attempt back for an invisible retry.
                self.close_connection = True
                return True
            if not headers_sent and not self._relay_headers_sent and not self.wfile.closed:
                try:
                    self._json_response(
                        502,
                        {
                            "error": {
                                "message": friendly_upstream_error(str(error), vendor),
                                "type": "sota_router_adapter_error",
                            }
                        },
                    )
                except CLIENT_GONE_ERRORS:
                    pass
            elif headers_sent or self._relay_headers_sent:
                # Mid-stream death inside the adapter: emit the terminal event so Codex
                # fails the turn cleanly instead of staring at a dropped connection. The
                # adapter always speaks Responses on this side.
                self._write_stream_error_frame(
                    friendly_upstream_error(relay_error, vendor), protocol="responses"
                )
        finally:
            self.close_connection = True
            if not relay_error and not 200 <= status < 300 and head:
                relay_error = bytes(head[:400]).decode("utf-8", "replace").strip()
            try:
                usage = extract_token_usage(bytes(head), b"".join(tail))
            except Exception:  # noqa: BLE001 - telemetry must never affect a served response
                usage = {}
            self.state.record(
                vendor,
                self.command,
                self.path,
                status,
                time.monotonic() - started,
                relay_error,
                model=model,
                usage=usage,
            )
        return False

    def _relay_chat(
        self,
        upstream: Any,
        vendor: str,
        status: int,
        started: float,
        model: str,
        request_payload: dict[str, Any] | None,
        tool_namespaces: dict[str, str] | None = None,
        tool_kinds: dict[str, str] | None = None,
        is_final_attempt: bool = True,
    ) -> bool:
        """Bridge one Chat Completions response back to the Responses wire shape.

        Mirrors the Messages adapter's relay: response headers are held back until the
        first frame so a zero-content death can still be retried invisibly, and a stream
        that ends without completion evidence records a failure instead of a silent 200.
        """
        headers_sent = False
        relay_error = ""
        head = bytearray()
        tail = deque(maxlen=64)
        tail_bytes = 0
        frames_written = 0

        def write_chunk(chunk: bytes) -> None:
            nonlocal tail_bytes, headers_sent, frames_written
            if not chunk:
                return
            if not headers_sent and not self._relay_headers_sent:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                self._relay_headers_sent = True
            self.wfile.write(chunk)
            self.wfile.flush()
            frames_written += 1
            if len(head) < USAGE_HEAD_BYTES:
                head.extend(chunk[: USAGE_HEAD_BYTES - len(head)])
            tail.append(chunk)
            tail_bytes += len(chunk)
            while tail_bytes > USAGE_TAIL_BYTES and len(tail) > 1:
                tail_bytes -= len(tail.popleft())

        try:
            with upstream:
                content_type = str(upstream.headers.get("Content-Type") or "").lower()
                upstream_is_sse = "text/event-stream" in content_type
                stream_requested = bool((request_payload or {}).get("stream"))
                if (stream_requested or upstream_is_sse) and self.command != "HEAD":
                    if upstream_is_sse:
                        outcome: dict[str, Any] = {}
                        for chunk in chat_sse_to_responses(
                            upstream, model, tool_namespaces, tool_kinds, outcome
                        ):
                            write_chunk(chunk)
                        if outcome.get("truncated"):
                            status = 502
                            relay_error = "truncated SSE stream: no completion event"
                        elif outcome.get("retryable") and not is_final_attempt:
                            return True
                    else:
                        raw = upstream.read(MAX_REQUEST_BODY_BYTES + 1)
                        if len(raw) > MAX_REQUEST_BODY_BYTES:
                            raise ValueError("upstream response exceeded the adapter limit")
                        response = chat_message_to_response(
                            json.loads(raw), model, tool_namespaces, tool_kinds
                        )
                        for chunk in response_to_sse(response):
                            write_chunk(chunk)
                elif self.command == "HEAD":
                    pass
                else:
                    raw = upstream.read(MAX_REQUEST_BODY_BYTES + 1)
                    if len(raw) > MAX_REQUEST_BODY_BYTES:
                        raise ValueError("upstream response exceeded the adapter limit")
                    response = chat_message_to_response(
                        json.loads(raw), model, tool_namespaces, tool_kinds
                    )
                    data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    headers_sent = True
                    self._relay_headers_sent = True
                    write_chunk(data)
        except CLIENT_GONE_ERRORS as error:
            status = 499
            relay_error = f"client gone: {type(error).__name__}"
        except Exception as error:  # noqa: BLE001 - report, never crash the handler thread
            status = 502
            relay_error = f"{type(error).__name__}: {error}"
            if frames_written == 0 and not is_final_attempt:
                self.close_connection = True
                return True
            if not headers_sent and not self._relay_headers_sent and not self.wfile.closed:
                try:
                    self._json_response(
                        502,
                        {
                            "error": {
                                "message": friendly_upstream_error(str(error), vendor),
                                "type": "sota_router_adapter_error",
                            }
                        },
                    )
                except CLIENT_GONE_ERRORS:
                    pass
            elif headers_sent or self._relay_headers_sent:
                self._write_stream_error_frame(
                    friendly_upstream_error(relay_error, vendor), protocol="responses"
                )
        finally:
            self.close_connection = True
            if not relay_error and not 200 <= status < 300 and head:
                relay_error = bytes(head[:400]).decode("utf-8", "replace").strip()
            try:
                usage = extract_token_usage(bytes(head), b"".join(tail))
            except Exception:  # noqa: BLE001 - telemetry must never affect a served response
                usage = {}
            self.state.record(
                vendor,
                self.command,
                self.path,
                status,
                time.monotonic() - started,
                relay_error,
                model=model,
                usage=usage,
            )
        return False

    def _relay_messages_chat(
        self,
        upstream: Any,
        vendor: str,
        status: int,
        started: float,
        model: str,
        request_payload: dict[str, Any] | None,
        is_final_attempt: bool = True,
    ) -> bool:
        """Bridge one Chat Completions response back to the Anthropic Messages wire.

        Same contract as the other bridges: headers are held back until the first frame so
        a zero-content death can be retried invisibly, and a stream without completion
        evidence fails the turn (Anthropic ``error`` event) instead of faking success.
        """
        headers_sent = False
        relay_error = ""
        head = bytearray()
        tail = deque(maxlen=64)
        tail_bytes = 0
        frames_written = 0

        def write_chunk(chunk: bytes) -> None:
            nonlocal tail_bytes, headers_sent, frames_written
            if not chunk:
                return
            if not headers_sent and not self._relay_headers_sent:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                self._relay_headers_sent = True
            self.wfile.write(chunk)
            self.wfile.flush()
            frames_written += 1
            if len(head) < USAGE_HEAD_BYTES:
                head.extend(chunk[: USAGE_HEAD_BYTES - len(head)])
            tail.append(chunk)
            tail_bytes += len(chunk)
            while tail_bytes > USAGE_TAIL_BYTES and len(tail) > 1:
                tail_bytes -= len(tail.popleft())

        try:
            with upstream:
                content_type = str(upstream.headers.get("Content-Type") or "").lower()
                upstream_is_sse = "text/event-stream" in content_type
                stream_requested = bool((request_payload or {}).get("stream"))
                if (stream_requested or upstream_is_sse) and self.command != "HEAD":
                    if upstream_is_sse:
                        outcome: dict[str, Any] = {}
                        for chunk in chat_sse_to_anthropic_sse(upstream, model, outcome):
                            write_chunk(chunk)
                        if outcome.get("truncated"):
                            status = 502
                            relay_error = "truncated SSE stream: no completion event"
                        elif outcome.get("retryable") and not is_final_attempt:
                            return True
                    else:
                        raw = upstream.read(MAX_REQUEST_BODY_BYTES + 1)
                        if len(raw) > MAX_REQUEST_BODY_BYTES:
                            raise ValueError("upstream response exceeded the adapter limit")
                        message = chat_completion_to_anthropic_message(json.loads(raw), model)
                        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        headers_sent = True
                        self._relay_headers_sent = True
                        write_chunk(data)
                elif self.command == "HEAD":
                    pass
                else:
                    raw = upstream.read(MAX_REQUEST_BODY_BYTES + 1)
                    if len(raw) > MAX_REQUEST_BODY_BYTES:
                        raise ValueError("upstream response exceeded the adapter limit")
                    message = chat_completion_to_anthropic_message(json.loads(raw), model)
                    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    headers_sent = True
                    self._relay_headers_sent = True
                    write_chunk(data)
        except CLIENT_GONE_ERRORS as error:
            status = 499
            relay_error = f"client gone: {type(error).__name__}"
        except Exception as error:  # noqa: BLE001 - report, never crash the handler thread
            status = 502
            relay_error = f"{type(error).__name__}: {error}"
            if frames_written == 0 and not is_final_attempt:
                self.close_connection = True
                return True
            if not headers_sent and not self._relay_headers_sent and not self.wfile.closed:
                try:
                    self._json_response(
                        502,
                        {
                            "error": {
                                "message": friendly_upstream_error(str(error), vendor),
                                "type": "sota_router_adapter_error",
                            }
                        },
                    )
                except CLIENT_GONE_ERRORS:
                    pass
            elif headers_sent or self._relay_headers_sent:
                self._write_stream_error_frame(
                    friendly_upstream_error(relay_error, vendor), protocol="messages"
                )
        finally:
            self.close_connection = True
            if not relay_error and not 200 <= status < 300 and head:
                relay_error = bytes(head[:400]).decode("utf-8", "replace").strip()
            try:
                usage = extract_token_usage(bytes(head), b"".join(tail))
            except Exception:  # noqa: BLE001 - telemetry must never affect a served response
                usage = {}
            self.state.record(
                vendor,
                self.command,
                self.path,
                status,
                time.monotonic() - started,
                relay_error,
                model=model,
                usage=usage,
            )
        return False

    def _relay(
        self,
        upstream: Any,
        vendor: str,
        status: int,
        started: float,
        model: str = "",
        provider: dict[str, Any] | None = None,
        request_payload: dict[str, Any] | None = None,
        tool_namespaces: dict[str, str] | None = None,
        tool_kinds: dict[str, str] | None = None,
        is_final_attempt: bool = True,
    ) -> bool:
        """Stream one upstream response straight through to the client.

        Returns True when the stream died before any state-bearing event was forwarded
        and nothing terminal was sent -- the caller may then retry the same vendor
        invisibly.  Response headers are held back until the first body byte so a
        zero-content death can still be answered with a plain JSON error.
        """
        if provider is not None and is_messages_to_chat_adapter(provider) and 200 <= status < 300:
            return self._relay_messages_chat(
                upstream,
                vendor,
                status,
                started,
                model,
                request_payload,
                is_final_attempt=is_final_attempt,
            )
        if provider is not None and is_chat_completions_adapter(provider) and 200 <= status < 300:
            return self._relay_chat(
                upstream,
                vendor,
                status,
                started,
                model,
                request_payload,
                tool_namespaces,
                tool_kinds,
                is_final_attempt=is_final_attempt,
            )
        if provider is not None and is_juno_adapter(provider) and 200 <= status < 300:
            return self._relay_juno(
                upstream,
                vendor,
                status,
                started,
                model,
                request_payload,
                tool_namespaces,
                tool_kinds,
                is_final_attempt=is_final_attempt,
            )
        headers_sent = False
        relay_error = ""
        upstream_is_sse = False
        # Bounded windows only: the body is forwarded chunk by chunk exactly as before, and
        # these two buffers can never grow past their caps no matter how long the stream runs.
        head = bytearray()
        tail = deque(maxlen=64)
        tail_bytes = 0
        saw_completion = False
        state_seen = False
        carry = b""

        def send_headers_once() -> None:
            nonlocal headers_sent
            if headers_sent or self._relay_headers_sent:
                headers_sent = True
                return
            self.send_response(status)
            for name, value in upstream.headers.items():
                if name.lower() not in RESPONSE_HEADERS_TO_DROP:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            self._relay_headers_sent = True

        try:
            with upstream:
                upstream_is_sse = "text/event-stream" in str(
                    upstream.headers.get("Content-Type") or ""
                ).lower()
                if self.command == "HEAD":
                    send_headers_once()
                else:
                    # SSE is relayed line by line.  Reading 65536 bytes at a time made the
                    # client wait for a full 64 KB before seeing its first byte -- huge
                    # first-token latency -- and a mid-stream death lost everything the
                    # fill loop had already decoded (an IncompleteRead with tens of KB
                    # "read" meant the client still had nothing at all).  readline()
                    # forwards each event the instant the gateway emits it, and a death
                    # costs at most the single unfinished line.  Non-SSE bodies have no
                    # incremental consumer, so they keep the large-buffer read.
                    def feed(chunk: bytes) -> None:
                        # Without the nonlocal declarations these assignments would create
                        # shadowing locals inside this closure and every completed stream
                        # would be misread as truncated.
                        nonlocal saw_completion, state_seen, carry, head, tail_bytes
                        # Hold the response line back until real content exists, so a
                        # stream that dies before any event can still be retried or
                        # answered with JSON without a half-open SSE response.
                        send_headers_once()
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if len(head) < USAGE_HEAD_BYTES:
                            head += chunk[: USAGE_HEAD_BYTES - len(head)]
                        tail.append(chunk)
                        tail_bytes += len(chunk)
                        while tail_bytes > USAGE_TAIL_BYTES and len(tail) > 1:
                            tail_bytes -= len(tail.popleft())
                        if not (saw_completion and state_seen):
                            probe = carry + chunk
                            if not saw_completion and any(
                                marker in probe
                                for marker in (
                                    b"response.completed",
                                    b"message_stop",
                                    b"[DONE]",
                                )
                            ):
                                saw_completion = True
                            if not state_seen and any(
                                marker in probe for marker in STREAM_STATE_MARKERS
                            ):
                                state_seen = True
                            carry = probe[-1024:]

                    if upstream_is_sse:
                        while True:
                            try:
                                line = upstream.readline()
                            except Exception as error:  # noqa: BLE001 - classify source
                                raise UpstreamReadError(
                                    f"{type(error).__name__}: {error}"
                                ) from error
                            if not line:
                                break
                            feed(line)
                    else:
                        while True:
                            try:
                                chunk = upstream.read(65536)
                            except Exception as error:  # noqa: BLE001 - classify source
                                raise UpstreamReadError(
                                    f"{type(error).__name__}: {error}"
                                ) from error
                            if not chunk:
                                break
                            feed(chunk)
                    send_headers_once()
                    if upstream_is_sse and not saw_completion:
                        # A gateway that closes the connection mid-stream does it *cleanly*
                        # at the TCP level half the time, which used to look exactly like a
                        # normal end: the client silently lost the rest of the answer and
                        # the log recorded a healthy 200.
                        if not state_seen and not is_final_attempt:
                            # Only advisory traffic (comments, pings, rate-limit notices)
                            # ever reached the client, so the death is still invisible.
                            self.close_connection = True
                            return True
                        self._write_stream_error_frame(
                            friendly_upstream_error("truncated", vendor),
                            protocol=self._client_protocol(),
                        )
                        status = 502
                        relay_error = "truncated SSE stream: no completion event"
        except CLIENT_GONE_ERRORS as error:
            # The client hung up or stalled mid-stream. Nothing is wrong upstream and there
            # is nobody left to tell, so record it as its own outcome instead of a 502.
            status = 499
            relay_error = f"client gone: {type(error).__name__}"
        except Exception as error:  # noqa: BLE001 - report, never crash the handler thread
            status = 502
            relay_error = f"{type(error).__name__}: {error}"
            if not state_seen and not headers_sent and not self._relay_headers_sent:
                if not is_final_attempt:
                    self.close_connection = True
                    return True
                if not self.wfile.closed:
                    if (request_payload or {}).get("stream") and self._client_protocol() == "responses":
                        # The client asked for a stream: answer in its own shape.  A bare
                        # 502 makes the app print its own vague "stream closed" wording,
                        # while an SSE stream carrying response.failed is a terminal
                        # event it understands and reports with our message.
                        try:
                            self._write_stream_error_frame(
                                friendly_upstream_error(str(error), vendor),
                                protocol="responses",
                                send_status=True,
                            )
                        except CLIENT_GONE_ERRORS:
                            pass
                    else:
                        try:
                            self._json_response(
                                502,
                                {
                                    "error": {
                                        "message": friendly_upstream_error(str(error), vendor),
                                        "type": "sota_router_error",
                                    }
                                },
                            )
                        except CLIENT_GONE_ERRORS:
                            pass
            elif upstream_is_sse and (headers_sent or self._relay_headers_sent):
                # The stream already started, so a raw close is all the client would see.
                # One official terminal event lets the turn fail cleanly with the actual
                # reason, in the client's own protocol.
                self._write_stream_error_frame(
                    friendly_upstream_error(relay_error, vendor),
                    protocol=self._client_protocol(),
                )
        finally:
            self.close_connection = True
            if not relay_error and not 200 <= status < 300 and head:
                # An upstream 4xx/5xx puts its reason in the body, and the log used to drop it
                # entirely: a bare `503` with no message cannot distinguish an exhausted relay
                # channel from a rejected model id, which is exactly the question asked after
                # the fact. Keep a short prefix of whatever it said.
                relay_error = bytes(head[:400]).decode("utf-8", "replace").strip()
            usage: dict[str, int] = {}
            try:
                usage = extract_token_usage(bytes(head), b"".join(tail))
            except Exception:  # noqa: BLE001 - accounting must never break a served response
                usage = {}
            self.state.record(
                vendor,
                self.command,
                self.path,
                status,
                time.monotonic() - started,
                relay_error,
                model=model,
                usage=usage,
            )
        return False

    def _attempt_upstream(
        self,
        provider: dict[str, Any],
        key: str,
        body: bytes,
        vendor: str,
        upstream_model: str,
        is_last: bool,
        *,
        allow_retry: bool,
    ) -> tuple[Any, int, str]:
        """Send one attempt, with retries only for explicitly replay-safe requests.

        The caller marks billable generation requests as non-replayable even when an
        Idempotency-Key is present. A third-party gateway may ignore that header, and another
        vendor cannot share its deduplication state. Only metadata/discovery operations may use
        the bounded same-vendor retry below.
        """
        # Keep the invariant here as well as at candidate construction.  This method is small
        # and private today, but making it impossible for a future caller to opt a generation
        # back into retries prevents a refactor from reopening the duplicate-charge bug.
        clean_path = self.path.split("?", 1)[0]
        if clean_path in BILLABLE_GENERATION_PATHS:
            allow_retry = False
        started = time.monotonic()
        upstream, status, error, pre_request = self._open_upstream(provider, key, body)
        # A pre-request failure (dead TLS handshake, DNS, refused port) never reached the
        # gateway, so retrying it cannot double-bill even on a billable generation -- while a
        # replay-safe metadata request may retry any failure class it always could.
        if not allow_retry and not (upstream is None and pre_request):
            return upstream, status, error
        for delay in SAME_VENDOR_RETRY_BACKOFF:
            if upstream is None:
                if not (pre_request or allow_retry):
                    break
            elif status not in SAME_VENDOR_RETRY_STATUSES:
                break
            elapsed = time.monotonic() - started
            if elapsed > SAME_VENDOR_RETRY_MAX_ELAPSED:
                # Slow, not broken. Asking again would double the wait and add load to an
                # upstream that is already struggling to answer the first copy.
                break
            if upstream is not None:
                upstream.close()
            self.state.record(
                vendor,
                self.command,
                self.path,
                status,
                elapsed,
                (error or f"HTTP {status}") + "; retrying the same vendor",
                model=upstream_model,
            )
            time.sleep(delay)
            started = time.monotonic()
            upstream, status, error, pre_request = self._open_upstream(provider, key, body)
        return upstream, status, error

    def _open_upstream(
        self,
        provider: dict[str, Any],
        key: str,
        body: bytes,
        timeout: int | None = None,
    ) -> tuple[Any, int, str, bool]:
        """Send one attempt. Returns (response_or_None, status, error, pre_request_failure).

        `pre_request_failure` is True only when nothing was sent -- the attempt died during
        DNS, TCP connect, or the TLS handshake -- which is the one failure class that is safe
        to retry even for a billable generation.
        """
        opened_at = time.monotonic()

        def classify_transport_failure(error: BaseException) -> bool:
            # Refused/DNS failures cannot happen after the request is on the wire. An
            # SSLError can: the same exception type covers a dead handshake and a gateway
            # that processed the request and dropped the connection before responding, so
            # only a fast one -- inside the handshake window -- counts as never-sent.
            if isinstance(error, (ConnectionRefusedError, socket.gaierror)):
                return True
            if isinstance(error, ssl.SSLError):
                return time.monotonic() - opened_at < PRE_REQUEST_SSL_HANDSHAKE_WINDOW
            return False

        try:
            configured_auth_header = str(
                provider.get("auth_header") or "Authorization"
            ).lower()
            dropped = REQUEST_HEADERS_TO_DROP | {configured_auth_header}
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in dropped
            }
            headers.update(auth_headers(provider, key, include_probe_defaults=False))
            headers["Accept-Encoding"] = "identity"
            adapter = is_juno_adapter(provider)
            # Cloudflare in front of these gateways bans unknown client signatures (error
            # 1010 browser_signature_banned), and it is not one vendor: the request log has
            # shown it from a dozen different gateways. A forwarded Python-urllib or curl
            # User-Agent is enough to 403 the whole request, so always present the codex
            # CLI signature the vendors document -- the real apps behind this router send
            # it themselves, and nothing downstream needs the caller's original UA.
            headers.setdefault("Accept", "application/json")
            headers["User-Agent"] = JUNO_CODEX_USER_AGENT
            headers["originator"] = "codex_cli_rs"
            # Forwarding is a denylist, so a client that sent anthropic-version keeps its own
            # value whatever the casing; only a caller that omitted one gets the default, and
            # only on the Anthropic paths -- an OpenAI-shaped upstream has no use for it.
            route = INFERENCE_PATHS.get(urllib.parse.urlsplit(self.path).path)
            if (adapter or (route and route[0] == "messages")) and not any(
                name.lower() == "anthropic-version" for name in headers
            ):
                headers["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION
            if is_chat_completions_adapter(provider) or is_messages_to_chat_adapter(provider):
                # Both bridges talk to the gateway's Chat Completions endpoint, with the
                # caller's query string preserved (some gateways carry options there).
                parsed = urllib.parse.urlsplit(self.path)
                upstream_url = endpoint_url(provider, "chat")
                if parsed.query:
                    upstream_url += ("&" if "?" in upstream_url else "?") + parsed.query
            elif adapter:
                parsed = urllib.parse.urlsplit(self.path)
                upstream_url = endpoint_url(provider, "messages")
                if parsed.query:
                    upstream_url += ("&" if "?" in upstream_url else "?") + parsed.query
            else:
                upstream_url = self._upstream_url(provider, self.path)
            request = urllib.request.Request(
                upstream_url,
                data=body if self.command not in {"GET", "HEAD"} else None,
                headers=headers,
                method=self.command,
            )
            budget = timeout or int(provider.get("timeout_seconds") or 120)
            # urllib follows redirects by default, which is another form of hidden replay and
            # may also carry the upstream credential to a different host. Provider endpoints
            # are required to be canonical; surface 3xx to the client instead of following it.
            opener = urllib.request.build_opener(NoRedirectHandler())
            response = opener.open(request, timeout=budget)
            return response, int(response.status), "", False
        except urllib.error.HTTPError as error:
            if 300 <= int(error.status) < 400:
                # Do not relay Location to the client: a client-side redirect would create a
                # second request outside this router (and may expose the vendor URL). Treat it
                # as a failed canonical endpoint instead. Billable routes still stop after this
                # one network attempt; metadata routes may use their ordinary bounded policy.
                try:
                    error.close()
                except Exception:  # noqa: BLE001 - cleanup must not mask the classification
                    pass
                return None, 502, f"upstream redirect refused (HTTP {error.status})", False
            return error, int(error.status), f"HTTP {error.status}", False
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            return None, 502, str(error), classify_transport_failure(reason)
        except Exception as error:  # noqa: BLE001 - any transport failure is a failover signal
            return None, 502, str(error), classify_transport_failure(error)

    do_GET = _route_request
    do_HEAD = _route_request
    do_POST = _route_request
    do_DELETE = _route_request
    do_PATCH = _route_request
    do_PUT = _route_request


def write_pid_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(str(os.getpid()), encoding="ascii")
    os.replace(temporary, path)


def check_configuration(registry_path: Path, auth_path: Path) -> dict[str, Any]:
    registry = load_registry(registry_path)
    providers = [provider for provider in registry["providers"] if provider["enabled"]]
    if not providers:
        raise ValueError("Provider registry has no enabled provider")
    key_status: dict[str, bool] = {}
    for provider in providers:
        if provider.get("auth_type") == "codex_auth":
            key = read_codex_auth_key(auth_path)
        else:
            key = provider_key(provider)
        if not key:
            raise RuntimeError(f"Provider {provider['id']!r} has no usable credential")
        # This also rejects CR/LF injection in a credential before the process is launched.
        auth_headers(provider, key, include_probe_defaults=False)
        key_status[provider["id"]] = True
        key = ""
    return {
        "status": "ready",
        "version": ROUTER_VERSION,
        "registry_hash": registry_digest(registry),
        "providers": [provider["id"] for provider in providers],
        "models": selectable_slugs(registry),
        "keys_present": key_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17895)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--tls-port", type=int, default=0)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args()

    if args.check:
        print(json.dumps(check_configuration(args.registry, args.auth), separators=(",", ":")))
        return 0
    if args.pid_file is None or args.log is None:
        parser.error("--pid-file and --log are required unless --check is used")
    tls_values = (args.tls_port, args.tls_cert, args.tls_key)
    if any(tls_values) and not all(tls_values):
        parser.error("--tls-port, --tls-cert and --tls-key must be supplied together")

    if sys.stderr is None:
        # pythonw has no stderr at all: a crash traceback used to vanish without a trace,
        # which is exactly how a dead router becomes an undiagnosable mystery. Keep the
        # last crash evidence in a small file next to the request log instead.
        crash_log = args.log.parent / "sota-router-crash.log"
        try:
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            if crash_log.exists() and crash_log.stat().st_size > 2_000_000:
                crash_log.write_text("", encoding="utf-8")
            sys.stderr = crash_log.open("a", encoding="utf-8", errors="replace")
            sys.stdout = sys.stderr
        except OSError:
            pass

    state = RouterState(args.registry, args.auth, args.log)
    server = ThreadingHTTPServer((args.host, args.port), SotaRouterHandler)
    server.daemon_threads = True
    # The default listen backlog of 5 is too small for the apps' request bursts (a turn can
    # fire the main generation, follow-up title/summary generations and count_tokens at
    # once). Excess connections are refused by the OS and surface as sporadic "connection
    # failed" in the client; 128 absorbs any burst this machine produces.
    server.request_queue_size = 128
    server.router_state = state  # type: ignore[attr-defined]
    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    tls_server = None
    if args.tls_port and args.tls_cert and args.tls_key:
        # Optional compatibility listener for clients that require TLS. Current Claude
        # Desktop builds explicitly allow HTTP on a loopback gateway, so the standard Claude
        # launcher does not need a certificate or a second port.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(args.tls_cert), str(args.tls_key))
        tls_server = ThreadingHTTPServer((args.host, args.tls_port), SotaRouterHandler)
        tls_server.daemon_threads = True
        tls_server.request_queue_size = 128
        tls_server.router_state = state  # type: ignore[attr-defined]
        tls_server.socket = context.wrap_socket(tls_server.socket, server_side=True)
        threading.Thread(
            target=tls_server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True
        ).start()

    write_pid_file(args.pid_file)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        if tls_server is not None:
            tls_server.shutdown()
            tls_server.server_close()
        try:
            if args.pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                args.pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        state.clear_keys()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
