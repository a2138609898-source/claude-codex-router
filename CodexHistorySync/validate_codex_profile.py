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
        if data.get("model_provider") != "true_sota":
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
        provider = _provider(data, "true_sota")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("Plus", "Cockpit", "Sota"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--probe-cockpit", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=2.0)
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
    try:
        if args.probe_cockpit:
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
    except Exception:
        valid, reason = False, "validation_failed"

    payload: dict[str, Any] = {"valid": valid, "reason": reason}
    if repair_report is not None:
        payload["repair"] = repair_report
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
