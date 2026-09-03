"""Isolation check: nothing done in the Claude workspace may touch Codex state, or vice versa.

Runs real transactional operations (add / reorder / bookkeeping / delete) against a throwaway
Claude root and asserts every Codex file is byte-identical afterwards. Also asserts the reverse,
because the leak that mattered was reorder/delete writing to whichever root was the default.
"""

import hashlib
import json
import shutil
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
import os.path  # noqa: E402 - keeps this script runnable from any checkout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sota_registry as R  # noqa: E402

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))


def fingerprint(root: Path) -> dict[str, str]:
    """Hash every file under a config root, so any stray write shows up."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # Files the Codex app itself churns constantly are not ours and would drown the signal.
        rel = path.relative_to(root).as_posix()
        if any(part in rel for part in ("log/", "sessions/", "sqlite", "-wal", "-shm", ".tmp",
                                        "global-state", "history.jsonl", "models_cache")):
            continue
        try:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except (OSError, PermissionError):
            # The running Codex App keeps some files exclusively open; size+mtime still
            # detects any write we might have made to them.
            stat = path.stat()
            out[rel] = f"locked:{stat.st_size}:{stat.st_mtime_ns}"
    return out


def make_workspace(name: str, root: Path, port: int) -> R.Workspace:
    return R.Workspace(
        name=name, label=name, root=root, router_port=port,
        router_starter=root / "starter.ps1", protocol="messages" if name == "claude" else "responses",
    )


def seed(ws: R.Workspace) -> None:
    """A minimal but valid registry: one enabled default with an empty prefix."""
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.secrets_root.mkdir(parents=True, exist_ok=True)
    # The default has to keep an empty prefix, and only codex_auth providers may — dpapi ones
    # get a prefix derived from their id — so the seed default mirrors the real registry's.
    ws.auth_path.write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "seed-auth-token"}), encoding="utf-8"
    )
    default = {
        "workspace": ws.name,
        "id": "seed_vendor", "name": "Seed", "base_url": "https://seed.example",
        "prefix": "", "enabled": True, "protected": False, "is_default": True,
        "auth_type": "codex_auth", "auth_header": "Authorization",
        "auth_prefix": "Bearer ", "models_path": "/v1/models",
        "responses_path": "/v1/responses", "messages_path": "/v1/messages",
        "timeout_seconds": 120, "extra_headers": {}, "protocols": ["responses", "messages"],
        "models": [{"id": "m-one", "enabled": True, "display_name": "", "description": ""}],
    }
    ws.registry_path.write_text(
        json.dumps({"version": 1, "providers": [default]}, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copy(R.CODEX.source_catalog_path, ws.source_catalog_path)
    R.build_model_catalog(R.load_registry(ws.registry_path), ws.source_catalog_path, ws.catalog_path)


def exercise(ws: R.Workspace) -> None:
    """Every transactional write path, so a leak in any of them shows up."""
    added = {
        "workspace": ws.name,
        "id": "second_vendor", "name": "Second", "base_url": "https://second.example",
        "prefix": "second--", "enabled": True, "protected": False, "is_default": False,
        "auth_type": "dpapi", "secret_file": "second.dpapi",
        "entropy": "Check.Isolation.Second.V1", "auth_header": "Authorization",
        "auth_prefix": "Bearer ", "models_path": "/v1/models",
        "responses_path": "/v1/responses", "messages_path": "/v1/messages",
        "timeout_seconds": 120, "extra_headers": {}, "protocols": ["responses", "messages"],
        "models": [{"id": "m-two", "enabled": True, "display_name": "", "description": ""}],
    }
    R.apply_provider(added, "second-token", restart=False, workspace=ws)
    R.reorder_providers(["second_vendor", "seed_vendor"], workspace=ws, restart=False)
    marked = deepcopy(R.load_registry(ws.registry_path)["providers"])
    for provider in marked:
        for model in provider["models"]:
            model["last_test_status"] = "ready"
    R.save_provider_bookkeeping(marked, workspace=ws)
    R.delete_provider("second_vendor", workspace=ws, restart=False)


print("=== 1) 在 Claude 工作区做完整一轮事务，Codex 必须一个字节都不变 ===")
codex_before = fingerprint(R.CODEX.root)
print(f"  Codex 侧纳入比对的文件 {len(codex_before)} 个")
with tempfile.TemporaryDirectory() as temporary:
    claude_ws = make_workspace("claude", Path(temporary) / "claude-sota", 17994)
    with mock.patch.dict(
        R.WORKSPACES, {"claude": claude_ws}
    ):
        seed(claude_ws)
        exercise(claude_ws)
        left = [p["id"] for p in R.load_registry(claude_ws.registry_path)["providers"]]
        check("Claude 侧操作确实生效", left == ["seed_vendor"], f"剩下 {left}")
codex_after = fingerprint(R.CODEX.root)
changed = {k for k in set(codex_before) | set(codex_after)
           if codex_before.get(k) != codex_after.get(k)}
check("Codex 配置根完全没被碰过", not changed, f"变动: {sorted(changed) or '无'}")

print()
print("=== 2) 反向：在 Codex 工作区做完整一轮，Claude 根不能被建出来 ===")
with tempfile.TemporaryDirectory() as temporary:
    codex_ws = make_workspace("codex", Path(temporary) / "codex-sota", 17895)
    claude_probe = Path(temporary) / "claude-sota"
    claude_ws = make_workspace("claude", claude_probe, 17994)
    with mock.patch.dict(
        R.WORKSPACES, {"codex": codex_ws, "claude": claude_ws}
    ):
        seed(codex_ws)
        exercise(codex_ws)
        left = [p["id"] for p in R.load_registry(codex_ws.registry_path)["providers"]]
        check("Codex 侧操作确实生效", left == ["seed_vendor"], f"剩下 {left}")
        check("没有顺手创建 Claude 根", not claude_probe.exists(),
              f"{claude_probe} 存在={claude_probe.exists()}")

print()
print("=== 3) 两个工作区的路径不能有任何重叠 ===")
codex_paths = {R.CODEX.registry_path, R.CODEX.catalog_path, R.CODEX.secrets_root,
               R.CODEX.lock_path, R.CODEX.auth_path, R.CODEX.log_path, R.CODEX.pid_path,
               R.CODEX.deleted_root, R.CODEX.source_catalog_path}
claude_paths = {R.CLAUDE.registry_path, R.CLAUDE.catalog_path, R.CLAUDE.secrets_root,
                R.CLAUDE.lock_path, R.CLAUDE.auth_path, R.CLAUDE.log_path, R.CLAUDE.pid_path,
                R.CLAUDE.deleted_root, R.CLAUDE.source_catalog_path}
overlap = codex_paths & claude_paths
check("九类路径零重叠", not overlap, f"重叠: {overlap or '无'}")
check("端口不同", R.CODEX.router_port != R.CLAUDE.router_port,
      f"{R.CODEX.router_port} vs {R.CLAUDE.router_port}")
check("启动脚本不同", R.CODEX.router_starter != R.CLAUDE.router_starter)

print()
print(f"合计 {sum(results)}/{len(results)} 项通过")
raise SystemExit(0 if all(results) else 1)
