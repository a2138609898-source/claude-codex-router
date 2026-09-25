"""Claude Desktop config-library writes, against a throwaway library — never the real one.

The whole point of the module is that it must not disturb a neighbour's profile, so the
fixture builds a library that looks like the real one (two foreign entries, one applied) and
asserts those files come out byte-identical.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_desktop as cd  # noqa: E402

FOREIGN_A = "79ae43da-be0a-4eb1-bf7c-2aee8fb6db95"
FOREIGN_B = "00000000-0000-4000-8000-000000157210"


def build_library(root: Path) -> dict[str, bytes]:
    library = root / "configLibrary"
    library.mkdir(parents=True)
    payloads = {}
    for entry_id, name, url in (
        (FOREIGN_A, "APIKEY.FUN", "https://fast.example.pro"),
        (FOREIGN_B, "CC Switch", "http://127.0.0.1:15721/claude-desktop"),
    ):
        body = {
            "coworkEgressAllowedHosts": ["*"],
            "disableDeploymentModeChooser": True,
            "inferenceGatewayApiKey": f"key-for-{name}",
            "inferenceGatewayAuthScheme": "bearer",
            "inferenceGatewayBaseUrl": url,
            "inferenceModels": [{"name": "claude-opus-4-5", "labelOverride": "opus"}],
            "inferenceProvider": "gateway",
        }
        path = library / f"{entry_id}.json"
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        payloads[entry_id] = path.read_bytes()
    (library / "_meta.json").write_text(
        json.dumps(
            {
                "appliedId": FOREIGN_B,
                "entries": [
                    {"id": FOREIGN_A, "name": "APIKEY.FUN"},
                    {"id": FOREIGN_B, "name": "CC Switch"},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return payloads


def point_module_at(root: Path) -> None:
    cd.CLAUDE_3P_ROOT = root
    cd.CONFIG_LIBRARY = root / "configLibrary"
    cd.META_PATH = cd.CONFIG_LIBRARY / "_meta.json"
    cd.BACKUP_ROOT = root / "backups"
    cd.LIBRARY_LOCK_PATH = root.parent / "claude-library.lock"
    cd.SLOT_CLAIM_PATH = root.parent / "claude-slot-claim.json"


MODELS = [
    {"name": "jw--claude-opus-5", "labelOverride": "JustWoker · claude-opus-5"},
    {"name": "jw--claude-sonnet-5", "labelOverride": "JustWoker · claude-sonnet-5"},
]

if __name__ == "__main__":
    temporary = Path(tempfile.mkdtemp())
    local = temporary / "Local"
    roaming = temporary / "Roaming"
    root = local / "Claude-3p"
    originals = build_library(root)
    point_module_at(root)
    local_1p = local / "Claude" / "claude_desktop_config.json"
    roaming_1p = roaming / "Claude" / "claude_desktop_config.json"
    local_1p.parent.mkdir(parents=True)
    roaming_1p.parent.mkdir(parents=True)
    local_1p.write_bytes(b'{"deploymentMode":"1p","owner":"local"}')
    roaming_1p.write_bytes(b'{"deploymentMode":"1p","owner":"roaming"}')
    one_p_before = (local_1p.read_bytes(), roaming_1p.read_bytes())
    environment = mock.patch.dict(
        os.environ, {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    )
    environment.start()
    results: list[bool] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))

    print("=== 1) 写入前先认得别人的档 ===")
    st = cd.library_status()
    check("看到两个外来档，CC Switch 生效，我的不存在",
          len(st["entries"]) == 2 and st["applied_name"] == "CC Switch" and not st["mine_present"],
          f"applied={st['applied_name']} entries={[e['name'] for e in st['entries']]}")

    print("\n=== 2) 写入我的档：默认不抢生效槽 ===")
    result = cd.write_profile(MODELS, "http://127.0.0.1:17895")
    st = cd.library_status()
    check("我的档已加入，但 CC Switch 依然生效",
          st["mine_present"] and len(st["entries"]) == 3
          and st["applied_id"] == FOREIGN_B and result["applied"] is False,
          f"applied={st['applied_name']} entries={[e['name'] for e in st['entries']]}")
    check("没写档就没有生效槽占用记录", cd.read_slot_claim() == {},
          f"claim={cd.read_slot_claim()}")

    print("\n=== 2b) 只有启动才接管生效槽 ===")
    claimed = cd.claim_slot(pids=[4242])
    st = cd.library_status()
    check("接管后我的档生效", st["applied_id"] == cd.SOTA_ENTRY_ID,
          f"applied={st['applied_name']} status={claimed['status']}")
    check("记下了被顶下去的是谁", claimed["displaced_id"] == FOREIGN_B
          and cd.read_slot_claim().get("previous_applied_id") == FOREIGN_B,
          f"displaced={claimed['displaced_name']!r} claim={cd.read_slot_claim()}")
    again_claim = cd.claim_slot(pids=[4243])
    check("重复接管不会把自己记成前任",
          again_claim["displaced_id"] == FOREIGN_B and again_claim["status"] == "already-owned",
          f"status={again_claim['status']} displaced={again_claim['displaced_id']}")
    deployment = json.loads((root / "claude_desktop_config.json").read_text(encoding="utf-8"))
    check("3P 模式只写入 canonical Claude-3p", deployment.get("deploymentMode") == "3p")
    check("Local/Roaming 两份 1P 配置保持原字节",
          (local_1p.read_bytes(), roaming_1p.read_bytes()) == one_p_before)

    profile = json.loads((root / "configLibrary" / f"{cd.SOTA_ENTRY_ID}.json").read_text(encoding="utf-8"))
    check("档内容符合 Claude Desktop 的键名",
          profile["inferenceProvider"] == "gateway"
          and profile["inferenceGatewayBaseUrl"] == "http://127.0.0.1:17895"
          and profile["inferenceGatewayAuthScheme"] == "bearer"
          and profile["inferenceModels"] == MODELS
          and len(profile["inferenceGatewayApiKey"]) >= 20,
          f"baseUrl={profile['inferenceGatewayBaseUrl']} models={len(profile['inferenceModels'])} key={len(profile['inferenceGatewayApiKey'])}字符")

    print("\n=== 3) 别人的档必须一个字节都没动 ===")
    for entry_id, before in originals.items():
        after = (root / "configLibrary" / f"{entry_id}.json").read_bytes()
        check(f"{entry_id[:8]}… 未被修改", before == after)

    print("\n=== 4) 重复写入应更新同一个档，不新增 ===")
    cd.write_profile(MODELS + [{"name": "jw--extra", "labelOverride": "x"}], "http://127.0.0.1:17895")
    st = cd.library_status()
    again = json.loads((root / "configLibrary" / f"{cd.SOTA_ENTRY_ID}.json").read_text(encoding="utf-8"))
    check("还是 3 个档，模型数已更新",
          len(st["entries"]) == 3 and len(again["inferenceModels"]) == 3)
    check("密钥被保留而不是每次重新生成",
          again["inferenceGatewayApiKey"] == profile["inferenceGatewayApiKey"])

    print("\n=== 5) 交还生效槽给 CC Switch ===")
    released = cd.release_slot()
    st = cd.library_status()
    check("appliedId 已交还，我的档仍留在库里",
          released["status"] == "released" and st["applied_id"] == FOREIGN_B and st["mine_present"],
          f"status={released['status']} applied={st['applied_name']}")
    check("交还后占用记录被清掉", cd.read_slot_claim() == {},
          f"claim={cd.read_slot_claim()}")
    check("没有占用记录时交还是空操作", cd.release_slot()["status"] == "no-claim")

    print("\n=== 5b) 别人已经抢走时不许抢回来 ===")
    cd.claim_slot()
    cd.apply_entry(FOREIGN_A)          # 模拟 cc-switch 在我们之后切了档
    check("手动切档会清掉我们的占用记录", cd.read_slot_claim() == {},
          f"claim={cd.read_slot_claim()}")
    cd._atomic_write_json(cd._slot_claim_path(), {
        "version": 1, "owner": "codex-sota", "entry_id": cd.SOTA_ENTRY_ID,
        "previous_applied_id": FOREIGN_B, "previous_applied_name": "CC Switch",
    })                                  # 手工伪造一条过期的占用记录
    stale = cd.release_slot()
    st = cd.library_status()
    check("过期记录不会把别人选的档顶掉",
          stale["status"] == "not-owner" and st["applied_id"] == FOREIGN_A,
          f"status={stale['status']} applied={st['applied_name']}")

    print("\n=== 5c) 崩溃后的兜底交还 ===")
    cd.apply_entry(FOREIGN_B)
    cd.claim_slot()
    check("Claude 还在跑时不动 appliedId",
          cd.reconcile_slot(claude_running=True)["status"] == "claude-running"
          and cd.library_status()["applied_id"] == cd.SOTA_ENTRY_ID)
    check("Claude 已退出则自动交还",
          cd.reconcile_slot(claude_running=False)["status"] == "released"
          and cd.library_status()["applied_id"] == FOREIGN_B)

    print("\n=== 5d) 切回 CC Switch（手动切档）===")
    back = cd.apply_entry(FOREIGN_B)
    st = cd.library_status()
    check("appliedId 已切回，我的档仍留在库里",
          st["applied_id"] == FOREIGN_B and st["mine_present"],
          f"applied={st['applied_name']} previous={back['previous_applied']}")

    print("\n=== 6) 拒绝不合法的输入 ===")
    for label, fn in (
        ("空模型列表", lambda: cd.write_profile([], "http://127.0.0.1:17895")),
        ("非 http 网关", lambda: cd.write_profile(MODELS, "127.0.0.1:17895")),
        ("不存在的档 id", lambda: cd.apply_entry("nope")),
        ("带路径分隔符的 id", lambda: cd.entry_path("../escape")),
    ):
        try:
            fn()
            check(label + " 被拒绝", False, "居然通过了")
        except (ValueError, RuntimeError) as error:
            check(label + " 被拒绝", True, str(error)[:70])

    print("\n=== 7) 备份确实留下了 ===")
    backups = list((root / "backups").glob("*/_meta.json"))
    check("每次写入都备份了 _meta.json", len(backups) >= 3, f"{len(backups)} 份备份")

    environment.stop()
    shutil.rmtree(temporary, ignore_errors=True)
    print(f"\n合计 {sum(results)}/{len(results)} 项通过；全程只碰临时目录，真实配置库未动")
    raise SystemExit(0 if all(results) else 1)
