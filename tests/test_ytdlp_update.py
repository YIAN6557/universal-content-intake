from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.queue.ytdlp_update import PROBE_ID, YtDlpUpdater, render_update_launch_agent_plist


class FakePip:
    """Simulates pip + yt-dlp: versions change on install, probes follow a table."""

    def __init__(self, version: str, *, new_version: str, probe_by_version: dict[str, bool], upgrade_ok: bool = True) -> None:
        self.version = version
        self.new_version = new_version
        self.probe_by_version = probe_by_version
        self.upgrade_ok = upgrade_ok
        self.commands: list[list[str]] = []

    def __call__(self, command, timeout):
        command = list(command)
        self.commands.append(command)
        if command[1:4] == ["-m", "pip", "show"]:
            return subprocess.CompletedProcess(command, 0, f"Name: yt-dlp\nVersion: {self.version}\n", "")
        if command[1:4] == ["-m", "pip", "install"]:
            if "--upgrade" in command:
                if not self.upgrade_ok:
                    return subprocess.CompletedProcess(command, 1, "", "network error")
                self.version = self.new_version
            else:
                self.version = command[-1].split("==", 1)[1]
            return subprocess.CompletedProcess(command, 0, "", "")
        ok = self.probe_by_version.get(self.version, False)
        return subprocess.CompletedProcess(command, 0 if ok else 1, PROBE_ID + "\n" if ok else "", "")


class YtDlpUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def updater(self, fake: FakePip) -> YtDlpUpdater:
        return YtDlpUpdater(python=Path("/py"), yt_dlp=Path("/yt-dlp"), deno=Path("/missing-deno"), state_dir=self.state_dir, runner=fake)

    def test_skips_while_worker_has_active_task(self) -> None:
        (self.state_dir / "active.json").write_text("{}", encoding="utf-8")
        fake = FakePip("1", new_version="2", probe_by_version={"2": True})
        self.assertEqual(self.updater(fake).run().status, "SKIPPED_ACTIVE_TASK")
        self.assertEqual(fake.commands, [])

    def test_verified_upgrade_is_kept(self) -> None:
        fake = FakePip("1", new_version="2", probe_by_version={"2": True})
        outcome = self.updater(fake).run()
        self.assertEqual((outcome.status, outcome.previous_version, outcome.current_version), ("UPDATED", "1", "2"))
        upgrade = next(command for command in fake.commands if "--upgrade" in command)
        self.assertIn("--pre", upgrade)
        self.assertIn("--user", upgrade)

    def test_failed_probe_rolls_back_to_previous_version(self) -> None:
        fake = FakePip("1", new_version="2", probe_by_version={"1": True, "2": False})
        outcome = self.updater(fake).run()
        self.assertEqual(outcome.status, "ROLLED_BACK")
        self.assertEqual(fake.version, "1")
        self.assertIn(["/py", "-m", "pip", "install", "--user", "--disable-pip-version-check", "--quiet", "yt-dlp[default]==1"], fake.commands)

    def test_unchanged_version_does_not_probe(self) -> None:
        fake = FakePip("2", new_version="2", probe_by_version={})
        self.assertEqual(self.updater(fake).run().status, "UNCHANGED")
        self.assertFalse(any(command[0] == "/yt-dlp" for command in fake.commands))

    def test_pip_failure_is_reported(self) -> None:
        fake = FakePip("1", new_version="2", probe_by_version={}, upgrade_ok=False)
        self.assertEqual(self.updater(fake).run().status, "FAILED")

    def test_launch_agent_runs_weekly_outside_production_window(self) -> None:
        plist = plistlib.loads(render_update_launch_agent_plist(
            project_root=Path("/p"), python_executable=Path("/py"), log_dir=Path("/logs"),
        ).encode("utf-8"))
        self.assertEqual(plist["StartCalendarInterval"], {"Weekday": 0, "Hour": 14, "Minute": 0})
        self.assertEqual(plist["ProgramArguments"], ["/py", "-m", "src.queue.ytdlp_update"])
        self.assertFalse(plist["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
