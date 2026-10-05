from __future__ import annotations

import unittest

from src.media.subtitles import Cue
from src.media.translation import TranslationProtocolError, apply_translation_response, make_translation_request


class TranslationProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cues = (
            Cue("cue-000001", 1.0, 2.5, "Hello", "en"),
            Cue("cue-000002", 3.0, 4.0, "World", "en"),
        )

    def test_request_contains_structured_cues_and_fixed_zh_hans_target(self) -> None:
        request = make_translation_request(self.cues, source_language="en", target_language="zh-Hans")
        self.assertEqual(request["target_language"], "zh-Hans")
        self.assertEqual(request["cues"][0], {
            "cue_id": "cue-000001", "start": 1.0, "end": 2.5,
            "source_text": "Hello", "source_language": "en", "target_language": "zh-Hans",
        })

    def test_response_preserves_identity_timestamps_and_source_text(self) -> None:
        response = {
            "source_language": "en", "target_language": "zh-Hans",
            "cues": [
                {"cue_id": "cue-000001", "start": 1.0, "end": 2.5, "source_text": "Hello", "translated_text": "你好", "source_language": "en", "target_language": "zh-Hans"},
                {"cue_id": "cue-000002", "start": 3.0, "end": 4.0, "source_text": "World", "translated_text": "世界", "source_language": "en", "target_language": "zh-Hans"},
            ],
        }
        result = apply_translation_response(self.cues, response, target_language="zh-Hans")
        self.assertEqual([(cue.cue_id, cue.start, cue.end, cue.text, cue.translated_text) for cue in result], [
            ("cue-000001", 1.0, 2.5, "Hello", "你好"),
            ("cue-000002", 3.0, 4.0, "World", "世界"),
        ])

    def test_timestamp_identity_text_or_language_mutation_is_rejected(self) -> None:
        response = {"source_language": "en", "target_language": "zh-Hans", "cues": [
            {"cue_id": "cue-000001", "start": 1.01, "end": 2.5, "source_text": "Hello", "translated_text": "你好", "source_language": "en", "target_language": "zh-Hans"},
            {"cue_id": "cue-000002", "start": 3.0, "end": 4.0, "source_text": "World", "translated_text": "世界", "source_language": "en", "target_language": "zh-Hans"},
        ]}
        with self.assertRaises(TranslationProtocolError):
            apply_translation_response(self.cues, response, target_language="zh-Hans")

    def test_missing_or_duplicate_cue_identity_is_rejected(self) -> None:
        response = {"source_language": "en", "target_language": "zh-Hans", "cues": [
            {"cue_id": "cue-000001", "start": 1.0, "end": 2.5, "source_text": "Hello", "translated_text": "你好", "source_language": "en", "target_language": "zh-Hans"},
            {"cue_id": "cue-000001", "start": 3.0, "end": 4.0, "source_text": "World", "translated_text": "世界", "source_language": "en", "target_language": "zh-Hans"},
        ]}
        with self.assertRaises(TranslationProtocolError):
            apply_translation_response(self.cues, response, target_language="zh-Hans")


if __name__ == "__main__":
    unittest.main()
