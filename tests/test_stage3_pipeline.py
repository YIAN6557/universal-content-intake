from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.job import ContentType, Job, JobState
from src.core.video_stage3_pipeline import run_video_stage3_pipeline
from src.media.asr import ASRRuntimeError
from src.media.stage3_workspace import sha256_file
from src.media.subtitles import Cue
from src.output.paths import OutputWorkspace
from src.providers.base import ProviderFailure, ProviderFailureKind


VIDEO_FACTS = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "3.0"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360, "sample_aspect_ratio": "1:1", "display_aspect_ratio": "16:9"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


class FakeVideoProvider:
    def __init__(self) -> None:
        self.downloads = 0

    def download_subtitle_track(self, job: Job, track: dict[str, object]) -> dict[str, object]:
        self.downloads += 1
        source_hash = str(track["source_sha256"])
        track_id = str(track["track_identifier"])
        safe_id = "".join(character if character.isalnum() or character in "_.-" else "_" for character in track_id).strip("._") or "track"
        relative = f"stage3/subtitles/source-{source_hash[:16]}/{track['source']}/{safe_id}/{track['language_code']}.vtt"
        path = Path(job.temp_path) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("WEBVTT\n\n00:00:00.500 --> 00:00:02.500\n你好，世界。\n", encoding="utf-8")
        return {
            "file_path": relative,
            "format": "vtt",
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "extraction_status": "downloaded",
            "authentication": {"auth_attempted": False, "auth_source": None, "authenticated_retry": False, "auth_result": "not_attempted"},
        }


def make_job(root: Path, *, tracks: list[dict[str, object]], language: str = "zh-Hans") -> tuple[Job, Path, Path]:
    paths = OutputWorkspace.for_job(root, "stage3-pipeline").prepare()
    source = paths.temp_dir / "source_video" / "source_video.mp4"
    source.parent.mkdir()
    source.write_bytes(b"validated-stage2-video-fixture")
    artifact_hash = sha256_file(source)
    job = Job(
        job_id="stage3-pipeline",
        source_url="https://youtube.com/watch?v=fixture",
        declared_content_type=ContentType.VIDEO,
        resolved_content_type=ContentType.VIDEO,
        current_state=JobState.DOWNLOADING,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"providers": {"yt-dlp": {
            "probe": {"video_id": "fixture", "canonical_url": "https://youtube.com/watch?v=fixture", "original_language": language, "subtitle_tracks": tracks},
            "validated_artifact": {"path": "temp/source_video/source_video.mp4", "size_bytes": source.stat().st_size, "sha256": artifact_hash},
        }}},
    )
    job_file = paths.job_dir / "job.json"
    job_file.write_text(job.to_json(), encoding="utf-8")
    return job, job_file, source


def zh_track() -> dict[str, object]:
    return {
        "language_code": "zh-Hans",
        "language_name": "Chinese (Simplified)",
        "source": "manual",
        "provider": "yt-dlp",
        "format": "vtt",
        "track_identifier": "manual:zh-Hans:vtt",
        "available_formats": ["vtt"],
        "file_path": None,
        "extraction_status": "pending",
    }


def en_track() -> dict[str, object]:
    return {
        "language_code": "en",
        "language_name": "English",
        "source": "automatic",
        "provider": "yt-dlp",
        "format": "vtt",
        "track_identifier": "automatic:en:vtt",
        "available_formats": ["vtt"],
        "file_path": None,
        "extraction_status": "pending",
    }


class Stage3PipelineTests(unittest.TestCase):
    def test_chinese_track_skips_translation_and_resume_reuses_subtitle_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file, _ = make_job(root, tracks=[zh_track()])
            provider = FakeVideoProvider()
            fonts = root / "fonts"
            fonts.mkdir()
            font = fonts / "font.otf"
            font.write_bytes(b"font fixture")
            rendered = {"calls": 0}

            def fake_render(source: Path, ass: Path, output: Path, **kwargs: object) -> dict[str, object]:
                rendered["calls"] += 1
                output.write_bytes(b"stage3 hardsub video")
                return {"ffprobe": {"container": "mp4", "artifact_readable": True}, "stage3_subtitle_render_transcode": True}

            with patch("src.core.video_stage3_pipeline.probe_media", return_value=VIDEO_FACTS), \
                 patch("src.core.video_stage3_pipeline.burn_in_subtitles", side_effect=fake_render), \
                 patch("src.core.video_stage3_pipeline.translation_preflight") as preflight, \
                 patch("src.core.video_stage3_pipeline.translate_cues") as translate:
                first = run_video_stage3_pipeline(job, job_file=job_file, video_provider=provider, ffprobe=Path("fake-ffprobe"), fonts_dir=fonts, font_file=font)
                self.assertEqual(first.status, "validated", first.error.to_dict() if first.error else first.result)
                first_hash = first.result["validated_artifact"]["sha256"]
                # Stage 5 only accepts Job-relative "temp/..." artifact paths.
                self.assertTrue(first.result["validated_artifact"]["path"].startswith("temp/stage3/render/"))
                second = run_video_stage3_pipeline(job, job_file=job_file, video_provider=provider, ffprobe=Path("fake-ffprobe"), fonts_dir=fonts, font_file=font)
            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(job.current_state, JobState.RENDERING)
            self.assertEqual(provider.downloads, 1)
            self.assertEqual(rendered["calls"], 1)
            self.assertEqual(second.result["validated_artifact"]["sha256"], first_hash)
            self.assertTrue(second.result["render"]["resumed"])
            self.assertEqual(second.result["translation"]["status"], "skipped_source_is_zh_hans")
            preflight.assert_not_called()
            translate.assert_not_called()
            render_files = list((Path(job.temp_path) / "stage3/render").glob("source-*/validated-subtitled-video.mp4"))
            self.assertEqual(len(render_files), 1)

    def test_no_subtitles_and_no_vad_speech_create_no_caption_and_skip_asr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file, _ = make_job(root, tracks=[])
            audio = Path(job.temp_path) / "stage3/audio/test.wav"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"pcm fixture")
            with patch("src.core.video_stage3_pipeline.probe_media", return_value=VIDEO_FACTS), \
                 patch("src.core.video_stage3_pipeline._cached_or_prepare_audio", return_value=(audio, {"sample_rate": 16000, "channels": 1, "codec": "pcm_s16le", "container": "wav"})), \
                 patch("src.core.video_stage3_pipeline._cached_or_vad", return_value={"speech_detected": False, "speech_duration_seconds": 0, "speech_segments": [], "engine": "Silero VAD", "result_status": "no_speech"}) as vad, \
                 patch("src.core.video_stage3_pipeline.transcribe_with_whisper_cpp") as transcribe:
                outcome = run_video_stage3_pipeline(job, job_file=job_file, video_provider=FakeVideoProvider(), ffprobe=Path("fake-ffprobe"))
            self.assertTrue(outcome.success, outcome.error.to_dict() if outcome.error else outcome.result)
            self.assertEqual(outcome.result["status"], "validated_no_speech_no_subtitles")
            self.assertEqual(outcome.result["render"]["status"], "not_required")
            self.assertEqual(outcome.result["validated_artifact"]["role"], "SOURCE_VIDEO_PASSTHROUGH")
            self.assertEqual(job.current_state, JobState.RENDERING)
            vad.assert_called_once()
            transcribe.assert_not_called()

    def test_unsupported_apple_pair_maps_to_translation_unsupported_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            track = {**zh_track(), "language_code": "en", "language_name": "English", "track_identifier": "manual:en:vtt"}
            job, job_file, _ = make_job(root, tracks=[track], language="en")
            provider = FakeVideoProvider()
            helper = root / "uci-translation"
            helper.write_bytes(b"helper")
            unsupported = {
                "status": "unsupported",
                "source_language": "en",
                "target_language": "zh-Hans",
                "error": {"code": "TRANSLATION_UNSUPPORTED", "message": "pair unavailable"},
            }
            with patch("src.core.video_stage3_pipeline.probe_media", return_value=VIDEO_FACTS), \
                 patch("src.core.video_stage3_pipeline.translation_preflight", return_value=unsupported), \
                 patch("src.core.video_stage3_pipeline.translate_cues") as translate:
                outcome = run_video_stage3_pipeline(job, job_file=job_file, video_provider=provider, translation_helper=helper)
            self.assertFalse(outcome.success)
            self.assertEqual(job.current_state, JobState.TRANSLATION_UNSUPPORTED, outcome.error.to_dict() if outcome.error else outcome.result)
            self.assertEqual(outcome.error.code.value, "TRANSLATION_UNSUPPORTED")
            self.assertEqual(provider.downloads, 1)
            translate.assert_not_called()

    def test_confirmed_subtitle_extraction_failure_maps_to_subtitle_failed_without_vad_fallback(self) -> None:
        class FailingProvider(FakeVideoProvider):
            def download_subtitle_track(self, job: Job, track: dict[str, object]) -> dict[str, object]:
                raise ProviderFailure(
                    ProviderFailureKind.FAILED,
                    "yt-dlp",
                    "subtitle download failed",
                    details={"cause": "fixture extraction failure"},
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file, _ = make_job(root, tracks=[zh_track()])
            with patch("src.core.video_stage3_pipeline.probe_media", return_value=VIDEO_FACTS), \
                 patch("src.core.video_stage3_pipeline._cached_or_prepare_audio") as prepare_audio, \
                 patch("src.core.video_stage3_pipeline._cached_or_vad") as vad:
                outcome = run_video_stage3_pipeline(job, job_file=job_file, video_provider=FailingProvider(), ffprobe=Path("fake-ffprobe"))
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error.code.value, "SUBTITLE_FAILED")
            self.assertEqual(job.current_state, JobState.SUBTITLE_FAILED)
            prepare_audio.assert_not_called()
            vad.assert_not_called()

    def test_speech_path_runs_asr_translation_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file, _ = make_job(root, tracks=[])
            audio = Path(job.temp_path) / "stage3/audio/test.wav"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"pcm fixture")
            original = Cue("cue-1", 0.5, 2.0, "Hello there", "en")
            translated = Cue("cue-1", 0.5, 2.0, "Hello there", "en", "你好")
            fonts = root / "fonts"
            fonts.mkdir()
            font = fonts / "font.otf"
            font.write_bytes(b"font fixture")

            def fake_render(source: Path, ass: Path, output: Path, **kwargs: object) -> dict[str, object]:
                output.write_bytes(b"rendered hardsub video")
                return {"ffprobe": {"container": "mp4", "artifact_readable": True}}

            with patch("src.core.video_stage3_pipeline.probe_media", return_value=VIDEO_FACTS), \
                 patch("src.core.video_stage3_pipeline._cached_or_prepare_audio", return_value=(audio, {"sample_rate": 16000})), \
                 patch("src.core.video_stage3_pipeline._cached_or_vad", return_value={"speech_detected": True, "speech_duration_seconds": 1.5, "speech_segments": [{"start": 0.4, "end": 2.1}]}), \
                 patch("src.core.video_stage3_pipeline._cached_or_transcribe", return_value={"detected_language": "en", "language_confidence": None, "cues": [original.to_dict()], "engine": "whisper.cpp", "model_sha256": "a" * 64}) as asr, \
                 patch("src.core.video_stage3_pipeline._translate", return_value=((translated,), {"status": "translated", "resumed": False})) as translate, \
                 patch("src.core.video_stage3_pipeline.burn_in_subtitles", side_effect=fake_render):
                outcome = run_video_stage3_pipeline(job, job_file=job_file, video_provider=FakeVideoProvider(), ffprobe=Path("fake-ffprobe"), fonts_dir=fonts, font_file=font)

            self.assertTrue(outcome.success, outcome.error.to_dict() if outcome.error else outcome.result)
            self.assertEqual(job.current_state, JobState.RENDERING)
            self.assertEqual(outcome.result["speech_status"], "speech_detected")
            self.assertEqual(outcome.result["asr"]["engine"], "whisper.cpp")
            self.assertEqual(outcome.result["translation"]["status"], "translated")
            asr.assert_called_once()
            translate.assert_called_once()

    def test_asr_execution_failure_maps_to_asr_failed_and_preserves_source_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file, source = make_job(root, tracks=[])
            audio = Path(job.temp_path) / "stage3/audio/test.wav"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"pcm fixture")
            source_hash = sha256_file(source)
            with patch("src.core.video_stage3_pipeline.probe_media", return_value=VIDEO_FACTS), \
                 patch("src.core.video_stage3_pipeline._cached_or_prepare_audio", return_value=(audio, {"sample_rate": 16000})), \
                 patch("src.core.video_stage3_pipeline._cached_or_vad", return_value={"speech_detected": True, "speech_duration_seconds": 1.5, "speech_segments": [{"start": 0.4, "end": 2.1}]}), \
                 patch("src.core.video_stage3_pipeline._cached_or_transcribe", side_effect=ASRRuntimeError("fixture whisper failure")):
                outcome = run_video_stage3_pipeline(job, job_file=job_file, video_provider=FakeVideoProvider(), ffprobe=Path("fake-ffprobe"))
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error.code.value, "ASR_FAILED")
            self.assertEqual(job.current_state, JobState.ASR_FAILED)
            self.assertEqual(sha256_file(source), source_hash)

    def test_translation_resource_unavailable_pauses_without_starting_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            track = en_track()
            job, job_file, _ = make_job(root, tracks=[track], language="en")
            helper = root / "uci-translation"
            helper.write_bytes(b"helper")
            unavailable = {
                "status": "resource_unavailable",
                "source_language": "en",
                "target_language": "zh-Hans",
                "error": {"code": "TRANSLATION_RESOURCE_UNAVAILABLE", "message": "not installed"},
            }
            with patch("src.core.video_stage3_pipeline.probe_media", return_value=VIDEO_FACTS), \
                 patch("src.core.video_stage3_pipeline.translation_preflight", return_value=unavailable), \
                 patch("src.core.video_stage3_pipeline.translate_cues") as translate:
                outcome = run_video_stage3_pipeline(job, job_file=job_file, video_provider=FakeVideoProvider(), translation_helper=helper)
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.status, "paused")
            self.assertEqual(job.current_state, JobState.TRANSLATING)
            self.assertEqual(outcome.result["status"], "paused_translation_resource_unavailable")
            translate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
