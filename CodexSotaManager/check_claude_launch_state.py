"""Read-only diagnosis of the Claude Desktop launch/config situation.

Nothing here launches, closes, or writes anything.  It answers the three questions the
codex-sota / cc-switch conflict turns on:

  1. which install would codex-sota start,
  2. whose profile is currently applied in the shared 3P slot,
  3. is Claude Desktop running right now.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import CodexSotaManager as manager  # noqa: E402
from claude_desktop import (  # noqa: E402
    CLAUDE_3P_ROOT,
    SOTA_ENTRY_ID,
    library_status,
    read_meta,
)


def main() -> int:
    print("== 3P tree ==")
    print(f"  root            : {CLAUDE_3P_ROOT}")
    print(f"  root exists     : {CLAUDE_3P_ROOT.is_dir()}")

    print("== launch target codex-sota would use ==")
    try:
        registered = sorted(manager.registered_claude_app_ids())
    except Exception as error:  # pragma: no cover - diagnostic only
        registered = [f"<query failed: {error}>"]
    print(f"  registered AUMIDs: {registered}")
    try:
        target = manager.resolve_claude_launch_target()
    except Exception as error:  # pragma: no cover - diagnostic only
        target = {"kind": "<error>", "value": str(error), "label": "<error>"}
    print(f"  resolved target  : {target}")
    if target:
        try:
            command, cwd = manager.claude_launch_command(target)
            print(f"  command          : {command}")
            print(f"  cwd              : {cwd}")
        except Exception as error:
            print(f"  command          : <error: {error}>")

    print("== applied profile in the shared slot ==")
    try:
        meta = read_meta()
    except Exception as error:
        print(f"  _meta.json       : <error: {error}>")
        meta = {}
    applied = str(meta.get("appliedId") or "")
    names = {
        str(entry.get("id")): str(entry.get("name"))
        for entry in meta.get("entries", [])
        if isinstance(entry, dict)
    }
    print(f"  appliedId        : {applied or '<none>'}  ({names.get(applied, '?')})")
    print(f"  is codex-sota    : {applied == SOTA_ENTRY_ID}")
    for entry_id, name in names.items():
        marker = " <-- applied" if entry_id == applied else ""
        print(f"    {entry_id}  {name}{marker}")

    print("== library_status() ==")
    try:
        status = library_status()
        print("  " + json.dumps(status, ensure_ascii=False, indent=2).replace("\n", "\n  "))
    except Exception as error:
        print(f"  <error: {error}>")

    print("== process state ==")
    try:
        print(f"  claude window    : {manager.claude_window_present()}")
    except Exception as error:
        print(f"  claude window    : <error: {error}>")
    try:
        print(f"  claude pids      : {sorted(manager.image_pids('claude.exe'))}")
    except Exception as error:
        print(f"  claude pids      : <error: {error}>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
