#!/usr/bin/env python3
"""Build the Codex SOTA model catalog from providers.json."""

from __future__ import annotations

import json

from sota_registry import build_model_catalog, default_workspace, load_registry, registry_write_lock


def main() -> int:
    workspace = default_workspace()
    # The external launcher must serialize its read/build/write with manager transactions.
    # Internal callers already own this lock, so build_model_catalog itself stays lock-free.
    with registry_write_lock(workspace=workspace):
        result = build_model_catalog(
            load_registry(workspace.registry_path),
            workspace.source_catalog_path,
            workspace.catalog_path,
        )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
