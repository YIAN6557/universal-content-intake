"""Run one UCI module from Windows Task Scheduler the way launchd runs it on macOS.

Task Scheduler starts a program but cannot send its output to a log file, and
it does not restart a job that exits with an error. This wrapper does both.
It also ties everything it starts to itself, so ending the task in Task
Scheduler (or uninstalling it) stops the Worker and any download it runs:

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
_JOB = None  # kept open for the life of this process; closing it ends every process in it


def _end_children_with_me() -> None:
    """Windows: a job object that kills every process started from here when this process ends."""

    global _JOB
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if job and kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) \
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        _JOB = job


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
    _end_children_with_me()
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
