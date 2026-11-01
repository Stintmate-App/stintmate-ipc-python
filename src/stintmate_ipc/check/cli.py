"""``stintmate-ipc-check``: the conformance tool, run from the bundled ``stintmate-ipc-check.exe``.

    stintmate-ipc-check server <pipe> --key-file <key>   # check a server (an endpoint)
    stintmate-ipc-check client <pipe> --key-file <key>   # serve a test endpoint, check a client
    stintmate-ipc-check list [--workspace <dir>]         # endpoints in the registry
    stintmate-ipc-check keygen [path]                    # create a key file (if missing)

Exit status 0 when every check passed (warnings allowed), 1 when one failed, 2 on a usage
error. ``stintmate-ipc-check --help`` lists the options. The checks are written in Rust; this
module only starts the executable and forwards its output through ``sys.stdout`` and
``sys.stderr`` (so ``capsys`` sees it) and returns its exit status.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

EXE = Path(__file__).resolve().parent.parent / "_bin" / "stintmate-ipc-check.exe"


def _pump(src, name: str) -> None:
    for chunk in iter(lambda: src.readline(), b""):
        stream = getattr(sys, name)
        try:
            stream.write(chunk.decode("utf-8", "replace"))
            stream.flush()
        except (OSError, ValueError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the conformance tool with ``argv`` (default ``sys.argv[1:]``); returns its exit
    status."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not EXE.is_file():
        print(f"stintmate-ipc-check: {EXE} is missing (the wheel was built without it)",
              file=sys.stderr)
        return 2
    proc = subprocess.Popen([str(EXE), *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pumps = [threading.Thread(target=_pump, args=(proc.stdout, "stdout")),
             threading.Thread(target=_pump, args=(proc.stderr, "stderr"))]
    for t in pumps:
        t.start()
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        code = 130
    for t in pumps:
        t.join()
    return code


def run() -> None:
    """The console entry point: leaves with the tool's exit status."""
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


if __name__ == "__main__":
    run()
