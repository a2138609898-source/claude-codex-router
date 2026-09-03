"""Router-side checks for the forced fast switch, all in-process against a temp registry.

No ports and no network: RouterState is driven directly, so the live router on 17895 is never
touched. The (provider, model) pairs come from the live registry, because the fixture used to
name a vendor by hand -- and when that vendor was deleted, sections 1-3 quietly stopped asserting
anything while still printing an expectation that could no longer come true. They also only
printed; nothing here could fail the run, so the exit code was always 0.
"""

import atexit
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import os.path  # noqa: E402 - keeps this script runnable from any checkout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import codex_sota_router as R  # noqa: E402
from sota_registry import AUTH_PATH, REGISTRY_PATH, published_slug  # noqa: E402

work = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, work, ignore_errors=True)
reg_path = work / "providers.json"
shutil.copy(REGISTRY_PATH, reg_path)
results: list[bool] = []


def registry_triples() -> list[tuple[str, str, str]]:
    """One (provider id, model id, slug) per prefixed provider in the live registry.

    The slug is the model's published slug -- normally prefix + model id, and not
    id + "--" + model id, because a prefix is not always the id (xlinks_gateway is not).
    A model carrying `publish_as` answers under that instead, which is also what the router
    puts in forced_fast, so asking for prefix + id would force a slug nothing dispatches on.
    Such a model also shadows the sibling whose plain slug it borrowed: enabling that sibling
    would make two models claim one slug and the registry would refuse to validate, so this
    picks a target that is safe for the fixture to switch on.
    """
    data = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8-sig"))
    triples = []
    for provider in data["providers"]:
        prefix = str(provider.get("prefix") or "")
        models = provider.get("models") or []
        if not prefix or not models:
            continue
        overridden = {
            str(model.get("publish_as") or "") for model in models if model.get("publish_as")
        }
        chosen = next(
            (model for model in models if prefix + str(model["id"]) not in overridden),
            None,
        )
        if chosen is None:
            continue
        triples.append(
            (provider["id"], chosen["id"], published_slug(provider, chosen))
        )
    if len(triples) < 2:
        raise SystemExit("跳过：注册表里凑不出两家带前缀的供应商，这个检查无从下手")
    return triples


def set_forced(path: Path, pairs: set[tuple[str, str]]) -> None:
    """Force exactly `pairs` and make sure those models are enabled.

    The router ignores the switch on a disabled model, so the fixture enables its own
    targets rather than inheriting whatever the live config happens to have selected.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    for provider in data["providers"]:
        for model in provider["models"]:
            wanted = (provider["id"], model["id"]) in pairs
            model["fast_tier_forced"] = wanted
            if wanted:
                provider["enabled"] = True
                model["enabled"] = True
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check(label: str, got, expect) -> None:
    ok = set(got) == set(expect)
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'} {label}："
          f"实际 {sorted(got) or '空'}（期望 {sorted(expect) or '空'}）")


TRIPLES = registry_triples()
(P1, M1, S1), (P2, M2, S2) = TRIPLES[0], TRIPLES[1]

print("=== 1) forced_fast_models 读取 ===")
set_forced(reg_path, {(P1, M1)})
state = R.RouterState(reg_path, AUTH_PATH, work / "log.jsonl")
check("只开一个", state.forced_fast_models(), {S1})

print("=== 2) providers.json 改动后免重启热更新 ===")
time.sleep(0.05)
set_forced(reg_path, {(P1, M1), (P2, M2)})
check("再开一个立刻生效", state.forced_fast_models(), {S1, S2})

print("=== 3) 关掉开关也要跟着变 ===")
time.sleep(0.05)
set_forced(reg_path, set())
check("全部关掉", state.forced_fast_models(), set())

print("=== 4) 文件损坏时保留上一次的集合，不能把路由器带崩 ===")
time.sleep(0.05)
set_forced(reg_path, {(P1, M1)})
check("损坏前", state.forced_fast_models(), {S1})
time.sleep(0.05)
reg_path.write_text("{ this is not json", encoding="utf-8")
check("损坏后保持不变", state.forced_fast_models(), {S1})
time.sleep(0.05)
reg_path.unlink()
check("文件消失后保持不变", state.forced_fast_models(), {S1})

print("=== 5) 健康窗口：连续失败的上游会被标成待避开 ===")
# unhealthy_vendors() 同时包含两类：已保存的测试结论判定全挂的家（重启也记得），
# 以及本次运行里连续失败的家。所以这里断言的是「运行时新增的那部分」，
# 而不是集合整体为空 —— 否则真实配置里本来就挂掉的几家会让判据永远失败。
shutil.copy(REGISTRY_PATH, reg_path)
state = R.RouterState(reg_path, AUTH_PATH, work / "log2.jsonl")
seeded = set(state.unhealthy_vendors())
print(f"  已保存结论判定全挂的家（开局就避开）: {sorted(seeded)}")
fresh = [pid for pid, _m, _s in TRIPLES if pid not in seeded]


def step(label, expect_extra) -> None:
    extra = set(state.unhealthy_vendors()) - seeded
    results.append(extra == expect_extra)
    print(f"  {'PASS' if extra == expect_extra else 'FAIL'} {label}："
          f"运行时新增 {sorted(extra) or '无'}（期望 {sorted(expect_extra) or '无'}）")


if len(fresh) < 2:
    # Asserting on a vendor that is already avoided would fail for a reason that has nothing
    # to do with the health window.
    print("  跳过：已保存结论已经把大多数家判成全挂，凑不出两个干净的家")
else:
    V1, V2 = fresh[0], fresh[1]
    step("刚启动", set())
    for status in (500, 500):
        state.record(V1, "POST", "/responses", status, 0.1)
    step("连续 2 次失败还不够阈值", set())
    state.record(V1, "POST", "/responses", 503, 0.1)
    step("连续 3 次失败被避开", {V1})
    state.record(V1, "POST", "/responses", 200, 0.1)
    step("之后成功一次立刻恢复", set())
    for status in (403,) * 6:
        state.record(V2, "POST", "/responses", status, 0.1)
    step("连续 6 次失败（窗口只留 5 条）仍被避开", {V2})

print(f"\n合计 {sum(results)}/{len(results)} 项通过；全程在进程内，线上 17895 未动")
raise SystemExit(0 if all(results) else 1)
