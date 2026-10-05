from __future__ import annotations

import dataclasses

import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.core.job import ContentType, Job
from src.core.policies import load_default_policies
from src.output.paths import OutputWorkspace
from src.providers.base import ProviderFailure, ProviderFailureKind
from src.providers.video import (
    VideoFormat,
    VideoProvider,
    _classify_process_failure,
    normalize_ffprobe_result,
    normalize_yt_dlp_info,
    select_video_format,
    verify_downloaded_video,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = PROJECT_ROOT / "config" / "defaults.yaml"
FFPROBE_1080 = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "10.0"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


def raw_info(formats: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "id": "stage2-fixture",
        "title": "Stage 2 public fixture",
        "webpage_url": "https://www.youtube.com/watch?v=stage2-fixture",
        "extractor_key": "Youtube",
        "channel": "Fixture Channel",
        "upload_date": "20250102",
        "duration": 10,
        "formats": formats if formats is not None else [
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "width": 640, "height": 360, "tbr": 500},
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080, "tbr": 2500},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 128},
            {"format_id": "401", "ext": "mp4", "vcodec": "av01.0.12M.08", "acodec": "none", "width": 3840, "height": 2160, "tbr": 8000},
        ],
        "subtitles": {"en": [{}]},
        "automatic_captions": {"zh-Hans": [{}]},
    }


def make_job(root: Path, *, requested_quality: str | None = None) -> Job:
    workspace = OutputWorkspace.for_job(root, "video-fixture")
    paths = workspace.prepare()
    return Job(
        job_id="video-fixture",
        source_url="https://www.youtube.com/watch?v=stage2-fixture",
        declared_content_type=ContentType.VIDEO,
        resolved_content_type=ContentType.VIDEO,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        requested_options={"video": {"quality": requested_quality}} if requested_quality else {},
    )


class ProbeNormalizationTests(unittest.TestCase):
    def test_failure_classifier_distinguishes_invalid_auth_network_and_provider_errors(self) -> None:
        self.assertEqual(_classify_process_failure(1, "ERROR: unable to download webpage: HTTP Error 404: Not Found"), ProviderFailureKind.INVALID_URL)
        self.assertEqual(_classify_process_failure(1, "Sign in to confirm your age"), ProviderFailureKind.AUTH_REQUIRED)
        self.assertEqual(_classify_process_failure(1, "ERROR: [youtube] LOGIN_REQUIRED"), ProviderFailureKind.AUTH_REQUIRED)
        self.assertEqual(_classify_process_failure(1, "ERROR: authentication required"), ProviderFailureKind.AUTH_REQUIRED)
        self.assertEqual(_classify_process_failure(1, "ERROR: HTTP Error 401: Unauthorized"), ProviderFailureKind.AUTH_REQUIRED)
        self.assertNotEqual(_classify_process_failure(1, "ERROR: HTTP Error 403: Forbidden"), ProviderFailureKind.AUTH_REQUIRED)
        self.assertEqual(_classify_process_failure(1, "ERROR: HTTP Error 429: Too Many Requests"), ProviderFailureKind.NETWORK)
        self.assertEqual(_classify_process_failure(1, "ERROR: unexpected extractor failure"), ProviderFailureKind.FAILED)

    def test_probe_normalizes_provider_neutral_metadata_and_subtitle_availability(self) -> None:
        video = normalize_yt_dlp_info(raw_info(), "https://www.youtube.com/watch?v=stage2-fixture")
        self.assertEqual(video.video_id, "stage2-fixture")
        self.assertEqual(video.platform, "Youtube")
        self.assertEqual(video.published_at, "2025-01-02")
        self.assertEqual(video.subtitle_languages, ("en",))
        self.assertEqual(video.automatic_subtitle_languages, ("zh-Hans",))
        self.assertEqual(len(video.formats), 4)

    def test_probe_preserves_original_language_and_safe_manual_automatic_track_catalog(self) -> None:
        info = {
            **raw_info(),
            "language": "en-US",
            "subtitles": {"en-US": [{"ext": "vtt", "format_id": "en-vtt", "name": "English", "url": "https://signed.example/sub?secret=redact"}]},
            "automatic_captions": {"zh-Hans": [{"ext": "json3", "format_id": "zh-auto", "name": "简体中文", "url": "https://signed.example/auto?secret=redact"}]},
        }
        video = normalize_yt_dlp_info(info, "https://www.youtube.com/watch?v=stage2-fixture")
        metadata = video.to_dict()
        self.assertEqual(video.original_language, "en-US")
        self.assertEqual(metadata["subtitle_tracks"], [
            {
                "language_code": "en-US",
                "language_name": "English",
                "source": "manual",
                "provider": "yt-dlp",
                "format": "vtt",
                "track_identifier": "manual:en-US:en-vtt",
                "available_formats": ["vtt"],
                "file_path": None,
                "extraction_status": "pending",
            },
            {
                "language_code": "zh-Hans",
                "language_name": "简体中文",
                "source": "automatic",
                "provider": "yt-dlp",
                "format": "json3",
                "track_identifier": "automatic:zh-Hans:zh-auto",
                "available_formats": ["json3"],
                "file_path": None,
                "extraction_status": "pending",
            },
        ])
        self.assertNotIn("signed.example", json.dumps(metadata))

    def test_playlist_and_collection_metadata_are_rejected_before_download(self) -> None:
        for source_type in ("playlist", "multi_video"):
            with self.subTest(source_type=source_type), self.assertRaises(ProviderFailure) as caught:
                normalize_yt_dlp_info({**raw_info(), "_type": source_type, "entries": [{}]}, "https://example.com/list")
            self.assertEqual(caught.exception.kind, ProviderFailureKind.UNSUPPORTED)

    def test_private_or_member_source_maps_to_auth_required(self) -> None:
        with self.assertRaises(ProviderFailure) as caught:
            normalize_yt_dlp_info({**raw_info(), "availability": "subscriber_only"}, "https://example.com/x")
        self.assertEqual(caught.exception.kind, ProviderFailureKind.AUTH_REQUIRED)


class FormatSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        info = normalize_yt_dlp_info(raw_info(), "https://www.youtube.com/watch?v=stage2-fixture")
        self.formats = info.formats

    def test_default_1080p_selects_separate_h264_aac_streams(self) -> None:
        result = select_video_format(self.formats, requested_resolution=1080)
        self.assertEqual(result.format_ids, ("137", "140"))
        self.assertEqual(result.selector, "137+140")
        self.assertEqual(result.selected_resolution, 1080)
        self.assertTrue(result.requires_merge)
        self.assertFalse(result.to_dict()["transcode"])

    def test_720p_falls_back_to_nearest_available_below_cap(self) -> None:
        result = select_video_format(self.formats, requested_resolution=720)
        self.assertEqual(result.selected_resolution, 360)
        self.assertEqual(result.width, 640)

    def test_1440p_and_4k_requests_never_select_above_the_cap(self) -> None:
        qhd = select_video_format(self.formats, requested_resolution=1440)
        uhd = select_video_format(self.formats, requested_resolution=2160)
        self.assertEqual(qhd.selected_resolution, 1080)
        self.assertEqual(uhd.selected_resolution, 2160)
        self.assertEqual(uhd.video_codec, "av01.0.12M.08")

    def test_highest_is_explicit_and_selects_maximum_available(self) -> None:
        result = select_video_format(self.formats, requested_resolution=None)
        self.assertEqual(result.selected_resolution, 2160)

    def test_no_upscale_and_no_over_default_resolution(self) -> None:
        only_360 = [item for item in self.formats if item.quality_dimension == 360]
        result = select_video_format(only_360, requested_resolution=1080)
        self.assertEqual(result.selected_resolution, 360)
        with self.assertRaises(ProviderFailure):
            select_video_format(self.formats, requested_resolution=240)

    def test_direct_mp4_h264_aac_is_supported_without_merge(self) -> None:
        direct = VideoFormat("22", "mp4", "avc1.64001F", "mp4a.40.2", 1280, 720, 30, 1800, None)
        result = select_video_format([direct], requested_resolution=1080)
        self.assertEqual(result.format_ids, ("22",))
        self.assertFalse(result.requires_merge)
        self.assertTrue(result.direct_compatible_mp4)

    def test_auto_dubbed_audio_tracks_never_replace_the_original_soundtrack(self) -> None:
        video = VideoFormat("270", "mp4", "avc1.640028", "none", 1920, 1080, 30, 2500, None)
        dubbed = [VideoFormat(f"140-{index}", "m4a", "none", "mp4a.40.2", None, None, None, 129.6, None,
                              language=language, language_preference=-1, format_note=f"{language}, medium")
                  for index, language in enumerate(("ar", "de-DE", "ja"))]
        original = VideoFormat("140-20", "m4a", "none", "mp4a.40.2", None, None, None, 129.5, None,
                               language="en-US", language_preference=10, format_note="English (US) original (default), medium")
        result = select_video_format([video, *dubbed, original], requested_resolution=1080)
        self.assertEqual(result.selector, "270+140-20")
        # A video without language metadata keeps the old behaviour.
        plain = VideoFormat("140", "m4a", "none", "mp4a.40.2", None, None, None, 128, None)
        self.assertEqual(select_video_format([video, plain], requested_resolution=1080).selector, "270+140")
        parsed = VideoFormat.from_mapping({"format_id": "140-0", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
                                           "language": "ar", "language_preference": -1, "format_note": "Arabic, medium"})
        self.assertTrue(parsed.is_dubbed)

    def test_avc3_dash_codec_is_classified_as_h264_preference(self) -> None:
        video = VideoFormat("avc3", "mp4", "avc3.640028", None, 1920, 1080, 25, 2000, None)
        audio = VideoFormat("aac", "m4a", None, "mp4a.40.2", None, None, None, 128, None)
        result = select_video_format([video, audio], requested_resolution=1080)
        self.assertEqual(result.format_ids, ("avc3", "aac"))
        self.assertTrue(result.requires_merge)


class FFprobeVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = select_video_format(
            normalize_yt_dlp_info(raw_info(), "https://www.youtube.com/watch?v=stage2-fixture").formats,
            requested_resolution=1080,
        )

    def test_ffprobe_accepts_mp4_with_expected_streams_and_records_real_codecs(self) -> None:
        result = verify_downloaded_video(FFPROBE_1080, self.selection, expected_audio=True)
        self.assertEqual(result.container, "mp4")
        self.assertEqual(result.video_codec, "h264")
        self.assertEqual(result.audio_codec, "aac")
        self.assertEqual(result.duration_seconds, 10.0)

    def test_ffprobe_rejects_non_mp4_missing_video_missing_audio_and_wrong_size(self) -> None:
        bad_inputs = (
            ({"format": {"format_name": "matroska", "duration": "10"}, "streams": FFPROBE_1080["streams"]}, False),
            ({"format": FFPROBE_1080["format"], "streams": [{"codec_type": "audio", "codec_name": "aac"}]}, False),
            ({"format": FFPROBE_1080["format"], "streams": [FFPROBE_1080["streams"][0]]}, True),
            ({"format": FFPROBE_1080["format"], "streams": [{**FFPROBE_1080["streams"][0], "width": 3840, "height": 2160}, FFPROBE_1080["streams"][1]]}, True),
        )
        for payload, expected_audio in bad_inputs:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                verify_downloaded_video(payload, self.selection, expected_audio=expected_audio)


class VideoProviderWorkspaceTests(unittest.TestCase):
    def _provider(
        self,
        tmp: Path,
        *,
        fail_first_download: bool = False,
        probe_data: dict[str, object] | None = None,
        probe_failure: str | None = None,
        authenticated_probe_failure: str | None = None,
        allow_browser_cookies: bool = True,
    ) -> tuple[VideoProvider, Path]:
        probe_payload = json.dumps(probe_data or raw_info())
        call_log = tmp / "calls.jsonl"
        marker = tmp / "first-download-failed"
        fetch_script = "\n".join((
            "import json, pathlib, sys",
            f"log = pathlib.Path({str(call_log)!r})",
            "log.open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')",
            "args = sys.argv[1:]",
            "if '--dump-single-json' in args:",
            f"    failure = {authenticated_probe_failure!r} if '--cookies-from-browser' in args else {probe_failure!r}",
            "    if failure:",
            "        print(failure, file=sys.stderr)",
            "        raise SystemExit(1)",
            f"    print({probe_payload!r})",
            "    raise SystemExit(0)",
            "paths = next((arg[5:] for arg in args if arg.startswith('temp:')), None)",
            "out = pathlib.Path(paths)",
            "out.mkdir(parents=True, exist_ok=True)",
            f"marker = pathlib.Path({str(marker)!r})",
            f"if {fail_first_download!r} and not marker.exists():",
            "    marker.write_text('failed once', encoding='utf-8')",
            "    (out / 'source_video.f137.mp4.part').write_bytes(b'partial')",
            "    print('ERROR: timed out while downloading', file=sys.stderr)",
            "    raise SystemExit(1)",
            "(out / 'source_video.mp4').write_bytes(b'validated-fixture-video')",
            "print('[Merger] Merging formats into mp4', file=sys.stderr)",
            "print('[VideoRemuxer] Remuxing video', file=sys.stderr)",
        ))
        ffprobe_payload = json.dumps(FFPROBE_1080)
        ffprobe_script = f"print({ffprobe_payload!r})"
        provider = VideoProvider(
            policies=dataclasses.replace(load_default_policies(DEFAULTS), video_allow_browser_cookies=allow_browser_cookies),
            yt_dlp_command=(sys.executable, "-c", fetch_script),
            ffprobe_command=(sys.executable, "-c", ffprobe_script),
            ffmpeg_path=Path("ffmpeg"),
        )
        return provider, call_log

    def _persist_probe(self, provider: VideoProvider, job: Job) -> str:
        result = provider.probe(job)
        job.store_provider_metadata(provider.name, result.metadata)
        return str(result.resume_token)

    def test_browser_session_retry_is_off_unless_the_installation_opts_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, call_log = self._provider(
                root,
                probe_failure="ERROR: [youtube] LOGIN_REQUIRED: Sign in to confirm you're not a bot",
                allow_browser_cookies=False,
            )
            with self.assertRaises(ProviderFailure) as raised:
                provider.probe(make_job(root / "jobs"))
            self.assertIs(raised.exception.kind, ProviderFailureKind.AUTH_REQUIRED)
            self.assertIn("allow_browser_cookies", str(raised.exception))
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertFalse(any("--cookies-from-browser" in part for part in calls[0]))
        self.assertFalse(load_default_policies(DEFAULTS).video_allow_browser_cookies)

    def test_probe_retries_with_chrome_only_after_explicit_youtube_auth_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, call_log = self._provider(
                root,
                probe_failure="ERROR: [youtube] LOGIN_REQUIRED: Sign in to confirm you're not a bot",
            )
            job = make_job(root / "jobs")

            result = provider.probe(job)

            self.assertEqual(result.metadata["authentication"], {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": True,
                "auth_result": "success",
            })
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertFalse(any("--cookies-from-browser" in call for call in calls[0]))
            self.assertEqual(calls[1][calls[1].index("--cookies-from-browser") + 1], "chrome")
            self.assertNotIn("cookies-from-browser", json.dumps(result.metadata).lower())
            self.assertEqual(list(Path(job.temp_path).rglob("*")), [])

    def test_probe_does_not_use_chrome_for_non_auth_failure(self) -> None:
        cases = (
            ("ERROR: HTTP Error 429: Too Many Requests", ProviderFailureKind.NETWORK),
            ("ERROR: HTTP Error 403: Forbidden", ProviderFailureKind.FAILED),
        )
        for message, expected_kind in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                provider, call_log = self._provider(root, probe_failure=message)
                job = make_job(root / "jobs")

                with self.assertRaises(ProviderFailure) as caught:
                    provider.probe(job)

                self.assertEqual(caught.exception.kind, expected_kind)
                calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(calls), 1)
                self.assertFalse(any("--cookies-from-browser" in call for call in calls[0]))

    def test_probe_keeps_auth_required_when_chrome_retry_fails_without_secret_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, call_log = self._provider(
                root,
                probe_failure="ERROR: [youtube] LOGIN_REQUIRED",
                authenticated_probe_failure="ERROR: [youtube] LOGIN_REQUIRED",
            )
            job = make_job(root / "jobs")

            with self.assertRaises(ProviderFailure) as caught:
                provider.probe(job)

            self.assertEqual(caught.exception.kind, ProviderFailureKind.AUTH_REQUIRED)
            self.assertEqual(caught.exception.details["authentication"], {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": False,
                "auth_result": "failure",
            })
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][calls[1].index("--cookies-from-browser") + 1], "chrome")
            self.assertNotIn("cookie value", json.dumps(caught.exception.details).lower())

    def test_probe_auth_fallback_is_limited_to_youtube_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, call_log = self._provider(root, probe_failure="ERROR: LOGIN_REQUIRED")
            job = make_job(root / "jobs")
            job.source_url = "https://example.com/video"

            with self.assertRaises(ProviderFailure) as caught:
                provider.probe(job)

            self.assertEqual(caught.exception.kind, ProviderFailureKind.AUTH_REQUIRED)
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertFalse(any("--cookies-from-browser" in call for call in calls[0]))

    def test_probe_authentication_is_reused_for_download_without_cookie_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, call_log = self._provider(
                root,
                probe_failure="ERROR: [youtube] LOGIN_REQUIRED",
            )
            job = make_job(root / "jobs")
            token = self._persist_probe(provider, job)

            result = provider.fetch(job, resume_token=token)

            self.assertEqual(result.metadata["authentication"], {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": True,
                "auth_result": "success",
            })
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            fetch_calls = [call for call in calls if "--dump-single-json" not in call]
            self.assertEqual(len(fetch_calls), 1)
            self.assertEqual(fetch_calls[0][fetch_calls[0].index("--cookies-from-browser") + 1], "chrome")
            encoded = json.dumps(result.metadata).lower()
            self.assertNotIn("cookie", encoded)
            self.assertEqual(list(Path(job.temp_path).rglob("*cookies*")), [])

    def test_anonymous_interrupted_download_resumes_and_ffprobe_validates_source_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, call_log = self._provider(root, fail_first_download=True)
            job = make_job(root / "jobs")
            token = self._persist_probe(provider, job)
            with self.assertRaises(ProviderFailure) as caught:
                provider.fetch(job, resume_token=token)
            self.assertEqual(caught.exception.kind, ProviderFailureKind.NETWORK)
            self.assertEqual(caught.exception.resume_token, token)
            result = provider.fetch(job, resume_token=token)
            artifact = job.temp_path and Path(job.temp_path) / "source_video" / "source_video.mp4"
            self.assertTrue(artifact.is_file())
            self.assertEqual(result.artifacts[0].role, "SOURCE_VIDEO")
            self.assertEqual(result.artifacts[0].relative_path, "temp/source_video/source_video.mp4")
            operations = result.metadata["fetch"]["operations"]
            self.assertTrue(operations["merge"])
            self.assertTrue(operations["remux"])
            self.assertEqual(operations["verified_output_video_codec"], "h264")
            self.assertEqual(operations["verified_output_audio_codec"], "aac")
            self.assertFalse(operations["transcode"])
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            fetch_calls = [call for call in calls if "--dump-single-json" not in call]
            self.assertEqual(len(fetch_calls), 2)
            self.assertIn("--continue", fetch_calls[1])
            self.assertIn("137+140", fetch_calls[1])
            self.assertTrue(all("--ignore-config" in call and "--no-cache-dir" in call for call in calls))
            self.assertFalse(any("cookie" in arg.lower() for call in calls for arg in call))
            output_template = fetch_calls[1][fetch_calls[1].index("--output") + 1]
            Path(output_template.replace("%(ext)s", "mp4")).relative_to(Path(job.temp_path))
            self.assertFalse((Path(job.output_path) / "final.mp4").exists())

    def test_completed_download_is_verified_and_reused_without_duplicate_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, call_log = self._provider(root)
            job = make_job(root / "jobs")
            token = self._persist_probe(provider, job)
            first = provider.fetch(job, resume_token=token)
            second = provider.fetch(job, resume_token=token)
            self.assertEqual(first.artifacts, second.artifacts)
            self.assertTrue(second.metadata["fetch"]["completed_artifact_reused"])
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum("--dump-single-json" not in call for call in calls), 1)

    def test_path_symlink_escape_is_rejected_before_resume_files_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, _ = self._provider(root)
            job = make_job(root / "jobs")
            token = self._persist_probe(provider, job)
            outside = root / "outside"
            outside.mkdir()
            (Path(job.temp_path) / "uci_video").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProviderFailure) as caught:
                provider.fetch(job, resume_token=token)
            self.assertEqual(caught.exception.kind, ProviderFailureKind.FAILED)
            self.assertEqual(list(outside.iterdir()), [])

    def test_missing_core_persisted_probe_or_resume_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, _ = self._provider(root)
            job = make_job(root / "jobs")
            with self.assertRaises(ProviderFailure):
                provider.fetch(job, resume_token="invented")
            token = self._persist_probe(provider, job)
            with self.assertRaises(ProviderFailure):
                provider.fetch(job, resume_token="wrong-token")
            self.assertTrue(token)

    def test_direct_media_with_incomplete_ytdlp_facts_is_enriched_by_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generic_direct = {
                **raw_info(),
                "formats": [{"format_id": "mp4", "ext": "mp4", "vcodec": None, "acodec": "none"}],
            }
            provider, _ = self._provider(root, probe_data=generic_direct)
            job = make_job(root / "jobs")
            result = provider.probe(job)
            self.assertEqual(result.metadata["selection"]["selected_resolution"], 1080)
            self.assertEqual(result.metadata["selection"]["video_codec"], "h264")
            self.assertEqual(result.metadata["selection"]["audio_codec"], "aac")
            self.assertEqual(result.metadata["probe"]["width"], 1920)
            self.assertEqual(result.metadata["probe"]["height"], 1080)
            self.assertEqual(result.metadata["probe"]["duration_seconds"], 10.0)


if __name__ == "__main__":
    unittest.main()
