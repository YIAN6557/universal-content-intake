"""Job-temp audio extraction and rendered-video ffprobe validation."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping


class MediaToolError(RuntimeError):
    pass


def probe_media(path: Path, *, ffprobe: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MediaToolError("media file is missing")
    command = [str(ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaToolError("ffprobe could not inspect the media artifact") from error
    if result.returncode != 0:
        raise MediaToolError("ffprobe rejected the media artifact")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MediaToolError("ffprobe returned malformed JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("streams"), list):
        raise MediaToolError("ffprobe result is missing streams")
    return value


def extract_asr_audio(
    source: Path,
    destination: Path,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-i", str(source),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "wav", str(destination),
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaToolError("ffmpeg could not prepare ASR input audio") from error
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 44:
        raise MediaToolError("ffmpeg failed to produce a usable PCM WAV file")
    facts = probe_media(destination, ffprobe=ffprobe)
    stream = next((item for item in facts["streams"] if isinstance(item, Mapping) and item.get("codec_type") == "audio"), None)
    if not isinstance(stream, Mapping) or stream.get("codec_name") != "pcm_s16le" or int(stream.get("sample_rate", 0)) != 16000 or int(stream.get("channels", 0)) != 1:
        raise MediaToolError("ASR WAV must be mono 16 kHz PCM signed 16-bit audio")
    return {"sample_rate": 16000, "channels": 1, "codec": "pcm_s16le", "container": "wav"}


def _ratio(value: str | None) -> Fraction | None:
    if not value or value == "N/A":
        return None
    try:
        if ":" in value:
            numerator, denominator = value.split(":", 1)
            return Fraction(int(numerator), int(denominator))
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None


def validate_rendered_video(
    source_facts: Mapping[str, Any],
    rendered_facts: Mapping[str, Any],
    *,
    expected_width: int,
    expected_height: int,
    duration_tolerance_seconds: float = 0.5,
) -> dict[str, Any]:
    source_streams = source_facts.get("streams")
    rendered_streams = rendered_facts.get("streams")
    source_video = next((item for item in source_streams or [] if isinstance(item, Mapping) and item.get("codec_type") == "video"), None)
    rendered_video = next((item for item in rendered_streams or [] if isinstance(item, Mapping) and item.get("codec_type") == "video"), None)
    source_audio = [item for item in source_streams or [] if isinstance(item, Mapping) and item.get("codec_type") == "audio"]
    rendered_audio = [item for item in rendered_streams or [] if isinstance(item, Mapping) and item.get("codec_type") == "audio"]
    if not isinstance(source_video, Mapping) or not isinstance(rendered_video, Mapping):
        raise MediaToolError("ffprobe did not find source and rendered video streams")
    if int(rendered_video.get("width", 0)) != expected_width or int(rendered_video.get("height", 0)) != expected_height:
        raise MediaToolError("subtitle render changed the source frame dimensions")
    if len(rendered_audio) < len(source_audio):
        raise MediaToolError("subtitle render lost one or more source audio streams")
    for before, after in zip(source_audio, rendered_audio):
        if before.get("codec_name") != after.get("codec_name"):
            raise MediaToolError("subtitle render unexpectedly changed an audio codec")
    before_sar, after_sar = _ratio(str(source_video.get("sample_aspect_ratio") or "")), _ratio(str(rendered_video.get("sample_aspect_ratio") or ""))
    before_dar, after_dar = _ratio(str(source_video.get("display_aspect_ratio") or "")), _ratio(str(rendered_video.get("display_aspect_ratio") or ""))
    if before_sar is not None and after_sar is not None and before_sar != after_sar:
        raise MediaToolError("subtitle render changed sample aspect ratio")
    if before_dar is not None and after_dar is not None and before_dar != after_dar:
        raise MediaToolError("subtitle render changed display aspect ratio")
    source_format, rendered_format = source_facts.get("format"), rendered_facts.get("format")
    if not isinstance(source_format, Mapping) or not isinstance(rendered_format, Mapping):
        raise MediaToolError("ffprobe is missing source or rendered container facts")
    names = str(rendered_format.get("format_name") or "").split(",")
    if not any(name in {"mp4", "mov"} for name in names):
        raise MediaToolError("subtitle render is not in the required MP4 container family")
    before_duration = float(source_format.get("duration", 0) or 0)
    after_duration = float(rendered_format.get("duration", 0) or 0)
    if before_duration <= 0 or after_duration <= 0 or abs(before_duration - after_duration) > duration_tolerance_seconds:
        raise MediaToolError("subtitle render changed or obscured the source duration")
    return {
        "container": "mp4",
        "video_stream": True,
        "audio_stream_count": len(rendered_audio),
        "width": expected_width,
        "height": expected_height,
        "duration_seconds": after_duration,
        "video_codec": rendered_video.get("codec_name"),
        "audio_codecs": [item.get("codec_name") for item in rendered_audio],
        "sample_aspect_ratio": rendered_video.get("sample_aspect_ratio"),
        "display_aspect_ratio": rendered_video.get("display_aspect_ratio"),
        "duration_delta_seconds": round(after_duration - before_duration, 6),
        "artifact_readable": True,
    }
