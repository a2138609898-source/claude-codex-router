"""The messages-side model prefix: shape, derivation, migration, and what Claude does with it.

Claude Desktop only offers a thinking-effort control for a model whose id, after its own
canonicalization, is in a table compiled into the app.  That canonicalizer strips a leading
`<vendor>.anthropic.` but not `vendor--`, so the prefix shape decides whether the control shows
up at all.  Everything here is in-process against synthetic registries: no ports, no vendors,
and neither live providers.json is read.
"""

import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
import os.path  # noqa: E402 - keeps this script runnable from any checkout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_desktop as cd  # noqa: E402
import sota_registry as R  # noqa: E402

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


def provider(pid: str, prefix: str | None, protocols: list[str], models: list[str],
             **extra: object) -> dict:
    entry = {
        "id": pid,
        "name": pid.replace("_", " ").title(),
        "base_url": "https://gateway.example.com",
        "protocols": protocols,
        "workspace": R.CLAUDE.name,
        "models": [{"id": m, "enabled": True} for m in models],
    }
    if prefix is not None:
        entry["prefix"] = prefix
    entry.update(extra)
    return entry


def registry(*providers: dict) -> dict:
    return {"version": R.REGISTRY_VERSION, "providers": list(providers)}


def validated(*providers: dict) -> dict:
    return R.validate_registry(json.loads(json.dumps(registry(*providers))),
                               allow_missing_secrets=True)


def home(prefix: str = "", **extra: object) -> dict:
    """The workspace default: every registry needs exactly one, and its prefix must stay empty."""
    return provider("juno", prefix, ["messages"], ["claude-opus-5"],
                    is_default=True, **extra)


def rejects(label: str, *providers: dict) -> None:
    try:
        validated(*providers)
    except ValueError as exc:
        check(label, True, str(exc)[:70])
    else:
        check(label, False, "居然通过了")


print("=== 1) 前缀形状：两种都自带分隔符，别的写法一律不算 ===")
for good in ("tango--", "x--", "alfa_relay--", "tango.anthropic.",
             "alfa-relay.anthropic.", "a.anthropic."):
    check(f"接受 {good!r}", bool(R.MODEL_PREFIX_PATTERN.fullmatch(good)))
for bad in ("anthropic.", ".anthropic.", "alfa_relay.anthropic.", "a.b.anthropic.",
            "tango.anthropic", "tango.anthropic..", "tango-", "tango",
            "--", "X--", "-x--", "tango.openai."):
    check(f"拒绝 {bad!r}", not R.MODEL_PREFIX_PATTERN.fullmatch(bad))

print("\n=== 2) 派生：只说 messages 的才给点号形式 ===")
for pid, protos, want in [
    ("tango", ["messages"], "tango.anthropic."),
    ("alfa_relay", ["messages"], "alfa-relay.anthropic."),
    ("mike", ["responses"], "mike--"),
    ("delta", ["responses", "messages"], "delta--"),
    ("nobody", [], "nobody--"),
    ("nobody", None, "nobody--"),
]:
    got = R.derive_model_prefix(pid, protos)
    check(f"{pid} {protos} -> {want!r}", got == want, f"实际 {got!r}")
    check(f"派生结果自己也合法 {got!r}", bool(R.MODEL_PREFIX_PATTERN.fullmatch(got)))

print("\n=== 3) 校验：点号前缀是合法输入，老的不变式一条没松 ===")
reg = validated(home(), provider("tango", None, ["messages"], ["claude-opus-5"]))
by_id = {p["id"]: p for p in reg["providers"]}
check("没填 prefix 的 messages 供应商被补成点号形式",
      by_id["tango"]["prefix"] == "tango.anthropic.", by_id["tango"]["prefix"])
check("默认那家的 prefix 还是空的", by_id["juno"]["prefix"] == "")
check("点号 slug 正常进发布列表",
      set(R.selectable_slugs(reg)) == {"claude-opus-5", "tango.anthropic.claude-opus-5"},
      str(R.selectable_slugs(reg)))
rejects("默认那家不许带前缀", home(prefix="juno.anthropic."),
        provider("tango", None, ["messages"], ["claude-opus-5"]))
rejects("两家不许撞同一个点号前缀", home(),
        provider("tango", "same.anthropic.", ["messages"], ["claude-opus-5"]),
        provider("delta", "same.anthropic.", ["messages"], ["claude-opus-5"]))
rejects("拼出来的 slug 撞车照样拦", home(),
        provider("aa", "shared--", ["messages"], ["x--y"]),
        provider("bb", "shared--x--", ["messages"], ["y"]))
# 唯一一种能跨两种前缀形状撞车的情形：默认那家没有前缀，模型名本身就长得像别人的 slug。
rejects("默认那家的裸模型名撞上别人的点号 slug 也拦",
        provider("juno", "", ["messages"], ["tango.anthropic.claude-opus-5"],
                 is_default=True),
        provider("tango", None, ["messages"], ["claude-opus-5"]))
print("\n=== 4) 迁移：只动自己派生过的那几家，重跑无害 ===")
mixed = validated(
    home(),
    provider("tango", "tango--", ["messages"], ["claude-opus-5"]),
    provider("sierra", "sierra--", ["messages"], ["claude-opus-4-6"]),
    provider("delta", "delta--", ["responses", "messages"], ["claude-opus-5"]),
    provider("mike", "mike--", ["responses"], ["gpt-5.6-sol"]),
    provider("kilo", "kk--", ["messages"], ["claude-opus-5"]),
)
moved = {pid: (old, new) for pid, old, new in R.upgrade_messages_prefixes(mixed)}
check("tango 改成点号", moved.get("tango") == ("tango--", "tango.anthropic."),
      str(moved.get("tango")))
check("sierra 改成点号", moved.get("sierra") == ("sierra--", "sierra.anthropic."),
      str(moved.get("sierra")))
check("两端都说的 delta 不动", "delta" not in moved)
check("纯 responses 的 mike 不动", "mike" not in moved)
check("手写的 kk-- 不动：那是人挑的名字", "kilo" not in moved)
check("默认那家不动，它得留着空前缀", "juno" not in moved)
check("一共只改了 2 家", len(moved) == 2, str(sorted(moved)))
check("改完仍然过校验",
      bool(R.validate_registry(json.loads(json.dumps(mixed)), allow_missing_secrets=True)))
check("再跑一遍就没得改了（幂等）", R.upgrade_messages_prefixes(mixed) == [])
print("\n=== 5) 归一化：照抄 Claude Desktop 1.40609.0.0 的剥法 ===")
for slug, want in [
    ("tango.anthropic.claude-opus-5", "claude-opus-5"),
    ("alfa-relay.anthropic.claude-opus-4-8", "claude-opus-4-8"),
    ("anthropic.claude-sonnet-4-5-v2:0", "claude-sonnet-4-5"),
    ("arn:aws:bedrock:us-east-1/anthropic.claude-opus-5[1m]", "claude-opus-5"),
    ("claude-sonnet-5@20260101", "claude-sonnet-5"),
    ("claude-opus-4-1-20250805", "claude-opus-4-1"),
    ("Tango.Anthropic.Claude-Opus-5", "claude-opus-5"),
    ("tango--claude-opus-5", "tango--claude-opus-5"),
    ("gpt-5.6-sol", "gpt-5.6-sol"),
]:
    got = cd.canonical_model_id(slug)
    check(f"{slug}  ->  {want}", got == want, f"实际 {got}")
check("vendor-- 没人给它剥，这就是当初没有控件的全部原因",
      cd.thinking_effort_levels("tango--claude-opus-5") is None)
check("点号形式落到表里了",
      cd.thinking_effort_levels("tango.anthropic.claude-opus-5")
      == ("low", "medium", "high", "xhigh", "max"))
check("认得但只有开关的那种给空元组",
      cd.thinking_effort_levels("anthropic.claude-sonnet-4-5-v2:0") == ())
check("表里没有的名字还是 None",
      cd.thinking_effort_levels("tango.anthropic.claude-opus-5-thinking") is None)
check("fable 家族走的是前缀匹配",
      cd.thinking_effort_levels("x.anthropic.claude-fable-5")
      == ("low", "medium", "high", "xhigh", "max"))
check("三种结论各有一句人话",
      [cd.thinking_summary(s) for s in ("tango.anthropic.claude-opus-5",
                                        "anthropic.claude-sonnet-4-5",
                                        "tango--claude-opus-5")]
      == ["思考档 low/medium/high/xhigh/max", "仅扩展思考开关", "无思考控件"],
      str([cd.thinking_summary(s) for s in ("tango.anthropic.claude-opus-5",
                                           "anthropic.claude-sonnet-4-5",
                                           "tango--claude-opus-5")]))
print("\n=== 6) 收益：迁移之后这些 slug 在 Claude 里能拿到控件 ===")
legacy = validated(
    home(),
    provider("tango", "tango--", ["messages"], ["claude-opus-5"]),
    provider("sierra", "sierra--", ["messages"], ["claude-opus-4-6"]),
    provider("kilo", "kilo--", ["messages"], ["claude-opus-5-thinking"]),
)
blind_before = sorted(s for s in R.selectable_slugs(legacy)
                      if cd.thinking_effort_levels(s) is None)
check("改之前：三家的 slug 一个控件都没有",
      blind_before == ["kilo--claude-opus-5-thinking", "sierra--claude-opus-4-6",
                       "tango--claude-opus-5"], str(blind_before))
R.upgrade_messages_prefixes(legacy)
blind_after = sorted(s for s in R.selectable_slugs(legacy)
                     if cd.thinking_effort_levels(s) is None)
check("改之后只剩 -thinking 那个：档位写进了厂商自己的模型名，换前缀救不了它",
      blind_after == ["kilo.anthropic.claude-opus-5-thinking"], str(blind_after))
check("sierra 拿到的是它那代的四档",
      cd.thinking_effort_levels("sierra.anthropic.claude-opus-4-6")
      == ("low", "medium", "high", "max"))
check("默认那家本来就有控件：空前缀 + 原厂模型名",
      cd.thinking_summary("claude-opus-5") == "思考档 low/medium/high/xhigh/max")

print("\n=== 7) 派发：点号 slug 照样解析回上游裸模型名 ===")
chain = R.failover_chain(legacy, "tango.anthropic.claude-opus-5", protocol="messages")
check("解析到 tango，发给上游的 id 不带前缀",
      list(chain)[:1] == [("tango", "claude-opus-5")], str(chain))
stale = R.failover_chain(legacy, "tango--claude-opus-5", protocol="messages")
check("老写法不再指向 tango（迁移完就该这样）",
      all(pid != "tango" for pid, _ in stale), str(stale))
wrong_wire = R.failover_chain(legacy, "tango.anthropic.claude-opus-5", protocol="responses")
check("要 responses 时不给 tango：它只会 messages",
      all(pid != "tango" for pid, _ in wrong_wire), str(wrong_wire))

total, passed = len(results), sum(results)
print(f"\n合计 {passed}/{total} 项通过；两个 providers.json 全程没读，也没起端口。")
raise SystemExit(0 if passed == total else 1)
