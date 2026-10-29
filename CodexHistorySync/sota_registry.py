#!/usr/bin/env python3
"""Shared provider registry, credential, catalog, and probe utilities."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from sota_network import build_network_opener


def _resolve_configured_path(value: str) -> Path:
    """Expand an environment/user path without depending on this checkout's location."""
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _resolve_install_root() -> Path:
    """Locate helper scripts beside this module, with an explicit deployment override."""
    configured = str(os.environ.get("CODEX_SOTA_CORE_ROOT") or "").strip()
    if configured:
        return _resolve_configured_path(configured)
    return Path(__file__).resolve().parent


def _resolve_workspace_root(env_name: str, default_name: str) -> Path:
    configured = str(os.environ.get(env_name) or "").strip()
    if configured:
        return _resolve_configured_path(configured)
    return Path.home() / default_name


INSTALL_ROOT = _resolve_install_root()


@dataclass(frozen=True)
class Workspace:
    """One isolated configuration root: its own registry, secrets, catalog, lock and port.

    Codex and Claude are separate products with separate accounts, keys and model names, so
    they get separate roots rather than a shared file with a discriminator column. Nothing a
    Claude edit does — including a failed write that rolls back — can reach Codex state.
    """

    name: str
    label: str
    root: Path
    router_port: int
    router_starter: Path
    protocol: str
    # The Codex App reads a generated model catalog file (config.toml's model_catalog_json).
    # Claude Desktop does not — it takes its model list from the configLibrary profile or
    # from the router's /v1/models — so generating one there is pointless and its empty
    # template only produced "Source catalog has no model templates" on every save.
    needs_catalog: bool = True

    @property
    def registry_path(self) -> Path:
        return self.root / "providers.json"

    @property
    def auth_path(self) -> Path:
        return self.root / "auth.json"

    @property
    def catalog_path(self) -> Path:
        return self.root / "sota-multi-vendor-model-catalog.json"

    @property
    def source_catalog_path(self) -> Path:
        # Keep existing installations' source templates, independently of the old
        # provider brand in their filename. Never mistake the generated catalog
        # (whose model slugs already contain provider prefixes) for a source.
        preferred = self.root / "tango-relay-model-catalog.json"
        if preferred.is_file():
            return preferred
        for candidate in sorted(self.root.glob("*-model-catalog.json")):
            if candidate != self.catalog_path and candidate.is_file():
                return candidate
        return preferred

    @property
    def secrets_root(self) -> Path:
        return self.root / "secrets"

    @property
    def deleted_root(self) -> Path:
        return self.root / "backups" / "deleted-providers"

    @property
    def lock_path(self) -> Path:
        return self.root / "providers.lock"

    @property
    def log_path(self) -> Path:
        return self.root / "log" / "sota-router.jsonl"

    @property
    def pid_path(self) -> Path:
        return self.root / "sota-router.pid"

    @property
    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.router_port}/healthz"


CODEX = Workspace(
    name="codex",
    label="Codex App",
    root=_resolve_workspace_root("CODEX_SOTA_CODEX_ROOT", ".codex-sota"),
    router_port=17895,
    router_starter=INSTALL_ROOT / "Start-CodexSotaRouter.ps1",
    protocol="responses",
)
CLAUDE = Workspace(
    name="claude",
    label="Claude Desktop",
    root=_resolve_workspace_root("CODEX_SOTA_CLAUDE_ROOT", ".claude-sota"),
    router_port=17994,
    router_starter=INSTALL_ROOT / "Start-ClaudeSotaRouter.ps1",
    protocol="messages",
    needs_catalog=False,
)
WORKSPACES = {CODEX.name: CODEX, CLAUDE.name: CLAUDE}

# Kept as module-level names so every existing caller and the router CLI keep working; they
# all point at the Codex workspace, which is the one that predates the split.
SOTA_ROOT = CODEX.root
REGISTRY_PATH = CODEX.registry_path
AUTH_PATH = CODEX.auth_path
SOURCE_CATALOG_PATH = CODEX.source_catalog_path
CATALOG_PATH = CODEX.catalog_path
SECRETS_ROOT = CODEX.secrets_root
DELETED_PROVIDERS_ROOT = CODEX.deleted_root
REGISTRY_LOCK_PATH = CODEX.lock_path
ROUTER_STARTER_PATH = CODEX.router_starter
REGISTRY_VERSION = 1
# Some gateways fingerprint the client and answer 401 "unauthorized client detected" to
# anything that does not look like Codex — ProviderA rejected the manager's own
# User-Agent while accepting the identical request from the app. Probes therefore
# identify themselves the way the CLI does; provider extra_headers still override this.
PROBE_USER_AGENT = "codex_cli_rs/0.144.1 (Windows 11.0.26200; x86_64) WindowsTerminal"
PROBE_ORIGINATOR = "codex_cli_rs"
# Wire protocols a provider can speak. "responses" is the OpenAI Responses shape the
# Codex App uses; "messages" is the Anthropic Messages shape Claude Desktop speaks in
# third-party inference mode. A gateway may offer both.
PROTOCOLS = ("responses", "messages")
# A Codex workspace still publishes this provider as a Responses model, but its upstream
# inference endpoint is Anthropic Messages.  Keep the marker in the registry so probes and the
# local router can opt into the translation without changing the protocol advertised to Codex.
RESPONSES_TO_ANTHROPIC_MESSAGES_ADAPTER = "responses_to_anthropic_messages"
# A gateway that only speaks OpenAI Chat Completions: neither Codex App (Responses) nor
# Claude Desktop (Messages) can talk to it directly, but the router can translate for the
# Codex side.  Same mechanism as the Messages adapter above, opposite direction.
RESPONSES_TO_CHAT_COMPLETIONS_ADAPTER = "responses_to_chat_completions"
# The mirror bridge: a Messages client (Claude Desktop) talking to a Chat-only gateway.
MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER = "messages_to_chat_completions"
REQUEST_ADAPTERS = (
    RESPONSES_TO_ANTHROPIC_MESSAGES_ADAPTER,
    RESPONSES_TO_CHAT_COMPLETIONS_ADAPTER,
    MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER,
)
# How many vendors one request may be handed to before giving up.
FAILOVER_MAX_ATTEMPTS = 3
REASONING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# What `protected` actually protects.  A protected entry is one whose *identity* this app does not
# own: the codex_auth provider borrows the Codex App's own login and base URL. Its model prefix is
# also pinned because it is part of the published slug written into config.toml and the catalog;
# changing it without a coordinated migration would retarget existing selections. Everything else
# about such a provider -- above all its model list and the enable/failover switches -- is ordinary
# user data, so it may still be edited.  Every field that can change where credentials, requests,
# or model slugs go is pinned back to the copy on disk instead of trusting a stale form, backup, or
# caller-supplied object.  Keep this list shared by the manager and the registry writer: adding an
# identity field in only one of those paths would reopen the silent-default/account-switch bug.
PROTECTED_PINNED_PROVIDER_KEYS = (
    "protected",
    "is_default",
    "auth_type",
    "secret_file",
    "entropy",
    "workspace",
    "request_adapter",
    "base_url",
    "prefix",
    "protocols",
    "auth_header",
    "auth_prefix",
    "models_path",
    "responses_path",
    "messages_path",
    "chat_path",
)
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# Two prefix shapes, both self-delimiting so `prefix + model id` stays one unambiguous slug:
#   `vendor--`           the original form, used wherever a provider speaks responses.
#   `vendor.anthropic.`  the Bedrock-style form, used by messages-only providers.
# The dotted form exists because Claude Desktop decides whether a model gets a thinking-effort
# control by canonicalizing the id and looking the result up in a hardcoded table. Its
# canonicalizer strips a leading `<label>.anthropic.` (the shape a Bedrock model id has) but
# knows nothing about `vendor--`, so only the dotted form lets a slug like
# `tango.anthropic.claude-opus-5` reach the table entry for `claude-opus-5`. The label is
# deliberately narrower than ID_PATTERN: Claude's own regex is `^(?:[a-z][a-z0-9-]*\.)?anthropic\.`,
# which rejects underscores, hence the `_` -> `-` swap in derive_model_prefix.
MESSAGES_PREFIX_SUFFIX = ".anthropic."
MODEL_PREFIX_PATTERN = re.compile(r"^(?:[a-z0-9][a-z0-9_-]*--|[a-z][a-z0-9-]*\.anthropic\.)$")
# A model may publish itself to Claude Desktop under a slug that is not `prefix + id`, for the
# one case the prefix cannot reach: a vendor that bakes the tier into the model name. Claude's
# canonicalizer strips `<label>.anthropic.`, a `[...]` suffix, `-vN`, `@date` and `-date`, but
# not `-thinking`, so `claude-opus-5-thinking` misses the capability table however it is
# prefixed. Publishing it as `claude-opus-5` reaches the table while the upstream still
# receives its own id. The shape allows exactly what a real Anthropic/Bedrock id contains.
PUBLISH_AS_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@-]*(?:\[[A-Za-z0-9._-]+\])?$"
)
HTTP_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
SECRET_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}\.dpapi$", re.IGNORECASE)
WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _has_forbidden_header_control(value: str) -> bool:
    return any((ord(character) < 32 and character != "\t") or ord(character) == 127 for character in value)


def derive_model_prefix(provider_id: str, protocols: Any = None) -> str:
    """The model prefix a provider gets when it did not choose one itself.

    Messages-only providers get the dotted form so Claude Desktop strips it and the model
    lands on that app's thinking-capability table (see MODEL_PREFIX_PATTERN). Anything that
    also speaks responses keeps `vendor--`: the prefix is opaque on the Codex side, the
    slugs there are pinned by config.toml's `model = ...`, and stamping `.anthropic.` onto
    a `gpt-...` id would claim a vendor the model does not come from.
    """
    label = str(provider_id).replace("_", "-")
    names = list(protocols or ())
    if "messages" in names and "responses" not in names:
        return label + MESSAGES_PREFIX_SUFFIX
    return label + "--"


def allows_legacy_bare_model(provider: dict[str, Any]) -> bool:
    """Whether this provider is allowed to publish a model without a vendor namespace.

    Claude Desktop's Messages-only compatibility profile predates the multi-vendor router and
    intentionally keeps its single default model bare.  Every other workspace/protocol must
    carry an explicit namespace: a missing prefix is provenance loss, and provenance loss must
    fail closed instead of selecting whichever account happens to be default.
    """
    workspace = str(provider.get("workspace") or CODEX.name)
    protocols = provider.get("protocols") or ["responses"]
    if isinstance(protocols, str):
        protocols = [protocols]
    return (
        workspace == CLAUDE.name
        and provider.get("is_default") is True
        and "messages" in protocols
        and "responses" not in protocols
    )


def effective_model_prefix(provider: dict[str, Any]) -> str:
    """Return the namespace to use even when handed an old, unvalidated registry entry.

    ``validate_provider`` repairs a missing prefix while loading the normal on-disk registry,
    but catalog builders, previews, and recovery/import paths can receive a raw provider
    dictionary.  Letting those callers concatenate ``"" + model_id`` recreates the exact
    provenance loss the router is meant to prevent: the picker advertises a bare model and a
    later request has no way to identify its billing account.  The only intentional exception is
    Claude's original Messages-only default, whose bare name is part of that product's profile.
    Invalid non-empty prefixes are left untouched here; the strict validator/router will reject
    them rather than silently retargeting a hand-edited configuration.
    """
    raw_prefix = provider.get("prefix")
    prefix = str(raw_prefix or "")
    if prefix or allows_legacy_bare_model(provider):
        return prefix
    provider_id = str(provider.get("id") or "").strip().lower()
    if not ID_PATTERN.fullmatch(provider_id):
        # A malformed raw entry must not acquire a guessed namespace.  Returning the empty
        # value keeps the caller's result visibly invalid; strict loading will fail closed.
        return prefix
    return derive_model_prefix(provider_id, provider.get("protocols"))


def allows_provider_publish_as(
    provider: dict[str, Any], publish_as: str, prefix: str | None = None
) -> bool:
    """Whether this provider's model may publish under ``publish_as`` safely.

    ``publish_as`` must never become a second way to erase the provider identity, so the
    alias always has to stay inside the provider's own published namespace:

    * Claude Messages-only providers publish Claude-shaped names
      (``juno.anthropic.claude-opus-5``) because Claude Desktop's capability lookup
      only recognises claude-* ids.  The one legacy default Messages profile may use a
      bare alias.
    * Codex Responses providers publish catalog-shaped names
      (``sierra--gpt-5.6-sol``) because the Codex App reads its capability metadata from
      the generated catalog, whose templates are keyed on known model names -- an unknown
      id silently falls back to the default GPT template with the wrong reasoning levels.

    Either way the upstream still receives ``model["id"]``; only the selectable name and
    the capability template change.
    """
    if not publish_as:
        return False
    protocols = provider.get("protocols") or ["responses"]
    if isinstance(protocols, str):
        protocols = [protocols]
    workspace = str(provider.get("workspace") or CODEX.name)
    if workspace == CLAUDE.name:
        if "messages" not in protocols or "responses" in protocols:
            return False
        if allows_legacy_bare_model(provider):
            return True
    elif workspace == CODEX.name:
        if "responses" not in protocols:
            return False
    else:
        return False
    namespace = prefix if prefix is not None else effective_model_prefix(provider)
    # A non-default provider must have a namespace after validation.  Require a non-empty suffix
    # as well, so the prefix itself cannot masquerade as a selectable model.
    return bool(namespace) and publish_as.startswith(namespace) and len(publish_as) > len(namespace)


# Historical name kept for callers written before the Codex side existed.
allows_claude_messages_publish_as = allows_provider_publish_as


def published_slug(provider: dict[str, Any], model: dict[str, Any]) -> str:
    """The slug clients select this model by -- `publish_as` when set, else `prefix + id`.

    Only the published slug is ever matched against a request or advertised to an app; the
    upstream always receives `model["id"]`. Keeping the two apart is what lets a model whose
    vendor id defeats a client's capability lookup still be offered under a name the app
    recognizes: a Claude-shaped alias for Claude Desktop, a catalog-shaped one for Codex.
    The alias must always retain the provider namespace, or be the legacy default's bare
    Claude alias.
    """
    # A raw or hand-edited registry may still contain a malformed/bare alias; ignoring it
    # here prevents it from bypassing the provider namespace before strict validation has
    # had a chance to run.
    override = str(model.get("publish_as") or "").strip()
    if override and allows_provider_publish_as(provider, override):
        return override
    return effective_model_prefix(provider) + str(model.get("id") or "")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def registry_write_lock(
    timeout_seconds: float = 15.0, workspace: Workspace | None = None
):
    """Serialize registry/catalog/secret transactions across manager processes."""
    workspace = workspace or default_workspace()
    lock_path = workspace.lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another codex-sota configuration update is still running")
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _blob(data: bytes) -> tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data)
    value = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return value, buffer


def dpapi_protect(value: str, path: Path, entropy_label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("API key is empty")
    raw = value.strip().encode("utf-8")
    entropy = entropy_label.encode("utf-8")
    input_blob, input_buffer = _blob(raw)
    entropy_blob, entropy_buffer = _blob(entropy)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    try:
        if not crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            1,  # CRYPTPROTECT_UI_FORBIDDEN: saving must never open a hidden prompt.
            ctypes.byref(output_blob),
        ):
            raise ctypes.WinError()
        protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="ascii") as handle:
                handle.write(base64.b64encode(protected).decode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)
        raw = b""
        del input_buffer, entropy_buffer


def dpapi_unprotect(path: Path, entropy_label: str) -> str:
    protected = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
    entropy = entropy_label.encode("utf-8")
    input_blob, input_buffer = _blob(protected)
    entropy_blob, entropy_buffer = _blob(entropy)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        1,  # Do not let a background router wait on a Windows credential dialog.
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return raw.decode("utf-8")
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(output_blob.pbData)
        del input_buffer, entropy_buffer


def read_codex_auth_key(auth_path: Path = AUTH_PATH) -> str:
    auth = json.loads(auth_path.read_text(encoding="utf-8-sig"))
    key = auth.get("OPENAI_API_KEY")
    if auth.get("auth_mode") != "apikey" or not isinstance(key, str) or not key.strip():
        raise RuntimeError("Tango Relay auth.json does not contain a usable API-key login")
    return key.strip()


def default_workspace() -> Workspace:
    """The Codex workspace, resolved through WORKSPACES so tests can redirect it.

    Binding it as a default argument would freeze the real paths at import time, which is
    exactly what made the transactional tests snapshot the live registry instead of their
    temporary one.
    """
    return WORKSPACES.get(CODEX.name, CODEX)


def provider_workspace(provider: dict[str, Any]) -> Workspace:
    """Which config root a provider belongs to, carried on the provider itself.

    Stamping the home onto each entry means every probe and credential lookup resolves the
    right secrets directory without threading a workspace argument through a dozen
    signatures — and it survives a round trip through providers.json.
    """
    name = str(provider.get("workspace") or CODEX.name)
    try:
        return WORKSPACES[name]
    except KeyError as error:
        raise ValueError(f"Provider {provider.get('id')!r} has an unknown workspace: {name!r}") from error


def uses_responses_to_anthropic_messages(provider: dict[str, Any]) -> bool:
    """Use the explicit adapter setting for any Responses-to-Messages provider."""
    return (
        str(provider.get("request_adapter") or "").strip()
        == RESPONSES_TO_ANTHROPIC_MESSAGES_ADAPTER
    )


def uses_messages_to_chat_completions(provider: dict[str, Any]) -> bool:
    """Whether this provider is an OpenAI-Chat-only gateway bridged for Claude Desktop."""
    return (
        str(provider.get("request_adapter") or "").strip()
        == MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER
    )


def uses_responses_to_chat_completions(provider: dict[str, Any]) -> bool:
    """Whether this provider is an OpenAI-Chat-only gateway bridged for the Codex App.

    Any gateway can explicitly opt into this adapter.
    """
    return (
        str(provider.get("request_adapter") or "").strip()
        == RESPONSES_TO_CHAT_COMPLETIONS_ADAPTER
    )


def pin_protected_provider_identity(
    candidate: dict[str, Any], current: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Pin a protected provider's routing/credential identity to its on-disk copy.

    The manager normally edits a provider from a snapshot that may be stale, and restore files
    are intentionally redacted.  Treating that snapshot as authoritative for a protected entry
    can silently change the account used by a later request (or turn the default back into a bare
    model).  The fields in :data:`PROTECTED_PINNED_PROVIDER_KEYS` therefore come from ``current``
    whenever they exist.  A field absent on disk is removed from the candidate rather than being
    invented by a stale caller.  The returned list contains field names whose incoming values were
    discarded so UI callers can explain the repair; an empty list means the candidate already
    matched the on-disk identity.

    A fresh/unprotected provider is copied unchanged.  Returning a deep copy keeps this helper
    side-effect free for callers that still need to inspect the submitted object after pinning.
    """
    pinned = deepcopy(candidate)
    if not isinstance(current, dict) or not current.get("protected"):
        return pinned, []

    changed: list[str] = []
    for key in PROTECTED_PINNED_PROVIDER_KEYS:
        if key in current:
            value = deepcopy(current[key])
            if key not in pinned or pinned[key] != value:
                changed.append(key)
            pinned[key] = value
        elif key in pinned:
            # Do not let a newly introduced field smuggle an identity change into an older,
            # protected entry whose on-disk representation never had that field.
            changed.append(key)
            pinned.pop(key, None)
    return pinned, changed


def _valid_secret_file_name(file_name: str) -> bool:
    """Reject traversal, NTFS alternate streams and Windows device aliases."""
    if not SECRET_FILE_PATTERN.fullmatch(file_name):
        return False
    first_component = file_name.split(".", 1)[0].lower()
    return first_component not in WINDOWS_DEVICE_NAMES


def secret_path(provider: dict[str, Any]) -> Path:
    file_name = str(provider.get("secret_file") or "")
    if not _valid_secret_file_name(file_name):
        raise ValueError(f"Provider {provider.get('id')!r} has an invalid secret_file")
    root = provider_workspace(provider).secrets_root
    candidate = root / file_name
    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
    except OSError as error:
        raise ValueError(f"Provider {provider.get('id')!r} has an invalid secret_file") from error
    if resolved_candidate.parent != resolved_root:
        raise ValueError(f"Provider {provider.get('id')!r} has an invalid secret_file")
    return candidate


class CredentialOwnershipError(ValueError):
    """A provider claims another identity's encrypted credential file."""


def _secret_identity(provider: dict[str, Any]) -> str:
    # The application runs on Windows: differently cased spellings are not independent files.
    return str(secret_path(provider).resolve()).replace("\\", "/").casefold()


def _secret_owners(providers: list[dict[str, Any]]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for provider in providers:
        if provider.get("auth_type") != "dpapi":
            continue
        identity = _secret_identity(provider)
        owners.setdefault(identity, set()).add(str(provider["id"]))
    return owners


def _validate_secret_ownership(
    providers: list[dict[str, Any]],
    previous_providers: list[dict[str, Any]] | None = None,
) -> None:
    previous = _secret_owners(previous_providers or [])
    for identity, owners in _secret_owners(providers).items():
        # Existing shared references remain readable/editable, and removing a claimant is
        # safe. A new claimant (including a rename) must choose its own credential file.
        # Check the previous owners even when the proposed registry has only one owner:
        # removing an identity must not let its replacement inherit the old credential.
        previous_owners = previous.get(identity, set())
        if (len(owners) > 1 or previous_owners) and not owners.issubset(previous_owners):
            raise CredentialOwnershipError(
                f"Encrypted credential file {identity!r} is claimed by providers "
                f"{', '.join(sorted(previous_owners | owners))}; "
                "use a separate secret_file for each provider"
            )


def provider_key(provider: dict[str, Any], temporary_key: str | None = None) -> str:
    if temporary_key and temporary_key.strip():
        return temporary_key.strip()
    auth_type = provider.get("auth_type", "dpapi")
    if auth_type == "codex_auth":
        return read_codex_auth_key(provider_workspace(provider).auth_path)
    if auth_type != "dpapi":
        raise ValueError(f"Unsupported auth_type: {auth_type}")
    path = secret_path(provider)
    if not path.exists():
        raise FileNotFoundError(f"Encrypted API key is missing for {provider.get('name')}")
    value = dpapi_unprotect(path, str(provider.get("entropy") or ""))
    if not value.strip():
        raise RuntimeError(f"Encrypted API key is empty for {provider.get('name')}")
    return value.strip()


def normalize_base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("Base URL must be an absolute HTTP or HTTPS URL")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Remote provider Base URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Base URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL must not contain a query string or fragment")
    return text


def normalize_path(value: str, fallback: str) -> str:
    text = str(value or fallback).strip()
    if text.startswith("https://") or text.startswith("http://"):
        return normalize_base_url(text)
    if not text.startswith("/"):
        text = "/" + text
    return text


ENDPOINT_KEYS = {
    "models": ("models_path", "/models"),
    "responses": ("responses_path", "/responses"),
    "messages": ("messages_path", "/v1/messages"),
    "chat": ("chat_path", "/v1/chat/completions"),
}


def endpoint_url(provider: dict[str, Any], kind: str) -> str:
    key, fallback = ENDPOINT_KEYS.get(kind, ENDPOINT_KEYS["responses"])
    path = normalize_path(str(provider.get(key) or fallback), fallback)
    if path.startswith("https://") or path.startswith("http://"):
        return path
    return normalize_base_url(str(provider.get("base_url") or "")) + path


def auth_headers(
    provider: dict[str, Any], key: str, include_probe_defaults: bool = True
) -> dict[str, str]:
    raw_header = str(provider.get("auth_header") or "Authorization")
    if _has_forbidden_header_control(raw_header):
        raise ValueError("Authentication header name contains a forbidden control character")
    header = raw_header.strip()
    if not HTTP_HEADER_NAME_PATTERN.fullmatch(header):
        raise ValueError("Authentication header name is invalid")
    headers: dict[str, str] = {}
    if include_probe_defaults:
        headers.update(
            {
                "Accept": "application/json",
                "User-Agent": PROBE_USER_AGENT,
                "originator": PROBE_ORIGINATOR,
            }
        )
    extra = provider.get("extra_headers") or {}
    if not isinstance(extra, dict):
        raise ValueError("extra_headers must be a JSON object")
    for name, value in extra.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("extra_headers keys and values must be strings")
        if not HTTP_HEADER_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid HTTP header name: {name!r}")
        if _has_forbidden_header_control(value):
            raise ValueError(f"HTTP header {name!r} contains a forbidden control character")
        if name.lower() in {"authorization", "content-length", "host", header.lower()}:
            continue
        # HTTP header names are case-insensitive. Remove an earlier spelling before adding
        # its override, including probe defaults, so urllib cannot reverse the winner later.
        for previous in list(headers):
            if previous.lower() == name.lower():
                del headers[previous]
        headers[name] = value
    prefix = str(provider.get("auth_prefix") if provider.get("auth_prefix") is not None else "Bearer ")
    auth_value = prefix + key
    if _has_forbidden_header_control(auth_value):
        raise ValueError("Authentication header value contains a forbidden control character")
    for previous in list(headers):
        if previous.lower() == header.lower():
            del headers[previous]
    headers[header] = auth_value
    return headers


def _read_http_body(response: Any, limit: int = 2_000_000) -> bytes:
    return response.read(limit)


def _http_json(
    url: str,
    method: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: int,
) -> tuple[int, Any, str]:
    request_headers = dict(headers)
    data = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        response = build_network_opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = int(response.status)
        raw = _read_http_body(response)
        text = raw.decode("utf-8", errors="replace")
        try:
            body = json.loads(text) if text else None
        except json.JSONDecodeError:
            body = None
        # Some gateways (notably Cloudflare/WAF layers) reject a model with an
        # empty body. Preserve only non-sensitive response headers so the caller
        # can distinguish an upstream rejection from a local transport failure.
        if status >= 300 and body is None and not text.strip():
            diagnostic_headers = (
                ("Server", response.headers.get("Server")),
                ("CF-RAY", response.headers.get("CF-RAY")),
                ("X-Request-ID", response.headers.get("X-Request-ID")),
                ("Content-Type", response.headers.get("Content-Type")),
            )
            details = [f"{name}={value.strip()}" for name, value in diagnostic_headers if value and value.strip()]
            text = "上游返回空响应（未提供错误正文）"
            if details:
                text += "；" + "; ".join(details)
        return status, body, text[:4000]


def _probe_http_json(
    url: str,
    method: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: int,
    attempts: int = 3,
) -> tuple[int, Any, str]:
    """`_http_json` with bounded retries for transport-level failures.

    Probes are tiny, idempotent, and the networks in front of these gateways drop a
    connection mid-TLS-handshake often enough (SSL UNEXPECTED_EOF, remote reset, timeout)
    that a single attempt reported healthy vendors as failed.  HTTP error statuses are NOT
    retried: they come back inside a successful transport round trip and mean the vendor
    answered, which is the thing being tested.
    """
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(1.5 * attempt)
        try:
            return _http_json(url, method, headers, payload, timeout)
        except urllib.error.HTTPError:
            raise
        except Exception as error:  # noqa: BLE001 - transport failures are retryable here
            last_error = error
    assert last_error is not None
    raise last_error


def _redact_text(value: str, secrets: list[str]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


def _model_ids(body: Any) -> list[str]:
    if isinstance(body, dict):
        candidates = body.get("data")
        if not isinstance(candidates, list):
            candidates = body.get("models")
    elif isinstance(body, list):
        candidates = body
    else:
        candidates = None
    if not isinstance(candidates, list):
        return []
    result: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("slug") or item.get("name")
        else:
            continue
        if isinstance(model_id, str) and model_id.strip() and model_id.strip() not in result:
            result.append(model_id.strip())
    return sorted(result, key=str.casefold)


def _model_capability_metadata(body: Any) -> dict[str, dict[str, Any]]:
    """Retain only explicit, bounded capability hints returned with model ids."""
    if isinstance(body, dict):
        candidates = body.get("data")
        if not isinstance(candidates, list):
            candidates = body.get("models")
    elif isinstance(body, list):
        candidates = body
    else:
        candidates = None
    if not isinstance(candidates, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("slug") or item.get("name")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        metadata: dict[str, Any] = {}
        context_window = next(
            (
                item.get(key)
                for key in ("context_window_tokens", "context_window", "context_length")
                if type(item.get(key)) is int
            ),
            None,
        )
        if context_window is not None and 0 < context_window <= 100_000_000:
            metadata["context_window_tokens"] = context_window
        for key in ("supports_1m_context", "supports1m"):
            explicit_1m = item.get(key)
            if type(explicit_1m) is bool:
                metadata["supports_1m_context"] = explicit_1m
                break
        if metadata:
            result[model_id.strip()] = metadata
    return result


def _candidate_model_paths(provider: dict[str, Any]) -> list[str]:
    configured = normalize_path(str(provider.get("models_path") or "/models"), "/models")
    candidates = [configured]
    base_path = urllib.parse.urlsplit(normalize_base_url(str(provider.get("base_url") or ""))).path.rstrip("/")
    if configured != "/models":
        candidates.append("/models")
    if not base_path.endswith("/v1"):
        candidates.append("/v1/models")
    unique: list[str] = []
    for value in candidates:
        if value not in unique:
            unique.append(value)
    return unique


def discover_models(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    key = provider_key(item, temporary_key)
    errors: list[dict[str, Any]] = []
    try:
        for path in _candidate_model_paths(item):
            probe = dict(item)
            probe["models_path"] = path
            url = endpoint_url(probe, "models")
            try:
                status, body, text = _http_json(
                    url,
                    "GET",
                    auth_headers(item, key),
                    None,
                    int(item.get("timeout_seconds") or 20),
                )
            except Exception as error:
                errors.append({"url": url, "error": _redact_text(str(error), [key])})
                continue
            models = _model_ids(body)
            if 200 <= status < 300 and models:
                return {
                    "ok": True,
                    "status": status,
                    "url": url,
                    "models_path": path,
                    "models": models,
                    "model_metadata": _model_capability_metadata(body),
                }
            errors.append(
                {
                    "url": url,
                    "status": status,
                    "detail": _redact_text(text[:500], [key]),
                }
            )
    finally:
        key = ""
    return {"ok": False, "models": [], "errors": errors}


def candidate_responses_paths(provider: dict[str, Any]) -> list[str]:
    """Ordered guesses for the Responses path, the configured one first.

    Mirrors _candidate_model_paths, plus the two repairs that actually bite in practice:
    a missing /v1 and a doubled one. The model list path is the strongest hint available —
    if listing works under /v1 then Responses almost certainly lives there too.
    """
    configured = normalize_path(str(provider.get("responses_path") or "/responses"), "/responses")
    base_path = urllib.parse.urlsplit(
        normalize_base_url(str(provider.get("base_url") or ""))
    ).path.rstrip("/")
    models_path = normalize_path(str(provider.get("models_path") or "/models"), "/models")
    candidates = [configured]
    if configured.startswith(("https://", "http://")):
        return candidates
    if models_path.startswith("/v1/") and not configured.startswith("/v1/"):
        candidates.append("/v1" + configured)
    if configured.startswith("/v1/v1/"):
        candidates.append(configured[3:])
    if base_path.endswith("/v1") and configured.startswith("/v1/"):
        candidates.append(configured[3:])
    if not base_path.endswith("/v1"):
        candidates.append("/v1/responses")
    candidates.append("/responses")
    unique: list[str] = []
    for value in candidates:
        if value not in unique:
            unique.append(value)
    return unique


def discover_responses_path(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    """Probe candidate Responses paths and keep the first that returns a real result.

    Status alone cannot decide this: the wrong path on a single-page-app gateway answers
    200 with HTML, so every candidate is checked for Responses shape as well.
    """
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    enabled = [m["id"] for m in item.get("models") or [] if m.get("enabled")]
    model = enabled[0] if enabled else None
    if model is None:
        return {"ok": False, "responses_path": None, "reason": "该供应商没有启用任何模型", "attempts": []}
    key = provider_key(item, temporary_key)
    # Probes are tiny health checks, not generations: the provider's inference
    # timeout (300 s for long silent reasoning) must not turn a hung gateway into a
    # five-minute stall per probe attempt in the GUI's test and checkup flows.
    timeout = min(120, max(30, int(item.get("timeout_seconds") or 120)))
    payload = {
        "model": model,
        "input": "Reply exactly OK",
        "stream": False,
        "max_output_tokens": 64,
        "reasoning": {"effort": "low"},
    }
    attempts: list[dict[str, Any]] = []
    try:
        for path in candidate_responses_paths(item):
            probe = dict(item)
            probe["responses_path"] = path
            url = endpoint_url(probe, "responses")
            try:
                status, body, text = _http_json(
                    url, "POST", auth_headers(item, key), payload, timeout
                )
            except Exception as error:
                attempts.append({"path": path, "status": None, "note": _redact_text(str(error), [key])[:160]})
                continue
            if not 200 <= status < 300:
                attempts.append({"path": path, "status": status, "note": f"HTTP {status}"})
                continue
            problem = response_shape_problem(body, text)
            if problem:
                attempts.append({"path": path, "status": status, "note": _redact_text(problem, [key])[:160]})
                continue
            return {"ok": True, "responses_path": path, "model": model, "attempts": attempts}
    finally:
        key = ""
    return {
        "ok": False,
        "responses_path": None,
        "model": model,
        "reason": "试过的路径都没有返回 Responses 结果",
        "attempts": attempts,
    }


def auto_repair_responses_path(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    """Probe and correct provider['responses_path'] in place.

    Returns {"changed": bool, "before": str, "after": str|None, "reason": str}. Callers
    persist the provider themselves; this only decides what the path should be.
    """
    before = normalize_path(str(provider.get("responses_path") or "/responses"), "/responses")
    try:
        result = discover_responses_path(provider, temporary_key)
    except Exception as error:
        return {"changed": False, "before": before, "after": None, "reason": str(error)}
    if not result["ok"]:
        return {
            "changed": False,
            "before": before,
            "after": None,
            "reason": result.get("reason") or "没有可用的 Responses 路径",
        }
    after = result["responses_path"]
    if after == before:
        return {"changed": False, "before": before, "after": after, "reason": "路径本来就是对的"}
    provider["responses_path"] = after
    return {
        "changed": True,
        "before": before,
        "after": after,
        "reason": f"{before} 打不到 Responses API，自动改成 {after}",
    }


def candidate_messages_paths(provider: dict[str, Any]) -> list[str]:
    """Ordered guesses for an Anthropic Messages endpoint."""
    configured = normalize_path(
        str(provider.get("messages_path") or "/v1/messages"), "/v1/messages"
    )
    base_path = urllib.parse.urlsplit(
        normalize_base_url(str(provider.get("base_url") or ""))
    ).path.rstrip("/")
    models_path = normalize_path(str(provider.get("models_path") or "/models"), "/models")
    candidates = [configured]
    if configured.startswith(("https://", "http://")):
        return candidates
    if models_path.startswith("/v1/") and not configured.startswith("/v1/"):
        candidates.append("/v1" + configured)
    if configured.startswith("/v1/v1/"):
        candidates.append(configured[3:])
    if base_path.endswith("/v1") and configured.startswith("/v1/"):
        candidates.append(configured[3:])
    if not base_path.endswith("/v1"):
        candidates.append("/v1/messages")
    candidates.append("/messages")
    unique: list[str] = []
    for value in candidates:
        if value not in unique:
            unique.append(value)
    return unique


def discover_messages_path(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    """Probe candidate Messages paths and verify an Anthropic-shaped response."""
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    enabled = [m["id"] for m in item.get("models") or [] if m.get("enabled")]
    model = enabled[0] if enabled else None
    if model is None:
        return {"ok": False, "messages_path": None, "reason": "该供应商没有启用任何模型", "attempts": []}
    key = provider_key(item, temporary_key)
    # Probes are tiny health checks, not generations: the provider's inference
    # timeout (300 s for long silent reasoning) must not turn a hung gateway into a
    # five-minute stall per probe attempt in the GUI's test and checkup flows.
    timeout = min(120, max(30, int(item.get("timeout_seconds") or 120)))
    payload = {
        "model": model,
        "max_tokens": 64,
        "stream": False,
        "messages": [{"role": "user", "content": "Reply exactly OK"}],
    }
    attempts: list[dict[str, Any]] = []
    try:
        for path in candidate_messages_paths(item):
            probe = dict(item)
            probe["messages_path"] = path
            url = endpoint_url(probe, "messages")
            try:
                status, body, text = _http_json(
                    url,
                    "POST",
                    auth_headers(item, key) | {"anthropic-version": ANTHROPIC_VERSION},
                    payload,
                    timeout,
                )
            except Exception as error:
                attempts.append({"path": path, "status": None, "note": _redact_text(str(error), [key])[:160]})
                continue
            if not 200 <= status < 300:
                attempts.append({"path": path, "status": status, "note": f"HTTP {status}"})
                continue
            problem = response_shape_problem(body, text, "messages")
            if problem:
                attempts.append({"path": path, "status": status, "note": _redact_text(problem, [key])[:160]})
                continue
            return {"ok": True, "messages_path": path, "model": model, "attempts": attempts}
    finally:
        key = ""
    return {
        "ok": False,
        "messages_path": None,
        "model": model,
        "reason": "试过的路径都没有返回 Messages 结果",
        "attempts": attempts,
    }


def auto_repair_messages_path(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    """Probe and correct provider['messages_path'] in place."""
    before = normalize_path(
        str(provider.get("messages_path") or "/v1/messages"), "/v1/messages"
    )
    try:
        result = discover_messages_path(provider, temporary_key)
    except Exception as error:
        return {"changed": False, "before": before, "after": None, "reason": str(error)}
    if not result.get("ok"):
        return {
            "changed": False,
            "before": before,
            "after": None,
            "reason": result.get("reason") or "没有可用的 Messages 路径",
        }
    after = result["messages_path"]
    if after == before:
        return {"changed": False, "before": before, "after": after, "reason": "路径本来就是对的"}
    provider["messages_path"] = after
    return {
        "changed": True,
        "before": before,
        "after": after,
        "reason": f"{before} 打不到 Messages API，自动改成 {after}",
    }


def candidate_chat_paths(provider: dict[str, Any]) -> list[str]:
    """Ordered Chat Completions paths without guessing another configured origin."""
    configured = normalize_path(
        str(provider.get("chat_path") or "/v1/chat/completions"), "/v1/chat/completions"
    )
    candidates = [configured]
    if configured.startswith(("https://", "http://")):
        return candidates
    base_path = urllib.parse.urlsplit(
        normalize_base_url(str(provider.get("base_url") or ""))
    ).path.rstrip("/")
    models_path = normalize_path(str(provider.get("models_path") or "/models"), "/models")
    if configured.startswith("/v1/v1/"):
        candidates.append(configured[3:])
    if base_path.endswith("/v1"):
        if configured.startswith("/v1/"):
            candidates.append(configured[3:])
    else:
        if models_path.startswith("/v1/") and not configured.startswith("/v1/"):
            candidates.append("/v1" + configured)
        candidates.append("/v1/chat/completions")
    candidates.append("/chat/completions")
    return list(dict.fromkeys(candidates))


def discover_chat_path(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    """Probe Chat endpoints with a Chat payload, regardless of the client-side protocol."""
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    enabled = [model["id"] for model in item.get("models") or [] if model.get("enabled")]
    model = enabled[0] if enabled else None
    if model is None:
        return {"ok": False, "chat_path": None, "reason": "该供应商没有启用任何模型", "attempts": []}
    key = provider_key(item, temporary_key)
    timeout = min(120, max(30, int(item.get("timeout_seconds") or 120)))
    payload = {
        "model": model,
        "max_tokens": 64,
        "stream": False,
        "messages": [{"role": "user", "content": "Reply exactly OK"}],
    }
    attempts: list[dict[str, Any]] = []
    try:
        for path in candidate_chat_paths(item):
            probe = dict(item)
            probe["chat_path"] = path
            url = endpoint_url(probe, "chat")
            try:
                status, body, text = _http_json(
                    url, "POST", auth_headers(item, key), payload, timeout
                )
            except Exception as error:
                attempts.append({"path": path, "status": None, "note": _redact_text(str(error), [key])[:160]})
                continue
            if not 200 <= status < 300:
                attempts.append({"path": path, "status": status, "note": f"HTTP {status}"})
                continue
            problem = response_shape_problem(body, text, "chat")
            if problem is None:
                choices = body["choices"]
                if not any(
                    isinstance(choice, dict)
                    and isinstance(choice.get("message"), dict)
                    and any(field in choice["message"] for field in ("content", "tool_calls"))
                    for choice in choices
                ):
                    problem = "Chat Completions 结果没有可用的 assistant message"
            if problem:
                attempts.append({"path": path, "status": status, "note": _redact_text(problem, [key])[:160]})
                continue
            return {"ok": True, "chat_path": path, "model": model, "attempts": attempts}
    finally:
        key = ""
    return {
        "ok": False,
        "chat_path": None,
        "model": model,
        "reason": "试过的路径都没有返回 Chat Completions 结果",
        "attempts": attempts,
    }


def auto_repair_chat_path(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    """Probe and correct only provider['chat_path']; callers choose when to persist it."""
    before = normalize_path(
        str(provider.get("chat_path") or "/v1/chat/completions"), "/v1/chat/completions"
    )
    try:
        result = discover_chat_path(provider, temporary_key)
    except Exception as error:
        return {"changed": False, "before": before, "after": None, "reason": str(error)}
    if not result.get("ok"):
        return {
            "changed": False,
            "before": before,
            "after": None,
            "reason": result.get("reason") or "没有可用的 Chat Completions 路径",
        }
    after = result["chat_path"]
    if after == before:
        return {"changed": False, "before": before, "after": after, "reason": "路径本来就是对的"}
    provider["chat_path"] = after
    return {
        "changed": True,
        "before": before,
        "after": after,
        "reason": f"{before} 打不到 Chat Completions API，自动改成 {after}",
    }


def measure_latency(
    provider: dict[str, Any],
    model_id: str,
    samples: int = 3,
    temporary_key: str | None = None,
) -> dict[str, Any]:
    """Time a few plain non-streaming requests so vendors serving one model can be ranked.

    Reports the median rather than the mean: these gateways throw the occasional 30-second
    outlier, and one of those would drag an average far away from what you actually feel.
    """
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    model = str(model_id or "").strip()
    if not model:
        raise ValueError("Model ID is empty")
    key = provider_key(item, temporary_key)
    # Probes are tiny health checks, not generations: the provider's inference
    # timeout (300 s for long silent reasoning) must not turn a hung gateway into a
    # five-minute stall per probe attempt in the GUI's test and checkup flows.
    timeout = min(120, max(30, int(item.get("timeout_seconds") or 120)))
    url, payload, extra, protocol = probe_request(item, model)
    timings: list[float] = []
    failures: list[str] = []
    try:
        for _ in range(max(1, samples)):
            started = time.monotonic()
            try:
                status, body, text = _probe_http_json(
                    url, "POST", auth_headers(item, key) | extra, payload, timeout
                )
            except Exception as error:
                failures.append(_redact_text(str(error), [key])[:200])
                continue
            if not 200 <= status < 300:
                failures.append(f"HTTP {status}")
                continue
            problem = response_shape_problem(body, text, protocol)
            if problem:
                failures.append(_redact_text(problem, [key])[:200])
                continue
            timings.append(time.monotonic() - started)
    finally:
        key = ""
    return {
        "provider": item["id"],
        "name": item.get("name") or item["id"],
        "model": model,
        "ok": len(timings),
        "attempts": max(1, samples),
        "median_seconds": round(statistics.median(timings), 2) if timings else None,
        "best_seconds": round(min(timings), 2) if timings else None,
        "failures": failures[:3],
    }


def response_shape_problem(body: Any, text: str, protocol: str = "responses") -> str | None:
    """Why a 2xx body is not a Responses-API result, or None when it looks like one.

    A gateway whose SPA catch-all answers the wrong path returns 200 with an HTML page,
    which passes a status-only check and then fails in the app as "stream closed before
    response.completed". Checking the shape here is what turns that into a clear verdict.
    """
    if not isinstance(body, dict):
        head = (text or "").strip()[:80].replace("\n", " ")
        if head.lower().startswith(("<!doctype", "<html")):
            if protocol == "chat":
                return (
                    "上游返回的是 HTML 网页而不是 Chat Completions 结果，"
                    "通常说明路径写错了（检查 /chat/completions 或 /v1/chat/completions）"
                )
            if protocol == "messages":
                return (
                    "上游返回的是 HTML 网页（打到了网站首页）而不是 API 结果。"
                    "两种可能：Anthropic Messages 路径写错了（试试 /v1/messages），"
                    "或者这家网关根本不提供 Anthropic 端点、只支持 OpenAI Chat Completions"
                    "——这种网关无法在 Claude Desktop 里直接使用"
                )
            return (
                "上游返回的是 HTML 网页而不是 API 结果，"
                "通常说明 Responses 路径写错了（比如少了 /v1）；"
                "如果各路径都如此，这家网关可能只支持 OpenAI Chat Completions——"
                "请在「供应商配置」页勾选「只支持 Chat Completions 网关（自动桥接）」"
                "并保存后再测试，桥接会自动完成协议转换"
            )
        return f"上游返回的不是 JSON 对象：{head!r}"
    if protocol == "messages":
        if body.get("type") == "message" or "content" in body:
            return None
        return f"返回的 JSON 不像 Anthropic message，顶层键：{sorted(body)[:6]}"
    if protocol == "chat":
        if isinstance(body.get("choices"), list):
            return None
        return f"返回的 JSON 不像 Chat Completions 结果，顶层键：{sorted(body)[:6]}"
    if body.get("object") == "response":
        return None
    if any(field in body for field in ("output", "output_text", "status", "id")):
        return None
    return f"返回的 JSON 里没有 Responses 结果字段，顶层键：{sorted(body)[:6]}"


ANTHROPIC_VERSION = "2023-06-01"


def probe_protocol(provider: dict[str, Any]) -> str:
    """Which wire protocol to probe a provider with.

    Prefers the protocol its workspace speaks — a Claude provider must be probed with
    Anthropic Messages, not the OpenAI Responses shape, or a perfectly healthy gateway
    comes back "failed" purely because it was asked the wrong question.
    """
    if uses_responses_to_anthropic_messages(provider):
        return "messages"
    if uses_responses_to_chat_completions(provider):
        return "chat"
    if uses_messages_to_chat_completions(provider):
        return "chat"
    supported = [p for p in (provider.get("protocols") or ["responses"]) if p in PROTOCOLS]
    if not supported:
        supported = ["responses"]
    preferred = provider_workspace(provider).protocol
    return preferred if preferred in supported else supported[0]


def probe_request(
    provider: dict[str, Any],
    model: str,
    reasoning_effort: str = "low",
    service_tier: str | None = None,
    max_tokens: int = 64,
    prompt: str = "Reply exactly OK",
) -> tuple[str, dict[str, Any], dict[str, str], str]:
    """(url, payload, extra headers, protocol) for one minimal completion."""
    protocol = probe_protocol(provider)
    if protocol == "chat":
        url = endpoint_url(provider, "chat")
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
        extra = {}
    elif protocol == "messages":
        url = endpoint_url(provider, "messages")
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
        extra = {"anthropic-version": ANTHROPIC_VERSION}
    else:
        url = endpoint_url(provider, "responses")
        payload = {
            "model": model,
            "input": prompt,
            "stream": False,
            "max_output_tokens": max_tokens,
        }
        if reasoning_effort and reasoning_effort != "none":
            payload["reasoning"] = {"effort": reasoning_effort}
        extra = {}
    if service_tier:
        payload["service_tier"] = service_tier
    return url, payload, extra, protocol


def test_model(
    provider: dict[str, Any],
    model_id: str,
    temporary_key: str | None = None,
    reasoning_effort: str = "low",
) -> dict[str, Any]:
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    model = str(model_id or "").strip()
    if not model:
        raise ValueError("Model ID is empty")
    key = provider_key(item, temporary_key)
    redaction_key = key
    url, payload, extra, protocol = probe_request(item, model, reasoning_effort)
    try:
        status, body, text = _probe_http_json(
            url,
            "POST",
            auth_headers(item, key) | extra,
            payload,
            max(30, int(item.get("timeout_seconds") or 120)),
        )
    except Exception as error:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "error": _redact_text(str(error), [redaction_key]),
        }
    finally:
        key = ""
    detail = ""
    if not 200 <= status < 300:
        head_probe = (text or "").strip()[:200].lower()
        if isinstance(body, dict):
            detail = json.dumps(body, ensure_ascii=False, separators=(",", ":"))[:1500]
        elif head_probe.startswith(("<!doctype", "<html")):
            # Dumping a gateway's HTML homepage into the failure dialog told the user
            # nothing; the shape checker's protocol-aware explanation does.
            hint = response_shape_problem(None, text, protocol) or "上游返回了 HTML 网页"
            detail = f"HTTP {status}：{hint}"
        else:
            detail = text[:1500] or f"HTTP {status}：上游返回空响应（未提供错误正文）"
        detail = _redact_text(detail, [redaction_key])
        return {"ok": False, "status": status, "url": url, "model": model, "detail": detail}
    shape_problem = response_shape_problem(body, text, protocol)
    if shape_problem:
        return {
            "ok": False,
            "status": status,
            "url": url,
            "model": model,
            "detail": _redact_text(shape_problem, [redaction_key]),
        }
    return {"ok": True, "status": status, "url": url, "model": model, "detail": ""}


PRIORITY_SERVICE_TIER = {
    "id": "priority",
    "name": "Fast",
    "description": "1.5x speed, increased usage",
}


def probe_fast_tier(
    provider: dict[str, Any],
    model_id: str,
    temporary_key: str | None = None,
    reasoning_effort: str = "low",
) -> dict[str, Any]:
    """Check whether an upstream accepts service_tier=priority for one model.

    A 2xx only proves the field was not rejected, never that the tier is honoured. A
    non-2xx is blamed on the tier only when the identical request succeeds without it,
    so a model that is broken anyway comes back "unknown" instead of "unsupported".
    """
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    model = str(model_id or "").strip()
    if not model:
        raise ValueError("Model ID is empty")
    key = provider_key(item, temporary_key)
    redaction_key = key
    # Probes are tiny health checks, not generations: the provider's inference
    # timeout (300 s for long silent reasoning) must not turn a hung gateway into a
    # five-minute stall per probe attempt in the GUI's test and checkup flows.
    timeout = min(120, max(30, int(item.get("timeout_seconds") or 120)))

    def attempt(with_tier: bool) -> tuple[int | None, str, float]:
        url, payload, extra, protocol = probe_request(
            item, model, reasoning_effort, "priority" if with_tier else None
        )
        started = time.monotonic()
        try:
            status, body, text = _probe_http_json(
                url, "POST", auth_headers(item, key) | extra, payload, timeout
            )
        except Exception as error:
            return None, _redact_text(str(error), [redaction_key]), time.monotonic() - started
        elapsed = time.monotonic() - started
        if 200 <= status < 300:
            problem = response_shape_problem(body, text, protocol)
            if problem:
                return status, _redact_text(problem, [redaction_key]), elapsed
            return status, "", elapsed
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if isinstance(body, dict) else text
        return status, _redact_text(raw[:1500], [redaction_key]), elapsed

    try:
        tier_status, tier_detail, tier_seconds = attempt(True)
        tier_ok = tier_status is not None and 200 <= tier_status < 300 and not tier_detail
        if tier_ok:
            return {
                "model": model,
                "verdict": "supported",
                "status": tier_status,
                "baseline_status": None,
                "tier_seconds": round(tier_seconds, 3),
                "baseline_seconds": None,
                "detail": "",
            }
        base_status, base_detail, base_seconds = attempt(False)
        accepted_without_tier = (
            base_status is not None and 200 <= base_status < 300 and not base_detail
        )
        return {
            "model": model,
            "verdict": "unsupported" if accepted_without_tier else "unknown",
            "status": tier_status,
            "baseline_status": base_status,
            "tier_seconds": round(tier_seconds, 3),
            "baseline_seconds": round(base_seconds, 3),
            "detail": tier_detail if accepted_without_tier else (tier_detail or base_detail),
        }
    finally:
        key = ""


def measure_fast_tier(
    provider: dict[str, Any],
    model_id: str,
    pairs: int = 8,
    temporary_key: str | None = None,
) -> dict[str, Any]:
    """Interleaved paired A/B on one model: is service_tier=priority measurably faster?

    Requests alternate priority-first and standard-first so upstream drift cancels out,
    and the verdict is taken from the paired differences rather than two separate pools —
    these gateways are noisy enough that unpaired medians invent speedups that do not exist.
    """
    item = validate_provider(deepcopy(provider), allow_missing_secret=True)
    model = str(model_id or "").strip()
    if not model:
        raise ValueError("Model ID is empty")
    key = provider_key(item, temporary_key)
    # Probes are tiny health checks, not generations: the provider's inference
    # timeout (300 s for long silent reasoning) must not turn a hung gateway into a
    # five-minute stall per probe attempt in the GUI's test and checkup flows.
    timeout = min(120, max(30, int(item.get("timeout_seconds") or 120)))

    def timed(with_tier: bool) -> float | None:
        url, payload, extra, _protocol = probe_request(
            item, model, "low", "priority" if with_tier else None,
            max_tokens=200, prompt="Count from 1 to 20, digits only, space separated.",
        )
        started = time.monotonic()
        try:
            status, _body, _text = _probe_http_json(
                url, "POST", auth_headers(item, key) | extra, payload, timeout
            )
        except Exception:
            return None
        return time.monotonic() - started if 200 <= status < 300 else None

    samples: list[tuple[float, float]] = []
    try:
        for index in range(max(4, pairs)):
            order = (False, True) if index % 2 == 0 else (True, False)
            taken: dict[bool, float] = {}
            for with_tier in order:
                value = timed(with_tier)
                if value is not None:
                    taken[with_tier] = value
            if len(taken) == 2:
                samples.append((taken[False], taken[True]))
    finally:
        key = ""

    if len(samples) < 4:
        return {"model": model, "verdict": "untested", "pairs": len(samples)}
    base = [pair[0] for pair in samples]
    diffs = [tier - standard for standard, tier in samples]
    faster = sum(1 for value in diffs if value < 0)
    median_diff = statistics.median(diffs)
    ratio = median_diff / statistics.median(base) * 100
    verdict = "faster" if faster >= len(samples) - 1 and ratio < -8 else "none"
    return {
        "model": model,
        "verdict": verdict,
        "pairs": len(samples),
        "faster_pairs": faster,
        "median_delta_seconds": round(median_diff, 3),
        "median_delta_percent": round(ratio, 1),
        "standard_median_seconds": round(statistics.median(base), 3),
    }


def failover_chain(
    registry: dict[str, Any],
    requested_slug: str,
    avoid: set[str] | None = None,
    protocol: str | None = None,
) -> list[tuple[str, str]]:
    """Ordered (provider id, upstream model id) attempts for one requested slug.

    The slug's own provider always comes first — the request named it, and demoting it
    would make routing unpredictable. Alternates are other enabled providers exposing the
    same bare model id, and only when the primary opted into failover; silently switching
    vendors is not something to guess at. Providers in `avoid` (recently failing every
    request) sink to the back rather than being dropped, so a stale health reading can
    never leave a model with nowhere to go. Shared with the router so the manager can show
    the exact chain the router will walk. When `protocol` is supplied, every attempt must
    explicitly support that wire shape; a Responses request must never fail over into an
    Anthropic-only Messages gateway, or vice versa.
    """
    avoid = avoid or set()
    primary: tuple[str, str] | None = None
    primary_provider: dict[str, Any] | None = None
    for provider in registry.get("providers") or []:
        if not provider.get("enabled"):
            continue
        if protocol and protocol not in provider.get("protocols", ["responses"]):
            continue
        prefix = str(provider.get("prefix") or "")
        for model in provider.get("models") or []:
            if model.get("enabled") and published_slug(provider, model) == requested_slug:
                primary = (provider["id"], str(model["id"]))
                primary_provider = provider
                break
        if primary is not None:
            break
    if primary is None or primary_provider is None:
        return []
    if not primary_provider.get("allow_failover"):
        return [primary]
    healthy: list[tuple[str, str]] = []
    degraded: list[tuple[str, str]] = []
    for provider in registry.get("providers") or []:
        if not provider.get("enabled") or provider["id"] == primary[0]:
            continue
        if protocol and protocol not in provider.get("protocols", ["responses"]):
            continue
        for model in provider.get("models") or []:
            if model.get("enabled") and str(model.get("id")) == primary[1]:
                target = degraded if provider["id"] in avoid else healthy
                target.append((provider["id"], primary[1]))
                break
    return ([primary] + healthy + degraded)[:FAILOVER_MAX_ATTEMPTS]


def validate_model(model: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(model, dict):
        raise ValueError("Provider model entries must be objects")
    model_id = str(model.get("id") or "").strip()
    if not model_id or len(model_id) > 160 or any(ch in model_id for ch in "\r\n\t"):
        raise ValueError(f"Invalid model ID: {model_id!r}")
    model["id"] = model_id
    publish_as = str(model.get("publish_as") or "").strip()
    if publish_as and not PUBLISH_AS_PATTERN.fullmatch(publish_as):
        raise ValueError(f"Model {model_id!r} has an invalid publish_as slug: {publish_as!r}")
    if len(publish_as) > 160:
        raise ValueError(f"Model {model_id!r} has an over-long publish_as slug")
    model["publish_as"] = publish_as
    enabled = model.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"Model {model_id!r} enabled must be a JSON boolean")
    model["enabled"] = enabled
    model["display_name"] = str(model.get("display_name") or "").strip()
    model["description"] = str(model.get("description") or "").strip()
    context_window = model.get("context_window_tokens")
    if type(context_window) is not int or not 0 < context_window <= 100_000_000:
        model.pop("context_window_tokens", None)
    max_output_tokens = model.get("max_output_tokens")
    if type(max_output_tokens) is not int or not 0 < max_output_tokens <= 100_000_000:
        model.pop("max_output_tokens", None)
    supports_1m = model.get("supports_1m_context")
    if type(supports_1m) is not bool:
        model.pop("supports_1m_context", None)
    if "reasoning_effort_ceiling" in model:
        ceiling = str(model["reasoning_effort_ceiling"]).strip().lower()
        if ceiling not in REASONING_EFFORT_ORDER:
            raise ValueError(f"Model {model_id!r} has an invalid reasoning_effort_ceiling")
        model["reasoning_effort_ceiling"] = ceiling
    if model.get("last_test_status") not in {None, "ready", "failed", "untested"}:
        model["last_test_status"] = "untested"
    if model.get("fast_tier_status") not in {None, "supported", "unsupported", "unknown"}:
        model["fast_tier_status"] = "unknown"
    if model.get("fast_tier_effect") not in {None, "faster", "none", "untested"}:
        model["fast_tier_effect"] = "untested"
    forced = model.get("fast_tier_forced", False)
    model["fast_tier_forced"] = forced if isinstance(forced, bool) else False
    return model


def validate_provider(
    provider: dict[str, Any], allow_missing_secret: bool = False
) -> dict[str, Any]:
    if not isinstance(provider, dict):
        raise ValueError("Provider entries must be objects")
    provider_id = str(provider.get("id") or "").strip().lower()
    if not ID_PATTERN.fullmatch(provider_id):
        raise ValueError("Provider ID must use 2-40 lowercase letters, numbers, or underscores")
    provider["id"] = provider_id
    name = str(provider.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError(f"Provider {provider_id!r} has an invalid name")
    provider["name"] = name
    provider["base_url"] = normalize_base_url(str(provider.get("base_url") or ""))
    provider["models_path"] = normalize_path(str(provider.get("models_path") or "/models"), "/models")
    provider["responses_path"] = normalize_path(
        str(provider.get("responses_path") or "/responses"), "/responses"
    )
    provider["messages_path"] = normalize_path(
        str(provider.get("messages_path") or "/v1/messages"), "/v1/messages"
    )
    provider["chat_path"] = normalize_path(
        str(provider.get("chat_path") or "/v1/chat/completions"), "/v1/chat/completions"
    )
    protocols = provider.get("protocols")
    if protocols is None:
        # Existing entries predate the Claude side and are all Responses gateways; assuming
        # that keeps every Codex provider working untouched.
        protocols = ["responses"]
    if isinstance(protocols, str):
        protocols = [protocols]
    if not isinstance(protocols, list) or not protocols:
        raise ValueError(f"Provider {provider_id!r} protocols must be a non-empty JSON array")
    unknown = [p for p in protocols if p not in PROTOCOLS]
    if unknown:
        raise ValueError(f"Provider {provider_id!r} has unknown protocols: {unknown}")
    provider["protocols"] = [p for p in PROTOCOLS if p in protocols]
    for field, default in (
        ("enabled", True),
        ("protected", False),
        ("is_default", False),
        ("allow_failover", False),
    ):
        value = provider.get(field, default)
        if not isinstance(value, bool):
            raise ValueError(f"Provider {provider_id!r} {field} must be a JSON boolean")
        provider[field] = value
    home = str(provider.get("workspace") or CODEX.name)
    if home not in WORKSPACES:
        raise ValueError(f"Provider {provider_id!r} has an unknown workspace: {home!r}")
    provider["workspace"] = home
    adapter = str(provider.get("request_adapter") or "").strip()
    if adapter not in ("",) + REQUEST_ADAPTERS:
        raise ValueError(
            f"Provider {provider_id!r} has an unknown request_adapter: {adapter!r}"
        )
    provider["request_adapter"] = adapter
    if (
        adapter == MESSAGES_TO_CHAT_COMPLETIONS_ADAPTER
        and "messages" not in provider["protocols"]
    ):
        # Mirror of the responses-bridge rule: without the messages protocol there is no
        # Messages client for this bridge to translate, and a Responses client would
        # receive a mangled body.
        raise ValueError(
            f"Provider {provider_id!r}: the Chat Completions bridge for Claude Desktop "
            "needs the 'messages' protocol"
        )
    if adapter == RESPONSES_TO_CHAT_COMPLETIONS_ADAPTER and "responses" not in provider["protocols"]:
        # The bridge translates Responses (the Codex App's wire protocol) into Chat
        # Completions; without the responses protocol on the client side there is nothing
        # for it to translate, and a Messages client would receive a mangled body.
        raise ValueError(
            f"Provider {provider_id!r}: the Chat Completions bridge needs the "
            "'responses' protocol (it serves the Codex App side)"
        )
    provider["auth_type"] = str(provider.get("auth_type") or "dpapi")
    raw_auth_header = str(provider.get("auth_header") or "Authorization")
    if _has_forbidden_header_control(raw_auth_header):
        raise ValueError(
            f"Provider {provider_id!r} authentication header name is invalid"
        )
    provider["auth_header"] = raw_auth_header.strip()
    if not HTTP_HEADER_NAME_PATTERN.fullmatch(provider["auth_header"]):
        raise ValueError(f"Provider {provider_id!r} authentication header name is invalid")
    provider["auth_prefix"] = str(
        provider.get("auth_prefix") if provider.get("auth_prefix") is not None else "Bearer "
    )
    if _has_forbidden_header_control(provider["auth_prefix"]):
        raise ValueError(f"Provider {provider_id!r} authentication prefix is invalid")
    try:
        timeout = int(provider.get("timeout_seconds") or 120)
    except (TypeError, ValueError):
        timeout = 120
    provider["timeout_seconds"] = min(900, max(5, timeout))
    if provider["auth_type"] in {"codex_auth", "dpapi"}:
        # A blank prefix is only a supported legacy shape for Claude's Messages-only default.
        # Normalize every other provider to a deterministic namespace, including an old Codex
        # registry loaded before this guard existed.  The router still rejects the old bare slug;
        # normalization makes the repaired catalog and all future writes converge on one name.
        prefix = str(provider.get("prefix") or "")
        if not prefix and not allows_legacy_bare_model(provider):
            prefix = derive_model_prefix(provider_id, provider["protocols"])
        if prefix and not MODEL_PREFIX_PATTERN.fullmatch(prefix):
            raise ValueError(f"Provider {provider_id!r} has an invalid model prefix")
        if provider["auth_type"] == "dpapi":
            provider["secret_file"] = str(
                provider.get("secret_file") or (provider_id + "-api-key.dpapi")
            )
            provider["entropy"] = str(
                provider.get("entropy") or ("CodexSota.Provider." + provider_id + ".v1")
            )
            provider_secret_path = secret_path(provider)
            # A disabled provider is still editable/deletable even if its old key file was
            # removed. Only enabled entries must have a credential for strict routing checks.
            if not allow_missing_secret and provider.get("enabled") and not provider_secret_path.exists():
                raise FileNotFoundError(f"Encrypted API key is missing for {name}")
    else:
        raise ValueError(f"Provider {provider_id!r} has an unsupported auth_type")
    provider["prefix"] = prefix
    extra = provider.get("extra_headers") or {}
    if not isinstance(extra, dict):
        raise ValueError(f"Provider {provider_id!r} extra_headers must be an object")
    clean_headers: dict[str, str] = {}
    for name, value in extra.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError(
                f"Provider {provider_id!r} extra_headers keys and values must be strings"
            )
        if not HTTP_HEADER_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Provider {provider_id!r} has an invalid HTTP header name")
        if _has_forbidden_header_control(value):
            raise ValueError(
                f"Provider {provider_id!r} HTTP header {name!r} has an invalid value"
            )
        clean_headers[name] = value
    provider["extra_headers"] = clean_headers
    models = provider.get("models") or []
    if not isinstance(models, list):
        raise ValueError(f"Provider {provider_id!r} models must be a list")
    seen_models: set[str] = set()
    clean_models: list[dict[str, Any]] = []
    for model in models:
        clean = validate_model(dict(model))
        # The alias must stay inside the provider's own namespace.  Refusing anything else
        # here is what keeps publish_as from ever becoming a second way to erase the
        # provider identity -- on either the Claude or the Codex side of the split.
        if clean["publish_as"] and not allows_provider_publish_as(
            provider, clean["publish_as"], prefix=prefix
        ):
            raise ValueError(
                f"Provider {provider_id!r} model {clean['id']!r} cannot use publish_as: "
                "the alias must keep this provider's model prefix"
            )
        if clean["publish_as"] == prefix + clean["id"]:
            # Same slug either way; dropping it keeps the digest stable and the file readable.
            clean["publish_as"] = ""
        if clean["id"] in seen_models:
            continue
        seen_models.add(clean["id"])
        clean_models.append(clean)
    provider["models"] = clean_models
    return provider


def validate_registry(
    registry: dict[str, Any], allow_missing_secrets: bool = False,
    *, allow_shared_secrets: bool = False,
) -> dict[str, Any]:
    if not isinstance(registry, dict):
        raise ValueError("Provider registry must be a JSON object")
    if int(registry.get("version") or 0) != REGISTRY_VERSION:
        raise ValueError(f"Unsupported provider registry version: {registry.get('version')}")
    providers = registry.get("providers")
    if not isinstance(providers, list):
        raise ValueError("Provider registry has no providers")
    ids: set[str] = set()
    prefixes: set[str] = set()
    slugs: dict[str, str] = {}
    default_count = 0
    clean_providers: list[dict[str, Any]] = []
    for provider in providers:
        clean = validate_provider(dict(provider), allow_missing_secret=allow_missing_secrets)
        provider_id = clean["id"]
        if provider_id in ids:
            raise ValueError(f"Duplicate provider ID: {provider_id}")
        ids.add(provider_id)
        prefix = clean["prefix"]
        if prefix:
            if prefix in prefixes:
                raise ValueError(f"Duplicate model prefix: {prefix}")
            prefixes.add(prefix)
        if clean.get("is_default"):
            default_count += 1
            if not clean["enabled"]:
                raise ValueError("Default provider must be enabled")
            # Claude's legacy Messages profile intentionally keeps its default model bare. Every
            # other workspace/protocol must use the namespace normalized above.
            if clean.get("workspace") == CLAUDE.name and allows_legacy_bare_model(clean):
                if prefix:
                    raise ValueError("Default Claude Messages provider must use an empty model prefix")
            elif not prefix:
                raise ValueError(
                    "Default provider must use a non-empty model prefix outside Claude Messages"
                )
        if clean["enabled"]:
            for model in clean["models"]:
                if not model["enabled"]:
                    continue
                slug = published_slug(clean, model)
                # Name both claimants. Once publish_as exists the usual collision is an
                # override against a sibling the user merely switched on, and which side to
                # change depends on which is which -- a bare "duplicate" sent them hunting.
                owner = f"{clean['id']}/{model['id']}" + (
                    " (publish_as)" if model["publish_as"] else ""
                )
                if slug in slugs:
                    raise ValueError(
                        f"Duplicate selectable model slug: {slug} "
                        f"(claimed by {slugs[slug]} and {owner})"
                    )
                slugs[slug] = owner
        clean_providers.append(clean)
    # An empty registry is the legitimate "freshly created workspace" state: there is
    # nothing to route yet, so the router refuses to start and the manager says so.
    # Demanding a default here would make a new workspace impossible to create at all.
    if providers and default_count != 1:
        raise ValueError("Provider registry must have exactly one default provider")
    if not allow_shared_secrets:
        _validate_secret_ownership(clean_providers)
    registry["providers"] = clean_providers
    registry["version"] = REGISTRY_VERSION
    return registry


def load_registry(
    path: Path = REGISTRY_PATH, allow_missing_secrets: bool = False,
    *, allow_shared_secrets: bool = True,
) -> dict[str, Any]:
    # Legacy shared credentials are not changed by reading/routing. Ownership changes are
    # checked against the current registry by write_registry and before any key overwrite.
    try:
        registry = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        # Reading a brand-new workspace is harmless. Creation belongs to the
        # first successful save, so opening or auditing the UI does not write it.
        registry = {"version": REGISTRY_VERSION, "providers": []}
    return validate_registry(
        registry, allow_missing_secrets=allow_missing_secrets,
        allow_shared_secrets=allow_shared_secrets,
    )


def write_registry(
    registry: dict[str, Any],
    path: Path = REGISTRY_PATH,
    allow_missing_secrets: bool = False,
) -> None:
    clean = validate_registry(
        deepcopy(registry), allow_missing_secrets=allow_missing_secrets,
        allow_shared_secrets=True,
    )
    previous = (
        load_registry(path, allow_missing_secrets=True)["providers"]
        if path.exists() else []
    )
    _validate_secret_ownership(clean["providers"], previous)
    clean["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(clean, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def find_provider(registry: dict[str, Any], provider_id: str) -> dict[str, Any]:
    target = str(provider_id or "").strip().lower()
    for provider in registry.get("providers", []):
        if provider.get("id") == target:
            return provider
    raise KeyError(f"Provider not found: {target}")


def selectable_slugs(registry: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for provider in registry["providers"]:
        if not provider["enabled"]:
            continue
        for model in provider["models"]:
            if model["enabled"]:
                result.append(published_slug(provider, model))
    return result


def upgrade_messages_prefixes(registry: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Move messages-only providers off the legacy `vendor--` prefix, in place.

    Returns one (provider id, old prefix, new prefix) row per rewrite and an empty list when
    there is nothing to do, so a caller can report what changed and re-run harmlessly. Left
    alone: providers already on the dotted form, providers that also speak responses (their
    slugs are pinned by the Codex catalog and config.toml), providers whose prefix was typed
    by hand rather than derived, and Claude's legacy default, which intentionally keeps an empty
    prefix for Messages compatibility.
    """
    changes: list[tuple[str, str, str]] = []
    for provider in registry.get("providers") or []:
        provider_id = str(provider.get("id") or "")
        protocols = provider.get("protocols") or []
        if "messages" not in protocols or "responses" in protocols:
            continue
        old = str(provider.get("prefix") or "")
        # Only the prefix this code would itself have derived is safe to rewrite. A prefix the
        # user typed is a deliberate choice, and renaming it would retarget a slug they picked.
        legacy = derive_model_prefix(provider_id, ["responses"])
        if not old or old != legacy:
            continue
        new = derive_model_prefix(provider_id, protocols)
        provider["prefix"] = new
        changes.append((provider_id, old, new))
    return changes


def registry_digest(registry: dict[str, Any]) -> str:
    stable = deepcopy(registry)
    stable.pop("updated_at", None)
    encoded = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _builtin_catalog_source() -> dict[str, Any]:
    """A small, neutral template lets a clean machine save its first provider.

    This contains no copied user instructions or credentials. Installations with
    an existing source catalog continue using their own capability metadata.
    Model-specific context limits and reasoning ceilings can narrow this template.
    """
    return {
        "models": [{
            "slug": "generic",
            "display_name": "Custom model",
            "description": "A model served through the local SOTA router.",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": level, "description": label}
                for level, label in (
                    ("low", "Light reasoning"),
                    ("medium", "Balanced reasoning"),
                    ("high", "Detailed reasoning"),
                )
            ],
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 1,
            "base_instructions": "You are a coding assistant. Help the user complete their task accurately.",
            "supports_reasoning_summaries": False,
            "default_reasoning_summary": "none",
            "support_verbosity": True,
            "default_verbosity": "medium",
            "apply_patch_tool_type": "freeform",
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "supports_parallel_tool_calls": True,
            "context_window": 128000,
            "max_context_window": 128000,
            "effective_context_window_percent": 95,
            "experimental_supported_tools": [],
            "input_modalities": ["text", "image"],
            "additional_speed_tiers": [],
            "service_tiers": [],
            "upgrade": None,
        }],
    }


def build_model_catalog(
    registry: dict[str, Any] | None = None,
    source_path: Path = SOURCE_CATALOG_PATH,
    destination_path: Path = CATALOG_PATH,
) -> dict[str, Any]:
    registry = registry or load_registry()
    # Catalog callers include recovery/import paths that may hand us a raw pre-guard registry.
    # `published_slug` still advertises a safe derived namespace for those entries; hash the same
    # canonical view or audit_registry will reject the freshly written catalog until the next
    # save. Keep this intentionally lightweight: full validation belongs to load/write paths and
    # would make a read-only catalog preview require credentials and every optional field.
    catalog_registry = deepcopy(registry)
    for provider in catalog_registry.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        if not str(provider.get("prefix") or ""):
            derived = effective_model_prefix(provider)
            if derived:
                provider["prefix"] = derived
        prefix = str(provider.get("prefix") or "")
        for model in provider.get("models") or []:
            if not isinstance(model, dict):
                continue
            override = str(model.get("publish_as") or "").strip()
            model["publish_as"] = override
            if override and not allows_provider_publish_as(provider, override, prefix=prefix):
                # A raw hand-edited alias that validation would reject must not influence either
                # the advertised slug or the catalog identity hash.
                model["publish_as"] = ""
            elif override and override == prefix + str(model.get("id") or ""):
                # Match validate_provider's canonical representation so aliases that add no
                # information do not change the registry digest.
                model["publish_as"] = ""
    registry = catalog_registry
    try:
        source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        source = _builtin_catalog_source()
    source_models = source.get("models")
    if not isinstance(source_models, list) or not source_models:
        raise RuntimeError("Source catalog has no model templates")
    by_slug = {
        model.get("slug"): model
        for model in source_models
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    default_template = by_slug.get("gpt-5.6-sol") or source_models[0]
    terra_template = by_slug.get("gpt-5.6-terra") or default_template
    output_models: list[dict[str, Any]] = []
    priority = 1
    for provider in registry["providers"]:
        if not provider["enabled"]:
            continue
        prefix = effective_model_prefix(provider)
        for model_config in provider["models"]:
            if not model_config["enabled"]:
                continue
            model_id = model_config["id"]
            # The alias decides which template the entry is built from.  Mapping a non-GPT
            # model onto a known slug (e.g. claude-opus-5 -> sierra--gpt-5.6-sol) is exactly
            # how it picks up correct capability metadata; an unknown id would otherwise
            # silently wear the default GPT template's reasoning levels.
            override = str(model_config.get("publish_as") or "").strip()
            template_key = model_id
            if override and prefix and override.startswith(prefix):
                template_key = override[len(prefix):]
            source_model = by_slug.get(template_key) or by_slug.get(model_id)
            if source_model is None:
                source_model = terra_template if "terra" in model_id.lower() else default_template
            model = deepcopy(source_model)
            if "luna" in model_id.lower():
                # Older direct-provider catalogs may not include Luna and therefore fall
                # back to the Sol template.  Luna *the model* supports reasoning through
                # `max`, not Sol's additional `ultra` level, so clamp the copied capability
                # metadata.  Keyed on the family rather than on one version string: matching
                # `gpt-5.6-luna` exactly left `gpt-6-luna` advertising an `ultra` level its
                # model cannot honour -- the same line of model with two different capability
                # sets, decided by nothing but a hardcoded suffix.
                levels = model.get("supported_reasoning_levels")
                if isinstance(levels, list):
                    model["supported_reasoning_levels"] = [
                        level
                        for level in levels
                        if not (
                            isinstance(level, dict)
                            and str(level.get("effort") or "").lower() == "ultra"
                        )
                        and not (
                            isinstance(level, str) and level.lower() == "ultra"
                        )
                    ]
            # Only explicit provider/model overrides narrow the copied template.  Do not
            # assume every Chat-compatible gateway has the same reasoning capabilities.
            ceiling = model_config.get("reasoning_effort_ceiling")
            if ceiling in REASONING_EFFORT_ORDER:
                allowed = set(REASONING_EFFORT_ORDER[:REASONING_EFFORT_ORDER.index(ceiling) + 1])
                model["supported_reasoning_levels"] = [
                    level for level in model.get("supported_reasoning_levels", [])
                    if (level.get("effort") if isinstance(level, dict) else level) in allowed
                ]
                if not model["supported_reasoning_levels"]:
                    raise ValueError(f"No reasoning level remains for {model_id!r} below {ceiling!r}")
            # The source templates are GPT-sized (272k).  A non-GPT model that keeps that
            # number makes the app believe the upstream accepts more context than it does,
            # so it never compacts in time.  The manager's per-model value overrides both
            # the usable window and the hard ceiling the app reads.
            configured_context = model_config.get("context_window_tokens")
            if type(configured_context) is int and 0 < configured_context <= 100_000_000:
                model["context_window"] = configured_context
                model["max_context_window"] = configured_context
            model["slug"] = published_slug(provider, model_config)
            model["display_name"] = model_config.get("display_name") or (
                model_id + " (" + provider["name"] + ")"
            )
            model["description"] = model_config.get("description") or (
                model_id + " through " + provider["name"] + "."
            )
            model["priority"] = priority
            model["default_reasoning_level"] = str(
                model_config.get("default_reasoning_level") or "medium"
            )
            if ceiling in REASONING_EFFORT_ORDER and model["default_reasoning_level"] not in allowed:
                last_level = model["supported_reasoning_levels"][-1]
                model["default_reasoning_level"] = last_level.get("effort") if isinstance(last_level, dict) else last_level
            if model_config.get("fast_tier_status") == "supported":
                model["additional_speed_tiers"] = ["fast"]
                model["service_tiers"] = [deepcopy(PRIORITY_SERVICE_TIER)]
            else:
                model["additional_speed_tiers"] = []
                model["service_tiers"] = []
            model["upgrade"] = None
            output_models.append(model)
            priority += 1
    if not output_models:
        raise RuntimeError("No enabled models are selected")
    digest = registry_digest(registry)
    source["models"] = output_models
    source["etag"] = "local-sota-" + digest[:16]
    source["registry_hash"] = digest
    source["fetched_at"] = utc_now()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.parent / f".{destination_path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(source, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination_path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return {
        "status": "ok",
        "destination": str(destination_path),
        "registry_hash": digest,
        "models": [model["slug"] for model in output_models],
    }


def ensure_codex_profile(
    registry: dict[str, Any], workspace: Workspace | None = None,
) -> dict[str, Any]:
    """Create only missing first-run client files, never an upstream credential.

    A random local token satisfies the Codex API-key login flow. It must not be
    created when a configured upstream borrows auth.json: that would silently
    replace a missing real login with a fake upstream key. Existing files are
    always preserved, including custom settings and the user's real login.
    """
    workspace = workspace or default_workspace()
    if workspace.name != CODEX.name or not workspace.needs_catalog:
        return {"status": "not-applicable", "created": []}
    slugs = selectable_slugs(registry)
    if not slugs:
        return {"status": "empty", "created": []}
    config_path = workspace.root / "config.toml"
    auth_path = workspace.auth_path
    uses_upstream_auth = any(
        provider.get("enabled") and provider.get("auth_type") == "codex_auth"
        for provider in registry.get("providers", [])
    )
    if not auth_path.exists() and uses_upstream_auth:
        return {"status": "authentication-required", "created": []}
    token = "sota-local-" + uuid4().hex + uuid4().hex
    config = (
        'model_provider = "codex_sota"\n'
        + "model = " + json.dumps(slugs[0], ensure_ascii=False) + "\n"
        + "model_catalog_json = " + json.dumps(workspace.catalog_path.as_posix(), ensure_ascii=False) + "\n"
        + 'cli_auth_credentials_store = "file"\n'
        + 'forced_login_method = "api"\n'
        + '\n[model_providers.codex_sota]\n'
        + 'name = "SOTA Local Router"\n'
        + f'base_url = "http://127.0.0.1:{workspace.router_port}"\n'
        + 'wire_api = "responses"\n'
        + 'requires_openai_auth = true\n'
    )
    auth = json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": token}, indent=2) + "\n"
    created: list[Path] = []
    workspace.root.mkdir(parents=True, exist_ok=True)
    try:
        for path, content in ((config_path, config), (auth_path, auth)):
            try:
                handle = path.open("x", encoding="utf-8", newline="\n")
            except FileExistsError:
                continue
            created.append(path)
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return {"status": "ready", "created": [str(path) for path in created]}


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _restore(snapshot: dict[Path, bytes | None]) -> None:
    for path, value in snapshot.items():
        if value is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.restore"
        try:
            with temporary.open("wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass


def _run_router_starter(command: list[str], timeout_seconds: float = 30.0) -> subprocess.CompletedProcess:
    """Bound the shell's wait even when a detached router inherits its output.

    Windows ``run(capture_output=True, timeout=...)`` can wait forever in the
    timeout cleanup's second ``communicate()`` if a long-lived child owns the
    pipe. A real file needs no EOF. Only this invocation's shell is stopped on
    timeout, with a bounded reap; its detached router is never killed here.
    """
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            raise RuntimeError(
                "Router starter did not finish within its timeout; its desktop "
                "and detached router were not stopped. Check the workspace router status."
            ) from None
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 131072))
        text = output.read(131072).decode("utf-8-sig", errors="replace")
    return subprocess.CompletedProcess(command, code, stdout=text, stderr="")


def restart_router(workspace: Workspace | None = None, *, force: bool = False) -> dict[str, Any]:
    """Make sure a healthy router for this workspace is up, and report what happened.

    By default this is an *ensure-running* path, which is what almost every caller wants: the
    starter polls /healthz and, when the running process already matches the expected router
    version, registry hash and provider list, it leaves that process alone and reports
    `started: false`.  Save-and-apply and the launch buttons rely on that -- restarting a router
    that is already correct would drop whatever requests happen to be in flight.

    `force` stops the workspace's router first, so a new process is guaranteed.  That is for the
    one case /healthz cannot detect: codex_sota_router.py itself changed on disk.  Its version and
    the registry hash are both unchanged by an edit to the routing code, so the default path would
    report success while the old code kept serving.
    """
    workspace = workspace or default_workspace()
    stopped: list[int] = []
    if force:
        stopped = [int(pid) for pid in (stop_router(workspace).get("stopped_process_ids") or [])]
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(workspace.router_starter),
    ]
    completed = _run_router_starter(command)
    lines = [line.strip() for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()]
    result = None
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("status"):
            result = candidate
            break
    if completed.returncode != 0 or not result or result.get("status") != "ready":
        raise RuntimeError("Router restart failed: " + "\n".join(lines[-12:]))
    if force:
        # The caller asked for a new process; say which old ones went away so a "restart" that
        # found nothing to stop is distinguishable from one that replaced a live router.
        result = dict(result)
        result["stopped_process_ids"] = stopped
    return result


def stop_router(workspace: Workspace | None = None) -> dict[str, Any]:
    """Stop the recorded router for a workspace, if one is running.

    An empty registry is a valid editing state, but the normal starter intentionally refuses
    to launch one with no enabled providers.  Deleting the last provider therefore uses this
    explicit stop path so the old in-memory routing table cannot keep serving a removed key.
    """
    workspace = workspace or default_workspace()
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(workspace.router_starter),
        "-Stop",
    ]
    completed = _run_router_starter(command)
    lines = [line.strip() for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()]
    result = None
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("status"):
            result = candidate
            break
    if completed.returncode != 0 or not result or result.get("status") != "stopped":
        raise RuntimeError("Router stop failed: " + "\n".join(lines[-12:]))
    return result


BOOKKEEPING_FIELDS = (
    "last_test_status",
    "last_test_at",
    "last_test_message",
    "fast_tier_status",
    "fast_tier_effect",
)
ENDPOINT_REPAIR_FIELDS = ("responses_path", "messages_path", "chat_path")
ENDPOINT_REPAIR_IDENTITY_FIELDS = PROTECTED_PINNED_PROVIDER_KEYS + (
    "id", "enabled", "extra_headers",
)


def save_provider_bookkeeping(
    providers: list[dict[str, Any]], workspace: Workspace | None = None,
    *, endpoint_baselines: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist probe results for many providers in one transaction, without restarting.

    Test verdicts and tier findings do not change routing, so the router never has to be
    told about them — and one write for the whole batch rebuilds the catalog once instead
    of once per provider, which is what turned a checkup into a burst of dropped
    connections. Protected providers are included: their results are only bookkeeping.

    Optional endpoint repairs are compare-and-swap updates against the full provider copy
    captured before probing. Only the three inference paths may change; concurrent routing
    or credential edits and protected identities are never overwritten by a stale probe.
    """
    workspace = workspace or default_workspace()
    sources = {provider["id"]: provider for provider in providers}
    wanted = {
        provider["id"]: {model["id"]: model for model in provider.get("models") or []}
        for provider in providers
    }
    endpoint_plans: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, str]]] = {}
    for provider_id, baseline in (endpoint_baselines or {}).items():
        source = sources.get(provider_id)
        if source is None:
            continue
        before = validate_provider(deepcopy(baseline), allow_missing_secret=True)
        after = validate_provider(deepcopy(source), allow_missing_secret=True)
        changes = {
            field: after[field] for field in ENDPOINT_REPAIR_FIELDS
            if after[field] != before[field]
        }
        if changes:
            endpoint_plans[provider_id] = (before, after, changes)
    with registry_write_lock(workspace=workspace):
        registry = load_registry(workspace.registry_path, allow_missing_secrets=True)
        touched: list[str] = []
        endpoint_updates: list[str] = []
        endpoint_conflicts: list[str] = []
        seen: set[str] = set()
        for provider in registry["providers"]:
            provider_id = provider["id"]
            seen.add(provider_id)
            plan = endpoint_plans.get(provider_id)
            if plan is not None:
                before, after, changes = plan
                stale = (
                    provider.get("protected")
                    or before["id"] != provider_id
                    or after["id"] != provider_id
                    or before["workspace"] != workspace.name
                    or any(
                        provider.get(field) != before.get(field)
                        for field in ENDPOINT_REPAIR_IDENTITY_FIELDS
                    )
                    or any(
                        after.get(field) != before.get(field)
                        for field in ENDPOINT_REPAIR_IDENTITY_FIELDS
                        if field not in ENDPOINT_REPAIR_FIELDS
                    )
                )
                if stale:
                    endpoint_conflicts.append(provider_id)
                else:
                    provider.update(changes)
                    endpoint_updates.append(provider_id)
            updates = wanted.get(provider["id"])
            if not updates:
                continue
            for model in provider["models"]:
                source = updates.get(model["id"])
                if not source:
                    continue
                for field in BOOKKEEPING_FIELDS:
                    if field in source:
                        model[field] = source[field]
            touched.append(provider["id"])
        endpoint_conflicts.extend(provider_id for provider_id in endpoint_plans if provider_id not in seen)
        snapshot = _snapshot([workspace.registry_path, workspace.catalog_path])
        try:
            write_registry(registry, workspace.registry_path, allow_missing_secrets=True)
            catalog = rebuild_catalog(workspace)
        except Exception:
            _restore(snapshot)
            raise
    return {
        "status": "ready", "providers": touched, "catalog": catalog,
        "endpoint_updates": endpoint_updates, "endpoint_conflicts": endpoint_conflicts,
    }


def apply_provider(
    provider: dict[str, Any],
    api_key: str | None = None,
    restart: bool = True,
    workspace: Workspace | None = None,
) -> dict[str, Any]:
    """Write one provider transactionally, rebuild the catalog, optionally bounce the router.

    Pass restart=False for writes the running router does not need told about: it hot-reloads
    providers.json by itself, and killing it aborts whatever requests are in flight — which
    surfaces to the client as a connection reset or a write timeout mid-stream.
    """
    workspace = workspace or default_workspace()
    with registry_write_lock(workspace=workspace):
        # Let the manager load an incomplete existing entry so a user can replace its missing
        # key. The final write remains strict for enabled providers.
        registry = load_registry(workspace.registry_path, allow_missing_secrets=True)
        submitted = deepcopy(provider)
        if not isinstance(submitted, dict):
            raise ValueError("Provider entries must be objects")
        submitted["workspace"] = workspace.name
        submitted_id = str(submitted.get("id") or "").strip().lower()
        existing_index = None
        for index, current in enumerate(registry["providers"]):
            if current["id"] == submitted_id:
                existing_index = index
                if current.get("protected"):
                    # Refusing the whole write here was too blunt.  It also made the model list of
                    # a protected provider permanently uncurateable -- and on the Codex side the
                    # protected entry is the default one, so its models are exactly the ones a user
                    # most needs to switch on.  Pin *all* routing/credential identity fields back
                    # to disk and let ordinary model/settings edits through.  The second validation
                    # is important when pinning changes auth_type (for example a stale dpapi form
                    # submitted for the codex_auth entry): it re-derives only the fields belonging
                    # to the identity that actually survived the pin.
                    submitted, _discarded_identity = pin_protected_provider_identity(
                        submitted, current
                    )
                break
        # Validate after protected identity pinning.  Besides blocking writes, this means stale
        # or hostile values in fields the caller is not allowed to own (an invalid secret path,
        # adapter, endpoint or prefix) are discarded before their validators can interfere with
        # an otherwise legitimate model/settings edit.
        if not registry["providers"]:
            submitted["is_default"] = True
        candidate = validate_provider(submitted, allow_missing_secret=True)
        # Reject a colliding credential identity BEFORE dpapi_protect can overwrite it. A
        # later write_registry failure and rollback must not be the first ownership check.
        proposed = [
            item for item in registry["providers"] if item["id"] != candidate["id"]
        ] + [candidate]
        _validate_secret_ownership(proposed, registry["providers"])
        if api_key and candidate.get("auth_type") == "dpapi":
            shared_with = _secret_owners(proposed).get(_secret_identity(candidate), set())
            if len(shared_with) > 1:
                raise CredentialOwnershipError(
                    f"Cannot replace encrypted credential {candidate['secret_file']!r}: "
                    f"it is still used by providers {', '.join(sorted(shared_with))}; "
                    "choose a separate secret_file before replacing this key"
                )
        # Check credential availability only after a protected entry has restored its on-disk
        # auth_type and secret identity.  Doing this against the caller's stale candidate first
        # lets a forged dpapi shape fail before the codex_auth pin can neutralize it.
        if candidate.get("auth_type") == "dpapi":
            existing_secret = secret_path(candidate).exists()
            if not api_key and not existing_secret:
                raise ValueError("API key is required for a new provider")
        paths = [workspace.registry_path, workspace.catalog_path]
        if workspace.name == CODEX.name:
            paths += [workspace.root / "config.toml", workspace.auth_path]
        if candidate.get("auth_type") == "dpapi":
            paths.append(secret_path(candidate))
        snapshot = _snapshot(paths)
        try:
            # A codex_auth provider borrows the Codex App credential and has no DPAPI file to
            # update.  The key field is disabled for protected entries, but callers may still
            # pass a temporary probe key; never turn that into a secret-file write after identity
            # pinning has restored auth_type=codex_auth.
            if api_key and candidate.get("auth_type") == "dpapi":
                dpapi_protect(api_key, secret_path(candidate), candidate["entropy"])
            if existing_index is None:
                registry["providers"].append(candidate)
            else:
                registry["providers"][existing_index] = candidate
            write_registry(registry, workspace.registry_path)
            catalog = rebuild_catalog(workspace)
            profile = ensure_codex_profile(registry, workspace)
            router = restart_router(workspace) if restart else {"status": "hot-reload"}
            return {"status": "ready", "catalog": catalog, "router": router, "profile": profile}
        except Exception:
            _restore(snapshot)
            if restart:
                try:
                    restart_router(workspace)
                except Exception:
                    pass
            raise


def reorder_providers(
    provider_ids: list[str],
    workspace: Workspace | None = None,
    restart: bool = True,
) -> dict[str, Any]:
    """Rewrite providers.json in the given order, transactionally.

    Order is not cosmetic: failover walks alternates in registry order, and the catalog's
    per-model `priority` follows it too, so this needs the same snapshot-and-rollback
    treatment as apply_provider rather than a bare write.
    """
    workspace = workspace or default_workspace()
    with registry_write_lock(workspace=workspace):
        registry = load_registry(workspace.registry_path, allow_missing_secrets=True)
        current = {provider["id"]: provider for provider in registry["providers"]}
        wanted = list(provider_ids)
        if sorted(wanted) != sorted(current):
            raise ValueError("Reordering must list every existing provider exactly once")
        snapshot = _snapshot([workspace.registry_path, workspace.catalog_path])
        try:
            registry["providers"] = [current[provider_id] for provider_id in wanted]
            write_registry(registry, workspace.registry_path, allow_missing_secrets=True)
            catalog = rebuild_catalog(workspace)
            router = restart_router(workspace) if restart else {"status": "hot-reload"}
            return {"status": "ready", "catalog": catalog, "router": router}
        except Exception:
            _restore(snapshot)
            if restart:
                try:
                    restart_router(workspace)
                except Exception:
                    pass
            raise


def delete_provider(
    provider_id: str, workspace: Workspace | None = None, restart: bool = True
) -> dict[str, Any]:
    workspace = workspace or default_workspace()
    with registry_write_lock(workspace=workspace):
        registry = load_registry(workspace.registry_path, allow_missing_secrets=True)
        provider = find_provider(registry, provider_id)
        if provider.get("protected"):
            raise ValueError("Protected provider cannot be deleted")
        remaining = [item for item in registry["providers"] if item["id"] != provider["id"]]
        shared_with = []
        if provider.get("auth_type") == "dpapi":
            identity = _secret_identity(provider)
            shared_with = [
                item["id"] for item in remaining
                if item.get("auth_type") == "dpapi" and _secret_identity(item) == identity
            ]
        promoted: str | None = None
        if provider.get("is_default"):
            # The registry needs exactly one enabled default, so deleting the current one is
            # only allowed if nothing is left, or if another enabled provider can take over.
            # Refusing outright would make the sole provider of a workspace undeletable.
            candidate = next((item for item in remaining if item.get("enabled")), None)
            if remaining and candidate is None:
                raise ValueError(
                    "删不了：它是默认供应商，而剩下的供应商都是停用状态，"
                    "注册表会没有可用的默认家。先启用另一家再删。"
                )
            if candidate is not None:
                candidate["is_default"] = True
                # A Codex default must keep a provider namespace after promotion. Otherwise a
                # later model-prefix loss would recreate the exact silent-default billing bug
                # this registry guard is meant to prevent. Keep Claude's legacy bare default,
                # whose Messages route remains compatible with the existing profile.
                if candidate.get("workspace") == CODEX.name:
                    candidate["prefix"] = str(
                        candidate.get("prefix")
                        or derive_model_prefix(candidate["id"], candidate.get("protocols"))
                    )
                else:
                    candidate["prefix"] = ""
                promoted = candidate["id"]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_root = workspace.deleted_root / (stamp + "-" + provider["id"])
        suffix = 1
        while archive_root.exists():
            archive_root = workspace.deleted_root / (
                stamp + "-" + provider["id"] + "-" + str(suffix)
            )
            suffix += 1
        archived_secret = archive_root / str(provider.get("secret_file") or "api-key.dpapi")
        metadata_path = archive_root / "provider.json"
        paths = [workspace.registry_path, workspace.catalog_path, metadata_path]
        if provider.get("auth_type") == "dpapi" and not shared_with:
            paths.extend([secret_path(provider), archived_secret])
        snapshot = _snapshot(paths)
        try:
            registry["providers"] = [
                item for item in registry["providers"] if item["id"] != provider["id"]
            ]
            write_registry(registry, workspace.registry_path, allow_missing_secrets=True)
            catalog = rebuild_catalog(workspace)
            if remaining:
                router = restart_router(workspace) if restart else {"status": "hot-reload"}
            else:
                # The running process cannot hot-reload to an empty routing table; stop it so
                # the deleted provider and its key are no longer reachable on the local port.
                router = stop_router(workspace)
            archive_root.mkdir(parents=True, exist_ok=False)
            metadata_path.write_text(
                json.dumps(provider, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if provider.get("auth_type") == "dpapi" and not shared_with and secret_path(provider).exists():
                os.replace(secret_path(provider), archived_secret)
            return {
                "status": "ready",
                "promoted_default": promoted,
                "catalog": catalog,
                "router": router,
                "archive": str(archive_root),
                "secret_retained_for": shared_with,
            }
        except Exception:
            _restore(snapshot)
            try:
                if archive_root.exists() and not any(archive_root.iterdir()):
                    archive_root.rmdir()
            except OSError:
                pass
            if restart:
                try:
                    restart_router(workspace)
                except Exception:
                    pass
            raise


def _endpoint_full_path(provider: dict[str, Any], kind: str) -> str:
    """The path part the upstream will actually receive, base_url's own path included."""
    key, fallback = ENDPOINT_KEYS.get(kind, ENDPOINT_KEYS["responses"])
    path = str(provider.get(key) or fallback)
    if path.startswith(("http://", "https://")):
        return urllib.parse.urlsplit(path).path or "/"
    base_path = urllib.parse.urlsplit(str(provider.get("base_url") or "")).path.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base_path + path


def lint_registry(
    registry: dict[str, Any] | None = None, workspace: Workspace | None = None
) -> list[dict[str, Any]]:
    """Configuration problems findable without touching the network.

    Exists because a wrong Responses path is invisible to a status-only probe: gateways
    whose front end answers every path with 200 and an HTML page look healthy right up to
    the moment a real streaming request finds no events in it.
    """
    workspace = workspace or default_workspace()
    registry = registry or load_registry(workspace.registry_path, allow_missing_secrets=True)
    findings: list[dict[str, Any]] = []

    def add(level: str, provider_id: str, message: str, model: str | None = None) -> None:
        findings.append(
            {"level": level, "provider": provider_id, "model": model, "message": message}
        )

    providers = registry.get("providers") or []
    for provider in providers:
        pid = provider.get("id", "?")
        protocols = set(provider.get("protocols") or ["responses"])
        models_full = _endpoint_full_path(provider, "models")
        endpoint_checks: list[tuple[str, str]] = [("模型列表", models_full)]
        if "responses" in protocols:
            endpoint_checks.append(("Responses", _endpoint_full_path(provider, "responses")))
        if "messages" in protocols:
            endpoint_checks.append(("Messages", _endpoint_full_path(provider, "messages")))
        for label, full in endpoint_checks:
            if "/v1/v1/" in full:
                add("error", pid, f"{label}路径拼出了重复的 /v1：{full}")
        models_v1 = models_full.startswith("/v1/")
        for label, full in endpoint_checks[1:]:
            endpoint_v1 = full.startswith("/v1/")
            if models_v1 != endpoint_v1:
                # Mixed-prefix gateways genuinely exist: on this install /models + /v1/messages
                # serves 160 models fine. The prefix heuristic cannot tell a working mixed
                # layout from a broken one -- only an actual probe can -- so surface it as a
                # hint, not an error, and say what to click.
                add(
                    "info",
                    pid,
                    f"模型列表和 {label} 的版本前缀不一致：模型列表是 {models_full}，"
                    f"{label} 是 {full}。这不一定是错的（有些网关就是混用前缀）；"
                    "如果「拉取模型」拿不到列表，再用「测试」自动寻找正确路径",
                )
        if provider.get("enabled"):
            enabled_models = [m for m in provider.get("models") or [] if m.get("enabled")]
            if not enabled_models:
                add("error", pid, "供应商已启用，但一个模型都没勾选，Codex 里看不到它")
            timeout = int(provider.get("timeout_seconds") or 0)
            if 0 < timeout < 60:
                add(
                    "warning",
                    pid,
                    f"超时只有 {timeout} 秒；流式推理中间的静默间隔常常超过它，会把流掐断",
                )
            if provider.get("auth_type") == "dpapi" and not secret_path(provider).exists():
                add("error", pid, "启用了但找不到已加密的 API Key")
            for model in enabled_models:
                if model.get("fast_tier_forced") and model.get("fast_tier_status") == "unsupported":
                    add(
                        "warning",
                        pid,
                        "开了「快速」，但探测显示这个上游拒绝 service_tier，可能直接报错",
                        model.get("id"),
                    )
            if provider.get("allow_failover"):
                bare = {m["id"] for m in enabled_models}
                alternates = {
                    other["id"]
                    for other in providers
                    if other.get("enabled")
                    and other.get("id") != pid
                    and bare & {m["id"] for m in other.get("models") or [] if m.get("enabled")}
                }
                if not alternates:
                    add(
                        "info",
                        pid,
                        "开了「失败时自动换别家」，但没有别的供应商提供同名模型，开关不会生效",
                    )

    # Which enabled models have no capability template to inherit from.
    #
    # `build_model_catalog` silently falls back to the default GPT template for a name the
    # source catalog does not carry, so a model the vendor just released wears another model's
    # reasoning levels and context metadata with nothing anywhere saying so.  Reported per
    # distinct name (not per provider-model) so a registry where six vendors all serve the same
    # new model produces one line, not six.
    if workspace.needs_catalog:
        try:
            source = json.loads(workspace.source_catalog_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            source = None
        template_slugs = {
            model["slug"]
            for model in ((source or {}).get("models") or [])
            if isinstance(model, dict) and isinstance(model.get("slug"), str)
        }
        if template_slugs:
            uncovered: dict[str, list[str]] = {}
            for provider in providers:
                if not provider.get("enabled"):
                    continue
                pid = str(provider.get("id") or "?")
                prefix = str(provider.get("prefix") or "")
                for model in provider.get("models") or []:
                    if not model.get("enabled"):
                        continue
                    model_id = str(model.get("id") or "")
                    override = str(model.get("publish_as") or "").strip()
                    key = (
                        override[len(prefix):]
                        if override and prefix and override.startswith(prefix)
                        else model_id
                    )
                    if key in template_slugs or model_id in template_slugs:
                        continue
                    uncovered.setdefault(key, []).append(pid)
            for key, owners in uncovered.items():
                add(
                    "info",
                    "、".join(sorted(set(owners))[:4]) + ("…" if len(set(owners)) > 4 else ""),
                    f"能力模板里没有「{key}」：Codex 会套用默认 GPT 模板，"
                    "推理档位/上下文可能和官方不一致。把它映射到模板里已有的名字，"
                    "或在 tango-relay-model-catalog.json 里补一份该名字的模板",
                    key,
                )
    return findings


TLS_ROOT = SOTA_ROOT / "tls"
TLS_CERT_PATH = TLS_ROOT / "router-cert.pem"
TLS_KEY_PATH = TLS_ROOT / "router-key.pem"
TLS_COMMON_NAME = "codex-sota local router"


def tls_certificate_status() -> dict[str, Any]:
    """Whether the local HTTPS certificate exists and how long it is still valid."""
    if not (TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()):
        return {"exists": False, "valid": False, "reason": "还没生成证书"}
    try:
        from cryptography import x509
    except ImportError:
        return {"exists": True, "valid": True, "reason": "无法校验（缺 cryptography），假定可用"}
    try:
        cert = x509.load_pem_x509_certificate(TLS_CERT_PATH.read_bytes())
    except Exception as error:  # noqa: BLE001 - a corrupt file just means regenerate
        return {"exists": True, "valid": False, "reason": f"证书无法解析：{error}"}
    now = datetime.now(timezone.utc)
    not_after = cert.not_valid_after_utc
    return {
        "exists": True,
        "valid": now < not_after,
        "not_after": not_after.isoformat(),
        "days_left": (not_after - now).days,
        "reason": "" if now < not_after else "证书已过期",
    }


def ensure_router_tls(days: int = 3650, force: bool = False) -> dict[str, Any]:
    """Create, once, a self-signed loopback certificate for the router's HTTPS listener.

    Claude Desktop rejects an http:// gateway URL outright, so the local router has to speak
    TLS. The certificate covers 127.0.0.1 and localhost only, never leaves this machine, and
    is marked as a CA so it can be handed to a client as a trust anchor without touching the
    Windows certificate store.
    """
    status = tls_certificate_status()
    if status["valid"] and not force:
        return {"created": False, **status, "cert": str(TLS_CERT_PATH), "key": str(TLS_KEY_PATH)}

    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TLS_COMMON_NAME)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    TLS_ROOT.mkdir(parents=True, exist_ok=True)
    TLS_KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    TLS_CERT_PATH.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return {"created": True, **tls_certificate_status(), "cert": str(TLS_CERT_PATH), "key": str(TLS_KEY_PATH)}


def redacted_registry(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    if registry is None:
        registry = load_registry(allow_missing_secrets=True)
    registry = deepcopy(registry)
    for provider in registry["providers"]:
        provider["key_present"] = (
            True
            if provider.get("auth_type") == "codex_auth"
            else secret_path(provider).exists()
        )
        provider.pop("entropy", None)
        # Extra headers are user-controlled and may contain a second API key, a cookie, or
        # a gateway signature.  Removing only the dedicated DPAPI secret is not enough for a
        # file explicitly labelled "without secrets".
        extra = provider.get("extra_headers")
        if isinstance(extra, dict):
            provider["extra_headers"] = {str(name): "<redacted>" for name in extra}
    return registry


def rebuild_catalog(workspace: Workspace) -> dict[str, Any]:
    """Regenerate the workspace's catalog, or report that it does not use one."""
    if not workspace.needs_catalog:
        return {"status": "not-applicable", "models": [], "destination": None}
    registry = load_registry(workspace.registry_path, allow_missing_secrets=True)
    if not registry["providers"]:
        # No router can run without an upstream. Remove the previous catalog so a deleted
        # provider cannot remain selectable in a later Codex launch.
        workspace.catalog_path.unlink(missing_ok=True)
        return {
            "status": "empty",
            "models": [],
            "destination": str(workspace.catalog_path),
        }
    return build_model_catalog(
        registry,
        workspace.source_catalog_path,
        workspace.catalog_path,
    )


def audit_registry(workspace: Workspace | None = None) -> dict[str, Any]:
    """Report a workspace's health. Never raises for a workspace that is merely empty.

    A brand new workspace has no providers and therefore no generated catalog, which is a
    normal state to be shown — not a failure. Reading the catalog unguarded is what turned
    "switch to Claude" into a FileNotFoundError dialog.
    """
    workspace = workspace or default_workspace()
    # Auditing must remain possible when a key file is missing; the result should expose that
    # problem instead of locking the user out of the editor/delete controls.
    registry = load_registry(workspace.registry_path, allow_missing_secrets=True)
    expected = selectable_slugs(registry)
    expected_hash = registry_digest(registry)
    enabled = [provider for provider in registry["providers"] if provider["enabled"]]
    actual: list[str] = []
    actual_hash = ""
    catalog_state = "ok"
    try:
        catalog = json.loads(workspace.catalog_path.read_text(encoding="utf-8-sig"))
        actual = [str(model.get("slug")) for model in catalog.get("models", [])]
        actual_hash = str(catalog.get("registry_hash") or "")
    except FileNotFoundError:
        catalog_state = "missing"
    except (OSError, ValueError):
        catalog_state = "invalid"
    catalog_matches = (
        catalog_state == "ok" and expected == actual and expected_hash == actual_hash
    )
    if not registry["providers"]:
        status = "empty"
    elif not workspace.needs_catalog:
        # This workspace publishes its model list some other way; the generated catalog
        # file is not part of its readiness.
        status = "ready"
    elif catalog_matches:
        status = "ready"
    elif catalog_state == "missing":
        status = "catalog_missing"
    elif catalog_state == "invalid":
        status = "catalog_invalid"
    else:
        status = "catalog_mismatch"
    return {
        "status": status,
        "workspace": workspace.name,
        "registry_version": registry["version"],
        "provider_count": len(enabled),
        "model_count": len(expected),
        "providers": [provider["id"] for provider in enabled],
        "models": expected,
        "catalog_state": catalog_state,
        "catalog_matches": catalog_matches,
        "registry_hash": expected_hash,
        "catalog_registry_hash": actual_hash,
        "secrets_ready": all(
            provider.get("auth_type") == "codex_auth" or secret_path(provider).exists()
            for provider in enabled
        ),
    }
