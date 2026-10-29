"""Opt-in PyInstaller hook: verify the frozen runtime before any manager code runs.

Normal launches do not import application modules or alter environment here. The
reserved self-test invocation exits in this hook, without opening a GUI, loading a
profile, starting a service, or connecting to a network.
"""
from __future__ import annotations

import sys


PROBE_FLAG = "--codex-sota-artifact-self-test"


def runtime_checks() -> dict[str, str]:
    import ctypes
    import os
    from pathlib import Path
    import socket
    import sqlite3
    import ssl

    bundle = Path(str(getattr(sys, "_MEIPASS", Path(__file__).parent)))
    # Custom runtime hooks run before PyInstaller's _tkinter hook. Point Tcl at
    # this bundle explicitly, never at another installed application's Tcl data.
    os.environ["TCL_LIBRARY"] = str(bundle / "_tcl_data")
    os.environ["TK_LIBRARY"] = str(bundle / "_tk_data")
    import tkinter

    if sys.version_info < (3, 11) or ctypes.sizeof(ctypes.c_void_p) not in {4, 8}:
        raise RuntimeError("Unsupported Python runtime")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if not context.check_hostname:
        raise RuntimeError("TLS initialization failed")
    with sqlite3.connect(":memory:") as connection:
        if connection.execute("select 40 + 2").fetchone() != (42,):
            raise RuntimeError("SQLite initialization failed")
    interpreter = tkinter.Tcl()  # Tcl only: no Tk window is created.
    if int(interpreter.eval("expr {40 + 2}")) != 42:
        raise RuntimeError("Tcl initialization failed")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
        pass  # Load the extension without binding, connecting, or sending data.
    return {name: "passed" for name in ("python", "ctypes", "ssl", "sqlite", "tcl", "socket")}


def main(arguments: list[str]) -> int:
    import hashlib
    import json
    from pathlib import Path
    import re

    if len(arguments) != 3 or arguments[0] != PROBE_FLAG or not re.fullmatch(r"[0-9a-f]{32}", arguments[2]):
        return 2
    destination = Path(arguments[1])
    if not destination.is_absolute() or not destination.parent.is_dir() or destination.exists():
        return 2
    report = {
        "probe_protocol": 1, "status": "failed", "nonce": arguments[2],
        "network_used": False, "profiles_loaded": False,
    }
    try:
        report["checks"] = runtime_checks()
        report["executable_sha256"] = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
        report["python_version"] = sys.version.split()[0]
        report["status"] = "ready"
    except Exception as error:
        report["error"] = type(error).__name__
    # Refuse to reuse or overwrite a result from any previous invocation.
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False)
        handle.write("\n")
    return 0 if report["status"] == "ready" else 1


if PROBE_FLAG in sys.argv[1:]:
    raise SystemExit(main(sys.argv[1:]))
