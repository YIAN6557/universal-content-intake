"""Deterministic ASS subtitle layout and ffmpeg hard-subtitle rendering."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from src.media.ffmpeg_tools import MediaToolError, probe_media, validate_rendered_video
from src.media.layout import calculate_vertical_layout, layout_cue
from src.media.subtitles import Cue


FONT_FAMILY = "Noto Sans CJK SC Medium"
FONT_POINT = "PingFang SC Medium (not installed; canonical fallback selected)"


class BurnInError(RuntimeError):
    pass


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, rem = divmod(centiseconds, 360000)
    minutes, rem = divmod(rem, 6000)
    whole, fraction = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def _ass_text(value: str) -> str:
    value = value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\N")


def _playres(width: int, height: int, fontsize: float) -> str:
    outline = max(1.0, round(fontsize * 0.055, 2))
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nScaledBorderAndShadow: yes\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT_FAMILY},{fontsize:.2f},&H00FFFFFF,&H00FFFFFF,&H00000000,&HFF000000,0,0,0,0,100,100,0,0,1,{outline:.2f},0,2,5,5,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def build_ass_document(
    original_cues: Sequence[Cue] | None,
    chinese_cues: Sequence[Cue],
    *,
    frame_width: int,
    frame_height: int,
) -> tuple[str, dict[str, object]]:
    if not chinese_cues or frame_width <= 0 or frame_height <= 0:
        raise BurnInError("subtitle cues and source frame dimensions are required")
    bilingual = bool(original_cues)
    if bilingual and len(original_cues or ()) != len(chinese_cues):
        raise BurnInError("original and Chinese cue counts differ")
    lines: list[str] = []
    audit: dict[str, object] = {"font": FONT_FAMILY, "requested_font": FONT_POINT, "cues": [], "delivery": "hard_subtitle_burn_in"}
    font_size = min(frame_width, frame_height) * 0.045
    lines.append(_playres(frame_width, frame_height, font_size))
    layout_facts: list[dict[str, object]] = []
    for index, chinese in enumerate(chinese_cues):
        original = (original_cues or ())[index] if bilingual else None
        if original and (original.cue_id != chinese.cue_id or original.start != chinese.start or original.end != chinese.end):
            raise BurnInError("bilingual cue identity or timestamps do not align")
        original_text = original.text if original else None
        chinese_text = chinese.translated_text or chinese.text
        original_layout = layout_cue(original_text, frame_width=frame_width, frame_height=frame_height) if original_text else None
        chinese_layout = layout_cue(chinese_text, frame_width=frame_width, frame_height=frame_height)
        cue_font_size = max(chinese_layout.font_size, original_layout.font_size if original_layout else 0)
        vertical = calculate_vertical_layout(
            frame_height=frame_height,
            original_line_count=original_layout.line_count if original_layout else 0,
            chinese_line_count=chinese_layout.line_count,
            font_size=cue_font_size,
        )
        if original_layout and "original" in vertical.order:
            original_margin = vertical.original_bottom_margin_px or vertical.zh_bottom_margin_px
            chinese_margin = vertical.zh_bottom_margin_px
        elif original_layout:
            original_margin = vertical.original_bottom_margin_px or round(frame_height * 0.05)
            chinese_margin = vertical.zh_bottom_margin_px
        else:
            original_margin = 0
            chinese_margin = vertical.zh_bottom_margin_px
        duration_start = _ass_time(chinese.start)
        duration_end = _ass_time(chinese.end)
        if original_layout and original is not None:
            original_text_lines = _ass_text("\n".join(original_layout.lines))
            original_text_lines = f"{{\\fs{original_layout.font_size:.2f}}}" + original_text_lines
            lines.append(
                f"Dialogue: 0,{duration_start},{duration_end},Default,,5,5,{original_margin},,{original_text_lines}\n"
            )
        chinese_text_lines = _ass_text("\n".join(chinese_layout.lines))
        chinese_text_lines = f"{{\\fs{chinese_layout.font_size:.2f}}}" + chinese_text_lines
        lines.append(
            f"Dialogue: 1,{duration_start},{duration_end},Default,,5,5,{chinese_margin},,{chinese_text_lines}\n"
        )
        layout_facts.append({
            "cue_id": chinese.cue_id,
            "start": chinese.start,
            "end": chinese.end,
            "original_line_count": original_layout.line_count if original_layout else 0,
            "chinese_line_count": chinese_layout.line_count,
            "font_size": cue_font_size,
            "vertical_order": list(vertical.order),
            "original_bottom_margin_px": original_margin if original_layout else None,
            "chinese_bottom_margin_px": chinese_margin,
            "text_truncated": False,
        })
    audit["cues"] = layout_facts
    return "".join(lines), audit


def _filter_path(path: Path) -> str:
    # Forward slashes on Windows too ("C\\:/Users/…"): ffmpeg's filter syntax treats backslashes as escapes.
    value = path.resolve().as_posix()
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def burn_in_subtitles(
    source: Path,
    ass_file: Path,
    output: Path,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    fonts_dir: Path,
) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    source_facts = probe_media(source, ffprobe=ffprobe)
    source_video = next((item for item in source_facts["streams"] if item.get("codec_type") == "video"), None)
    if not isinstance(source_video, dict):
        raise BurnInError("ffprobe found no source video stream")
    width, height = int(source_video.get("width", 0)), int(source_video.get("height", 0))
    if width <= 0 or height <= 0:
        raise BurnInError("source video dimensions are unavailable")
    subtitle_filter = f"subtitles=filename='{_filter_path(ass_file)}':fontsdir='{_filter_path(fonts_dir)}'"
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0",
        "-vf", subtitle_filter, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", str(output),
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=7200)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BurnInError("ffmpeg subtitle rendering could not execute") from error
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        safe_stderr = result.stderr[-1600:].replace("\n", " ")
        raise BurnInError(f"ffmpeg subtitle rendering failed (exit {result.returncode}): {safe_stderr}")
    rendered_facts = probe_media(output, ffprobe=ffprobe)
    try:
        verification = validate_rendered_video(source_facts, rendered_facts, expected_width=width, expected_height=height)
    except MediaToolError as error:
        raise BurnInError(str(error)) from error
    decode = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=7200,
    )
    if decode.returncode != 0:
        raise BurnInError("ffmpeg could not fully decode the rendered subtitle artifact")
    video_stream = next(item for item in rendered_facts["streams"] if item.get("codec_type") == "video")
    return {
        "ffprobe": verification,
        "render_video_codec": video_stream.get("codec_name"),
        "render_audio_codecs": verification["audio_codecs"],
        "source_audio_stream_copy": True,
        "stage3_subtitle_render_transcode": True,
        "decoded_readable": True,
    }
