"""Subtitle track catalog, deterministic primary selection, and cue parsing."""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_TIME_LINE = re.compile(
    r"(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})(?:\s+.*)?$"
)
_TRACK_PART = re.compile(r"[^A-Za-z0-9_.-]+")
_TAG = re.compile(r"<[^>]*>")


def normalize_language_code(value: str | None) -> str | None:
    """Normalize common BCP-47 spellings without guessing language from text."""

    if not value or not value.strip():
        return None
    raw_parts = re.split(r"[-_]", value.strip())
    if not raw_parts or not raw_parts[0].isalpha():
        return None
    parts = [raw_parts[0].lower()]
    for part in raw_parts[1:]:
        if not part:
            return None
        if len(part) == 4 and part.isalpha():
            parts.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            parts.append(part.upper())
        else:
            parts.append(part.lower())
    return "-".join(parts)


def is_zh_hans_language(value: str | None) -> bool:
    """Return true only for a Simplified Chinese code/locale, not visible text."""

    normalized = normalize_language_code(value)
    if normalized is None or normalized.split("-", 1)[0] != "zh":
        return False
    subtags = normalized.split("-")[1:]
    if "Hant" in subtags:
        return False
    if "Hans" in subtags:
        return True
    # yt-dlp commonly labels Simplified Chinese as zh, zh-CN, or zh-SG.
    return not any(region in {"TW", "HK", "MO"} for region in subtags)


@dataclass(frozen=True)
class SubtitleTrack:
    language_code: str
    language_name: str | None
    source: str
    provider: str
    format: str
    track_identifier: str
    available_formats: tuple[str, ...] = ()
    file_path: str | None = None
    extraction_status: str = "pending"

    def __post_init__(self) -> None:
        if self.source not in {"manual", "automatic"}:
            raise ValueError("subtitle source must be manual or automatic")
        if not self.language_code or not self.provider or not self.format or not self.track_identifier:
            raise ValueError("subtitle track metadata must have a language, provider, format and identifier")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "language_code": self.language_code,
            "language_name": self.language_name,
            "source": self.source,
            "provider": self.provider,
            "format": self.format,
            "track_identifier": self.track_identifier,
            "available_formats": list(self.available_formats),
            "file_path": self.file_path,
            "extraction_status": self.extraction_status,
        }


@dataclass(frozen=True)
class PrimaryTrackDecision:
    track: SubtitleTrack
    reason: str
    tie_break: str


@dataclass(frozen=True)
class Cue:
    cue_id: str
    start: float
    end: float
    text: str
    source_language: str
    translated_text: str | None = None

    def __post_init__(self) -> None:
        if not self.cue_id or not self.source_language:
            raise ValueError("cue identity and source language are required")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("cue timestamps must be increasing and non-negative")
        if not self.text.strip():
            raise ValueError("cue text cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cue_id": self.cue_id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "source_language": self.source_language,
            "translated_text": self.translated_text,
        }


def _safe_track_part(value: str) -> str:
    cleaned = _TRACK_PART.sub("_", value).strip("._")
    return cleaned or "unknown"


def normalize_subtitle_tracks(
    manual: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    automatic: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *,
    provider: str = "yt-dlp",
) -> tuple[SubtitleTrack, ...]:
    """Normalize both subtitle catalogs while dropping signed download URLs."""

    tracks: dict[tuple[str, str], SubtitleTrack] = {}
    for source, catalog in (("manual", manual or {}), ("automatic", automatic or {})):
        if not isinstance(catalog, Mapping):
            continue
        for raw_language, raw_formats in catalog.items():
            language = normalize_language_code(str(raw_language))
            if language is None or not isinstance(raw_formats, Sequence) or isinstance(raw_formats, (str, bytes)):
                continue
            formats: set[str] = set()
            identifiers: set[str] = set()
            language_name: str | None = None
            for raw in raw_formats:
                if not isinstance(raw, Mapping):
                    continue
                ext = str(raw.get("ext") or raw.get("format") or "unknown").strip().lower()
                raw_id = str(raw.get("format_id") or raw.get("id") or ext)
                name = raw.get("language_name") or raw.get("display_name") or raw.get("name")
                if language_name is None and isinstance(name, str) and name.strip():
                    language_name = name.strip()
                formats.add(ext)
                identifiers.add(_safe_track_part(raw_id))
            if not formats:
                continue
            format_priority = {"vtt": 0, "srt": 1, "webvtt": 2, "json3": 3, "ttml": 4}
            available_formats = tuple(sorted(formats, key=lambda ext: (format_priority.get(ext, 10), ext)))
            stable_id = f"{source}:{language}:{sorted(identifiers)[0]}"
            track = SubtitleTrack(
                language, language_name, source, provider, available_formats[0], stable_id,
                available_formats=available_formats,
            )
            tracks[(source, language)] = track
    return tuple(sorted(tracks.values(), key=lambda track: (
        track.language_code.casefold(), track.source, track.track_identifier,
    )))


def _base_language(value: str) -> str:
    return value.split("-", 1)[0].casefold()


def exclude_machine_translated_tracks(
    tracks: Sequence[SubtitleTrack],
    original_language: str | None,
) -> tuple[tuple[SubtitleTrack, ...], tuple[SubtitleTrack, ...]]:
    """Drop provider machine translations of an automatic caption track.

    YouTube lists its speech-recognition track as ``<lang>-orig`` and then offers
    that same track machine-translated into every other language. Those
    translations are not subtitle tracks of the video, and fetching all of them
    (often 150+ requests) triggers HTTP 429. Manual tracks are always kept.
    Catalogs without an ``-orig`` automatic track are returned unchanged.
    """

    orig_bases = {
        _base_language(track.language_code)
        for track in tracks
        if track.source == "automatic" and track.language_code.casefold().endswith("-orig")
    }
    if not orig_bases:
        return tuple(tracks), ()
    allowed = set(orig_bases)
    normalized_original = normalize_language_code(original_language)
    if normalized_original:
        allowed.add(_base_language(normalized_original))
    kept: list[SubtitleTrack] = []
    excluded: list[SubtitleTrack] = []
    for track in tracks:
        if track.source == "manual" or _base_language(track.language_code) in allowed:
            kept.append(track)
        else:
            excluded.append(track)
    return tuple(kept), tuple(excluded)


def select_primary_track(
    tracks: Sequence[SubtitleTrack],
    original_language: str | None,
) -> PrimaryTrackDecision:
    """Choose a deterministic track; manual/automatic never change language priority."""

    if not tracks:
        raise ValueError("cannot select a primary track from an empty catalog")
    source = normalize_language_code(original_language)
    ranked: list[tuple[tuple[int, str, str, str, str], SubtitleTrack]] = []
    for track in tracks:
        language = normalize_language_code(track.language_code) or track.language_code
        if source and language == source:
            rank, reason = 0, "original_language_exact_match"
        elif source and language.split("-", 1)[0] == source.split("-", 1)[0]:
            rank, reason = 1, "original_language_family_match"
        else:
            rank, reason = 2, "provider_language_metadata"
        key = (rank, language.casefold(), track.format.casefold(), track.track_identifier)
        ranked.append((key, track))
    ranked.sort(key=lambda pair: pair[0])
    key, selected = ranked[0]
    reason = (
        "original_language_exact_match" if key[0] == 0 else
        "original_language_family_match" if key[0] == 1 else
        "provider_language_metadata"
    )
    return PrimaryTrackDecision(
        selected,
        reason,
        "language_code, format, track_identifier (source kind has equal priority)",
    )


def _timestamp_seconds(value: str) -> float:
    value = value.strip().replace(",", ".")
    clock, _, fraction = value.partition(".")
    parts = [int(part) for part in clock.split(":")]
    if len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("invalid subtitle timestamp")
    millis = int((fraction + "000")[:3]) if fraction else 0
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _ttml_time_seconds(value: str) -> float:
    value = value.strip()
    if re.fullmatch(r"(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?", value):
        normalized = value if "." in value else value + ".000"
        return _timestamp_seconds(normalized)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if not match:
        raise ValueError("unsupported TTML time expression")
    scalar = float(match.group(1))
    unit = match.group(2)
    return scalar / 1000 if unit == "ms" else scalar * {"s": 1, "m": 60, "h": 3600}[unit]


def _clean_caption_text(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = _TAG.sub("", value)
    lines = [html.unescape(line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_subtitle_text(payload: str, format_name: str, *, source_language: str) -> tuple[Cue, ...]:
    """Parse SRT/WebVTT cues using cue-level time ranges only."""

    format_name = format_name.lower().lstrip(".")
    if format_name == "ttml":
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as error:
            raise ValueError("invalid TTML subtitle track") from error
        cues: list[Cue] = []
        for paragraph in (node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "p"):
            begin = paragraph.attrib.get("begin")
            end = paragraph.attrib.get("end")
            duration = paragraph.attrib.get("dur")
            if not begin or not (end or duration):
                continue
            try:
                start = _ttml_time_seconds(begin)
                finish = _ttml_time_seconds(end) if end else start + _ttml_time_seconds(str(duration))
            except (TypeError, ValueError):
                continue
            text_value = _clean_caption_text("".join(paragraph.itertext()))
            if text_value and finish > start:
                cues.append(Cue(f"cue-{len(cues) + 1:06d}", start, finish, text_value, source_language))
        if not cues:
            raise ValueError("TTML subtitle track contained no valid cues")
        return tuple(cues)
    if format_name == "json3":
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("invalid json3 subtitle track") from error
        events = document.get("events") if isinstance(document, Mapping) else None
        cues: list[Cue] = []
        if not isinstance(events, list):
            raise ValueError("json3 subtitle track has no events array")
        for event in events:
            if not isinstance(event, Mapping):
                continue
            try:
                start_ms = int(event["tStartMs"])
                duration_ms = int(event["dDurationMs"])
            except (KeyError, TypeError, ValueError):
                continue
            segments = event.get("segs")
            if start_ms < 0 or duration_ms <= 0 or not isinstance(segments, list):
                continue
            text_value = _clean_caption_text("".join(
                str(segment.get("utf8", ""))
                for segment in segments
                if isinstance(segment, Mapping)
            ))
            if text_value:
                cues.append(Cue(
                    f"cue-{len(cues) + 1:06d}",
                    start_ms / 1000,
                    (start_ms + duration_ms) / 1000,
                    text_value,
                    source_language,
                ))
        if not cues:
            raise ValueError("json3 subtitle track contained no valid cues")
        return tuple(cues)
    if format_name not in {"vtt", "srt", "webvtt"}:
        raise ValueError(f"unsupported subtitle format: {format_name}")
    normalized = payload.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", normalized)
    cues: list[Cue] = []
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        if lines[0].strip().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((index for index, line in enumerate(lines) if _TIME_LINE.match(line.strip())), None)
        if timing_index is None:
            continue
        match = _TIME_LINE.match(lines[timing_index].strip())
        assert match is not None
        start = _timestamp_seconds(match.group("start"))
        end = _timestamp_seconds(match.group("end"))
        text_value = _clean_caption_text("\n".join(lines[timing_index + 1:]))
        if not text_value:
            continue
        cues.append(Cue(f"cue-{len(cues) + 1:06d}", start, end, text_value, source_language))
    if not cues:
        raise ValueError("subtitle track contained no valid cues")
    return tuple(cues)
