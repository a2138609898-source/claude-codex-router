#!/usr/bin/env python3
"""Native desktop manager for the registry-driven Codex SOTA profile."""

from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Mapping
import unicodedata
import urllib.request


def _expanded_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def resolve_core_root(
    environ: Mapping[str, str] | None = None,
    module_path: Path | None = None,
    executable_path: Path | None = None,
    bundle_root: Path | None = None,
) -> Path:
    """Locate the shared core without assuming one user's Documents layout."""
    env = os.environ if environ is None else environ
    configured = str(env.get("CODEX_SOTA_CORE_ROOT") or "").strip()
    if configured:
        return _expanded_path(configured)

    source = (module_path or Path(__file__)).resolve()
    executable = (executable_path or Path(sys.executable)).resolve()
    bundled = bundle_root or (
        Path(str(getattr(sys, "_MEIPASS"))).resolve()
        if getattr(sys, "_MEIPASS", None)
        else None
    )
    candidates = [
        source.parent.parent / "CodexHistorySync",
        source.parent / "CodexHistorySync",
        executable.parent / "CodexHistorySync",
        executable.parent.parent / "CodexHistorySync",
    ]
    if bundled is not None:
        candidates.insert(0, bundled)
        candidates.insert(1, bundled / "CodexHistorySync")
    candidates.append(Path.home() / "Documents" / "Codex" / "CodexHistorySync")
    for candidate in candidates:
        if (candidate / "sota_registry.py").is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def _powershell_command_source(name: str) -> str | None:
    powershell = shutil.which("powershell.exe") or str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Command -Name '{name}' -CommandType Application,ExternalScript "
                "-ErrorAction Stop | Select-Object -First 1 -ExpandProperty Source)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[-1] if completed.returncode == 0 and lines else None


def resolve_codex_sota_command(
    environ: Mapping[str, str] | None = None,
    module_path: Path | None = None,
    executable_path: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    powershell_lookup: Callable[[str], str | None] | None = None,
) -> Path | None:
    """Find the launcher from an override, adjacent install, PATH, or Get-Command."""
    env = os.environ if environ is None else environ
    configured = str(env.get("CODEX_SOTA_COMMAND") or "").strip()
    if configured:
        candidate = _expanded_path(configured)
        return candidate if candidate.is_file() else None

    source = (module_path or Path(__file__)).resolve()
    executable = (executable_path or Path(sys.executable)).resolve()
    roots = (
        source.parent,
        source.parent.parent / "bin",
        executable.parent,
        executable.parent / "bin",
    )
    for root in roots:
        for name in ("codex-sota.cmd", "codex-sota.bat", "codex-sota-cli.exe"):
            candidate = root / name
            if candidate.is_file():
                return candidate.resolve()

    find_on_path = which or shutil.which
    for name in ("codex-sota.cmd", "codex-sota"):
        value = find_on_path(name)
        if value and Path(value).is_file():
            return Path(value).resolve()

    lookup = powershell_lookup or _powershell_command_source
    value = lookup("codex-sota")
    if value and Path(value).is_file():
        return Path(value).resolve()
    return None


def codex_sota_invocation(command: Path) -> tuple[list[str], Path]:
    """Build an invocation appropriate for a script or native executable."""
    suffix = command.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        processor = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
        return [str(processor), "/d", "/c", str(command)], command.parent
    if suffix == ".ps1":
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        return [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(command),
        ], command.parent
    return [str(command)], command.parent


CORE_ROOT = resolve_core_root()
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from claude_desktop import (  # noqa: E402
    CLAUDE_3P_ROOT,
    SOTA_ENTRY_ID as CLAUDE_ENTRY_ID,
    SOTA_ENTRY_NAME as CLAUDE_ENTRY_NAME,
    apply_entry as apply_claude_entry,
    build_inference_models,
    claim_slot as claim_claude_slot,
    ensure_deployment_mode,
    library_status as claude_library_status,
    migrate_legacy_entry,
    reconcile_slot as reconcile_claude_slot,
    release_slot as release_claude_slot,
    slot_state as claude_slot_state,
    thinking_effort_levels as claude_thinking_effort_levels,
    thinking_summary as claude_thinking_summary,
    write_profile as write_claude_profile,
)
from codex_sota_router import ROUTER_VERSION  # noqa: E402
from sota_registry import (  # noqa: E402
    AUTH_PATH,
    CLAUDE,
    CODEX,
    MESSAGES_PREFIX_SUFFIX,
    WORKSPACES,
    REGISTRY_PATH,
    SOTA_ROOT,
    apply_provider,
    audit_registry,
    auto_repair_messages_path,
    auto_repair_responses_path,
    delete_provider,
    derive_model_prefix,
    discover_models,
    failover_chain,
    find_provider,
    lint_registry,
    load_registry,
    measure_fast_tier,
    measure_latency,
    probe_fast_tier,
    rebuild_catalog,
    redacted_registry,
    registry_digest,
    registry_write_lock,
    reorder_providers,
    restart_router,
    save_provider_bookkeeping,
    secret_path,
    selectable_slugs,
    test_model,
    utc_now,
    validate_provider,
    validate_registry,
    write_registry,
)


APP_NAME = "codex-sota"
LAUNCH_TIMEOUT_SECONDS = 900
LAUNCH_SLOW_HINT_SECONDS = 45
LAUNCH_WINDOW_WAIT_SECONDS = 300
LAUNCH_WINDOW_POLL_SECONDS = 3
ROUTER_LOG_PATH = SOTA_ROOT / "log" / "sota-router.jsonl"
ROUTER_LOG_TAIL_BYTES = 262144
ROUTER_LOG_SAMPLE = 200
FAST_MEASURE_PAIRS = 8
LATENCY_SAMPLES = 3
# Claude Desktop talks to the Claude workspace router, which is a separate process on a
# separate port from the Codex one so the two can never disturb each other.
CLAUDE_GATEWAY_URL = f"http://127.0.0.1:{CLAUDE.router_port}"
# Two Claude Desktop installations can be registered at once on Windows.  The Squirrel
# installation is the one that reads %LOCALAPPDATA%\\Claude-3p; the MSIX package normally
# reads the 1P %LOCALAPPDATA%\\Claude tree.  Keep the Squirrel id as the compatibility default,
# but resolve the actually registered target at launch time so an update or a different machine
# does not silently open the wrong account.
CLAUDE_SQUIRREL_AUMID = "com.squirrel.AnthropicClaude.claude"
CLAUDE_MSIX_AUMID = "Claude_pzs8sxrjxfjjc!Claude"
CLAUDE_AUMID = CLAUDE_SQUIRREL_AUMID
CLAUDE_AUMID_ENV = "CODEX_SOTA_CLAUDE_AUMID"
HEALTH_REFRESH_MS = 30000
DEFAULT_WINDOW_WIDTH = 1220
DEFAULT_WINDOW_HEIGHT = 790
MIN_WINDOW_WIDTH = 1040
MIN_WINDOW_HEIGHT = 700
BG = "#F4F6F8"
SURFACE = "#FFFFFF"
SIDEBAR = "#20252B"
SIDEBAR_2 = "#2B323A"
TEXT = "#18212B"
MUTED = "#66717E"
ACCENT = "#1677FF"
SUCCESS = "#1F8A5B"
WARNING = "#B7791F"
DANGER = "#C53B3B"
BORDER = "#D8DEE6"


def enable_dpi_awareness() -> None:
    try:
        ctypes = __import__("ctypes")
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not text:
        digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:8]
        text = "provider_" + digest
    elif not text[0].isalpha():
        text = "provider_" + text
    return text[:40]


def draft_protocols(responses: bool, messages: bool, workspace: Any) -> list[str]:
    """The protocols an editor's two checkboxes select, in registry order.

    A free function rather than a method so the save path stays drivable with a duck-typed
    form object, and so the prefix autofill and the saved provider cannot disagree about
    what the checkboxes mean. Neither box ticked falls back to the workspace's own protocol.
    """
    chosen = [
        name
        for name, ticked in (("responses", responses), ("messages", messages))
        if ticked
    ]
    return chosen or (["messages"] if workspace is CLAUDE else ["responses"])


def bundled_resource(name: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parent
    return root / name


API_KEY_REJECT = re.compile(r"Get-Clipboard|codex-sota|--with-api-key|[|\r\n]", re.IGNORECASE)


def sota_auth_problem() -> str | None:
    """Mirror Test-ExistingSotaAuth so the GUI never triggers a hidden login prompt."""
    if not AUTH_PATH.exists():
        return f"未找到 {AUTH_PATH}"
    try:
        auth = json.loads(AUTH_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return f"{AUTH_PATH} 读取或解析失败：{error}"
    if not isinstance(auth, dict):
        return f"{AUTH_PATH} 不是一个 JSON 对象"
    if auth.get("auth_mode") != "apikey":
        return f"auth_mode 是 {auth.get('auth_mode')!r}，应为 'apikey'"
    key = auth.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        return "auth.json 里缺少 OPENAI_API_KEY"
    if API_KEY_REJECT.search(key):
        return "auth.json 里的 OPENAI_API_KEY 看起来是一条命令而不是密钥"
    return None


VOLATILE_MODEL_KEYS = (
    "last_test_status",
    "last_test_at",
    "last_test_message",
    "fast_tier_status",
    "fast_tier_effect",
)

# Everything the config editor writes on save.  Any provider key outside this set belongs to
# whoever set it (protected, is_default, auth_type, secret_file, entropy, and whatever a future
# version adds) and must be carried over from disk rather than from the editor's snapshot.
FORM_OWNED_PROVIDER_KEYS = (
    "name",
    "base_url",
    "prefix",
    "enabled",
    "allow_failover",
    "protocols",
    "auth_header",
    "auth_prefix",
    "models_path",
    "responses_path",
    "messages_path",
    "timeout_seconds",
    "extra_headers",
    "models",
)


def comparable_provider(provider: dict[str, Any]) -> dict[str, Any]:
    """Provider snapshot without probe bookkeeping, for "did the user change anything".

    Test results and tier verdicts are written by the app itself, so counting them as
    edits would make every checkup look like unsaved work. fast_tier_forced stays in —
    that one is a deliberate setting.
    """
    trimmed = deepcopy(provider)
    trimmed.pop("updated_at", None)
    for model in trimmed.get("models") or []:
        for key in VOLATILE_MODEL_KEYS:
            model.pop(key, None)
    return trimmed


# What `redacted_registry` writes in place of a header value it refuses to export.
REDACTED_HEADER = "<redacted>"


def merge_restored_registry(
    incoming: dict[str, Any], current: dict[str, Any], workspace_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold a restored registry onto what is on disk, and report what the swap would do.

    An exported snapshot is deliberately incomplete: it has no keys, no entropy, and its
    extra headers read `<redacted>`. Writing it back verbatim would therefore break every
    provider that authenticates with a header value — so anything the file cannot carry is
    taken from the provider of the same id still on disk, and only genuinely absent secrets
    are reported as needing attention.

    `workspace_name` is the workspace being restored into. Every provider carries the home it
    belongs to, and a Codex backup dropped onto the Claude registry would validate cleanly
    while quietly pointing one side's router at the other side's providers — so entries from
    a different home are reported rather than merged, and the caller refuses the whole file.
    """
    on_disk = {
        str(provider.get("id") or ""): provider for provider in current.get("providers") or []
    }
    restored: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "added": [],
        "removed": [],
        "protected_removed": [],
        "needs_key": [],
        "kept_headers": [],
        "foreign_workspace": [],
    }
    for provider in incoming.get("providers") or []:
        provider = deepcopy(provider)
        # A marker the exporter adds for the reader's benefit; validate_provider does not strip
        # unknown keys, so leaving it in would write it straight into providers.json.
        provider.pop("key_present", None)
        provider_id = str(provider.get("id") or "")
        home = str(provider.get("workspace") or CODEX.name)
        if home != workspace_name:
            report["foreign_workspace"].append(f"{provider_id}（{home}）")
        previous = on_disk.get(provider_id)
        if previous is None:
            report["added"].append(provider_id)
        elif "entropy" not in provider and "entropy" in previous:
            provider["entropy"] = previous["entropy"]
        headers = provider.get("extra_headers")
        old_headers = (previous or {}).get("extra_headers") or {}
        if isinstance(headers, dict):
            for name, value in list(headers.items()):
                if value != REDACTED_HEADER:
                    continue
                # Never write the placeholder itself: as a header value it is a wrong
                # credential, which fails as a 401 the user has no way to explain. A brand
                # new provider has no disk twin to borrow from, so the header goes instead —
                # a missing header is at least a problem the lint board can name.
                if name in old_headers:
                    headers[name] = old_headers[name]
                    report["kept_headers"].append(f"{provider_id}.{name}")
                else:
                    headers.pop(name)
        restored.append(provider)
    incoming_ids = {str(provider.get("id") or "") for provider in restored}
    for provider_id, provider in on_disk.items():
        if provider_id in incoming_ids:
            continue
        report["removed"].append(provider_id)
        if provider.get("protected"):
            report["protected_removed"].append(provider_id)
    for provider in restored:
        if provider.get("auth_type") == "codex_auth":
            continue
        try:
            if not secret_path(provider).exists():
                report["needs_key"].append(str(provider.get("id") or ""))
        except Exception:  # noqa: BLE001 - a malformed secret name is the validator's problem
            report["needs_key"].append(str(provider.get("id") or ""))
    candidate = {"version": incoming.get("version"), "providers": restored}
    return candidate, report


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; the router log is small enough that interpolation is noise."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def read_router_vendor_health(
    log_path: Path = ROUTER_LOG_PATH, sample: int = ROUTER_LOG_SAMPLE
) -> dict[str, dict[str, Any]]:
    """Summarise the most recent router requests per upstream vendor."""
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - ROUTER_LOG_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return {}
    summary: dict[str, dict[str, Any]] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines()[-sample:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        vendor = entry.get("vendor")
        status = entry.get("status")
        if not isinstance(vendor, str) or not isinstance(status, int):
            continue
        bucket = summary.setdefault(
            vendor,
            {"total": 0, "ok": 0, "last_status": None, "last_time": None, "durations": []},
        )
        bucket["total"] += 1
        if 200 <= status < 300:
            bucket["ok"] += 1
        bucket["last_status"] = status
        bucket["last_time"] = entry.get("time")
        duration = entry.get("duration_ms")
        if isinstance(duration, (int, float)) and 200 <= status < 300:
            bucket["durations"].append(float(duration))
    for bucket in summary.values():
        durations = bucket.pop("durations")
        bucket["p50_ms"] = round(percentile(durations, 0.50))
        bucket["p95_ms"] = round(percentile(durations, 0.95))
        bucket["success_rate"] = bucket["ok"] / bucket["total"] if bucket["total"] else 0.0
    return summary


def degraded_vendors(health: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Upstreams whose every recent request failed — the app looks broken when this is non-empty."""
    return sorted(
        ((name, data) for name, data in health.items() if data["total"] and not data["ok"]),
        key=lambda item: item[0],
    )


# The health board asks "is this vendor up right now" and 200 recent lines answer that. Usage
# asks "what have I spent", which needs a much longer reach back through the same file.
USAGE_LOG_TAIL_BYTES = 8 * 1024 * 1024
USAGE_SAMPLE = 50000
DEFAULT_PRICE = {"in": 0.0, "out": 0.0}


def load_usage_prices(path: Path) -> dict[str, Any]:
    """Per-million-token prices, or an empty table when the file is absent or unusable.

    Never raises: a hand-edited price file with a typo in it must degrade to "cost unknown"
    rather than take the usage panel — or the app — down with it.
    """
    table: dict[str, Any] = {"version": 1, "currency": "USD", "models": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return table
    if not isinstance(raw, dict):
        return table
    currency = raw.get("currency")
    if isinstance(currency, str) and currency.strip():
        table["currency"] = currency.strip()[:8]
    models = raw.get("models")
    if isinstance(models, dict):
        for model, price in models.items():
            if not isinstance(model, str) or not isinstance(price, dict):
                continue
            entry = {}
            for side in ("in", "out"):
                value = price.get(side)
                if isinstance(value, (int, float)) and value >= 0:
                    entry[side] = float(value)
            if entry:
                table["models"][model] = {**DEFAULT_PRICE, **entry}
    return table


def save_usage_prices(path: Path, table: dict[str, Any]) -> None:
    """Write the price table atomically, the same way the registry is written.

    Prices are only reference data, but a half-written file would read back as "no prices" and
    silently zero every cost figure, so the temp-then-replace dance is worth it here too.
    """
    clean = load_usage_prices_payload(table)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
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


def load_usage_prices_payload(table: dict[str, Any]) -> dict[str, Any]:
    """Normalise a price table in memory, using the same rules as reading one off disk."""
    models = {}
    for model, price in (table.get("models") or {}).items():
        if not isinstance(model, str) or not isinstance(price, dict):
            continue
        entry = {
            side: float(price[side])
            for side in ("in", "out")
            if isinstance(price.get(side), (int, float)) and price[side] >= 0
        }
        if entry:
            models[model] = {**DEFAULT_PRICE, **entry}
    currency = str(table.get("currency") or "USD").strip()[:8] or "USD"
    return {"version": 1, "currency": currency, "models": models}


def price_for_model(prices: dict[str, Any], model: str) -> dict[str, float] | None:
    """The price row for a model, tolerating the vendor prefixes the router strips.

    A slug reaches the log as the vendor's own model id, but users type prices as the
    familiar name, so `openai/gpt-5` in the table still matches `gpt-5` in the log.
    Both prefix shapes are stripped: `vendor--` and the messages-side `vendor.anthropic.`.
    """
    table = prices.get("models") or {}
    if model in table:
        return table[model]
    for key, value in table.items():
        tail = key.rsplit("/", 1)[-1].rsplit("--", 1)[-1].rsplit(MESSAGES_PREFIX_SUFFIX, 1)[-1]
        if tail and tail == model:
            return value
    return None


def entry_cost(entry_tokens: dict[str, int], price: dict[str, float] | None) -> float:
    """Cost of one rollup at per-million-token prices, 0.0 when the model has no price."""
    if not price:
        return 0.0
    return (
        entry_tokens.get("tokens_in", 0) * price.get("in", 0.0)
        + entry_tokens.get("tokens_out", 0) * price.get("out", 0.0)
    ) / 1_000_000


def utc_day_boundaries(days_back: int = 0) -> tuple[str, str]:
    """The UTC timestamps bracketing a local calendar day, in the log's own text format.

    The log stores UTC but the user thinks in local days, so the window is computed once here
    and compared as fixed-width strings. That is exact for this format and avoids parsing tens
    of thousands of timestamps to answer "how much today".
    """
    local_midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_back)
    start = local_midnight.astimezone(timezone.utc)
    end = start + timedelta(days=1)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(stamp), end.strftime(stamp)


def _blank_rollup() -> dict[str, Any]:
    return {"requests": 0, "ok": 0, "tokens_in": 0, "tokens_out": 0, "durations": []}


def _finish_rollup(bucket: dict[str, Any], prices: dict[str, Any], model: str = "") -> None:
    durations = bucket.pop("durations", [])
    bucket["p50_ms"] = round(percentile(durations, 0.50))
    bucket["p95_ms"] = round(percentile(durations, 0.95))
    bucket["success_rate"] = bucket["ok"] / bucket["requests"] if bucket["requests"] else 0.0
    bucket["tokens"] = bucket["tokens_in"] + bucket["tokens_out"]
    if model:
        price = price_for_model(prices, model)
        bucket["priced"] = price is not None
        bucket["cost"] = entry_cost(bucket, price)


def read_router_usage(
    log_path: Path,
    prices: dict[str, Any] | None = None,
    sample: int = USAGE_SAMPLE,
    tail_bytes: int = USAGE_LOG_TAIL_BYTES,
) -> dict[str, Any]:
    """Roll the router's request log up into per-vendor and per-model usage and cost.

    Reads the tail of a plain file — no upstream calls, nothing to authenticate, and safe to
    run against a log the router is appending to. Lines written before token accounting
    existed are still counted as requests and reported separately, so the panel can say the
    totals start from the upgrade instead of quietly implying the vendor sent no tokens.
    """
    prices = prices or {"models": {}, "currency": "USD"}
    summary: dict[str, Any] = {
        "currency": prices.get("currency", "USD"),
        "lines": 0,
        "legacy_lines": 0,
        "totals": _blank_rollup(),
        "today": _blank_rollup(),
        "vendors": {},
        "models": {},
        "first_time": "",
        "last_time": "",
        "log_bytes": 0,
        "truncated": False,
    }
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            summary["log_bytes"] = size
            summary["truncated"] = size > tail_bytes
            handle.seek(max(0, size - tail_bytes))
            raw = handle.read()
    except OSError:
        _finish_rollup(summary["totals"], prices)
        _finish_rollup(summary["today"], prices)
        return summary

    day_start, day_end = utc_day_boundaries()
    # Tokens per (vendor, model) so a vendor is only ever billed for what it actually served.
    pairs: dict[tuple[str, str], dict[str, int]] = {}
    today_pairs: dict[tuple[str, str], dict[str, int]] = {}
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if summary["truncated"] and lines:
        # The seek lands mid-line; that first fragment is not valid JSON anyway, but dropping
        # it explicitly keeps "lines" honest rather than counting a parse failure.
        lines = lines[1:]
    for line in lines[-sample:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        vendor = entry.get("vendor")
        status = entry.get("status")
        if not isinstance(vendor, str) or not isinstance(status, int):
            continue
        # 499 is our own client walking away. It is a real request that really cost tokens
        # upstream, so it counts here even though the health board ignores it.
        summary["lines"] += 1
        stamp = str(entry.get("time") or "")
        if stamp:
            summary["first_time"] = summary["first_time"] or stamp
            summary["last_time"] = stamp
        tokens_in = entry.get("tokens_in")
        tokens_out = entry.get("tokens_out")
        model = str(entry.get("model") or "")
        if not model:
            # Written before the router logged models at all. Counting these as "reported no
            # tokens" would understate every total, so they are reported as their own figure.
            summary["legacy_lines"] += 1
        counted = {
            "tokens_in": tokens_in if isinstance(tokens_in, int) else 0,
            "tokens_out": tokens_out if isinstance(tokens_out, int) else 0,
        }
        succeeded = 200 <= status < 300
        duration = entry.get("duration_ms")
        is_today = bool(stamp) and day_start <= stamp < day_end
        targets = [summary["totals"], summary["vendors"].setdefault(vendor, _blank_rollup())]
        if model:
            targets.append(summary["models"].setdefault(model, _blank_rollup()))
            for book in (pairs, today_pairs) if is_today else (pairs,):
                pair = book.setdefault((vendor, model), {"tokens_in": 0, "tokens_out": 0})
                pair["tokens_in"] += counted["tokens_in"]
                pair["tokens_out"] += counted["tokens_out"]
        if is_today:
            targets.append(summary["today"])
        for bucket in targets:
            bucket["requests"] += 1
            bucket["ok"] += 1 if succeeded else 0
            bucket["tokens_in"] += counted["tokens_in"]
            bucket["tokens_out"] += counted["tokens_out"]
            if isinstance(duration, (int, float)) and succeeded:
                bucket["durations"].append(float(duration))
        if model:
            summary["models"][model].setdefault("vendors", set()).add(vendor)

    for name, bucket in summary["models"].items():
        bucket["vendors"] = sorted(bucket.get("vendors") or [])
        _finish_rollup(bucket, prices, name)
    for bucket in summary["vendors"].values():
        _finish_rollup(bucket, prices)
        bucket["cost"] = 0.0
    # A vendor rollup mixes models, so its cost is built from what that vendor actually served
    # of each model.  Fanning a model's whole cost out to every vendor that ever answered for
    # it would bill a vendor for tokens a different vendor delivered.
    total_cost = 0.0
    for (vendor, model), tokens in pairs.items():
        cost = entry_cost(tokens, price_for_model(prices, model))
        total_cost += cost
        row = summary["vendors"].get(vendor)
        if row is not None:
            row["cost"] = row.get("cost", 0.0) + cost
    _finish_rollup(summary["totals"], prices)
    _finish_rollup(summary["today"], prices)
    summary["totals"]["cost"] = total_cost
    summary["today"]["cost"] = sum(
        entry_cost(tokens, price_for_model(prices, model))
        for (_vendor, model), tokens in today_pairs.items()
    )
    summary["unpriced"] = sorted(
        name for name, row in summary["models"].items() if not row["priced"] and row["tokens"]
    )
    return summary


def format_tokens(value: int) -> str:
    """Compact token counts; a million-token month should not read as seven digits."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_cost(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "CNY": "¥", "RMB": "¥", "EUR": "€"}.get(currency.upper(), "")
    if not value:
        return "—"
    if value < 0.01:
        return f"{symbol}{value:.4f}" if symbol else f"{value:.4f} {currency}"
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def speed_label(model: dict[str, Any]) -> str:
    """The switch the router acts on: 快速 makes it send service_tier=priority upstream."""
    return "快速" if model.get("fast_tier_forced") else "标准"


def fast_tier_note(model: dict[str, Any]) -> str:
    """Extra context for the log — what the probe found out about this upstream."""
    status = model.get("fast_tier_status") or "unknown"
    if status == "unsupported":
        return "（探测时这个上游拒绝过 service_tier，开了可能会报错）"
    if status != "supported":
        return "（还没探测过这个上游收不收 service_tier）"
    if model.get("fast_tier_effect") == "none":
        return "（上游收下了，但实测没有提速）"
    if model.get("fast_tier_effect") == "faster":
        return "（实测有提速）"
    return ""


def first_meaningful_line(*blocks: str) -> str:
    for block in blocks:
        for line in (block or "").splitlines():
            text = line.strip()
            if text:
                return text
    return "启动器没有输出任何错误信息。"


def _safe_claude_aumid(value: str) -> str | None:
    """Accept only a single AppUserModelId token supplied by the local app registry."""
    text = str(value or "").strip()
    if not text or any(ord(character) < 32 or ord(character) == 127 for character in text):
        return None
    if "/" in text or "\\" in text or "\"" in text or "'" in text:
        return None
    return text


def registered_claude_app_ids() -> set[str]:
    """Read Claude AppUserModelIds without starting either installation.

    `Get-StartApps` is the supported Windows view of shell-registered desktop apps.  A failed
    query returns an empty set; callers then use the executable/3P-root fallback rather than
    guessing that the MSIX package is the right account.
    """
    if os.name != "nt":
        return set()
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | Where-Object { $_.Name -eq 'Claude' } | ForEach-Object { $_.AppID }",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode != 0:
        return set()
    return {
        value
        for value in (_safe_claude_aumid(line) for line in completed.stdout.splitlines())
        if value
    }


def resolve_claude_launch_target() -> dict[str, str] | None:
    """Select the installed Claude entry that can read the isolated 3P configuration.

    Squirrel is preferred whenever it is registered or its installation is present, because
    that is the deployment which owns `%LOCALAPPDATA%\\Claude-3p`.  A registered MSIX entry is
    never selected implicitly: the two Claude packages can point at different data roots, so
    the presence of a 3P tree does not prove that MSIX will read it.  An MSIX launch is allowed
    only through the explicit ``CODEX_SOTA_CLAUDE_AUMID`` override above.
    """
    override = _safe_claude_aumid(os.environ.get(CLAUDE_AUMID_ENV, ""))
    if override:
        return {"kind": "aumid", "value": override, "label": "环境变量指定的 Claude 入口"}

    registered = registered_claude_app_ids()
    local_appdata = Path(
        os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    )
    squirrel_root = local_appdata / "AnthropicClaude"
    squirrel_exe = squirrel_root / "claude.exe"
    squirrel_update = squirrel_root / "Update.exe"

    if CLAUDE_SQUIRREL_AUMID in registered:
        return {
            "kind": "aumid",
            "value": CLAUDE_SQUIRREL_AUMID,
            "label": "Claude Squirrel 3P",
        }
    if squirrel_exe.is_file() or squirrel_update.is_file():
        # An unregistered Squirrel install can still be started through its updater, which
        # preserves the same user-data root and avoids opening the MSIX account.
        if squirrel_update.is_file():
            return {
                "kind": "update",
                "value": str(squirrel_update),
                "cwd": str(squirrel_root),
                "label": "Claude Squirrel 3P（Update.exe）",
            }
        return {
            "kind": "path",
            "value": str(squirrel_exe),
            "cwd": str(squirrel_root),
            "label": "Claude Squirrel 3P（claude.exe）",
        }

    return None


EXPLORER_EXE = str(
    Path(os.environ.get("SystemRoot") or r"C:\Windows") / "explorer.exe"
)


def claude_launch_command(target: Mapping[str, str]) -> tuple[list[str], Path | None]:
    """Convert a resolved Claude target into a safe, testable process invocation."""
    kind = target.get("kind")
    value = str(target.get("value") or "").strip()
    if kind == "aumid":
        app_id = _safe_claude_aumid(value)
        if not app_id:
            raise ValueError("Claude AppUserModelId 无效")
        # Absolute path on purpose: CreateProcess searches the current directory before PATH,
        # and the manager can be started from any working directory.
        return [EXPLORER_EXE, f"shell:AppsFolder\\{app_id}"], None
    if kind == "update":
        path = Path(value)
        return [str(path), "--processStart", "Claude.exe"], Path(
            target.get("cwd") or path.parent
        )
    if kind == "path":
        path = Path(value)
        return [str(path)], Path(target.get("cwd") or path.parent)
    raise ValueError("未知的 Claude 启动目标")


def start_claude_process(target: Mapping[str, str]) -> dict[str, Any]:
    """Start Claude Desktop and report *how* it was started, never whether it worked.

    explorer.exe is a shell dispatcher, not a launcher.  Measured on this machine
    (check_explorer_launch_exitcode.py): activating a valid AppsFolder target returns exit
    code 1 even though the app starts, and a nonexistent AUMID also returns 1.  The exit code
    therefore carries no information, and treating non-zero as failure rejected every working
    launch.  Whether a Claude window appears is the only authoritative signal, so it is kept
    here purely as a diagnostic hint for the "no window appeared" message.

    The direct-exe kinds must not go through capture_output: Claude Desktop holds its stdout
    pipe open for its whole lifetime, so the reader would block until the timeout expires.
    """
    command, command_cwd = claude_launch_command(target)
    cwd = str(command_cwd) if command_cwd else None
    if target.get("kind") == "aumid":
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            check=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "detail": first_meaningful_line(completed.stderr, completed.stdout),
        }
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
    process = subprocess.Popen(  # noqa: S603 - command is built from a validated target
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return {"command": command, "returncode": None, "pid": process.pid, "detail": ""}


def resolve_helper_python() -> Path | None:
    """Find an interpreter that can run the CodexHistorySync helper scripts.

    The candidate list mirrors Start-CodexSotaRouter.ps1's resolver, and for the same reason:
    the python.exe on PATH here is a Microsoft Store stub that exits instead of running code,
    so PATH is consulted only as a last resort.  sys.executable is unusable in a PyInstaller
    build, where it is the manager's own .exe.
    """
    candidates: list[Path] = []
    override = str(os.environ.get("CODEX_SOTA_PYTHON") or os.environ.get("CODEX_PYTHON") or "").strip()
    if override:
        candidates.append(_expanded_path(override))
    if not getattr(sys, "frozen", False):
        # pythonw keeps the watcher from flashing a console window on the user's desktop.
        interpreter = Path(sys.executable)
        candidates.extend([interpreter.with_name("pythonw.exe"), interpreter])
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    local_appdata = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    candidates.append(
        home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "pythonw.exe"
    )
    for version in ("Python313", "Python312", "Python311"):
        candidates.append(local_appdata / "Programs" / "Python" / version / "pythonw.exe")
        candidates.append(local_appdata / "Programs" / "Python" / version / "python.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("pythonw.exe") or shutil.which("python.exe")
    return Path(found) if found else None


def arm_claude_slot_release() -> dict[str, Any]:
    """Start the detached watcher that returns the shared config slot when Claude exits.

    Deliberately best-effort: a Claude that is already up must not be reported as a failed
    launch just because the watcher could not start.  reconcile_slot() on the next manager
    start is the backstop, and the manual "交还" button is always available.
    """
    script = CORE_ROOT / "release_claude_slot_after_exit.py"
    if not script.is_file():
        return {"status": "missing-script", "detail": str(script)}
    interpreter = resolve_helper_python()
    if interpreter is None:
        return {"status": "no-python", "detail": "找不到可用的 Python 解释器"}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed script path, no user input
            [str(interpreter), str(script)],
            cwd=str(CORE_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "failed", "detail": str(error)}
    return {"status": "armed", "pid": process.pid, "python": str(interpreter)}




def image_pids(image_name: str) -> set[int]:
    """Return PIDs for an image, failing closed when Windows cannot answer.

    An empty set means tasklist completed successfully and found no matching process.
    Query failures must stay visible to callers: treating them as an empty set could make
    the launcher start a second Claude/Codex instance over a still-running one.
    """
    tasklist = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tasklist.exe"
    )
    try:
        completed = subprocess.run(
            [tasklist, "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法查询 {image_name} 进程状态：{error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise RuntimeError(
            f"tasklist 查询 {image_name} 进程失败（退出码 {completed.returncode}）：{detail}"
        )
    return {int(match) for match in re.findall(r'^"[^"]*","(\d+)"', completed.stdout, re.MULTILINE)}


def pids_own_visible_window(pids: set[int]) -> bool:
    """True once any of these processes owns a visible top-level window."""
    if os.name != "nt" or not pids:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = False

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _: int) -> bool:
        nonlocal found
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            found = True
            return False
        return True

    user32.EnumWindows(visit, 0)
    return found


def close_visible_process_windows(pids: set[int]) -> set[int]:
    """Ask visible top-level windows owned by `pids` to close; return the owners signalled."""
    if os.name != "nt" or not pids:
        return set()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    signalled: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            if user32.PostMessageW(hwnd, 0x0010, 0, 0):  # WM_CLOSE
                signalled.add(owner.value)
        return True

    user32.EnumWindows(visit, 0)
    return signalled


def chatgpt_pids() -> set[int]:
    return image_pids("ChatGPT.exe")


def chatgpt_window_present() -> bool:
    """True once a ChatGPT.exe process owns a visible top-level window."""
    return pids_own_visible_window(chatgpt_pids())


def claude_pids() -> set[int]:
    return image_pids("claude.exe")


def claude_window_present() -> bool:
    """True once a claude.exe process owns a visible top-level window."""
    return pids_own_visible_window(claude_pids())


def close_running_claude(timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Gracefully close Claude Desktop so the next AUMID activation reads the new 3P profile.

    Only processes that own a visible Claude window are targeted.  A command-line executable
    also named claude.exe is therefore never terminated by this launcher.
    """
    initial = claude_pids()
    signalled = close_visible_process_windows(initial)
    if not signalled:
        return {"requested": [], "closed": []}
    deadline = time.monotonic() + timeout_seconds
    remaining = set(signalled)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        remaining &= claude_pids()
    if remaining:
        raise RuntimeError(
            "Claude Desktop 没有在 20 秒内退出。为避免旧的 1P 实例接管启动，"
            "请先手动关闭 Claude Desktop，再重试。"
        )
    return {"requested": sorted(signalled), "closed": sorted(signalled)}


def auto_repair_active_inference_path(
    provider: dict[str, Any], temporary_key: str | None = None
) -> dict[str, Any]:
    """Repair the endpoint for the protocol this provider's workspace actually probes."""
    protocols = provider.get("protocols") or ["responses"]
    use_messages = provider.get("workspace") == CLAUDE.name and "messages" in protocols
    repair = (
        auto_repair_messages_path(provider, temporary_key)
        if use_messages
        else auto_repair_responses_path(provider, temporary_key)
    )
    repair["protocol"] = "messages" if use_messages else "responses"
    return repair


def router_health_matches_workspace(health: dict[str, Any], workspace: Any) -> bool:
    """True only when the listener is this router build with this workspace loaded."""
    if health.get("status") != "ok" or str(health.get("version")) != str(ROUTER_VERSION):
        return False
    try:
        registry = load_registry(workspace.registry_path)
    except Exception:
        return False
    enabled = {
        str(provider["id"])
        for provider in registry.get("providers") or []
        if provider.get("enabled")
    }
    upstreams = health.get("upstreams")
    if not isinstance(upstreams, list):
        return False
    return (
        str(health.get("registry_hash") or "") == registry_digest(registry)
        and {str(provider_id) for provider_id in upstreams} == enabled
    )


class ModelDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("添加模型")
        self.geometry("440x220")
        self.minsize(420, 210)
        self.configure(bg=SURFACE)
        self.transient(parent)
        self.grab_set()
        self.result: dict[str, Any] | None = None
        self.model_id = tk.StringVar()
        self.display_name = tk.StringVar()
        self.enabled = tk.BooleanVar(value=True)

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="模型 ID").grid(row=0, column=0, sticky="w", pady=(0, 6))
        entry = ttk.Entry(body, textvariable=self.model_id)
        entry.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(body, text="显示名称").grid(row=2, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(body, textvariable=self.display_name).grid(row=3, column=0, sticky="ew")
        ttk.Checkbutton(body, text="加入 Codex 模型列表", variable=self.enabled).grid(
            row=4, column=0, sticky="w", pady=(12, 0)
        )
        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, sticky="e", pady=(16, 0))
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="添加", style="Accent.TButton", command=self._accept).pack(side="left")
        body.columnconfigure(0, weight=1)
        entry.focus_set()
        self.bind("<Return>", lambda _event: self._accept())
        self.bind("<Escape>", lambda _event: self.destroy())

    def _accept(self) -> None:
        model_id = self.model_id.get().strip()
        if not model_id or any(ch in model_id for ch in "\r\n\t"):
            messagebox.showerror("模型 ID 无效", "请输入有效的模型 ID。", parent=self)
            return
        self.result = {
            "id": model_id,
            "enabled": self.enabled.get(),
            "display_name": self.display_name.get().strip(),
            "description": "",
            "last_test_status": "untested",
            "fast_tier_status": "unknown",
            "fast_tier_effect": "untested",
            "fast_tier_forced": False,
        }
        self.destroy()


class PriceDialog(tk.Toplevel):
    """Per-million-token prices for the models this workspace actually uses.

    A form rather than a JSON file the user has to hand-edit, because a typo in that file is
    the difference between a cost figure and a silently wrong one. Prices are reference data
    only: nothing here is ever written back into providers.json.
    """

    def __init__(self, parent: tk.Misc, models: list[str], prices: dict[str, Any]):
        super().__init__(parent)
        self.title("模型价格（每百万 token）")
        self.geometry("560x520")
        self.minsize(520, 380)
        self.configure(bg=SURFACE)
        self.transient(parent)
        self.grab_set()
        self.result: dict[str, Any] | None = None
        self.currency = tk.StringVar(value=str(prices.get("currency") or "USD"))
        self.rows: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}

        body = ttk.Frame(self, padding=18, style="Surface.TFrame")
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="填上每百万 token 的单价，留空表示这个模型不计价。",
            style="Field.TLabel",
        ).pack(anchor="w")
        head = ttk.Frame(body, style="Surface.TFrame")
        head.pack(fill="x", pady=(8, 10))
        ttk.Label(head, text="货币", style="Field.TLabel").pack(side="left")
        ttk.Combobox(
            head, textvariable=self.currency, width=8, state="readonly",
            values=("USD", "CNY", "EUR"),
        ).pack(side="left", padx=(8, 0))

        canvas = tk.Canvas(body, bg=SURFACE, borderwidth=0, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        grid = ttk.Frame(canvas, padding=(0, 4, 12, 4), style="Surface.TFrame")
        window = canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        ttk.Label(grid, text="模型", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(grid, text="输入价", style="Section.TLabel").grid(row=0, column=1, padx=(12, 0))
        ttk.Label(grid, text="输出价", style="Section.TLabel").grid(row=0, column=2, padx=(8, 0))
        for index, model in enumerate(models, start=1):
            existing = price_for_model(prices, model) or {}
            money_in = tk.StringVar(value=self._as_text(existing.get("in")))
            money_out = tk.StringVar(value=self._as_text(existing.get("out")))
            self.rows[model] = (money_in, money_out)
            ttk.Label(grid, text=model, style="Field.TLabel").grid(
                row=index, column=0, sticky="w", pady=2
            )
            ttk.Entry(grid, textvariable=money_in, width=11).grid(
                row=index, column=1, padx=(12, 0), pady=2
            )
            ttk.Entry(grid, textvariable=money_out, width=11).grid(
                row=index, column=2, padx=(8, 0), pady=2
            )
        grid.columnconfigure(0, weight=1)
        if not models:
            ttk.Label(
                grid, text="这个工作区还没有启用任何模型。", style="Field.TLabel"
            ).grid(row=1, column=0, sticky="w")

        actions = ttk.Frame(self, padding=(18, 0, 18, 16), style="Surface.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(
            actions, text="保存价格", style="Accent.TButton", command=self._accept
        ).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())

    @staticmethod
    def _as_text(value: Any) -> str:
        if not isinstance(value, (int, float)) or not value:
            return ""
        return f"{float(value):g}"

    def _accept(self) -> None:
        table: dict[str, dict[str, float]] = {}
        for model, (money_in, money_out) in self.rows.items():
            entry = {}
            for side, variable in (("in", money_in), ("out", money_out)):
                # NFKC folds the full-width digits and full-width period a Chinese IME produces
                # into ASCII.  A comma is deliberately NOT stripped: "2,5" meant as a decimal
                # comma would silently become 25, so it is rejected rather than guessed at.
                text = unicodedata.normalize("NFKC", variable.get()).strip()
                if not text:
                    continue
                try:
                    value = float(text)
                except ValueError:
                    messagebox.showerror(
                        "价格无效", f"{model} 的{'输入' if side == 'in' else '输出'}价"
                        f"不是数字：{text}", parent=self,
                    )
                    return
                if value < 0:
                    messagebox.showerror("价格无效", f"{model} 的价格不能是负数。", parent=self)
                    return
                entry[side] = value
            if entry:
                table[model] = {**DEFAULT_PRICE, **entry}
        self.result = {
            "version": 1,
            "currency": self.currency.get().strip() or "USD",
            "models": table,
        }
        self.destroy()


class CodexSotaApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title(APP_NAME)
        try:
            self.iconbitmap(str(bundled_resource("codex-sota.ico")))
        except (OSError, tk.TclError):
            pass
        self.geometry(f"{DEFAULT_WINDOW_WIDTH}x{DEFAULT_WINDOW_HEIGHT}")
        self.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.configure(bg=BG)
        self._closing = False
        self._task_events: queue.Queue[tuple[str, Any, Any]] = queue.Queue()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.registry: dict[str, Any] = {}
        self.current_id: str | None = None
        # What the editor was populated from, so a save can tell "the user changed this" from
        # "someone else changed this on disk while the editor sat open".
        self.loaded_provider: dict[str, Any] | None = None
        self.draft_models: list[dict[str, Any]] = []
        self.provider_ids: list[str] = []
        self.provider_filter_var = tk.StringVar()
        self.provider_count_var = tk.StringVar(value="")
        self._busy = False
        self.workspace = CODEX
        self.workspace_var = tk.StringVar(value=CODEX.name)
        # Last usage roll-up, so the CSV export and the price dialog work off what is on screen
        # instead of re-scanning a multi-megabyte log.
        self._usage_summary: dict[str, Any] = {}
        self._launch_phase = "正在启动 Codex"
        self._protected = False
        self._id_touched = False
        self._prefix_touched = False

        self.name_var = tk.StringVar()
        self.id_var = tk.StringVar()
        self.base_url_var = tk.StringVar()
        self.prefix_var = tk.StringVar()
        self.api_key_var = tk.StringVar()
        self.models_path_var = tk.StringVar(value="/models")
        self.responses_path_var = tk.StringVar(value="/responses")
        self.messages_path_var = tk.StringVar(value="/v1/messages")
        self.auth_header_var = tk.StringVar(value="Authorization")
        self.auth_prefix_var = tk.StringVar(value="Bearer ")
        self.timeout_var = tk.StringVar(value="120")
        self.enabled_var = tk.BooleanVar(value=True)
        self.failover_var = tk.BooleanVar(value=False)
        self.proto_responses_var = tk.BooleanVar(value=True)
        self.proto_messages_var = tk.BooleanVar(value=False)
        self.claude_status_var = tk.StringVar(value="Claude Desktop：检查中")
        self._claude_entries: list[dict[str, Any]] = []
        self.reasoning_var = tk.StringVar(value="low")
        self.model_filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在读取配置")
        self.header_status_var = tk.StringVar(value="检查中")
        self.model_count_var = tk.StringVar(value="0 个模型")

        self._configure_styles()
        self._build_ui()
        self.name_var.trace_add("write", self._auto_identity)
        # The suggested prefix depends on the protocol choice, so a toggle has to re-run the
        # same autofill; both are no-ops once the user types a prefix of their own.
        self.proto_responses_var.trace_add("write", self._auto_identity)
        self.proto_messages_var.trace_add("write", self._auto_identity)
        self.model_filter_var.trace_add("write", lambda *_args: self._render_models())
        self.provider_filter_var.trace_add("write", self._on_provider_filter)
        self._place_in_work_area()
        self.deiconify()
        self.after(20, self._initial_load)
        self.after(50, self._drain_task_events)

    def _place_in_work_area(self) -> None:
        """Center the complete native window frame inside its monitor work area."""
        try:
            ctypes = __import__("ctypes")
            wintypes = __import__("ctypes.wintypes", fromlist=["wintypes"])
            user32 = ctypes.windll.user32

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            self.update_idletasks()
            child_handle = wintypes.HWND(self.winfo_id())
            window_handle = user32.GetParent(child_handle) or child_handle
            current_rect = wintypes.RECT()
            if not user32.GetWindowRect(window_handle, ctypes.byref(current_rect)):
                raise OSError("GetWindowRect failed")

            monitor = user32.MonitorFromWindow(window_handle, 2)
            monitor_info = MONITORINFO()
            monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
                work = monitor_info.rcWork
            else:
                work = wintypes.RECT()
                if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
                    raise OSError("SPI_GETWORKAREA failed")

            # A withdrawn Tk window can temporarily report 200x200 after
            # iconbitmap(), while its native frame already has the requested size.
            client_width = DEFAULT_WINDOW_WIDTH
            client_height = DEFAULT_WINDOW_HEIGHT
            frame_width = max(0, current_rect.right - current_rect.left - client_width)
            frame_height = max(0, current_rect.bottom - current_rect.top - client_height)
            work_width = max(1, work.right - work.left)
            work_height = max(1, work.bottom - work.top)
            target_width = min(DEFAULT_WINDOW_WIDTH, max(1, work_width - frame_width))
            target_height = min(DEFAULT_WINDOW_HEIGHT, max(1, work_height - frame_height))
            self.minsize(
                min(MIN_WINDOW_WIDTH, target_width),
                min(MIN_WINDOW_HEIGHT, target_height),
            )
            self.geometry(f"{target_width}x{target_height}")
            self.update_idletasks()

            actual_rect = wintypes.RECT()
            if not user32.GetWindowRect(window_handle, ctypes.byref(actual_rect)):
                raise OSError("GetWindowRect failed after sizing")
            outer_width = actual_rect.right - actual_rect.left
            outer_height = actual_rect.bottom - actual_rect.top
            target_x = work.left + max(0, (work_width - outer_width) // 2)
            target_y = work.top + max(0, (work_height - outer_height) // 2)
            flags = 0x0001 | 0x0004 | 0x0010 | 0x0200
            if not user32.SetWindowPos(
                window_handle,
                0,
                target_x,
                target_y,
                0,
                0,
                flags,
            ):
                raise OSError("SetWindowPos failed")
            self.update_idletasks()
        except Exception:
            self.update_idletasks()
            width = min(DEFAULT_WINDOW_WIDTH, self.winfo_screenwidth())
            height = min(DEFAULT_WINDOW_HEIGHT, self.winfo_screenheight())
            x = max(0, (self.winfo_screenwidth() - width) // 2)
            y = max(0, (self.winfo_screenheight() - height) // 2)
            self.geometry(f"{width}x{height}+{x}+{y}")

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        default_font = ("Microsoft YaHei UI", 10)
        self.option_add("*Font", default_font)
        style.configure(".", font=default_font, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Sidebar.TFrame", background=SIDEBAR)
        style.configure("Sidebar.TLabel", background=SIDEBAR, foreground="#F7F9FB")
        style.configure("MutedSidebar.TLabel", background=SIDEBAR, foreground="#A9B3BE")
        style.configure("Header.TLabel", background=SURFACE, foreground=TEXT, font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Section.TLabel", background=SURFACE, foreground=TEXT, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Field.TLabel", background=SURFACE, foreground=MUTED)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Status.TLabel", background=BG, foreground=MUTED)
        style.configure("Accent.TButton", background=ACCENT, foreground="white", borderwidth=0, padding=(14, 8))
        style.map("Accent.TButton", background=[("active", "#0E65D8"), ("disabled", "#A9C9F4")])
        style.configure("Danger.TButton", foreground=DANGER, padding=(10, 7))
        style.configure("TButton", padding=(10, 7))
        style.configure("TEntry", fieldbackground=SURFACE, bordercolor=BORDER, padding=7)
        style.configure("TCombobox", fieldbackground=SURFACE, bordercolor=BORDER, padding=6)
        style.configure("TSpinbox", fieldbackground=SURFACE, bordercolor=BORDER, padding=6)
        style.configure("TCheckbutton", background=SURFACE, foreground=TEXT)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), background=BG, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", SURFACE)], foreground=[("selected", TEXT)])
        style.configure("Treeview", rowheight=30, fieldbackground=SURFACE, background=SURFACE, bordercolor=BORDER)
        style.configure("Treeview.Heading", background="#EDF1F5", foreground=TEXT, padding=(8, 8), relief="flat")
        style.map("Treeview", background=[("selected", "#DCEBFF")], foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor="#E8EDF2", background=ACCENT, borderwidth=0)

    def _build_ui(self) -> None:
        header = ttk.Frame(self, style="Surface.TFrame", padding=(24, 16))
        header.grid(row=0, column=0, columnspan=2, sticky="nsew")
        ttk.Label(header, text="Codex SOTA", style="Header.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.header_status_var, style="Field.TLabel").pack(side="right", padx=(12, 0))
        self.launch_button = ttk.Button(header, text="启动 Codex", command=self._launch_codex)
        self.launch_button.pack(side="right")
        switcher = ttk.Frame(header, style="Surface.TFrame")
        switcher.pack(side="left", padx=(28, 0))
        for ws in (CODEX, CLAUDE):
            ttk.Radiobutton(
                switcher,
                text=ws.label,
                value=ws.name,
                variable=self.workspace_var,
                command=self._switch_workspace,
            ).pack(side="left", padx=(0, 10))
        self.audit_button = ttk.Button(header, text="全局审计", command=self._run_audit)
        self.audit_button.pack(side="right", padx=(0, 8))

        sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=285)
        sidebar.grid(row=1, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        header_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        header_row.pack(fill="x", padx=18, pady=(18, 6))
        ttk.Label(
            header_row, text="供应商", style="Sidebar.TLabel",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left")
        ttk.Label(header_row, textvariable=self.provider_count_var, style="Sidebar.TLabel").pack(
            side="right"
        )
        self.provider_filter_entry = ttk.Entry(sidebar, textvariable=self.provider_filter_var)
        self.provider_filter_entry.pack(fill="x", padx=12, pady=(0, 8))
        # The two button rows are packed against the bottom BEFORE the tree, so they reserve
        # their height first. Packed after an expand=True tree they get squeezed to nothing
        # and vanish whenever the window is short.
        side_actions = ttk.Frame(sidebar, style="Sidebar.TFrame")
        side_actions.pack(side="bottom", fill="x", padx=12, pady=(0, 14))
        self.add_button = ttk.Button(side_actions, text="＋ 添加", command=self._new_provider)
        self.add_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.delete_button = ttk.Button(side_actions, text="删除", style="Danger.TButton", command=self._delete_current)
        self.delete_button.pack(side="left", fill="x", expand=True)
        order_actions = ttk.Frame(sidebar, style="Sidebar.TFrame")
        order_actions.pack(side="bottom", fill="x", padx=12, pady=(0, 8))
        self.move_up_button = ttk.Button(
            order_actions, text="↑ 上移", command=lambda: self._move_provider(-1)
        )
        self.move_up_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.move_down_button = ttk.Button(
            order_actions, text="↓ 下移", command=lambda: self._move_provider(1)
        )
        self.move_down_button.pack(side="left", fill="x", expand=True)
        self.provider_tree = ttk.Treeview(
            sidebar,
            columns=("models",),
            show="tree headings",
            selectmode="browse",
            height=6,
        )
        self.provider_tree.heading("#0", text="名称")
        self.provider_tree.heading("models", text="模型")
        self.provider_tree.column("#0", width=190, minwidth=150)
        self.provider_tree.column("models", width=60, minwidth=50, anchor="center", stretch=False)
        self.provider_tree.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.provider_tree.bind("<<TreeviewSelect>>", self._provider_selected)

        content = ttk.Frame(self, padding=(18, 14, 18, 0))
        content.grid(row=1, column=1, sticky="nsew")
        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill="both", expand=True)
        self.config_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=22)
        self.models_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=18)
        self.tools_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=20)
        self.usage_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=20)
        self.notebook.add(self.config_tab, text="供应商配置")
        self.notebook.add(self.models_tab, text="模型选择")
        self.notebook.add(self.tools_tab, text="状态工具")
        self.notebook.add(self.usage_tab, text="用量花费")
        self._build_config_tab()
        self._build_models_tab()
        self._build_tools_tab()
        self._build_usage_tab()
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._bind_shortcuts()

        footer = ttk.Frame(self, padding=(18, 11))
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=160)
        self.progress.pack(side="left")
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").pack(side="left", padx=12)
        self.save_button = ttk.Button(footer, text="保存并应用", style="Accent.TButton", command=self._save_current)
        self.save_button.pack(side="right")
        self.reload_button = ttk.Button(footer, text="重新载入", command=self._reload_current)
        self.reload_button.pack(side="right", padx=(0, 8))

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

    def _field(self, parent: ttk.Frame, label: str, variable: tk.Variable, row: int, column: int, **kwargs: Any) -> ttk.Entry:
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=column, sticky="w", pady=(0, 5))
        entry = ttk.Entry(parent, textvariable=variable, **kwargs)
        entry.grid(row=row + 1, column=column, sticky="ew", padx=(0, 16), pady=(0, 14))
        return entry

    def _build_config_tab(self) -> None:
        outer = self.config_tab
        self.config_canvas = tk.Canvas(
            outer,
            bg=SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        config_scroll = ttk.Scrollbar(outer, orient="vertical", command=self.config_canvas.yview)
        self.config_canvas.configure(yscrollcommand=config_scroll.set)
        config_scroll.pack(side="right", fill="y")
        self.config_canvas.pack(side="left", fill="both", expand=True)
        tab = ttk.Frame(self.config_canvas, style="Surface.TFrame")
        self.config_content = tab
        config_window = self.config_canvas.create_window((0, 0), window=tab, anchor="nw")

        def update_scroll_region(_event: tk.Event[Any]) -> None:
            self.config_canvas.configure(scrollregion=self.config_canvas.bbox("all"))

        def fit_content_width(event: tk.Event[Any]) -> None:
            self.config_canvas.itemconfigure(config_window, width=event.width)

        tab.bind("<Configure>", update_scroll_region)
        self.config_canvas.bind("<Configure>", fit_content_width)
        self.bind("<MouseWheel>", self._scroll_config, add="+")

        ttk.Label(tab, text="基础", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        self.name_entry = self._field(tab, "供应商名称", self.name_var, 1, 0)
        self.id_entry = self._field(tab, "供应商 ID", self.id_var, 1, 1)
        self.id_entry.bind("<Key>", lambda _event: setattr(self, "_id_touched", True))
        ttk.Label(tab, text="Base URL", style="Field.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.base_url_entry = ttk.Entry(tab, textvariable=self.base_url_var)
        self.base_url_entry.grid(row=4, column=0, columnspan=2, sticky="ew", padx=(0, 16), pady=(0, 14))
        self.prefix_entry = self._field(tab, "模型前缀", self.prefix_var, 5, 0)
        self.prefix_entry.bind("<Key>", lambda _event: setattr(self, "_prefix_touched", True))
        enabled_wrap = ttk.Frame(tab, style="Surface.TFrame")
        enabled_wrap.grid(row=6, column=1, sticky="w", pady=(5, 14))
        self.enabled_check = ttk.Checkbutton(enabled_wrap, text="启用供应商", variable=self.enabled_var)
        self.enabled_check.pack(anchor="w")
        self.failover_check = ttk.Checkbutton(
            enabled_wrap, text="这家失败时自动换别家", variable=self.failover_var
        )
        self.failover_check.pack(anchor="w", pady=(4, 0))
        self.proto_responses_check = ttk.Checkbutton(
            enabled_wrap, text="说 Responses 协议（Codex App）", variable=self.proto_responses_var
        )
        self.proto_responses_check.pack(anchor="w", pady=(4, 0))
        self.proto_messages_check = ttk.Checkbutton(
            enabled_wrap, text="说 Messages 协议（Claude Desktop）", variable=self.proto_messages_var
        )
        self.proto_messages_check.pack(anchor="w")

        ttk.Separator(tab).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(2, 16))
        ttk.Label(tab, text="认证与端点", style="Section.TLabel").grid(row=8, column=0, columnspan=2, sticky="w", pady=(0, 14))
        ttk.Label(tab, text="API Key", style="Field.TLabel").grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 5))
        key_row = ttk.Frame(tab, style="Surface.TFrame")
        key_row.grid(row=10, column=0, columnspan=2, sticky="ew", padx=(0, 16), pady=(0, 14))
        key_row.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(key_row, textvariable=self.api_key_var, show="●")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key_button = ttk.Button(key_row, text="显示", width=7, command=self._toggle_key)
        self.show_key_button.grid(row=0, column=1, padx=(8, 0))
        self.models_path_entry = self._field(tab, "模型列表路径", self.models_path_var, 11, 0)
        self.responses_path_entry = self._field(tab, "Responses 路径", self.responses_path_var, 11, 1)
        self.messages_path_entry = self._field(tab, "Messages 路径", self.messages_path_var, 13, 0)
        self.auth_header_entry = self._field(tab, "认证请求头", self.auth_header_var, 15, 0)
        self.auth_prefix_entry = self._field(tab, "密钥前缀", self.auth_prefix_var, 15, 1)

        ttk.Label(tab, text="超时（秒）", style="Field.TLabel").grid(row=17, column=0, sticky="w", pady=(0, 5))
        self.timeout_spin = ttk.Spinbox(tab, from_=5, to=900, textvariable=self.timeout_var, width=12)
        self.timeout_spin.grid(row=18, column=0, sticky="ew", padx=(0, 16), pady=(0, 14))
        ttk.Label(tab, text="测试推理强度", style="Field.TLabel").grid(row=17, column=1, sticky="w", pady=(0, 5))
        self.reasoning_combo = ttk.Combobox(
            tab,
            textvariable=self.reasoning_var,
            values=("none", "low", "medium", "high", "xhigh", "max", "ultra"),
            state="readonly",
        )
        self.reasoning_combo.grid(row=18, column=1, sticky="ew", padx=(0, 16), pady=(0, 14))

        ttk.Label(tab, text="附加请求头（JSON）", style="Field.TLabel").grid(row=19, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.headers_text = tk.Text(
            tab,
            height=4,
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            bg=SURFACE,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Consolas", 10),
        )
        self.headers_text.grid(row=20, column=0, columnspan=2, sticky="nsew", padx=(0, 16), pady=(0, 14))
        action_row = ttk.Frame(tab, style="Surface.TFrame")
        action_row.grid(row=21, column=0, columnspan=2, sticky="w")
        self.connection_button = ttk.Button(action_row, text="测试连接", command=self._test_connection)
        self.connection_button.pack(side="left")
        self.fetch_button = ttk.Button(action_row, text="获取可用模型", command=self._fetch_models)
        self.fetch_button.pack(side="left", padx=(8, 0))
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(20, weight=1)

    def _scroll_config(self, event: tk.Event[Any]) -> str | None:
        if self.notebook.select() != str(self.config_tab):
            return None
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget is None or isinstance(widget, tk.Text):
            return None
        current: tk.Misc | None = widget
        while current is not None and current is not self.config_canvas:
            current = getattr(current, "master", None)
        if current is not self.config_canvas:
            return None
        delta = int(getattr(event, "delta", 0))
        if not delta:
            return None
        units = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)
        self.config_canvas.yview_scroll(units, "units")
        return "break"

    def _build_models_tab(self) -> None:
        tab = self.models_tab
        toolbar = ttk.Frame(tab, style="Surface.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(toolbar, textvariable=self.model_count_var, style="Section.TLabel").pack(side="left")
        self.add_model_button = ttk.Button(toolbar, text="添加模型", command=self._add_custom_model)
        self.add_model_button.pack(side="right")
        self.clear_models_button = ttk.Button(toolbar, text="全不选", command=lambda: self._set_all_models(False))
        self.clear_models_button.pack(side="right", padx=(0, 8))
        self.select_models_button = ttk.Button(toolbar, text="全选", command=lambda: self._set_all_models(True))
        self.select_models_button.pack(side="right", padx=(0, 8))
        filter_wrap = ttk.Frame(tab, style="Surface.TFrame")
        filter_wrap.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(filter_wrap, text="筛选", style="Field.TLabel").pack(side="left", padx=(0, 8))
        self.model_filter_entry = ttk.Entry(filter_wrap, textvariable=self.model_filter_var)
        self.model_filter_entry.pack(side="left", fill="x", expand=True)

        model_actions = ttk.Frame(tab, style="Surface.TFrame")
        model_actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.test_models_button = ttk.Button(model_actions, text="测试已勾选模型", command=self._test_selected_models)
        self.test_models_button.pack(side="left")
        self.measure_fast_button = ttk.Button(model_actions, text="测量 Fast 提速", command=self._measure_fast_selected_models)
        self.measure_fast_button.pack(side="left", padx=(8, 0))
        self.speed_button = ttk.Button(model_actions, text="切换 标准/快速", command=self._toggle_speed_selected)
        self.speed_button.pack(side="left", padx=(8, 0))
        self.rank_button = ttk.Button(model_actions, text="延迟排行", command=self._rank_model_latency)
        self.rank_button.pack(side="left", padx=(8, 0))
        self.remove_models_button = ttk.Button(
            model_actions,
            text="移除选中行",
            style="Danger.TButton",
            command=self._remove_selected_models,
        )
        self.remove_models_button.pack(side="left", padx=(8, 0))

        table = ttk.Frame(tab, style="Surface.TFrame")
        table.grid(row=2, column=0, sticky="nsew")
        columns = ("enabled", "model", "display", "status", "speed")
        self.model_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended")
        self.model_tree.heading("enabled", text="启用")
        self.model_tree.heading("model", text="模型 ID")
        self.model_tree.heading("display", text="显示名称")
        self.model_tree.heading("status", text="测试")
        self.model_tree.heading("speed", text="速度")
        self.model_tree.column("enabled", width=56, minwidth=56, anchor="center", stretch=False)
        self.model_tree.column("model", width=260, minwidth=180)
        self.model_tree.column("display", width=260, minwidth=160)
        self.model_tree.column("status", width=92, minwidth=80, anchor="center", stretch=False)
        self.model_tree.column("speed", width=84, minwidth=72, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.model_tree.yview)
        self.model_tree.configure(yscrollcommand=scroll.set)
        self.model_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.model_tree.bind("<Double-1>", self._toggle_model_event)
        self.model_tree.bind("<space>", self._toggle_model_event)
        self.model_tree.bind("<<TreeviewSelect>>", self._show_route_chain)
        self.model_tree.tag_configure("ready", foreground=SUCCESS)
        self.model_tree.tag_configure("failed", foreground=DANGER)
        self.model_tree.tag_configure("untested", foreground=MUTED)
        self.route_chain_var = tk.StringVar(value="选中一个模型可以看它的路由链")
        ttk.Label(tab, textvariable=self.route_chain_var, style="Field.TLabel").grid(
            row=4, column=0, sticky="w", pady=(10, 0)
        )
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

    def _show_route_chain(self, _event: tk.Event[Any] | None = None) -> None:
        """Spell out the vendors a request for the selected model will actually walk."""
        selection = self.model_tree.selection()
        if len(selection) != 1:
            self.route_chain_var.set("选中一个模型可以看它的路由链")
            return
        index = self._model_index_from_item(selection[0])
        if index is None or not 0 <= index < len(self.draft_models):
            self.route_chain_var.set("")
            return
        model = self.draft_models[index]
        if not model.get("enabled"):
            self.route_chain_var.set(f"路由链：{model['id']} 未启用，Codex 里看不到它")
            return
        provider = self.registry_snapshot_with_form()
        slug = str(provider.get("prefix") or "") + model["id"]
        chain = failover_chain(self._registry_for_routing(provider), slug)
        if not chain:
            self.route_chain_var.set(f"路由链：{slug} 目前不可路由（供应商或模型未启用）")
            return
        names = {p["id"]: p["name"] for p in self.registry.get("providers", [])}
        arrow = "  →  ".join(names.get(vendor, vendor) for vendor, _model in chain)
        speed = "快速" if model.get("fast_tier_forced") else "标准"
        tail = "（只有这一家；勾上「这家失败时自动换别家」才会有备用）" if len(chain) == 1 else ""
        self.route_chain_var.set(f"路由链：{slug}［{speed}］  {arrow}{tail}")

    def registry_snapshot_with_form(self) -> dict[str, Any]:
        """The provider as currently edited, falling back to the saved copy if the form is invalid."""
        try:
            return self._provider_from_form()
        except Exception:
            if not self.current_id:
                return {}
            try:
                saved = find_provider(self.registry, self.current_id)
            except KeyError:
                return {}
            return deepcopy(saved)

    def _registry_for_routing(self, edited: dict[str, Any]) -> dict[str, Any]:
        """Saved registry with the edited provider swapped in, so the preview matches the form."""
        registry = deepcopy(self.registry)
        if not edited.get("id"):
            return registry
        edited = deepcopy(edited)
        edited["models"] = deepcopy(self.draft_models)
        replaced = False
        for index, provider in enumerate(registry.get("providers", [])):
            if provider["id"] == edited["id"]:
                registry["providers"][index] = edited
                replaced = True
                break
        if not replaced:
            registry.setdefault("providers", []).append(edited)
        return registry

    def _build_tools_tab(self) -> None:
        tab = self.tools_tab
        metrics = ttk.Frame(tab, style="Surface.TFrame")
        metrics.pack(fill="x", pady=(0, 14))
        self.registry_metric = ttk.Label(metrics, text="注册表：检查中", style="Section.TLabel")
        self.registry_metric.pack(side="left")
        self.router_metric = ttk.Label(metrics, text="路由器：检查中", style="Section.TLabel")
        self.router_metric.pack(side="left", padx=(32, 0))
        self.traffic_metric = ttk.Label(metrics, text="", style="Section.TLabel")
        self.traffic_metric.pack(side="left", padx=(32, 0))
        self.lint_metric = ttk.Label(metrics, text="配置：检查中", style="Section.TLabel")
        self.lint_metric.pack(side="left", padx=(32, 0))

        tools = ttk.Frame(tab, style="Surface.TFrame")
        tools.pack(fill="x", pady=(0, 14))
        self.refresh_button = ttk.Button(tools, text="刷新状态", command=self._run_audit)
        self.refresh_button.pack(side="left")
        self.checkup_button = ttk.Button(tools, text="一键体检全部供应商", command=self._run_full_checkup)
        self.checkup_button.pack(side="left", padx=(8, 0))
        self.open_config_button = ttk.Button(tools, text="打开配置目录", command=lambda: self._open_path(self.workspace.root))
        self.open_config_button.pack(side="left", padx=(8, 0))
        self.open_registry_button = ttk.Button(tools, text="打开注册表", command=lambda: self._open_path(self.workspace.registry_path))
        self.open_registry_button.pack(side="left", padx=(8, 0))
        self.export_log_button = ttk.Button(tools, text="导出日志", command=self._export_log)
        self.export_log_button.pack(side="left", padx=(8, 0))
        self.clear_log_button = ttk.Button(tools, text="清空日志", command=self._clear_log)
        self.clear_log_button.pack(side="left", padx=(8, 0))
        self.shortcuts_button = ttk.Button(tools, text="快捷键", command=self._show_shortcuts)
        self.shortcuts_button.pack(side="left", padx=(8, 0))

        tools2 = ttk.Frame(tab, style="Surface.TFrame")
        tools2.pack(fill="x", pady=(0, 14))
        self.lint_button = ttk.Button(tools2, text="配置检查", command=lambda: self._run_lint(True))
        self.lint_button.pack(side="left", padx=(0, 8))
        self.restart_router_button = ttk.Button(tools2, text="重启路由器", command=self._restart_router_only)
        self.restart_router_button.pack(side="left")
        self.export_config_button = ttk.Button(tools2, text="导出配置（不含密钥）", command=self._export_config)
        self.export_config_button.pack(side="left", padx=(8, 0))
        self.backup_config_button = ttk.Button(tools2, text="备份配置", command=self._backup_config)
        self.backup_config_button.pack(side="left", padx=(8, 0))
        self.restore_config_button = ttk.Button(
            tools2, text="从备份恢复配置", command=self._restore_config
        )
        self.restore_config_button.pack(side="left", padx=(8, 0))

        ttk.Separator(tab).pack(fill="x", pady=(14, 12))
        ttk.Label(tab, text="Claude Desktop（第三方推理档）", style="Section.TLabel").pack(anchor="w")
        ttk.Label(tab, textvariable=self.claude_status_var, style="Field.TLabel").pack(
            anchor="w", pady=(4, 8)
        )
        claude_row = ttk.Frame(tab, style="Surface.TFrame")
        claude_row.pack(fill="x", pady=(0, 14))
        self.claude_publish_button = ttk.Button(
            claude_row, text="写入 Claude 档", command=self._publish_claude_profile
        )
        self.claude_publish_button.pack(side="left")
        self.claude_release_button = ttk.Button(
            claude_row, text="交还生效档", command=self._release_claude_slot
        )
        self.claude_release_button.pack(side="left", padx=(8, 0))
        self.claude_entry_combo = ttk.Combobox(claude_row, width=22, state="readonly")
        self.claude_entry_combo.pack(side="left", padx=(12, 6))
        self.claude_switch_button = ttk.Button(
            claude_row, text="切到这个档", command=self._switch_claude_entry
        )
        self.claude_switch_button.pack(side="left")
        self.claude_refresh_button = ttk.Button(
            claude_row, text="刷新", command=self._refresh_claude_status
        )
        self.claude_refresh_button.pack(side="left", padx=(8, 0))

        ttk.Label(tab, text="供应商健康（取自路由器日志最近 200 条请求）", style="Section.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        health_frame = ttk.Frame(tab, style="Surface.TFrame")
        health_frame.pack(fill="x", pady=(0, 14))
        health_columns = ("vendor", "requests", "rate", "p50", "p95", "last")
        self.health_tree = ttk.Treeview(
            health_frame, columns=health_columns, show="headings", height=7, selectmode="none"
        )
        for column, title, width, anchor in (
            ("vendor", "供应商", 190, "w"),
            ("requests", "请求数", 84, "center"),
            ("rate", "成功率", 92, "center"),
            ("p50", "p50 延迟", 100, "center"),
            ("p95", "p95 延迟", 100, "center"),
            ("last", "最后一次", 210, "w"),
        ):
            self.health_tree.heading(column, text=title)
            self.health_tree.column(column, width=width, minwidth=width - 20, anchor=anchor, stretch=False)
        health_scroll = ttk.Scrollbar(health_frame, orient="vertical", command=self.health_tree.yview)
        self.health_tree.configure(yscrollcommand=health_scroll.set)
        self.health_tree.pack(side="left", fill="x", expand=True)
        health_scroll.pack(side="right", fill="y")
        self.health_tree.tag_configure("good", foreground=SUCCESS)
        self.health_tree.tag_configure("warn", foreground=WARNING)
        self.health_tree.tag_configure("bad", foreground=DANGER)

        ttk.Label(tab, text="运行记录", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.log_text = tk.Text(
            tab,
            state="disabled",
            wrap="word",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            bg="#FAFBFC",
            fg=TEXT,
            insertbackground=TEXT,
            font=("Microsoft YaHei UI", 9),
        )
        self.log_text.pack(fill="both", expand=True)

    def _build_usage_tab(self) -> None:
        """Spend and traffic, read out of the same router log the health board uses.

        Reading only: nothing on this tab touches providers.json, and the price table it
        multiplies by lives in its own file so a wrong number here can never break a launch.
        """
        tab = self.usage_tab
        self.usage_total_var = tk.StringVar(value="全部：统计中")
        self.usage_today_var = tk.StringVar(value="今日：统计中")
        self.usage_span_var = tk.StringVar(value="")
        self.usage_note_var = tk.StringVar(value="")

        metrics = ttk.Frame(tab, style="Surface.TFrame")
        metrics.pack(fill="x", pady=(0, 10))
        ttk.Label(metrics, textvariable=self.usage_today_var, style="Section.TLabel").pack(
            side="left"
        )
        ttk.Label(metrics, textvariable=self.usage_total_var, style="Section.TLabel").pack(
            side="left", padx=(32, 0)
        )
        ttk.Label(tab, textvariable=self.usage_span_var, style="Field.TLabel").pack(
            anchor="w", pady=(0, 12)
        )

        tools = ttk.Frame(tab, style="Surface.TFrame")
        tools.pack(fill="x", pady=(0, 14))
        self.usage_refresh_button = ttk.Button(
            tools, text="刷新用量", command=lambda: self._refresh_usage(True)
        )
        self.usage_refresh_button.pack(side="left")
        self.usage_price_button = ttk.Button(
            tools, text="编辑价格表", command=self._edit_usage_prices
        )
        self.usage_price_button.pack(side="left", padx=(8, 0))
        self.usage_open_price_button = ttk.Button(
            tools,
            text="打开价格文件",
            command=lambda: self._open_path(self.workspace.usage_prices_path),
        )
        self.usage_open_price_button.pack(side="left", padx=(8, 0))
        self.usage_export_button = ttk.Button(
            tools, text="导出用量 CSV", command=self._export_usage_csv
        )
        self.usage_export_button.pack(side="left", padx=(8, 0))

        ttk.Label(tab, text="按模型", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        model_frame = ttk.Frame(tab, style="Surface.TFrame")
        model_frame.pack(fill="both", expand=True, pady=(0, 14))
        self.usage_model_tree = self._usage_tree(
            model_frame,
            (
                ("model", "模型", 200, "w"),
                ("vendors", "供应商", 150, "w"),
                ("requests", "请求数", 80, "center"),
                ("tokens_in", "输入 token", 104, "e"),
                ("tokens_out", "输出 token", 104, "e"),
                ("cost", "花费", 104, "e"),
                ("priced", "计价", 60, "center"),
            ),
        )

        ttk.Label(tab, text="按供应商", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        vendor_frame = ttk.Frame(tab, style="Surface.TFrame")
        vendor_frame.pack(fill="both", expand=True, pady=(0, 10))
        self.usage_vendor_tree = self._usage_tree(
            vendor_frame,
            (
                ("vendor", "供应商", 200, "w"),
                ("requests", "请求数", 80, "center"),
                ("rate", "成功率", 84, "center"),
                ("p50", "p50 延迟", 96, "center"),
                ("p95", "p95 延迟", 96, "center"),
                ("tokens", "token 合计", 116, "e"),
                ("cost", "花费", 104, "e"),
            ),
        )
        ttk.Label(tab, textvariable=self.usage_note_var, style="Field.TLabel", wraplength=880).pack(
            anchor="w"
        )

    def _on_tab_changed(self, _event: Any = None) -> None:
        """Repaint the usage tab when it comes into view, so it is never showing stale spend.

        Silent and cheap: one tail read of a local file, and skipped while a task holds the UI.
        """
        if self._closing or self._busy:
            return
        try:
            if self.notebook.select() == str(self.usage_tab):
                self._refresh_usage()
        except tk.TclError:
            pass

    @staticmethod
    def _usage_tree(
        parent: ttk.Frame, columns: tuple[tuple[str, str, int, str], ...]
    ) -> ttk.Treeview:
        """A scrolled, read-only Treeview — the same shape the health board uses."""
        names = tuple(column[0] for column in columns)
        tree = ttk.Treeview(parent, columns=names, show="headings", height=6, selectmode="none")
        for column, title, width, anchor in columns:
            tree.heading(column, text=title)
            tree.column(
                column, width=width, minwidth=max(48, width - 24), anchor=anchor, stretch=False
            )
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.tag_configure("good", foreground=SUCCESS)
        tree.tag_configure("warn", foreground=WARNING)
        tree.tag_configure("bad", foreground=DANGER)
        return tree

    def _bind_shortcuts(self) -> None:
        """Accelerators for the actions that get used every session."""
        for sequence, handler in (
            ("<Control-s>", lambda _e: self._save_current()),
            ("<Control-S>", lambda _e: self._save_current()),
            ("<F5>", lambda _e: self._run_audit()),
            ("<Control-r>", lambda _e: self._reload_current()),
            ("<Control-t>", lambda _e: self._test_selected_models()),
            ("<Control-e>", lambda _e: self._run_full_checkup()),
            ("<Control-Return>", lambda _e: self._launch_codex()),
            ("<Control-f>", lambda _e: self._focus_filter()),
        ):
            self.bind_all(sequence, handler)

    def _focus_filter(self) -> None:
        self.notebook.select(self.models_tab)
        self.model_filter_entry.focus_set()
        self.model_filter_entry.select_range(0, "end")

    def _switch_workspace(self) -> None:
        """Point the whole window at the other config root.

        Refuses mid-task so a background worker cannot finish against a registry that is no
        longer the one on screen, and drops the current selection because provider ids are
        only meaningful inside one workspace.
        """
        wanted = WORKSPACES.get(self.workspace_var.get(), CODEX)
        if wanted is self.workspace:
            return
        if self._busy:
            self.workspace_var.set(self.workspace.name)
            messagebox.showinfo("正在忙", "有任务在跑，等它结束再切换工作区。", parent=self)
            return
        self.workspace = wanted
        self.current_id = None
        self.loaded_provider = None
        self.draft_models = []
        # Blank the editor before loading the other root. Leaving the previous workspace's
        # provider in the form makes every action think there are unsaved changes, and a
        # stray 保存并应用 would write that provider into the workspace it does not belong to.
        self._reset_editor()
        self.launch_button.configure(text="启动 Claude" if wanted is CLAUDE else "启动 Codex")
        self._append_log(f"已切到「{wanted.label}」工作区：{wanted.root}")
        try:
            self._load_registry()
        except Exception as error:
            self._show_error(f"无法载入 {wanted.label} 配置", error)
            return
        self._run_audit()
        # Usage is per workspace too: the Claude router keeps its own log and its own prices.
        self._refresh_usage()

    def _reset_editor(self) -> None:
        """Clear every editor field so nothing from the previous provider leaks forward."""
        self._id_touched = False
        self._prefix_touched = False
        for variable in (
            self.name_var, self.id_var, self.base_url_var, self.prefix_var, self.api_key_var
        ):
            variable.set("")
        self.models_path_var.set("/models")
        self.responses_path_var.set("/responses")
        self.messages_path_var.set("/v1/messages")
        self.auth_header_var.set("Authorization")
        self.auth_prefix_var.set("Bearer ")
        self.timeout_var.set("120")
        self.enabled_var.set(True)
        self.failover_var.set(False)
        self.proto_responses_var.set(self.workspace is not CLAUDE)
        self.proto_messages_var.set(self.workspace is CLAUDE)
        self._set_key_revealed(False)
        self.headers_text.configure(state="normal")
        self.headers_text.delete("1.0", "end")
        self.headers_text.insert("1.0", "{}")
        self._render_models()

    def _initial_load(self) -> None:
        try:
            self._load_registry()
            self._append_log("供应商注册表已载入。")
            self._run_audit()
        except Exception as error:
            self._show_error("无法载入 codex-sota 配置", error)
        self._refresh_usage()
        self.after(HEALTH_REFRESH_MS, self._tick_health)

    def _load_registry(self, select_id: str | None = None) -> None:
        # Keep the editor usable when an old provider's key file is missing. The audit board
        # reports the credential problem, while saving that provider with a replacement key
        # still performs the strict final validation.
        self.registry = load_registry(
            self.workspace.registry_path, allow_missing_secrets=True
        )
        current = select_id or self.current_id
        self.provider_ids = [provider["id"] for provider in self.registry["providers"]]
        visible = self._render_providers()
        if current not in visible:
            current = visible[0] if visible else None
        if current:
            self.provider_tree.selection_set(current)
            self.provider_tree.focus(current)
            self.provider_tree.see(current)
            self._load_provider(current)
        self._run_lint()
        self._refresh_claude_status()

    def _render_providers(self) -> list[str]:
        """Repaint the sidebar, honouring the search box. Returns the ids actually shown.

        The filter only hides rows; provider_ids stays the full registry order because that
        order is what failover walks, and hiding a row must not silently reorder anything.
        """
        query = self.provider_filter_var.get().strip().lower()
        self.provider_tree.delete(*self.provider_tree.get_children())
        visible: list[str] = []
        for provider in self.registry.get("providers", []):
            haystack = f"{provider['name']} {provider['id']} {provider.get('prefix') or ''}".lower()
            models = [model for model in provider["models"] if model["enabled"]]
            if query and query not in haystack:
                if not any(query in model["id"].lower() for model in provider["models"]):
                    continue
            label = provider["name"] + ("  · 已停用" if not provider["enabled"] else "")
            self.provider_tree.insert(
                "", "end", iid=provider["id"], text=label, values=(len(models),)
            )
            visible.append(provider["id"])
        total = len(self.registry.get("providers", []))
        self.provider_count_var.set(
            f"{len(visible)} / {total} 家" if query else f"共 {total} 家"
        )
        return visible

    def _on_provider_filter(self, *_args: Any) -> None:
        visible = self._render_providers()
        if self.current_id and self.current_id in visible:
            self.provider_tree.selection_set(self.current_id)
            self.provider_tree.see(self.current_id)

    def _provider_selected(self, _event: tk.Event[Any] | None = None) -> None:
        if self._busy:
            if (
                self.current_id
                and self.provider_tree.exists(self.current_id)
                and self.provider_tree.selection() != (self.current_id,)
            ):
                self.provider_tree.selection_set(self.current_id)
            return
        selection = self.provider_tree.selection()
        if selection:
            self._load_provider(selection[0])

    def _load_provider(self, provider_id: str) -> None:
        try:
            provider = deepcopy(find_provider(self.registry, provider_id))
        except KeyError:
            # The registry may have been edited by another manager instance between painting
            # the tree and the click. Reload instead of surfacing a raw traceback in the UI.
            self.current_id = None
            self._load_registry()
            return
        self.current_id = provider_id
        self.loaded_provider = deepcopy(provider)
        self._id_touched = True
        self._prefix_touched = True
        self.name_var.set(provider["name"])
        self.id_var.set(provider["id"])
        self.base_url_var.set(provider["base_url"])
        self.prefix_var.set(provider["prefix"])
        self.api_key_var.set("")
        self._set_key_revealed(False)
        self.models_path_var.set(provider["models_path"])
        self.responses_path_var.set(provider["responses_path"])
        self.messages_path_var.set(provider.get("messages_path") or "/v1/messages")
        self.auth_header_var.set(provider["auth_header"])
        self.auth_prefix_var.set(provider["auth_prefix"])
        self.timeout_var.set(str(provider["timeout_seconds"]))
        self.enabled_var.set(provider["enabled"])
        self.failover_var.set(bool(provider.get("allow_failover")))
        protocols = provider.get("protocols") or ["responses"]
        self.proto_responses_var.set("responses" in protocols)
        self.proto_messages_var.set("messages" in protocols)
        self.headers_text.delete("1.0", "end")
        self.headers_text.insert("1.0", json.dumps(provider.get("extra_headers") or {}, ensure_ascii=False, indent=2))
        self.draft_models = deepcopy(provider["models"])
        self.model_filter_var.set("")
        self._render_models()
        protected = bool(provider.get("protected"))
        self._set_editor_protected(protected)
        self.config_canvas.yview_moveto(0.0)
        self.status_var.set(provider["name"])

    def _set_editor_protected(self, protected: bool) -> None:
        self._protected = protected
        state = "disabled" if protected or self._busy else "normal"
        for widget in (
            self.name_entry,
            self.id_entry,
            self.base_url_entry,
            self.prefix_entry,
            self.api_key_entry,
            self.models_path_entry,
            self.responses_path_entry,
            self.messages_path_entry,
            self.auth_header_entry,
            self.auth_prefix_entry,
            self.timeout_spin,
        ):
            widget.configure(state=state)
        self.enabled_check.configure(state=state)
        self.failover_check.configure(state=state)
        self.proto_responses_check.configure(state=state)
        self.proto_messages_check.configure(state=state)
        self.headers_text.configure(state=state)
        self.show_key_button.configure(state=state)
        edit_models_state = "disabled" if protected or self._busy else "normal"
        for widget in (
            self.add_model_button,
            self.select_models_button,
            self.clear_models_button,
            self.remove_models_button,
        ):
            widget.configure(state=edit_models_state)
        if protected or self._busy:
            self.model_tree.state(["disabled"])
        else:
            self.model_tree.state(["!disabled"])
        probe_state = "disabled" if self._busy else "normal"
        self.fetch_button.configure(state=probe_state)
        self.connection_button.configure(state=probe_state)
        self.test_models_button.configure(state=probe_state)
        self.measure_fast_button.configure(state=probe_state)
        self.speed_button.configure(state=probe_state)
        self.rank_button.configure(state=probe_state)
        self.reasoning_combo.configure(state="disabled" if self._busy else "readonly")
        self.save_button.configure(state="disabled" if protected or self._busy else "normal")
        self.delete_button.configure(state="disabled" if protected or self._busy else "normal")

    def _new_provider(self) -> None:
        if self._busy:
            return
        self.current_id = None
        self.loaded_provider = None
        self.provider_tree.selection_remove(self.provider_tree.selection())
        self._id_touched = False
        self._prefix_touched = False
        self.name_var.set("")
        self.id_var.set("")
        self.base_url_var.set("")
        self.prefix_var.set("")
        self.api_key_var.set("")
        self._set_key_revealed(False)
        self.models_path_var.set("/models")
        self.responses_path_var.set("/responses")
        self.messages_path_var.set("/v1/messages")
        self.auth_header_var.set("Authorization")
        self.auth_prefix_var.set("Bearer ")
        self.timeout_var.set("120")
        self.enabled_var.set(True)
        self.failover_var.set(False)
        self.proto_responses_var.set(self.workspace is not CLAUDE)
        self.proto_messages_var.set(self.workspace is CLAUDE)
        self.headers_text.configure(state="normal")
        self.headers_text.delete("1.0", "end")
        self.headers_text.insert("1.0", "{}")
        self.draft_models = []
        self._render_models()
        self._set_editor_protected(False)
        self.id_entry.configure(state="normal")
        self.config_canvas.yview_moveto(0.0)
        self.name_entry.focus_set()
        self.status_var.set("新供应商")
        self.notebook.select(self.config_tab)

    def _auto_identity(self, *_args: Any) -> None:
        if self.current_id is not None:
            return
        identity = slugify(self.name_var.get()) if self.name_var.get().strip() else ""
        if identity and not self._id_touched:
            self.id_var.set(identity)
        if identity and not self._prefix_touched:
            # Protocol-dependent: a messages-only provider needs the dotted form to keep
            # Claude Desktop's thinking-effort control, so the suggestion follows the two
            # protocol checkboxes as well as the name.
            self.prefix_var.set(derive_model_prefix(identity, self._draft_protocols()))

    def _draft_protocols(self) -> list[str]:
        return draft_protocols(
            self.proto_responses_var.get(), self.proto_messages_var.get(), self.workspace
        )

    def _toggle_key(self) -> None:
        self._set_key_revealed(self.api_key_entry.cget("show") != "")

    def _set_key_revealed(self, revealed: bool) -> None:
        self.api_key_entry.configure(show="" if revealed else "●")
        self.show_key_button.configure(text="隐藏" if revealed else "显示")

    def _enabled_default_exists(self) -> bool:
        """Whether this workspace already has an enabled default provider."""
        return any(
            provider.get("enabled") and provider.get("is_default")
            for provider in self.registry.get("providers", [])
        )

    def _disk_provider(self, provider_id: str) -> dict[str, Any] | None:
        """The provider as providers.json has it right now, or None if it cannot be read."""
        try:
            fresh = load_registry(self.workspace.registry_path, allow_missing_secrets=True)
            return deepcopy(find_provider(fresh, provider_id))
        except (KeyError, OSError, ValueError):
            return None

    def _outside_edits(self, provider_id: str) -> list[str]:
        """Form-owned fields that changed on disk since the editor was populated.

        These are the ones a save would overwrite with whatever the editor happens to show, so
        the user gets told instead of losing the change.  Fields the editor does not own are
        already carried over from disk by _provider_from_form and need no warning.
        """
        baseline = self.loaded_provider
        current = self._disk_provider(provider_id)
        if not baseline or not current or baseline.get("id") != provider_id:
            return []
        labels = {
            "name": "显示名",
            "base_url": "Base URL",
            "prefix": "模型前缀",
            "enabled": "启用状态",
            "allow_failover": "自动换家",
            "protocols": "协议",
            "auth_header": "认证头",
            "auth_prefix": "认证前缀",
            "models_path": "models 路径",
            "responses_path": "responses 路径",
            "messages_path": "messages 路径",
            "timeout_seconds": "超时",
            "extra_headers": "附加请求头",
            "models": "模型清单",
        }
        drifted = []
        for key in FORM_OWNED_PROVIDER_KEYS:
            before, after = baseline.get(key), current.get(key)
            if key == "models":
                # Probe bookkeeping is written by this app between load and save, so comparing
                # it raw would flag a drift on every provider the checkup has touched.
                before = comparable_provider({"models": before or []})["models"]
                after = comparable_provider({"models": after or []})["models"]
            if before != after:
                drifted.append(labels.get(key, key))
        return drifted

    def _provider_from_form(self) -> dict[str, Any]:
        provider_id = self.id_var.get().strip().lower()
        existing = None
        if self.current_id:
            # Merge onto the provider as it is on disk right now, not onto the copy the editor
            # was filled from.  A second manager instance, a hand edit, or a key this version
            # does not know about would otherwise be silently reverted by every save.
            existing = self._disk_provider(self.current_id)
            if existing is None:
                try:
                    existing = deepcopy(find_provider(self.registry, self.current_id))
                except KeyError:
                    # Treat a provider removed by another instance as a new draft; the save path
                    # will validate the complete candidate before writing anything.
                    existing = None
        try:
            extra_headers = json.loads(self.headers_text.get("1.0", "end").strip() or "{}")
        except json.JSONDecodeError as error:
            raise ValueError("附加请求头不是有效 JSON。") from error
        # A registry needs exactly one enabled default, and a default must carry an empty
        # prefix. In a brand new workspace nothing holds that role, so the first provider
        # saved takes it — otherwise the very first save can never pass validation.
        first_in_workspace = existing is None and not self._enabled_default_exists()
        provider = existing or {
            "id": provider_id,
            "protected": False,
            "is_default": first_in_workspace,
            "auth_type": "dpapi",
            "secret_file": provider_id + "-api-key.dpapi",
            "entropy": "CodexSota.Provider." + provider_id + ".v1",
        }
        prefix = "" if first_in_workspace else self.prefix_var.get().strip()
        provider.update(
            {
                "id": provider_id,
                "name": self.name_var.get().strip(),
                "base_url": self.base_url_var.get().strip(),
                "prefix": prefix,
                "workspace": self.workspace.name,
                "enabled": self.enabled_var.get(),
                "allow_failover": self.failover_var.get(),
                "protocols": draft_protocols(
                    self.proto_responses_var.get(),
                    self.proto_messages_var.get(),
                    self.workspace,
                ),
                "auth_header": self.auth_header_var.get().strip(),
                "auth_prefix": self.auth_prefix_var.get(),
                "models_path": self.models_path_var.get().strip(),
                "responses_path": self.responses_path_var.get().strip(),
                "messages_path": self.messages_path_var.get().strip(),
                "timeout_seconds": int(self.timeout_var.get().strip()),
                "extra_headers": extra_headers,
                "models": deepcopy(self.draft_models),
            }
        )
        return validate_provider(provider, allow_missing_secret=True)

    def _render_models(self) -> None:
        if not hasattr(self, "model_tree"):
            return
        selected_ids = {self.model_tree.item(item, "values")[1] for item in self.model_tree.selection() if self.model_tree.exists(item)}
        self.model_tree.delete(*self.model_tree.get_children())
        query = self.model_filter_var.get().strip().lower()
        enabled_count = 0
        for index, model in enumerate(self.draft_models):
            if model.get("enabled"):
                enabled_count += 1
            display = str(model.get("display_name") or "")
            if query and query not in model["id"].lower() and query not in display.lower():
                continue
            status = model.get("last_test_status") or "untested"
            status_text = {"ready": "可用", "failed": "失败", "untested": "未测试"}.get(status, "未测试")
            iid = "model_" + str(index)
            self.model_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "✓" if model.get("enabled") else "",
                    model["id"],
                    display,
                    status_text,
                    speed_label(model),
                ),
                tags=(status,),
            )
            if model["id"] in selected_ids:
                self.model_tree.selection_add(iid)
        self.model_count_var.set(f"{enabled_count} 个已启用 / {len(self.draft_models)} 个可选")
        self._show_route_chain()

    def _model_index_from_item(self, item: str) -> int | None:
        if item.startswith("model_"):
            try:
                return int(item.split("_", 1)[1])
            except ValueError:
                return None
        return None

    def _toggle_model_event(self, event: tk.Event[Any]) -> str:
        if self._busy or self._protected:
            return "break"
        item = self.model_tree.identify_row(event.y) if event.type == tk.EventType.ButtonPress else self.model_tree.focus()
        index = self._model_index_from_item(item)
        if index is not None and 0 <= index < len(self.draft_models):
            self.draft_models[index]["enabled"] = not bool(self.draft_models[index].get("enabled"))
            self._render_models()
            if self.model_tree.exists(item):
                self.model_tree.selection_set(item)
        return "break"

    def _set_all_models(self, enabled: bool) -> None:
        if self._busy or self._protected:
            return
        query = self.model_filter_var.get().strip().lower()
        for model in self.draft_models:
            display = str(model.get("display_name") or "")
            if not query or query in model["id"].lower() or query in display.lower():
                model["enabled"] = enabled
        self._render_models()

    def _add_custom_model(self) -> None:
        if self._busy or self._protected:
            return
        dialog = ModelDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        if any(model["id"] == dialog.result["id"] for model in self.draft_models):
            messagebox.showwarning("模型已存在", "该模型 ID 已在列表中。", parent=self)
            return
        self.draft_models.append(dialog.result)
        self._render_models()

    def _remove_selected_models(self) -> None:
        if self._busy or self._protected:
            return
        indices = sorted(
            [index for item in self.model_tree.selection() if (index := self._model_index_from_item(item)) is not None],
            reverse=True,
        )
        for index in indices:
            if 0 <= index < len(self.draft_models):
                self.draft_models.pop(index)
        self._render_models()

    def _probe_key(self) -> str | None:
        value = self.api_key_var.get().strip()
        return value or None

    def _test_connection(self) -> None:
        try:
            provider = self._provider_from_form()
            key = self._probe_key()
        except Exception as error:
            self._show_error("配置无效", error)
            return

        def worker() -> dict[str, Any]:
            return discover_models(provider, key)

        def success(result: dict[str, Any]) -> None:
            if result.get("ok"):
                self.models_path_var.set(result["models_path"])
                self._append_log(f"{provider['name']} 连接成功，发现 {len(result['models'])} 个模型。")
                messagebox.showinfo("连接可用", f"HTTP {result['status']} · {len(result['models'])} 个模型", parent=self)
            else:
                raise RuntimeError(self._probe_failure_text(result))

        self._run_task("正在测试连接", worker, success, [key])

    def _fetch_models(self) -> None:
        try:
            provider = self._provider_from_form()
            key = self._probe_key()
        except Exception as error:
            self._show_error("配置无效", error)
            return

        def worker() -> dict[str, Any]:
            found = discover_models(provider, key)
            if not found.get("ok"):
                return {"models": found, "endpoint": None}
            # The model list path is the strongest clue for where inference lives. Repair the
            # protocol this workspace will actually use before the provider is first saved.
            probe = deepcopy(provider)
            probe["models_path"] = found["models_path"]
            probe["models"] = [
                {"id": model_id, "enabled": True} for model_id in found["models"][:1]
            ] or provider.get("models")
            return {"models": found, "endpoint": auto_repair_active_inference_path(probe, key)}

        def success(payload: dict[str, Any]) -> None:
            result = payload["models"]
            if not result.get("ok"):
                raise RuntimeError(self._probe_failure_text(result))
            repair = payload.get("endpoint") or {}
            if repair.get("changed"):
                if repair.get("protocol") == "messages":
                    self.messages_path_var.set(repair["after"])
                    label = "Messages"
                else:
                    self.responses_path_var.set(repair["after"])
                    label = "Responses"
                self._append_log(f"已自动修正 {label} 路径 — " + repair["reason"])
            self.models_path_var.set(result["models_path"])
            existing = {model["id"]: model for model in self.draft_models}
            merged: list[dict[str, Any]] = []
            for model_id in result["models"]:
                merged.append(
                    existing.get(model_id)
                    or {
                        "id": model_id,
                        "enabled": False,
                        "display_name": "",
                        "description": "",
                        "last_test_status": "untested",
                        "fast_tier_status": "unknown",
                        "fast_tier_effect": "untested",
                        "fast_tier_forced": False,
                    }
                )
            for model in self.draft_models:
                if model["id"] not in result["models"]:
                    merged.append(model)
            self.draft_models = merged
            self.model_filter_var.set("")
            self._render_models()
            self.notebook.select(self.models_tab)
            self._append_log(f"{provider['name']} 模型列表已更新，共 {len(result['models'])} 个远端模型。")

        self._run_task("正在获取模型", worker, success, [key])

    @staticmethod
    def _probe_failure_text(result: dict[str, Any]) -> str:
        errors = result.get("errors") or []
        if not errors:
            return "供应商没有返回可用模型。"
        last = errors[-1]
        status = last.get("status")
        detail = last.get("detail") or last.get("error") or "未知错误"
        return f"模型接口测试失败（{status or '网络错误'}）：{detail}"

    def _test_selected_models(self) -> None:
        if self._blank_editor():
            messagebox.showinfo(
                "还没有供应商",
                f"「{self.workspace.label}」里还没有供应商。先在「供应商配置」填好名称、"
                "Base URL、API Key 并保存，再回来测试模型。",
                parent=self,
            )
            return
        try:
            provider = self._provider_from_form()
            key = self._probe_key()
            reasoning = self.reasoning_var.get()
        except Exception as error:
            self._show_error("配置无效", error)
            return
        targets = [model["id"] for model in self.draft_models if model.get("enabled")]
        if not targets:
            selected = []
            for item in self.model_tree.selection():
                index = self._model_index_from_item(item)
                if index is not None and 0 <= index < len(self.draft_models):
                    selected.append(self.draft_models[index]["id"])
            targets = selected
        if not targets:
            messagebox.showwarning("没有测试目标", "请先勾选或选中模型。", parent=self)
            return

        def worker() -> dict[str, Any]:
            repair = auto_repair_active_inference_path(provider, key)
            persisted = False
            # A newly drafted provider is not in the registry yet.  Avoid
            # find_provider() here because it intentionally raises KeyError
            # for that normal, unsaved form state.
            provider_is_persisted = any(
                str(entry.get("id")) == str(provider.get("id"))
                for entry in self.registry.get("providers", [])
                if isinstance(entry, dict)
            )
            if repair["changed"] and not provider.get("protected") and provider_is_persisted:
                try:
                    apply_provider(provider, restart=False, workspace=self.workspace)
                    persisted = True
                except Exception as error:
                    repair["reason"] += f"（自动保存失败：{error}）"
            outcome: list[dict[str, Any]] = []
            for model_id in targets:
                result = test_model(provider, model_id, key, reasoning)
                result["model"] = model_id
                tier: dict[str, Any] = {"verdict": "unknown"}
                if result["ok"]:
                    try:
                        tier = probe_fast_tier(provider, model_id, key, reasoning)
                    except Exception as error:
                        tier = {"verdict": "unknown", "detail": str(error)}
                outcome.append({"test": result, "tier": tier})
            return {"models": outcome, "repair": repair, "persisted": persisted}

        def success(payload: dict[str, Any]) -> None:
            results = payload["models"]
            repair = payload["repair"]
            if repair["changed"]:
                if repair.get("protocol") == "messages":
                    self.messages_path_var.set(repair["after"])
                    label = "Messages"
                else:
                    self.responses_path_var.set(repair["after"])
                    label = "Responses"
                self._append_log(f"已自动修正 {label} 路径 — " + repair["reason"])
                if not payload["persisted"]:
                    self._append_log("    这条修正还没写盘，记得点「保存并应用」")
            by_id = {entry["test"]["model"]: entry for entry in results}
            ready = 0
            accepts_fast = 0
            for model in self.draft_models:
                entry = by_id.get(model["id"])
                if entry is None:
                    continue
                result = entry["test"]
                model["last_test_status"] = "ready" if result["ok"] else "failed"
                model["last_test_at"] = utc_now()
                model["last_test_message"] = result.get("detail") or ("HTTP " + str(result.get("status")))
                verdict = entry["tier"].get("verdict")
                model["fast_tier_status"] = (
                    verdict if verdict in {"supported", "unsupported"} else "unknown"
                )
                if model["fast_tier_status"] != "supported":
                    model["fast_tier_effect"] = "untested"
                elif not model.get("fast_tier_effect"):
                    model["fast_tier_effect"] = "untested"
                if result["ok"]:
                    ready += 1
                if model["fast_tier_status"] == "supported":
                    accepts_fast += 1
                self._append_log(
                    f"{provider['name']} / {model['id']}：{'可用' if result['ok'] else '失败'}"
                    f"（{result.get('status') or '网络错误'}），service_tier "
                    f"{'可用' if model['fast_tier_status'] == 'supported' else '未确认'}"
                )
            self._render_models()
            messagebox.showinfo(
                "模型测试完成",
                f"{ready} / {len(results)} 个模型可用\n"
                f"{accepts_fast} 个上游收下了 service_tier=priority\n"
                + (
                    f"\nResponses 路径已自动修正：{repair['before']} → {repair['after']}"
                    + ("（已写盘）" if payload["persisted"] else "（待你保存）")
                    + "\n"
                    if repair["changed"]
                    else ""
                )
                + "\n想让某个模型走 fast，在列表里选中它，点「切换 标准/快速」，"
                "然后「保存并应用」。收下不代表真的更快。",
                parent=self,
            )

        self._run_task("正在测试模型", worker, success, [key])

    def _toggle_speed_selected(self) -> None:
        if self._busy:
            return
        indexes = [
            index
            for item in self.model_tree.selection()
            if (index := self._model_index_from_item(item)) is not None
            and 0 <= index < len(self.draft_models)
        ]
        if not indexes:
            messagebox.showwarning(
                "没有选中模型", "请先在列表里选中要切换速度的模型（可多选）。", parent=self
            )
            return
        for index in indexes:
            model = self.draft_models[index]
            model["fast_tier_forced"] = not model.get("fast_tier_forced")
            self._append_log(
                f"{model['id']}：速度 → {speed_label(model)}"
                + (fast_tier_note(model) if model["fast_tier_forced"] else "")
            )
        self._render_models()
        self.status_var.set("速度已改，点「保存并应用」写入，下次启动 Codex 生效")

    def _measure_fast_selected_models(self) -> None:
        if self._blank_editor():
            messagebox.showinfo(
                "还没有供应商",
                f"「{self.workspace.label}」里还没有供应商。先在「供应商配置」填好名称、"
                "Base URL、API Key 并保存，再回来测速。",
                parent=self,
            )
            return
        try:
            provider = self._provider_from_form()
            key = self._probe_key()
        except Exception as error:
            self._show_error("配置无效", error)
            return
        targets = [
            model["id"]
            for model in self.draft_models
            if model.get("fast_tier_status") == "supported"
        ]
        if not targets:
            messagebox.showwarning(
                "没有可测量的模型",
                "只有先被「测试已勾选模型」判定为接受 service_tier=priority 的模型才能测速。",
                parent=self,
            )
            return
        if not messagebox.askokcancel(
            "测量 Fast 提速",
            f"将对 {len(targets)} 个模型各发 {FAST_MEASURE_PAIRS} 对交错请求"
            f"（共约 {len(targets) * FAST_MEASURE_PAIRS * 2} 次调用，会消耗额度），"
            f"每个模型约需 1 分钟。继续？",
            parent=self,
        ):
            return

        def worker() -> list[dict[str, Any]]:
            return [
                measure_fast_tier(provider, model_id, FAST_MEASURE_PAIRS, key)
                for model_id in targets
            ]

        def success(results: list[dict[str, Any]]) -> None:
            by_id = {result["model"]: result for result in results}
            faster = 0
            for model in self.draft_models:
                result = by_id.get(model["id"])
                if result is None:
                    continue
                verdict = result.get("verdict")
                model["fast_tier_effect"] = (
                    verdict if verdict in {"faster", "none"} else "untested"
                )
                if verdict == "faster":
                    faster += 1
                if result.get("pairs", 0) >= 4:
                    self._append_log(
                        f"{provider['name']} / {model['id']}：标准 "
                        f"{result['standard_median_seconds']}s，配对差中位 "
                        f"{result['median_delta_seconds']:+}s（{result['median_delta_percent']:+}%），"
                        f"{result['faster_pairs']}/{result['pairs']} 对更快 → "
                        f"{'实测提速' if verdict == 'faster' else '无提速'}"
                    )
                else:
                    self._append_log(
                        f"{provider['name']} / {model['id']}：有效样本不足，无法判定"
                    )
            self._render_models()
            messagebox.showinfo(
                "Fast 提速测量完成",
                f"{faster} / {len(results)} 个模型实测有提速。\n\n详细数字见状态工具里的日志。",
                parent=self,
            )

        self._run_task("正在测量 Fast 提速", worker, success, [key])

    def _save_current(self) -> None:
        try:
            provider = self._provider_from_form()
            key = self._probe_key()
            if provider["enabled"] and not any(model["enabled"] for model in provider["models"]):
                raise ValueError("启用的供应商至少需要选择一个模型。")
            if self.current_id is None and not key:
                raise ValueError("新供应商必须填写 API Key。")
            if self.current_id is None and any(
                item["id"] == provider["id"] for item in self.registry.get("providers", [])
            ):
                raise ValueError("供应商 ID 已存在，请使用另一个 ID。")
            if self.current_id and provider["id"] != self.current_id:
                raise ValueError("已保存供应商的 ID 不能修改。")
        except Exception as error:
            self._show_error("无法保存", error)
            return

        if self.current_id:
            drifted = self._outside_edits(self.current_id)
            if drifted:
                listing = "\n".join(f"  · {item}" for item in drifted)
                if not messagebox.askyesno(
                    "磁盘上的配置已被改动",
                    "编辑器打开期间，这些字段在 providers.json 里被别处改过：\n\n"
                    f"{listing}\n\n"
                    "继续保存会用编辑器里的值覆盖它们。\n"
                    "选「否」可以放弃这次保存，然后按 Ctrl+R 重新载入磁盘上的版本。",
                    parent=self,
                ):
                    self._append_log(
                        f"保存已取消：{provider['name']} 在磁盘上被改动的字段为 "
                        f"{'、'.join(drifted)}。"
                    )
                    return

        def worker() -> dict[str, Any]:
            # No restart: the router hot-reloads providers.json, so a save no longer costs
            # the user their in-flight requests. It is only started when it is not up.
            result = apply_provider(provider, key, restart=False, workspace=self.workspace)
            result["router_started"] = self._ensure_router_running()
            return result

        def success(result: dict[str, Any]) -> None:
            self.api_key_var.set("")
            self._load_registry(provider["id"])
            how = "路由器已重新拉起" if result.get("router_started") else "路由器热加载，未中断连接"
            # The Claude workspace generates no catalog, so its result carries an empty model
            # list. Printing that verbatim read as "the save just lost all your models".
            models = result["catalog"]["models"] or selectable_slugs(self.registry)
            self._append_log(
                f"{provider['name']} 已保存，{how}，共 {len(models)} 个模型。"
            )
            self._update_health_metrics()
            messagebox.showinfo("保存成功", f"供应商配置和模型目录已生效（{how}）。", parent=self)

        self._run_task("正在保存", worker, success, [key])

    def _ensure_router_running(self, workspace: Any = None, *, force: bool = False) -> bool:
        """Start or replace a router that is down, stale, or serving the wrong workspace.

        `force` replaces it even when /healthz looks right.  That is the only way to pick up an
        edit to codex_sota_router.py, because such an edit changes neither the router version nor
        the registry hash and the health probe therefore still matches.  It drops whatever is
        mid-request, so only ask for it in a window where nothing is -- see the Claude launch path,
        which forces it in the gap after Claude has been closed.
        """
        workspace = workspace or self.workspace
        if force:
            restart_router(workspace, force=True)
            return True
        try:
            with urllib.request.urlopen(workspace.health_url, timeout=3) as response:
                health = json.loads(response.read())
                if router_health_matches_workspace(health, workspace):
                    return False
        except Exception:  # noqa: BLE001 - unreachable/invalid health means it needs starting
            pass
        restart_router(workspace)
        return True

    def _delete_current(self) -> None:
        if not self.current_id:
            return
        try:
            provider = find_provider(self.registry, self.current_id)
        except KeyError:
            self.current_id = None
            self._load_registry()
            return
        if provider.get("protected"):
            return
        if not messagebox.askyesno(
            "删除供应商",
            f"确认删除 {provider['name']}？\n\n已有任务中的模型名称将保留在历史记录中，但不能再发起请求。",
            parent=self,
        ):
            return

        provider_id = provider["id"]

        def worker() -> dict[str, Any]:
            return delete_provider(provider_id, self.workspace)

        def success(_result: dict[str, Any]) -> None:
            self.current_id = None
            self._load_registry()
            self._append_log(f"{provider['name']} 已从供应商注册表移除。")

        self._run_task("正在删除供应商", worker, success)

    def _reload_current(self) -> None:
        if self._busy:
            return
        try:
            self._load_registry(self.current_id)
            self.status_var.set("已重新载入")
        except Exception as error:
            self._show_error("重新载入失败", error)

    def _run_audit(self) -> None:
        def worker() -> dict[str, Any]:
            registry = audit_registry(self.workspace)
            health = None
            try:
                with urllib.request.urlopen(self.workspace.health_url, timeout=3) as response:
                    health = json.loads(response.read().decode("utf-8"))
            except Exception:
                health = None
            return {
                "registry": registry,
                "health": health,
                "vendors": read_router_vendor_health(self.workspace.log_path),
            }

        def success(result: dict[str, Any]) -> None:
            registry = result["registry"]
            health = result["health"]
            empty = registry["status"] == "empty"
            ready = registry["status"] == "ready"
            if empty:
                registry_text = f"注册表：还没有供应商（{self.workspace.label}）"
                registry_colour = MUTED
            else:
                registry_text = (
                    f"注册表：{registry['provider_count']} 家 / {registry['model_count']} 个模型"
                )
                registry_colour = SUCCESS if ready else DANGER
            self.registry_metric.configure(text=registry_text, foreground=registry_colour)
            running = bool(health and router_health_matches_workspace(health, self.workspace))
            config_error = str((health or {}).get("config_error") or "")
            if running:
                router_text = "路由器：运行中"
            elif config_error:
                # It is listening and answering, it just refused the file on disk and kept
                # serving the previous one. "未运行" would send the user hunting for a process
                # that is right there in front of them.
                router_text = "路由器：配置被拒"
            else:
                router_text = "路由器：未运行"
            self.router_metric.configure(
                text=router_text,
                foreground=SUCCESS if running else (MUTED if empty else WARNING),
            )
            if empty:
                # A workspace with nothing in it is a starting point, not a fault.
                self.header_status_var.set(f"{self.workspace.label} 未配置")
                self._append_log(
                    f"{self.workspace.label} 还没有供应商 —— 在「供应商配置」里填好名称、Base URL、"
                    "API Key 之后点「保存并应用」，模型目录和路由器会自动就绪。"
                )
            else:
                self.header_status_var.set("就绪" if ready and running else "需要检查")
                self._append_log(
                    f"审计：注册表 {registry['status']}，{registry['provider_count']} 家供应商，"
                    f"{registry['model_count']} 个模型，路由器{'运行中' if running else '未运行'}。"
                )
            self._render_health(result["vendors"])
            self._report_degraded_vendors(result["vendors"])
            if config_error and not running:
                # Last, so this wins the header over a degraded upstream: it is the rarer
                # problem and the only one the user can fix from this window.
                self._append_log(
                    "路由器还在用上一份配置服务：它拒绝了当前的 providers.json —— "
                    f"{config_error}。修好后点「保存并应用」重试，或重启路由器。"
                )
                self.header_status_var.set("路由器拒绝了配置")

        self._run_task("正在审计", worker, success)

    def _update_health_metrics(self) -> None:
        self.after(100, self._run_audit)

    def _launch_codex(self) -> None:
        """The header button: each workspace launches its own app."""
        if self._busy:
            return
        if self.workspace is CLAUDE:
            self._launch_claude()
            return
        codex_sota_command = resolve_codex_sota_command()
        if codex_sota_command is None:
            messagebox.showerror(
                "启动失败",
                "找不到 codex-sota 启动命令。请把它加入 PATH，或设置 "
                "CODEX_SOTA_COMMAND 指向实际的 .cmd/.bat/.ps1/.exe。",
                parent=self,
            )
            return
        auth_problem = sota_auth_problem()
        if auth_problem:
            messagebox.showerror(
                "启动失败",
                "True SOTA API key 不可用。这样启动的话，启动器会停在一个你看不见的"
                "输入提示上，永远不返回。\n\n"
                f"{auth_problem}\n\n"
                "请在 PowerShell 里运行 codex-sota，按提示粘贴 key，然后再回来启动。",
                parent=self,
            )
            return

        invocation, command_cwd = codex_sota_invocation(codex_sota_command)
        started = time.monotonic()
        self._launch_phase = "正在运行 codex-sota"

        def worker() -> dict[str, Any]:
            # 输出走临时文件而不是管道：Start-SotaApp 用 UseShellExecute=$false 启动
            # ChatGPT.exe，子孙进程会继承 std 句柄，管道要等到 Codex App 自己退出才 EOF。
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
                try:
                    completed = subprocess.run(
                        invocation,
                        cwd=str(command_cwd),
                        stdin=subprocess.DEVNULL,
                        stdout=sink,
                        stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=LAUNCH_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    raise RuntimeError(
                        f"codex-sota 超过 {LAUNCH_TIMEOUT_SECONDS} 秒没有返回，已停止等待。"
                        "Codex App 可能还在后台启动，检查一下任务管理器里的 ChatGPT.exe。"
                    ) from None
                sink.seek(0)
                output = sink.read()
            if completed.returncode != 0:
                raise RuntimeError(
                    f"codex-sota 退出码 {completed.returncode}：{first_meaningful_line(output)}"
                )

            self._launch_phase = "正在等待 Codex 窗口"
            window_deadline = time.monotonic() + LAUNCH_WINDOW_WAIT_SECONDS
            window_shown = False
            while time.monotonic() < window_deadline:
                if chatgpt_window_present():
                    window_shown = True
                    break
                time.sleep(LAUNCH_WINDOW_POLL_SECONDS)
            return {
                "elapsed": time.monotonic() - started,
                "window_shown": window_shown,
                "vendors": read_router_vendor_health(self.workspace.log_path),
            }

        self._run_task("正在启动 Codex", worker, self._launch_finished)
        self._tick_launch_status(started)

    def _launch_claude(self) -> None:
        """Publish the Claude profile, make sure its router is up, then activate the 3P app.

        Windows can register both the Squirrel and MSIX Claude packages under the same display
        name.  Resolving the target before changing any configuration prevents a successful
        write from being followed by a launch of the unrelated 1P login instance.
        """
        models = build_inference_models(self.registry)
        if not models:
            messagebox.showwarning(
                "还没有可用模型",
                "Claude 工作区里没有启用任何说 Messages 协议的模型，发布出去的模型选择器会是空的。",
                parent=self,
            )
            return
        try:
            running = claude_window_present()
        except Exception as error:
            self._show_error("无法检查 Claude Desktop 状态", error)
            return
        try:
            launch_target = resolve_claude_launch_target()
        except Exception as error:
            self._show_error("无法定位 Claude Desktop 3P 启动入口", error)
            return
        if launch_target is None:
            messagebox.showerror(
                "找不到 Claude 3P 启动入口",
                "检测不到能读取 Claude-3p 配置的 Claude Desktop 安装。\n\n"
                "请确认 Claude 的 Squirrel 版仍已安装并出现在开始菜单；"
                f"也可以设置 {CLAUDE_AUMID_ENV} 指定已验证的 AppUserModelId。"
                "为避免误打开 1P 登录实例，本次不会改配置或启动程序。",
                parent=self,
            )
            return
        note = "检测到 Claude Desktop 已在运行；确认后会先让它正常退出，再重新启动。\n\n" if running else ""
        if not messagebox.askokcancel(
            "启动 Claude Desktop",
            f"{note}会做五件事：\n"
            "  1. 关闭正在运行的 Claude Desktop，让它重新读取配置\n"
            "  2. 确保 Claude 使用第三方推理模式（deploymentMode=3p）\n"
            f"  3. 重启 {CLAUDE.router_port} 端口的路由器，并写好「{CLAUDE_ENTRY_NAME}」档\n"
            f"  4. 借用 Claude 的「生效档」槽位（本次启动期间指向「{CLAUDE_ENTRY_NAME}」）\n"
            f"  5. 用已检测到的 {launch_target['label']} 入口拉起 Claude Desktop\n\n"
            "cc-switch 的档不会被改动；这次 Claude 退出后，生效档会自动交还给原来的管理器，\n"
            "所以之后从 cc-switch 启动 Claude 仍然是 cc-switch 的配置。\n\n继续？",
            parent=self,
        ):
            return

        def worker() -> dict[str, Any]:
            closed = close_running_claude()
            migration = migrate_legacy_entry()
            deployment = ensure_deployment_mode()
            # Forced, because Claude is down as of the line above and the profile written on the
            # next one can assert capabilities -- `supports1m` publishes a `<slug>[1m]` picker
            # entry -- that only a router carrying the current code knows how to route.  /healthz
            # cannot tell a stale router from a current one when only the routing code changed, so
            # the health short-circuit would leave the old process serving a profile written for
            # the new one.  This gap, between the close and the launch, is the one moment when
            # replacing it interrupts nothing.  The Codex router is a separate process on its own
            # port and is not touched.
            started_router = self._ensure_router_running(CLAUDE, force=True)
            published = write_claude_profile(models, CLAUDE_GATEWAY_URL)
            target = resolve_claude_launch_target() or launch_target
            # The shared appliedId slot is taken here, not when the profile is written: Claude
            # reads it at startup, so it has to be ours before the process starts and can go
            # back to cc-switch as soon as this Claude exits.
            claim = claim_claude_slot()
            try:
                launch = start_claude_process(target)
                deadline = time.monotonic() + LAUNCH_WINDOW_WAIT_SECONDS
                appeared = False
                while time.monotonic() < deadline:
                    if claude_window_present():
                        appeared = True
                        break
                    time.sleep(LAUNCH_WINDOW_POLL_SECONDS)
            except Exception:
                release_claude_slot()
                raise
            if appeared:
                watcher = arm_claude_slot_release()
            else:
                # Nothing came up, so nothing is reading our profile.  Hand the slot straight
                # back instead of leaving cc-switch pointed at our gateway.
                watcher = {"status": "released-no-window", "detail": release_claude_slot()}
            return {
                "router_started": started_router,
                "published": published,
                "deployment": deployment,
                "migration": migration,
                "closed": closed,
                "launch_target": target,
                "launch_code": launch["returncode"],
                "launch_detail": launch["detail"],
                "claim": claim,
                "watcher": watcher,
                "window": appeared,
            }

        def success(result: dict[str, Any]) -> None:
            displaced = result["claim"].get("displaced_name") or ""
            self._append_log(
                f"Claude Desktop 启动流程完成：路由器"
                f"{'已拉起' if result['router_started'] else '本来就在跑'}，"
                f"发布 {result['published']['models']} 个模型，"
                f"3P 模式{'已修正' if result['deployment']['changed'] else '本来就正确'}，"
                f"生效档已接管"
                f"{f'（原来是「{displaced}」，退出后自动交还）' if displaced else ''}，"
                f"窗口{'已出现' if result['window'] else '未在等待窗口内出现'}（{result['launch_target']['label']}）"
            )
            self._refresh_claude_status()
            if result["window"]:
                self.header_status_var.set("Claude 运行中")
            else:
                hint = result.get("launch_detail") or ""
                messagebox.showwarning(
                    "已发布，但没等到窗口",
                    "配置和路由器都就绪了，但没在等待时间内看到 Claude Desktop 的窗口。\n\n"
                    "生效档已经交还给原来的管理器，cc-switch 不受影响。\n\n"
                    "请检查已检测到的 Claude 3P 入口是否仍可用："
                    f"{result['launch_target']['label']}。"
                    + (f"\n\n启动器输出：{hint}" if hint else ""),
                    parent=self,
                )

        self._run_task("正在启动 Claude Desktop", worker, success)

    def _launch_finished(self, result: dict[str, Any]) -> None:
        waited = int(result["elapsed"])
        if result["window_shown"]:
            self._append_log(f"Codex App 窗口已出现，共耗时 {waited} 秒。")
            self.header_status_var.set("Codex 运行中")
        else:
            self._append_log(
                f"codex-sota 成功返回（{waited} 秒，退出码 0），但 "
                f"{LAUNCH_WINDOW_WAIT_SECONDS} 秒内没等到 Codex 窗口。"
            )
            self.header_status_var.set("需要检查")
            messagebox.showwarning(
                "启动已完成，但窗口没出现",
                f"codex-sota 本身跑完了（{waited} 秒，退出码 0），但等了 "
                f"{LAUNCH_WINDOW_WAIT_SECONDS} 秒还没看到 Codex 窗口。\n\n"
                "Codex App 冷启动本来就慢，可以再等等；如果始终不出现，"
                "检查任务管理器里有没有 ChatGPT.exe。",
                parent=self,
            )
        self._report_degraded_vendors(result["vendors"])

    def _tick_launch_status(self, started: float) -> None:
        if self._closing or not self._busy:
            return
        waited = int(time.monotonic() - started)
        label = f"{self._launch_phase}（已等待 {waited} 秒）"
        if waited >= LAUNCH_SLOW_HINT_SECONDS:
            label += "，冷启动通常需要 1-3 分钟，请勿重复点击"
        self.status_var.set(label)
        self.after(1000, lambda: self._tick_launch_status(started))

    LINT_TAGS = {"error": "✗ 错误", "warning": "⚠ 警告", "info": "· 提示"}

    def _rank_model_latency(self) -> None:
        """Rank every enabled vendor serving the selected bare model by measured latency."""
        if self._busy:
            return
        selection = self.model_tree.selection()
        if len(selection) != 1:
            messagebox.showwarning(
                "先选一个模型", "在模型列表里选中一行（只选一个），我去比同名模型在各家的快慢。",
                parent=self,
            )
            return
        index = self._model_index_from_item(selection[0])
        if index is None or not 0 <= index < len(self.draft_models):
            return
        bare = self.draft_models[index]["id"]
        try:
            registry = load_registry(
                self.workspace.registry_path, allow_missing_secrets=True
            )
        except Exception as error:
            self._show_error("读取注册表失败", error)
            return
        targets = [
            deepcopy(provider)
            for provider in registry["providers"]
            if provider.get("enabled")
            and any(m.get("enabled") and m["id"] == bare for m in provider["models"])
        ]
        if len(targets) < 2:
            messagebox.showinfo(
                "没什么可比的",
                f"目前只有 {len(targets)} 家启用了 {bare}，凑不成排行。",
                parent=self,
            )
            return
        if not messagebox.askokcancel(
            "延迟排行",
            f"{bare} 有 {len(targets)} 家提供，将各发 {LATENCY_SAMPLES} 次非流式请求"
            f"（共 {len(targets) * LATENCY_SAMPLES} 次，会消耗额度）。\n\n继续？",
            parent=self,
        ):
            return

        def worker() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for provider in targets:
                try:
                    rows.append(measure_latency(provider, bare, LATENCY_SAMPLES))
                except Exception as error:
                    rows.append(
                        {
                            "provider": provider["id"],
                            "name": provider.get("name") or provider["id"],
                            "model": bare,
                            "ok": 0,
                            "attempts": LATENCY_SAMPLES,
                            "median_seconds": None,
                            "best_seconds": None,
                            "failures": [str(error)[:200]],
                        }
                    )
            return rows

        def success(rows: list[dict[str, Any]]) -> None:
            ranked = sorted(
                rows,
                key=lambda row: (row["median_seconds"] is None, row["median_seconds"] or 0),
            )
            lines = []
            for position, row in enumerate(ranked, 1):
                if row["median_seconds"] is None:
                    text = f"{position}. {row['name']}：{row['attempts']} 次全失败"
                    if row["failures"]:
                        text += f"（{row['failures'][0][:50]}）"
                else:
                    text = (
                        f"{position}. {row['name']}：中位 {row['median_seconds']}s"
                        f"，最快 {row['best_seconds']}s，成功 {row['ok']}/{row['attempts']}"
                    )
                lines.append(text)
                self._append_log(f"延迟排行 {bare} — {text}")
            best = ranked[0]
            headline = (
                f"最快的是 {best['name']}（中位 {best['median_seconds']}s）"
                if best["median_seconds"] is not None
                else "没有一家跑通"
            )
            messagebox.showinfo(
                f"{bare} 延迟排行",
                headline + "\n\n" + "\n".join(lines)
                + "\n\n想让某家优先被 failover 选中，用左边的 ↑ 上移。",
                parent=self,
            )

        self._run_task(f"正在测 {bare} 的各家延迟", worker, success)

    def _rank_model_latency_shortcut(self) -> None:
        self.notebook.select(self.models_tab)
        self._rank_model_latency()

    def _refresh_claude_status(self) -> None:
        """Show which 3P profile Claude Desktop is on and how many models we could publish."""
        self._claude_entries = []
        try:
            # Repair the old malformed SOTA id before rendering the picker; otherwise Claude
            # can remain pointed at a profile it rejects even though the UI says the tab loaded.
            migrate_legacy_entry()
            # Backstop for a release that never ran (manager killed, watcher lost, reboot).
            # Only acts when Claude is gone, so a live session is never touched.
            self._reconcile_claude_slot()
            status = claude_library_status()
        except Exception as error:  # noqa: BLE001 - a bad library file must not break the tab
            self.claude_status_var.set(f"Claude 配置库读不了：{error}")
            return
        if not status["available"]:
            self.claude_status_var.set(status["reason"])
            self.claude_entry_combo.configure(values=[])
            return
        self._claude_entries = status["entries"]
        self.claude_entry_combo.configure(values=[e["name"] for e in status["entries"]])
        if status["applied_name"]:
            self.claude_entry_combo.set(status["applied_name"])
        count = len(build_inference_models(self.registry))
        self.claude_status_var.set(
            f"当前生效的档：{status['applied_name'] or '（无）'}{self._claude_slot_note()}　|　"
            f"可发布的 Messages 模型：{count} 个　|　共 {len(status['entries'])} 个档"
        )

    def _reconcile_claude_slot(self) -> dict[str, Any]:
        try:
            running = claude_window_present() or bool(claude_pids())
        except Exception:  # noqa: BLE001 - fail closed: unknown state means "leave it alone"
            return {"status": "unknown-process-state"}
        try:
            result = reconcile_claude_slot(claude_running=running)
        except Exception as error:  # noqa: BLE001 - never break the tab over housekeeping
            return {"status": "failed", "detail": str(error)}
        if result.get("status") == "released":
            self._append_log(
                f"Claude 已退出，生效档已自动交还给「{result['applied_name']}」。"
            )
        return result

    def _claude_slot_note(self) -> str:
        """Spell out that the slot is borrowed, since it is shared with cc-switch."""
        try:
            state = claude_slot_state()
        except Exception:  # noqa: BLE001 - decorative only
            return ""
        if not state.get("available") or not state.get("claimed"):
            return ""
        displaced = state.get("displaced_name") or ""
        if not displaced:
            return "（本次由 codex-sota 借用）"
        return f"（codex-sota 借用中，退出后交还「{displaced}」）"

    def _release_claude_slot(self) -> None:
        """Hand the shared appliedId slot back without waiting for Claude to exit."""
        if self._busy:
            return
        try:
            state = claude_slot_state()
        except Exception as error:
            self._show_error("读不到 Claude 生效档状态", error)
            return
        if not state.get("available"):
            messagebox.showwarning("读不到配置库", str(state.get("reason") or ""), parent=self)
            return
        if not state.get("claimed"):
            messagebox.showinfo(
                "没有要交还的",
                f"codex-sota 现在没有借用 Claude 的生效档槽位。\n\n"
                f"当前生效的档是「{state.get('applied_name') or '（无）'}」。\n\n"
                "如果要手动指定，用下面的下拉框直接切档。",
                parent=self,
            )
            return
        displaced = state.get("displaced_name") or state.get("displaced_id") or ""
        if not displaced:
            messagebox.showwarning(
                "没有记录前一个档",
                "借用记录里没有前一个生效档（可能第一次就是 codex-sota 在用），"
                "所以没有可以交还的对象。用下面的下拉框手动选一个档即可。",
                parent=self,
            )
            return
        if not messagebox.askokcancel(
            "交还生效档",
            f"把 Claude Desktop 的生效档交还给「{displaced}」？\n\n"
            "只改 _meta.json 里的 appliedId（会先备份），各个档的内容都不动。\n"
            "已经在运行的 Claude 不受影响 —— 它的配置在启动时就读完了；\n"
            "下次从 cc-switch 启动 Claude 就会用回它自己的配置。\n\n继续？",
            parent=self,
        ):
            return

        def worker() -> dict[str, Any]:
            return release_claude_slot(force=True)

        def success(result: dict[str, Any]) -> None:
            outcome = result.get("status")
            if outcome == "released":
                self._append_log(f"生效档已交还给「{result['applied_name']}」。")
            else:
                self._append_log(f"交还生效档：{outcome}")
            self._refresh_claude_status()
            messages = {
                "released": f"生效档现在是「{result.get('applied_name')}」。",
                "no-claim": "本来就没有借用记录，没做任何改动。",
                "no-previous": "借用记录里没有前一个档，只清掉了借用记录。",
                "previous-gone": "前一个档已经不在配置库里了，只清掉了借用记录。",
                "not-owner": f"生效档已经被别人切成「{result.get('applied_name')}」，没有动它。",
            }
            messagebox.showinfo("交还完成", messages.get(outcome, str(outcome)), parent=self)

        self._run_task("正在交还 Claude 生效档", worker, success)


    def _publish_claude_profile(self) -> None:
        if self._busy:
            return
        source = (
            self.registry
            if self.workspace is CLAUDE
            else load_registry(CLAUDE.registry_path)
        )
        models = build_inference_models(source)
        if not models:
            messagebox.showwarning(
                "还没有 Messages 供应商",
                "Claude Desktop 只能用说 Anthropic Messages 协议的供应商。\n\n"
                "去「供应商配置」里给一个 Anthropic 兼容的网关勾上"
                "「说 Messages 协议（Claude Desktop）」，保存之后再回来。",
                parent=self,
            )
            return
        preview = "\n".join(
            f"    {m['labelOverride']}  →  {m['name']}\n        {claude_thinking_summary(m['name'])}"
            + ("  ·  另给一档 1M 上下文" if m.get("supports1m") else "")
            for m in models[:12]
        )
        more = f"\n    ……另外 {len(models) - 12} 个" if len(models) > 12 else ""
        # Claude Desktop only offers a thinking control for model names it recognises after
        # stripping the vendor prefix, so say which ones will come up without one instead of
        # letting the user find out in the picker.
        blind = [m["name"] for m in models if claude_thinking_effort_levels(m["name"]) is None]
        hint = (
            "\n有 " + str(len(blind)) + " 个模型不会有思考控件：Claude 只认得它内置的那几个"
            "模型名，带 -thinking 这类后缀的名字它对不上。\n"
            if blind
            else ""
        )
        # supports1m adds a second `<slug>[1m]` row rather than changing the existing one, so
        # say how many rows the picker gains.  Otherwise the model count in this dialog and the
        # one in the picker disagree, which reads as a bug.
        wide = sum(1 for m in models if m.get("supports1m"))
        wide_hint = (
            f"\n有 {wide} 个模型会多出一档 1M 上下文（选择器里显示为 name[1m]），"
            "原来的标准档一个都不会少。\n"
            if wide
            else ""
        )
        if not messagebox.askokcancel(
            "写入 Claude 档",
            f"会在 Claude Desktop 的配置库里创建/更新一个叫「{CLAUDE_ENTRY_NAME}」的档。"
            f"\n\n网关：{CLAUDE_GATEWAY_URL}\n模型 {len(models)} 个：\n"
            f"{preview}{more}\n{hint}{wide_hint}\n"
            "不会动「当前生效的档」：那个槽位是和 cc-switch 共用的，只有从这里启动 "
            "Claude Desktop 时才会临时借用，退出后自动交还。\n"
            "别人的档（比如 cc-switch 的）一个字节都不会动，_meta.json 会先备份。\n\n继续？",
            parent=self,
        ):
            return

        def worker() -> dict[str, Any]:
            self._ensure_router_running(CLAUDE)
            return write_claude_profile(models, CLAUDE_GATEWAY_URL)

        def success(result: dict[str, Any]) -> None:
            self._append_log(
                f"Claude 档已写入：{result['models']} 个模型，网关 {result['gateway_url']}，"
                f"备份在 {result['backup']}（生效档未改动）"
            )
            self._refresh_claude_status()
            messagebox.showinfo(
                "写好了",
                f"「{CLAUDE_ENTRY_NAME}」已更新，发布了 {result['models']} 个模型。\n\n"
                "当前生效的档没有变动 —— 用上面的「启动 Claude Desktop」才会切到这个档，"
                "那次 Claude 退出后会自动交还给原来的管理器。\n\n"
                "要立刻改生效档，用下面的下拉框手动切。",
                parent=self,
            )

        self._run_task("正在写入 Claude 档", worker, success)

    def _switch_claude_entry(self) -> None:
        if self._busy:
            return
        wanted = self.claude_entry_combo.get().strip()
        entry = next((e for e in self._claude_entries if e["name"] == wanted), None)
        if entry is None:
            messagebox.showwarning("先选一个档", "在下拉框里选要切到哪个档。", parent=self)
            return
        if entry["applied"]:
            messagebox.showinfo("已经是它了", f"「{entry['name']}」当前就是生效档。", parent=self)
            return
        if not messagebox.askokcancel(
            "切换 Claude 档",
            f"把 Claude Desktop 的生效档切成「{entry['name']}」？\n\n"
            "只改 _meta.json 里的 appliedId（会先备份），各个档的内容都不动。\n"
            "同样需要重启 Claude Desktop 才生效。",
            parent=self,
        ):
            return

        def worker() -> dict[str, Any]:
            return apply_claude_entry(entry["id"])

        def success(result: dict[str, Any]) -> None:
            self._append_log(f"Claude 生效档已切为 {result['applied_name']}，备份在 {result['backup']}")
            self._refresh_claude_status()
            messagebox.showinfo(
                "切好了", f"生效档现在是「{result['applied_name']}」，重启 Claude Desktop 后生效。",
                parent=self,
            )

        self._run_task("正在切换 Claude 档", worker, success)

    def _run_lint(self, announce: bool = False) -> None:
        """Static configuration check — no network, so it is cheap enough to run on every load."""
        try:
            findings = lint_registry(deepcopy(self.registry), self.workspace)
        except Exception as error:
            if announce:
                self._show_error("配置检查失败", error)
            return
        errors = [f for f in findings if f["level"] == "error"]
        warnings = [f for f in findings if f["level"] == "warning"]
        if hasattr(self, "lint_metric"):
            if errors:
                text, colour = f"配置：{len(errors)} 个错误", DANGER
            elif warnings:
                text, colour = f"配置：{len(warnings)} 个警告", WARNING
            elif findings:
                text, colour = f"配置：{len(findings)} 条提示", MUTED
            else:
                text, colour = "配置：没有问题", SUCCESS
            self.lint_metric.configure(text=text, foreground=colour)
        if findings and (announce or errors):
            for finding in findings:
                where = finding["provider"] + (f"/{finding['model']}" if finding["model"] else "")
                self._append_log(
                    f"配置检查 {self.LINT_TAGS[finding['level']]} {where}：{finding['message']}"
                )
        if errors and not announce:
            self.header_status_var.set("配置有错误")
        if not announce:
            return
        if not findings:
            messagebox.showinfo(
                "配置检查", "没发现问题。\n\n检查的是路径拼接、启用状态、超时、密钥、"
                "以及快速/换家开关是否真的能生效。", parent=self
            )
            return
        lines = []
        for finding in findings[:12]:
            where = finding["provider"] + (f"/{finding['model']}" if finding["model"] else "")
            lines.append(f"{self.LINT_TAGS[finding['level']]} {where}\n    {finding['message']}")
        more = f"\n\n……还有 {len(findings) - 12} 条，见运行记录" if len(findings) > 12 else ""
        messagebox.showwarning(
            "配置检查",
            f"{len(errors)} 个错误、{len(warnings)} 个警告、"
            f"{len(findings) - len(errors) - len(warnings)} 条提示\n\n" + "\n\n".join(lines) + more,
            parent=self,
        )

    def _export_config(self) -> None:
        """Write a secret-free snapshot of the registry — safe to keep or share."""
        default = "codex-sota-providers-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出供应商配置（不含密钥）",
            defaultextension=".json",
            initialfile=default,
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            snapshot = redacted_registry(
                load_registry(self.workspace.registry_path, allow_missing_secrets=True)
            )
            Path(path).write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as error:
            self._show_error("导出失败", error)
            return
        self._append_log(
            f"配置已导出到 {path}（{len(snapshot.get('providers', []))} 家，密钥和 entropy 已剔除）"
        )
        messagebox.showinfo(
            "导出完成",
            f"已写入：\n{path}\n\nAPI Key 和 entropy 都没有导出，只保留 key_present 标记。",
            parent=self,
        )

    def _backup_config(self) -> None:
        """Copy providers.json and the generated catalog into a timestamped backup folder."""
        target = self.workspace.root / "backups" / ("manual-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            target.mkdir(parents=True, exist_ok=True)
            copied = []
            for source in (self.workspace.registry_path, self.workspace.catalog_path):
                if source.exists():
                    shutil.copy2(source, target / source.name)
                    copied.append(source.name)
        except OSError as error:
            self._show_error("备份失败", error)
            return
        self._append_log(f"已备份 {'、'.join(copied)} 到 {target}")
        messagebox.showinfo("备份完成", f"已复制到：\n{target}\n\n{'、'.join(copied)}", parent=self)

    def _restore_config(self) -> None:
        """Put a backed-up providers.json back, after showing exactly what would change.

        This is the only tool on the tab that overwrites live config, so it is deliberately
        talkative: the file is merged against what is on disk, fully validated, and summarised
        before anything is written, and the current registry is copied into
        backups/pre-restore-* first. A file whose providers belong to the other workspace, or
        one that would drop a protected provider, is refused outright rather than half applied.
        """
        if self._busy:
            return
        if not self._confirm_discarding_edits("恢复配置"):
            return
        backups = self.workspace.root / "backups"
        chosen = filedialog.askopenfilename(
            parent=self,
            title=f"选择要恢复到「{self.workspace.label}」的配置文件",
            initialdir=str(backups if backups.exists() else self.workspace.root),
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not chosen:
            return
        try:
            incoming = json.loads(Path(chosen).read_text(encoding="utf-8-sig"))
            if not isinstance(incoming, dict) or not isinstance(incoming.get("providers"), list):
                raise ValueError("这个文件里没有 providers 列表，不像是备份或导出的配置。")
            current = load_registry(self.workspace.registry_path, allow_missing_secrets=True)
            candidate, report = merge_restored_registry(incoming, current, self.workspace.name)
            # Validate before asking anything. This is the same gate write_registry applies, so
            # a file that cannot be written never gets as far as a confirmation dialog, and a
            # confirmed restore cannot then fail halfway through the write.
            candidate = validate_registry(deepcopy(candidate), allow_missing_secrets=True)
        except Exception as error:
            self._show_error("这个配置不能恢复", error)
            return
        if report["foreign_workspace"]:
            messagebox.showerror(
                "配置属于另一个工作区",
                "文件里这些供应商登记的家不是当前工作区，恢复过去会让 Codex 和 Claude 串配置：\n\n"
                + "、".join(report["foreign_workspace"][:8])
                + f"\n\n当前工作区是「{self.workspace.label}」，请选这个工作区自己备份出来的文件。",
                parent=self,
            )
            return
        if report["protected_removed"]:
            messagebox.showerror(
                "会删掉受保护的供应商",
                "这份配置里没有以下受保护的供应商，恢复等于把它们删掉：\n\n"
                + "、".join(report["protected_removed"])
                + "\n\n受保护的供应商不能这样移除，恢复已取消。",
                parent=self,
            )
            return
        self._confirm_and_restore(chosen, candidate, report)

    def _restore_summary(self, source: str, candidate: dict[str, Any], report: dict[str, Any]) -> str:
        """The before/after read-out shown in the restore confirmation dialog."""
        lines = [
            f"从：{source}",
            f"恢复到：{self.workspace.label} — {self.workspace.registry_path}",
            "",
            f"恢复后共 {len(candidate['providers'])} 家供应商。",
        ]
        if report["added"]:
            lines.append("新增：" + "、".join(report["added"]))
        if report["removed"]:
            lines.append("删除：" + "、".join(report["removed"]))
        if not report["added"] and not report["removed"]:
            lines.append("名单不变，只覆盖各家的设置。")
        if report["kept_headers"]:
            lines.append(
                f"{len(report['kept_headers'])} 个打码的请求头沿用磁盘上的现值，"
                "不会写成 <redacted>。"
            )
        if report["needs_key"]:
            lines.append("恢复后还缺 API Key：" + "、".join(report["needs_key"]))
        lines += [
            "",
            "现在的 providers.json 会先备份到 backups\\pre-restore-*。",
            "路由器会自己热加载，不用重启，正在跑的请求不受影响。",
            "",
            "确认恢复？",
        ]
        return "\n".join(lines)

    def _confirm_and_restore(
        self, source: str, candidate: dict[str, Any], report: dict[str, Any]
    ) -> None:
        """Ask once, then back up and write under the registry lock."""
        if not messagebox.askokcancel(
            "恢复配置", self._restore_summary(source, candidate, report), parent=self
        ):
            return
        workspace = self.workspace
        total = len(candidate["providers"])

        def worker() -> dict[str, Any]:
            # Backup inside the lock, so nothing can slip between the copy and the write and
            # leave a "backup" that never matched what was replaced.
            with registry_write_lock(workspace=workspace):
                target = workspace.root / "backups" / ("pre-restore-" + time.strftime("%Y%m%d-%H%M%S"))
                target.mkdir(parents=True, exist_ok=True)
                saved = []
                for path in (workspace.registry_path, workspace.catalog_path):
                    if path.exists():
                        shutil.copy2(path, target / path.name)
                        saved.append(path.name)
                write_registry(candidate, workspace.registry_path, allow_missing_secrets=True)
                catalog = rebuild_catalog(workspace)
            return {"backup": target, "saved": saved, "catalog": catalog}

        def success(result: dict[str, Any]) -> None:
            self.current_id = None
            self._load_registry()
            self._run_audit()
            self._append_log(
                f"已从 {source} 恢复配置（{total} 家），原配置备份在 {result['backup']}"
            )
            models = result.get("catalog", {}).get("models") or []
            detail = f"配置已恢复，共 {total} 家供应商。\n\n原配置备份在：\n{result['backup']}"
            if models:
                detail += f"\n\n模型目录已重建：{len(models)} 个模型。"
            if report["needs_key"]:
                detail += "\n\n这些家还没有密钥，先补上再用：" + "、".join(report["needs_key"])
            messagebox.showinfo("恢复完成", detail, parent=self)

        self._run_task("正在恢复配置", worker, success)

    def _restart_router_only(self) -> None:
        """Restart just the router process, leaving the Codex App alone."""
        if self._busy:
            return
        if not messagebox.askokcancel(
            "重启路由器",
            "会结束当前路由器进程再拉起一个新的。\n\n"
            "正在传输中的请求会断掉 —— 如果 Codex 里有任务在跑，等它跑完再重启。\n\n"
            "Codex App 本身不受影响，不会被关掉。\n\n继续？",
            parent=self,
        ):
            return

        def worker() -> dict[str, Any]:
            # force=True because the usual reason to press this button is that the router's own
            # code changed, which /healthz cannot see -- the version and registry hash are both
            # unchanged, so the default ensure-running path would report success and leave the
            # old process serving.
            return restart_router(self.workspace, force=True)

        def success(result: dict[str, Any]) -> None:
            replaced = result.get("stopped_process_ids") or []
            detail = f"（换掉了 {len(replaced)} 个旧进程）" if replaced else "（原本没有在跑）"
            self._append_log(f"路由器已重启：{result.get('status', '未知状态')}{detail}")
            self._run_audit()

        self._run_task("正在重启路由器", worker, success)

    def _move_provider(self, offset: int) -> None:
        """Move the selected provider up or down — failover tries alternates in this order."""
        if self._busy:
            return
        if not self.current_id:
            messagebox.showwarning("没有选中供应商", "先在左边选一个供应商。", parent=self)
            return
        if not self._confirm_discarding_edits("调整顺序"):
            return
        order = [provider["id"] for provider in self.registry.get("providers", [])]
        try:
            index = order.index(self.current_id)
        except ValueError:
            return
        target = index + offset
        if not 0 <= target < len(order):
            self.status_var.set("已经在最" + ("上" if offset < 0 else "下") + "面了")
            return
        order[index], order[target] = order[target], order[index]
        moved_id = self.current_id

        def worker() -> dict[str, Any]:
            return reorder_providers(order, self.workspace)

        def success(_result: dict[str, Any]) -> None:
            self._load_registry(moved_id)
            self._append_log("供应商顺序已调整：" + " → ".join(order))
            self._update_health_metrics()

        self._run_task("正在调整顺序", worker, success)

    def _blank_editor(self) -> bool:
        """True when there is nothing in the editor to act on.

        Without this, probe buttons on an empty workspace surface the raw validator message
        ("Provider ID must use 2-40 lowercase letters...") which reads like a defect rather
        than "you have not filled anything in yet".
        """
        return not self.current_id and not self.id_var.get().strip()

    def _has_unsaved_changes(self) -> bool:
        """Whether the editor holds edits that a registry reload would silently throw away."""
        if not self.current_id:
            return bool(self.name_var.get().strip() or self.base_url_var.get().strip())
        try:
            saved = find_provider(self.registry, self.current_id)
        except KeyError:
            return True
        try:
            edited = self._provider_from_form()
        except Exception:
            return True
        return comparable_provider(edited) != comparable_provider(deepcopy(saved))

    def _confirm_discarding_edits(self, action: str) -> bool:
        """Ask before an action that reloads the registry over unsaved editor changes."""
        if not self._has_unsaved_changes():
            return True
        answer = messagebox.askyesnocancel(
            "编辑框里有未保存的改动",
            f"{action}结束后会重新载入配置，编辑框里没保存的改动会丢掉。\n\n"
            "是＝先保存（保存完再点一次）\n否＝丢掉这些改动，继续\n取消＝什么都不做",
            parent=self,
        )
        if answer is None:
            return False
        if answer:
            self._save_current()
            return False
        return True

    def _run_full_checkup(self) -> None:
        if self._busy:
            return
        if not self._confirm_discarding_edits("体检"):
            return
        try:
            registry = load_registry(
                self.workspace.registry_path, allow_missing_secrets=True
            )
        except Exception as error:
            self._show_error("读取注册表失败", error)
            return
        targets = [
            (provider["id"], model["id"])
            for provider in registry["providers"]
            if provider.get("enabled")
            for model in provider["models"]
            if model.get("enabled")
        ]
        if not targets:
            messagebox.showwarning("没有体检目标", "当前没有已启用的供应商模型。", parent=self)
            return
        if not messagebox.askokcancel(
            "一键体检全部供应商",
            f"将对 {len(targets)} 个已启用模型各发 1-2 次探测请求（会消耗额度），预计 1-3 分钟。\n\n"
            "体检读的是已保存的配置，编辑框里没保存的改动不参与；结果会写回 providers.json。\n\n继续？",
            parent=self,
        ):
            return
        by_provider = {provider["id"]: deepcopy(provider) for provider in registry["providers"]}

        def worker() -> dict[str, Any]:
            rows: list[dict[str, Any]] = []
            repairs: list[str] = []
            for provider_id in dict.fromkeys(pid for pid, _model in targets):
                provider = by_provider[provider_id]
                if provider.get("protected"):
                    continue
                fix = auto_repair_active_inference_path(provider)
                if fix["changed"]:
                    repairs.append(f"{provider['name']}：{fix['reason']}")
            for provider_id, model_id in targets:
                provider = by_provider[provider_id]
                try:
                    result = test_model(provider, model_id)
                    ok = bool(result.get("ok"))
                    status = result.get("status")
                    detail = result.get("detail") or result.get("error") or ""
                except Exception as error:
                    ok, status, detail = False, None, str(error)
                tier = "unknown"
                if ok:
                    try:
                        verdict = probe_fast_tier(provider, model_id).get("verdict")
                        tier = verdict if verdict in {"supported", "unsupported"} else "unknown"
                    except Exception:
                        tier = "unknown"
                for model in provider["models"]:
                    if model["id"] == model_id:
                        model["last_test_status"] = "ready" if ok else "failed"
                        model["last_test_at"] = utc_now()
                        model["last_test_message"] = detail or ("HTTP " + str(status))
                        model["fast_tier_status"] = tier
                        break
                rows.append(
                    {
                        "provider": provider_id,
                        "name": provider["name"],
                        "model": model_id,
                        "ok": ok,
                        "status": status,
                        "tier": tier,
                        "detail": detail[:200],
                    }
                )
            saved, failed_saves = [], []
            # One transaction, no router restart: these are probe verdicts rather than
            # routing changes, and bouncing the router per provider used to abort whatever
            # Codex requests were in flight.
            try:
                saved = save_provider_bookkeeping(
                    list(by_provider.values()), self.workspace
                )["providers"]
            except Exception as error:
                failed_saves.append(str(error))
            return {"rows": rows, "saved": saved, "failed_saves": failed_saves, "repairs": repairs}

        self._run_task("正在体检全部供应商", worker, self._checkup_finished)

    def _checkup_finished(self, result: dict[str, Any]) -> None:
        rows = result["rows"]
        for repair in result.get("repairs") or []:
            self._append_log("已自动修正 Responses 路径 — " + repair)
        ok_rows = [row for row in rows if row["ok"]]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["name"], []).append(row)
        for name, group in sorted(grouped.items()):
            good = sum(1 for row in group if row["ok"])
            fast = sum(1 for row in group if row["tier"] == "supported")
            self._append_log(f"体检 {name}：{good}/{len(group)} 可用，{fast} 个收 service_tier")
            for row in group:
                if not row["ok"]:
                    self._append_log(
                        f"    {row['model']} 失败（{row['status'] or '网络错误'}）{row['detail']}"
                    )
        for problem in result["failed_saves"]:
            self._append_log("体检结果写回失败 " + problem)
        dead = sorted({row["name"] for row in rows if not row["ok"]})
        repairs = result.get("repairs") or []
        self._load_registry(self.current_id)
        messagebox.showinfo(
            "体检完成",
            f"{len(ok_rows)} / {len(rows)} 个模型可用。\n"
            f"结果已写回 {len(result['saved'])} 家供应商。\n"
            + (f"自动修正了 {len(repairs)} 家的 Responses 路径。\n" if repairs else "")
            + "\n"
            + ("有问题的供应商：" + "、".join(dead) if dead else "所有已启用供应商都正常。")
            + "\n\n逐条明细见下方运行记录。",
            parent=self,
        )

    def _render_health(self, health: dict[str, dict[str, Any]]) -> None:
        self.health_tree.delete(*self.health_tree.get_children())
        rows = sorted(health.items(), key=lambda item: (-item[1]["total"], item[0]))
        for name, data in rows:
            rate = data["success_rate"]
            tag = "good" if rate >= 0.95 else ("warn" if rate > 0 else "bad")
            last = f"HTTP {data['last_status']}"
            if data.get("last_time"):
                last += "  " + str(data["last_time"]).replace("T", " ").rstrip("Z")
            self.health_tree.insert(
                "",
                "end",
                values=(
                    name,
                    data["total"],
                    f"{rate * 100:.0f}%",
                    f"{data['p50_ms']} ms" if data["p50_ms"] else "-",
                    f"{data['p95_ms']} ms" if data["p95_ms"] else "-",
                    last,
                ),
                tags=(tag,),
            )
        total = sum(data["total"] for _name, data in rows)
        self.traffic_metric.configure(
            text=f"流量：最近 {total} 次请求 / {len(rows)} 家上游" if rows else "流量：还没有路由记录"
        )

    def _refresh_usage(self, announce: bool = False) -> None:
        """Re-read the router log and repaint the usage tab.

        Both the log read and the price read are file reads that can be slow on a large log, so
        they go through `_run_task` and off the UI thread. Nothing here contacts an upstream.
        """
        prices_path = self.workspace.usage_prices_path
        log_path = self.workspace.log_path

        def worker() -> dict[str, Any]:
            return read_router_usage(log_path, load_usage_prices(prices_path))

        def done(summary: dict[str, Any]) -> None:
            self._render_usage(summary)
            if announce:
                self._append_log(
                    f"用量已刷新：{summary['lines']} 条记录，"
                    f"合计 {format_cost(summary['totals'].get('cost', 0.0), summary['currency'])}"
                )

        if announce:
            self._run_task("统计用量…", worker, done)
            return
        # The first paint happens while the window is still opening, where _run_task's busy
        # lock would fight the startup audit -- so do it inline; it is one bounded file read.
        try:
            done(worker())
        except Exception:  # noqa: BLE001 - an empty usage tab must never block startup
            self.usage_note_var.set("读取用量失败，点「刷新用量」重试。")

    def _render_usage(self, summary: dict[str, Any]) -> None:
        currency = summary.get("currency", "USD")
        self._usage_summary = summary
        today, totals = summary["today"], summary["totals"]
        self.usage_today_var.set(
            f"今日：{today['requests']} 次 / {format_tokens(today['tokens'])} token / "
            f"{format_cost(today.get('cost', 0.0), currency)}"
        )
        self.usage_total_var.set(
            f"全部：{totals['requests']} 次 / {format_tokens(totals['tokens'])} token / "
            f"{format_cost(totals.get('cost', 0.0), currency)}"
        )
        span = ""
        if summary["first_time"] and summary["last_time"]:
            span = (
                str(summary["first_time"]).replace("T", " ").rstrip("Z")
                + " → "
                + str(summary["last_time"]).replace("T", " ").rstrip("Z")
                + f"（UTC，日志 {summary['log_bytes'] / 1024:.0f} KB）"
            )
        elif not summary["lines"]:
            span = "还没有路由记录。启动 Codex 跑一次请求，这里就会有数字。"
        self.usage_span_var.set(span)

        self.usage_model_tree.delete(*self.usage_model_tree.get_children())
        for model, row in sorted(
            summary["models"].items(), key=lambda item: (-item[1]["tokens"], item[0])
        ):
            self.usage_model_tree.insert(
                "",
                "end",
                values=(
                    model,
                    "、".join(row.get("vendors") or []),
                    row["requests"],
                    format_tokens(row["tokens_in"]),
                    format_tokens(row["tokens_out"]),
                    format_cost(row.get("cost", 0.0), currency),
                    "是" if row.get("priced") else "未定价",
                ),
                tags=("good" if row.get("priced") else "warn",),
            )
        self.usage_vendor_tree.delete(*self.usage_vendor_tree.get_children())
        for vendor, row in sorted(
            summary["vendors"].items(), key=lambda item: (-item[1]["requests"], item[0])
        ):
            rate = row["success_rate"]
            self.usage_vendor_tree.insert(
                "",
                "end",
                values=(
                    vendor,
                    row["requests"],
                    f"{rate * 100:.0f}%",
                    f"{row['p50_ms']} ms" if row["p50_ms"] else "-",
                    f"{row['p95_ms']} ms" if row["p95_ms"] else "-",
                    format_tokens(row["tokens"]),
                    format_cost(row.get("cost", 0.0), currency),
                ),
                tags=("good" if rate >= 0.95 else ("warn" if rate > 0 else "bad"),),
            )

        notes: list[str] = []
        if summary["legacy_lines"]:
            notes.append(
                f"其中 {summary['legacy_lines']} 条是升级前写的旧记录，只有请求数没有 token，"
                "花费统计从升级那一刻算起。"
            )
        if summary.get("unpriced"):
            notes.append("这些模型还没填价格：" + "、".join(summary["unpriced"][:8]))
        if summary["truncated"]:
            notes.append("日志太大，只统计了最近 8 MB。")
        self.usage_note_var.set("　".join(notes))

    def _edit_usage_prices(self) -> None:
        if self._busy:
            return
        prices = load_usage_prices(self.workspace.usage_prices_path)
        # Everything worth pricing: the models the registry offers, plus anything the log has
        # actually billed for (a model can outlive its provider entry) and any stale price row.
        names = {
            str(model.get("id") or "")
            for provider in self.registry.get("providers", [])
            for model in provider.get("models", [])
            if model.get("id")
        }
        names.update(getattr(self, "_usage_summary", {}).get("models", {}))
        names.update(prices.get("models") or {})
        dialog = PriceDialog(self, sorted(names), prices)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        try:
            save_usage_prices(self.workspace.usage_prices_path, dialog.result)
        except OSError as error:
            self._show_error("价格未保存", error)
            return
        self._append_log(f"价格表已保存（{len(dialog.result['models'])} 个模型计价）")
        self._refresh_usage()

    def _export_usage_csv(self) -> None:
        summary = getattr(self, "_usage_summary", None)
        if not summary or not summary["lines"]:
            messagebox.showinfo("没有用量", "还没有可导出的路由记录。", parent=self)
            return
        default = "codex-sota-usage-" + time.strftime("%Y%m%d-%H%M%S") + ".csv"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出用量",
            defaultextension=".csv",
            initialfile=default,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        currency = summary.get("currency", "USD")
        rows = [("类别", "名称", "供应商", "请求数", "成功率", "输入token", "输出token", f"花费({currency})")]
        for model, row in sorted(summary["models"].items()):
            rows.append((
                "模型", model, " ".join(row.get("vendors") or []), row["requests"],
                f"{row['success_rate'] * 100:.1f}%", row["tokens_in"], row["tokens_out"],
                f"{row.get('cost', 0.0):.6f}",
            ))
        for vendor, row in sorted(summary["vendors"].items()):
            rows.append((
                "供应商", vendor, "", row["requests"], f"{row['success_rate'] * 100:.1f}%",
                row["tokens_in"], row["tokens_out"], f"{row.get('cost', 0.0):.6f}",
            ))
        totals = summary["totals"]
        rows.append((
            "合计", "", "", totals["requests"], f"{totals['success_rate'] * 100:.1f}%",
            totals["tokens_in"], totals["tokens_out"], f"{totals.get('cost', 0.0):.6f}",
        ))
        try:
            # utf-8-sig: Excel on a Chinese Windows opens a plain UTF-8 CSV as mojibake, and the
            # BOM is the only thing that makes it guess right without an import wizard.
            with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerows(rows)
        except OSError as error:
            self._show_error("导出失败", error)
            return
        self._append_log(f"用量已导出到 {path}")

    def _export_log(self) -> None:
        content = self.log_text.get("1.0", "end").strip()
        if not content:
            messagebox.showinfo("没有内容", "运行记录还是空的。", parent=self)
            return
        default = "codex-sota-log-" + time.strftime("%Y%m%d-%H%M%S") + ".txt"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出运行记录",
            defaultextension=".txt",
            initialfile=default,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(content + "\n", encoding="utf-8")
        except OSError as error:
            self._show_error("导出失败", error)
            return
        self._append_log(f"运行记录已导出到 {path}")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _tick_health(self) -> None:
        """Keep the health board current from the router log — a local file read, no upstream calls."""
        if self._closing or not self.winfo_exists():
            return
        if not self._busy:
            try:
                self._render_health(read_router_vendor_health(self.workspace.log_path))
            except Exception:  # noqa: BLE001 - a background refresh must never surface a dialog
                pass
        self.after(HEALTH_REFRESH_MS, self._tick_health)

    def _show_shortcuts(self) -> None:
        messagebox.showinfo(
            "快捷键",
            "Ctrl+S        保存并应用当前供应商\n"
            "Ctrl+R        重新载入（丢弃未保存的改动）\n"
            "F5            刷新状态 / 全局审计\n"
            "Ctrl+T        测试已勾选模型\n"
            "Ctrl+E        一键体检全部供应商\n"
            "Ctrl+F        跳到模型筛选框\n"
            "Ctrl+Enter    启动 Codex\n"
            "双击 / 空格    切换某个模型的启用状态",
            parent=self,
        )

    def _report_degraded_vendors(self, health: dict[str, dict[str, Any]]) -> None:
        broken = degraded_vendors(health)
        if not broken:
            return
        detail = "；".join(
            f"{name} 最近 {data['total']} 次请求全部失败（HTTP {data['last_status']}）"
            for name, data in broken
        )
        self._append_log("上游异常：" + detail)
        self.header_status_var.set("上游异常")

    def _run_task(
        self,
        label: str,
        worker: Callable[[], Any],
        success: Callable[[Any], None],
        secrets: list[str | None] | None = None,
    ) -> None:
        if self._busy:
            return
        self._set_busy(True, label)
        secret_values = [value for value in (secrets or []) if value]

        def target() -> None:
            try:
                result = worker()
            except Exception as error:
                text = str(error)
                for value in secret_values:
                    text = text.replace(value, "<redacted>")
                self._task_events.put(("error", text, None))
                return
            self._task_events.put(("success", success, result))

        threading.Thread(target=target, daemon=True).start()

    def _drain_task_events(self) -> None:
        if self._closing:
            return
        while True:
            try:
                kind, payload, result = self._task_events.get_nowait()
            except queue.Empty:
                break
            if kind == "success":
                self._task_succeeded(payload, result)
            else:
                self._task_failed(str(payload))
        self.after(50, self._drain_task_events)

    def _task_succeeded(self, callback: Callable[[Any], None], result: Any) -> None:
        try:
            callback(result)
        except Exception as error:
            self._show_error("操作未完成", error)
        finally:
            self._set_busy(False, "就绪")

    def _task_failed(self, detail: str) -> None:
        self._set_busy(False, "操作失败")
        self._append_log("错误：" + detail)
        messagebox.showerror("操作失败", detail, parent=self)

    def _set_busy(self, busy: bool, label: str) -> None:
        self._busy = busy
        self.status_var.set(label)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        general_state = "disabled" if busy else "normal"
        for widget in (
            self.add_button,
            self.move_up_button,
            self.move_down_button,
            self.reload_button,
            self.audit_button,
            self.refresh_button,
            self.launch_button,
            self.open_config_button,
            self.open_registry_button,
            self.checkup_button,
            self.export_log_button,
            self.clear_log_button,
            self.shortcuts_button,
            self.restart_router_button,
            self.export_config_button,
            self.backup_config_button,
            self.restore_config_button,
            self.lint_button,
            self.claude_publish_button,
            self.claude_release_button,
            self.claude_switch_button,
            self.claude_refresh_button,
            self.usage_refresh_button,
            self.usage_price_button,
            self.usage_open_price_button,
            self.usage_export_button,
        ):
            widget.configure(state=general_state)
        if busy:
            self.provider_tree.state(["disabled"])
        else:
            self.provider_tree.state(["!disabled"])
        self._set_editor_protected(self._protected)

    def _open_path(self, path: Path) -> None:
        if self._busy:
            return
        try:
            os.startfile(str(path))
        except OSError as error:
            self._show_error("无法打开", error)

    def _on_close(self) -> None:
        self._closing = True
        self.api_key_var.set("")
        self.destroy()

    def _append_log(self, message: str) -> None:
        if not hasattr(self, "log_text"):
            return
        timestamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _show_error(self, title: str, error: Exception) -> None:
        detail = str(error)
        key = self.api_key_var.get().strip() if hasattr(self, "api_key_var") else ""
        if key:
            detail = detail.replace(key, "<redacted>")
        self._append_log("错误：" + detail)
        messagebox.showerror(title, detail, parent=self)


def main() -> int:
    enable_dpi_awareness()
    app = CodexSotaApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
