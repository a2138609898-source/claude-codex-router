"""Refresh fast-tier capability for every enabled gpt-5.6 model and persist it to providers.json."""

import sys

sys.stdout.reconfigure(encoding="utf-8")
import os.path  # noqa: E402 - keeps this script runnable from any checkout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from copy import deepcopy  # noqa: E402

from sota_registry import (  # noqa: E402
    apply_provider,
    load_registry,
    measure_fast_tier,
    probe_fast_tier,
)

MEASURE: set[tuple[str, str]] = set()  # 填 (供应商, 模型) 才会跑测速；默认只探测接受性
VERDICT = {"supported": "接受", "unsupported": "拒绝", "unknown": "无法判定"}

registry = load_registry()
print(f"{'供应商':<16}{'模型':<16}{'接受 tier':<12}{'实测提速':<24}{'写回'}")
print("-" * 82)

for source in registry["providers"]:
    if not source.get("enabled"):
        continue
    provider = deepcopy(source)
    changed = False
    for model in provider["models"]:
        if not model.get("enabled") or "gpt-5.6" not in model["id"]:
            continue
        try:
            probe = probe_fast_tier(provider, model["id"])
            verdict = probe.get("verdict")
        except Exception as error:
            print(f"{provider['id']:<16}{model['id']:<16}探测出错 {type(error).__name__}")
            continue
        status = verdict if verdict in {"supported", "unsupported"} else "unknown"
        model["fast_tier_status"] = status
        effect_text = "-"
        if status == "supported":
            if (provider["id"], model["id"]) in MEASURE:
                result = measure_fast_tier(provider, model["id"], 8)
                effect = result.get("verdict")
                model["fast_tier_effect"] = effect if effect in {"faster", "none"} else "untested"
                if result.get("pairs", 0) >= 4:
                    effect_text = (
                        f"{result['median_delta_percent']:+.1f}% "
                        f"{result['faster_pairs']}/{result['pairs']}对更快 → "
                        f"{'提速' if effect == 'faster' else '无提速'}"
                    )
                else:
                    effect_text = "样本不足"
            else:
                model.setdefault("fast_tier_effect", "untested")
                effect_text = "未测速"
        else:
            model["fast_tier_effect"] = "untested"
        changed = True
        note = "protected 跳过" if provider.get("protected") else "待写回"
        print(f"{provider['id']:<16}{model['id']:<16}{VERDICT[status]:<12}{effect_text:<24}{note}")

    if changed and not provider.get("protected"):
        try:
            apply_provider(provider)
            print(f"{'':<16}└─ {provider['id']} 已写回 providers.json 并重建目录")
        except Exception as error:
            print(f"{'':<16}└─ {provider['id']} 写回失败: {error}")
