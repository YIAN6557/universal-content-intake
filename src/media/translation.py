"""Cue-preserving boundary for the independent Apple Translation CLI helper."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.media.subtitles import Cue, normalize_language_code


class TranslationProtocolError(ValueError):
    """The Translation helper changed cue identity or returned malformed output."""


def make_translation_request(
    cues: Sequence[Cue],
    *,
    source_language: str,
    target_language: str = "zh-Hans",
) -> dict[str, Any]:
    source = normalize_language_code(source_language)
    target = normalize_language_code(target_language)
    if source is None or target != "zh-Hans":
        raise TranslationProtocolError("translation requires a known source language and zh-Hans target")
    return {
        "schema_version": 1,
        "source_language": source,
        "target_language": "zh-Hans",
        "cues": [
            {
                "cue_id": cue.cue_id,
                "start": cue.start,
                "end": cue.end,
                "source_text": cue.text,
                "source_language": source,
                "target_language": "zh-Hans",
            }
            for cue in cues
        ],
    }


def apply_translation_response(
    source_cues: Sequence[Cue],
    response: Mapping[str, Any],
    *,
    target_language: str = "zh-Hans",
) -> tuple[Cue, ...]:
    target = normalize_language_code(target_language)
    if target != "zh-Hans" or response.get("target_language") != "zh-Hans":
        raise TranslationProtocolError("Translation helper returned the wrong target language")
    if int(response.get("schema_version", 1)) != 1:
        raise TranslationProtocolError("Translation helper returned an unsupported schema version")
    expected_source = normalize_language_code(source_cues[0].source_language) if source_cues else None
    response_source = normalize_language_code(str(response.get("source_language") or ""))
    if expected_source is None or response_source != expected_source:
        raise TranslationProtocolError("Translation helper changed or omitted the source language")
    raw_cues = response.get("cues")
    if not isinstance(raw_cues, list) or len(raw_cues) != len(source_cues):
        raise TranslationProtocolError("Translation helper changed the number of cues")
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in raw_cues:
        if not isinstance(item, Mapping):
            raise TranslationProtocolError("Translation helper returned a malformed cue")
        cue_id = item.get("cue_id")
        if not isinstance(cue_id, str) or cue_id in by_id:
            raise TranslationProtocolError("Translation helper returned a missing or duplicate cue identity")
        by_id[cue_id] = item
    expected_ids = {cue.cue_id for cue in source_cues}
    if set(by_id) != expected_ids:
        raise TranslationProtocolError("Translation helper changed the cue identity set")
    translated: list[Cue] = []
    for cue in source_cues:
        item = by_id[cue.cue_id]
        if (
            item.get("start") != cue.start
            or item.get("end") != cue.end
            or item.get("source_text") != cue.text
            or normalize_language_code(str(item.get("source_language") or "")) != expected_source
            or item.get("target_language") != "zh-Hans"
        ):
            raise TranslationProtocolError("Translation helper altered cue identity, timestamps, text, or languages")
        translated_text = item.get("translated_text")
        if not isinstance(translated_text, str) or not translated_text.strip():
            raise TranslationProtocolError("Translation helper returned an empty translation")
        translated.append(Cue(
            cue.cue_id,
            cue.start,
            cue.end,
            cue.text,
            expected_source,
            translated_text,
        ))
    return tuple(translated)
