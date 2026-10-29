"""Small Python-compatible launcher sharing the manager's frozen standard library.

PowerShell launchers and post-exit watchers use the same bundled runtime, so an
installed release never depends on a separately installed CPython interpreter.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys


def configure_environment() -> Path:
    # Helpers are application resources. Running them must not mutate the
    # installed bundle with interpreter-version-specific __pycache__ files.
    sys.dont_write_bytecode = True
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    core = bundle / "CodexHistorySync"
    if not core.is_dir():
        core = Path(__file__).resolve().parent.parent / "CodexHistorySync"
    os.environ.setdefault("CODEX_SOTA_CORE_ROOT", str(core.resolve()))
    if getattr(sys, "frozen", False):
        os.environ.setdefault("CODEX_SOTA_PYTHON", str(Path(sys.executable).resolve()))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if str(core) not in sys.path:
        sys.path.insert(0, str(core))
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    return core


def main(arguments: list[str] | None = None) -> int:
    configure_environment()
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    while arguments and arguments[0] in {"-I", "-S", "-B", "-u", "-E", "-s"}:
        # The frozen application already has an isolated, read-only import set.
        arguments.pop(0)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print("Codex SOTA bundled Python runtime: [-I -S -B -u] <script.py> [args] | -c code | -m module | --version")
        return 0
    option = arguments.pop(0)
    if option in {"-V", "--version"}:
        print("Python " + sys.version.split()[0] + " (Codex SOTA bundled runtime)")
        return 0
    if option in {"-c", "-m"}:
        if not arguments:
            print(f"{option} requires an argument", file=sys.stderr)
            return 2
        target = arguments.pop(0)
        if option == "-c":
            sys.argv = ["-c", *arguments]
            exec(compile(target, "<string>", "exec"), {"__name__": "__main__", "__package__": None})
        else:
            sys.argv = [target, *arguments]
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        return 0
    script = Path(option).resolve()
    if script.suffix.lower() != ".py" or not script.is_file():
        print("A Python helper script was not found: " + str(script), file=sys.stderr)
        return 2
    sys.argv = [str(script), *arguments]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
