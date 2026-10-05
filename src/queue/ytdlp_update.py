"""Weekly yt-dlp self-maintenance for the local Worker host.

YouTube changes often and an old yt-dlp is the most common reason downloads
start failing. This job upgrades yt-dlp (same nightly/pre-release channel the
host already uses), verifies it with a metadata-only probe, and rolls back to
the previous version if the new one does not work. It never runs while the
Worker has an active task.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from src.providers.video import DENO_PATH, YTDLP_PATH
from src.queue.worker import Notifier, macos_notifier

# The interpreter that runs this job is the one the Worker uses (see the LaunchAgent).
PYTHON = Path(sys.executable)
DEFAULT_STATE_DIR = Path("~/Library/Application Support/Universal Content Intake/worker").expanduser()
# "Me at the zoo": the first YouTube upload, stable and public, used only for a
# metadata probe (no media is downloaded).
PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
PROBE_ID = "jNQXAC9IVRw"

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, timeout=timeout, check=False)


@dataclass(frozen=True)
class UpdateOutcome:
    status: str  # SKIPPED_ACTIVE_TASK | UNCHANGED | UPDATED | ROLLED_BACK | FAILED
    previous_version: str | None
    current_version: str | None
    detail: str = ""


class YtDlpUpdater:
    def __init__(
        self,
        *,
        python: Path = PYTHON,
        yt_dlp: Path = YTDLP_PATH,
        deno: Path = DENO_PATH,
        state_dir: Path = DEFAULT_STATE_DIR,
        runner: Runner = _run,
    ) -> None:
        self.python = python
        self.yt_dlp = yt_dlp
        self.deno = deno
        self.state_dir = state_dir
        self.runner = runner

    def installed_version(self) -> str | None:
        try:
            result = self.runner([str(self.python), "-m", "pip", "show", "yt-dlp"], 60)
        except (OSError, subprocess.SubprocessError):
            return None
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip() or None
        return None

    def probe_ok(self) -> bool:
        command = [str(self.yt_dlp)]
        if self.deno.is_file():
            command += ["--js-runtimes", f"deno:{self.deno}"]
        command += ["--ignore-config", "--no-cache-dir", "--simulate", "--no-warnings", "--print", "id", PROBE_URL]
        try:
            result = self.runner(command, 180)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip() == PROBE_ID

    def _pip_install(self, requirement: str, *, upgrade: bool) -> bool:
        command = [str(self.python), "-m", "pip", "install", "--user", "--disable-pip-version-check", "--quiet"]
        if upgrade:
            command += ["--upgrade", "--pre"]
        command.append(requirement)
        try:
            return self.runner(command, 900).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def run(self) -> UpdateOutcome:
        if (self.state_dir / "active.json").exists():
            return UpdateOutcome("SKIPPED_ACTIVE_TASK", None, None, "Worker has an active task")
        previous = self.installed_version()
        if not self._pip_install("yt-dlp[default]", upgrade=True):
            return UpdateOutcome("FAILED", previous, self.installed_version(), "pip upgrade failed")
        current = self.installed_version()
        if current == previous:
            return UpdateOutcome("UNCHANGED", previous, current)
        if self.probe_ok():
            return UpdateOutcome("UPDATED", previous, current)
        if previous and self._pip_install(f"yt-dlp[default]=={previous}", upgrade=False) and self.probe_ok():
            return UpdateOutcome("ROLLED_BACK", previous, self.installed_version(), f"{current} failed the probe")
        return UpdateOutcome("FAILED", previous, self.installed_version(), f"{current} failed the probe and rollback did not verify")


def main(argv: list[str] | None = None, *, notifier: Notifier = macos_notifier) -> int:
    parser = argparse.ArgumentParser(prog="uci-ytdlp-update", description="Upgrade and verify yt-dlp for the UCI Worker")
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    outcome = YtDlpUpdater().run()
    logging.getLogger("uci.ytdlp_update").info(
        "operation=ytdlp_update status=%s previous=%s current=%s detail=%s",
        outcome.status, outcome.previous_version or "-", outcome.current_version or "-", outcome.detail or "-",
    )
    if outcome.status == "UPDATED":
        notifier("UCI：yt-dlp 已更新", f"{outcome.previous_version} → {outcome.current_version}，自检通过。")
    elif outcome.status == "ROLLED_BACK":
        notifier("UCI：yt-dlp 更新已回滚", f"新版本自检失败，已恢复 {outcome.current_version}。")
    elif outcome.status == "FAILED":
        notifier("UCI：yt-dlp 更新失败", outcome.detail or "请查看更新日志。")
    return 0 if outcome.status in {"SKIPPED_ACTIVE_TASK", "UNCHANGED", "UPDATED", "ROLLED_BACK"} else 1


def render_update_launch_agent_plist(*, project_root: Path, python_executable: Path, log_dir: Path) -> str:
    """Weekly Sunday 14:00 local time: outside the 00:00–08:00 production window."""

    import plistlib

    payload = {
        "Label": "local.universal-content-intake.ytdlp-update",
        "ProgramArguments": [str(python_executable), "-m", "src.queue.ytdlp_update"],
        "WorkingDirectory": str(project_root),
        "StartCalendarInterval": {"Weekday": 0, "Hour": 14, "Minute": 0},
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "ytdlp-update.log"),
        "StandardErrorPath": str(log_dir / "ytdlp-update.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


if __name__ == "__main__":
    sys.exit(main())
