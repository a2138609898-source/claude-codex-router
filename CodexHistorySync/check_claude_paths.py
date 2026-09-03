"""Claude 3P root migration check, entirely inside a temporary directory."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_desktop as cd  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        local = base / "Local"
        roaming = base / "Roaming"
        canonical = local / "Claude-3p"
        legacy = roaming / "Claude-3p"
        library = legacy / "configLibrary"
        library.mkdir(parents=True)
        (library / "_meta.json").write_text(
            json.dumps({"appliedId": "theirs", "entries": [{"id": "theirs", "name": "CC Switch"}]}),
            encoding="utf-8",
        )
        (library / "theirs.json").write_text("{}", encoding="utf-8")
        (legacy / "claude_desktop_config.json").write_text(
            json.dumps({"deploymentMode": "1p", "preserve": True}), encoding="utf-8"
        )

        one_p = local / "Claude" / "claude_desktop_config.json"
        one_p.parent.mkdir(parents=True)
        one_p.write_bytes(b'{"deploymentMode":"1p","untouched":true}')
        before = one_p.read_bytes()

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}), \
             mock.patch.object(cd, "CLAUDE_3P_ROOT", canonical), \
             mock.patch.object(cd, "CONFIG_LIBRARY", canonical / "configLibrary"), \
             mock.patch.object(cd, "META_PATH", canonical / "configLibrary" / "_meta.json"), \
             mock.patch.object(cd, "BACKUP_ROOT", base / "backups"), \
             mock.patch.object(cd, "LIBRARY_LOCK_PATH", base / "claude.lock"):
            status = cd.library_status()
            deployment = cd.ensure_deployment_mode()

        config = json.loads((canonical / "claude_desktop_config.json").read_text(encoding="utf-8"))
        checks = [
            status.get("available") is True,
            status.get("applied_name") == "CC Switch",
            status.get("root_migration", {}).get("status") in {"moved", "copied"},
            deployment.get("status") == "ready",
            config == {"deploymentMode": "3p", "preserve": True},
            one_p.read_bytes() == before,
        ]
        labels = [
            "legacy roaming library becomes available at canonical root",
            "foreign applied profile survives migration",
            "migration is reported",
            "canonical deployment mode is ready",
            "other canonical 3P keys are preserved",
            "1P config remains byte-identical",
        ]
        for label, passed in zip(labels, checks):
            print(f"  {'PASS' if passed else 'FAIL'} {label}")
        print(f"\n{sum(checks)}/{len(checks)} checks passed")
        return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
