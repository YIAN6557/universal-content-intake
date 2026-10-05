from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.job import Job
from src.media.stage3_workspace import Stage3Workspace
from src.media.subtitle_pipeline import SubtitleStageFailure, discover_and_select_subtitles
from src.media.subtitles import SubtitleTrack, exclude_machine_translated_tracks
from src.providers.base import ProviderFailure, ProviderFailureKind
from tests.test_stage3_pipeline import FakeVideoProvider, make_job


def auto_track(language: str, name: str = "") -> dict[str, object]:
    return {
        "language_code": language,
        "language_name": name or language,
        "source": "automatic",
        "provider": "yt-dlp",
        "format": "vtt",
        "track_identifier": f"automatic:{language}:json3",
        "available_formats": ["vtt"],
        "file_path": None,
        "extraction_status": "pending",
    }


def youtube_catalog() -> list[dict[str, object]]:
    # YouTube: one speech-recognition track plus machine translations into every language.
    translations = ["aa", "ab", "af", "ar", "de", "es", "fr", "ja", "ko", "zh-Hans", "zh-Hant"]
    return [auto_track("en", "English"), auto_track("en-Orig", "English (Original)")] + [auto_track(code) for code in translations]


class RecordingProvider(FakeVideoProvider):
    def __init__(self, fail_languages: set[str] | None = None, kind: ProviderFailureKind = ProviderFailureKind.NETWORK) -> None:
        super().__init__()
        self.languages: list[str] = []
        self.fail_languages = fail_languages or set()
        self.kind = kind

    def download_subtitle_track(self, job: Job, track: dict[str, object]) -> dict[str, object]:
        self.languages.append(str(track["language_code"]))
        if track["language_code"] in self.fail_languages:
            raise ProviderFailure(self.kind, "yt-dlp", "HTTP Error 429: Too Many Requests")
        return super().download_subtitle_track(job, track)


def track(language: str, source: str) -> SubtitleTrack:
    return SubtitleTrack(language, language, source, "yt-dlp", "vtt", f"{source}:{language}:vtt")


class MachineTranslationFilterTests(unittest.TestCase):
    def test_youtube_translations_are_excluded_and_manual_tracks_kept(self) -> None:
        tracks = [track("en", "automatic"), track("en-Orig", "automatic"), track("fr", "automatic"), track("de", "manual")]
        kept, excluded = exclude_machine_translated_tracks(tracks, "en")
        self.assertEqual([t.language_code for t in kept], ["en", "en-Orig", "de"])
        self.assertEqual([t.language_code for t in excluded], ["fr"])

    def test_catalog_without_orig_track_is_unchanged(self) -> None:
        tracks = [track("en", "automatic"), track("fr", "automatic")]
        kept, excluded = exclude_machine_translated_tracks(tracks, "en")
        self.assertEqual(kept, tuple(tracks))
        self.assertEqual(excluded, ())


class SubtitleDownloadScopeTests(unittest.TestCase):
    def test_youtube_catalog_downloads_only_original_language_tracks_primary_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, _, _ = make_job(Path(directory), tracks=youtube_catalog(), language="en")
            provider = RecordingProvider()
            result = discover_and_select_subtitles(job, provider, Stage3Workspace(job), "a" * 64)
        self.assertEqual(provider.languages, ["en", "en-Orig"])
        self.assertEqual(result["primary_language_code"], "en")
        self.assertIn("zh-Hans", result["excluded_machine_translated_languages"])
        self.assertEqual(len(result["excluded_machine_translated_languages"]), 11)

    def test_secondary_network_failure_is_recorded_and_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = [auto_track("en"), {**auto_track("en-GB"), "source": "manual", "track_identifier": "manual:en-GB:vtt"}, auto_track("en-Orig")]
            job, _, _ = make_job(Path(directory), tracks=catalog, language="en")
            provider = RecordingProvider(fail_languages={"en-GB"})
            result = discover_and_select_subtitles(job, provider, Stage3Workspace(job), "b" * 64)
        statuses = {item["language_code"]: item["extraction_status"] for item in result["tracks"]}
        self.assertEqual(result["status"], "selected")
        self.assertEqual(statuses["en"], "downloaded")
        self.assertEqual(statuses["en-GB"], "failed_non_primary")
        # after a network failure no further requests are sent
        self.assertEqual(statuses["en-Orig"], "skipped_after_network_failure")
        self.assertEqual(provider.languages, ["en", "en-GB"])

    def test_primary_failure_still_raises_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, _, _ = make_job(Path(directory), tracks=youtube_catalog(), language="en")
            provider = RecordingProvider(fail_languages={"en"})
            with self.assertRaises(SubtitleStageFailure) as caught:
                discover_and_select_subtitles(job, provider, Stage3Workspace(job), "c" * 64)
        self.assertEqual(caught.exception.provider_kind, ProviderFailureKind.NETWORK)
        self.assertEqual(provider.languages, ["en"])


if __name__ == "__main__":
    unittest.main()
