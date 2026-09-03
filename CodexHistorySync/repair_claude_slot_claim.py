"""One-off repair for the shared Claude 3P slot that codex-sota took and never gave back.

Publishing a profile used to set `appliedId` (claude_desktop.write_profile defaulted to
apply=True), so codex-sota has been holding Claude Desktop's single shared config slot since
2026-09-02 11:28:45 -- which is why launching Claude from cc-switch produced codex-sota's
gateway.  write_profile no longer does that, but the slot is still held and the new
claim/release protocol has no record of who to give it back to.

This seeds that record.  It deliberately does NOT flip `appliedId` while Claude Desktop is
running: the profile is read at startup, so flipping it now would change nothing for the live
session, and the handback belongs at exit.  With the record in place, release happens through
the normal paths -- the post-exit watcher, reconcile-on-startup, or the "交还生效档" button.

Run with --apply to write.  Default is a dry run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import claude_desktop as cd  # noqa: E402

# cc-switch's own entry: the profile the user was launching Claude with before codex-sota took
# the slot, and the last non-codex-sota entry file to be written (2026-09-01 11:35).
CC_SWITCH_ENTRY_ID = "00000000-0000-4000-8000-000000157210"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write the claim record")
    parser.add_argument("--entry-id", default=CC_SWITCH_ENTRY_ID)
    args = parser.parse_args()

    meta = cd.read_meta()
    applied = str(meta.get("appliedId") or "")
    names = {
        str(e.get("id")): str(e.get("name"))
        for e in meta.get("entries", [])
        if isinstance(e, dict)
    }
    existing = cd.read_slot_claim()

    print(f"claim file        : {cd._slot_claim_path()}")
    print(f"appliedId now     : {applied}  ({names.get(applied, '?')})")
    print(f"existing claim    : {json.dumps(existing, ensure_ascii=False) or '{}'}")
    print(f"hand back to      : {args.entry_id}  ({names.get(args.entry_id, '?')})")

    if applied != cd.SOTA_ENTRY_ID:
        print("\nNOTHING TO DO: codex-sota is not the applied profile, so it is not holding "
              "the slot.")
        return 0
    if args.entry_id not in names:
        print(f"\nREFUSING: {args.entry_id} is not in the config library.")
        return 1
    if str(existing.get("previous_applied_id") or "") == args.entry_id:
        print("\nNOTHING TO DO: the claim already records that entry as the previous owner.")
        return 0
    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write the claim record.")
        print("appliedId is NOT touched either way.")
        return 0

    payload = {
        "version": 1,
        "owner": "codex-sota",
        "entry_id": cd.SOTA_ENTRY_ID,
        "previous_applied_id": args.entry_id,
        "previous_applied_name": names.get(args.entry_id, args.entry_id),
        "claimed_at": cd._utc_stamp(),
        "claimed_pids": [],
        "note": "seeded by repair_claude_slot_claim.py; the original claim predates the "
                "claim/release protocol",
    }
    cd._atomic_write_json(cd._slot_claim_path(), payload)
    written = cd.read_slot_claim()
    print("\nWROTE claim record:")
    print("  " + json.dumps(written, ensure_ascii=False, indent=2).replace("\n", "\n  "))
    print(f"\nappliedId is still {cd.read_meta().get('appliedId')} -- unchanged, as intended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
