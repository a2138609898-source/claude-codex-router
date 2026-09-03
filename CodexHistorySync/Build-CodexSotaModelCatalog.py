#!/usr/bin/env python3
"""Build the Codex SOTA model catalog from providers.json."""

from __future__ import annotations

import json

from sota_registry import build_model_catalog


def main() -> int:
    print(json.dumps(build_model_catalog(), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
