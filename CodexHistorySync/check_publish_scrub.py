#!/usr/bin/env python3
"""发布擦洗自检：扫描版本库里是否残留真实供应商身份、个人标识或密钥。

对齐 README 的承诺——「测试夹具里的供应商 id 和域名都是中性占位」「仓库不包含任何
个人账号、密钥、聊天记录或运行状态」。本脚本【只读】扫描 git 跟踪的文本文件，按
CodexHistorySync/check_*.py 的约定打中文分节、末尾输出 `合计 N/M 项通过`，全过退 0，
有残留退 1。它不改任何文件，也不联网。

真实供应商标识、域名、个人标识全部用字符串片段【拼接】得到，因此本文件自身不含任何
一个字面量：既能干净地通过自己的扫描，也不会成为误报来源（扫描时另外再排除本脚本）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 控制台可能是 GBK；和其它 check_*.py 一样把输出固定成 UTF-8，中文分节和标记才不会崩。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SELF = Path(__file__).resolve()
REPO = SELF.parents[1]


def _j(*parts: str) -> str:
    return "".join(parts)


# 真实商用中转的可辨识片段（小写）。凑齐才是身份，散着放本文件就干净。
VENDOR_SUBSTRINGS = [
    _j("agent", "router"),
    _j("ling", "zhan"),
    _j("ai", "shenji"),
    _j("cic", "adas"),
    _j("mai", "xun"),
    _j("miao", "miao"),
    _j("just", "woker"),
]
# 真实中转域名，同样片段拼接。true-sota.com 是上游域名，和本机路由器 provider
# 名 "true_sota"（127.0.0.1）无关，所以只拦带 .com 的域名形式。
VENDOR_DOMAINS = [
    _j(VENDOR_SUBSTRINGS[0], ".org"),
    _j(VENDOR_SUBSTRINGS[1], "ai", ".top"),
    _j(VENDOR_SUBSTRINGS[2], ".top"),
    _j(VENDOR_SUBSTRINGS[3], ".top"),
    _j(VENDOR_SUBSTRINGS[4], ".icu"),
    _j(VENDOR_SUBSTRINGS[5], "code", ".com"),
    _j(VENDOR_SUBSTRINGS[6], ".icu"),
    _j("true", "-sota", ".com"),
    _j("free", "api", ".site"),
]
# 个人标识（Windows 用户名 / 家目录片段）。
PII_TOKENS = [_j("861", "58")]
# 疑似密钥 / 私钥。字面 "sk-" 只是模式的一部分，不会匹配到本文件自身。
SECRET_PATTERNS = [
    re.compile(_j("sk", "-ant-") + r"[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b" + _j("sk", "-") + r"[A-Za-z0-9]{20,}\b"),
    re.compile(_j("-----BEGIN ") + r"[A-Z ]*PRIVATE KEY-----"),
]
BINARY_EXT = {
    ".png", ".ico", ".jpg", ".jpeg", ".gif", ".pdf", ".zip",
    ".exe", ".dll", ".pyc", ".ttf", ".woff", ".woff2",
}


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout
    files = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        path = REPO / rel
        if path.resolve() == SELF or path.suffix.lower() in BINARY_EXT:
            continue
        files.append(path)
    return files


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def find_substrings(files: list[Path], needles: list[str]) -> list[tuple]:
    low = [n.lower() for n in needles]
    hits = []
    for path in files:
        for lineno, line in enumerate(_lines(path), 1):
            lowered = line.lower()
            for needle in low:
                if needle in lowered:
                    hits.append((path, lineno, needle, line.strip()[:100]))
    return hits


def find_regexes(files: list[Path], patterns: list[re.Pattern]) -> list[tuple]:
    hits = []
    for path in files:
        for lineno, line in enumerate(_lines(path), 1):
            for pattern in patterns:
                if pattern.search(line):
                    hits.append((path, lineno, "secret", line.strip()[:80]))
    return hits


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def section(title: str, hits: list[tuple]) -> bool:
    print(f"\n=== {title} ===")
    if not hits:
        print("  OK：无残留")
        return True
    seen = set()
    for path, lineno, token, snippet in hits:
        key = (_rel(path), lineno, token)
        if key in seen:
            continue
        seen.add(key)
        print(f"  [X] {_rel(path)}:{lineno}  [{token}]  {snippet}")
    print(f"  -- 命中 {len(seen)} 处，应改为中性占位（example.com / .invalid / vendor-a 等）")
    return False


def gitignore_present() -> bool:
    needed = ["providers.json", "auth.json", "*.dpapi", "config.toml", "*.sqlite", "secrets/"]
    ignore = REPO / ".gitignore"
    text = ignore.read_text(encoding="utf-8", errors="replace") if ignore.exists() else ""
    missing = [rule for rule in needed if rule not in text]
    print("\n=== .gitignore 第二道防线仍在 ===")
    if missing:
        for rule in missing:
            print(f"  [X] 缺少排除项：{rule}")
        return False
    print("  OK：providers.json / auth.json / *.dpapi / config.toml / *.sqlite / secrets/ 均已忽略")
    return True


def main() -> int:
    files = tracked_files()
    print(f"扫描 {len(files)} 个 git 跟踪文本文件（已排除本脚本与二进制）")
    results = [
        section("真实供应商标识残留", find_substrings(files, VENDOR_SUBSTRINGS)),
        section("真实供应商域名残留", find_substrings(files, VENDOR_DOMAINS)),
        section("个人标识 / 用户名残留", find_substrings(files, PII_TOKENS)),
        section("疑似密钥 / 私钥残留", find_regexes(files, SECRET_PATTERNS)),
        gitignore_present(),
    ]
    passed = sum(1 for ok in results if ok)
    total = len(results)
    print(f"\n合计 {passed}/{total} 项通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
