import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.queue import engine_check
from src.setup import environment


@unittest.skipIf(os.name == "nt", "Windows downloads prebuilt binaries; tests/test_online_translation.py covers that")
class WhisperUpdateTests(unittest.TestCase):
    """whisper.cpp follows its newest release, but a build only replaces the old one after the self-test passes."""

    def setUp(self) -> None:
        self.log: list[str] = []
        patches = [
            mock.patch.object(environment.shutil, "which", return_value="/usr/bin/cmake"),
            mock.patch.object(environment, "_executable", return_value=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, *, installed, latest, failing=()):
        built = []

        def build_tag(tag, cmake, log):
            built.append(tag)
            if tag in failing:
                raise RuntimeError("识别结果不对")

        fetch = mock.Mock(side_effect=latest) if isinstance(latest, Exception) else mock.Mock(return_value=latest)
        with mock.patch.object(environment, "installed_whisper_version", return_value=installed), \
                mock.patch.object(engine_check, "latest_release", fetch), \
                mock.patch.object(environment, "_build_whisper_tag", side_effect=build_tag):
            try:
                environment.build_whisper(update=True, log=self.log.append)
            except RuntimeError as error:
                return built, str(error)
        return built, None

    def test_a_newer_release_is_built(self) -> None:
        self.assertEqual(self.build(installed="v1.9.4", latest="v1.9.5"), (["v1.9.5"], None))

    def test_nothing_happens_when_already_current(self) -> None:
        self.assertEqual(self.build(installed="v1.9.5", latest="v1.9.5"), ([], None))
        self.assertIn("已是最新", self.log[-1])

    def test_a_release_that_fails_the_self_test_leaves_the_old_build_in_place(self) -> None:
        built, error = self.build(installed="v1.9.4", latest="v1.9.5", failing={"v1.9.5"})
        self.assertEqual(built, ["v1.9.5"])
        self.assertIn("已保留原来的 v1.9.4", error)

    def test_a_first_install_falls_back_to_the_known_good_release(self) -> None:
        built, error = self.build(installed=None, latest="v2.0.0", failing={"v2.0.0"})
        self.assertEqual((built, error), (["v2.0.0", environment.WHISPER_FALLBACK_TAG], None))
        built, error = self.build(installed=None, latest=OSError("offline"))
        self.assertEqual((built, error), ([environment.WHISPER_FALLBACK_TAG], None))

    def test_without_github_an_existing_build_is_kept(self) -> None:
        built, error = self.build(installed="v1.9.4", latest=OSError("offline"))
        self.assertEqual(built, [])
        self.assertIn("保留现有的 v1.9.4", error)

    def test_builds_from_before_version_tracking_count_as_the_fallback_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(environment, "WHISPER_VERSION_FILE", Path(directory) / "VERSION"):
            self.assertEqual(environment.installed_whisper_version(), environment.WHISPER_FALLBACK_TAG)
            (Path(directory) / "VERSION").write_text("v1.9.5\n", encoding="utf-8")
            self.assertEqual(environment.installed_whisper_version(), "v1.9.5")


if __name__ == "__main__":
    unittest.main()
