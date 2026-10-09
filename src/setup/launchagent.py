"""Install the background agents: per-user LaunchAgents on macOS, Task Scheduler tasks on Windows.

Two maintenance agents keep the download engines current for every user: the
weekly yt-dlp updater and the engine check (daily trigger, runs every two
weeks, only notifies). The Worker is installed only when automatic monitoring
is on.

On Windows each agent becomes a task in the "Universal Content Intake" folder
of Task Scheduler. Task Scheduler cannot redirect output or restart a job that
exits with an error, so tasks run ``src.setup.scheduled_run``, which does both
the way launchd does on macOS. A copy of each task's definition is kept in
AGENTS_DIR, so ownership is checked the same way on both systems.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from src.core import compat
from src.queue.status_cli import WORKER_LABEL
from src.setup.environment import PROJECT_ROOT

UPDATE_LABEL = "local.universal-content-intake.ytdlp-update"
ENGINE_CHECK_LABEL = "local.universal-content-intake.engine-check"
MAINTENANCE_LABELS = (UPDATE_LABEL, ENGINE_CHECK_LABEL)
ALL_LABELS = (WORKER_LABEL, *MAINTENANCE_LABELS)
AGENTS_DIR = compat.data_dir() / "scheduled-tasks" if compat.WINDOWS else Path.home() / "Library" / "LaunchAgents"
LOG_DIR = compat.log_dir()
TASK_FOLDER = "Universal Content Intake"
SEARCH_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def plist_path(label: str) -> Path:
    return AGENTS_DIR / f"{label}.{'xml' if compat.WINDOWS else 'plist'}"


def task_name(label: str) -> str:
    return f"\\{TASK_FOLDER}\\{label.removeprefix('local.universal-content-intake.')}"


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


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, errors="replace", timeout=30, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def is_loaded(label: str) -> bool:
    if compat.WINDOWS:
        return _schtasks("/Query", "/TN", task_name(label)).returncode == 0
    return _launchctl("print", f"gui/{os.getuid()}/{label}").returncode == 0


def is_running(label: str) -> bool:
    """Windows: whether Task Scheduler reports the task as running right now."""

    result = _schtasks("/Query", "/TN", task_name(label), "/FO", "CSV", "/NH")
    # One CSV row: "TaskName","Next Run Time","Status"; the status is localised, so compare the English and Chinese words.
    status = result.stdout.strip().rsplit(",", 1)[-1].strip('" ').lower() if result.returncode == 0 else ""
    return status in {"running", "正在运行"}


def owner(label: str = WORKER_LABEL) -> Path | None:
    """The project directory an installed agent runs from, or None when it is not installed."""

    if compat.WINDOWS:
        try:
            match = re.search(r"<WorkingDirectory>(.*?)</WorkingDirectory>", plist_path(label).read_text(encoding="utf-16"))
        except (OSError, UnicodeError):
            return None
        directory = match.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") if match else None
        return Path(directory).resolve() if directory else None
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
    if compat.WINDOWS:
        return _install_tasks(python, labels)
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
        if compat.WINDOWS:
            if is_loaded(label):
                _schtasks("/End", "/TN", task_name(label))  # a running Worker stops with everything it started
                _schtasks("/Delete", "/TN", task_name(label), "/F")
        elif is_loaded(label):
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        plist_path(label).unlink(missing_ok=True)


# --- Windows: Task Scheduler ----------------------------------------------

def _windowless(python: str) -> str:
    """pythonw.exe runs without opening a console window each time a task fires."""

    candidate = Path(python).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else python


def task_xml(label: str, python: str) -> str:
    """The Task Scheduler definition matching the LaunchAgent with the same label."""

    definition = definitions(python)[label]
    module_args = definition["ProgramArguments"][1:]  # ["-m", "src.queue.…", …]
    runner = ["-m", "src.setup.scheduled_run", "--stdout", definition["StandardOutPath"],
              "--stderr", definition["StandardErrorPath"]]
    if label == WORKER_LABEL:
        runner += ["--keep-alive", str(definition["ThrottleInterval"])]
    arguments = " ".join(f'"{part}"' if " " in part else part for part in [*runner, "--", *module_args[1:]])
    start = datetime.now().strftime("%Y-%m-%dT")
    calendar = definition.get("StartCalendarInterval")
    if calendar is None:
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
        user_id = f"<UserId>{escape(user)}</UserId>" if user else ""
        trigger = f"<LogonTrigger><Enabled>true</Enabled>{user_id}</LogonTrigger>"
    else:
        at = f"{start}{calendar['Hour']:02d}:{calendar['Minute']:02d}:00"
        if "Weekday" in calendar:
            day = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")[calendar["Weekday"] % 7]
            schedule = f"<ScheduleByWeek><DaysOfWeek><{day} /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
        else:
            schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
        trigger = f"<CalendarTrigger><StartBoundary>{at}</StartBoundary><Enabled>true</Enabled>{schedule}</CalendarTrigger>"
    time_limit = "PT0S" if label == WORKER_LABEL else "PT2H"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Universal Content Intake: {escape(label)}</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_windowless(python))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(PROJECT_ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _install_tasks(python: str, labels: tuple[str, ...]) -> list[Path]:
    written = []
    for label in definitions(python):
        if label not in labels:
            continue
        path = plist_path(label)
        if label == WORKER_LABEL and is_loaded(label):
            _schtasks("/End", "/TN", task_name(label))  # restart with the new definition, like bootout + bootstrap
        path.write_text(task_xml(label, python), encoding="utf-16")
        result = _schtasks("/Create", "/TN", task_name(label), "/XML", str(path), "/F")
        if result.returncode != 0:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"任务计划程序无法创建 {task_name(label)}：{(result.stderr or result.stdout).strip()}")
        if label == WORKER_LABEL:  # RunAtLoad
            _schtasks("/Run", "/TN", task_name(label))
        written.append(path)
    return written
