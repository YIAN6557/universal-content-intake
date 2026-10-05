from __future__ import annotations

import random
import unittest

from src.media.subtitles import (
    SubtitleTrack,
    is_zh_hans_language,
    normalize_language_code,
    normalize_subtitle_tracks,
    parse_subtitle_text,
    select_primary_track,
)


class SubtitleNormalizationTests(unittest.TestCase):
    def test_normalizes_all_manual_and_automatic_tracks_without_quality_preference(self) -> None:
        tracks = normalize_subtitle_tracks(
            {"en_us": [{"ext": "vtt", "format_id": "vtt", "name": "English"}]},
            {"zh-Hans": [{"ext": "json3", "format_id": "json3", "name": "简体中文"}]},
        )
        self.assertEqual([(t.language_code, t.source, t.format) for t in tracks], [
            ("en-US", "manual", "vtt"),
            ("zh-Hans", "automatic", "json3"),
        ])

    def test_primary_prefers_exact_original_then_family_and_records_reason(self) -> None:
        tracks = normalize_subtitle_tracks(
            {"en": [{"ext": "vtt", "format_id": "en"}]},
            {"en-US": [{"ext": "vtt", "format_id": "en-us"}], "fr": [{"ext": "vtt", "format_id": "fr"}]},
        )
        decision = select_primary_track(tracks, "en-US")
        self.assertEqual(decision.track.language_code, "en-US")
        self.assertEqual(decision.reason, "original_language_exact_match")
        fallback = select_primary_track(tracks, "en-GB")
        self.assertEqual(fallback.track.language_code, "en")
        self.assertEqual(fallback.reason, "original_language_family_match")

    def test_manual_and_automatic_are_equal_priority_and_tie_break_is_order_independent(self) -> None:
        tracks = normalize_subtitle_tracks(
            {"en": [{"ext": "vtt", "format_id": "manual-vtt"}]},
            {"en": [{"ext": "vtt", "format_id": "auto-vtt"}]},
        )
        expected = select_primary_track(tracks, "en").track.track_identifier
        for seed in range(10):
            shuffled = list(tracks)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(select_primary_track(shuffled, "en").track.track_identifier, expected)
        # The source kind itself is not assigned a priority; lexical stable ID settles a tie.
        self.assertEqual(expected, "automatic:en:auto-vtt")

    def test_unknown_original_language_uses_provider_catalog_order_not_mapping_order(self) -> None:
        tracks = normalize_subtitle_tracks(
            {"fr": [{"ext": "srt", "format_id": "fr"}], "de": [{"ext": "vtt", "format_id": "de"}]},
            {"es": [{"ext": "vtt", "format_id": "es"}]},
        )
        self.assertEqual(select_primary_track(tracks, None).track.language_code, "de")

    def test_language_normalization_and_simplified_chinese_detection_are_code_based(self) -> None:
        self.assertEqual(normalize_language_code("zh_hans_cn"), "zh-Hans-CN")
        self.assertTrue(is_zh_hans_language("zh-Hans"))
        self.assertTrue(is_zh_hans_language("zh-CN"))
        self.assertTrue(is_zh_hans_language("zh"))
        self.assertFalse(is_zh_hans_language("zh-Hant"))
        self.assertFalse(is_zh_hans_language("en"))

    def test_vtt_and_srt_parsing_preserve_segment_times_and_text(self) -> None:
        vtt = """WEBVTT\n\ncue-a\n00:00:01.250 --> 00:00:02.500 align:start\n<c.green>Hello &amp; welcome</c>\n\n00:00:03.000 --> 00:00:04.125\nSecond line<br>continues\n"""
        cues = parse_subtitle_text(vtt, "vtt", source_language="en")
        self.assertEqual([(c.cue_id, c.start, c.end, c.text) for c in cues], [
            ("cue-000001", 1.25, 2.5, "Hello & welcome"),
            ("cue-000002", 3.0, 4.125, "Second line\ncontinues"),
        ])
        srt = """1\n00:00:00,500 --> 00:00:01,250\nFirst <i>caption</i>\n\n2\n00:00:01,500 --> 00:00:02,000\nSecond\n"""
        parsed = parse_subtitle_text(srt, "srt", source_language="fr")
        self.assertEqual(parsed[0].text, "First caption")
        self.assertEqual((parsed[1].start, parsed[1].end), (1.5, 2.0))

    def test_invalid_or_empty_confirmed_track_is_a_parse_failure(self) -> None:
        for payload in ("", "WEBVTT\n\nnot a cue\n", "00:00:02.000 --> 00:00:01.000\nBackwards"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_subtitle_text(payload, "vtt", source_language="en")

    def test_json3_autocaption_cues_use_provider_segment_timestamps(self) -> None:
        payload = '{"events":[{"tStartMs":1250,"dDurationMs":750,"segs":[{"utf8":"Hello "},{"utf8":"world"}]}]}'
        cues = parse_subtitle_text(payload, "json3", source_language="en")
        self.assertEqual((cues[0].start, cues[0].end, cues[0].text), (1.25, 2.0, "Hello world"))

    def test_ttml_cues_accept_clock_and_offset_timestamps(self) -> None:
        payload = '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p begin="00:00:01.200" dur="1.5s">Hi <span>there</span></p></div></body></tt>'
        cues = parse_subtitle_text(payload, "ttml", source_language="en")
        self.assertEqual((cues[0].start, cues[0].end, cues[0].text), (1.2, 2.7, "Hi there"))


if __name__ == "__main__":
    unittest.main()
