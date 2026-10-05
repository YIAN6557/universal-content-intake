from __future__ import annotations

import unittest

from src.media.layout import (
    SubtitleLayoutError,
    calculate_vertical_layout,
    layout_cue,
)


class SubtitleLayoutTests(unittest.TestCase):
    def test_short_cue_stays_one_line_at_canonical_base_size(self) -> None:
        result = layout_cue("你好，世界", frame_width=1280, frame_height=720)
        self.assertEqual(result.lines, ("你好，世界",))
        self.assertEqual(result.font_size, 32.4)
        self.assertEqual(result.line_count, 1)

    def test_long_cue_wraps_to_two_lines_without_truncation(self) -> None:
        text = "這是一段很長的中文字幕，需要完整保留並按確定性的寬度規則分成兩行顯示，而且不能在任何情況下截斷字幕內容。"
        result = layout_cue(text, frame_width=640, frame_height=360)
        self.assertEqual(result.line_count, 2)
        self.assertEqual("".join(result.lines), text)
        self.assertLessEqual(result.font_size, 16.2)

    def test_longest_layout_shrinks_slightly_then_allows_three_lines(self) -> None:
        text = "This long but finite Latin caption keeps every original word visible while it wraps deterministically across the subtitle-safe width."
        result = layout_cue(text, frame_width=320, frame_height=180)
        self.assertIn(result.line_count, (2, 3))
        self.assertEqual(" ".join(" ".join(result.lines).split()), text)
        self.assertLessEqual(result.font_size, 8.1)

    def test_unrenderable_extreme_text_fails_visibly_instead_of_truncating(self) -> None:
        with self.assertRaises(SubtitleLayoutError):
            layout_cue("字" * 2000, frame_width=320, frame_height=180)

    def test_original_and_chinese_default_to_original_above_chinese_then_swap_when_band_is_too_tall(self) -> None:
        default = calculate_vertical_layout(frame_height=720, original_line_count=1, chinese_line_count=1)
        self.assertEqual(default.order, ("original", "zh-Hans"))
        self.assertGreaterEqual(default.zh_bottom_margin_px, 36)
        self.assertGreater(default.original_bottom_margin_px, default.zh_bottom_margin_px)
        constrained = calculate_vertical_layout(frame_height=180, original_line_count=3, chinese_line_count=3)
        self.assertEqual(constrained.order, ("zh-Hans", "original"))

    def test_single_language_uses_seven_percent_bottom_safe_margin(self) -> None:
        result = calculate_vertical_layout(frame_height=720, original_line_count=0, chinese_line_count=1)
        self.assertEqual(result.order, ("zh-Hans",))
        self.assertEqual(result.zh_bottom_margin_px, 50)


if __name__ == "__main__":
    unittest.main()
