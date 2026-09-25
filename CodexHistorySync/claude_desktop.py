#!/usr/bin/env python3
"""Claude Desktop 3P config-library management for the codex-sota manager.

Claude Desktop keeps its third-party inference settings as a library of named profiles under
%LOCALAPPDATA%\\Claude-3p\\configLibrary, with _meta.json naming the applied one. Other tools
(cc-switch among them) own their own entries there, so this module only ever creates and
updates its own profile and flips appliedId — never edits or deletes a neighbour's entry.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from typing import Any
from uuid import uuid4
import urllib.parse

from sota_registry import CLAUDE, published_slug

CLAUDE_3P_ROOT = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
) / "Claude-3p"
CONFIG_LIBRARY = CLAUDE_3P_ROOT / "configLibrary"
META_PATH = CONFIG_LIBRARY / "_meta.json"
# Keep Claude's recovery material in the Claude workspace.  The previous location under
# .codex-sota made an otherwise isolated Claude operation look like a Codex configuration edit.
BACKUP_ROOT = CLAUDE.root / "backups" / "claude-3p"
LIBRARY_LOCK_PATH = CLAUDE.root / "claude-library.lock"
# appliedId in _meta.json is a single global slot that every 3P manager on the machine shares
# (cc-switch, APIKEY.FUN, this one).  There is no way to own it privately, so ownership is
# tracked here instead: who we displaced, so the slot can be handed back on exit.
SLOT_CLAIM_PATH = CLAUDE.root / "claude-slot-claim.json"
_DEFAULT_CONFIG_LIBRARY = CONFIG_LIBRARY
_DEFAULT_LIBRARY_LOCK_PATH = LIBRARY_LOCK_PATH
_DEFAULT_SLOT_CLAIM_PATH = SLOT_CLAIM_PATH

# A fixed, standards-compliant UUID so Claude accepts the complete config library.  The old
# value contained non-hexadecimal characters and could make Claude reject every profile.
SOTA_ENTRY_ID = "c0de507a-0000-4000-8000-c1a0de5074a1"
LEGACY_ENTRY_IDS = ("c0dexs0t-a000-4000-8000-c1aude5074a1",)
SOTA_ENTRY_NAME = "Codex SOTA"
# Derived from the Claude workspace so the two products can never share a router port.
DEFAULT_GATEWAY_URL = f"http://127.0.0.1:{CLAUDE.router_port}"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class ConcurrentClaudeConfigUpdate(RuntimeError):
    """The config library changed after this operation read it."""


def _utc_stamp() -> str:
    """Microsecond precision on purpose: two writes in the same second must not share a
    backup directory, or the earlier snapshot is silently overwritten by the later one."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def entry_path(entry_id: str) -> Path:
    entry_id = str(entry_id or "")
    first_component = entry_id.split(".", 1)[0].lower()
    if not ENTRY_ID_RE.fullmatch(entry_id) or first_component in WINDOWS_DEVICE_NAMES:
        raise ValueError(f"Invalid config library entry id: {entry_id!r}")
    candidate = CONFIG_LIBRARY / f"{entry_id}.json"
    try:
        root = CONFIG_LIBRARY.resolve()
        resolved = candidate.resolve()
    except OSError as error:
        raise ValueError(f"Invalid config library entry id: {entry_id!r}") from error
    if resolved.parent != root:
        raise ValueError(f"Invalid config library entry id: {entry_id!r}")
    return candidate


def _safe_entry_path(entry_id: str) -> Path | None:
    """Return an entry path without allowing a malformed metadata id to break the UI."""
    try:
        return entry_path(entry_id)
    except ValueError:
        return None


def _library_lock_path() -> Path:
    # Tests and portable installs replace CONFIG_LIBRARY/CLAUDE.root at runtime.  Never let an
    # isolated library test acquire the production lock under the user's real Claude workspace.
    if LIBRARY_LOCK_PATH != _DEFAULT_LIBRARY_LOCK_PATH:
        return LIBRARY_LOCK_PATH
    if CONFIG_LIBRARY != _DEFAULT_CONFIG_LIBRARY:
        return CONFIG_LIBRARY.parent / "claude-library.lock"
    return Path(CLAUDE.root) / "claude-library.lock"


@contextmanager
def _library_lock(timeout_seconds: float = 20.0):
    """Serialize profile/index edits made by the manager and by a second manager instance."""
    lock_path = _library_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = __import__("time").monotonic() + timeout_seconds
        locked = False
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - production is Windows, but keeps temp tests portable
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, IOError):
                if __import__("time").monotonic() >= deadline:
                    raise TimeoutError("Claude 配置库正在被另一个配置操作占用")
                __import__("time").sleep(0.1)
        try:
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - see branch above
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _read_meta_with_revision() -> tuple[dict[str, Any], bytes | None]:
    """Read and validate the external index while retaining an optimistic revision token."""
    try:
        raw = META_PATH.read_bytes()
    except FileNotFoundError:
        return {"appliedId": None, "entries": []}, None
    except OSError as error:
        raise RuntimeError(f"读不了 Claude Desktop 的配置索引 {META_PATH}：{error}") from error
    try:
        meta = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise RuntimeError(f"读不了 Claude Desktop 的配置索引 {META_PATH}：{error}") from error
    if not isinstance(meta, dict):
        raise RuntimeError(f"{META_PATH} 不是一个 JSON 对象")
    meta.setdefault("appliedId", None)
    if meta["appliedId"] is not None and not isinstance(meta["appliedId"], str):
        raise RuntimeError(f"{META_PATH} 的 appliedId 不是字符串或 null")
    entries = meta.get("entries")
    if entries is None and "entries" not in meta:
        entries = []
    if not isinstance(entries, list):
        raise RuntimeError(f"{META_PATH} 的 entries 不是 JSON 数组；为避免覆盖外部档案，已停止写入")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"{META_PATH} 的 entries[{index}] 不是 JSON 对象；为避免覆盖外部档案，已停止写入"
            )
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or _safe_entry_path(entry_id) is None:
            raise RuntimeError(
                f"{META_PATH} 的 entries[{index}].id 不安全；为避免路径穿越，已停止写入"
            )
    meta["entries"] = entries
    return meta, raw


def read_meta() -> dict[str, Any]:
    """The library index, or an empty skeleton when Claude Desktop has never written one."""
    return _read_meta_with_revision()[0]


def _assert_meta_revision(expected: bytes | None) -> None:
    try:
        current = META_PATH.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError as error:
        raise RuntimeError(f"无法复核 Claude Desktop 配置索引：{error}") from error
    if current != expected:
        raise ConcurrentClaudeConfigUpdate(
            "Claude Desktop 配置库刚被其他程序修改。为避免覆盖 cc-switch 的改动，本次操作已取消；请重试。"
        )


def library_status() -> dict[str, Any]:
    """What profiles exist, which is applied, and whether ours is among them."""
    with _library_lock():
        root_migration = _migrate_legacy_3p_root()
        if not CONFIG_LIBRARY.exists():
            return {
                "available": False,
                "reason": f"没有 {CONFIG_LIBRARY}，Claude Desktop 可能还没进过 3P 模式",
                "root_migration": root_migration,
            }
        entry_migration = _migrate_legacy_locked()
        meta = read_meta()
    applied = meta.get("appliedId")
    entries = []
    for entry in meta["entries"]:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        path = _safe_entry_path(entry_id)
        entries.append(
            {
                "id": entry_id,
                "name": str(entry.get("name") or entry_id),
                "applied": entry_id == applied,
                "mine": entry_id == SOTA_ENTRY_ID,
                "exists": bool(path and path.exists()),
                "valid_id": bool(UUID_RE.fullmatch(entry_id)),
            }
        )
    return {
        "available": True,
        "applied_id": applied,
        "applied_name": next((e["name"] for e in entries if e["applied"]), None),
        "entries": entries,
        "mine_present": any(e["mine"] for e in entries),
        "invalid_ids": [e["id"] for e in entries if not e["valid_id"]],
        "root_migration": root_migration,
        "entry_migration": entry_migration,
    }


def _valid_gateway_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("网关地址必须是完整的 http:// 或 https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("网关地址不能包含账号、密码、查询参数或片段")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程网关必须使用 HTTPS；只有本机回环地址允许 HTTP")
    return text


def _legacy_3p_root() -> Path | None:
    """Pre-MSIX Windows builds could leave the 3P tree under roaming AppData."""
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return None
    candidate = Path(roaming) / "Claude-3p"
    try:
        if candidate.resolve() == CLAUDE_3P_ROOT.resolve():
            return None
    except OSError:
        pass
    return candidate


def _migrate_legacy_3p_root() -> dict[str, Any]:
    """Move an old roaming 3P tree to the canonical LocalAppData location.

    Claude Desktop 1.40609 performs this same migration before choosing its user-data root.
    Doing it before the manager creates configLibrary avoids accidentally creating an empty
    canonical root that would make Claude skip its own migration.
    """
    legacy = _legacy_3p_root()
    if CLAUDE_3P_ROOT.exists() or legacy is None or not legacy.exists():
        return {"status": "not-needed", "source": str(legacy) if legacy else None}
    CLAUDE_3P_ROOT.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(legacy, CLAUDE_3P_ROOT)
        return {"status": "moved", "source": str(legacy), "target": str(CLAUDE_3P_ROOT)}
    except OSError:
        # Cross-volume moves and files held open by an old build can fail. Copy through a
        # sibling staging directory and leave the legacy tree intact as recovery material.
        staging = CLAUDE_3P_ROOT.parent / f".{CLAUDE_3P_ROOT.name}.migration-{uuid4().hex}"
        try:
            shutil.copytree(legacy, staging)
            os.replace(staging, CLAUDE_3P_ROOT)
        except Exception as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(
                f"Claude 旧版 3P 目录迁移失败：{legacy} -> {CLAUDE_3P_ROOT}：{error}"
            ) from error
        return {"status": "copied", "source": str(legacy), "target": str(CLAUDE_3P_ROOT)}


# How Claude Desktop decides whether a model gets a thinking control, transcribed from the
# app's own bundle (1.40609.0.0): it lowercases the id, strips a Bedrock ARN and a
# `<vendor>.anthropic.` prefix plus a few version/date suffixes, then looks the result up in a
# hardcoded table.  A 3P profile has no field that can carry this -- the per-model schema is
# name/labelOverride/supports1m/prefer1m/anthropicFamilyTier/isFamilyDefault -- and the app's
# richer `hybridModelSelector` map is only built for its own first-party bootstrap.  So the
# published slug is the only lever we have, which is why messages-only providers use the
# `<vendor>.anthropic.` prefix form (see sota_registry.MODEL_PREFIX_PATTERN).
_BEDROCK_ARN_RE = re.compile(r"^arn:aws[a-z-]*:bedrock:[^/]+/")
_VENDOR_PREFIX_RE = re.compile(r"^(?:[a-z][a-z0-9-]*\.)?anthropic\.")
_FIRST_PARTY_RE = re.compile(r"^claude-(?:[a-z]+-)?\d")
_BRACKET_SUFFIX_RE = re.compile(r"\[[^\]]+\]$")
_VERSION_SUFFIX_RE = re.compile(r"-v\d+(?::\d+)?$")
_AT_DATE_SUFFIX_RE = re.compile(r"@\d{8}$")
_DASH_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
# The table itself: canonical id -> effort levels offered.  An empty tuple means the app has a
# record but only an extended-thinking on/off switch for it; a missing key means no thinking UI
# at all.  Values are Claude Desktop's, not ours -- a future version can change them.
CLAUDE_THINKING_MODELS: dict[str, tuple[str, ...]] = {
    "claude-haiku-4-5": (),
    "claude-sonnet-4-5": (),
    "claude-sonnet-4-6": ("low", "medium", "high", "max"),
    "claude-sonnet-5": ("low", "medium", "high", "xhigh", "max"),
    "claude-opus-4-6": ("low", "medium", "high", "max"),
    "claude-opus-4-7": ("low", "medium", "high", "xhigh", "max"),
    "claude-opus-4-8": ("low", "medium", "high", "xhigh", "max"),
    "claude-opus-5": ("low", "medium", "high", "xhigh", "max"),
}
_FABLE_FAMILY_RE = re.compile(r"^(?:claude-)?(?:fable|mythos)(?:-|$)")
_FABLE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def canonical_model_id(model_id: str) -> str:
    """Reduce a published slug the way Claude Desktop does before its capability lookup."""
    lowered = str(model_id).lower()
    stripped = _VENDOR_PREFIX_RE.sub("", _BEDROCK_ARN_RE.sub("", lowered))
    # A version suffix is only dropped from an id the app already believes is an Anthropic
    # one -- either a vendor prefix came off, or it reads like a first-party model name.
    anthropic_shaped = stripped != lowered or bool(_FIRST_PARTY_RE.match(stripped))
    result = _BRACKET_SUFFIX_RE.sub("", stripped)
    if anthropic_shaped:
        result = _VERSION_SUFFIX_RE.sub("", result)
    return _DASH_DATE_SUFFIX_RE.sub("", _AT_DATE_SUFFIX_RE.sub("", result))


def thinking_effort_levels(model_id: str) -> tuple[str, ...] | None:
    """The effort levels Claude Desktop will offer for a slug, or None for no thinking UI.

    An empty tuple is the third case: the app knows the model but only offers the extended
    thinking on/off switch.  Unlocking the picker is not a promise that the gateway honours
    the choice -- it only decides what the app is willing to show.
    """
    canonical = canonical_model_id(model_id)
    if canonical in CLAUDE_THINKING_MODELS:
        return CLAUDE_THINKING_MODELS[canonical]
    if _FABLE_FAMILY_RE.match(canonical):
        return _FABLE_EFFORTS
    return None


def thinking_summary(model_id: str) -> str:
    """One short label for what thinking UI a published slug will get, for reports and dialogs."""
    levels = thinking_effort_levels(model_id)
    if levels is None:
        return "无思考控件"
    if not levels:
        return "仅扩展思考开关"
    return "思考档 " + "/".join(levels)


# Which published slugs are offered with a 1M-token context window.  Unlike the thinking
# slider, `supports1m` is not gated by a table inside the app -- the app's own settings help
# calls it a claim about the deployment ("Set only if the deployment accepts 1M-token context
# for it"), and honouring it only *adds* a second picker entry (`<slug>[1m]`) beside the
# standard one.  So this table is our assertion about what the relays behind the router will
# take, not a transcript of Claude Desktop's.  Keyed on the same canonical id the thinking
# lookup uses, so a vendor prefix or a date suffix never has to be repeated here.
CLAUDE_1M_CONTEXT_MODELS: frozenset[str] = frozenset(
    {
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    }
)


def supports_1m_context(model_id: str) -> bool:
    """Whether a published slug should also be offered as a 1M-context variant.

    Haiku and anything unrecognised are left out.  A wrong claim here does not fail at write
    time or at launch -- it fails much later, by letting a conversation grow past what the
    upstream actually accepts and turning the next turn into an opaque 400.
    """
    canonical = canonical_model_id(model_id)
    if canonical in CLAUDE_1M_CONTEXT_MODELS:
        return True
    return bool(_FABLE_FAMILY_RE.match(canonical))


def build_inference_models(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn messages-capable registry models into Claude Desktop's inferenceModels shape.

    `name` is the slug the router dispatches on -- `publish_as` when a model sets one, so a
    vendor id like `claude-opus-5-thinking` can still reach the app's thinking-capability
    table. `labelOverride` is what the model picker shows, and it always carries the vendor
    and the real upstream id, so an overridden slug never hides which model is being called.

    `supports1m` is set per CLAUDE_1M_CONTEXT_MODELS.  It is additive: the app keeps the plain
    entry and adds a `<slug>[1m]` one beside it, so a relay that turns out to refuse 1M costs
    the user one unused picker row rather than a broken default.
    """
    models: list[dict[str, Any]] = []
    for provider in registry.get("providers") or []:
        if not provider.get("enabled"):
            continue
        if "messages" not in provider.get("protocols", ["responses"]):
            continue
        for model in provider.get("models") or []:
            if not model.get("enabled"):
                continue
            slug = published_slug(provider, model)
            entry: dict[str, Any] = {
                "name": slug,
                "labelOverride": f"{provider.get('name') or provider['id']} · {model['id']}",
            }
            if supports_1m_context(slug):
                entry["supports1m"] = True
            models.append(entry)
    return models


def _backup(paths: list[Path]) -> Path:
    """Copy the library index and our own profile aside before touching either."""
    target = BACKUP_ROOT / _utc_stamp()
    suffix = 1
    while target.exists():
        target = BACKUP_ROOT / f"{_utc_stamp()}-{suffix}"
        suffix += 1
    target.mkdir(parents=True, exist_ok=False)
    used_names: dict[str, int] = {}
    manifest: dict[str, str] = {}
    for path in paths:
        if path.exists():
            name = path.name
            count = used_names.get(name, 0) + 1
            used_names[name] = count
            if count > 1:
                stem = path.stem
                suffix = path.suffix
                name = f"{stem}-{count}{suffix}"
            destination = target / name
            shutil.copy2(path, destination)
            manifest[name] = str(path)
    if manifest:
        _atomic_write_json(target / "_manifest.json", manifest)
    return target


def _atomic_write_json(path: Path, payload: Any) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write through a unique file and replace it only after the complete payload is flushed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _restore(snapshot: dict[Path, bytes | None]) -> None:
    errors: list[str] = []
    for path, value in snapshot.items():
        try:
            if value is None:
                path.unlink()
            else:
                _atomic_write_bytes(path, value)
        except FileNotFoundError:
            if value is not None:
                errors.append(f"{path}: file disappeared during restore")
        except OSError as error:
            errors.append(f"{path}: {error}")
    if errors:
        raise RuntimeError("Claude 配置回滚不完整：" + "; ".join(errors))


def _legacy_ids(meta: dict[str, Any]) -> list[str]:
    ids = list(LEGACY_ENTRY_IDS)
    for entry in meta.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        if (
            entry_id
            and entry_id != SOTA_ENTRY_ID
            and entry.get("name") == SOTA_ENTRY_NAME
            and not UUID_RE.fullmatch(entry_id)
        ):
            ids.append(entry_id)
    return list(dict.fromkeys(ids))


def _legacy_candidates(meta: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for old_id in _legacy_ids(meta):
        old_path = _safe_entry_path(old_id)
        indexed = any(
            str(entry.get("id")) == old_id
            for entry in meta.get("entries") or []
            if isinstance(entry, dict)
        )
        if (old_path is not None and old_path.exists()) or indexed:
            candidates.append(old_id)
    return candidates


def _migration_paths(meta: dict[str, Any]) -> list[Path]:
    paths = [META_PATH, entry_path(SOTA_ENTRY_ID)]
    for old_id in _legacy_candidates(meta):
        old_path = _safe_entry_path(old_id)
        if old_path is not None:
            paths.append(old_path)
    return list(dict.fromkeys(paths))


def _migrate_legacy_locked(*, manage_recovery: bool = True) -> dict[str, Any]:
    """Move the pre-UUID SOTA profile out of Claude's library without touching other entries."""
    if not CONFIG_LIBRARY.exists():
        return {"status": "not-needed", "migrated": [], "backup": None}
    meta, meta_revision = _read_meta_with_revision()
    candidates = _legacy_candidates(meta)
    if not candidates:
        return {"status": "not-needed", "migrated": [], "backup": None}

    new_path = entry_path(SOTA_ENTRY_ID)
    paths = _migration_paths(meta)
    old_paths = []
    for old_id in candidates:
        old_path = _safe_entry_path(old_id)
        if old_path is not None:
            old_paths.append((old_id, old_path))
    snapshot = _snapshot(paths) if manage_recovery else {}
    backup = _backup(paths) if manage_recovery else None
    try:
        source_id, source_path = next(
            ((old_id, old_path) for old_id, old_path in old_paths if old_path.exists()),
            (None, None),
        )
        if source_path is None:
            raise RuntimeError("旧版 Codex SOTA 档案只在索引中存在，找不到档案文件，已保留原状")
        # The fixed id is reserved for this profile.  If a partial previous repair left a file
        # there, the backup above makes it recoverable and the old active profile wins.
        _atomic_write_bytes(new_path, source_path.read_bytes())

        rewritten: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in meta.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            item = deepcopy(entry)
            item_id = str(item.get("id") or "")
            if item_id in candidates:
                item["id"] = SOTA_ENTRY_ID
            item_id = str(item.get("id") or "")
            if item_id in seen:
                continue
            seen.add(item_id)
            rewritten.append(item)
        if SOTA_ENTRY_ID not in seen:
            rewritten.append({"id": SOTA_ENTRY_ID, "name": SOTA_ENTRY_NAME})
        meta["entries"] = rewritten
        if str(meta.get("appliedId") or "") in candidates:
            meta["appliedId"] = SOTA_ENTRY_ID
        _assert_meta_revision(meta_revision)
        _atomic_write_json(META_PATH, meta)

        # Leaving the malformed JSON beside the repaired profile makes Claude reject the
        # library on some versions.  It is already byte-for-byte recoverable in `backup`.
        for _old_id, old_path in old_paths:
            if old_path.exists() and old_path != new_path:
                old_path.unlink()
        return {
            "status": "migrated",
            "migrated": [source_id] if source_id else candidates,
            "entry_id": SOTA_ENTRY_ID,
            "backup": str(backup) if backup is not None else None,
        }
    except ConcurrentClaudeConfigUpdate:
        if manage_recovery:
            _restore({path: value for path, value in snapshot.items() if path != META_PATH})
        raise
    except Exception:
        if manage_recovery:
            _restore(snapshot)
        raise


def migrate_legacy_entry() -> dict[str, Any]:
    """Public, idempotent migration used before status checks and launches."""
    with _library_lock():
        return _migrate_legacy_locked()


def ensure_deployment_mode() -> dict[str, Any]:
    """Ensure Claude reads the canonical 3P tree, without touching any 1P profile."""
    with _library_lock():
        migration = _migrate_legacy_3p_root()
        paths = [CLAUDE_3P_ROOT / "claude_desktop_config.json"]
        values: dict[Path, dict[str, Any]] = {}
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            except (OSError, ValueError) as error:
                raise RuntimeError(f"Claude 配置文件无法解析：{path}：{error}") from error
            if not isinstance(value, dict):
                raise RuntimeError(f"Claude 配置文件不是 JSON 对象：{path}")
            values[path] = value

        changing = [path for path, value in values.items() if value.get("deploymentMode") != "3p"]
        if not changing:
            return {
                "status": "ready",
                "changed": [],
                "backup": None,
                "paths": [str(path) for path in paths],
                "migration": migration,
            }

        snapshot = _snapshot(changing)
        backup = str(_backup(changing))
        changed: list[str] = []
        try:
            for path in changing:
                value = values[path]
                value["deploymentMode"] = "3p"
                _atomic_write_json(path, value)
                changed.append(str(path))
        except Exception:
            _restore(snapshot)
            raise
    return {
        "status": "ready",
        "changed": changed,
        "backup": backup,
        "paths": [str(path) for path in paths],
        "migration": migration,
    }


def write_profile(
    models: list[dict[str, Any]],
    gateway_url: str = DEFAULT_GATEWAY_URL,
    api_key: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Create or update our profile, and optionally make it the applied one.

    Only our own entry file and the index are written; every other profile is left byte for
    byte alone so cc-switch can flip back to its own entry whenever you use it.

    apply defaults to False on purpose.  appliedId is one global slot shared with every other
    3P manager, so writing a profile used to silently hijack whatever cc-switch had applied --
    the user would launch Claude from cc-switch and get codex-sota's gateway.  The slot is now
    taken only by claim_slot() as part of an actual codex-sota launch, and handed back after.
    """
    if not models:
        raise ValueError("没有任何启用了 messages 协议的模型，写出去的模型选择器会是空的")
    clean_models: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("Claude inferenceModels 必须是 JSON 对象列表")
        name = str(model.get("name") or "").strip()
        if not name or any(ch in name for ch in "\r\n\t"):
            raise ValueError("Claude 模型名不能为空或包含换行")
        if name in seen_names:
            continue
        seen_names.add(name)
        label = str(model.get("labelOverride") or name).strip()
        entry: dict[str, Any] = {"name": name, "labelOverride": label}
        # Capability flags are passed through, not invented here.  The profile is the only place
        # a 3P deployment can assert them -- once inferenceModels is non-empty the app takes
        # capabilities from this list alone and uses GET /v1/models only for display names --
        # so build_inference_models decides and this loop just refuses malformed values.
        if model.get("supports1m") is True:
            entry["supports1m"] = True
            # prefer1m is a sub-toggle of supports1m in the app's own settings UI and is
            # ignored without it, so it never travels alone.
            if model.get("prefer1m") is True:
                entry["prefer1m"] = True
        if model.get("isFamilyDefault") is True:
            entry["isFamilyDefault"] = True
        tier = str(model.get("anthropicFamilyTier") or "").strip()
        if tier and not any(ch in tier for ch in "\r\n\t"):
            entry["anthropicFamilyTier"] = tier
        clean_models.append(entry)
    url = _valid_gateway_url(gateway_url)
    deployment = ensure_deployment_mode()

    with _library_lock():
        CONFIG_LIBRARY.mkdir(parents=True, exist_ok=True)
        mine = entry_path(SOTA_ENTRY_ID)
        initial_meta = read_meta()
        transaction_paths = _migration_paths(initial_meta)
        snapshot = _snapshot(transaction_paths)
        conflict_snapshot = snapshot
        backup = _backup(transaction_paths)
        try:
            migration = _migrate_legacy_locked(manage_recovery=False)
            if migration.get("status") == "migrated":
                migration["backup"] = str(backup)
            meta, meta_revision = _read_meta_with_revision()
            conflict_snapshot = _snapshot(transaction_paths)
            previous_applied = meta.get("appliedId")
            existing: dict[str, Any] = {}
            if mine.exists():
                try:
                    parsed = json.loads(mine.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as error:
                    raise RuntimeError(f"Codex SOTA Claude 档案无法解析：{mine}：{error}") from error
                if not isinstance(parsed, dict):
                    raise RuntimeError(f"Codex SOTA Claude 档案不是 JSON 对象：{mine}")
                existing = parsed
            token = api_key.strip() if isinstance(api_key, str) and api_key.strip() else existing.get("inferenceGatewayApiKey")
            if not isinstance(token, str) or not token.strip() or any(ch in token for ch in "\r\n\t"):
                token = secrets.token_hex(18)

            profile = deepcopy(existing)
            profile.update(
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": url,
                    "inferenceGatewayApiKey": token,
                    "inferenceGatewayAuthScheme": "bearer",
                    "inferenceModels": clean_models,
                    "disableDeploymentModeChooser": True,
                }
            )
            profile.setdefault("coworkEgressAllowedHosts", ["*"])
            _atomic_write_json(mine, profile)

            entries = [deepcopy(e) for e in meta["entries"] if isinstance(e, dict)]
            replaced = False
            for entry in entries:
                if str(entry.get("id")) == SOTA_ENTRY_ID:
                    entry["name"] = SOTA_ENTRY_NAME
                    replaced = True
            if not replaced:
                entries.append({"id": SOTA_ENTRY_ID, "name": SOTA_ENTRY_NAME})
            meta["entries"] = entries
            if apply:
                meta["appliedId"] = SOTA_ENTRY_ID
            _assert_meta_revision(meta_revision)
            _atomic_write_json(META_PATH, meta)
        except ConcurrentClaudeConfigUpdate:
            _restore(
                {
                    path: value
                    for path, value in conflict_snapshot.items()
                    if path != META_PATH
                }
            )
            raise
        except Exception:
            _restore(snapshot)
            raise
        if apply:
            # Taking the slot here also records who to give it back to, so an explicit
            # apply=True can still be undone by release_slot().
            _record_claim_locked(meta, previous_applied)
    return {
        "status": "ready",
        "entry_id": SOTA_ENTRY_ID,
        "entry_path": str(mine),
        "gateway_url": url,
        "models": len(clean_models),
        "applied": bool(apply),
        "previous_applied": previous_applied,
        "backup": str(backup),
        "migration": migration,
        "deployment": deployment,
    }


def _entry_name(meta: dict[str, Any], entry_id: Any) -> str:
    wanted = str(entry_id or "")
    if not wanted:
        return ""
    for entry in meta.get("entries", []):
        if isinstance(entry, dict) and str(entry.get("id")) == wanted:
            return str(entry.get("name") or wanted)
    return wanted


def _slot_claim_path() -> Path:
    # Mirrors _library_lock_path: an isolated test library must never read or write the claim
    # that governs the user's real Claude Desktop.
    if SLOT_CLAIM_PATH != _DEFAULT_SLOT_CLAIM_PATH:
        return SLOT_CLAIM_PATH
    if CONFIG_LIBRARY != _DEFAULT_CONFIG_LIBRARY:
        return CONFIG_LIBRARY.parent / "claude-slot-claim.json"
    return Path(CLAUDE.root) / "claude-slot-claim.json"


def read_slot_claim() -> dict[str, Any]:
    """The recorded claim, or {} when codex-sota does not believe it holds the slot."""
    try:
        parsed = json.loads(_slot_claim_path().read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clear_slot_claim() -> None:
    try:
        _slot_claim_path().unlink()
    except (FileNotFoundError, OSError):
        pass


def _record_claim_locked(
    meta: dict[str, Any], previous_applied: Any, *, pids: Any = None
) -> dict[str, Any]:
    """Note that codex-sota now holds the slot, and who has to get it back.

    Re-claiming while already the applied profile must not record *ourselves* as the neighbour
    to restore -- that would strand cc-switch permanently -- so an existing claim's
    previous_applied_id wins whenever the current value is ours or unknown.
    """
    claim = read_slot_claim()
    previous_id = str(previous_applied or "")
    if not previous_id or previous_id == SOTA_ENTRY_ID:
        previous_id = str(claim.get("previous_applied_id") or "")
        previous_name = str(claim.get("previous_applied_name") or "")
    else:
        previous_name = _entry_name(meta, previous_id)
    if previous_id == SOTA_ENTRY_ID:
        previous_id, previous_name = "", ""
    try:
        claimed_pids = sorted({int(pid) for pid in (pids or [])})
    except (TypeError, ValueError):
        claimed_pids = []
    payload = {
        "version": 1,
        "owner": "codex-sota",
        "entry_id": SOTA_ENTRY_ID,
        "previous_applied_id": previous_id,
        "previous_applied_name": previous_name,
        "claimed_at": _utc_stamp(),
        "claimed_pids": claimed_pids,
    }
    _atomic_write_json(_slot_claim_path(), payload)
    return payload


def claim_slot(*, pids: Any = None) -> dict[str, Any]:
    """Take the shared slot for a codex-sota launch, remembering the neighbour displaced.

    Called from the launch path only.  Writing a profile no longer claims the slot, so
    cc-switch keeps whatever it applied until the user actually starts Claude from here.
    """
    deployment = ensure_deployment_mode()
    with _library_lock():
        meta = read_meta()
        current = str(meta.get("appliedId") or "")
        if current == SOTA_ENTRY_ID:
            claim = _record_claim_locked(meta, "", pids=pids)
            return {
                "status": "already-owned",
                "applied_id": current,
                "applied_name": _entry_name(meta, current),
                "displaced_id": claim["previous_applied_id"],
                "displaced_name": claim["previous_applied_name"],
                "claim": claim,
                "deployment": deployment,
            }
        result = _set_applied_locked(SOTA_ENTRY_ID)
        claim = _record_claim_locked(result.pop("meta"), result["previous_applied"], pids=pids)
    return {
        "status": "claimed",
        "applied_id": result["applied_id"],
        "applied_name": result["applied_name"],
        "displaced_id": claim["previous_applied_id"],
        "displaced_name": claim["previous_applied_name"],
        "claim": claim,
        "backup": result["backup"],
        "deployment": deployment,
    }


def release_slot(*, force: bool = False) -> dict[str, Any]:
    """Hand the shared slot back to whoever held it before codex-sota launched Claude.

    force=True skips the "are we still the applied profile" test, for the explicit
    "give it back to cc-switch" button.  Without it, a slot somebody else has since taken is
    left alone: stealing it back would be the exact bug this protocol exists to prevent.
    """
    claim = read_slot_claim()
    if not claim:
        return {"status": "no-claim"}
    target = str(claim.get("previous_applied_id") or "")
    if not target or target == SOTA_ENTRY_ID:
        _clear_slot_claim()
        return {"status": "no-previous", "claim": claim}
    deployment = ensure_deployment_mode()
    with _library_lock():
        meta = read_meta()
        current = str(meta.get("appliedId") or "")
        if current != SOTA_ENTRY_ID and not force:
            _clear_slot_claim()
            return {
                "status": "not-owner",
                "applied_id": current,
                "applied_name": _entry_name(meta, current),
                "claim": claim,
                "deployment": deployment,
            }
        known = {str(e.get("id")) for e in meta.get("entries", []) if isinstance(e, dict)}
        if target not in known:
            _clear_slot_claim()
            return {
                "status": "previous-gone",
                "applied_id": current,
                "target_id": target,
                "claim": claim,
                "deployment": deployment,
            }
        result = _set_applied_locked(target)
        result.pop("meta", None)
        _clear_slot_claim()
    return {
        "status": "released",
        "applied_id": result["applied_id"],
        "applied_name": result["applied_name"],
        "claim": claim,
        "backup": result["backup"],
        "deployment": deployment,
    }


def reconcile_slot(*, claude_running: bool) -> dict[str, Any]:
    """Catch-all for a release that never happened (manager killed, watcher lost, reboot).

    Only releases when Claude Desktop is not running: flipping appliedId under a live Claude
    is pointless at best and confusing at worst, since the profile is read at startup.
    """
    claim = read_slot_claim()
    if not claim:
        return {"status": "no-claim"}
    if claude_running:
        return {"status": "claude-running", "claim": claim}
    return release_slot()


def slot_state(*, claude_running: bool | None = None) -> dict[str, Any]:
    """Who owns the shared slot right now, for the manager's Claude panel."""
    try:
        meta = read_meta()
    except Exception as error:  # pragma: no cover - surfaced as text in the UI
        return {"available": False, "reason": str(error)}
    applied = str(meta.get("appliedId") or "")
    claim = read_slot_claim()
    return {
        "available": True,
        "applied_id": applied,
        "applied_name": _entry_name(meta, applied),
        "mine": applied == SOTA_ENTRY_ID,
        "claimed": bool(claim),
        "displaced_id": str(claim.get("previous_applied_id") or ""),
        "displaced_name": str(claim.get("previous_applied_name") or ""),
        "claimed_at": str(claim.get("claimed_at") or ""),
        "claude_running": claude_running,
        "claim_path": str(_slot_claim_path()),
    }


def _set_applied_locked(entry_id: str) -> dict[str, Any]:
    """Flip appliedId inside an already-held library lock.

    Split out of apply_entry so the slot claim/release protocol can reuse exactly the same
    transaction (snapshot, backup, optimistic revision check, restore-on-failure) instead of
    growing a second, subtly different copy of it.  Callers must hold _library_lock() and must
    call ensure_deployment_mode() *before* taking the lock -- that helper takes the lock itself.
    """
    initial_meta = read_meta()
    legacy = _legacy_candidates(initial_meta)
    wanted_id = SOTA_ENTRY_ID if entry_id in legacy else entry_id
    transaction_paths = _migration_paths(initial_meta)
    snapshot = _snapshot(transaction_paths)
    conflict_snapshot = snapshot
    backup = _backup(transaction_paths)
    try:
        migration = _migrate_legacy_locked(manage_recovery=False)
        if migration.get("status") == "migrated":
            migration["backup"] = str(backup)
        meta, meta_revision = _read_meta_with_revision()
        conflict_snapshot = _snapshot(transaction_paths)
        known = {str(e.get("id")) for e in meta["entries"] if isinstance(e, dict)}
        if wanted_id not in known:
            raise ValueError(f"配置库里没有这个档：{wanted_id}")
        path = _safe_entry_path(wanted_id)
        if path is None or not path.exists():
            raise RuntimeError(f"这个档的文件不在了：{wanted_id}")
        previous = meta.get("appliedId")
        meta["appliedId"] = wanted_id
        _assert_meta_revision(meta_revision)
        _atomic_write_json(META_PATH, meta)
    except ConcurrentClaudeConfigUpdate:
        _restore(
            {
                path: value
                for path, value in conflict_snapshot.items()
                if path != META_PATH
            }
        )
        raise
    except Exception:
        _restore(snapshot)
        raise
    name = next(
        (str(e.get("name")) for e in meta["entries"] if str(e.get("id")) == wanted_id), wanted_id
    )
    return {"status": "ready", "applied_id": wanted_id, "applied_name": name,
            "previous_applied": previous, "previous_applied_name": _entry_name(meta, previous),
            "backup": str(backup), "migration": migration, "meta": meta}


def apply_entry(entry_id: str) -> dict[str, Any]:
    """Switch which profile Claude Desktop uses, e.g. back to cc-switch's own entry."""
    deployment = ensure_deployment_mode()
    with _library_lock():
        result = _set_applied_locked(entry_id)
        meta = result.pop("meta")
        # An explicit switch redefines who owns the shared slot, so the launch-time claim from
        # a previous codex-sota launch must not survive it: releasing later would otherwise
        # yank the slot away from whatever the user just chose here.
        if result["applied_id"] == SOTA_ENTRY_ID:
            _record_claim_locked(meta, result["previous_applied"])
        else:
            _clear_slot_claim()
    result["deployment"] = deployment
    return result

