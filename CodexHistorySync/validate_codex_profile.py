from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _read_config(root: Path) -> dict[str, Any]:
    config_path = root / "config.toml"
    with config_path.open("rb") as stream:
        data = tomllib.load(stream)
    if not isinstance(data, dict):
        raise ValueError("config.toml did not contain a TOML table")
    return data


def _provider(data: dict[str, Any], provider_id: str) -> dict[str, Any]:
    providers = data.get("model_providers")
    if not isinstance(providers, dict):
        return {}
    provider = providers.get(provider_id)
    return provider if isinstance(provider, dict) else {}


def _catalog_slugs(catalog: Path) -> set[str] | None:
    """Read the selectable model slugs without contacting a provider."""
    try:
        with catalog.open("rb") as stream:
            data = json.load(stream)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    return {
        str(model.get("slug"))
        for model in models
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }


def _without_context_1m_suffix(value: str) -> str:
    suffix = "[1m]"
    return value[:-len(suffix)] if value.lower().endswith(suffix) else value


def _same_path(value: object, expected: Path, config_root: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_root / candidate
    try:
        return candidate.resolve(strict=False) == expected.resolve(strict=False)
    except OSError:
        return False


def _catalog_slug_list(catalog: Path) -> list[str]:
    """The selectable slugs in catalog order (the order is the picker's priority)."""
    try:
        with catalog.open("rb") as stream:
            data = json.load(stream)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [
        str(model.get("slug"))
        for model in models
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    ]


def _provider_prefix_of(slug: str) -> str:
    if ".anthropic." in slug:
        return slug.split(".anthropic.", 1)[0] + ".anthropic."
    if "--" in slug:
        return slug.split("--", 1)[0] + "--"
    return ""


def repair_pinned_models(root: Path, catalog: Path) -> dict[str, Any]:
    """Rewrite config.toml's pinned model names when they fell out of the catalog.

    Providers and model mappings change constantly; the Codex App pins whatever the user
    last selected into config.toml, so a stale pin is a routine event, not a broken
    install.  Refusing to launch over it was wrong.  Prefer another model from the same
    provider namespace; fall back to the first selectable model otherwise.
    """
    config_path = root / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
    except (OSError, ValueError):
        return {"reason": "config_unreadable", "repaired": {}}
    original_text = text
    slugs = _catalog_slug_list(catalog)
    slug_set = set(slugs)
    if not slugs:
        return {"reason": "catalog_unreadable", "repaired": {}}
    repaired: dict[str, Any] = {}
    for field in ("model", "review_model"):
        original_value = parsed.get(field)
        if not isinstance(original_value, str):
            continue
        current = _without_context_1m_suffix(original_value.strip())
        if current in slug_set:
            continue
        prefix = _provider_prefix_of(current)
        candidates = [slug for slug in slugs if prefix and slug.startswith(prefix)]
        replacement = (candidates or slugs)[0]
        expected = {**parsed, field: replacement}
        pattern = re.compile(r'(?m)^([ \t]*' + field + r'''[ \t]*=[ \t]*)("(?:[^"\\\n]|\\.)*"|'[^'\n]*')''')
        for match in pattern.finditer(text):
            candidate = text[:match.start(2)] + json.dumps(replacement, ensure_ascii=False) + text[match.end(2):]
            try:
                candidate_data = tomllib.loads(candidate)
            except ValueError:
                continue
            # Match the parsed root setting, never a profile or a multiline string.
            if candidate_data == expected:
                text, parsed = candidate, candidate_data
                repaired[field] = {"from": original_value, "to": replacement}
                break
    if repaired:
        fd, name = tempfile.mkstemp(prefix=".config-repair-", dir=root)
        os.close(fd)
        temporary = Path(name)
        try:
            temporary.write_text(text, encoding="utf-8")
            if config_path.read_text(encoding="utf-8") != original_text:
                return {"reason": "config_changed_during_repair", "repaired": {}}
            os.replace(temporary, config_path)
        except OSError as error:
            return {"reason": f"write_failed: {error}", "repaired": {}}
        finally:
            temporary.unlink(missing_ok=True)
    return {"reason": "ok", "repaired": repaired}


def validate_profile(profile: str, root: Path, catalog: Path | None) -> tuple[bool, str]:
    try:
        data = _read_config(root)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return False, "config_unreadable_or_invalid"

    if data.get("cli_auth_credentials_store") != "file":
        return False, "credential_store_mismatch"

    if profile == "Plus":
        if data.get("forced_login_method") != "chatgpt":
            return False, "login_method_mismatch"
        selected = data.get("model_provider")
        if selected not in (None, "", "openai"):
            return False, "unexpected_model_provider"
        if isinstance(data.get("openai_base_url"), str) and data["openai_base_url"].strip():
            return False, "unexpected_openai_base_url"
        return True, "ok"

    if data.get("forced_login_method") != "api":
        return False, "login_method_mismatch"

    if profile == "Cockpit":
        if data.get("model_provider") != "codex_local_access":
            return False, "model_provider_mismatch"
        provider = _provider(data, "codex_local_access")
        if provider.get("base_url") not in (
            "http://localhost:56319/v1",
            "http://127.0.0.1:56319/v1",
        ):
            return False, "base_url_mismatch"
        if provider.get("wire_api") != "responses":
            return False, "wire_api_mismatch"
        if provider.get("requires_openai_auth") is not False:
            return False, "auth_requirement_mismatch"
        token = provider.get("experimental_bearer_token")
        if not isinstance(token, str) or not token.strip():
            return False, "bearer_token_missing"
        return True, "ok"

    if profile == "Sota":
        selected_provider = data.get("model_provider")
        if not isinstance(selected_provider, str) or not selected_provider.strip() or not _provider(data, selected_provider):
            return False, "model_provider_mismatch"
        if catalog is None or not _same_path(data.get("model_catalog_json"), catalog, root):
            return False, "model_catalog_mismatch"
        slugs = _catalog_slugs(catalog) if catalog is not None else None
        if slugs is None:
            return False, "model_catalog_unreadable"
        for field, required in (("model", True), ("review_model", False)):
            value = data.get(field)
            if value is None:
                if required:
                    return False, "model_missing"
                continue
            if not isinstance(value, str) or not value.strip():
                return False, f"{field}_invalid"
            slug = _without_context_1m_suffix(value.strip())
            if "--" not in slug:
                return False, f"{field}_unqualified"
            if slug not in slugs:
                return False, f"{field}_not_in_catalog"
        provider = _provider(data, selected_provider)
        if provider.get("base_url") != "http://127.0.0.1:17895":
            return False, "base_url_mismatch"
        if provider.get("wire_api") != "responses":
            return False, "wire_api_mismatch"
        if provider.get("requires_openai_auth") is not True:
            return False, "auth_requirement_mismatch"
        return True, "ok"

    return False, "unknown_profile"


def probe_cockpit(root: Path, timeout_seconds: float) -> tuple[bool, str]:
    valid, reason = validate_profile("Cockpit", root, None)
    if not valid:
        return False, reason

    data = _read_config(root)
    provider = _provider(data, "codex_local_access")
    base_url = str(provider["base_url"]).rstrip("/")
    token = str(provider["experimental_bearer_token"])
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return False, "unexpected_http_status"
            payload = json.load(response)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError):
        return False, "request_failed"
    if not isinstance(payload, dict):
        return False, "invalid_response"
    models = payload.get("data")
    if not isinstance(models, list):
        return False, "invalid_models_response"
    return True, "ok"


def validate_chatgpt_direct_profile(
    root: Path, protected_root: Path | None = None
) -> tuple[bool, str]:
    """Read-only guard for the manager's isolated ChatGPT-account launcher.

    Never repair, migrate, copy, or reset an existing config/auth file here.
    In particular, an account profile must not resolve into the SOTA directory,
    or redirect OpenAI account credentials to a custom provider endpoint.
    """
    try:
        selected_root = root.resolve()
        configured = str(os.environ.get("CODEX_SOTA_CODEX_ROOT") or "").strip()
        fallback = Path(os.path.expandvars(configured)).expanduser() if configured else Path.home() / ".codex-sota"
        sota_root = (protected_root or fallback).resolve()
        if selected_root.is_relative_to(sota_root) or sota_root.is_relative_to(selected_root):
            return False, "profile_roots_overlap"
    except (OSError, ValueError):
        return False, "profile_root_unreadable"
    valid, reason = validate_profile("Plus", root, None)
    if not valid:
        return valid, reason
    data = _read_config(root)
    if data.get("chatgpt_base_url"):
        return False, "unexpected_chatgpt_base_url"
    provider = _provider(data, "openai")
    for field in (
        "base_url", "env_key", "experimental_bearer_token", "http_headers",
        "env_http_headers", "auth", "gateway_oauth", "model_catalog_url",
    ):
        if field in provider:
            return False, "unexpected_openai_provider_override"
    if provider.get("requires_openai_auth") is False:
        return False, "openai_auth_disabled"
    return True, "ok"



def _remove_root_assignment(text: str, field: str) -> str:
    """Remove one root TOML value, preserving all other parsed values and text.

    A regex alone could edit a profile or a line inside a multiline prompt.
    Accept a candidate only when parsing proves the intended root-only change.
    """
    expected = tomllib.loads(text)
    if field not in expected:
        return text
    del expected[field]
    key = re.escape(field)
    starts = re.compile(r'(?m)^[ \t]*(?:' + key + '|"' + key + '"|\'' + key + r"')[ \t]*=")
    endings = [match.end() for match in re.finditer(r"\n", text)] + [len(text)]
    for match in starts.finditer(text):
        for end in endings:
            if end <= match.start():
                continue
            candidate = text[:match.start()] + text[end:]
            try:
                if tomllib.loads(candidate) == expected:
                    return candidate
            except ValueError:
                continue
    raise ValueError("Cannot safely remove root model setting")


def prepare_chatgpt_official_models(
    root: Path, protected_root: Path | None = None
) -> dict[str, Any]:
    """Explicit, narrow migration; ordinary validation remains read-only.

    The launcher calls this only after normal app exit, while holding its
    lifecycle/history leases. Remove a static catalog and the model selections
    made under it, then leave future discovery, caching, and defaults to Codex.
    Never fetch or hard-code a model list, change credentials, or touch SOTA.
    """
    report: dict[str, Any] = {
        "valid": False, "reason": "official_model_preparation_failed",
        "mode": "official_account_model_discovery", "changed": False,
        "removed_fields": [],
    }
    valid, reason = validate_chatgpt_direct_profile(root, protected_root)
    if not valid:
        return {**report, "reason": reason}
    config_path = root / "config.toml"
    temporary: Path | None = None
    try:
        resolved_root = root.resolve()
        if config_path.resolve() != resolved_root / "config.toml":
            return {**report, "reason": "config_path_outside_account_profile"}
        auth_path = root / "auth.json"
        if auth_path.exists():
            auth = json.loads(auth_path.read_bytes())
            if not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt":
                return {**report, "reason": "incompatible_account_credentials"}
        original = config_path.read_bytes()
        text = original.decode("utf-8")
        parsed = tomllib.loads(text)
        if "model_catalog_json" not in parsed:
            return {**report, "valid": True, "reason": "ok"}
        removed = [field for field in ("model_catalog_json", "model", "review_model")
                   if field in parsed]
        for field in removed:
            text = _remove_root_assignment(text, field)
        # A second semantic check bounds the entire migration, not just each edit.
        if tomllib.loads(text) != {key: value for key, value in parsed.items() if key not in removed}:
            return {**report, "reason": "unexpected_model_config_change"}
        if config_path.read_bytes() != original:
            return {**report, "reason": "config_changed_during_preparation"}
        fd, name = tempfile.mkstemp(prefix="config.toml.before-official-models-", suffix=".bak", dir=root)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        report["backup_path"] = name
        fd, name = tempfile.mkstemp(prefix=".official-model-config-", dir=root)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if config_path.read_bytes() != original:
            return {**report, "reason": "config_changed_during_preparation"}
        os.replace(temporary, config_path)
        return {**report, "valid": True, "reason": "ok", "changed": True,
                "removed_fields": removed}
    except (OSError, ValueError):
        return report
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("Plus", "Cockpit", "Sota"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--probe-cockpit", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=2.0)
    parser.add_argument(
        "--chatgpt-direct", action="store_true",
        help="Read-only validation of the separate official-account profile unless explicit model preparation is requested.",
    )
    parser.add_argument(
        "--prepare-official-models", action="store_true",
        help=("Explicitly back up and remove a static account model catalog and its selected models. "
              "Requires --profile Plus --chatgpt-direct and an idle account profile; never edits credentials."),
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Rewrite a stale pinned model (model/review_model no longer in the catalog) "
            "and re-validate.  Only that one failure class is repaired: a genuinely broken "
            "config still reports invalid."
        ),
    )
    args = parser.parse_args()

    repair_report: dict[str, Any] | None = None
    model_provider: str | None = None
    try:
        if args.prepare_official_models:
            if (args.profile != "Plus" or not args.chatgpt_direct or args.repair
                    or args.probe_cockpit or args.catalog is not None):
                valid, reason = False, "official_models_require_direct_plus_profile"
            else:
                repair_report = prepare_chatgpt_official_models(args.root)
                valid, reason = repair_report["valid"], repair_report["reason"]
        elif args.chatgpt_direct:
            if args.profile != "Plus" or args.repair or args.probe_cockpit:
                valid, reason = False, "chatgpt_direct_requires_readonly_plus_profile"
            else:
                valid, reason = validate_chatgpt_direct_profile(args.root)
        elif args.probe_cockpit:
            if args.profile != "Cockpit":
                valid, reason = False, "probe_requires_cockpit_profile"
            else:
                valid, reason = probe_cockpit(args.root, max(0.1, args.timeout_seconds))
        else:
            valid, reason = validate_profile(args.profile, args.root, args.catalog)
            if (
                not valid
                and args.repair
                and args.catalog is not None
                and reason in {"model_not_in_catalog", "review_model_not_in_catalog"}
            ):
                repair_report = repair_pinned_models(args.root, args.catalog)
                if repair_report.get("repaired"):
                    valid, reason = validate_profile(args.profile, args.root, args.catalog)
        selected_provider = _read_config(args.root).get("model_provider")
        if isinstance(selected_provider, str):
            model_provider = selected_provider
    except Exception:
        valid, reason = False, "validation_failed"

    payload: dict[str, Any] = {"valid": valid, "reason": reason}
    if model_provider is not None:
        payload["model_provider"] = model_provider
    if repair_report is not None:
        payload["repair"] = repair_report
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
