import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from src.core import compat, policies
from src.get_cli import _safe_name


class LockTests(unittest.TestCase):
    def test_a_second_holder_is_refused_until_the_first_lets_go(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.lock"
            first = os.open(path, os.O_CREAT | os.O_RDWR)
            second = os.open(path, os.O_RDWR)
            try:
                compat.lock(first, blocking=False)
                with self.assertRaises(BlockingIOError):
                    compat.lock(second, blocking=False)
                compat.unlock(first)
                compat.lock(second, blocking=False)
                compat.unlock(second)
            finally:
                os.close(first)
                os.close(second)


class PathTests(unittest.TestCase):
    def test_tools_are_found_under_their_platform_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            name = "ffmpeg.exe" if compat.WINDOWS else "ffmpeg"
            tool = Path(directory) / name
            tool.write_text("")
            tool.chmod(0o755)
            from src.providers.video import locate_tool

            with mock.patch.dict(os.environ, {"PATH": ""}), mock.patch.object(compat, "tool_dirs", return_value=[Path(directory)]):
                self.assertEqual(locate_tool("ffmpeg", "UCI_TEST_UNSET"), tool)

    def test_mac_default_folders_are_respelled_on_windows(self) -> None:
        values = {"output": {"delivery_root": "~/Movies/Universal Content Intake/"},
                  "worker_state_dir": "~/Library/Application Support/Universal Content Intake/worker"}
        with mock.patch.object(policies.os, "name", "nt"), mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/a/AppData/Local"}):
            native = policies._native_defaults(values)
        self.assertEqual(native["output"]["delivery_root"], "~/Videos/Universal Content Intake/")
        self.assertEqual(native["worker_state_dir"], "C:/Users/a/AppData/Local/Universal Content Intake/worker")
        self.assertEqual(policies._native_defaults(values), values if os.name != "nt" else native)

    def test_titles_never_become_windows_device_names(self) -> None:
        self.assertEqual(_safe_name("CON"), "_CON")
        self.assertEqual(_safe_name("nul"), "_nul")
        self.assertEqual(_safe_name('a: "b"?'), "a b")
        self.assertEqual(_safe_name("ends with dots..."), "ends with dots")


class TaskSchedulerTests(unittest.TestCase):
    """The Windows background tasks mirror the LaunchAgents; these run on any system."""

    def setUp(self) -> None:
        from src.setup import launchagent

        self.launchagent = launchagent
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target, name, value in ((compat, "WINDOWS", True), (launchagent, "AGENTS_DIR", self.root),
                                    (launchagent, "LOG_DIR", self.root / "logs")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_task_runs_its_module_through_the_runner_on_the_launchagent_schedule(self) -> None:
        names = {"Sunday", "ScheduleByDay", "LogonTrigger"}
        seen = set()
        for label in self.launchagent.ALL_LABELS:
            document = self.launchagent.task_xml(label, sys.executable)
            tree = ET.fromstring(document.split("?>", 1)[1])
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            arguments = tree.find(".//t:Arguments", ns).text
            self.assertIn("-m src.setup.scheduled_run", arguments)
            self.assertEqual(Path(tree.find(".//t:WorkingDirectory", ns).text), self.launchagent.PROJECT_ROOT)
            seen |= {name for name in names if name in document}
            if label == self.launchagent.WORKER_LABEL:
                self.assertIn("--keep-alive 30", arguments)
                self.assertTrue(arguments.endswith("-- src.queue.worker_cli"))
            if label == self.launchagent.ENGINE_CHECK_LABEL:
                self.assertTrue(arguments.endswith("-- src.queue.engine_check --scheduled"))
                self.assertIn("T14:30:00", document)
        self.assertEqual(seen, names)

    def test_install_registers_only_the_maintenance_tasks_and_owns_them(self) -> None:
        calls = []
        with mock.patch.object(self.launchagent, "_schtasks", side_effect=lambda *args: calls.append(args) or mock.Mock(returncode=0)):
            written = self.launchagent.install(labels=self.launchagent.labels_for(False))
            self.assertEqual(len(written), 2)
            self.assertTrue(self.launchagent.installed_here(self.launchagent.UPDATE_LABEL))
            self.assertFalse(self.launchagent.installed_here(self.launchagent.WORKER_LABEL))
            self.assertEqual([call[0] for call in calls], ["/Create", "/Create"])
            self.assertIn("\\Universal Content Intake\\ytdlp-update", calls[0])
            self.launchagent.uninstall(labels=self.launchagent.MAINTENANCE_LABELS)
        self.assertFalse(any(self.root.glob("*.xml")))
        self.assertIn("/Delete", [call[0] for call in calls])

    def test_tasks_of_another_installation_block_install_and_survive_uninstall(self) -> None:
        with mock.patch.object(self.launchagent, "PROJECT_ROOT", self.root / "elsewhere"), \
                mock.patch.object(self.launchagent, "_schtasks", return_value=mock.Mock(returncode=0)):
            self.launchagent.install(labels=self.launchagent.MAINTENANCE_LABELS)
        with mock.patch.object(self.launchagent, "_schtasks", return_value=mock.Mock(returncode=0)) as schtasks:
            with self.assertRaises(RuntimeError):
                self.launchagent.install(labels=self.launchagent.MAINTENANCE_LABELS)
            self.launchagent.uninstall(labels=self.launchagent.MAINTENANCE_LABELS)
            self.assertFalse(any(call.args[0] == "/Delete" for call in schtasks.call_args_list))
        self.assertEqual(len(list(self.root.glob("*.xml"))), 2)


if __name__ == "__main__":
    unittest.main()
