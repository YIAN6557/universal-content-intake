import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src import get_cli, uci_cli
from src.setup import cli as setup_cli
from src.setup import environment, preferences, store


class TempConfig(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = mock.patch.dict(os.environ, {"UCI_CONFIG": str(self.root / "config.yaml")})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Never mistake this Mac's own LaunchAgent for an existing installation.
        plist = mock.patch.object(preferences.launchagent, "installed_here", return_value=False)
        plist.start()
        self.addCleanup(plist.stop)

    def answers(self, *replies):
        queue = list(replies)
        return lambda prompt="": queue.pop(0)


class FirstRunTests(TempConfig):
    def test_nothing_runs_until_both_questions_are_answered_when_nobody_can_be_asked(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(uci_cli, "_interactive", return_value=False):
            code = uci_cli.main(["https://example.com/report.pdf"])
        self.assertEqual(code, 2)
        self.assertIn("bin/uci settings --monitoring on|off --subtitles on|off|ask", output.getvalue())
        self.assertFalse(preferences.load().complete)

    def test_interactive_first_run_saves_both_choices(self) -> None:
        lines: list[str] = []
        prefs = preferences.ensure_first_run(interactive=True, read=self.answers("maybe", "n", "3"), out=lines.append)
        self.assertEqual((prefs.monitoring, prefs.subtitles), (False, "ask"))
        self.assertEqual(preferences.load(), prefs)
        self.assertTrue(any("请输入方括号里的选项" in line for line in lines))
        # Asked once only.
        self.assertEqual(preferences.ensure_first_run(interactive=False, out=lines.append), prefs)

    def test_an_installation_with_monitoring_already_set_up_is_only_asked_about_subtitles(self) -> None:
        store.update_user_config({"queue_api_url": "https://script.google.com/macros/s/x/exec"})
        lines: list[str] = []
        prefs = preferences.ensure_first_run(interactive=True, read=self.answers("1"), out=lines.append)
        self.assertEqual((prefs.monitoring, prefs.subtitles), (True, "on"))
        self.assertFalse(any("问题 1" in line for line in lines))

    def test_answering_through_settings_skips_the_questions(self) -> None:
        with redirect_stdout(io.StringIO()), mock.patch.object(uci_cli, "_interactive", return_value=False):
            code = uci_cli.main(["settings", "--monitoring", "off", "--subtitles", "off"])
        self.assertEqual(code, 0)
        self.assertEqual(preferences.load(), preferences.Preferences(False, "off"))

    def test_status_without_monitoring_explains_how_to_turn_it_on(self) -> None:
        preferences.save(monitoring=False, subtitles="off")
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(uci_cli, "_interactive", return_value=False):
            self.assertEqual(uci_cli.main(["status"]), 0)
        self.assertIn("bin/uci settings --monitoring on", output.getvalue())


class SubtitleChoiceTests(unittest.TestCase):
    def test_flags_win_then_the_saved_choice(self) -> None:
        decide = get_cli.decide_subtitles
        self.assertTrue(decide(zh=True, no_zh=False, preference="off", interactive=False))
        self.assertFalse(decide(zh=False, no_zh=True, preference="on", interactive=False))
        self.assertTrue(decide(zh=False, no_zh=False, preference="on", interactive=False))
        self.assertFalse(decide(zh=False, no_zh=False, preference="off", interactive=False))
        with self.assertRaises(get_cli.GetError):
            decide(zh=True, no_zh=True, preference="on", interactive=True)

    def test_ask_every_time_prompts_or_stops_without_a_terminal(self) -> None:
        decide = get_cli.decide_subtitles
        replies = iter(["", "y"])
        self.assertTrue(decide(zh=False, no_zh=False, preference="ask", interactive=True, read=lambda _: next(replies)))
        self.assertFalse(decide(zh=False, no_zh=False, preference="ask", interactive=True, read=lambda _: "n"))
        with self.assertRaises(get_cli.GetError) as caught:
            decide(zh=False, no_zh=False, preference="ask", interactive=False)
        self.assertIn("--zh", str(caught.exception))


class SettingsTests(TempConfig):
    def test_turning_monitoring_off_stops_the_worker_and_pauses_the_cloud(self) -> None:
        store.update_user_config({"queue_api_url": "https://script.google.com/macros/s/x/exec"})
        preferences.save(monitoring=True, subtitles="on")
        from src.setup import cloud, launchagent

        with mock.patch.object(cloud, "secret_exists", return_value=True), \
                mock.patch.object(cloud, "set_monitoring") as paused, \
                mock.patch.object(launchagent, "installed_here", return_value=True), \
                mock.patch.object(launchagent, "uninstall") as stopped, redirect_stdout(io.StringIO()):
            uci_cli.cmd_settings(["--monitoring", "off"], interactive=False)
        paused.assert_called_once_with(False)
        stopped.assert_called_once()
        self.assertFalse(preferences.load().monitoring)

    def test_turning_monitoring_on_before_setup_points_to_the_wizard(self) -> None:
        preferences.save(monitoring=False, subtitles="off")
        output = io.StringIO()
        with redirect_stdout(output):
            uci_cli.cmd_settings(["--monitoring", "on"], interactive=False)
        self.assertTrue(preferences.load().monitoring)
        self.assertIn("bin/uci setup", output.getvalue())

    def test_switching_subtitles_on_offers_the_missing_tools(self) -> None:
        preferences.save(monitoring=False, subtitles="off")
        output = io.StringIO()
        with mock.patch.object(get_cli, "subtitle_tools_missing", return_value=["whisper.cpp"]), redirect_stdout(output):
            uci_cli.cmd_settings(["--subtitles", "ask"], interactive=False)
        self.assertEqual(preferences.load().subtitles, "ask")
        self.assertIn("bin/uci setup build-tools", output.getvalue())


class WizardStepsTests(TempConfig):
    def steps(self, monitoring, subtitles):
        ctx = setup_cli.Context(checks=[], state={}, ids={}, config={}, secret=False, inspect=None, inspect_error="",
                                prefs=preferences.Preferences(monitoring, subtitles))
        with mock.patch.object(environment, "translation_status", return_value=(True, "ready")):
            return [step.key for step in setup_cli.build_steps(ctx)]

    def test_download_only_needs_just_the_environment_and_a_trial_download(self) -> None:
        self.assertEqual(self.steps(False, "off"), ["env", "verify-download"])

    def test_subtitles_add_the_tools_and_translation_pack(self) -> None:
        self.assertEqual(self.steps(False, "ask"), ["env", "tools", "translation", "verify-download"])

    def test_monitoring_adds_the_cloud_steps_and_always_needs_the_subtitle_tools(self) -> None:
        keys = self.steps(True, "off")
        self.assertEqual(keys[:3], ["env", "tools", "translation"])
        self.assertIn("clasp-login", keys)
        self.assertEqual(keys[-1], "verify")
        self.assertNotIn("verify-download", keys)

    def test_doctor_ignores_tools_for_features_that_are_off(self) -> None:
        check = environment.Check("clasp", "clasp", False)
        self.assertTrue(setup_cli.relevant(check, preferences.Preferences(False, "on")))
        self.assertEqual(setup_cli.relevant(check, preferences.Preferences(True, "off")), "")
        whisper = environment.Check("whisper", "whisper", False)
        self.assertTrue(setup_cli.relevant(whisper, preferences.Preferences(False, "off")))
        self.assertEqual(setup_cli.relevant(whisper, preferences.Preferences(False, "ask")), "")


class LaunchAgentOwnershipTests(unittest.TestCase):
    """The agents are per user; a second clone (or a test) must never touch another installation's worker."""

    def setUp(self) -> None:
        from src.setup import launchagent

        self.launchagent = launchagent
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.agents = Path(directory.name)
        patcher = mock.patch.object(launchagent, "AGENTS_DIR", self.agents)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_agent(self, label, working_directory):
        import plistlib

        (self.agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "WorkingDirectory": str(working_directory)}))

    def test_another_installations_agents_are_left_alone(self) -> None:
        for label in (self.launchagent.WORKER_LABEL, self.launchagent.UPDATE_LABEL):
            self.write_agent(label, self.agents / "elsewhere")
        self.assertFalse(self.launchagent.installed_here())
        with mock.patch.object(self.launchagent, "_launchctl") as launchctl:
            self.launchagent.uninstall()
            launchctl.assert_not_called()
        self.assertTrue((self.agents / f"{self.launchagent.WORKER_LABEL}.plist").is_file())
        with self.assertRaises(RuntimeError):
            self.launchagent.install()

    def test_this_installations_agents_are_recognized_and_removed(self) -> None:
        for label in (self.launchagent.WORKER_LABEL, self.launchagent.UPDATE_LABEL):
            self.write_agent(label, self.launchagent.PROJECT_ROOT)
        self.assertTrue(self.launchagent.installed_here())
        with mock.patch.object(self.launchagent, "_launchctl", return_value=mock.Mock(returncode=1)):
            self.launchagent.uninstall()
        self.assertFalse((self.agents / f"{self.launchagent.WORKER_LABEL}.plist").exists())


if __name__ == "__main__":
    unittest.main()
