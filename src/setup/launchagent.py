"""Install the background Worker and the weekly yt-dlp updater as per-user LaunchAgents."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.queue.status_cli import WORKER_LABEL
from src.setup.environment import PROJECT_ROOT

UPDATE_LABEL = "local.universal-content-intake.ytdlp-update"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "Universal Content Intake"
SEARCH_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def plist_path(label: str) -> Path:
    return AGENTS_DIR / f"{label}.plist"


def definitions(python: str = sys.executable) -> dict[str, dict[str, Any]]:
    common = {
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {"PATH": SEARCH_PATH, "PYTHONUNBUFFERED": "1"},
    }
    return {
        WORKER_LABEL: {
            "Label": WORKER_LABEL,
            "ProgramArguments": [python, "-m", "src.queue.worker_cli"],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "StandardOutPath": str(LOG_DIR / "worker.stdout.log"),
            "StandardErrorPath": str(LOG_DIR / "worker.stderr.log"),
            **common,
        },
        UPDATE_LABEL: {
            "Label": UPDATE_LABEL,
            "ProgramArguments": [python, "-m", "src.queue.ytdlp_update"],
            "RunAtLoad": False,
            "StartCalendarInterval": {"Weekday": 0, "Hour": 14, "Minute": 0},
            "StandardOutPath": str(LOG_DIR / "ytdlp-update.log"),
            "StandardErrorPath": str(LOG_DIR / "ytdlp-update.log"),
            **common,
        },
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=30, check=False)


def is_loaded(label: str) -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{label}").returncode == 0


def install(python: str = sys.executable) -> list[Path]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for label, definition in definitions(python).items():
        path = plist_path(label)
        if is_loaded(label):
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        path.write_bytes(plistlib.dumps(definition))
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        if result.returncode != 0 and not is_loaded(label):
            raise RuntimeError(f"launchctl 无法加载 {label}：{result.stderr.strip()}")
        written.append(path)
    return written


def uninstall() -> None:
    for label in (WORKER_LABEL, UPDATE_LABEL):
        if is_loaded(label):
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        plist_path(label).unlink(missing_ok=True)
