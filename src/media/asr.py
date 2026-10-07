"""whisper.cpp Silero VAD gating and cue-level multilingual transcription."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.media.stage3_workspace import sha256_file
from src.media.subtitles import Cue, normalize_language_code


_VAD_SEGMENT = re.compile(
    r"Speech segment\s+\d+\s*:\s*start\s*=\s*(\d+(?:\.\d+)?)\s*,\s*end\s*=\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})$")


@dataclass(frozen=True)
class SpeechSegment:
    start: float
    end: float


@dataclass(frozen=True)
class VADResult:
    speech_detected: bool
    speech_duration_seconds: float
    speech_segments: tuple[SpeechSegment, ...]
    engine: str
    model_path: str
    model_sha256: str
    threshold: float
    minimum_speech_duration_ms: int
    minimum_silence_duration_ms: int
    result_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "speech_detected": self.speech_detected,
            "speech_duration_seconds": round(self.speech_duration_seconds, 3),
            "speech_segments": [{"start": item.start, "end": item.end} for item in self.speech_segments],
            "engine": self.engine,
            "model_path": self.model_path,
            "model_sha256": self.model_sha256,
            "threshold": self.threshold,
            "minimum_speech_duration_ms": self.minimum_speech_duration_ms,
            "minimum_silence_duration_ms": self.minimum_silence_duration_ms,
            "result_status": self.result_status,
        }


class ASRRuntimeError(RuntimeError):
    pass


class ASRLanguageUndeterminedError(ASRRuntimeError):
    pass


def _runtime_environment(binary: Path) -> dict[str, str]:
    library_dir = str(binary.resolve().parent.parent / "lib")
    environment = dict(os.environ)
    environment["DYLD_LIBRARY_PATH"] = library_dir
    return environment


def parse_vad_output(text: str) -> tuple[SpeechSegment, ...]:
    segments: list[SpeechSegment] = []
    for start_value, end_value in _VAD_SEGMENT.findall(text):
        # whisper-vad-speech-segments prints centiseconds (documented as of v1.9.4; whisper_self_test catches a change).
        start, end = float(start_value) / 100, float(end_value) / 100
        if start >= 0 and end > start:
            segments.append(SpeechSegment(start, end))
    return tuple(segments)


def run_silero_vad(
    audio_path: Path,
    *,
    vad_binary: Path,
    vad_model: Path,
    threshold: float = 0.5,
    minimum_speech_duration_ms: int = 250,
    minimum_silence_duration_ms: int = 100,
    timeout_seconds: int = 900,
) -> VADResult:
    if not audio_path.is_file() or not vad_model.is_file() or not vad_binary.is_file():
        raise ASRRuntimeError("VAD input, binary, or model is missing")
    command = [
        str(vad_binary), "--vad-model", str(vad_model), "--file", str(audio_path),
        "--vad-threshold", str(threshold),
        "--vad-min-speech-duration-ms", str(minimum_speech_duration_ms),
        "--vad-min-silence-duration-ms", str(minimum_silence_duration_ms),
        "--no-prints",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout_seconds, env=_runtime_environment(vad_binary))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ASRRuntimeError("Silero VAD could not execute") from error
    if result.returncode != 0:
        raise ASRRuntimeError(f"Silero VAD failed with exit code {result.returncode}")
    segments = parse_vad_output(f"{result.stdout}\n{result.stderr}")
    duration = sum(segment.end - segment.start for segment in segments)
    return VADResult(
        speech_detected=bool(segments),
        speech_duration_seconds=duration,
        speech_segments=segments,
        engine="whisper.cpp vad-speech-segments / Silero VAD",
        model_path=str(vad_model),
        model_sha256=sha256_file(vad_model),
        threshold=threshold,
        minimum_speech_duration_ms=minimum_speech_duration_ms,
        minimum_silence_duration_ms=minimum_silence_duration_ms,
        result_status="speech_detected" if segments else "no_speech",
    )


def _timestamp_seconds(value: str) -> float:
    match = _CLOCK.fullmatch(value.strip())
    if not match:
        raise ValueError("unknown whisper.cpp cue timestamp")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int((match.group(4) + "000")[:3])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def parse_whisper_json(document: Mapping[str, Any], *, source_language: str | None = None) -> tuple[str | None, tuple[Cue, ...]]:
    """Read whisper.cpp JSON cue segments; never consumes token/word timestamps."""
    result = document.get("result")
    detected: str | None = None
    if isinstance(result, Mapping):
        raw_language = result.get("language") or result.get("language_code")
        detected = normalize_language_code(str(raw_language)) if raw_language else None
    if not detected and source_language:
        detected = normalize_language_code(source_language)
    raw_segments = document.get("transcription")
    if not isinstance(raw_segments, list):
        raise ASRRuntimeError("whisper.cpp JSON is missing cue-level transcription segments")
    cues: list[Cue] = []
    for raw in raw_segments:
        if not isinstance(raw, Mapping):
            continue
        times = raw.get("timestamps")
        if not isinstance(times, Mapping):
            continue
        try:
            start = _timestamp_seconds(str(times["from"]))
            end = _timestamp_seconds(str(times["to"]))
        except (KeyError, TypeError, ValueError):
            continue
        text = str(raw.get("text") or "").strip()
        if text and end > start:
            cues.append(Cue(f"cue-{len(cues) + 1:06d}", start, end, text, detected or "und"))
    if not cues:
        raise ASRRuntimeError("whisper.cpp returned no valid cue-level segments")
    if detected is None:
        candidate = normalize_language_code(cues[0].source_language)
        detected = None if candidate == "und" else candidate
    return detected, tuple(cues)


def transcribe_with_whisper_cpp(
    audio_path: Path,
    *,
    whisper_binary: Path,
    model_path: Path,
    output_base: Path,
    source_language_hint: str | None = None,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    if not audio_path.is_file() or not model_path.is_file() or not whisper_binary.is_file():
        raise ASRRuntimeError("ASR input, binary, or model is missing")
    command = [
        str(whisper_binary), "--model", str(model_path), "--file", str(audio_path),
        "--language", source_language_hint or "auto", "--output-json", "--output-file", str(output_base),
        "--no-prints", "--no-gpu",
    ]
    try:
        process = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout_seconds, env=_runtime_environment(whisper_binary))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ASRRuntimeError("whisper.cpp could not execute") from error
    if process.returncode != 0:
        raise ASRRuntimeError(f"whisper.cpp failed with exit code {process.returncode}")
    json_path = output_base.with_suffix(".json")
    try:
        document = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ASRRuntimeError("whisper.cpp transcript JSON is missing or invalid") from error
    if not isinstance(document, Mapping):
        raise ASRRuntimeError("whisper.cpp transcript JSON must contain an object")
    detected, cues = parse_whisper_json(document, source_language=source_language_hint)
    if detected in (None, "und"):
        raise ASRLanguageUndeterminedError("whisper.cpp could not reliably detect a transcript language")
    return {
        "detected_language": detected,
        "language_confidence": None,
        "cues": [cue.to_dict() for cue in cues],
        "engine": "whisper.cpp",
        "model_path": str(model_path),
        "model_sha256": sha256_file(model_path),
        "invocation": {
            "language": source_language_hint or "auto",
            "output_format": "json",
            "timestamps": "cue_segments",
            "vad_timestamp_mapping": False,
            "backend": "cpu_no_gpu",
        },
        "raw_json_path": str(json_path),
    }
