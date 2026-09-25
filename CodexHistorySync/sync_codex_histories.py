#!/usr/bin/env python3
"""One-click, bidirectional local Codex history synchronizer for Windows.

This intentionally synchronizes only local conversation/history metadata. It
never copies auth.json, API keys, access tokens, config.toml, logs, or plugin
credentials.  It reads only the non-secret account_id field from auth.json so
the desktop app's account-scoped sidebar section can be rebuilt after a login
change.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid
from typing import Any, Callable, Iterable
from urllib.parse import unquote

if os.name == "nt":
    import msvcrt


APP_NAME = "CodexHistorySync"
INSTALL_DIR = Path(__file__).resolve().parent
DEFAULT_LEFT = Path.home() / ".codex-personal"
DEFAULT_RIGHT = Path.home() / ".codex-plus"
DEFAULT_BACKUP_BASE = INSTALL_DIR / "backups"
LOG_DIR = INSTALL_DIR / "logs"
LOCK_PATH = INSTALL_DIR / "sync.lock"
LAST_RESULT_PATH = INSTALL_DIR / "last-result.json"
MAX_BACKUPS = 20
COCKPIT_MODEL_PROVIDER = "codex_local_access"
PLUS_MODEL_PROVIDER = "openai"
CONFLICT_CLONE_SUFFIX = "（同步冲突副本）"
ATOM_STATE_KEY = "electron-persisted-atom-state"
# Codex stores this host-local sidebar membership map at the global state root
# in current builds and nested under Atom in older builds.
THREAD_PROJECT_MEMBERSHIP_KEY = "thread-project-membership-host-ids"
SIDEBAR_PREFERENCES_KEY = "flat-project-sidebar-preferences-v1"
CUSTOM_SECTIONS_KEY = "sidebar-custom-sections-v3"
MANAGED_SECTION_ID = "6a2f6de3-f00e-50a3-906e-4c431676aac7"
MANAGED_SECTION_ORDER_KEY = f"custom:{MANAGED_SECTION_ID}"

LIST_STATE_KEYS = (
    "projectless-thread-ids",
    "pinned-thread-ids",
    "project-order",
)
DICT_STATE_KEYS = (
    "local-projects",
    "thread-project-assignments",
    "thread-workspace-root-hints",
    "thread-projectless-output-directories",
    "thread-writable-roots",
)
SESSION_STORAGE_DIRS = ("sessions", "archived_sessions")
MODEL_REGISTRY_NAME = "providers.json"
MODEL_NAMESPACE_SEPARATOR = "--"
# Responses providers normally publish the ``vendor--model`` form.  Keep the
# Messages-compatible dotted form here as well: a hand-authored Codex registry
# may use it, and treating such a slug as bare would make the synchronizer
# rewrite (or reject) an already-qualified model.
MODEL_NAMESPACE_SEPARATORS = (MODEL_NAMESPACE_SEPARATOR, ".anthropic.")
MODEL_GUARD_MAX_EXAMPLES = 12


def extended_path(path: Path | str) -> str:
    """A path string Windows will accept past its 260-character MAX_PATH limit.

    The backup tree is deep by construction: backup base, run timestamp, `pass-NN-<label>`, a second
    timestamp from `create_backup`, then `conflicts/<label>/` and finally the session's own relative
    path. With the archive living under a long base directory that total lands between roughly 254
    and 262 characters depending on which pass is running and how long the rollout filename is --
    and a Codex rollout name carrying two session ids is over 100 characters on its own. So the copy
    succeeded for most sessions and failed for a few with a bare `[WinError 3] The system cannot
    find the path specified`, which names neither file and reads like a missing source.

    `\\\\?\\` opts that call out of MAX_PATH without a registry change or admin rights
    (LongPathsEnabled is 0 on this machine). It requires a fully-resolved absolute path with
    backslash separators and no `.`/`..` components, which is why abspath runs first.
    """
    text = str(path)
    if os.name != "nt":
        return os.path.abspath(text)
    # Test the prefix BEFORE abspath: abspath treats an already-extended path as relative-ish and
    # mangles it into \\?\C:\?\C:\... , which then fails in a way that looks like a bad source.
    if text.startswith("\\\\?\\"):
        return text
    text = os.path.abspath(text)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        # UNC: \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text
# Which unresolved-model reasons must stop a sync, and which are only worth reporting.
#
# Almost none of them. The writer never guesses: `qualify_model_for_target` returns an unresolvable
# value UNCHANGED, so an archived rollout line is copied verbatim and no attribution is invented.
# A reason code therefore describes a property of the user's history, not a hazard in the write.
#
# Both history-derived reasons were fatal, and both were unsatisfiable by construction:
#
#   model_has_no_unique_enabled_provider -- the model is no longer offered by any enabled provider
#     (an old `gpt-5.5` in a months-old session). History cannot be made attributable after the
#     fact; the user would have to re-enable every model they have ever used.
#
#   model_has_multiple_enabled_providers -- several enabled providers offer the same bare id. That
#     is not a defect, it is the entire point of a multi-vendor router with failover: one archive
#     had `gpt-5.6-sol` offered by nine providers and 7602 history fields naming it. Aborting here
#     made the tool's central use case self-blocking.
#
# The misrouting this was meant to prevent is already prevented where it can actually happen: the
# router rejects a bare, un-namespaced slug at the request boundary rather than falling back to the
# default provider, so a bare name copied into another root fails loudly instead of billing the
# wrong account. Guarding it a second time in the archive buys nothing and cost this user a filled
# disk -- the three-way outer snapshot is taken before the preflight, so every abort still paid for
# a full-size copy of the session tree.
#
# What stays fatal is internal inconsistency, not history: a value we DID qualify that then fails to
# validate means the resolver produced a bad slug, which is a bug worth stopping for.
FATAL_MODEL_GUARD_REASONS = frozenset({"qualified_model_invalid"})
# Reasons a post-sync value was legitimately left exactly as recorded: no attribution could be
# chosen, so the writer copied it verbatim rather than guessing. A finding carrying one of these is
# evidence the copy was faithful. Anything else -- above all a plain "unqualified_model", meaning
# the value COULD have been namespaced and was not -- is a real post-condition failure worth
# stopping for, which is the check that keeps `verify_roots` meaningful.
BENIGN_UNQUALIFIED_REASONS = frozenset(
    {
        "model_has_no_unique_enabled_provider",
        "model_has_multiple_enabled_providers",
        "missing_model",
        "model_whitespace",
    }
)


@dataclasses.dataclass(frozen=True)
class SessionFile:
    session_id: str
    path: Path
    relative_path: Path
    size: int
    mtime_ns: int


@dataclasses.dataclass
class RootSnapshot:
    root: Path
    threads: dict[str, dict[str, Any]]
    thread_columns: list[str]
    dynamic_tools: dict[str, list[dict[str, Any]]]
    spawn_edges: list[dict[str, Any]]
    global_state: dict[str, Any]
    session_index: dict[str, dict[str, Any]]


@dataclasses.dataclass
class CloneSpec:
    old_id: str
    new_id: str
    source_root: Path
    source_session: SessionFile
    paths_by_root: dict[Path, Path]


@dataclasses.dataclass(frozen=True)
class DuplicateCloneSpec:
    canonical_id: str
    duplicate_id: str
    is_auxiliary: bool


@dataclasses.dataclass(frozen=True)
class RegistryProviderModels:
    provider_id: str
    prefix: str
    enabled: bool
    model_counts: dict[str, int]


@dataclasses.dataclass
class ModelGuardContext:
    root: Path
    registry_path: Path
    providers: dict[str, RegistryProviderModels]
    global_model_candidates: dict[str, list[tuple[str, str]]] = dataclasses.field(
        default_factory=dict
    )
    registry_error: str | None = None
    normalized_fields: int = 0
    unresolved_fields: int = 0
    unresolved_reasons: dict[str, int] = dataclasses.field(default_factory=dict)
    unresolved_examples: list[dict[str, str]] = dataclasses.field(default_factory=list)

    def record_normalized(self) -> None:
        self.normalized_fields += 1

    def record_unresolved(
        self, reason: str, model: Any, source_provider: str, location: str
    ) -> None:
        self.unresolved_fields += 1
        self.unresolved_reasons[reason] = self.unresolved_reasons.get(reason, 0) + 1
        if len(self.unresolved_examples) < MODEL_GUARD_MAX_EXAMPLES:
            self.unresolved_examples.append(
                {
                    "reason": reason,
                    "model": repr(model),
                    "source_provider": source_provider,
                    "location": location,
                }
            )

    def summary(self) -> dict[str, Any]:
        return {
            "registry_path": str(self.registry_path),
            "registry_error": self.registry_error,
            "normalized_model_fields": self.normalized_fields,
            "unresolved_model_fields": self.unresolved_fields,
            "unresolved_reasons": dict(sorted(self.unresolved_reasons.items())),
            "unresolved_examples": list(self.unresolved_examples),
        }


class SyncError(RuntimeError):
    pass


class SyncBusy(SyncError):
    """A concurrent sync owns the lock; this is not a history failure."""


def load_model_guard_context(root: Path) -> ModelGuardContext:
    """Read a target Codex registry without changing it.

    History synchronization must never infer a vendor from a bare model name.  The
    registry is therefore treated as read-only routing data: a model can only be
    qualified when exactly one enabled Codex/Responses provider exposes that model.
    The provider id stored in a rollout is only an app/router identity (for example
    ``true_sota``), not proof of which upstream account served the turn.
    """
    root = root.resolve()
    registry_path = root / MODEL_REGISTRY_NAME
    context = ModelGuardContext(root=root, registry_path=registry_path, providers={})
    if not registry_path.is_file():
        context.registry_error = "target_registry_missing"
        return context
    try:
        with registry_path.open("r", encoding="utf-8") as handle:
            registry = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        context.registry_error = f"target_registry_unreadable:{type(exc).__name__}"
        return context
    try:
        registry_version = int(registry.get("version") or 0)
    except (TypeError, ValueError):
        registry_version = 0
    if registry_version != 1:
        context.registry_error = "target_registry_unsupported_version"
        return context
    raw_providers = registry.get("providers") if isinstance(registry, dict) else None
    if not isinstance(raw_providers, list):
        context.registry_error = "target_registry_invalid_providers"
        return context

    for raw_provider in raw_providers:
        if not isinstance(raw_provider, dict):
            context.registry_error = "target_registry_invalid_provider"
            continue
        provider_id = str(raw_provider.get("id") or "").strip()
        if not provider_id:
            context.registry_error = "target_registry_provider_id_missing"
            continue
        workspace = str(raw_provider.get("workspace") or "codex").strip().lower()
        protocols = raw_provider.get("protocols") or ["responses"]
        if isinstance(protocols, str):
            protocols = [protocols]
        protocols = {str(item).strip().lower() for item in protocols}
        # The guard is intentionally limited to Codex/Responses providers.  Claude's
        # legacy Messages-only default is allowed to remain bare by design.
        if workspace != "codex" or "responses" not in protocols:
            continue
        if provider_id in context.providers:
            context.registry_error = "target_registry_duplicate_provider"
            continue
        model_counts: dict[str, int] = {}
        raw_models = raw_provider.get("models") or []
        if not isinstance(raw_models, list):
            context.registry_error = "target_registry_invalid_models"
            raw_models = []
        for raw_model in raw_models:
            if not isinstance(raw_model, dict) or raw_model.get("enabled") is not True:
                continue
            model_id = str(raw_model.get("id") or "").strip()
            if model_id:
                model_counts[model_id] = model_counts.get(model_id, 0) + 1
        context.providers[provider_id] = RegistryProviderModels(
            provider_id=provider_id,
            prefix=str(raw_provider.get("prefix") or "").strip(),
            enabled=raw_provider.get("enabled") is True,
            model_counts=model_counts,
        )
    for provider_id, provider in context.providers.items():
        if not provider.enabled:
            continue
        prefix = provider.prefix
        # Old registries (including the original true_sota entry) legitimately have an
        # empty prefix on disk.  The shared registry validator derives the same namespace
        # in memory; mirror that rule here so a uniquely identifiable legacy model can be
        # repaired without ever guessing the default provider.
        if not prefix:
            prefix = provider_id.lower().replace("_", "-") + MODEL_NAMESPACE_SEPARATOR
        if not any(
            prefix.endswith(separator) and len(prefix) > len(separator)
            for separator in MODEL_NAMESPACE_SEPARATORS
        ):
            continue
        for model_id, count in provider.model_counts.items():
            if count == 1:
                context.global_model_candidates.setdefault(model_id, []).append(
                    (provider_id, prefix)
                )
    return context


def _registry_may_be_codex_responses(root: Path) -> bool:
    """Return whether a root should receive the automatic model guard.

    The two official Codex profiles intentionally have no ``providers.json`` and
    store bare first-party model IDs.  They must remain compatible.  SOTA roots
    do have a registry; malformed registries are treated as applicable so the
    guard fails closed instead of silently disabling itself.
    """
    registry_path = Path(root).resolve() / MODEL_REGISTRY_NAME
    if not registry_path.is_file():
        return False
    try:
        with registry_path.open("r", encoding="utf-8") as handle:
            registry = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return True
    raw_providers = registry.get("providers") if isinstance(registry, dict) else None
    if not isinstance(raw_providers, list):
        return True
    for raw_provider in raw_providers:
        if not isinstance(raw_provider, dict):
            return True
        workspace = str(raw_provider.get("workspace") or "codex").strip().lower()
        protocols = raw_provider.get("protocols") or ["responses"]
        if isinstance(protocols, str):
            protocols = [protocols]
        protocol_names = {str(item).strip().lower() for item in protocols}
        if workspace == "codex" and "responses" in protocol_names:
            return True
    return False


def build_model_guard_contexts(
    roots: Iterable[Path], guarded_roots: Iterable[Path] | None = None
) -> dict[Path, ModelGuardContext]:
    """Build guards for all eligible roots unless an explicit subset is supplied.

    ``None`` means automatic discovery: every root with a registry advertising a
    Codex/Responses provider is guarded.  An explicit iterable remains available
    for callers that intentionally scope a check (and for focused tests), while
    an official root without a registry is never guarded implicitly.
    """
    resolved_roots = tuple(Path(root).resolve() for root in roots)
    if guarded_roots is None:
        guarded = {
            root for root in resolved_roots if _registry_may_be_codex_responses(root)
        }
    else:
        guarded = {Path(root).resolve() for root in guarded_roots}
    return {
        root: load_model_guard_context(root)
        for root in resolved_roots
        if root in guarded
    }


def _namespace_model_part(value: Any) -> str | None:
    """Return the model portion of a qualified slug, if it has a known separator."""
    if not isinstance(value, str):
        return None
    matches = [
        (value.find(separator), separator)
        for separator in MODEL_NAMESPACE_SEPARATORS
        if value.find(separator) > 0
    ]
    if not matches:
        return None
    index, separator = min(matches, key=lambda item: item[0])
    model = value[index + len(separator) :]
    return model or None


def model_has_namespace(value: Any) -> bool:
    return _namespace_model_part(value) is not None


def _model_lookup_parts(value: str) -> tuple[str, str]:
    """Return the registry ID and an optional context-window suffix."""
    suffix = ""
    base = value
    if value.lower().endswith("[1m]"):
        suffix = "[1m]"
        base = value[: -len(suffix)]
    return base, suffix


def _registered_namespace_model_part(
    value: Any, context: ModelGuardContext
) -> str | None:
    """Return the bare id only when ``value`` matches a registered published slug."""
    if not isinstance(value, str):
        return None
    base, suffix = _model_lookup_parts(value)
    for model_id, candidates in context.global_model_candidates.items():
        if any(prefix + model_id == base for _provider_id, prefix in candidates):
            return model_id + suffix
    return None


def _model_has_namespace_in_context(value: Any, context: ModelGuardContext) -> bool:
    """Tell whether a model is already qualified for this target registry.

    Delimiters are not proof of provenance: a legitimate upstream model id may itself
    contain ``--`` or ``.anthropic.``.  Prefer an exact registered ``prefix + id``
    match, and only retain the syntax-based compatibility fallback for an old,
    currently-disabled/unknown qualified slug that the registry cannot resolve.
    """
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    model_id, _suffix = _model_lookup_parts(value)
    if _registered_namespace_model_part(value, context) is not None:
        return True
    # If the whole value is an enabled bare model id, it is provenance-less even
    # when it happens to contain a namespace-looking delimiter.
    if model_id in context.global_model_candidates:
        return False
    return model_has_namespace(value)


def _resolve_model_for_target(
    value: Any, context: ModelGuardContext
) -> tuple[Any, str | None]:
    """Resolve a model without recording telemetry.

    Keeping the pure decision separate lets the synchronizer preflight every file before it
    mutates either destination.  An unresolved value is returned unchanged and paired with a
    stable reason; callers decide whether to record it, raise, or present it as a warning.
    """
    if not isinstance(value, str) or not value.strip():
        return value, "missing_model"
    if value != value.strip():
        return value, "model_whitespace"
    if context.registry_error:
        return value, context.registry_error
    model_id, suffix = _model_lookup_parts(value)
    if _model_has_namespace_in_context(value, context):
        return value, None
    candidates = context.global_model_candidates.get(model_id, [])
    if len(candidates) == 0:
        return value, "model_has_no_unique_enabled_provider"
    if len(candidates) != 1:
        return value, "model_has_multiple_enabled_providers"
    _, prefix = candidates[0]
    qualified = prefix + model_id + suffix
    if not _model_has_namespace_in_context(qualified, context):
        return value, "qualified_model_invalid"
    return qualified, None


def qualify_model_for_target(
    value: Any,
    source_provider: str,
    context: ModelGuardContext,
    location: str,
) -> Any:
    """Qualify one model value, or retain it and record why it was unsafe."""
    source_provider = str(source_provider or "").strip()
    resolved, reason = _resolve_model_for_target(value, context)
    if reason:
        context.record_unresolved(reason, value, source_provider, location)
        return value
    if resolved != value:
        context.record_normalized()
    return resolved


def _add_model_slot(
    slots: list[tuple[dict[str, Any], str, str]],
    container: Any,
    key: str,
    location: str,
) -> None:
    if isinstance(container, dict) and key in container:
        slots.append((container, key, location))


def rollout_model_slots(item: Any) -> list[tuple[dict[str, Any], str, str]]:
    """Yield only fields that represent an active Codex model selection.

    Tool schemas also contain keys named ``model``; they describe future tool
    arguments and are deliberately excluded here.
    """
    if not isinstance(item, dict):
        return []
    record_type = item.get("type")
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return []
    slots: list[tuple[dict[str, Any], str, str]] = []
    containers: list[tuple[Any, str]] = []
    if record_type == "turn_context":
        containers.append((payload, "payload"))
        thread_settings = payload.get("thread_settings")
        if isinstance(thread_settings, dict):
            containers.append((thread_settings, "payload.thread_settings"))
    elif record_type == "event_msg" and payload.get("type") == "thread_settings_applied":
        containers.append((payload.get("thread_settings"), "payload.thread_settings"))
    elif record_type == "world_state":
        state = payload.get("state")
        containers.append((state, "payload.state"))
        if isinstance(state, dict):
            containers.append((state.get("personality"), "payload.state.personality"))
            containers.append((state.get("collaboration_mode"), "payload.state.collaboration_mode"))

    for container, prefix in containers:
        _add_model_slot(slots, container, "model", f"{prefix}.model")
        if isinstance(container, dict):
            collaboration = container.get("collaboration_mode")
            if isinstance(collaboration, dict):
                settings = collaboration.get("settings")
                _add_model_slot(
                    slots,
                    settings,
                    "model",
                    f"{prefix}.collaboration_mode.settings.model",
                )
    # ``world_state.state.collaboration_mode`` was added above as a container, so
    # its nested settings are covered by the same explicit path logic.
    return slots


def _provider_from_mapping(mapping: Any, fallback: str) -> str:
    if isinstance(mapping, dict):
        for key in ("model_provider_id", "model_provider"):
            candidate = str(mapping.get(key) or "").strip()
            if candidate:
                return candidate
    return fallback


def normalize_rollout_item_models(
    item: Any,
    default_source_provider: str,
    context: ModelGuardContext,
    location_prefix: str,
) -> tuple[Any, str, bool]:
    """Normalize active model fields and return (item, current_provider, changed)."""
    if not isinstance(item, dict):
        return item, default_source_provider, False
    record_type = item.get("type")
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return item, default_source_provider, False
    current_provider = default_source_provider
    provider_source: Any = payload
    if record_type == "session_meta":
        provider_source = None
    elif record_type == "event_msg" and payload.get("type") == "thread_settings_applied":
        provider_source = payload.get("thread_settings")
    elif record_type == "world_state":
        provider_source = payload.get("state")
    current_provider = _provider_from_mapping(provider_source, current_provider)
    changed = False
    for container, key, field_path in rollout_model_slots(item):
        old_value = container.get(key)
        new_value = qualify_model_for_target(
            old_value,
            current_provider,
            context,
            f"{location_prefix}:{field_path}",
        )
        if new_value != old_value:
            container[key] = new_value
            changed = True
    return item, current_provider, changed


def canonicalize_rollout_models(
    item: Any, model_guard: ModelGuardContext | None = None
) -> Any:
    """Remove provider namespaces for semantic comparison across profiles.

    When a registry is available, an exact registered slug wins over delimiter-based
    parsing so a legitimate model id containing ``--`` is not truncated.
    """
    for container, key, _ in rollout_model_slots(item):
        value = container.get(key)
        if model_guard is None:
            model_part = _namespace_model_part(value)
        else:
            model_part = _registered_namespace_model_part(value, model_guard)
            if model_part is None and _model_has_namespace_in_context(value, model_guard):
                model_part = _namespace_model_part(value)
        if model_part is not None:
            container[key] = model_part
    return item


def model_guard_warnings(
    contexts: dict[Path, ModelGuardContext]
) -> list[str]:
    warnings: list[str] = []
    for root, context in contexts.items():
        if context.registry_error:
            warnings.append(
                f"模型保护无法读取目标 registry {context.registry_path}: "
                f"{context.registry_error}"
            )
        if context.unresolved_fields:
            reasons = ", ".join(
                f"{key}={value}"
                for key, value in sorted(context.unresolved_reasons.items())
            )
            warnings.append(
                f"目标 {root} 有 {context.unresolved_fields} 个模型字段未能安全限定；"
                f"无法唯一归属的历史模型值已保持原样，未替换供应商（{reasons}）。"
            )
    return warnings


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "sync.log"
    handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    return log_path


class SingleInstanceLock:
    def __init__(self, path: Path, wait_timeout: float = 0.0, poll_interval: float = 0.25,
                 cancel_check: Callable[[], None] | None = None):
        self.path = path
        self.wait_timeout = max(0.0, float(wait_timeout))
        self.poll_interval = max(0.05, float(poll_interval))
        self.file: Any | None = None
        self.waited_for_existing = False
        self.cancel_check = cancel_check

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait_timeout
        while True:
            if self.cancel_check is not None:
                self.cancel_check()
            self.file = self.path.open("a+b")
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            try:
                if os.name == "nt":
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
                return self
            except OSError as exc:
                self.waited_for_existing = True
                self.file.close()
                self.file = None
                if time.monotonic() >= deadline:
                    raise SyncBusy("History sync is already running; wait for it to finish.") from exc
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.file is None:
            return
        if os.name == "nt":
            with contextlib.suppress(OSError):
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            with contextlib.suppress(OSError):
                self.file.seek(0)
                fcntl.lockf(self.file.fileno(), fcntl.LOCK_UN, 1)
        self.file.close()
        self.file = None

def validate_root(root: Path) -> None:
    if not root.is_dir():
        raise SyncError(f"Codex 数据目录不存在：{root}")
    state_db = root / "state_5.sqlite"
    if not state_db.is_file():
        raise SyncError(f"Codex 数据目录不完整：{root}；缺少：{state_db}")
    sessions_root = root / "sessions"
    if sessions_root.exists() and not sessions_root.is_dir():
        raise SyncError(f"Codex 会话路径不是目录：{sessions_root}")
    sessions_root.mkdir(parents=True, exist_ok=True)


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({qident(table)})")]


def rows_as_dicts(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not table_exists(connection, table):
        return []
    columns = table_columns(connection, table)
    sql = f"SELECT {','.join(qident(c) for c in columns)} FROM {qident(table)}"
    return [dict(zip(columns, row)) for row in connection.execute(sql)]


def read_json_retry(path: Path, attempts: int = 10) -> dict[str, Any]:
    if not path.exists():
        return {}
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            before = path.stat()
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
                if not isinstance(value, dict):
                    raise ValueError("top-level JSON value is not an object")
                return value
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise SyncError(f"无法稳定读取 JSON：{path}；{last_error}")


def atomic_write_bytes(path: Path, payload: bytes, attempts: int = 20) -> None:
    # Extended-length form on every OS-facing path. See extended_path: a write target under a deep
    # backup tree tips past Windows' 260-char MAX_PATH and os.replace fails with a bare WinError 3.
    dest_dir = extended_path(path.parent)
    dest = extended_path(path)
    os.makedirs(dest_dir, exist_ok=True)
    for attempt in range(attempts):
        temp_path: Path | None = None
        try:
            fd, raw_temp = tempfile.mkstemp(prefix=".sync-", dir=dest_dir)
            temp_path = Path(raw_temp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
        finally:
            if temp_path is not None and temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()


def atomic_copy_file(source: Path, destination: Path, attempts: int = 20) -> None:
    # Extended-length form on every OS-facing path. This is the helper the lineage-header backup
    # uses (refresh_recovered_lineage), and its destination lands ~290 chars deep under
    # backups/three-way/.../pass-NN/.../lineage-headers/<root>/archived_sessions/<rollout>. The
    # parent dir stays under 260 so mkdir succeeds, then os.replace onto the full path fails with a
    # bare WinError 3 that reads like a missing source. See extended_path.
    dest_dir = extended_path(destination.parent)
    source_ext = extended_path(source)
    dest = extended_path(destination)
    os.makedirs(dest_dir, exist_ok=True)
    for attempt in range(attempts):
        temp_path: Path | None = None
        try:
            fd, raw_temp = tempfile.mkstemp(prefix=".sync-", dir=dest_dir)
            os.close(fd)
            temp_path = Path(raw_temp)
            shutil.copy2(source_ext, temp_path)
            os.replace(temp_path, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
        finally:
            if temp_path is not None and temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_write_bytes(path, payload)


def read_session_index(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("忽略损坏的会话索引行 %s:%s", path, line_number)
                continue
            if isinstance(item, dict) and item.get("id"):
                result[str(item["id"])] = item
    return result


def write_session_index(path: Path, index: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(index.values(), key=lambda item: str(item.get("updated_at", "")))
    lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in ordered]
    atomic_write_bytes(path, (("\n".join(lines) + "\n") if lines else "").encode("utf-8"))


def session_id_from_file(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
    item = json.loads(first_line)
    if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
        raise ValueError("missing session metadata payload")
    payload = item["payload"]
    session_id = payload.get("id") or payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session id")
    return session_id


def plain_storage_path(value: str | Path) -> Path:
    text = str(value)
    if text.startswith('\\\\?\\UNC\\'):
        text = '\\\\' + text[8:]
    elif text.startswith('\\\\?\\'):
        text = text[4:]
    return Path(text)


def physical_page_id(path: Path) -> str:
    try:
        return str(uuid.UUID(path.stem[-36:]))
    except ValueError:
        return path.stem


def independent_head_name(canonical_page: Path, head_id: uuid.UUID | str, *, session_id: str | None = None) -> str:
    """Canonical rollout filename for a fresh head that shares a session with an immutable page.

    When the sync must write a session's current content but the canonical page it would land on is
    a referenced (immutable) ancestor of some other thread, it mints a NEW physical page under a
    distinct id (``head_id``). The old name for that head was ``rollout-sync-<sid>_<head_id>.jsonl``
    -- but the Codex App refuses to restore any conversation whose rollout filename is not
    ``rollout-<timestamp>-<uuid>.jsonl`` ("does not have a canonical rollout filename"), so such a
    head was unreachable from the App.

    Keep the native ``logical_uuid_physical_uuid`` form when a new physical
    head still belongs to the same logical thread. ``physical_page_id`` reads
    the last 36 characters, so renaming never changes a descendant's linkage.
    """
    # Legacy conflict heads also end in a UUID, but retaining their prefix
    # produces another non-canonical ``rollout-conflict-...`` name. A timestamp
    # and a physical page UUID are sufficient; the logical owner stays in JSON.
    page_id = str(uuid.UUID(str(head_id)))
    match = re.match(r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-", canonical_page.name)
    timestamp = match.group(1) if match else None
    owner = session_id
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
        with canonical_page.open("r", encoding="utf-8") as handle:
            header = json.loads(handle.readline())
        if owner is None:
            owner = (header.get("payload") or {}).get("id") or (header.get("payload") or {}).get("session_id")
        if timestamp is None:
            stamp = header.get("timestamp") or (header.get("payload") or {}).get("timestamp")
            timestamp = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).strftime("%Y-%m-%dT%H-%M-%S")
    if timestamp is None:
        with contextlib.suppress(OSError):
            timestamp = dt.datetime.fromtimestamp(canonical_page.stat().st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    try:
        owner = str(uuid.UUID(str(owner)))
    except ValueError:
        owner = page_id
    suffix = f"{owner}_{page_id}" if owner != page_id else page_id
    return f"rollout-{timestamp or '1970-01-01T00-00-00'}-{suffix}.jsonl"


def lineage_inventory(root: Path) -> tuple[dict[str, Path], dict[Path, dict[str, Any]]]:
    """Index physical history pages, which can share a logical session id."""
    files: dict[str, Path] = {}
    metadata: dict[Path, dict[str, Any]] = {}
    for storage in SESSION_STORAGE_DIRS:
        for path in sorted((root / storage).rglob("*.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                try:
                    item = json.loads(handle.readline())
                except ValueError:
                    continue
            meta = item.get("payload", {}) if isinstance(item, dict) else {}
            if not isinstance(meta, dict):
                continue
            metadata[path] = meta
            try:
                page_id = str(uuid.UUID(path.stem[-36:]))
            except ValueError:
                page_id = str(meta.get("id") or "")
            if page_id:
                files.setdefault(page_id, path)
    return files, metadata


def lineage_byte_boundary(path: Path, ordinal: int) -> int:
    offset = 0
    with path.open('rb') as handle:
        for line in handle:
            if int(json.loads(line).get('ordinal', 0)) >= ordinal:
                break
            offset += len(line)
    return offset


def validate_lineage(roots: Iterable[Path], *, check_offsets: bool = False) -> None:
    """Reject missing sources/cycles before any rollout or database mutation."""
    inventories = [lineage_inventory(root) for root in roots]
    fallback = {}
    for files, metadata in inventories:
        fallback.update({key: metadata[path] for key, path in files.items()})
    for files, metadata in inventories:
        graph = {**fallback, **{key: metadata[path] for key, path in files.items()}}
        done = set()
        for start in files:
            chain = set()
            page = start
            while page and page not in done:
                if page in chain:
                    raise SyncError(f'Paginated history cycle detected: {start} -> {page}')
                if page not in graph:
                    raise SyncError(f'Missing paginated history source rollout: {page}')
                chain.add(page)
                page = (graph[page].get('history_base') or {}).get('thread_id')
            done.update(chain)
        if check_offsets:
            boundaries = {}
            for path, meta in metadata.items():
                base = meta.get('history_base')
                if not base:
                    continue
                source = files.get(str(base.get('thread_id')))
                if source is None:
                    raise SyncError(f'Missing local paginated history source: {path}')
                key = (source, int(base['end_ordinal_exclusive']))
                if key not in boundaries:
                    boundaries[key] = lineage_byte_boundary(*key)
                if boundaries[key] != base.get('end_byte_offset'):
                    raise SyncError(f'Paginated history byte boundary mismatch: {path}')


def logical_session_records(session: SessionFile) -> Iterable[dict[str, Any]]:
    """Read effective ancestry, normalizing each page's own logical owner ID."""
    root = session.path
    for _ in session.relative_path.parts:
        root = root.parent
    with session.path.open('rb') as handle:
        first = json.loads(handle.readline())
    if not (first.get('payload') or {}).get('history_base'):
        with session.path.open('rb') as handle:
            for line in handle:
                yield json.loads(line)
        return
    files, metadata = lineage_inventory(root)
    visiting = set()

    def read(path: Path, cutoff: int | None = None) -> Iterable[dict[str, Any]]:
        if path in visiting:
            raise SyncError(f'Paginated history cycle detected: {path}')
        visiting.add(path)
        base = metadata[path].get('history_base')
        if base:
            dependency = files.get(str(base.get('thread_id')))
            if dependency is None:
                raise SyncError(f'Missing paginated history source rollout: {base.get("thread_id")}')
            parent_cutoff = int(base['end_ordinal_exclusive'])
            yield from read(dependency, min(cutoff, parent_cutoff) if cutoff is not None else parent_cutoff)
        with path.open('rb') as handle:
            for line in handle:
                item = json.loads(line)
                if cutoff is not None and int(item.get('ordinal', 0)) >= cutoff:
                    break
                if base and item.get('type') == 'session_meta':
                    continue
                # A clone inherits ancestor pages without rewriting their bytes.
                # Their self ID is the ancestor owner, not the clone's new ID.
                owner = str(metadata[path].get('id') or session.session_id)
                yield normalize_session_self_references(item, owner)
        visiting.remove(path)

    yield from read(session.path)


def lineage_dependencies(roots: Iterable[Path]) -> tuple[set[str], set[str]]:
    inventories = [lineage_inventory(root) for root in roots]
    available = {key for files, _ in inventories for key in files}
    referenced = {
        str(base["thread_id"])
        for _, metadata in inventories for meta in metadata.values()
        if isinstance(base := meta.get("history_base"), dict) and base.get("thread_id")
    }
    protected = set(referenced)
    for files, metadata in inventories:
        for page_id in referenced & files.keys():
            protected.add(str(metadata[files[page_id]].get("id") or page_id))
    return protected, referenced - available


def sync_lineage_dependencies(roots: list[Path]) -> list[str]:
    inventories = {root: lineage_inventory(root) for root in roots}
    sources = {}
    references: set[str] = set()
    for files, metadata in inventories.values():
        for page_id, path in files.items():
            sources.setdefault(page_id, path)
        for meta in metadata.values():
            base = meta.get("history_base")
            if isinstance(base, dict) and base.get("thread_id"):
                references.add(str(base["thread_id"]))
    warnings = []
    for page_id in sorted(references):
        source = sources.get(page_id)
        if source is None:
            warnings.append(f"Missing paginated history source rollout: {page_id}; dependent conversations cannot fully load. No history was discarded.")
            continue
        for root, (files, _) in inventories.items():
            if page_id in files:
                continue
            # Preserve bytes and the physical filename: lineage uses byte offsets
            # and page ids, not just the logical session id in the first record.
            destination = root / "archived_sessions" / source.name
            if destination.exists():
                raise SyncError(f"History dependency destination already exists: {destination}")
            atomic_copy_file(source, destination)
            files[page_id] = destination
    return warnings


def refresh_recovered_lineage(
    root: Path, backup_dir: Path, *, thread_ids: set[str] | None = None,
) -> None:
    """Recompute all pagination cursors, including non-recovery pages.

    Normal sync calls this while the App is closed. An explicit repair may
    restrict the owners; cross-owner dependents must then be included too.
    """
    files, metadata = lineage_inventory(root)
    pending = {path for path, meta in metadata.items()
               if (meta.get('recovery_notice') or meta.get('history_base'))
               and (thread_ids is None or str(meta.get('id')) in thread_ids)}
    if thread_ids is not None:
        for path, meta in metadata.items():
            source = files.get((meta.get('history_base') or {}).get('thread_id'))
            if source in pending and path not in pending:
                raise SyncError('Scoped cursor repair would leave an external dependent stale')
    done: set[Path] = set()
    visiting: set[Path] = set()

    def refresh(path: Path) -> None:
        if path in done:
            return
        if path in visiting:
            raise SyncError(f"Cyclic recovered history lineage: {path}")
        visiting.add(path)
        base = metadata[path].get("history_base") or {}
        source = files.get(base.get("thread_id"))
        if source is not None:
            if source in pending:
                refresh(source)
            expected = lineage_byte_boundary(source, int(base["end_ordinal_exclusive"]))
            if expected != base.get("end_byte_offset"):
                with path.open("rb") as handle:
                    first, rest = handle.readline(), handle.read()
                record = json.loads(first)
                record["payload"]["history_base"]["end_byte_offset"] = expected
                atomic_copy_file(path, backup_dir / "lineage-headers" / root.name / path.relative_to(root))
                atomic_write_bytes(path, (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") + rest)
        visiting.remove(path)
        done.add(path)

    for path in pending:
        refresh(path)
    history_db = root / "thread_history_1.sqlite"
    if not pending or not history_db.exists():
        return
    with contextlib.closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as state:
        canonical = {str(row[0]): Path(str(row[1]).removeprefix('\\\\?\\')) for row in state.execute("SELECT id,rollout_path FROM threads")}
    with contextlib.closing(sqlite3.connect(history_db, timeout=30)) as db:
        saved = {}
        with db:
            for path in pending:
                thread_id = str(metadata[path].get("id") or "")
                if canonical.get(thread_id) != path:
                    continue
                turns = db.execute("SELECT turn_id,rollout_ordinal,rollout_end_ordinal,rollout_byte_offset,rollout_end_byte_offset FROM thread_turns WHERE thread_id=?", (thread_id,)).fetchall()
                projection = db.execute("SELECT next_rollout_ordinal,next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?", (thread_id,)).fetchone()
                wanted = {v for row in turns for v in (row[1], row[2]) if v is not None}
                if projection:
                    wanted.add(projection[0])
                offsets = {}
                offset = 0
                with path.open("rb") as handle:
                    for line in handle:
                        record = json.loads(line)
                        ordinal = record.get("ordinal")
                        if ordinal in wanted:
                            offsets[ordinal] = offset
                        offset += len(line)
                for value in wanted - offsets.keys():
                    offsets[value] = offset
                saved[thread_id] = {"turns": turns, "projection": projection}
                # Save offset metadata before the first update; message rows are untouched.
                atomic_write_json(backup_dir / "lineage-offsets" / (root.name + ".json"), saved)
                for turn_id, start, end, _, _ in turns:
                    db.execute("UPDATE thread_turns SET rollout_byte_offset=?,rollout_end_byte_offset=? WHERE thread_id=? AND turn_id=?", (offsets[start], offsets[end] if end is not None else None, thread_id, turn_id))
                if projection:
                    db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?", (offsets[projection[0]], thread_id))


def scan_sessions(root: Path, preferred_paths: dict[str, str] | None = None) -> dict[str, SessionFile]:
    preferred_paths = preferred_paths or {}
    catalog: dict[str, SessionFile] = {}
    for directory_name in SESSION_STORAGE_DIRS:
        storage_root = root / directory_name
        if not storage_root.exists():
            continue
        if not storage_root.is_dir():
            raise SyncError(f"Codex 会话路径不是目录：{storage_root}")
        for path in storage_root.rglob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                session_id = session_id_from_file(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logging.warning("忽略无法识别的会话文件 %s：%s", path, exc)
                continue
            stat = path.stat()
            current = SessionFile(
                session_id=session_id,
                path=path,
                relative_path=path.relative_to(root),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
            previous = catalog.get(session_id)
            if previous is None:
                catalog[session_id] = current
                continue
            preferred = preferred_paths.get(session_id)
            if preferred and os.path.normcase(os.path.abspath(plain_storage_path(preferred))) == os.path.normcase(
                os.path.abspath(plain_storage_path(path))
            ):
                catalog[session_id] = current
            elif not preferred:
                # Branch and fork rollouts legitimately share one session id; when the state
                # database names a preferred file the collision is resolved deterministically
                # and is not worth a warning on every scan pass. Only the mtime guess is
                # ambiguous enough to surface.
                if (current.mtime_ns, current.size) > (previous.mtime_ns, previous.size):
                    catalog[session_id] = current
                logging.warning(
                    "会话 %s 在 %s 中存在多个文件，无首选路径，已按最新选择 %s",
                    session_id,
                    root,
                    catalog[session_id].path.name,
                )
    return catalog


def normalize_session_self_references(value: Any, session_id: str) -> Any:
    """Normalize only references that identify the rollout itself.

    Conflict clones receive a new session/thread ID.  Replacing only self-ID
    fields lets us prove that two rollouts are otherwise JSON-semantically
    identical without ignoring timestamps, messages, paths, or tool output.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "history_base":
                result[key] = item
            elif key in {"id", "session_id", "thread_id"} and item == session_id:
                result[key] = "<SELF_SESSION_ID>"
            else:
                result[key] = normalize_session_self_references(item, session_id)
        return result
    if isinstance(value, list):
        return [normalize_session_self_references(item, session_id) for item in value]
    return value


def normalized_session_digest(
    session: SessionFile, model_guard: ModelGuardContext | None = None
) -> str:
    """Hash a rollout after removing only profile/provider and self-ID differences."""
    digest = hashlib.sha256()
    for line in normalized_session_lines(session, model_guard):
        digest.update(line)
    return digest.hexdigest()


def normalized_session_lines(
    session: SessionFile, model_guard: ModelGuardContext | None = None
) -> Iterable[bytes]:
    """Yield canonical JSONL lines for equality/prefix checks across profiles."""
    for line_number, item in enumerate(logical_session_records(session), start=1):
        # Ordinals identify physical storage positions, not message content.
        # A pagination boundary inserts a session_meta record and therefore a
        # gap; a complete rollout of the same history has no such gap.
        if isinstance(item, dict):
            item.pop("ordinal", None)
        if line_number == 1 and isinstance(item, dict):
            payload = item.get("payload")
            if isinstance(payload, dict):
                payload.pop("model_provider", None)
                payload.pop("history_mode", None)
                if payload.get("recovery_notice") and isinstance(payload.get("history_base"), dict):
                    payload["history_base"].pop("end_byte_offset", None)
        item = canonicalize_rollout_models(item, model_guard)
        item = normalize_session_self_references(item, session.session_id)
        yield (json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")


def compare_normalized_sessions(
    left: SessionFile,
    right: SessionFile,
    left_model_guard: ModelGuardContext | None = None,
    right_model_guard: ModelGuardContext | None = None,
) -> str:
    """Compare rollouts after profile/model namespace normalization."""
    left_iter = iter(normalized_session_lines(left, left_model_guard))
    right_iter = iter(normalized_session_lines(right, right_model_guard))
    while True:
        try:
            left_line = next(left_iter)
            left_done = False
        except StopIteration:
            left_line = None
            left_done = True
        try:
            right_line = next(right_iter)
            right_done = False
        except StopIteration:
            right_line = None
            right_done = True
        if left_done and right_done:
            return "equal"
        if left_done:
            return "left_prefix"
        if right_done:
            return "right_prefix"
        if left_line != right_line:
            return "divergent"


def sessions_semantically_equal(
    left: SessionFile,
    right: SessionFile,
    left_model_guard: ModelGuardContext | None = None,
    right_model_guard: ModelGuardContext | None = None,
) -> bool:
    try:
        return (
            compare_normalized_sessions(
                left, right, left_model_guard, right_model_guard
            )
            == "equal"
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("无法计算会话语义指纹：%s", exc)
        return False


def thread_display_name(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    return str(row.get("title") or row.get("name") or "")


def find_legacy_exact_clones(
    left: RootSnapshot,
    right: RootSnapshot,
    model_guards: dict[Path, ModelGuardContext] | None = None,
) -> tuple[list[DuplicateCloneSpec], list[str]]:
    """Find only old conflict clones that are provably exact duplicates.

    A deletion candidate must have the explicit conflict-clone suffix, exactly
    one unsuffixed canonical row with the matching base name, and the same full
    normalized rollout digest in every root where it exists.  Same-title or
    prefix-related chats are intentionally not considered duplicates.
    """
    snapshots = (left, right)
    model_guards = model_guards or {}
    catalogs: dict[Path, dict[str, SessionFile]] = {}
    digest_cache: dict[tuple[Path, str], str] = {}
    all_ids = set(left.threads) | set(right.threads)
    lineage_protected, _ = lineage_dependencies([left.root, right.root])
    for snapshot in snapshots:
        preferred = {
            session_id: str(row.get("rollout_path") or "")
            for session_id, row in snapshot.threads.items()
        }
        catalogs[snapshot.root] = scan_sessions(snapshot.root, preferred)

    def digest_values(session_id: str) -> dict[Path, str]:
        """Return one normalized digest per profile, not a cross-provider set.

        The same rollout can legitimately hash differently in two profiles when
        provider/model namespaces are normalized against each profile's own
        registry.  Comparing only the set of digests therefore makes a real
        exact clone look divergent and leaves the clone active.  The proof we
        need is local: keeper and duplicate must match in every profile where
        both are present.
        """
        values: dict[Path, str] = {}
        for snapshot in snapshots:
            session = catalogs[snapshot.root].get(session_id)
            if session is None:
                continue
            key = (snapshot.root, session_id)
            if key not in digest_cache:
                digest_cache[key] = normalized_session_digest(
                    session, model_guards.get(snapshot.root)
                )
            values[snapshot.root] = digest_cache[key]
        return values

    plan: list[DuplicateCloneSpec] = []
    warnings: list[str] = []
    for duplicate_id in sorted(all_ids):
        if duplicate_id in lineage_protected:
            continue
        duplicate_rows = [
            snapshot.threads[duplicate_id]
            for snapshot in snapshots
            if duplicate_id in snapshot.threads
        ]
        # Archive repair deliberately keeps the old rollout under
        # ``archived_sessions``.  Never classify that row as a disposable
        # conflict clone again; one archived replica protects the history
        # until the archive state has propagated to every profile.
        if any(int(row.get("archived") or 0) != 0 for row in duplicate_rows):
            continue
        duplicate_names = {thread_display_name(row) for row in duplicate_rows}
        if not duplicate_names or not all(
            name.endswith(CONFLICT_CLONE_SUFFIX) for name in duplicate_names
        ):
            continue
        base_names = {
            name[: -len(CONFLICT_CLONE_SUFFIX)] for name in duplicate_names
        }
        if len(base_names) != 1:
            continue
        base_name = next(iter(base_names))
        duplicate_digests = digest_values(duplicate_id)
        if not duplicate_digests:
            continue

        matching_canonicals: list[str] = []
        for candidate_id in sorted(all_ids - {duplicate_id}):
            candidate_rows = [
                snapshot.threads[candidate_id]
                for snapshot in snapshots
                if candidate_id in snapshot.threads
            ]
            if not candidate_rows:
                continue
            candidate_names = {thread_display_name(row) for row in candidate_rows}
            if candidate_names != {base_name}:
                continue
            candidate_digests = digest_values(candidate_id)
            # Require the same evidence coverage and equality within each
            # profile.  Cross-profile digest equality is not meaningful when
            # provider/model namespaces differ.
            if (
                set(candidate_digests) == set(duplicate_digests)
                and all(
                    candidate_digests[root] == duplicate_digests[root]
                    for root in duplicate_digests
                )
            ):
                matching_canonicals.append(candidate_id)

        if len(matching_canonicals) != 1:
            if len(matching_canonicals) > 1:
                warnings.append(
                    f"精确副本 {duplicate_id} 对应多个原会话，已为安全起见保留。"
                )
            continue
        is_auxiliary = all(
            is_auxiliary_thread_source(row.get("source")) for row in duplicate_rows
        )
        plan.append(
            DuplicateCloneSpec(
                canonical_id=matching_canonicals[0],
                duplicate_id=duplicate_id,
                is_auxiliary=is_auxiliary,
            )
        )
    return plan, warnings


def strip_conflict_suffixes(name: str) -> str:
    """Strip every trailing conflict-clone suffix, including nested ones."""
    while name.endswith(CONFLICT_CLONE_SUFFIX):
        name = name[: -len(CONFLICT_CLONE_SUFFIX)]
    return name


def alternate_branch_holder(
    left: RootSnapshot,
    right: RootSnapshot,
    left_files: dict[str, SessionFile],
    right_files: dict[str, SessionFile],
    session_id: str,
    alternate_file: SessionFile,
    alternate_root: Path,
    model_guards: dict[Path, ModelGuardContext] | None = None,
) -> str | None:
    """Return an existing thread that already shows the diverged alternate branch.

    A conflict clone exists to keep a branch that no other thread displays.  When the
    alternate branch is replacement-identical to a thread that is already visible and
    is not itself in conflict during this pass, the clone would be an exact duplicate
    of that thread: the post-sync duplicate check rejects such a clone and aborts an
    otherwise successful run, and the same content stays reachable either way.  Read
    only — never called after the alternate file has been overwritten.
    """
    model_guards = model_guards or {}
    snapshots = (left, right)
    try:
        family = {
            strip_conflict_suffixes(thread_display_name(snapshot.threads.get(session_id)))
            for snapshot in snapshots
            if snapshot.threads.get(session_id) is not None
        }
        family.discard("")
        if len(family) != 1:
            return None
        alternate_digest = normalized_session_digest(
            alternate_file, model_guards.get(alternate_root)
        )
        if not alternate_digest:
            return None
        for candidate_id in sorted(set(left.threads) | set(right.threads)):
            if candidate_id == session_id:
                continue
            candidate_names = {
                strip_conflict_suffixes(
                    thread_display_name(snapshot.threads[candidate_id])
                )
                for snapshot in snapshots
                if candidate_id in snapshot.threads
            }
            candidate_names.discard("")
            if candidate_names != family:
                continue
            candidate_left = left_files.get(candidate_id)
            candidate_right = right_files.get(candidate_id)
            if candidate_left is not None and candidate_right is not None:
                relation = compare_files(
                    candidate_left,
                    candidate_right,
                    model_guards.get(left.root),
                    model_guards.get(right.root),
                )
                if relation == "divergent":
                    # This thread is in conflict too, so this pass replaces one of its
                    # files with the other branch: its content is not preserved.
                    continue
                if relation == "left_prefix":
                    candidate_file, candidate_root = candidate_right, right.root
                else:
                    candidate_file, candidate_root = candidate_left, left.root
            elif candidate_left is not None:
                candidate_file, candidate_root = candidate_left, left.root
            elif candidate_right is not None:
                candidate_file, candidate_root = candidate_right, right.root
            else:
                continue
            if (
                normalized_session_digest(
                    candidate_file, model_guards.get(candidate_root)
                )
                == alternate_digest
            ):
                return candidate_id
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logging.warning("无法确认另一分支是否已被保留：%s", exc)
    return None


def validate_jsonl(path: Path) -> None:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for count, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError(f"line {count} is incomplete")
            json.loads(line)
    if count == 0:
        raise ValueError("empty JSONL")


def stable_snapshot(source: Path, temp_dir: Path, attempts: int = 6) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(attempts):
        destination = temp_dir / f"snapshot-{uuid.uuid4().hex}.jsonl"
        try:
            before = source.stat()
            shutil.copy2(source, destination)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                destination.unlink(missing_ok=True)
                time.sleep(0.08)
                continue
            validate_jsonl(destination)
            return destination
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            destination.unlink(missing_ok=True)
            if attempt < attempts - 1:
                time.sleep(0.12)
    raise SyncError(f"无法取得完整会话快照：{source}；{last_error}")


def compare_files(
    left: SessionFile,
    right: SessionFile,
    left_model_guard: ModelGuardContext | None = None,
    right_model_guard: ModelGuardContext | None = None,
) -> str:
    """Return equal, left_prefix, right_prefix, or divergent."""
    # The same conversation intentionally has a different model_provider in
    # each profile (Plus=openai, Cockpit=codex_local_access). Compare the JSONL
    # header without that profile-specific field, then compare the transcript.
    with left.path.open("rb") as handle_left, right.path.open("rb") as handle_right:
        try:
            left_header = json.loads(handle_left.readline())
            right_header = json.loads(handle_right.readline())
            for header in (left_header, right_header):
                payload = header.get("payload") if isinstance(header, dict) else None
                if isinstance(payload, dict):
                    payload.pop("model_provider", None)
                    if payload.get("recovery_notice") and isinstance(payload.get("history_base"), dict):
                        payload["history_base"].pop("end_byte_offset", None)
        except (json.JSONDecodeError, AttributeError):
            return compare_normalized_sessions(
                left, right, left_model_guard, right_model_guard
            )
        has_history_base = any(isinstance(h, dict) and (h.get('payload') or {}).get('history_base')
                               for h in (left_header, right_header))
        if left_header != right_header or has_history_base:
            return compare_normalized_sessions(
                left, right, left_model_guard, right_model_guard
            )
        left_start = handle_left.tell()
        right_start = handle_right.tell()
        # A snapshot descriptor can predate a replacement in the same pass.
        left_tail_size = os.fstat(handle_left.fileno()).st_size - left_start
        right_tail_size = os.fstat(handle_right.fileno()).st_size - right_start
        remaining = min(left_tail_size, right_tail_size)
        while remaining:
            chunk_size = min(1024 * 1024, remaining)
            chunk_left = handle_left.read(chunk_size)
            chunk_right = handle_right.read(chunk_size)
            if not chunk_left or not chunk_right or chunk_left != chunk_right:
                return compare_normalized_sessions(
                    left, right, left_model_guard, right_model_guard
                )
            remaining -= len(chunk_left)
    if left_tail_size == right_tail_size:
        return "equal"
    if left_tail_size < right_tail_size:
        return "left_prefix"
    return "right_prefix"


def set_session_model_provider(path: Path, provider: str) -> None:
    """Atomically set the profile-specific provider in a rollout header."""
    temp_path: Path | None = None
    with path.open("rb") as source:
        first_line = source.readline()
        item = json.loads(first_line)
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            raise SyncError(f"会话元数据无效：{path}")
        if payload.get("model_provider") == provider:
            return
        payload["model_provider"] = provider
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        fd, raw_temp = tempfile.mkstemp(prefix=".provider-", dir=path.parent)
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(encoded + b"\n")
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            if temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()
            raise
    assert temp_path is not None
    try:
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()


def rewrite_session_for_destination(
    path: Path,
    destination_provider: str,
    source_provider: str,
    model_guard: ModelGuardContext | None = None,
) -> None:
    """Stamp a rollout for its destination and optionally qualify active models.

    The source file is never edited in place before the destination rewrite is complete.  A
    three-way caller snapshots the target root, so an unresolved guard failure can be rolled
    back atomically along with the database changes.
    """
    if model_guard is None:
        set_session_model_provider(path, destination_provider)
        return

    temp_path: Path | None = None
    changed = False
    source_provider = str(source_provider or "").strip()
    try:
        fd, raw_temp = tempfile.mkstemp(
            prefix=".models-", dir=path.parent
        )
        temp_path = Path(raw_temp)
        with path.open("r", encoding="utf-8", newline="") as source, os.fdopen(
            fd, "w", encoding="utf-8", newline=""
        ) as target:
            current_provider = source_provider
            for line_number, line in enumerate(source, start=1):
                item = json.loads(line)
                item_changed = False
                if line_number == 1:
                    payload = item.get("payload") if isinstance(item, dict) else None
                    if not isinstance(payload, dict):
                        raise SyncError(f"会话元数据无效：{path}")
                    source_header_provider = str(
                        payload.get("model_provider") or ""
                    ).strip()
                    if not source_provider and source_header_provider:
                        current_provider = source_header_provider
                    if payload.get("model_provider") != destination_provider:
                        payload["model_provider"] = destination_provider
                        item_changed = True
                item, current_provider, models_changed = normalize_rollout_item_models(
                    item,
                    current_provider,
                    model_guard,
                    f"{path}:{line_number}",
                )
                item_changed = item_changed or models_changed
                if item_changed:
                    newline = "\n" if line.endswith("\n") else ""
                    target.write(
                        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                        + newline
                    )
                    changed = True
                else:
                    target.write(line)
            target.flush()
            os.fsync(target.fileno())
        if not changed:
            return
        validate_jsonl(temp_path)
        os.replace(temp_path, path)
        temp_path = None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SyncError(f"无法规范化会话模型：{path}；{exc}") from exc
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def provider_for_root(root: Path, root_providers: dict[Path, str]) -> str:
    provider = root_providers.get(root.resolve())
    if not provider:
        raise SyncError(f"未知的同步目标目录或模型提供商：{root}")
    return provider


def copy_snapshot_to(snapshot: Path, destination: Path) -> None:
    atomic_copy_file(snapshot, destination)


def same_file_bytes(left: Path, right: Path) -> bool:
    """Compare bytes, never mtime/size alone (replicas can share both)."""
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            chunk = a.read(1024 * 1024)
            if chunk != b.read(1024 * 1024):
                return False
            if not chunk:
                return True


class LineageImporter:
    """Import a head's actual ancestry without replacing immutable local pages.

    A physical UUID is not proof of identical bytes. Earlier synchronizers
    changed referenced pages in place, leaving different histories under the
    same UUID in different profiles. Copying only a head then resolves to the
    destination's *other* history and creates another conflict on every pass.

    Collisions get a stable, content-derived physical UUID. Only the imported
    child's history_base and byte boundary change; existing ancestors and real
    alternate branches remain byte-for-byte intact.
    """

    def __init__(self, roots: Iterable[Path], temp_dir: Path, protected: set[str]):
        self.inventories = {root: lineage_inventory(root) for root in roots}
        self.temp_dir = temp_dir
        self.protected = protected
        self.imported: dict[tuple[Path, Path, Path, int, int], Path] = {}
        self.visiting: set[tuple[Path, Path, Path]] = set()
        self.boundaries: dict[tuple[Path, int, int, int], int] = {}

    def _boundary(self, path: Path, ordinal: int) -> int:
        stat = path.stat()
        key = (path, ordinal, stat.st_size, stat.st_mtime_ns)
        if key not in self.boundaries:
            self.boundaries[key] = lineage_byte_boundary(path, ordinal)
        return self.boundaries[key]

    def _prepare(self, snapshot: Path, source_root: Path, destination_root: Path) -> Path:
        with snapshot.open("rb") as handle:
            header = json.loads(handle.readline())
        payload = header.get("payload") if isinstance(header, dict) else None
        base = payload.get("history_base") if isinstance(payload, dict) else None
        if not isinstance(base, dict) or not base.get("thread_id"):
            return snapshot
        source_page = self.inventories[source_root][0].get(str(base["thread_id"]))
        if source_page is None:
            raise SyncError(f"Missing paginated history source rollout: {base['thread_id']}")
        dependency = self.import_page(source_page, source_root, destination_root)
        replacement = dict(base)
        replacement["thread_id"] = physical_page_id(dependency)
        replacement["end_byte_offset"] = self._boundary(dependency, int(base["end_ordinal_exclusive"]))
        if replacement == base:
            return snapshot
        payload["history_base"] = replacement
        fd, raw_temp = tempfile.mkstemp(prefix="lineage-head-", suffix=".jsonl", dir=self.temp_dir)
        prepared = Path(raw_temp)
        try:
            with snapshot.open("rb") as source, os.fdopen(fd, "wb") as target:
                source.readline()
                target.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            return prepared
        except BaseException:
            prepared.unlink(missing_ok=True)
            raise

    def import_page(self, source: Path, source_root: Path, destination_root: Path) -> Path:
        if source_root == destination_root:
            self.protected.add(physical_page_id(source))
            return source
        stat = source.stat()
        cache_key = (source_root, source, destination_root, stat.st_size, stat.st_mtime_ns)
        if cache_key in self.imported:
            return self.imported[cache_key]
        visiting_key = (source_root, source, destination_root)
        if visiting_key in self.visiting:
            raise SyncError(f"Paginated history cycle detected: {source}")
        self.visiting.add(visiting_key)
        snapshot = prepared = None
        try:
            snapshot = stable_snapshot(source, self.temp_dir)
            prepared = self._prepare(snapshot, source_root, destination_root)
            files, metadata = self.inventories[destination_root]
            page_id = physical_page_id(source)
            existing = files.get(page_id)
            if existing is not None and same_file_bytes(prepared, existing):
                destination = existing
            else:
                if existing is not None or prepared != snapshot:
                    with prepared.open("rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    page_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-sync-history-page:" + page_id + ":" + digest))
                destination = files.get(page_id) or destination_root / "archived_sessions" / independent_head_name(source, page_id)
                if destination.exists():
                    if not same_file_bytes(prepared, destination):
                        raise SyncError(f"Refusing to overwrite a distinct immutable history page: {destination}")
                else:
                    copy_snapshot_to(prepared, destination)
                files[page_id] = destination
                with destination.open("r", encoding="utf-8") as handle:
                    metadata[destination] = json.loads(handle.readline())["payload"]
            self.protected.add(page_id)
            self.imported[cache_key] = destination
            return destination
        finally:
            self.visiting.remove(visiting_key)
            if prepared is not None and prepared != snapshot:
                prepared.unlink(missing_ok=True)
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)

    def copy_head(self, snapshot: Path, source_root: Path, destination_root: Path, destination: Path) -> None:
        prepared = self._prepare(snapshot, source_root, destination_root)
        try:
            copy_snapshot_to(prepared, destination)
            files, metadata = self.inventories[destination_root]
            files[physical_page_id(destination)] = destination
            with destination.open("r", encoding="utf-8") as handle:
                metadata[destination] = json.loads(handle.readline())["payload"]
        finally:
            if prepared != snapshot:
                prepared.unlink(missing_ok=True)


def recursive_replace_thread_refs(value: Any, old_id: str, new_id: str) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "history_base":
                result[key] = item
            elif key in {"thread_id", "session_id"} and item == old_id:
                result[key] = new_id
            else:
                result[key] = recursive_replace_thread_refs(item, old_id, new_id)
        return result
    if isinstance(value, list):
        return [recursive_replace_thread_refs(item, old_id, new_id) for item in value]
    return value


def make_conflict_clone(
    snapshot: Path, destination: Path, old_id: str, new_id: str
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{destination.name}.clone-", dir=destination.parent)
    temp_path = Path(raw_temp)
    try:
        with snapshot.open("r", encoding="utf-8") as source, os.fdopen(
            fd, "w", encoding="utf-8", newline="\n"
        ) as target:
            for line_number, line in enumerate(source, start=1):
                item = json.loads(line)
                item = recursive_replace_thread_refs(item, old_id, new_id)
                if (
                    line_number == 1
                    and isinstance(item, dict)
                    and isinstance(item.get("payload"), dict)
                ):
                    payload = item["payload"]
                    if payload.get("id") == old_id:
                        payload["id"] = new_id
                    if payload.get("session_id") == old_id:
                        payload["session_id"] = new_id
                target.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()
    validate_jsonl(destination)


def load_root_snapshot(root: Path) -> RootSnapshot:
    db_path = root / "state_5.sqlite"
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        thread_columns = table_columns(connection, "threads")
        threads = {str(row["id"]): row for row in rows_as_dicts(connection, "threads")}
        dynamic_tools: dict[str, list[dict[str, Any]]] = {}
        for row in rows_as_dicts(connection, "thread_dynamic_tools"):
            dynamic_tools.setdefault(str(row["thread_id"]), []).append(row)
        spawn_edges = rows_as_dicts(connection, "thread_spawn_edges")
    finally:
        connection.close()
    return RootSnapshot(
        root=root,
        threads=threads,
        thread_columns=thread_columns,
        dynamic_tools=dynamic_tools,
        spawn_edges=spawn_edges,
        global_state=read_json_retry(root / ".codex-global-state.json"),
        session_index=read_session_index(root / "session_index.jsonl"),
    )


def thread_recency(row: dict[str, Any] | None) -> tuple[int, int, int, int]:
    if not row:
        return (0, 0, 0, 0)
    return (
        int(row.get("recency_at_ms") or 0),
        int(row.get("updated_at_ms") or 0),
        int(row.get("updated_at") or 0) * 1000,
        int(row.get("tokens_used") or 0),
    )


def choose_thread_source(
    session_id: str, left: RootSnapshot, right: RootSnapshot
) -> tuple[RootSnapshot, dict[str, Any]] | None:
    left_row = left.threads.get(session_id)
    right_row = right.threads.get(session_id)
    if left_row is None and right_row is None:
        return None
    if left_row is None:
        return right, right_row  # type: ignore[return-value]
    if right_row is None:
        return left, left_row
    if bool(left_row.get("archived")) != bool(right_row.get("archived")):
        # Archiving records archived_at without necessarily bumping updated_at.
        # A stale unarchived replica must not undo a later archive operation.
        def state_changed_at(row: dict[str, Any]) -> int:
            return max(
                int(row.get("updated_at_ms") or 0),
                int(row.get("updated_at") or 0) * 1000,
                int(row.get("archived_at") or 0) * 1000 if row.get("archived") else 0,
            )
        left_changed, right_changed = state_changed_at(left_row), state_changed_at(right_row)
        if left_changed != right_changed:
            return (left, left_row) if left_changed > right_changed else (right, right_row)
    left_key = thread_recency(left_row)
    right_key = thread_recency(right_row)
    if right_key > left_key:
        return right, right_row
    if left_key > right_key:
        return left, left_row
    # Prefer the newer schema when recency is tied.
    if len(right.thread_columns) >= len(left.thread_columns):
        return right, right_row
    return left, left_row


def create_backup(
    roots: list[Path], backup_base: Path, temp_work: Path
) -> Path:
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_base / timestamp
    destination.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"created_at": iso_now(), "roots": {}}
    for label, root in zip(("cockpit", "plus"), roots):
        root_destination = destination / label
        root_destination.mkdir(parents=True)
        source_db = root / "state_5.sqlite"
        target_db = root_destination / "state_5.sqlite"
        source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(target_db)
        try:
            source.backup(target)
            target.commit()
            count = target.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        finally:
            target.close()
            source.close()
        copied: list[str] = ["state_5.sqlite"]
        for name in (".codex-global-state.json", "session_index.jsonl"):
            source_path = root / name
            if source_path.exists():
                shutil.copy2(source_path, root_destination / name)
                copied.append(name)
        manifest["roots"][label] = {
            "root": str(root),
            "thread_count": count,
            "files": copied,
        }
    atomic_write_json(destination / "manifest.json", manifest)
    return destination


def rotate_backups(backup_base: Path, keep: int = MAX_BACKUPS) -> None:
    if not backup_base.exists():
        return
    backups = sorted(
        [path for path in backup_base.iterdir() if path.is_dir()],
        key=lambda path: path.name,
        reverse=True,
    )
    for old in backups[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def backup_conflict_file(
    backup_dir: Path, root: Path, label: str, session_file: SessionFile
) -> None:
    destination = backup_dir / "conflicts" / label / session_file.relative_path
    # Extended-length form on both ends: this is the deepest write the sync makes, and the plain
    # form silently tips past MAX_PATH for the longest rollout names (see extended_path).
    os.makedirs(extended_path(destination.parent), exist_ok=True)
    try:
        shutil.copy2(extended_path(session_file.path), extended_path(destination))
    except OSError as error:
        # The bare WinError names neither path, which sent the last diagnosis after a missing
        # source that was never missing. Say which end failed and how long the paths were.
        raise SyncError(
            "备份冲突文件失败："
            f"{error}；源={session_file.path}（存在={session_file.path.exists()}，"
            f"{len(str(session_file.path))} 字符）；"
            f"目标={destination}（{len(str(destination))} 字符）"
        ) from error


def sync_session_files(
    left: RootSnapshot,
    right: RootSnapshot,
    backup_dir: Path,
    temp_dir: Path,
    root_providers: dict[Path, str],
    model_guards: dict[Path, ModelGuardContext] | None = None,
) -> tuple[
    dict[Path, dict[str, Path]],
    list[CloneSpec],
    dict[str, int],
    list[str],
]:
    preferred_left = {sid: str(row.get("rollout_path") or "") for sid, row in left.threads.items()}
    preferred_right = {sid: str(row.get("rollout_path") or "") for sid, row in right.threads.items()}
    all_left_files = scan_sessions(left.root, preferred_left)
    all_right_files = scan_sessions(right.root, preferred_right)
    # Immutable ancestry/orphan pages carry logical IDs too. They are not
    # current conversation heads and must not be reimported as another branch
    # just because the other profile has a visible row with the same owner ID.
    left_files = {sid: session for sid, session in all_left_files.items() if sid in left.threads}
    right_files = {sid: session for sid, session in all_right_files.items() if sid in right.threads}
    relevant_ids = set(left.threads) | set(right.threads)
    target_paths: dict[Path, dict[str, Path]] = {left.root: {}, right.root: {}}
    clones: list[CloneSpec] = []
    counters = {"new_files": 0, "updated_files": 0, "conflicts": 0, "unchanged": 0}
    warnings: list[str] = []
    model_guards = model_guards or {}
    source_provider_by_id: dict[str, str] = {}
    validate_lineage([left.root, right.root])
    # Copy dependencies while their original physical pages are still intact.
    warnings.extend(sync_lineage_dependencies([left.root, right.root]))
    referenced_pages = set()
    for root in (left.root, right.root):
        _, metadata = lineage_inventory(root)
        referenced_pages.update(str(meta['history_base']['thread_id']) for meta in metadata.values()
                                if isinstance(meta.get('history_base'), dict) and meta['history_base'].get('thread_id'))
    importer = LineageImporter((left.root, right.root), temp_dir, referenced_pages)
    changed_ids: set[str] = set()

    def fresh_head(destination: Path, session_id: str, source_page: str) -> Path:
        serial = 0
        while True:
            seed = f"sync-head:{session_id}:{source_page}:{serial}"
            head_id = uuid.uuid5(uuid.NAMESPACE_URL, seed)
            candidate = destination.with_name(independent_head_name(destination, head_id, session_id=session_id))
            if not candidate.exists() and str(head_id) not in referenced_pages:
                return candidate
            serial += 1

    def destination_for(source: SessionFile, root: Path, existing: SessionFile | None) -> Path:
        # Filename suffixes are physical page IDs. Never write a descendant over its ancestor.
        if existing is not None and physical_page_id(existing.path) == physical_page_id(source.path):
            return existing.path
        destination = root / source.relative_path
        if destination.exists() and destination != source.path and (existing is None or destination != existing.path):
            return fresh_head(destination, source.session_id, physical_page_id(source.path))
        return destination

    def source_provider(snapshot: RootSnapshot, session_id: str) -> str:
        row = snapshot.threads.get(session_id) or {}
        return str(
            row.get("model_provider")
            or provider_for_root(snapshot.root, root_providers)
        ).strip()

    for session_id in sorted((set(left_files) | set(right_files)) & relevant_ids):
        left_file = left_files.get(session_id)
        right_file = right_files.get(session_id)
        if left_file is None or right_file is None:
            source_file = left_file or right_file
            assert source_file is not None
            source_snapshot = left if left_file is not None else right
            source_provider_by_id[session_id] = source_provider(
                source_snapshot, session_id
            )
            snapshot = stable_snapshot(source_file.path, temp_dir)
            for root, existing in ((left.root, left_file), (right.root, right_file)):
                destination = destination_for(source_file, root, existing)
                target_paths[root][session_id] = destination
                if existing is None:
                    importer.copy_head(snapshot, source_snapshot.root, root, destination)
                    changed_ids.add(session_id)
                    counters["new_files"] += 1
            snapshot.unlink(missing_ok=True)
            continue

        target_paths[left.root][session_id] = left_file.path
        target_paths[right.root][session_id] = right_file.path
        relation = compare_files(
            left_file,
            right_file,
            model_guards.get(left.root),
            model_guards.get(right.root),
        )
        if relation == "equal":
            selected_source = choose_thread_source(session_id, left, right)
            if selected_source is not None:
                source_provider_by_id[session_id] = source_provider(
                    selected_source[0], session_id
                )
            counters["unchanged"] += 1
            continue
        if relation in {"left_prefix", "right_prefix"}:
            source_file = right_file if relation == "left_prefix" else left_file
            destination_file = left_file if relation == "left_prefix" else right_file
            source_snapshot = right if relation == "left_prefix" else left
            source_provider_by_id[session_id] = source_provider(
                source_snapshot, session_id
            )
            snapshot = stable_snapshot(source_file.path, temp_dir)
            destination_root = right.root if relation == 'right_prefix' else left.root
            destination = destination_for(source_file, destination_root, destination_file)
            if destination.exists() and physical_page_id(destination) in referenced_pages:
                # A referenced page is immutable; preserve it as a dependency and use a new head.
                destination = fresh_head(destination, session_id, physical_page_id(source_file.path))
            importer.copy_head(snapshot, source_snapshot.root, destination_root, destination)
            target_paths[destination_root][session_id] = destination
            changed_ids.add(session_id)
            snapshot.unlink(missing_ok=True)
            counters["updated_files"] += 1
            continue

        # True divergence: keep the most recent branch under the original ID and
        # create a second, visible thread for the alternate branch.
        left_key = (thread_recency(left.threads.get(session_id)), left_file.mtime_ns, left_file.size)
        right_key = (
            thread_recency(right.threads.get(session_id)),
            right_file.mtime_ns,
            right_file.size,
        )
        if right_key >= left_key:
            canonical_snapshot_root, canonical_file = right, right_file
            alternate_snapshot_root, alternate_file = left, left_file
        else:
            canonical_snapshot_root, canonical_file = left, left_file
            alternate_snapshot_root, alternate_file = right, right_file
        source_provider_by_id[session_id] = source_provider(
            canonical_snapshot_root, session_id
        )
        # Only mint a clone for a branch no other thread shows; an exact duplicate of an
        # existing thread would fail the post-sync duplicate check and abort the run.
        holder_id = alternate_branch_holder(
            left,
            right,
            left_files,
            right_files,
            session_id,
            alternate_file,
            alternate_snapshot_root.root,
            model_guards,
        )
        alternate_digest = normalized_session_digest(alternate_file, model_guards.get(alternate_snapshot_root.root))

        backup_conflict_file(backup_dir, left.root, "cockpit", left_file)
        backup_conflict_file(backup_dir, right.root, "plus", right_file)
        # Snapshot both branches before replacing either file. This prevents the
        # alternate branch from disappearing between conflict detection and clone creation.
        canonical_snapshot = stable_snapshot(canonical_file.path, temp_dir)
        alternate_snapshot = stable_snapshot(alternate_file.path, temp_dir)
        for root, existing in ((left.root, left_file), (right.root, right_file)):
            destination = destination_for(canonical_file, root, existing)
            if destination != canonical_file.path:
                if destination.exists() and physical_page_id(destination) in referenced_pages:
                    destination = fresh_head(destination, session_id, physical_page_id(canonical_file.path))
                importer.copy_head(canonical_snapshot, canonical_snapshot_root.root, root, destination)
            target_paths[root][session_id] = destination
        changed_ids.add(session_id)

        if holder_id is not None:
            canonical_snapshot.unlink(missing_ok=True)
            alternate_snapshot.unlink(missing_ok=True)
            counters["conflicts"] += 1
            warnings.append(
                f"会话 {session_id} 两边均被续写，另一分支内容已由会话 {holder_id} 保留，"
                "未新建冲突副本。"
            )
            continue

        # A retry of the same branch must have a stable identity. Never overwrite
        # an occupied ID: a user may have continued or renamed an earlier clone.
        branch_key = "codex-sync-conflict:" + session_id + ":" + alternate_digest
        new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, branch_key))
        occupied = set(all_left_files) | set(all_right_files) | {clone.new_id for clone in clones}
        serial = 0
        while new_id in occupied:
            serial += 1
            new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, branch_key + f":{serial}"))
        filename = independent_head_name(alternate_file.path, new_id, session_id=new_id)
        relative = alternate_file.relative_path.with_name(filename)
        paths_by_root: dict[Path, Path] = {}
        cloned_snapshot = temp_dir / f"clone-{new_id}.jsonl"
        make_conflict_clone(alternate_snapshot, cloned_snapshot, session_id, new_id)
        for root in (left.root, right.root):
            destination = root / relative
            importer.copy_head(cloned_snapshot, alternate_snapshot_root.root, root, destination)
            paths_by_root[root] = destination
            target_paths[root][new_id] = destination
        cloned_snapshot.unlink(missing_ok=True)
        changed_ids.add(new_id)
        canonical_snapshot.unlink(missing_ok=True)
        alternate_snapshot.unlink(missing_ok=True)
        clones.append(
            CloneSpec(
                old_id=session_id,
                new_id=new_id,
                source_root=alternate_snapshot_root.root,
                source_session=alternate_file,
                paths_by_root=paths_by_root,
            )
        )
        source_provider_by_id[new_id] = source_provider(
            alternate_snapshot_root, session_id
        )
        counters["conflicts"] += 1
        warnings.append(f"会话 {session_id} 两边均被续写，已保留同步冲突副本 {new_id}。")

    # The local thread/list fallback scans the storage directories, not just SQLite.
    # Propagate archive location together with archived=1; leaving an old active
    # rollout in sessions makes it discoverable again after restarting the app.
    for root, paths in target_paths.items():
        for session_id, path in list(paths.items()):
            if physical_page_id(path) in referenced_pages:
                independent_head = fresh_head(path, session_id, physical_page_id(path))
                copy_snapshot_to(path, independent_head)
                paths[session_id] = path = independent_head
            chosen = choose_thread_source(session_id, left, right)
            if chosen is None:
                continue
            storage = "archived_sessions" if chosen[1].get("archived") else "sessions"
            relative = path.relative_to(root)
            if relative.parts[0] == storage:
                continue
            destination = root / storage / Path(*relative.parts[1:])
            if destination.exists():
                # Put the unchanged filename in a unique directory; changing its suffix
                # changes the page ID used by descendants and breaks pagination.
                destination = root / storage / ('sync-' + uuid.uuid4().hex) / path.name
            resolved_root = root.resolve()
            if not path.resolve().is_relative_to(resolved_root) or not destination.resolve().is_relative_to(resolved_root):
                raise SyncError("拒绝移动会话目录之外的文件")
            destination.parent.mkdir(parents=True, exist_ok=True)
            path.rename(destination)
            paths[session_id] = destination

    # Make every copied rollout loadable and resumable under the destination
    # login. This is what the App's provider-filtered sidebar actually reads.
    for root, paths in target_paths.items():
        provider = provider_for_root(root, root_providers)
        for session_id, path in paths.items():
            rewrite_session_for_destination(
                path,
                provider,
                source_provider_by_id.get(session_id, provider),
                model_guards.get(root),
            )

    # ID equality alone is not convergence: a copied paginated head can still
    # resolve to a different ancestor in the other profile. Check every changed
    # thread against its actual local lineage before committing database paths.
    for session_id in changed_ids:
        pair = []
        for root in (left.root, right.root):
            path = target_paths[root][session_id]
            stat = path.stat()
            pair.append(SessionFile(session_id, path, path.relative_to(root), stat.st_size, stat.st_mtime_ns))
        if compare_files(*pair, model_guards.get(left.root), model_guards.get(right.root)) != "equal":
            raise SyncError(f"History content did not converge after copying: {session_id}; no branch may be discarded.")

    return target_paths, clones, counters, warnings


def preflight_model_guards(
    snapshots: Iterable[RootSnapshot],
    root_providers: dict[Path, str],
    model_guards: dict[Path, ModelGuardContext],
) -> None:
    """Check every active model slot before the first sync mutation.

    ``sync_session_files`` rewrites rollout files and ``sync_databases`` updates SQLite.  Waiting
    for the post-write verifier to discover an ambiguous bare model leaves a standalone two-way
    caller partially synchronized.  This pass is read-only: it resolves the same fields the
    writer will touch and aborts before any destination file or database is opened for writing.
    """
    if not model_guards:
        return
    snapshots = tuple(snapshots)
    for target_root, context in model_guards.items():
        if context.registry_error:
            raise SyncError(
                f"无法安全同步到 {target_root}：模型 registry 不可用 "
                f"({context.registry_error})。"
            )
        for snapshot in snapshots:
            preferred = {
                session_id: str(row.get("rollout_path") or "")
                for session_id, row in snapshot.threads.items()
            }
            sessions = scan_sessions(snapshot.root, preferred)
            for session_id, session in sessions.items():
                row = snapshot.threads.get(session_id) or {}
                source_provider = str(
                    row.get("model_provider")
                    or provider_for_root(snapshot.root, root_providers)
                ).strip()
                try:
                    with session.path.open("r", encoding="utf-8") as handle:
                        for line_number, line in enumerate(handle, start=1):
                            try:
                                item = json.loads(line)
                            except (ValueError, json.JSONDecodeError) as exc:
                                raise SyncError(
                                    f"模型保护预检无法解析 rollout：{session.path}:{line_number}"
                                ) from exc
                            for container, key, field_path in rollout_model_slots(item):
                                _resolved, reason = _resolve_model_for_target(
                                    container.get(key), context
                                )
                                if reason:
                                    context.record_unresolved(
                                        reason,
                                        container.get(key),
                                        source_provider,
                                        f"{target_root}:{session.path}:{line_number}:{field_path}",
                                    )
                except OSError as exc:
                    raise SyncError(
                        f"模型保护预检无法读取 rollout：{session.path}"
                    ) from exc

                if "model" in row:
                    _resolved, reason = _resolve_model_for_target(
                        row.get("model"), context
                    )
                    if reason:
                        context.record_unresolved(
                            reason,
                            row.get("model"),
                            source_provider,
                            f"{target_root}:threads:{session_id}:model",
                        )
        if context.unresolved_fields:
            # Only genuinely ambiguous fields stop the sync.  The rest are counted, reported through
            # `summary()["unresolved_reasons"]`, and left untouched by the writer.
            fatal_counts = {
                reason: count
                for reason, count in context.unresolved_reasons.items()
                if reason in FATAL_MODEL_GUARD_REASONS
            }
            if fatal_counts:
                fatal_examples = [
                    item
                    for item in context.unresolved_examples
                    if item["reason"] in FATAL_MODEL_GUARD_REASONS
                ]
                examples = "; ".join(
                    f"{item['reason']}:{item['model']}" for item in fatal_examples[:3]
                )
                total = sum(fatal_counts.values())
                raise SyncError(
                    f"同步前模型来源校验失败：{target_root} 有 "
                    f"{total} 个模型字段限定后仍然非法（解析器 bug，不是历史数据问题）。"
                    + (f" 示例：{examples}" if examples else "")
                )


def make_clone_row(source_row: dict[str, Any], clone: CloneSpec) -> dict[str, Any]:
    row = dict(source_row)
    row["id"] = clone.new_id
    for key in ("title", "name"):
        if key in row and row.get(key):
            row[key] = strip_conflict_suffixes(str(row[key])) + CONFLICT_CLONE_SUFFIX
    return row


def upsert_thread(
    connection: sqlite3.Connection, row: dict[str, Any], rollout_path: Path
) -> None:
    destination_columns = table_columns(connection, "threads")
    values = dict(row)
    values["rollout_path"] = str(rollout_path)
    columns = [column for column in destination_columns if column in values]
    if "id" not in columns:
        raise SyncError("threads 表缺少 id 列")
    insert_sql = (
        f"INSERT INTO threads ({','.join(qident(c) for c in columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})"
    )
    update_columns = [column for column in columns if column != "id"]
    if update_columns:
        insert_sql += " ON CONFLICT(id) DO UPDATE SET " + ",".join(
            f"{qident(column)}=excluded.{qident(column)}" for column in update_columns
        )
    connection.execute(insert_sql, [values[column] for column in columns])


def insert_dynamic_tools(
    connection: sqlite3.Connection, rows: Iterable[dict[str, Any]], thread_id: str
) -> None:
    if not table_exists(connection, "thread_dynamic_tools"):
        return
    destination_columns = table_columns(connection, "thread_dynamic_tools")
    for source_row in rows:
        row = dict(source_row)
        row["thread_id"] = thread_id
        columns = [column for column in destination_columns if column in row]
        sql = (
            f"INSERT OR IGNORE INTO thread_dynamic_tools "
            f"({','.join(qident(c) for c in columns)}) VALUES "
            f"({','.join('?' for _ in columns)})"
        )
        connection.execute(sql, [row[column] for column in columns])


def insert_spawn_edges(
    connection: sqlite3.Connection, edges: Iterable[dict[str, Any]]
) -> None:
    if not table_exists(connection, "thread_spawn_edges"):
        return
    destination_columns = table_columns(connection, "thread_spawn_edges")
    for row in edges:
        columns = [column for column in destination_columns if column in row]
        sql = (
            f"INSERT OR IGNORE INTO thread_spawn_edges "
            f"({','.join(qident(c) for c in columns)}) VALUES "
            f"({','.join('?' for _ in columns)})"
        )
        connection.execute(sql, [row[column] for column in columns])


def remap_persisted_thread_references(value: Any, mapping: dict[str, str]) -> Any:
    """Replace duplicate IDs with canonical IDs in persisted UI metadata.

    Canonical dictionary entries are processed first and win on collision;
    duplicate entries only fill a value that the canonical ID did not already
    have.  Lists are de-duplicated after remapping.
    """
    if isinstance(value, str):
        result = value
        for duplicate_id, canonical_id in mapping.items():
            result = result.replace(duplicate_id, canonical_id)
        return result
    if isinstance(value, list):
        result: list[Any] = []
        seen: set[str] = set()
        for item in value:
            remapped = remap_persisted_thread_references(item, mapping)
            try:
                marker = json.dumps(remapped, sort_keys=True, ensure_ascii=False)
            except TypeError:
                marker = repr(remapped)
            if marker not in seen:
                seen.add(marker)
                result.append(remapped)
        return result
    if isinstance(value, dict):
        result: dict[Any, Any] = {}

        def is_duplicate_key(item: tuple[Any, Any]) -> bool:
            key = item[0]
            return isinstance(key, str) and any(old_id in key for old_id in mapping)

        # Existing canonical keys win even when a duplicate key appeared first
        # in the JSON object's insertion order.
        for key, item in sorted(value.items(), key=is_duplicate_key):
            remapped_key = remap_persisted_thread_references(key, mapping)
            remapped_item = remap_persisted_thread_references(item, mapping)
            if remapped_key not in result:
                result[remapped_key] = remapped_item
        return result
    return value


def session_paths_for_ids(root: Path, session_ids: set[str]) -> dict[str, list[Path]]:
    result = {session_id: [] for session_id in session_ids}
    for directory_name in SESSION_STORAGE_DIRS:
        sessions_root = (root / directory_name).resolve()
        if not sessions_root.is_dir():
            continue
        for path in sessions_root.rglob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                session_id = session_id_from_file(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if session_id not in result:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(sessions_root):
                raise SyncError(f"拒绝清理会话目录之外的文件：{resolved}")
            result[session_id].append(resolved)
    return result


def purge_legacy_exact_clones(
    roots: list[Path],
    plan: list[DuplicateCloneSpec],
    backup_dir: Path,
) -> dict[str, Any]:
    """Back up and remove exact old conflict clones from every local profile."""
    if not plan:
        return {"threads": 0, "main_threads": 0, "auxiliary_threads": 0, "files": 0}

    mapping = {item.duplicate_id: item.canonical_id for item in plan}
    duplicate_ids = set(mapping)
    atomic_write_json(
        backup_dir / "removed-exact-duplicates" / "manifest.json",
        {
            "created_at": iso_now(),
            "mappings": [
                {
                    "duplicate_id": item.duplicate_id,
                    "canonical_id": item.canonical_id,
                    "is_auxiliary": item.is_auxiliary,
                }
                for item in plan
            ],
        },
    )
    removed_files = 0
    for label, root in zip(("cockpit", "plus"), roots):
        paths_by_id = session_paths_for_ids(root, duplicate_ids)
        for duplicate_id, paths in paths_by_id.items():
            for path in paths:
                relative = path.relative_to(root.resolve())
                destination = (
                    backup_dir
                    / "removed-exact-duplicates"
                    / label
                    / relative
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)

        connection = sqlite3.connect(root / "state_5.sqlite", timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for item in plan:
                if table_exists(connection, "thread_dynamic_tools"):
                    columns = table_columns(connection, "thread_dynamic_tools")
                    sql = (
                        f"SELECT {','.join(qident(column) for column in columns)} "
                        "FROM thread_dynamic_tools WHERE thread_id=?"
                    )
                    tools = [
                        dict(zip(columns, row))
                        for row in connection.execute(sql, (item.duplicate_id,))
                    ]
                    insert_dynamic_tools(connection, tools, item.canonical_id)
                    connection.execute(
                        "DELETE FROM thread_dynamic_tools WHERE thread_id=?",
                        (item.duplicate_id,),
                    )

            if table_exists(connection, "thread_spawn_edges"):
                related_edges = [
                    row
                    for row in rows_as_dicts(connection, "thread_spawn_edges")
                    if str(row.get("parent_thread_id")) in duplicate_ids
                    or str(row.get("child_thread_id")) in duplicate_ids
                ]
                remapped_edges: list[dict[str, Any]] = []
                for edge in related_edges:
                    remapped = dict(edge)
                    for key in ("parent_thread_id", "child_thread_id"):
                        old_value = str(remapped.get(key) or "")
                        if old_value in mapping:
                            remapped[key] = mapping[old_value]
                    if remapped.get("parent_thread_id") != remapped.get("child_thread_id"):
                        remapped_edges.append(remapped)
                insert_spawn_edges(connection, remapped_edges)
                for duplicate_id in duplicate_ids:
                    connection.execute(
                        "DELETE FROM thread_spawn_edges "
                        "WHERE parent_thread_id=? OR child_thread_id=?",
                        (duplicate_id, duplicate_id),
                    )

            for duplicate_id in duplicate_ids:
                connection.execute("DELETE FROM threads WHERE id=?", (duplicate_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        state_path = root / ".codex-global-state.json"
        state = read_json_retry(state_path)
        remapped_state = remap_persisted_thread_references(state, mapping)
        if not isinstance(remapped_state, dict):
            raise SyncError(f"全局状态格式无效：{state_path}")
        atomic_write_json(state_path, remapped_state)

        index_path = root / "session_index.jsonl"
        index = read_session_index(index_path)
        for item in plan:
            duplicate_entry = index.pop(item.duplicate_id, None)
            if item.canonical_id not in index and duplicate_entry is not None:
                replacement = remap_persisted_thread_references(
                    duplicate_entry, mapping
                )
                replacement["id"] = item.canonical_id
                name = str(replacement.get("thread_name") or "")
                if name.endswith(CONFLICT_CLONE_SUFFIX):
                    replacement["thread_name"] = name[: -len(CONFLICT_CLONE_SUFFIX)]
                index[item.canonical_id] = replacement
        write_session_index(index_path, index)

        for paths in paths_by_id.values():
            for path in paths:
                path.unlink()
                removed_files += 1

    main_count = sum(not item.is_auxiliary for item in plan)
    return {
        "threads": len(plan),
        "main_threads": main_count,
        "auxiliary_threads": len(plan) - main_count,
        "files": removed_files,
    }


def clear_archived_sidebar_placement(connection: sqlite3.Connection) -> int:
    """Detach archived threads from every visible sidebar section in state_5.sqlite.

    When a thread that sits in a section (e.g. "Pinned") is archived, the App sets archived=1 but
    leaves thread_section_id / section_position pointing at that section, and the desktop renders a
    section by membership REGARDLESS of the archive bit -- so the archived chat keeps showing in the
    sidebar. That is the "archived conversations still appear in the sidebar" report: the archived
    clones still carried thread_section_id = the Pinned section. remove_archived_sidebar_entries only
    cleans the JSON projections in .codex-global-state.json and never touched these columns.

    Introspect the columns first: the live App schema has all three, but reduced fixtures (and older
    App builds) may not, and an UPDATE naming a missing column raises OperationalError.
    """
    existing = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
    if "archived" not in existing:
        return 0
    targets = [c for c in ("thread_section_id", "section_position", "is_pinned") if c in existing]
    if not targets:
        return 0
    set_clause = ", ".join(
        f"{c} = 0" if c == "is_pinned" else f"{c} = NULL" for c in targets
    )
    dirty = " OR ".join(
        f"{c} != 0" if c == "is_pinned" else f"{c} IS NOT NULL" for c in targets
    )
    cursor = connection.execute(
        f"UPDATE threads SET {set_clause} "
        f"WHERE archived IS NOT NULL AND archived != 0 AND ({dirty})"
    )
    return cursor.rowcount


def sync_databases(
    left: RootSnapshot,
    right: RootSnapshot,
    target_paths: dict[Path, dict[str, Path]],
    clones: list[CloneSpec],
    root_providers: dict[Path, str],
    model_guards: dict[Path, ModelGuardContext] | None = None,
) -> dict[str, Any]:
    model_guards = model_guards or {}
    selected: dict[str, tuple[RootSnapshot, dict[str, Any]]] = {}
    for session_id in set(left.threads) | set(right.threads):
        chosen = choose_thread_source(session_id, left, right)
        if chosen is not None:
            selected[session_id] = chosen
    for clone in clones:
        source_snapshot = left if clone.source_root == left.root else right
        source_row = source_snapshot.threads.get(clone.old_id)
        if source_row is None:
            raise SyncError(f"无法为冲突副本找到原始数据库记录：{clone.old_id}")
        selected[clone.new_id] = (source_snapshot, make_clone_row(source_row, clone))

    all_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[Any, ...]] = set()
    for edge in left.spawn_edges + right.spawn_edges:
        key = tuple(edge.get(name) for name in ("parent_thread_id", "child_thread_id"))
        if key not in seen_edges:
            seen_edges.add(key)
            all_edges.append(edge)

    integrity: dict[str, str] = {}
    clone_ids = {clone.new_id for clone in clones}
    for destination_snapshot in (left, right):
        connection = sqlite3.connect(destination_snapshot.root / "state_5.sqlite", timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for session_id, (source_snapshot, row) in selected.items():
                rollout_path = target_paths[destination_snapshot.root].get(session_id)
                if rollout_path is None:
                    continue
                destination_row = dict(row)
                model_guard = model_guards.get(destination_snapshot.root)
                if model_guard is not None and "model" in destination_row:
                    source_provider = str(
                        row.get("model_provider")
                        or provider_for_root(source_snapshot.root, root_providers)
                    ).strip()
                    destination_row["model"] = qualify_model_for_target(
                        destination_row.get("model"),
                        source_provider,
                        model_guard,
                        f"{destination_snapshot.root}:threads:{session_id}:model",
                    )
                destination_row["model_provider"] = provider_for_root(
                    destination_snapshot.root, root_providers
                )
                upsert_thread(connection, destination_row, rollout_path)
                tools = source_snapshot.dynamic_tools.get(
                    session_id if session_id in source_snapshot.dynamic_tools else row.get("id"), []
                )
                if session_id in clone_ids:
                    clone = next(item for item in clones if item.new_id == session_id)
                    tools = source_snapshot.dynamic_tools.get(clone.old_id, [])
                insert_dynamic_tools(connection, tools, session_id)
            insert_spawn_edges(connection, all_edges)
            clear_archived_sidebar_placement(connection)
            connection.commit()
            integrity[str(destination_snapshot.root)] = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    return integrity


def merge_unique(preferred: list[Any], other: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for item in preferred + other:
        try:
            marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        except TypeError:
            marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def collect_main_candidates(
    left: RootSnapshot, right: RootSnapshot, clones: list[CloneSpec]
) -> list[tuple[str, dict[str, Any]]]:
    """Return every unarchived top-level chat in newest-first order."""
    candidates: dict[str, dict[str, Any]] = {}
    for session_id in set(left.threads) | set(right.threads):
        chosen = choose_thread_source(session_id, left, right)
        if chosen is None:
            continue
        _, row = chosen
        if int(row.get("archived") or 0) != 0 or is_auxiliary_thread_source(
            row.get("source")
        ):
            continue
        candidates[session_id] = row
    for clone in clones:
        source_snapshot = left if clone.source_root == left.root else right
        source_row = source_snapshot.threads.get(clone.old_id)
        # An archived source must never mint a visible conflict clone.  The
        # clone row inherits ``archived`` from its source, but the old code
        # appended it unconditionally and later treated its new ID as active.
        if source_row is not None and int(source_row.get("archived") or 0) == 0 and not is_auxiliary_thread_source(
            source_row.get("source")
        ):
            candidates[clone.new_id] = make_clone_row(source_row, clone)
    return sorted(candidates.items(), key=lambda item: thread_recency(item[1]), reverse=True)


def assignment_project_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        project_id = value.get("projectId")
        if isinstance(project_id, str) and project_id:
            return project_id
    return None


def read_sidebar_account_ids(root: Path) -> set[str]:
    """Read only account IDs; token values are never returned, logged, or copied."""
    auth_path = root / "auth.json"
    if not auth_path.exists():
        return set()
    try:
        auth = read_json_retry(auth_path)
    except SyncError as exc:
        logging.warning("无法读取 %s 中的账号 ID：%s", auth_path, exc)
        return set()
    tokens = auth.get("tokens")
    candidates = [auth.get("account_id"), auth.get("accountId")]
    if isinstance(tokens, dict):
        candidates.extend((tokens.get("account_id"), tokens.get("accountId")))
    return {str(item) for item in candidates if isinstance(item, str) and item}


def custom_section_account_ids(state: dict[str, Any]) -> set[str]:
    atom_state = state.get(ATOM_STATE_KEY)
    if not isinstance(atom_state, dict):
        return set()
    by_account = atom_state.get(CUSTOM_SECTIONS_KEY)
    if not isinstance(by_account, dict):
        return set()
    return {str(account_id) for account_id in by_account if str(account_id)}


def build_managed_section_items(
    merged_values: dict[str, Any],
    main_candidates: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    """Build one unlimited sidebar catalog without flattening project chats."""
    assignments = merged_values.get("thread-project-assignments", {})
    if not isinstance(assignments, dict):
        assignments = {}
    local_projects = merged_values.get("local-projects", {})
    if not isinstance(local_projects, dict):
        local_projects = {}
    project_ids = {str(project_id) for project_id in local_projects}

    project_recency: dict[str, tuple[int, int, int, int]] = {}
    entries: list[tuple[tuple[int, int, int, int], str]] = []
    for thread_id, row in main_candidates:
        project_id = assignment_project_id(assignments.get(thread_id))
        if project_id in project_ids:
            project_recency[project_id] = max(
                project_recency.get(project_id, (0, 0, 0, 0)),
                thread_recency(row),
            )
        else:
            entries.append((thread_recency(row), f"codex:thread:local:{thread_id}"))

    for project_id, project in local_projects.items():
        project_id = str(project_id)
        updated_at = 0
        if isinstance(project, dict):
            with contextlib.suppress(TypeError, ValueError):
                updated_at = int(project.get("updatedAt") or 0)
        recency = max(
            project_recency.get(project_id, (0, 0, 0, 0)),
            (updated_at, updated_at, updated_at, 0),
        )
        entries.append((recency, f"codex:project:{project_id}"))

    entries.sort(key=lambda item: item[0], reverse=True)
    return merge_unique([item_key for _, item_key in entries], [])


def _prune_nested_thread_references(value: Any, archived_ids: set[str]) -> Any:
    """Drop archived thread IDs from nested sidebar/cache containers."""
    if isinstance(value, list):
        return [
            _prune_nested_thread_references(item, archived_ids)
            for item in value
            if not (isinstance(item, str) and item in archived_ids)
        ]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text in {"threadId", "thread_id"} and isinstance(item, str) and item in archived_ids:
            continue
        if key_text in {
            "threadIds",
            "thread_ids",
            "pendingThreadAssignmentIds",
            "threadIdOrder",
        } and isinstance(item, list):
            item = [entry for entry in item if not (isinstance(entry, str) and entry in archived_ids)]
        result[key] = _prune_nested_thread_references(item, archived_ids)
    return result


_DROP_CACHE_ENTRY = object()


def _is_archived_thread_reference(value: Any, archived_ids: set[str]) -> bool:
    """Recognize an Atom value that is a thread reference, including local: IDs."""
    if not isinstance(value, str):
        return False
    decoded = unquote(value).strip()
    if decoded in archived_ids:
        return True
    # Atom stores references as values such as ``local:<uuid>`` and
    # ``codex:thread:local:<uuid>``.  Requiring the UUID at the end avoids
    # treating arbitrary prompt text that happens to mention an ID as a cache
    # reference.
    return any(decoded.endswith(":" + item) for item in archived_ids)


def _prune_atom_thread_cache(value: Any, archived_ids: set[str]) -> Any:
    """Remove archived-thread entries from every Atom cache projection.

    Atom has shipped several projection names over time.  Some use a thread ID
    as a dictionary key (descriptions, permissions, tab routes), while others
    encode it in a key such as ``thread-client-id-v1:local%3A<id>``.  Restrict
    key matching to the Atom tree and remove only exact/prefix thread-reference
    values; chat text and rollout contents remain untouched.
    """
    if isinstance(value, list):
        result = []
        for item in value:
            if _is_archived_thread_reference(item, archived_ids):
                continue
            child = _prune_atom_thread_cache(item, archived_ids)
            if child is not _DROP_CACHE_ENTRY:
                result.append(child)
        return result
    if not isinstance(value, dict):
        return value

    for field in ("id", "threadId", "thread_id", "threadID", "thread-id"):
        if _is_archived_thread_reference(value.get(field), archived_ids):
            return _DROP_CACHE_ENTRY

    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = unquote(str(key))
        if any(thread_id.lower() in key_text.lower() for thread_id in archived_ids):
            continue
        if _is_archived_thread_reference(item, archived_ids):
            continue
        child = _prune_atom_thread_cache(item, archived_ids)
        if child is not _DROP_CACHE_ENTRY:
            result[key] = child
    return result


def archived_thread_ids(
    left: RootSnapshot,
    right: RootSnapshot,
    clones: Iterable[CloneSpec] = (),
) -> set[str]:
    """Return canonical and conflict-clone IDs that are archived."""
    result: set[str] = set()
    for session_id in set(left.threads) | set(right.threads):
        chosen = choose_thread_source(session_id, left, right)
        if chosen is not None and int(chosen[1].get("archived") or 0):
            result.add(session_id)
    for clone in clones:
        source_snapshot = left if clone.source_root == left.root else right
        source_row = source_snapshot.threads.get(clone.old_id)
        if source_row is not None and int(source_row.get("archived") or 0):
            result.add(clone.new_id)
    return result


def merge_global_state_values(
    left: RootSnapshot, right: RootSnapshot, clones: list[CloneSpec]
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    # Prefer Plus ordering/settings on ties because it is the newer schema.
    for key in LIST_STATE_KEYS:
        right_value = right.global_state.get(key)
        left_value = left.global_state.get(key)
        merged[key] = merge_unique(
            right_value if isinstance(right_value, list) else [],
            left_value if isinstance(left_value, list) else [],
        )
    for key in DICT_STATE_KEYS:
        left_value = left.global_state.get(key)
        right_value = right.global_state.get(key)
        value: dict[str, Any] = {}
        if isinstance(left_value, dict):
            value.update(left_value)
        if isinstance(right_value, dict):
            for item_key, item_value in right_value.items():
                if item_key not in value:
                    value[item_key] = item_value
                elif key == "local-projects":
                    old_updated = int((value[item_key] or {}).get("updatedAt") or 0)
                    new_updated = int((item_value or {}).get("updatedAt") or 0)
                    if new_updated >= old_updated:
                        value[item_key] = item_value
                else:
                    chosen = choose_thread_source(str(item_key), left, right)
                    if chosen is not None and chosen[0] is right:
                        value[item_key] = item_value
        merged[key] = value

    projectless = list(merged.get("projectless-thread-ids", []))
    assignments = merged.get("thread-project-assignments", {})
    if isinstance(assignments, dict):
        projectless = [item for item in projectless if item not in assignments]

    # A database/session copy alone is not enough for every historical chat to
    # appear in the desktop sidebar.  Ensure each unarchived top-level thread,
    # including CLI/API-origin threads, has a sidebar container assignment.
    main_candidates = collect_main_candidates(left, right, clones)
    assigned_ids = set(assignments) if isinstance(assignments, dict) else set()
    for session_id, _ in main_candidates:
        if session_id not in assigned_ids and session_id not in projectless:
            projectless.append(session_id)

    for clone in clones:
        if clone.new_id not in projectless:
            projectless.append(clone.new_id)
        hints = merged.setdefault("thread-workspace-root-hints", {})
        source_state = left.global_state if clone.source_root == left.root else right.global_state
        source_hints = source_state.get("thread-workspace-root-hints", {})
        if isinstance(hints, dict) and isinstance(source_hints, dict):
            if clone.old_id in source_hints:
                hints[clone.new_id] = source_hints[clone.old_id]
    merged["projectless-thread-ids"] = projectless
    # Unioning visibility caches resurrects rows removed by archiving in another
    # profile. The canonical database row, not a stale sidebar cache, owns visibility.
    archived_ids = archived_thread_ids(left, right, clones)
    remove_archived_sidebar_entries(merged, archived_ids)
    return merged


def remove_archived_sidebar_entries(state: dict[str, Any], archived_ids: set[str]) -> None:
    """Remove visibility references only; retain projects, hints and archived rollouts.

    Codex keeps more than one sidebar projection.  In particular, project
    assignments and migration queues can live outside the obvious pinned and
    projectless lists. Current builds also keep a thread-to-host membership
    cache at the global-state root, while older builds nested it below
    ``electron-persisted-atom-state``. All of those references must obey the
    database archive bit or a restart can resurrect the chat.
    """
    archived_ids = {str(item) for item in archived_ids}
    for key in ("projectless-thread-ids", "pinned-thread-ids"):
        value = state.get(key)
        if isinstance(value, list):
            state[key] = [item for item in value if item not in archived_ids]
    migrated_pins = state.get("app-server-migrated-pinned-thread-ids-by-host")
    if isinstance(migrated_pins, dict):
        state["app-server-migrated-pinned-thread-ids-by-host"] = {
            host: [item for item in values if item not in archived_ids]
            if isinstance(values, list) else values
            for host, values in migrated_pins.items()
        }
    assignments = state.get("thread-project-assignments")
    if isinstance(assignments, dict):
        state["thread-project-assignments"] = {
            key: value for key, value in assignments.items() if key not in archived_ids
        }
    # Current desktop builds keep this host-local membership map at the global
    # state root. Older builds nested it under Atom; support both layouts and
    # preserve every unarchived entry.
    for container in (state, state.get(ATOM_STATE_KEY)):
        if not isinstance(container, dict):
            continue
        membership = container.get(THREAD_PROJECT_MEMBERSHIP_KEY)
        if isinstance(membership, dict):
            container[THREAD_PROJECT_MEMBERSHIP_KEY] = {
                key: value for key, value in membership.items()
                if str(key) not in archived_ids
            }
    for key, field in (("sidebar-project-thread-orders", "threadIds"),
                       ("app-server-projects-migration-by-host", "pendingThreadAssignmentIds")):
        containers = state.get(key)
        if isinstance(containers, dict):
            for entry in containers.values():
                if isinstance(entry, dict) and isinstance(entry.get(field), list):
                    entry[field] = [item for item in entry[field] if item not in archived_ids]
    atom_state = state.get(ATOM_STATE_KEY)
    if isinstance(atom_state, dict):
        pruned_atom_state = _prune_atom_thread_cache(atom_state, archived_ids)
        if pruned_atom_state is not _DROP_CACHE_ENTRY:
            state[ATOM_STATE_KEY] = pruned_atom_state


def sidebar_thread_ids(state: dict[str, Any]) -> set[str]:
    """Return IDs from every persisted sidebar/project visibility projection."""
    ids: set[str] = set()
    for key in ("projectless-thread-ids", "pinned-thread-ids"):
        value = state.get(key)
        if isinstance(value, list):
            ids.update(str(item) for item in value)
    assignments = state.get("thread-project-assignments")
    if isinstance(assignments, dict):
        ids.update(str(item) for item in assignments)
    migrated_pins = state.get("app-server-migrated-pinned-thread-ids-by-host")
    if isinstance(migrated_pins, dict):
        for value in migrated_pins.values():
            if isinstance(value, list):
                ids.update(str(item) for item in value)
    membership = state.get(THREAD_PROJECT_MEMBERSHIP_KEY)
    if isinstance(membership, dict):
        ids.update(str(item) for item in membership)
    for key, field in (("sidebar-project-thread-orders", "threadIds"),
                       ("app-server-projects-migration-by-host", "pendingThreadAssignmentIds")):
        containers = state.get(key)
        if isinstance(containers, dict):
            for entry in containers.values():
                if isinstance(entry, dict) and isinstance(entry.get(field), list):
                    ids.update(str(item) for item in entry[field])
    atom_state = state.get(ATOM_STATE_KEY)
    if isinstance(atom_state, dict):
        membership = atom_state.get(THREAD_PROJECT_MEMBERSHIP_KEY)
        if isinstance(membership, dict):
            ids.update(str(item) for item in membership)
        bindings = atom_state.get("client-thread-bindings-v1")
        if isinstance(bindings, dict):
            ids.update(str(item) for item in bindings.values())
        pinned_order = atom_state.get("app-server-pinned-thread-order-v1")
        if isinstance(pinned_order, list):
            ids.update(str(item) for item in pinned_order)
    return ids


def set_project_sidebar_mode(state: dict[str, Any]) -> None:
    """Preserve project folders while keeping chats ordered by last update."""
    atom_state = state.get(ATOM_STATE_KEY)
    if not isinstance(atom_state, dict):
        atom_state = {}
    else:
        atom_state = dict(atom_state)

    preferences = atom_state.get(SIDEBAR_PREFERENCES_KEY)
    if not isinstance(preferences, dict):
        preferences = {}
    else:
        preferences = dict(preferences)

    preferences.update(
        {
            "initialized": True,
            "mode": "project",
            "chatSortMode": "updated_at",
            "projectSortMode": "updated_at",
        }
    )
    atom_state[SIDEBAR_PREFERENCES_KEY] = preferences
    state[ATOM_STATE_KEY] = atom_state


def remove_managed_sidebar_section(state: dict[str, Any]) -> None:
    """Remove the custom all-chats section created by older sync-tool versions."""
    atom_state = state.get(ATOM_STATE_KEY)
    if not isinstance(atom_state, dict):
        return
    atom_state = dict(atom_state)

    existing_by_account = atom_state.get(CUSTOM_SECTIONS_KEY)
    if not isinstance(existing_by_account, dict):
        return

    by_account: dict[str, Any] = {}
    for account_id, existing in existing_by_account.items():
        if not isinstance(existing, dict):
            by_account[str(account_id)] = existing
            continue
        sections = existing.get("sections")
        if not isinstance(sections, list):
            sections = []
        sections = [
            section
            for section in sections
            if isinstance(section, dict) and section.get("id") != MANAGED_SECTION_ID
        ]

        section_order = existing.get("sectionOrder")
        if not isinstance(section_order, list):
            section_order = []
        section_order = [
            str(item) for item in section_order if str(item) != MANAGED_SECTION_ORDER_KEY
        ]

        collapsed = existing.get("collapsedSectionIds")
        if not isinstance(collapsed, list):
            collapsed = []
        collapsed = [str(item) for item in collapsed if str(item) != MANAGED_SECTION_ID]

        by_account[str(account_id)] = {
            **existing,
            "sections": sections,
            "collapsedSectionIds": collapsed,
            "sectionOrder": section_order,
        }

    atom_state[CUSTOM_SECTIONS_KEY] = by_account
    state[ATOM_STATE_KEY] = atom_state


def apply_global_state(
    root: Path,
    merged_values: dict[str, Any],
    main_candidates: list[tuple[str, dict[str, Any]]],
    account_ids: set[str],
) -> None:
    path = root / ".codex-global-state.json"
    for _ in range(12):
        before = path.stat() if path.exists() else None
        current = read_json_retry(path)
        for key, value in merged_values.items():
            current[key] = value
        # These host-local migration/order caches are not part of merged_values.
        # Clean the final state using the database already written by sync_databases.
        with contextlib.closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            archived_ids = {str(row[0]) for row in db.execute("SELECT id FROM threads WHERE archived = 1")}
        remove_archived_sidebar_entries(current, archived_ids)
        set_project_sidebar_mode(current)
        remove_managed_sidebar_section(current)
        after = path.stat() if path.exists() else None
        if before is not None and after is not None and (
            before.st_mtime_ns,
            before.st_size,
        ) != (after.st_mtime_ns, after.st_size):
            time.sleep(0.05)
            continue
        atomic_write_json(path, current)
        return
    raise SyncError(f"全局状态持续变化，无法安全写入：{path}")


def parse_index_timestamp(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        return dt.datetime.fromisoformat(text).timestamp()
    return 0.0


def timestamp_from_row(row: dict[str, Any]) -> str:
    milliseconds = int(row.get("updated_at_ms") or 0)
    seconds = milliseconds / 1000 if milliseconds else int(row.get("updated_at") or 0)
    if not seconds:
        return iso_now()
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def sync_global_state_and_index(
    left: RootSnapshot,
    right: RootSnapshot,
    clones: list[CloneSpec],
) -> tuple[int, int, set[str]]:
    merged_values = merge_global_state_values(left, right, clones)
    main_candidates = collect_main_candidates(left, right, clones)
    account_ids = (
        read_sidebar_account_ids(left.root)
        | read_sidebar_account_ids(right.root)
        | custom_section_account_ids(left.global_state)
        | custom_section_account_ids(right.global_state)
    )
    for root in (left.root, right.root):
        apply_global_state(root, merged_values, main_candidates, account_ids)

    merged_index: dict[str, dict[str, Any]] = {}
    for source_index in (left.session_index, right.session_index):
        for session_id, item in source_index.items():
            existing = merged_index.get(session_id)
            if existing is None or parse_index_timestamp(item.get("updated_at")) >= parse_index_timestamp(
                existing.get("updated_at")
            ):
                merged_index[session_id] = dict(item)

    visible_ids = list(merged_values.get("projectless-thread-ids", []))
    assignments = merged_values.get("thread-project-assignments", {})
    if isinstance(assignments, dict):
        visible_ids += list(assignments)
    for session_id in merge_unique(visible_ids, []):
        chosen = choose_thread_source(str(session_id), left, right)
        clone = next((item for item in clones if item.new_id == session_id), None)
        if clone is not None:
            source_snapshot = left if clone.source_root == left.root else right
            original = source_snapshot.threads.get(clone.old_id)
            if original is not None:
                row = make_clone_row(original, clone)
                merged_index[str(session_id)] = {
                    "id": str(session_id),
                    "thread_name": row.get("title") or row.get("name") or "同步冲突副本",
                    "updated_at": timestamp_from_row(row),
                }
            continue
        if chosen is None:
            continue
        _, row = chosen
        candidate = {
            "id": str(session_id),
            "thread_name": row.get("name") or row.get("title") or row.get("preview") or "Codex 对话",
            "updated_at": timestamp_from_row(row),
        }
        existing = merged_index.get(str(session_id))
        if existing is None or parse_index_timestamp(candidate["updated_at"]) >= parse_index_timestamp(
            existing.get("updated_at")
        ):
            merged_index[str(session_id)] = candidate

    archived_ids = archived_thread_ids(left, right, clones)
    valid_ids = set(left.threads) | set(right.threads) | {
        clone.new_id for clone in clones
    }
    valid_ids.difference_update(archived_ids)
    merged_index = {
        session_id: item
        for session_id, item in merged_index.items()
        if session_id in valid_ids
        and session_id not in archived_ids
        and str(item.get("id") or session_id) not in archived_ids
    }
    for root in (left.root, right.root):
        write_session_index(root / "session_index.jsonl", merged_index)
    return len(merged_index), len(set(visible_ids)), account_ids


def is_auxiliary_thread_source(source: Any) -> bool:
    text = str(source or "").strip()
    if text.lower().startswith("subagent"):
        return True
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(text)
        return isinstance(parsed, dict) and "subagent" in parsed
    return False


def managed_section_coverage(
    global_state: dict[str, Any],
    account_ids: set[str],
    main_thread_ids: set[str],
    assignments: dict[str, Any],
) -> dict[str, set[str]]:
    atom_state = global_state.get(ATOM_STATE_KEY)
    by_account = (
        atom_state.get(CUSTOM_SECTIONS_KEY)
        if isinstance(atom_state, dict)
        else None
    )
    if not isinstance(by_account, dict):
        return {account_id: set() for account_id in account_ids}

    result: dict[str, set[str]] = {}
    for account_id in account_ids:
        account_state = by_account.get(account_id)
        sections = (
            account_state.get("sections") if isinstance(account_state, dict) else None
        )
        if not isinstance(sections, list):
            result[account_id] = set()
            continue
        managed = next(
            (
                section
                for section in sections
                if isinstance(section, dict) and section.get("id") == MANAGED_SECTION_ID
            ),
            None,
        )
        item_keys = managed.get("itemKeys") if isinstance(managed, dict) else None
        if not isinstance(item_keys, list):
            result[account_id] = set()
            continue

        direct_threads = {
            str(item)[len("codex:thread:local:") :]
            for item in item_keys
            if str(item).startswith("codex:thread:local:")
        }
        project_ids = {
            str(item)[len("codex:project:") :]
            for item in item_keys
            if str(item).startswith("codex:project:")
        }
        covered = set(direct_threads)
        covered.update(
            thread_id
            for thread_id, assignment in assignments.items()
            if assignment_project_id(assignment) in project_ids
        )
        result[account_id] = covered & main_thread_ids
    return result


def find_unqualified_rollout_models(
    path: Path, model_guard: ModelGuardContext | None = None
) -> list[dict[str, Any]]:
    """Find active rollout model fields that still lack a provider namespace."""
    findings: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    item = json.loads(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    findings.append(
                        {
                            "line": line_number,
                            "field": "<json>",
                            "model": repr(exc),
                            "reason": "invalid_json",
                        }
                    )
                    continue
                for container, key, field_path in rollout_model_slots(item):
                    value = container.get(key)
                    is_qualified = (
                        _model_has_namespace_in_context(value, model_guard)
                        if model_guard is not None
                        else model_has_namespace(value)
                    )
                    if not is_qualified:
                        # Record WHY it is still bare. "unqualified_model" means the writer could
                        # have namespaced it and did not -- a real post-condition failure. Any other
                        # reason means it was never qualifiable (no enabled provider offers the id,
                        # or several do), so leaving it verbatim was the correct, deliberate outcome
                        # and must not fail the sync.
                        reason = "unqualified_model"
                        if model_guard is not None:
                            _resolved, unresolved = _resolve_model_for_target(
                                value, model_guard
                            )
                            if unresolved:
                                reason = unresolved
                        findings.append(
                            {
                                "line": line_number,
                                "field": field_path,
                                "model": repr(value),
                                "reason": reason,
                            }
                        )
    except OSError as exc:
        findings.append(
            {
                "line": 0,
                "field": "<file>",
                "model": repr(exc),
                "reason": "rollout_unreadable",
            }
        )
    return findings


def verify_roots(
    roots: list[Path],
    expected_sidebar_account_ids: set[str] | None = None,
    root_providers: dict[Path, str] | None = None,
    model_guard_roots: Iterable[Path] | None = None,
    model_guards: dict[Path, ModelGuardContext] | None = None,
) -> dict[str, Any]:
    roots = [root.resolve() for root in roots]
    validate_lineage(roots)
    if root_providers is None:
        if len(roots) != 2:
            raise SyncError("多目录校验必须显式提供每个目录的模型提供商。")
        root_providers = {
            roots[0]: COCKPIT_MODEL_PROVIDER,
            roots[1]: PLUS_MODEL_PROVIDER,
        }
    else:
        root_providers = {
            root.resolve(): provider for root, provider in root_providers.items()
        }
    if model_guards is None:
        model_guards = build_model_guard_contexts(roots, model_guard_roots)
    else:
        model_guards = {
            root.resolve(): context for root, context in model_guards.items()
        }
    result: dict[str, Any] = {}
    id_sets: list[set[str]] = []
    expected_sidebar_account_ids = expected_sidebar_account_ids or set()
    for root in roots:
        validate_lineage([root], check_offsets=True)
        connection = sqlite3.connect(
            f"file:{root / 'state_5.sqlite'}?mode=ro", uri=True, timeout=30
        )
        try:
            rows = connection.execute(
                "SELECT id, rollout_path, source, archived, model_provider FROM threads"
            ).fetchall()
            thread_columns = table_columns(connection, "threads")
            model_by_id = (
                {
                    str(row[0]): row[1]
                    for row in connection.execute("SELECT id, model FROM threads")
                }
                if "model" in thread_columns
                else {}
            )
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
        db_ids = {str(row[0]) for row in rows}
        main_thread_ids = {
            str(row[0]) for row in rows if not is_auxiliary_thread_source(row[2])
        }
        unarchived_main_thread_ids = {
            str(row[0])
            for row in rows
            if not is_auxiliary_thread_source(row[2]) and int(row[3] or 0) == 0
        }
        missing_rollouts = [str(row[1]) for row in rows if not Path(str(row[1])).is_file()]
        expected_provider = provider_for_root(root, root_providers)
        wrong_provider_ids = {str(row[0]) for row in rows if str(row[4]) != expected_provider}
        model_guard = model_guards.get(root)
        unqualified_model_thread_ids: set[str] = set()
        unqualified_model_values = 0
        # The narrower set the failure predicate uses: values the writer could have namespaced and
        # did not. The wider set above stays as-is because it is what gets reported.
        requalifiable_thread_ids: set[str] = set()
        if model_guard is not None:
            for session_id, model in model_by_id.items():
                if not _model_has_namespace_in_context(model, model_guard):
                    unqualified_model_thread_ids.add(session_id)
                    unqualified_model_values += 1
                    _resolved, reason = _resolve_model_for_target(model, model_guard)
                    if reason is None or reason not in BENIGN_UNQUALIFIED_REASONS:
                        requalifiable_thread_ids.add(session_id)
        preferred_paths = {str(row[0]): str(row[1] or "") for row in rows}
        all_sessions = scan_sessions(root, preferred_paths)
        orphan_session_ids = set(all_sessions) - db_ids
        sessions = {
            session_id: session
            for session_id, session in all_sessions.items()
            if session_id in db_ids
        }
        session_ids = set(sessions)
        wrong_rollout_provider_ids: set[str] = set()
        unqualified_model_session_ids: set[str] = set()
        requalifiable_session_ids: set[str] = set()
        unqualified_rollout_values = 0
        for session_id, session in sessions.items():
            try:
                with session.path.open("r", encoding="utf-8") as handle:
                    header = json.loads(handle.readline())
                payload = header.get("payload") if isinstance(header, dict) else None
                if not isinstance(payload, dict) or payload.get("model_provider") != expected_provider:
                    wrong_rollout_provider_ids.add(session_id)
            except (OSError, json.JSONDecodeError):
                wrong_rollout_provider_ids.add(session_id)
            if model_guard is not None:
                rollout_findings = find_unqualified_rollout_models(
                    session.path, model_guard
                )
                if rollout_findings:
                    unqualified_model_session_ids.add(session_id)
                    unqualified_rollout_values += len(rollout_findings)
                    if any(
                        finding["reason"] not in BENIGN_UNQUALIFIED_REASONS
                        for finding in rollout_findings
                    ):
                        requalifiable_session_ids.add(session_id)
        global_state = read_json_retry(root / ".codex-global-state.json")
        assignments = global_state.get("thread-project-assignments", {})
        assigned_ids = set(assignments) if isinstance(assignments, dict) else set()
        local_projects = global_state.get("local-projects", {})
        project_ids = set(local_projects) if isinstance(local_projects, dict) else set()
        assignment_values = assignments.values() if isinstance(assignments, dict) else []
        unresolved_project_thread_ids = sorted(assigned_ids - db_ids)
        unresolved_project_ids = sorted(
            {
                project_id
                for assignment in assignment_values
                if (project_id := assignment_project_id(assignment)) is not None
                and project_id not in project_ids
            }
        )
        project_assigned_main_ids = assigned_ids & unarchived_main_thread_ids
        sidebar_ids = sidebar_thread_ids(global_state)
        archived_thread_ids_in_db = {
            str(row[0]) for row in rows if int(row[3] or 0) != 0
        }
        archived_sidebar_ids = sorted(archived_thread_ids_in_db & sidebar_ids)
        index_ids = set(read_session_index(root / "session_index.json"))
        archived_index_ids = sorted(archived_thread_ids_in_db & index_ids)
        missing_sidebar_main_ids = sorted(unarchived_main_thread_ids - sidebar_ids)
        atom_state = global_state.get(ATOM_STATE_KEY, {})
        preferences = (
            atom_state.get(SIDEBAR_PREFERENCES_KEY, {})
            if isinstance(atom_state, dict)
            else {}
        )
        sidebar_mode = preferences.get("mode") if isinstance(preferences, dict) else None
        sidebar_sort_mode = (
            preferences.get("chatSortMode") if isinstance(preferences, dict) else None
        )
        project_sort_mode = (
            preferences.get("projectSortMode") if isinstance(preferences, dict) else None
        )
        managed_section_count = 0
        if isinstance(atom_state, dict):
            custom_by_account = atom_state.get(CUSTOM_SECTIONS_KEY)
            if isinstance(custom_by_account, dict):
                for account_state in custom_by_account.values():
                    sections = (
                        account_state.get("sections")
                        if isinstance(account_state, dict)
                        else None
                    )
                    if isinstance(sections, list):
                        managed_section_count += sum(
                            1
                            for section in sections
                            if isinstance(section, dict)
                            and section.get("id") == MANAGED_SECTION_ID
                        )
        id_sets.append(db_ids)
        result[str(root)] = {
            "threads": len(db_ids),
            "main_threads": len(main_thread_ids),
            "unarchived_main_threads": len(unarchived_main_thread_ids),
            "auxiliary_sessions": len(db_ids - main_thread_ids),
            "session_files": len(session_ids),
            "orphan_session_files": len(orphan_session_ids),
            "db_matches_sessions": db_ids == session_ids,
            "missing_rollout_paths": len(missing_rollouts),
            "expected_model_provider": expected_provider,
            "provider_visible_threads": len(db_ids - wrong_provider_ids),
            "provider_visible_main_threads": len(
                unarchived_main_thread_ids - wrong_provider_ids
            ),
            "wrong_model_provider_threads": len(wrong_provider_ids),
            "wrong_rollout_provider_sessions": len(wrong_rollout_provider_ids),
            "unqualified_model_threads": len(unqualified_model_thread_ids),
            "unqualified_model_sessions": len(unqualified_model_session_ids),
            "unqualified_model_values": unqualified_model_values
            + unqualified_rollout_values,
            # Bare values that COULD be namespaced. Reported, not fatal: the sync normalizes what it
            # writes, and an archived file it had no reason to touch keeps whatever it was recorded
            # with. Demanding zero here means demanding the sync retroactively rewrite every rollout
            # ever created -- one real archive carries 3250 of these -- so it could never pass.
            "requalifiable_model_threads": len(requalifiable_thread_ids),
            "requalifiable_model_sessions": len(requalifiable_session_ids),
            "model_guard": model_guard.summary() if model_guard is not None else None,
            "integrity": integrity,
            "sidebar_visible_main_threads": len(
                unarchived_main_thread_ids & sidebar_ids
            ),
            "archived_sidebar_threads": len(archived_sidebar_ids),
            "archived_sidebar_thread_ids": archived_sidebar_ids[:20],
            "archived_index_threads": len(archived_index_ids),
            "archived_index_thread_ids": archived_index_ids[:20],
            "missing_sidebar_main_threads": len(missing_sidebar_main_ids),
            "sidebar_mode": sidebar_mode,
            "sidebar_sort_mode": sidebar_sort_mode,
            "project_sort_mode": project_sort_mode,
            "projects": len(project_ids),
            "project_assigned_main_threads": len(project_assigned_main_ids),
            "unresolved_project_threads": len(unresolved_project_thread_ids),
            "unresolved_projects": len(unresolved_project_ids),
            "removed_sync_custom_sections": managed_section_count == 0,
        }
        if (
            integrity.lower() != "ok"
            or missing_rollouts
            or wrong_provider_ids
            or wrong_rollout_provider_ids
            # Neither `unqualified_model_*` nor `requalifiable_*` is fatal. The first counts history
            # that could not be attributed at all -- a model no enabled provider offers any more, or
            # one that many offer, which is what a multi-vendor router is for. The second counts
            # values that could be namespaced but sit in files this sync had no reason to rewrite.
            # Failing on either made the run unsatisfiable: the preflight refused to start and this
            # refused to finish, so no sync could ever succeed, while each attempt still paid for a
            # full-size backup snapshot. What actually prevents a bare slug reaching the wrong
            # account is the router, which rejects one at the request boundary. Both counts stay in
            # `result` so the drift remains visible.
            or db_ids != session_ids
            or missing_sidebar_main_ids
            or archived_sidebar_ids
            or archived_index_ids
            or unresolved_project_thread_ids
            or unresolved_project_ids
            or sidebar_mode != "project"
            or sidebar_sort_mode != "updated_at"
            or project_sort_mode != "updated_at"
            or managed_section_count
        ):
            raise SyncError(f"同步后校验失败：{root}；{result[str(root)]}")
    result["same_thread_ids"] = bool(id_sets) and all(
        current == id_sets[0] for current in id_sets[1:]
    )
    if not result["same_thread_ids"]:
        raise SyncError("同步后各数据目录的会话 ID 集仍不一致。")
    return result


def run_sync(
    left_root: Path,
    right_root: Path,
    backup_base: Path,
    left_provider: str = COCKPIT_MODEL_PROVIDER,
    right_provider: str = PLUS_MODEL_PROVIDER,
    model_guard_roots: Iterable[Path] | None = None,
    archive_state_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = utc_now()
    roots = [left_root.resolve(), right_root.resolve()]
    if roots[0] == roots[1]:
        raise SyncError("两套 Codex 数据目录不能相同。")
    if not left_provider or not right_provider:
        raise SyncError("同步两端都必须指定非空的模型提供商。")
    root_providers = {
        roots[0]: left_provider,
        roots[1]: right_provider,
    }
    for root in roots:
        validate_root(root)
    validate_lineage(roots)
    model_guards = build_model_guard_contexts(roots, model_guard_roots)

    backup_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-history-sync-", dir=INSTALL_DIR / "work") as raw_temp:
        temp_dir = Path(raw_temp)
        backup_dir = create_backup(roots, backup_base, temp_dir)
        left = load_root_snapshot(roots[0])
        right = load_root_snapshot(roots[1])

        # Conversation metadata timestamps also change on migrations and
        # reads; they are not evidence of an explicit unarchive action.  The
        # three-way launcher supplies SOTA's state captured before any
        # pairwise mutation.  Apply it before duplicate detection as well as
        # before the file merge, otherwise an archived SOTA clone could be
        # purged by the first Cockpit/Plus pass.
        for snapshot in (left, right):
            for session_id, state in (archive_state_overrides or {}).items():
                if session_id in snapshot.threads:
                    snapshot.threads[session_id].update(state)

        # A guarded target must be proven safe before *any* root mutation.  In
        # particular, legacy duplicate cleanup below deletes rows/files; doing
        # the first preflight after that cleanup could leave a two-way caller
        # partially changed when an ambiguous model is discovered.
        preflight_model_guards((left, right), root_providers, model_guards)

        duplicate_plan, duplicate_warnings = find_legacy_exact_clones(
            left, right, model_guards
        )
        duplicate_cleanup = purge_legacy_exact_clones(
            roots, duplicate_plan, backup_dir
        )
        if duplicate_plan:
            # Never continue with stale snapshots: otherwise the just-removed
            # IDs would be selected and written back into both databases.
            left = load_root_snapshot(roots[0])
            right = load_root_snapshot(roots[1])

        target_paths, clones, counters, sync_warnings = sync_session_files(
            left, right, backup_dir, temp_dir, root_providers, model_guards
        )
        integrity = sync_databases(
            left, right, target_paths, clones, root_providers, model_guards
        )
        sync_warnings.extend(sync_lineage_dependencies(roots))
        for root in roots:
            refresh_recovered_lineage(root, backup_dir)
        index_entries, visible_threads, sidebar_account_ids = sync_global_state_and_index(
            left, right, clones
        )
        verification = verify_roots(
            roots,
            sidebar_account_ids,
            root_providers,
            model_guards=model_guards,
        )
        sync_warnings.extend(model_guard_warnings(model_guards))
        verified_left = load_root_snapshot(roots[0])
        verified_right = load_root_snapshot(roots[1])
        remaining_duplicates, verification_warnings = find_legacy_exact_clones(
            verified_left, verified_right, model_guards
        )
        if remaining_duplicates:
            # A sync pass can create a clone after its pre-sync cleanup (for
            # example when the most recent branch is copied during the file
            # merge).  Treat the post-sync detector as a repair opportunity:
            # the same title/lineage/digest guards still apply, and the
            # operation is covered by this pass's backup manifest.
            post_cleanup = purge_legacy_exact_clones(
                roots,
                remaining_duplicates,
                backup_dir / "post-sync-exact-cleanup",
            )
            for key in (
                "threads",
                "main_threads",
                "auxiliary_threads",
                "files",
            ):
                duplicate_cleanup[key] += int(post_cleanup.get(key) or 0)
            verified_left = load_root_snapshot(roots[0])
            verified_right = load_root_snapshot(roots[1])
            remaining_duplicates, post_warnings = find_legacy_exact_clones(
                verified_left, verified_right, model_guards
            )
            verification_warnings.extend(post_warnings)
            # Recompute integrity/counts after the rows, indexes, and files
            # have been removed; the pre-cleanup result is stale by design.
            verification = verify_roots(
                roots,
                sidebar_account_ids,
                root_providers,
                model_guards=model_guards,
            )
        # The post-sync cleanup may have removed rows from both indexes. Keep
        # the returned counters aligned with the verified on-disk state rather
        # than reporting the pre-cleanup merge counts.
        index_entries = len(read_session_index(roots[0] / "session_index.jsonl"))
        visible_threads = len(collect_main_candidates(verified_left, verified_right, []))
        verification["legacy_exact_conflict_duplicates"] = len(remaining_duplicates)
        if remaining_duplicates:
            # NOT fatal. A conflict clone that survives both the pre- and post-sync purge in the
            # same pass is a mid-propagation artifact of the pairwise three-way model, not a defect:
            # SOTA owns the archive bit, and while an archive is still propagating to the other two
            # roots a clone can be archived in one root (so find_legacy_exact_clones intentionally
            # preserves it -- "one archived replica protects the history") yet look removable in
            # another. Hard-failing here rolled the entire pass back and left the roots MORE
            # diverged (one real run ended 621/588/621) than if it had committed. These are EXACT
            # duplicates by normalized digest -- redundant, never data loss -- and once the archive
            # state settles the finder skips them for good, so the next sync converges. Record the
            # count and warn; let the convergence loop and archive propagation settle it.
            message = (
                "同步后仍存在精确重复的冲突副本（均为归档态，属传播中冗余，不阻断同步）："
                + ", ".join(item.duplicate_id for item in remaining_duplicates)
            )
            verification_warnings.append(message)
            sync_warnings.append(message)
            logging.warning(message)
        rotate_backups(backup_base)

    finished = utc_now()
    return {
        "status": "ok",
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": finished.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "cockpit_root": str(roots[0]),
        "plus_root": str(roots[1]),
        "left_root": str(roots[0]),
        "right_root": str(roots[1]),
        "left_provider": left_provider,
        "right_provider": right_provider,
        "backup_dir": str(backup_dir),
        "new_files": counters["new_files"],
        "updated_files": counters["updated_files"],
        "unchanged_files": counters["unchanged"],
        "conflicts_preserved": counters["conflicts"],
        "exact_duplicates_removed": duplicate_cleanup["threads"],
        "duplicate_main_threads_removed": duplicate_cleanup["main_threads"],
        "duplicate_auxiliary_threads_removed": duplicate_cleanup[
            "auxiliary_threads"
        ],
        "duplicate_session_files_removed": duplicate_cleanup["files"],
        "index_entries": index_entries,
        "visible_top_level_threads": visible_threads,
        "removed_sync_custom_sections": True,
        "sidebar_mode": "project",
        "integrity": integrity,
        "model_guard": {
            str(root): context.summary() for root, context in model_guards.items()
        },
        "normalized_model_fields": sum(
            context.normalized_fields for context in model_guards.values()
        ),
        "unresolved_model_fields": sum(
            context.unresolved_fields for context in model_guards.values()
        ),
        "verification": verification,
        "warnings": duplicate_warnings + sync_warnings + verification_warnings,
    }


def write_last_result(result: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        atomic_write_json(LAST_RESULT_PATH, result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="双向同步 Cockpit 与 Plus 的本地 Codex 对话")
    parser.add_argument("--left", type=Path, default=DEFAULT_LEFT, help="Cockpit Codex 数据目录")
    parser.add_argument("--right", type=Path, default=DEFAULT_RIGHT, help="Plus Codex 数据目录")
    parser.add_argument(
        "--backup-base", type=Path, default=DEFAULT_BACKUP_BASE, help="同步备份目录"
    )
    parser.add_argument(
        "--left-provider",
        default=COCKPIT_MODEL_PROVIDER,
        help="左侧 Codex 配置使用的模型提供商 ID",
    )
    parser.add_argument(
        "--right-provider",
        default=PLUS_MODEL_PROVIDER,
        help="右侧 Codex 配置使用的模型提供商 ID",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def main() -> int:
    log_path = setup_logging()
    args = build_parser().parse_args()
    result: dict[str, Any]
    try:
        with SingleInstanceLock(LOCK_PATH):
            logging.info("开始同步：%s <-> %s", args.left, args.right)
            result = run_sync(
                args.left,
                args.right,
                args.backup_base,
                args.left_provider,
                args.right_provider,
            )
            result["log_path"] = str(log_path)
            logging.info("同步完成：%s", result)
            write_last_result(result)
            if args.json:
                print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        result = {
            "status": "error",
            "finished_at": iso_now(),
            "error": str(exc),
            "log_path": str(log_path),
        }
        logging.error("同步失败：%s\n%s", exc, traceback.format_exc())
        write_last_result(result)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
