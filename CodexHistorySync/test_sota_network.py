"""Offline regression tests; all sockets and HTTP traffic stay on loopback.

Run with: python -m unittest -v test_sota_network
Router/registry integration extracts only the relevant definitions with AST,
so tests never import their startup, registry, credential, or probe machinery.
"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

import sota_network


SOURCE_ROOT = Path(__file__).resolve().parent


def without_proxy_environment():
    # Windows/OpenSSL needs system environment values even for context creation;
    # isolate proxy discovery without clearing unrelated runtime dependencies.
    return {
        key: value for key, value in os.environ.items()
        if not key.lower().endswith("_proxy") and key not in {sota_network.POLICY_ENV, "REQUEST_METHOD"}
    }


def isolated_definitions(filename, names, namespace):
    tree = ast.parse((SOURCE_ROOT / filename).read_text(encoding="utf-8-sig"))
    definitions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    }
    module = ast.Module(body=[definitions[name] for name in names], type_ignores=[])
    exec(compile(module, filename, "exec"), namespace)
    return namespace


class _LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.events.append((self.command, self.path, dict(self.headers), raw))
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/redirect":
            self.send_response(int(query.get("status", ["302"])[0]))
            self.send_header("Location", query["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"server": self.server.label, "method": self.command}).encode()
        self.send_response(int(query.get("status", ["200"])[0]))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


class _CaptureHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, request):
        self.request = request
        self.tunnel_host = request._tunnel_host
        result = urllib.response.addinfourl(BytesIO(b"{}"), {}, request.full_url, 200)
        result.msg = "OK"
        return result


class _ScriptedTransport(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Exercise the real opener/redirect pipeline without DNS, sockets, or TLS."""

    def __init__(self, responses):
        urllib.request.HTTPHandler.__init__(self)
        self.responses = responses
        self.requests = []

    def http_open(self, request):
        self.requests.append({
            "url": request.full_url,
            "headers": {name.lower(): value for name, value in request.header_items()},
            "method": request.get_method(),
            "data": request.data,
        })
        status, location = self.responses[request.full_url]
        headers = Message()
        if location is not None:
            headers["Location"] = location
        result = urllib.response.addinfourl(BytesIO(b"{}"), headers, request.full_url, status)
        result.msg = "Synthetic test response"
        return result

    https_open = http_open


SYNTHETIC_SECRET = "synthetic-redirect-secret-never-a-real-key"


def synthetic_credential_headers():
    # auth_header and extra_headers permit arbitrary names, including names a
    # conventional credential denylist would never know to remove.
    return {name: SYNTHETIC_SECRET for name in (
        "aUtHoRiZaTiOn", "pRoXy-AuThOrIzAtIoN", "X-API-Key", "api-key", "Cookie",
        "Cookie2", "X-Vendor-Custom", "OpenAI-Organization", "Referer", "Origin",
        "Accept", "User-Agent",
    )}


class NetworkPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for label in ("origin", "proxy"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalHandler)
            server.daemon_threads = True
            server.label = label
            server.events = []
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            cls.servers.append((server, worker))
        cls.origin = cls.servers[0][0]
        cls.proxy = cls.servers[1][0]
        cls.dead_socket = socket.socket()
        cls.dead_socket.bind(("127.0.0.1", 0))  # Bound but intentionally never listening.
        cls.dead_proxy = f"http://127.0.0.1:{cls.dead_socket.getsockname()[1]}"
        cls.live_proxy = f"http://127.0.0.1:{cls.proxy.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.dead_socket.close()
        for server, worker in cls.servers:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directory = self.contexts.enter_context(tempfile.TemporaryDirectory(prefix="sota-network-test-"))
        self.policy_path = Path(directory) / "policy.json"
        self.write_policy()
        environment = without_proxy_environment()
        environment.update({
            "HTTP_PROXY": self.dead_proxy,
            "HTTPS_PROXY": self.dead_proxy,
            "ALL_PROXY": self.dead_proxy,
            "NO_PROXY": "",
            sota_network.POLICY_ENV: str(self.policy_path),
        })
        self.contexts.enter_context(mock.patch.dict(os.environ, environment, clear=True))
        self.contexts.enter_context(mock.patch("urllib.request.proxy_bypass", return_value=False))
        original_getaddrinfo = socket.getaddrinfo

        def local_dns(host, port, *args, **kwargs):
            if host in {"direct.invalid", "proxied.invalid", "unknown.invalid"}:
                host = "127.0.0.1"
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise AssertionError("Test attempted non-loopback DNS")
            return original_getaddrinfo(host, port, *args, **kwargs)

        self.contexts.enter_context(mock.patch("socket.getaddrinfo", side_effect=local_dns))
        self.origin.events.clear()
        self.proxy.events.clear()

    def write_policy(self, **overrides):
        data = {
            "version": 1,
            "default_route": "inherit",
            "proxy_url": self.live_proxy,
            "direct_hosts": ["direct.invalid"],
            "proxy_hosts": ["proxied.invalid"],
        }
        data.update(overrides)
        self.policy_path.write_text(json.dumps(data), encoding="utf-8")

    def url(self, host="direct.invalid", path="/"):
        return f"http://{host}:{self.origin.server_port}{path}"

    def get_json(self, url, opener=None):
        if opener is None:
            opener = sota_network.build_network_opener()
        with opener.open(url, timeout=1) as response:
            return json.load(response)

    def redirect_url(self, target, status=302):
        return self.url(path="/redirect?" + urllib.parse.urlencode({"to": target, "status": status}))

    def test_allowlisted_direct_works_with_dead_environment_proxy(self):
        self.assertEqual(self.get_json(self.url())["server"], "origin")
        self.assertEqual(len(self.origin.events), 1)
        self.assertEqual(self.proxy.events, [])

    def test_changed_sources_compile_without_import_side_effects(self):
        for name in ("sota_network.py", "codex_sota_router.py", "sota_registry.py", "test_sota_network.py"):
            with self.subTest(source=name):
                path = SOURCE_ROOT / name
                compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")

    def test_allowlisted_direct_bypasses_simulated_wininet(self):
        registry_function = "getproxies_registry" if hasattr(urllib.request, "getproxies_registry") else "getproxies"
        with mock.patch.dict(os.environ, without_proxy_environment(), clear=True), mock.patch.object(
            urllib.request, registry_function, return_value={"http": self.dead_proxy, "https": self.dead_proxy}
        ) as wininet, mock.patch("urllib.request.proxy_bypass") as bypass:
            opener = sota_network.build_network_opener(policy_path=self.policy_path)
            self.assertEqual(self.get_json(self.url(), opener)["server"], "origin")
            wininet.assert_called()
            bypass.assert_not_called()

    def test_explicit_proxy_overrides_dead_environment_and_bypass(self):
        with mock.patch("urllib.request.proxy_bypass", return_value=True) as bypass:
            result = self.get_json(self.url("proxied.invalid"))
            self.assertEqual(result["server"], "proxy")
            self.assertTrue(self.proxy.events[0][1].startswith("http://proxied.invalid:"))
            self.assertEqual(self.origin.events, [])
            bypass.assert_not_called()

    def test_unknown_host_still_inherits_dead_environment_proxy(self):
        with self.assertRaises(urllib.error.URLError):
            self.get_json(self.url("unknown.invalid"))
        self.assertEqual(self.origin.events, [])

    def test_unknown_host_retains_environment_no_proxy_bypass(self):
        with mock.patch("urllib.request.proxy_bypass", return_value=True):
            self.assertEqual(self.get_json(self.url("unknown.invalid"))["server"], "origin")

    def test_loopback_is_direct_even_without_policy(self):
        self.policy_path.unlink()
        for url in ("http://localhost:17895", "http://127.20.30.40", "https://[::1]", "https://[::ffff:127.0.0.1]"):
            with self.subTest(url=url):
                self.assertEqual(sota_network.route_for_url(url), ("direct", None))
        self.assertEqual(self.get_json(self.url("127.0.0.1"))["server"], "origin")

    def test_hosts_match_exactly_and_only_for_http_https(self):
        self.assertEqual(sota_network.route_for_url("https://DIRECT.INVALID.:443/path"), ("direct", None))
        for url in (
            "https://sub.direct.invalid", "https://direct.invalid.evil.invalid",
            "https://direct.invalid@evil.invalid", "ftp://direct.invalid/file",
            "https://evil.invalid/#https://direct.invalid", "https://[broken",
        ):
            with self.subTest(url=url):
                self.assertEqual(sota_network.route_for_url(url), ("inherit", None))

    def test_missing_corrupt_or_unsupported_policy_safely_inherits(self):
        cases = [
            None, "{", "[]", '{"version":true}',
            json.dumps({"version": 1, "default_route": "direct"}),
            "{" * 70_000,
        ]
        for contents in cases:
            with self.subTest(contents=repr(contents)[:60]):
                if contents is None:
                    self.policy_path.unlink(missing_ok=True)
                else:
                    self.policy_path.write_text(contents, encoding="utf-8")
                self.assertEqual(sota_network.route_for_url(self.url()), ("inherit", None))
                with self.assertRaises(urllib.error.URLError):
                    self.get_json(self.url())
        for overrides in (
            {"direct_hosts": ["*.invalid"]}, {"direct_hosts": "direct.invalid"},
            {"proxy_hosts": ["direct.invalid"]}, {"proxy_url": "socks5://127.0.0.1:9"},
            {"proxy_url": "http://127.0.0.1:99999"}, {"proxy_url": "http://127.0.0.1:0"},
        ):
            with self.subTest(overrides=overrides):
                self.write_policy(**overrides)
                self.assertEqual(sota_network.route_for_url(self.url()), ("inherit", None))

    def test_policy_updates_and_corruption_apply_to_existing_opener(self):
        opener = sota_network.build_network_opener()
        self.assertEqual(self.get_json(self.url(), opener)["server"], "origin")
        self.write_policy(direct_hosts=[], proxy_hosts=["direct.invalid"])
        self.assertEqual(self.get_json(self.url(), opener)["server"], "proxy")
        self.policy_path.write_text("{", encoding="utf-8")
        with self.assertRaises(urllib.error.URLError):
            self.get_json(self.url(), opener)
        self.write_policy()
        self.assertEqual(self.get_json(self.url(), opener)["server"], "origin")

    def test_default_policy_uses_user_home_not_codex_home(self):
        fake_home = self.policy_path.parent / "user-home"
        with mock.patch.dict(os.environ, {"CODEX_HOME": "Z:/unrelated-profile"}, clear=True), mock.patch.object(
            Path, "home", return_value=fake_home
        ):
            self.assertEqual(sota_network._policy_location(None), fake_home / ".config/agent-network/policy.json")
        self.assertEqual(sota_network._policy_location(None), self.policy_path)
        other = self.policy_path.parent / "explicit.json"
        self.assertEqual(sota_network._policy_location(other), other)

    def test_https_direct_ignores_inherited_proxy(self):
        capture = _CaptureHTTPS()
        with sota_network.build_network_opener(capture).open("https://direct.invalid:443/", timeout=1):
            self.assertEqual(capture.request.host, "direct.invalid:443")
            self.assertIsNone(capture.tunnel_host)

    def test_https_proxy_uses_connect_target(self):
        capture = _CaptureHTTPS()
        with sota_network.build_network_opener(capture).open("https://proxied.invalid:443/", timeout=1):
            self.assertEqual(capture.request.host, f"127.0.0.1:{self.proxy.server_port}")
            self.assertEqual(capture.tunnel_host, "proxied.invalid:443")

    def test_http_through_https_proxy_is_not_applied_twice(self):
        self.write_policy(proxy_url="https://127.0.0.1:9443")
        capture = _CaptureHTTPS()
        with sota_network.build_network_opener(capture).open("http://proxied.invalid/", timeout=1):
            self.assertEqual(capture.request.host, "127.0.0.1:9443")
            self.assertIsNone(capture.tunnel_host)

    def test_redirect_target_gets_its_own_policy(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": self.live_proxy}):
            response = self.get_json(self.redirect_url(self.url("unknown.invalid")))
        self.assertEqual(response["server"], "proxy")
        self.assertEqual(len(self.origin.events), 1)
        self.assertEqual(len(self.proxy.events), 1)

    def test_redirect_from_proxy_to_direct_does_not_forward_proxy_credentials(self):
        self.write_policy(proxy_url=self.live_proxy.replace("http://", "http://local-test:dummy-only@"))
        path = "/redirect?" + urllib.parse.urlencode({"to": self.url()})
        result = self.get_json(self.url("proxied.invalid", path))
        self.assertEqual(result["server"], "origin")
        self.assertEqual(len(self.proxy.events), 1)
        self.assertEqual(len(self.origin.events), 1)
        self.assertIn("proxy-authorization", {key.lower() for key in self.proxy.events[0][2]})
        self.assertNotIn("proxy-authorization", {key.lower() for key in self.origin.events[0][2]})

    def test_non_http_proxy_handlers_remain_inherited(self):
        with mock.patch("urllib.request.getproxies", return_value={"ftp": self.live_proxy}):
            handler = sota_network._PolicyProxyHandler(self.policy_path)
        handler.add_parent(mock.Mock())
        request = urllib.request.Request("ftp://direct.invalid/file")
        request.timeout = 1
        handler.ftp_open(request)
        self.assertEqual(request.host, f"127.0.0.1:{self.proxy.server_port}")
        handler.parent.open.assert_called_once_with(request, timeout=1)

    def test_router_no_redirect_handler_is_preserved(self):
        namespace = isolated_definitions("codex_sota_router.py", ["NoRedirectHandler"], {"urllib": urllib, "Any": Any})
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.origin.events.clear()
                opener = sota_network.build_network_opener(namespace["NoRedirectHandler"]())
                request = urllib.request.Request(self.redirect_url(self.url("unknown.invalid"), status), data=b"{}")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(request, timeout=1)
                self.assertEqual(raised.exception.code, status)
                raised.exception.close()
                self.assertEqual(len(self.origin.events), 1)
                self.assertEqual(self.proxy.events, [])

    def test_router_callsite_refuses_redirect_without_replay(self):
        namespace = {
            "urllib": urllib, "Any": Any, "PRE_REQUEST_FAILURE_CLASSES": (ConnectionRefusedError, socket.gaierror),
            "REQUEST_HEADERS_TO_DROP": set(), "INFERENCE_PATHS": {},
            "JUNO_CODEX_USER_AGENT": "local-network-test", "DEFAULT_ANTHROPIC_VERSION": "2023-06-01",
            "auth_headers": lambda *_args, **_kwargs: {}, "is_juno_adapter": lambda _provider: False,
            "is_chat_completions_adapter": lambda _provider: False, "is_messages_to_chat_adapter": lambda _provider: False,
            "build_network_opener": sota_network.build_network_opener,
        }
        isolated_definitions("codex_sota_router.py", ["NoRedirectHandler", "_open_upstream"], namespace)
        request_handler = SimpleNamespace(
            headers={}, command="POST", path="/responses", state=mock.Mock(),
            _upstream_url=lambda *_args: self.redirect_url(self.url("unknown.invalid")),
        )
        response, status, message, pre_request = namespace["_open_upstream"](
            request_handler, {"id": "local-test-only", "timeout_seconds": 1}, "", b"{}"
        )
        self.assertIsNone(response)
        self.assertEqual(status, 502)
        self.assertIn("upstream redirect refused (HTTP 302)", message)
        self.assertFalse(pre_request)
        self.assertEqual(len(self.origin.events), 1)
        self.assertEqual(self.proxy.events, [])
        request_handler.state.reject_credential.assert_not_called()

    def test_registry_helper_keeps_redirect_and_json_semantics(self):
        namespace = isolated_definitions("sota_registry.py", ["_read_http_body", "_http_json"], {
            "urllib": urllib, "Any": Any, "json": json, "build_network_opener": sota_network.build_network_opener,
        })
        with mock.patch.dict(os.environ, {"HTTP_PROXY": self.live_proxy}):
            status, body, text = namespace["_http_json"](
                self.redirect_url(self.url("unknown.invalid")), "POST", {}, {"local": "test"}, 1
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"server": "proxy", "method": "GET"})
        self.assertEqual(json.loads(text), body)
        self.assertEqual(self.origin.events[0][0], "POST")
        self.assertEqual(json.loads(self.origin.events[0][3]), {"local": "test"})
        status, body, _text = namespace["_http_json"](self.url(path="/?status=403"), "GET", {}, None, 1)
        self.assertEqual(status, 403)
        self.assertEqual(body["server"], "origin")


    def test_cross_origin_clears_all_original_headers_for_get_and_post_redirects(self):
        for method, statuses in (("GET", (301, 302, 303, 307, 308)), ("POST", (301, 302, 303))):
            for status in statuses:
                with self.subTest(method=method, status=status):
                    self.origin.events.clear()
                    self.proxy.events.clear()
                    target = self.url("proxied.invalid", "/redirected")
                    credentials = synthetic_credential_headers()
                    credentials["Host"] = f"direct.invalid:{self.origin.server_port}"
                    request = urllib.request.Request(
                        self.redirect_url(target, status), headers=credentials,
                        data=b"synthetic-body" if method == "POST" else None, method=method,
                    )
                    request.add_unredirected_header("X-Once-Only", SYNTHETIC_SECRET)
                    self.assertEqual(self.get_json(request)["server"], "proxy")
                    self.assertEqual(len(self.origin.events), 1)
                    self.assertEqual(len(self.proxy.events), 1)
                    source_headers = {key.lower(): value for key, value in self.origin.events[0][2].items()}
                    target_headers = {key.lower(): value for key, value in self.proxy.events[0][2].items()}
                    self.assertEqual(source_headers["authorization"], SYNTHETIC_SECRET)
                    self.assertFalse(any(SYNTHETIC_SECRET in value for value in target_headers.values()))
                    for name in ("authorization", "proxy-authorization", "x-api-key", "cookie", "x-vendor-custom", "referer"):
                        self.assertNotIn(name, target_headers)
                    self.assertEqual(target_headers["host"], f"proxied.invalid:{self.origin.server_port}")
                    self.assertEqual(self.proxy.events[0][0], "GET")
                    self.assertEqual(self.proxy.events[0][3], b"")
                    self.assertEqual(request.get_header("Authorization"), SYNTHETIC_SECRET)

    def test_same_origin_keeps_authentication_for_get_and_post_redirects(self):
        for method, statuses in (("GET", (301, 302, 303, 307, 308)), ("POST", (301, 302, 303))):
            for status in statuses:
                with self.subTest(method=method, status=status):
                    self.origin.events.clear()
                    request = urllib.request.Request(
                        self.redirect_url(self.url(path="/redirected"), status),
                        headers=synthetic_credential_headers(),
                        data=b"synthetic-body" if method == "POST" else None, method=method,
                    )
                    self.assertEqual(self.get_json(request)["server"], "origin")
                    self.assertEqual(len(self.origin.events), 2)
                    target_headers = {key.lower(): value for key, value in self.origin.events[-1][2].items()}
                    for name in synthetic_credential_headers():
                        if name.lower() != "proxy-authorization":
                            self.assertEqual(target_headers[name.lower()], SYNTHETIC_SECRET)
                    self.assertNotIn("proxy-authorization", target_headers)
                    self.assertEqual(self.origin.events[-1][0], "GET")
                    self.assertEqual(self.origin.events[-1][3], b"")

    def test_different_port_does_not_receive_origin_credentials(self):
        target = f"http://direct.invalid:{self.proxy.server_port}/redirected"
        request = urllib.request.Request(self.redirect_url(target), headers=synthetic_credential_headers())
        self.assertEqual(self.get_json(request)["server"], "proxy")
        self.assertEqual(len(self.origin.events), 1)
        self.assertEqual(len(self.proxy.events), 1)
        self.assertFalse(any(SYNTHETIC_SECRET in value for value in self.proxy.events[0][2].values()))

    def test_credential_bearing_redirect_urls_are_refused_not_rewritten(self):
        targets = (
            self.url("proxied.invalid", "/redirected?api_key=" + SYNTHETIC_SECRET),
            self.url("proxied.invalid", "/redirected?arbitrary_parameter=" + SYNTHETIC_SECRET),
            self.url("proxied.invalid", "/redirected?harmless=also-refused-fail-closed"),
            self.url("proxied.invalid").replace("http://", "http://user:" + SYNTHETIC_SECRET + "@"),
            self.url().replace("http://", "http://user:" + SYNTHETIC_SECRET + "@"),
            "ftp://user:" + SYNTHETIC_SECRET + "@proxied.invalid/file",
        )
        for target in targets:
            with self.subTest(target_type=urllib.parse.urlsplit(target).scheme):
                self.origin.events.clear()
                self.proxy.events.clear()
                request = urllib.request.Request(self.redirect_url(target), headers=synthetic_credential_headers())
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.get_json(request)
                self.assertEqual(raised.exception.code, 302)
                self.assertNotIn(SYNTHETIC_SECRET, str(raised.exception))
                raised.exception.close()
                self.assertEqual(len(self.origin.events), 1)
                self.assertEqual(self.proxy.events, [])

    def test_same_origin_query_is_preserved_and_source_query_is_not_forwarded_cross_origin(self):
        target_path = "/redirected?api_key=" + SYNTHETIC_SECRET
        request = urllib.request.Request(
            self.redirect_url(self.url(path=target_path)), headers=synthetic_credential_headers()
        )
        self.assertEqual(self.get_json(request)["server"], "origin")
        self.assertEqual(self.origin.events[-1][1], target_path)
        self.assertEqual(self.origin.events[-1][2]["Authorization"], SYNTHETIC_SECRET)
        request = urllib.request.Request(
            self.redirect_url(self.url("proxied.invalid", "/redirected")) + "&api_key=" + SYNTHETIC_SECRET,
            headers=synthetic_credential_headers(),
        )
        self.assertEqual(self.get_json(request)["server"], "proxy")
        self.assertNotIn(SYNTHETIC_SECRET, self.proxy.events[-1][1])
        self.assertFalse(any(SYNTHETIC_SECRET in value for value in self.proxy.events[-1][2].values()))

    def test_inherited_proxy_credentials_are_not_forwarded_to_direct_origin(self):
        proxy_with_auth = self.live_proxy.replace("http://", "http://local-test:dummy-only@")
        source_path = "/redirect?" + urllib.parse.urlencode({"to": self.url()})
        with mock.patch.dict(os.environ, {"HTTP_PROXY": proxy_with_auth}):
            self.assertEqual(self.get_json(self.url("unknown.invalid", source_path))["server"], "origin")
        self.assertEqual(len(self.proxy.events), 1)
        self.assertEqual(len(self.origin.events), 1)
        self.assertIn("proxy-authorization", {key.lower() for key in self.proxy.events[0][2]})
        self.assertNotIn("proxy-authorization", {key.lower() for key in self.origin.events[0][2]})

    def test_registry_cross_origin_auth_is_removed_and_post_307_308_remain_errors(self):
        namespace = isolated_definitions("sota_registry.py", ["_read_http_body", "_http_json"], {
            "urllib": urllib, "Any": Any, "json": json, "build_network_opener": sota_network.build_network_opener,
        })
        for method, redirect_status in (("GET", 302), ("POST", 302), ("POST", 307), ("POST", 308)):
            with self.subTest(method=method, redirect_status=redirect_status):
                self.origin.events.clear()
                self.proxy.events.clear()
                status, body, response_text = namespace["_http_json"](
                    self.redirect_url(self.url("proxied.invalid", "/redirected"), redirect_status),
                    method, synthetic_credential_headers(), {"local": "test"} if method == "POST" else None, 1,
                )
                self.assertEqual(len(self.origin.events), 1)
                self.assertNotIn(SYNTHETIC_SECRET, response_text)
                if redirect_status in (307, 308):
                    self.assertEqual(status, redirect_status)
                    self.assertIsNone(body)
                    self.assertEqual(self.proxy.events, [])
                else:
                    self.assertEqual(status, 200)
                    self.assertEqual(body, {"server": "proxy", "method": "GET"})
                    self.assertEqual(len(self.proxy.events), 1)
                    self.assertFalse(any(SYNTHETIC_SECRET in value for value in self.proxy.events[0][2].values()))

    def test_explicit_no_redirect_handler_class_is_preserved(self):
        namespace = isolated_definitions("codex_sota_router.py", ["NoRedirectHandler"], {"urllib": urllib, "Any": Any})
        opener = sota_network.build_network_opener(namespace["NoRedirectHandler"])
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(self.redirect_url(self.url("proxied.invalid")), timeout=1)
        self.assertEqual(raised.exception.code, 302)
        raised.exception.close()
        self.assertEqual(len(self.origin.events), 1)
        self.assertEqual(self.proxy.events, [])


class RedirectOriginTests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(mock.patch("urllib.request.getproxies", return_value={}))
        self.contexts.enter_context(mock.patch("sota_network.route_for_url", return_value=("direct", None)))
        self.contexts.enter_context(mock.patch("socket.create_connection", side_effect=AssertionError("No sockets allowed")))

    def test_origin_comparison_includes_scheme_hostname_and_effective_port(self):
        cases = (
            ("https://origin.invalid/start", "https://origin.invalid/finish", True),
            ("https://origin.invalid/start", "https://ORIGIN.INVALID:443/finish", True),
            ("https://origin.invalid:443/start", "https://origin.invalid/finish", True),
            ("http://origin.invalid/start", "http://origin.invalid:80/finish", True),
            ("http://origin.invalid:80/start", "http://origin.invalid/finish", True),
            ("https://origin.invalid:8443/start", "https://origin.invalid:8443/finish", True),
            ("https://[::1]/start", "https://[::1]:443/finish", True),
            ("https://origin.invalid/start", "http://origin.invalid/finish", False),
            ("https://origin.invalid:443/start", "http://origin.invalid:443/finish", False),
            ("http://origin.invalid:80/start", "https://origin.invalid:80/finish", False),
            ("https://origin.invalid/start", "https://other.invalid/finish", False),
            ("https://origin.invalid/start", "https://origin.invalid:8443/finish", False),
            ("https://origin.invalid/start", "https://origin.invalid:0/finish", False),
            ("https://origin.invalid/start", "https://origin.invalid./finish", False),
            ("https://[::1]/start", "https://[::2]/finish", False),
        )
        for source, target, same_origin in cases:
            with self.subTest(source=source, target=target):
                transport = _ScriptedTransport({source: (302, target), target: (200, None)})
                request = urllib.request.Request(source, headers=synthetic_credential_headers())
                with sota_network.build_network_opener(transport).open(request, timeout=1) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(len(transport.requests), 2)
                target_headers = transport.requests[-1]["headers"]
                self.assertEqual(target_headers.get("authorization"), SYNTHETIC_SECRET if same_origin else None)
                self.assertNotIn("proxy-authorization", target_headers)
                if not same_origin:
                    self.assertFalse(any(SYNTHETIC_SECRET in value for value in target_headers.values()))

    def test_https_downgrade_strips_credentials_without_changing_replay_rules(self):
        source = "https://origin.invalid/start"
        target = "http://origin.invalid/finish"
        for method in ("GET", "POST"):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(method=method, status=status):
                    transport = _ScriptedTransport({source: (status, target), target: (200, None)})
                    request = urllib.request.Request(
                        source, headers=synthetic_credential_headers(), method=method,
                        data=b"synthetic-body" if method == "POST" else None,
                    )
                    opener = sota_network.build_network_opener(transport)
                    if method == "POST" and status in (307, 308):
                        with self.assertRaises(urllib.error.HTTPError) as raised:
                            opener.open(request, timeout=1)
                        self.assertEqual(raised.exception.code, status)
                        raised.exception.close()
                        self.assertEqual(len(transport.requests), 1)
                    else:
                        with opener.open(request, timeout=1) as response:
                            self.assertEqual(response.status, 200)
                        self.assertEqual(len(transport.requests), 2)
                        redirected = transport.requests[-1]
                        self.assertEqual(redirected["method"], "GET")
                        self.assertIsNone(redirected["data"])
                        self.assertFalse(any(SYNTHETIC_SECRET in value for value in redirected["headers"].values()))

    def test_redirect_chain_does_not_restore_credentials_when_returning_to_origin(self):
        source = "https://origin.invalid/start"
        second = "https://other.invalid/middle"
        third = "https://other.invalid/next"
        target = "https://origin.invalid/finish"
        transport = _ScriptedTransport({
            source: (302, second), second: (302, third), third: (302, target), target: (200, None),
        })
        request = urllib.request.Request(source, headers=synthetic_credential_headers())
        with sota_network.build_network_opener(transport).open(request, timeout=1):
            pass
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(transport.requests[0]["headers"]["authorization"], SYNTHETIC_SECRET)
        for event in transport.requests[1:]:
            self.assertFalse(any(SYNTHETIC_SECRET in value for value in event["headers"].values()))


if __name__ == "__main__":
    unittest.main()
