"""Configure a self-contained installation before loading the GUI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def configure_environment() -> Path:
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    core = bundle / "CodexHistorySync"
    if not core.is_dir():
        core = Path(__file__).resolve().parent.parent / "CodexHistorySync"
    os.environ.setdefault("CODEX_SOTA_CORE_ROOT", str(core.resolve()))
    if getattr(sys, "frozen", False):
        runtime = Path(sys.executable).with_name("codex-sota-runtime.exe")
        if not runtime.is_file():
            raise RuntimeError("The bundled runtime is missing. Extract or reinstall the complete Codex SOTA release.")
        os.environ.setdefault("CODEX_SOTA_PYTHON", str(runtime.resolve()))
    if str(core) not in sys.path:
        sys.path.insert(0, str(core))
    return core


def main() -> int:
    core = configure_environment()
    import CodexSotaManager
    if "--self-test" in sys.argv[1:]:
        # Import the complete control chain without creating windows or touching profiles.
        import claude_desktop
        import codex_sota_router
        import sqlite3
        import ssl
        import sota_compaction
        import sota_network
        import sync_codex_histories_three_way
        print(json.dumps({"status": "ok", "core_root": str(core), "python": os.environ.get("CODEX_SOTA_PYTHON"), "version": "0.2.1"}))
        return 0
    return CodexSotaManager.main()


if __name__ == "__main__":
    raise SystemExit(main())
