from __future__ import annotations

import unittest

from src.media.ffmpeg_tools import MediaToolError, validate_rendered_video
from src.media.render import BurnInError, build_ass_document
from src.media.subtitles import Cue


def video_facts(*, width: int = 1280, height: int = 720, duration: float = 10.0, video_codec: str = "h264", audio_codec: str = "aac") -> dict[str, object]:
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": str(duration)},
        "streams": [
            {"codec_type": "video", "codec_name": video_codec, "width": width, "height": height, "sample_aspect_ratio": "1:1", "display_aspect_ratio": f"{width}:{height}"},
            {"codec_type": "audio", "codec_name": audio_codec},
        ],
    }


class Stage3RenderTests(unittest.TestCase):
    def test_ass_document_preserves_bilingual_text_and_contract_font_outline_and_alignment(self) -> None:
        original = Cue("cue-1", 1.25, 3.5, "A complete original caption stays visible.", "en")
        chinese = Cue("cue-1", 1.25, 3.5, original.text, "en", "完整保留原文并显示中文字幕。")
        ass, audit = build_ass_document((original,), (chinese,), frame_width=1280, frame_height=720)
        self.assertIn("Noto Sans CJK SC Medium", ass)
        self.assertIn("&H00FFFFFF", ass)
        self.assertIn("&H00000000", ass)
        self.assertIn(",1.78,0,2,5,5,0,1", ass)
        self.assertIn("A complete original caption stays visible.", ass)
        self.assertIn("完整保留原文并显示中文字幕。", ass)
        self.assertEqual(audit["delivery"], "hard_subtitle_burn_in")
        cue_facts = audit["cues"][0]
        self.assertEqual(cue_facts["vertical_order"], ["original", "zh-Hans"])
        self.assertFalse(cue_facts["text_truncated"])

    def test_ass_three_line_fallback_keeps_every_character(self) -> None:
        source = "This long but finite Latin caption keeps every original word visible while it wraps deterministically across the subtitle-safe width."
        cue = Cue("cue-2", 0, 5, source, "en", "这是完整保留的中文翻译文本。")
        ass, audit = build_ass_document((Cue("cue-2", 0, 5, source, "en"),), (cue,), frame_width=320, frame_height=180)
        layout = audit["cues"][0]
        self.assertIn(source.split()[0], ass)
        self.assertIn(r"\N", ass)
        self.assertLessEqual(layout["original_line_count"], 3)
        self.assertFalse(layout["text_truncated"])

    def test_bilingual_identity_mismatch_fails_instead_of_misaligning_captions(self) -> None:
        original = Cue("source", 1, 2, "Hello", "en")
        translated = Cue("other", 1, 2, "你好", "zh-Hans", "你好")
        with self.assertRaises(BurnInError):
            build_ass_document((original,), (translated,), frame_width=640, frame_height=360)

    def test_ffprobe_validation_accepts_same_geometry_duration_and_streams(self) -> None:
        result = validate_rendered_video(video_facts(), video_facts(video_codec="h264"), expected_width=1280, expected_height=720)
        self.assertTrue(result["artifact_readable"])
        self.assertEqual(result["audio_codecs"], ["aac"])
        self.assertEqual(result["duration_delta_seconds"], 0.0)

    def test_ffprobe_validation_rejects_crop_duration_change_audio_loss_and_codec_change(self) -> None:
        source = video_facts()
        bad_outputs = (
            video_facts(width=640, height=720),
            video_facts(duration=11.0),
            {"format": video_facts()["format"], "streams": [video_facts()["streams"][0]]},
            video_facts(audio_codec="opus"),
        )
        for output in bad_outputs:
            with self.subTest(output=output), self.assertRaises(MediaToolError):
                validate_rendered_video(source, output, expected_width=1280, expected_height=720)


if __name__ == "__main__":
    unittest.main()
