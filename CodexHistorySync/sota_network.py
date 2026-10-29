"""Per-host HTTP(S) egress policy shared by the SOTA router and manager.

The optional policy lives in ~/.config/agent-network/policy.json, independently
of CODEX_HOME. AGENT_NETWORK_POLICY_FILE or a builder argument can override it.
Only exact hosts are eligible for an override; missing/invalid policy and unknown
hosts retain urllib's environment/WinINET behavior. No process globals change.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


POLICY_ENV = "AGENT_NETWORK_POLICY_FILE"
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_MAX_POLICY_CHARS = 65_536


@dataclass(frozen=True)
class _Policy:
    direct_hosts: frozenset[str]
    proxy_hosts: frozenset[str]
    proxy_url: str


def _normal_host(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("An exact hostname is required")
    host = value.lower().removesuffix(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        host = host.encode("idna").decode("ascii")
        if len(host) > 253 or not all(_HOST_LABEL.fullmatch(part) for part in host.split(".")):
            raise ValueError("An exact hostname is required") from None
        return host


def _policy_location(policy_path: str | Path | None) -> Path:
    if policy_path is not None:
        return Path(policy_path)
    override = os.environ.get(POLICY_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "agent-network" / "policy.json"


def _read_policy(policy_path: str | Path | None) -> _Policy | None:
    # Read per request (including redirects): the file is small, atomic replaces
    # take effect immediately, and partial writes safely fall back to inheritance.
    try:
        with _policy_location(policy_path).open(encoding="utf-8-sig") as stream:
            raw = stream.read(_MAX_POLICY_CHARS + 1)
        if len(raw) > _MAX_POLICY_CHARS:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            return None
        # Never interpret a typo or an unsupported future default as global direct.
        if data.get("default_route") != "inherit":
            return None
        direct = data.get("direct_hosts")
        proxied = data.get("proxy_hosts")
        if not isinstance(direct, list) or not isinstance(proxied, list):
            return None
        direct_hosts = frozenset(_normal_host(host) for host in direct)
        proxy_hosts = frozenset(_normal_host(host) for host in proxied)
        if direct_hosts & proxy_hosts:
            return None
        proxy_url = data.get("proxy_url")
        if not isinstance(proxy_url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in proxy_url):
            return None
        proxy = urllib.parse.urlsplit(proxy_url)
        if proxy.scheme not in {"http", "https"} or not proxy.hostname:
            return None
        _normal_host(proxy.hostname)
        if proxy.port is not None and proxy.port == 0:
            return None
        if proxy.path not in {"", "/"} or proxy.query or proxy.fragment:
            return None
        return _Policy(direct_hosts, proxy_hosts, proxy_url)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def route_for_url(url: str, policy_path: str | Path | None = None) -> tuple[str, str | None]:
    """Return (inherit/direct/proxy, optional proxy URL) without any network I/O."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "inherit", None
        host = _normal_host(parsed.hostname)
        if host == "localhost":
            return "direct", None
        try:
            address = ipaddress.ip_address(host)
            if address.is_loopback or (
                isinstance(address, ipaddress.IPv6Address)
                and address.ipv4_mapped is not None
                and address.ipv4_mapped.is_loopback
            ):
                return "direct", None
        except ValueError:
            pass
    except (ValueError, TypeError, UnicodeError):
        return "inherit", None
    policy = _read_policy(policy_path)
    if policy is not None:
        if host in policy.direct_hosts:
            return "direct", None
        if host in policy.proxy_hosts:
            return "proxy", policy.proxy_url
    return "inherit", None


class _ExplicitProxyHandler(urllib.request.ProxyHandler):
    def proxy_open(self, request, proxy_url, scheme):
        # Equivalent to urllib ProxyHandler for validated HTTP(S) proxies, except
        # an explicit proxy policy must not be undone by NO_PROXY/WinINET bypass.
        original_scheme = request.type
        proxy = urllib.parse.urlsplit(proxy_url)
        if proxy.username and proxy.password:
            credentials = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password)}"
            encoded = base64.b64encode(credentials.encode()).decode("ascii")
            # Proxy credentials must not survive a redirect to a direct origin.
            request.add_unredirected_header("Proxy-Authorization", "Basic " + encoded)
        request.set_proxy(urllib.parse.unquote(proxy.netloc.rsplit("@", 1)[-1]), proxy.scheme)
        if original_scheme == proxy.scheme or original_scheme == "https":
            return None
        # An HTTP URL through an HTTPS proxy changes the transport handler. Avoid
        # applying the same proxy twice during urllib's internal redispatch.
        request._sota_proxy_redispatch = True
        try:
            return self.parent.open(request, timeout=request.timeout)
        finally:
            del request._sota_proxy_redispatch


class _PolicyProxyHandler(urllib.request.ProxyHandler):
    def __init__(self, policy_path: str | Path | None):
        # Preserve urllib's handlers for any non-HTTP protocols. Replace only
        # HTTP(S), even when the inherited proxy map has no entry for a scheme.
        super().__init__()
        self._policy_path = policy_path
        self.http_open = lambda request: self._policy_open(request, "http")
        self.https_open = lambda request: self._policy_open(request, "https")

    def _policy_open(self, request, scheme):
        if getattr(request, "_sota_proxy_redispatch", False):
            return None
        route, proxy_url = route_for_url(request.full_url, self._policy_path)
        if route == "direct":
            # An explicitly empty handler bypasses both environment and WinINET.
            handler = urllib.request.ProxyHandler({})
        elif route == "proxy":
            handler = _ExplicitProxyHandler({"http": proxy_url, "https": proxy_url})
        else:
            handler = urllib.request.ProxyHandler(self.proxies)
        handler.add_parent(self.parent)
        open_request = getattr(handler, f"{scheme}_open", None)
        return open_request(request) if open_request is not None else None


def _redirect_origin(url: str) -> tuple[str, str, int] | None:
    """Use origin identity, not routing-policy hostname equivalence."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    # Hostnames are case-insensitive, but a trailing dot is not silently trusted
    # as an equivalent origin. Explicit default ports are equivalent to omitted.
    port = parsed.port
    return parsed.scheme, parsed.hostname.lower(), port if port is not None else (443 if parsed.scheme == "https" else 80)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep urllib redirect/replay rules, without extending origin credentials."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # In particular, urllib refuses POST 307/308; do not introduce a replay.
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        try:
            source_origin = _redirect_origin(request.full_url)
            target_origin = _redirect_origin(redirected.full_url)
            target = urllib.parse.urlsplit(redirected.full_url)
        except (ValueError, TypeError, UnicodeError):
            raise urllib.error.HTTPError(
                request.full_url, code, "Unsafe redirect target refused", headers, fp
            ) from None
        cross_origin = source_origin is None or source_origin != target_origin
        # Credentials may be embedded in userinfo or under arbitrary query
        # parameter names. Do not try to guess/redact them or change API routes:
        # refuse cross-origin query URLs and all userinfo/non-HTTP(S) targets.
        if target_origin is None or target.username is not None or (cross_origin and target.query):
            raise urllib.error.HTTPError(
                request.full_url, code, "Credential-bearing or non-HTTP redirect refused", headers, fp
            )
        if cross_origin:
            # Registry providers may use ANY auth_header/extra_headers name.
            # A fixed credential denylist would miss custom keys. Carry none of
            # the original headers (including Host and Referer) across origins;
            # urllib will rebuild transport headers for the actual target URL.
            redirected.headers.clear()
            redirected.unredirected_hdrs.clear()
        # Proxy authorization belongs to the selected proxy, not the origin.
        # Let the per-URL proxy handler establish it again, even for same-origin
        # redirects, since policy may have changed since the previous request.
        redirected.remove_header("Proxy-authorization")
        return redirected


def build_network_opener(*handlers, policy_path: str | Path | None = None):
    """Build a fresh urllib opener, preserving caller-supplied redirect/TLS handlers.

    The proxy handler checks each actual URL, so redirects do not extend an
    allowlisted host's direct permission to a different, unlisted host. The
    default redirect handler also confines request credentials to their origin.
    An explicit redirect handler (notably the router's NoRedirectHandler) takes
    precedence, including when supplied as a handler class rather than instance.
    """
    has_redirect_handler = any(
        issubclass(handler, urllib.request.HTTPRedirectHandler)
        if isinstance(handler, type) else isinstance(handler, urllib.request.HTTPRedirectHandler)
        for handler in handlers
    )
    redirect_handlers = () if has_redirect_handler else (_SafeRedirectHandler(),)
    return urllib.request.build_opener(_PolicyProxyHandler(policy_path), *redirect_handlers, *handlers)
