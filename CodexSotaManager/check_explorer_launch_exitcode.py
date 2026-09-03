"""Measure what explorer.exe actually reports when it activates an AppsFolder target.

`_launch_claude` treats a non-zero exit code from `explorer.exe shell:AppsFolder\\<aumid>` as a
failed launch.  explorer is a shell dispatcher, not a launcher: it hands the request to the shell
and exits on its own schedule, so its exit code is not a reliable success signal.

Claude Desktop cannot be used as the probe here because it is hosting the live session.  The
Calculator package is used instead: same code path, no side effects worth worrying about, and it
is closed again immediately.
"""

from __future__ import annotations

import subprocess
import sys
import time

PROBE_AUMID = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
PROBE_IMAGE = "CalculatorApp.exe"
BOGUS_AUMID = "Codex.Sota.NoSuchApp_0000000000000!App"


def running(image: str) -> bool:
    completed = subprocess.run(
        ["tasklist.exe", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return image.lower() in (completed.stdout or "").lower()


def activate(aumid: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["explorer.exe", f"shell:AppsFolder\\{aumid}"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )


def main() -> int:
    if running(PROBE_IMAGE):
        print(f"SKIP  {PROBE_IMAGE} is already running; refusing to disturb it")
        return 2

    print(f"probe   : {PROBE_AUMID}")
    result = activate(PROBE_AUMID)
    print(f"exitcode: {result.returncode}")
    print(f"stdout  : {(result.stdout or '').strip()!r}")
    print(f"stderr  : {(result.stderr or '').strip()!r}")

    appeared = False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if running(PROBE_IMAGE):
            appeared = True
            break
        time.sleep(0.4)
    print(f"launched: {appeared}")

    if appeared:
        subprocess.run(
            ["taskkill.exe", "/IM", PROBE_IMAGE, "/F"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        print("cleanup : probe app closed")

    bogus = activate(BOGUS_AUMID)
    print(f"bogus   : exitcode={bogus.returncode} stderr={(bogus.stderr or '').strip()!r}")

    print()
    if appeared and result.returncode != 0:
        print("CONFIRMED: a successful activation reports a non-zero exit code.")
        print("           Treating exitcode != 0 as failure rejects working launches.")
        return 0
    if appeared and result.returncode == 0:
        print("Successful activation reported 0 here, but the exit code still cannot")
        print("distinguish success from failure: the bogus id reports "
              f"{bogus.returncode} as well.")
        return 0
    print("INCONCLUSIVE: the probe app never appeared.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
