"""Give Claude Desktop's thinking-effort slider to models whose vendor id hides the tier.

Claude Desktop renders a thinking control only for a model whose id, after its own
canonicalization, is in a table compiled into the app.  The prefix migration
(migrate_messages_prefixes.py) fixes the vendor part of the slug, but it cannot help a model
whose id itself carries the tier: `claude-opus-5-thinking` canonicalizes to
`claude-opus-5-thinking`, which is not in the table, so the picker stays hidden however the
model is prefixed.

The lever for that case is `publish_as`: the slug the app selects the model by, while the
upstream still receives the vendor's own id.  This tool sets it wherever dropping a cosmetic
tier suffix turns a slug the app does not recognize into one it does -- and nowhere else.

Only the Claude workspace is eligible; `publish_as` is rejected outright for anything that also
speaks responses, because the Codex App's slugs are pinned by `model = ...` in config.toml and
by the generated catalog.

Run with --apply to write; the default is a dry run.  Add --publish to also refresh codex-sota's
own Claude Desktop profile entry -- `appliedId` is never touched, and a running Claude Desktop
keeps its current models until it is restarted, since the profile is read at startup.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

sys.stdout.reconfigure(encoding="utf-8")

import claude_desktop as cd  # noqa: E402
import sota_registry as R  # noqa: E402

# Suffixes a vendor bolts onto a model name to mean "the thinking variant".  Deliberately short:
# every entry here is a claim that the shortened name denotes the same model, so `-high`-style
# tier names stay out -- those can be genuinely different deployments.
COSMETIC_TIER_SUFFIXES = ("-thinking", "-reasoning", "-think")


def plan(registry: dict[str, object]) -> list[tuple[str, str, str, str]]:
    """Rows of (provider id, model id, published slug now, slug to publish as).

    Returns an empty list when there is nothing to do, so the tool is safe to re-run.
    """
    rows: list[tuple[str, str, str, str]] = []
    for provider in registry.get("providers") or []:
        protocols = provider.get("protocols") or []
        if "messages" not in protocols or "responses" in protocols:
            continue
        for model in provider.get("models") or []:
            if not model.get("enabled"):
                continue
            current = R.published_slug(provider, model)
            # Already recognized -- there is nothing to buy by renaming it.
            if cd.thinking_effort_levels(current) is not None:
                continue
            for suffix in COSMETIC_TIER_SUFFIXES:
                if not current.lower().endswith(suffix):
                    continue
                candidate = current[: -len(suffix)]
                if cd.thinking_effort_levels(candidate) is None:
                    continue
                rows.append(
                    (str(provider.get("id")), str(model.get("id")), current, candidate)
                )
                break
    return rows


def apply_plan(registry: dict[str, object], rows: list[tuple[str, str, str, str]]) -> None:
    wanted = {(row[0], row[1]): row[3] for row in rows}
    for provider in registry.get("providers") or []:
        for model in provider.get("models") or []:
            slug = wanted.get((str(provider.get("id")), str(model.get("id"))))
            if slug:
                model["publish_as"] = slug


def report_slugs(registry: dict[str, object]) -> None:
    for slug in R.selectable_slugs(registry):
        print(f"    {slug:48s} {cd.thinking_summary(slug)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually rewrite providers.json")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="after writing, refresh codex-sota's own Claude profile entry (never appliedId)",
    )
    args = parser.parse_args()
    workspace = R.CLAUDE

    if not workspace.registry_path.exists():
        print(f"没有 {workspace.registry_path}，这个工作区还没配过供应商")
        return 1
    registry = R.load_registry(workspace.registry_path, allow_missing_secrets=True)
    print(f"工作区   : {workspace.label}  ({workspace.registry_path})")
    print("\n改之前发布的 slug：")
    report_slugs(registry)

    rows = plan(registry)
    if not rows:
        print("\nNOTHING TO DO：没有能靠改发布名拿到思考控件的模型（可能已经设过了）。")
        return 0
    print("\n要设的 publish_as（上游收到的仍然是厂商自己的模型名）：")
    for provider_id, model_id, current, candidate in rows:
        print(f"    {provider_id:12s} 上游 {model_id:28s} {current} -> {candidate}")
    apply_plan(registry, rows)
    print("\n改之后发布的 slug：")
    report_slugs(registry)
    # Fail before touching the file: this is also what catches a candidate slug that collides
    # with one another provider already publishes.
    R.validate_registry(json.loads(json.dumps(registry)), allow_missing_secrets=True)

    if not args.apply:
        print("\nDRY RUN。加 --apply 才会写入。")
        return 0

    with R.registry_write_lock(workspace=workspace):
        fresh = R.load_registry(workspace.registry_path, allow_missing_secrets=True)
        again = plan(fresh)
        if not again:
            print("\nNOTHING TO DO：另一个进程已经设过了。")
            return 0
        apply_plan(fresh, again)
        backup_dir = workspace.root / "backups" / "publish-as"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"providers.{R.utc_now().replace(':', '').replace('-', '')}.json"
        shutil.copy2(workspace.registry_path, backup)
        R.write_registry(fresh, workspace.registry_path, allow_missing_secrets=True)
        print(f"\n已写入 {workspace.registry_path}")
        print(f"备份   {backup}")

    if args.publish:
        models = cd.build_inference_models(R.load_registry(R.CLAUDE.registry_path))
        result = cd.write_profile(models, cd.DEFAULT_GATEWAY_URL)
        print(f"\nClaude 档已刷新：{result['models']} 个模型，网关 {result['gateway_url']}")
        print(f"备份 {result['backup']}；当前生效档未改动")
    else:
        print("\n还没写 Claude 档。用管理器的「写入 Claude 档」按钮，或重跑一次加 --publish。")
    print("Claude Desktop 在启动时才读这份档，所以要退出重开才会看到新的模型名和思考控件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
