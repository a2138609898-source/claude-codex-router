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
import ssl
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from sota_registry import (
    auth_headers,
    endpoint_url,
    failover_chain,
    load_registry,
    provider_key,
    published_slug,
    read_codex_auth_key,
    registry_digest,
    selectable_slugs,
)


# Bumped whenever /healthz gains a field the manager reads, so a manager built from this
# source refuses to trust an older listener that cannot answer for itself. 13 adds
# `config_error`. Deliberately not bumped for `publish_as`: the manager treats a version
# mismatch as "not running", so bumping would have made the Codex router -- which serves fine
# and is not restarted until that workspace is next saved -- read as down in the status line.
# A listener predating publish_as is replaced by restarting it, not by failing this check.
ROUTER_VERSION = "13"
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
# Statuses that mean "this gateway has no count_tokens route" rather than "not right now".
# Every one of them is an answer about the route itself, so it is safe to remember: 404 no
# such path, 405 wrong method for a path that exists, 501 not implemented.
COUNT_TOKENS_MISSING_STATUSES = frozenset({404, 405, 501})
# A vendor that answers 5xx before producing a single byte is worth asking again. The relays
# in front of these gateways return a bare 502/503 while a channel is momentarily out, and one
# retry turns that into a served request instead of a profile that looks broken to the user.
# Deliberately NOT provider failover: the *same* vendor is retried, so `allow_failover: false`
# keeps meaning what it says -- no request is ever silently billed to a different account.
SAME_VENDOR_RETRY_STATUSES = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 529})
SAME_VENDOR_RETRY_BACKOFF = (0.4, 1.2)
# Only retry a *fast* failure. Retrying a 120s read timeout is how a slow gateway becomes a
# hammered one: the retries pile onto an upstream that is merely busy, its relay starts
# answering "no channel available" to everything, and that outage is precisely the symptom
# this path exists to prevent. A rejection that arrived in under this many seconds is a
# decision, not a queue.
SAME_VENDOR_RETRY_MAX_ELAPSED = 20.0

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


class UpstreamReadError(RuntimeError):
    """The upstream stopped producing a body after its response had started."""


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

        Deliberately not written to the request log: `read_router_usage` counts every line
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
        """
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
        try:
            with self._log_lock:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_log_if_needed()
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
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
            )
            self._routing = cleared
            self.keys = cleared.keys


class SotaRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexSotaRouter/2"

    @property
    def state(self) -> RouterState:
        return self.server.router_state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json_response(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

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
            if isinstance(payload.get("model"), str):
                # The 1M picker entry asks for `<slug>[1m]`; route it as `<slug>`.  The error
                # below still echoes what the client actually sent, not the rewritten form.
                requested_model = strip_context_1m_suffix(payload["model"])
                if requested_model not in routing.model_routes:
                    self._json_response(
                        400,
                        {
                            "error": {
                                "message": f"Model is not enabled in providers.json: {payload['model']}",
                                "type": "model_not_enabled",
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

        candidates = (
            self.state.failover_candidates(requested_model, request_protocol, routing)
            if requested_model is not None
            else [(routing.default_provider_id, "")]
        )
        if requested_model is None and request_protocol:
            default_provider = routing.providers.get(routing.default_provider_id)
            if default_provider is None or request_protocol not in default_provider.get(
                "protocols", ["responses"]
            ):
                candidates = []
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
        forced_fast = requested_model in routing.forced_fast if requested_model else False
        counting_tokens = clean_path in COUNT_TOKENS_PATHS and isinstance(payload, dict)
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
            if isinstance(payload, dict) and upstream_model:
                payload["model"] = upstream_model
                if forced_fast and clean_path not in {"/messages", "/v1/messages"}:
                    payload["service_tier"] = "priority"
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            started = time.monotonic()
            is_last = index == len(candidates) - 1
            upstream, status, error = self._attempt_upstream(
                provider, key, body, vendor, upstream_model, is_last
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
            self._relay(upstream, vendor, status, started, upstream_model)
            return

        self.close_connection = True
        if not self.wfile.closed:
            try:
                self._json_response(
                    last_status if last_status >= 400 else 502,
                    {"error": {"message": last_error, "type": "sota_router_error"}},
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _relay(
        self,
        upstream: Any,
        vendor: str,
        status: int,
        started: float,
        model: str = "",
    ) -> None:
        """Stream one upstream response straight through to the client."""
        headers_sent = False
        relay_error = ""
        # Bounded windows only: the body is forwarded chunk by chunk exactly as before, and
        # these two buffers can never grow past their caps no matter how long the stream runs.
        head = bytearray()
        tail = deque(maxlen=64)
        tail_bytes = 0
        try:
            with upstream:
                self.send_response(status)
                for name, value in upstream.headers.items():
                    if name.lower() not in RESPONSE_HEADERS_TO_DROP:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                if self.command != "HEAD":
                    while True:
                        try:
                            chunk = upstream.read(65536)
                        except Exception as error:  # noqa: BLE001 - classify source correctly
                            raise UpstreamReadError(
                                f"{type(error).__name__}: {error}"
                            ) from error
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if len(head) < USAGE_HEAD_BYTES:
                            head += chunk[: USAGE_HEAD_BYTES - len(head)]
                        tail.append(chunk)
                        tail_bytes += len(chunk)
                        while tail_bytes > USAGE_TAIL_BYTES and len(tail) > 1:
                            tail_bytes -= len(tail.popleft())
        except CLIENT_GONE_ERRORS as error:
            # The client hung up or stalled mid-stream. Nothing is wrong upstream and there
            # is nobody left to tell, so record it as its own outcome instead of a 502.
            status = 499
            relay_error = f"client gone: {type(error).__name__}"
        except Exception as error:  # noqa: BLE001 - report, never crash the handler thread
            status = 502
            relay_error = f"{type(error).__name__}: {error}"
            if not headers_sent and not self.wfile.closed:
                # Only safe before the status line goes out; afterwards a JSON error body
                # would be spliced into a half-written SSE stream.
                try:
                    self._json_response(
                        502, {"error": {"message": str(error), "type": "sota_router_error"}}
                    )
                except CLIENT_GONE_ERRORS:
                    pass
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

    def _attempt_upstream(
        self,
        provider: dict[str, Any],
        key: str,
        body: bytes,
        vendor: str,
        upstream_model: str,
        is_last: bool,
    ) -> tuple[Any, int, str]:
        """One upstream attempt, retried in place on a fast transient rejection.

        Retrying here is safe in a way retrying later is not: nothing has been written back to
        the client yet, so a second attempt cannot splice a fresh response into a half-sent
        stream.  It is also not failover -- the vendor never changes -- so a provider with
        `allow_failover: false` still never has its traffic billed to another account.

        Only the last candidate retries.  If another vendor is queued behind this one, moving on
        is both faster and more likely to work than asking the same gateway twice.
        """
        started = time.monotonic()
        upstream, status, error = self._open_upstream(provider, key, body)
        if not is_last:
            return upstream, status, error
        for delay in SAME_VENDOR_RETRY_BACKOFF:
            if upstream is not None and status not in SAME_VENDOR_RETRY_STATUSES:
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
            upstream, status, error = self._open_upstream(provider, key, body)
        return upstream, status, error

    def _open_upstream(
        self,
        provider: dict[str, Any],
        key: str,
        body: bytes,
        timeout: int | None = None,
    ) -> tuple[Any, int, str]:
        """Send one attempt. Returns (response_or_None, status, error) and writes nothing back."""
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
            # Forwarding is a denylist, so a client that sent anthropic-version keeps its own
            # value whatever the casing; only a caller that omitted one gets the default, and
            # only on the Anthropic paths -- an OpenAI-shaped upstream has no use for it.
            route = INFERENCE_PATHS.get(urllib.parse.urlsplit(self.path).path)
            if route and route[0] == "messages" and not any(
                name.lower() == "anthropic-version" for name in headers
            ):
                headers["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION
            request = urllib.request.Request(
                self._upstream_url(provider, self.path),
                data=body if self.command not in {"GET", "HEAD"} else None,
                headers=headers,
                method=self.command,
            )
            budget = timeout or int(provider.get("timeout_seconds") or 120)
            response = urllib.request.urlopen(request, timeout=budget)
            return response, int(response.status), ""
        except urllib.error.HTTPError as error:
            return error, int(error.status), f"HTTP {error.status}"
        except Exception as error:  # noqa: BLE001 - any transport failure is a failover signal
            return None, 502, str(error)

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

    state = RouterState(args.registry, args.auth, args.log)
    server = ThreadingHTTPServer((args.host, args.port), SotaRouterHandler)
    server.daemon_threads = True
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
