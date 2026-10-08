"""Run one UCI module from Windows Task Scheduler the way launchd runs it on macOS.

Task Scheduler starts a program but cannot send its output to a log file, and
it does not restart a job that exits with an error. This wrapper does both:

  pythonw -m src.setup.scheduled_run --stdout LOG --stderr LOG [--keep-alive SECONDS] -- MODULE [ARGS…]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scheduled_run")
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    parser.add_argument("--keep-alive", type=float, metavar="SECONDS",
                        help="start the module again this many seconds after it exits with an error")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing module")
    args.stdout.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    while True:
        with args.stdout.open("a", encoding="utf-8") as out, args.stderr.open("a", encoding="utf-8") as err:
            code = subprocess.run([sys.executable, "-m", *command], cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
                                  stdout=out, stderr=err, creationflags=flags, check=False).returncode
        if args.keep_alive is None or code == 0:
            return code
        time.sleep(args.keep_alive)


if __name__ == "__main__":
    raise SystemExit(main())
