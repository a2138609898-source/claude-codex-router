"""Move messages-only providers from the `vendor--` model prefix to `vendor.anthropic.`.

Claude Desktop decides whether a model gets a thinking-effort control by canonicalizing the
model id and looking it up in a table compiled into the app.  Its canonicalizer strips a
leading `<vendor>.anthropic.` -- the shape a Bedrock model id has -- but knows nothing about
`vendor--`, so `tango--claude-opus-5` misses the table and the picker is not rendered, while
`tango.anthropic.claude-opus-5` reduces to `claude-opus-5` and hits it.  There is no config
field for this: the 3P profile's per-model schema has no thinking key at all.

Only the Claude workspace is migrated by default.  The Codex side keeps `vendor--` on purpose:
prefixes are opaque to the Codex App (each catalog entry carries its own reasoning levels), and
its slugs are pinned by `model = ...` in ~/.codex-sota/config.toml.

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


def report_slugs(registry: dict[str, object]) -> None:
    for slug in R.selectable_slugs(registry):
        print(f"    {slug:52s} {cd.thinking_summary(slug)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=R.CLAUDE.name, choices=sorted(R.WORKSPACES))
    parser.add_argument("--apply", action="store_true", help="actually rewrite providers.json")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="after writing, refresh codex-sota's own Claude profile entry (never appliedId)",
    )
    args = parser.parse_args()
    workspace = R.WORKSPACES[args.workspace]

    if not workspace.registry_path.exists():
        print(f"没有 {workspace.registry_path}，这个工作区还没配过供应商")
        return 1
    # Not routing, just renaming: a provider whose key file went missing should not block the
    # rename, and validate_provider still enforces the key everywhere that actually routes.
    registry = R.load_registry(workspace.registry_path, allow_missing_secrets=True)
    print(f"工作区   : {workspace.label}  ({workspace.registry_path})")
    print(f"路由端口 : {workspace.router_port}   协议: {workspace.protocol}")
    print("\n改之前发布的 slug：")
    report_slugs(registry)

    changes = R.upgrade_messages_prefixes(registry)
    if not changes:
        print("\nNOTHING TO DO：没有需要改前缀的 messages 供应商（可能已经改过了）。")
        return 0
    print("\n要改的前缀：")
    for provider_id, old, new in changes:
        print(f"    {provider_id:16s} {old!r} -> {new!r}")
    print("\n改之后发布的 slug：")
    report_slugs(registry)
    # Cheap insurance: the same rules that reject a hand-edited registry apply here, and it is
    # better to fail before touching the file than to write something the router will refuse.
    R.validate_registry(json.loads(json.dumps(registry)), allow_missing_secrets=True)

    if not args.apply:
        print("\nDRY RUN。加 --apply 才会写入。")
        return 0

    with R.registry_write_lock(workspace=workspace):
        # Re-read under the lock: the manager may have saved something since the dry-run read.
        fresh = R.load_registry(workspace.registry_path, allow_missing_secrets=True)
        again = R.upgrade_messages_prefixes(fresh)
        if not again:
            print("\nNOTHING TO DO：另一个进程已经改过了。")
            return 0
        backup_dir = workspace.root / "backups" / "prefix-migration"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"providers.{R.utc_now().replace(':', '').replace('-', '')}.json"
        shutil.copy2(workspace.registry_path, backup)
        R.write_registry(fresh, workspace.registry_path, allow_missing_secrets=True)
        print(f"\n已写入 {workspace.registry_path}")
        print(f"备份   {backup}")

    if args.publish:
        if workspace is not R.CLAUDE:
            print("\n--publish 只对 Claude 工作区有意义，已跳过。")
        else:
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
