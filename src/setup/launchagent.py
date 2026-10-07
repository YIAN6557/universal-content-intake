"""Install the background agents as per-user LaunchAgents.

Two maintenance agents keep the download engines current for every user: the
weekly yt-dlp updater and the engine check (daily trigger, runs every two
weeks, only notifies). The Worker is installed only when automatic monitoring
is on.
"""

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
ENGINE_CHECK_LABEL = "local.universal-content-intake.engine-check"
MAINTENANCE_LABELS = (UPDATE_LABEL, ENGINE_CHECK_LABEL)
ALL_LABELS = (WORKER_LABEL, *MAINTENANCE_LABELS)
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
        ENGINE_CHECK_LABEL: {
            "Label": ENGINE_CHECK_LABEL,
            "ProgramArguments": [python, "-m", "src.queue.engine_check", "--scheduled"],
            "RunAtLoad": False,
            # Fires daily; the job itself only checks when the last check is two weeks old.
            "StartCalendarInterval": {"Hour": 14, "Minute": 30},
            "StandardOutPath": str(LOG_DIR / "engine-check.log"),
            "StandardErrorPath": str(LOG_DIR / "engine-check.log"),
            **common,
        },
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=30, check=False)


def is_loaded(label: str) -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{label}").returncode == 0


def owner(label: str = WORKER_LABEL) -> Path | None:
    """The project directory an installed agent runs from, or None when it is not installed."""

    try:
        definition = plistlib.loads(plist_path(label).read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    directory = definition.get("WorkingDirectory")
    return Path(str(directory)).resolve() if directory else None


def installed_here(label: str = WORKER_LABEL) -> bool:
    return owner(label) == PROJECT_ROOT.resolve()


def labels_for(monitoring: bool) -> tuple[str, ...]:
    return ALL_LABELS if monitoring else MAINTENANCE_LABELS


def install(python: str = sys.executable, labels: tuple[str, ...] = ALL_LABELS) -> list[Path]:
    # The labels are per user, so another clone of this project may already own them.
    for label in labels:
        other = owner(label)
        if other is not None and other != PROJECT_ROOT.resolve():
            raise RuntimeError(f"另一个安装目录（{other}）已经在用后台程序。先在那个目录运行 bin/uci settings --monitoring off，再在这里安装。")
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for label, definition in definitions(python).items():
        if label not in labels:
            continue
        path = plist_path(label)
        if is_loaded(label):
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        path.write_bytes(plistlib.dumps(definition))
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        if result.returncode != 0 and not is_loaded(label):
            raise RuntimeError(f"launchctl 无法加载 {label}：{result.stderr.strip()}")
        written.append(path)
    return written


def uninstall(labels: tuple[str, ...] = ALL_LABELS) -> None:
    """Remove this installation's agents; agents installed from another directory are left alone."""

    for label in labels:
        if owner(label) not in (None, PROJECT_ROOT.resolve()):
            continue
        if is_loaded(label):
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        plist_path(label).unlink(missing_ok=True)
