from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.media.asr import ASRRuntimeError, parse_vad_output, parse_whisper_json, transcribe_with_whisper_cpp


class Stage3ASRTests(unittest.TestCase):
    def test_silero_vad_speech_segments_are_parsed_structurally(self) -> None:
        output = "Detected 2 speech segments:\nSpeech segment 0: start = 32.00, end = 221.00\nSpeech segment 1: start = 330.00, end = 377.00\n"
        self.assertEqual([(item.start, item.end) for item in parse_vad_output(output)], [(0.32, 2.21), (3.3, 3.77)])

    def test_vad_empty_segment_output_means_no_detected_speech(self) -> None:
        self.assertEqual(parse_vad_output("Detected 0 speech segments:"), ())

    def test_asr_uses_cue_timestamps_and_ignores_word_tokens(self) -> None:
        document = {
            "result": {"language": "en"},
            "transcription": [{
                "timestamps": {"from": "00:00:01,250", "to": "00:00:02,500"},
                "text": " hello",
                "tokens": [{"text": "hello", "timestamps": {"from": 1.8, "to": 2.0}}],
            }],
        }
        detected, cues = parse_whisper_json(document)
        self.assertEqual(detected, "en")
        self.assertEqual((cues[0].start, cues[0].end, cues[0].text), (1.25, 2.5, "hello"))

    def test_language_detection_failure_is_not_guessed(self) -> None:
        detected, cues = parse_whisper_json({"transcription": [{
                "timestamps": {"from": "00:00:00.000", "to": "00:00:01.000"},
                "text": "speech",
            }]})
        self.assertIsNone(detected)
        self.assertEqual(cues[0].source_language, "und")

    def test_whisper_json_language_missing_maps_to_asr_language_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, model, binary = (root / name for name in ("input.wav", "small.bin", "whisper-cli"))
            for path in (audio, model, binary):
                path.touch()
            output_base = root / "transcript"

            def fake_run(command: list[str], **kwargs: object) -> object:
                Path(command[command.index("--output-file") + 1] + ".json").write_text(json.dumps({
                    "transcription": [{
                        "timestamps": {"from": "00:00:00.000", "to": "00:00:01.000"},
                        "text": "speech",
                    }],
                }), encoding="utf-8")
                return type("Process", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("src.media.asr.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(ASRRuntimeError, "reliably detect"):
                    transcribe_with_whisper_cpp(audio, whisper_binary=binary, model_path=model, output_base=output_base)


if __name__ == "__main__":
    unittest.main()
