import io
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest import mock

from src.queue import engine_check
from src.queue.engine_check import Engine


def engine(key, installed, repo="owner/repo"):
    return Engine(key, f"{key} 引擎", repo, lambda: installed, lambda latest: f"update {key} to {latest}")


NOW = datetime(2026, 10, 7, 6, 30, tzinfo=timezone.utc)


class VersionTests(unittest.TestCase):
    def test_versions_compare_numerically_whatever_the_tag_looks_like(self) -> None:
        self.assertTrue(engine_check.is_newer("v1.32.15", "1.32.13"))
        self.assertTrue(engine_check.is_newer("release-1.37.0", "1.36.9"))
        self.assertTrue(engine_check.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(engine_check.is_newer("v2.9.4", "deno 2.9.4 (stable)"))
        self.assertFalse(engine_check.is_newer("2.9", "2.9.0"))
        self.assertFalse(engine_check.is_newer("nightly", "1.0"))


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "engine-check.json"

    def test_reports_newer_engines_with_the_update_command_and_skips_missing_ones(self) -> None:
        engines = [engine("gallery-dl", "1.32.13"), engine("rclone", None), engine("deno", "2.9.7")]
        tags = {"owner/repo": "v1.32.15"}
        findings = engine_check.check(engines, fetch=lambda repo: tags[repo] if True else "")
        self.assertEqual([item.key for item in findings], ["gallery-dl", "deno"])
        self.assertTrue(findings[0].newer)
        self.assertEqual(findings[0].update, "update gallery-dl to 1.32.15")
        self.assertFalse(findings[1].newer)  # 2.9.7 installed vs 1.32.15 "latest": never a downgrade

    def test_a_failed_lookup_is_reported_not_raised(self) -> None:
        def fetch(repo):
            raise RuntimeError("没有找到正式发布的版本")

        findings = engine_check.check([engine("aria2", "1.37.0")], fetch=fetch)
        self.assertEqual(findings[0].error, "没有找到正式发布的版本")
        self.assertFalse(findings[0].newer)

    def test_runs_every_two_weeks_and_retries_tomorrow_when_github_was_unreachable(self) -> None:
        engines = [engine("gdown", "6.4.0")]
        ok = lambda repo: "v6.4.1"
        self.assertIsNotNone(engine_check.run(force=False, now=NOW, fetch=ok, engines=engines, state_path=self.state))
        for days in (1, 7, 13):
            self.assertIsNone(engine_check.run(force=False, now=NOW + timedelta(days=days), fetch=ok, engines=engines,
                                               state_path=self.state))
        # The daily trigger fires at the same clock time; day 14 counts even if the last run started a bit later.
        self.assertIsNotNone(engine_check.run(force=False, now=NOW + timedelta(days=14, minutes=-5), fetch=ok,
                                              engines=engines, state_path=self.state))

        def offline(repo):
            raise OSError("network down")

        later = NOW + timedelta(days=28)
        self.assertIsNotNone(engine_check.run(force=False, now=later, fetch=offline, engines=engines, state_path=self.state))
        self.assertEqual(engine_check.read_state(self.state)["checked_at"], (NOW + timedelta(days=14, minutes=-5)).isoformat())
        self.assertIsNotNone(engine_check.run(force=False, now=later + timedelta(days=1), fetch=ok, engines=engines,
                                              state_path=self.state))

    def test_status_summary_reads_the_last_result(self) -> None:
        self.assertIn("还没有检查过", engine_check.summary_lines({})[0])
        engine_check.run(force=True, now=NOW, fetch=lambda repo: "v6.4.1", engines=[engine("gdown", "6.4.0")],
                         state_path=self.state)
        lines = engine_check.summary_lines(engine_check.read_state(self.state))
        self.assertIn("2026-10-07", lines[0])
        self.assertIn("6.4.0 → 6.4.1", lines[1])
        self.assertIn("update gdown to 6.4.1", lines[2])

    def test_scheduled_run_notifies_only_when_something_is_newer(self) -> None:
        notes = []
        findings = [engine_check.Finding("gdown", "gdown", "r", "6.4.0", "6.4.1", True, "u")]
        with mock.patch.object(engine_check, "run", return_value=findings):
            engine_check.main(["--scheduled"], notifier=lambda *message: notes.append(message))
        self.assertEqual(notes, [("UCI：1 个下载引擎有新版本", "gdown 6.4.1。查看更新命令：bin/uci engines")])
        with mock.patch.object(engine_check, "run", return_value=[]):
            engine_check.main(["--scheduled"], notifier=lambda *message: notes.append(message))
        self.assertEqual(len(notes), 1)
        output = io.StringIO()
        with mock.patch.object(engine_check, "run", return_value=findings), redirect_stdout(output):
            engine_check.main([], notifier=lambda *message: notes.append(message))
        self.assertEqual(len(notes), 1)  # a manual check prints instead of notifying
        self.assertIn("GitHub 最新 6.4.1", output.getvalue())


class LatestReleaseTests(unittest.TestCase):
    def test_reads_the_tag_from_the_releases_latest_redirect(self) -> None:
        headers = Message()
        headers["Location"] = "https://github.com/mikf/gallery-dl/releases/tag/v1.32.15"
        error = urllib.error.HTTPError("https://github.com/mikf/gallery-dl/releases/latest", 302, "Found", headers, None)
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch.object(engine_check.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(engine_check.latest_release("mikf/gallery-dl"), "v1.32.15")
        request = opener.open.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method()), ("https://github.com/mikf/gallery-dl/releases/latest", "HEAD"))

    def test_a_repository_without_releases_is_an_error(self) -> None:
        headers = Message()
        headers["Location"] = "https://github.com/owner/repo/releases"
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("u", 302, "Found", headers, None)
        with mock.patch.object(engine_check.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(RuntimeError):
                engine_check.latest_release("owner/repo")


if __name__ == "__main__":
    unittest.main()
