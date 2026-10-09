from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch

from src.media import online_translation
from src.core.job import ContentType, Job
from src.core.video_stage3_pipeline import _cached_or_transcribe, _translate
from src.media.stage3_workspace import Stage3Workspace
from src.media.subtitles import Cue
from src.output.paths import OutputWorkspace


def workspace(root: Path) -> tuple[Job, Stage3Workspace]:
    paths = OutputWorkspace.for_job(root, "stage3-resume").prepare()
    job = Job(
        job_id="stage3-resume",
        source_url="https://youtube.com/watch?v=resume",
        declared_content_type=ContentType.VIDEO,
        resolved_content_type=ContentType.VIDEO,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
    )
    return job, Stage3Workspace(job)



# These tests cover the Apple Translation path, which is the default only on macOS.
_APPLE_ENGINE = mock.patch.object(online_translation, "load_settings",
                                  return_value=online_translation.Settings("apple", "", "", ""))


def setUpModule() -> None:
    _APPLE_ENGINE.start()


def tearDownModule() -> None:
    _APPLE_ENGINE.stop()

class Stage3ResumeTests(unittest.TestCase):
    def test_completed_asr_artifact_is_reused_without_second_whisper_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, ws = workspace(Path(directory))
            audio = ws.path("stage3/audio/input.wav")
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"wav input")
            model = Path(directory) / "small.bin"
            model.write_bytes(b"small model")
            binary = Path(directory) / "whisper-cli"
            calls = {"count": 0}

            def transcribe(*args: object, **kwargs: object) -> dict[str, object]:
                calls["count"] += 1
                output_base = Path(str(kwargs["output_base"]))
                raw_path = output_base.with_suffix(".json")
                raw_path.write_text('{"result":{"language":"en"},"transcription":[]}', encoding="utf-8")
                return {
                    "detected_language": "en",
                    "language_confidence": None,
                    "cues": [{"cue_id": "cue-1", "start": 0.0, "end": 1.0, "text": "Hello", "source_language": "en", "translated_text": None}],
                    "engine": "whisper.cpp",
                    "model_path": str(model),
                    "model_sha256": "a" * 64,
                    "invocation": {"language": "auto", "timestamps": "cue_segments"},
                    "raw_json_path": str(raw_path),
                }

            with patch("src.core.video_stage3_pipeline.transcribe_with_whisper_cpp", side_effect=transcribe):
                first = _cached_or_transcribe(audio, ws, audio_sha="b" * 64, language_hint=None, whisper_binary=binary, model_path=model)
                second = _cached_or_transcribe(audio, ws, audio_sha="b" * 64, language_hint=None, whisper_binary=binary, model_path=model)
            self.assertEqual(calls["count"], 1)
            self.assertEqual(first["cues"], second["cues"])
            self.assertEqual(first["model_sha256"], second["model_sha256"])

    def test_completed_translation_artifact_is_reused_without_second_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, ws = workspace(Path(directory))
            helper = Path(directory) / "uci-translation"
            helper.write_bytes(b"helper binary")
            cue = Cue("cue-1", 1.25, 2.5, "Hello world", "en")
            calls = {"translate": 0, "preflight": 0}
            response = {
                "schema_version": 1,
                "status": "success",
                "runtime": "Apple Translation CLI",
                "source_language": "en",
                "target_language": "zh-Hans",
                "language_pair_supported": True,
                "language_resources_installed": True,
                "translation_engine": "Apple Translation / lowLatency",
                "cues": [{
                    "cue_id": "cue-1", "start": 1.25, "end": 2.5,
                    "source_text": "Hello world", "source_language": "en",
                    "target_language": "zh-Hans", "translated_text": "你好，世界",
                }],
            }

            def translate(*args: object, **kwargs: object) -> dict[str, object]:
                calls["translate"] += 1
                response_path = Path(str(kwargs["response_path"]))
                response_path.parent.mkdir(parents=True, exist_ok=True)
                response_path.write_text(json.dumps(response), encoding="utf-8")
                return response

            def preflight(*args: object, **kwargs: object) -> dict[str, object]:
                calls["preflight"] += 1
                return {"status": "ready", "source_language": "en", "target_language": "zh-Hans"}

            with patch("src.core.video_stage3_pipeline.translate_cues", side_effect=translate), \
                 patch("src.core.video_stage3_pipeline.translation_preflight", side_effect=preflight):
                first, _ = _translate((cue,), "en", "c" * 64, ws, helper=helper)
                second, metadata = _translate((cue,), "en", "c" * 64, ws, helper=helper)
            self.assertEqual(calls, {"translate": 1, "preflight": 1})
            self.assertEqual(first[0].translated_text, "你好，世界")
            self.assertEqual(second[0].translated_text, first[0].translated_text)
            self.assertTrue(metadata["resumed"])


if __name__ == "__main__":
    unittest.main()
