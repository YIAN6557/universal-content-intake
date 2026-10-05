"""Publishing assist for VIDEO deliverables.

Produces a Simplified Chinese publish title (at most 30 characters), a
publishing copy, and optional hashtags for re-posting the video on Chinese
platforms:

1. When Claude or Gemini is set up (``src/media/publish_writer.py``), it writes all three
   from the original title, author, description, tags and the translated
   subtitles.
2. Otherwise the rule-based path translates the cleaned original title with the
   local Apple Translation helper and builds the copy from the creator's own
   description (links, promos and timestamps removed), falling back to the
   opening Chinese subtitles only when there is no usable description.

Title length rules: <= 30 characters used as is; 31-40 compressed by dropping
decorations and trailing clauses; longer ones regenerated from the leading
clause or the first subtitle sentence. Nothing here may block delivery: every
failure yields an empty field and a recorded reason.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.media.subtitles import Cue, is_zh_hans_language, normalize_language_code
from src.media.translation import TranslationProtocolError, apply_translation_response, make_translation_request
from src.media.translation_runtime import TranslationRuntimeError, translate_cues, translation_preflight

TITLE_LIMIT = 30
COMPRESS_LIMIT = 40
COPY_LIMIT = 150
SCHEMA_VERSION = 2
DESCRIPTION_SOURCE_LIMIT = 600
SUBTITLE_EXCERPT_LIMIT = 1500

_BRACKETS = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)|「[^」]*」|『[^』]*』|《[^》]*》(?=\s*$)")
_HASHTAG = re.compile(r"#\S+")
_SEPARATORS = re.compile(r"\s*(?:[｜|—–]+|\s-\s|[:：;；,，!！?？·•])\s*")
_SENTENCE_END = re.compile(r"(?<=[。！？!?])")
_TRAILING_PUNCT = "，,、;；:：-—–|｜·•~～ "

TitleTranslator = Callable[[str, str], str | None]


def char_count(text: str) -> int:
    """Characters a viewer sees, ignoring whitespace."""

    return len(re.sub(r"\s+", "", text))


def _strip_emoji(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) not in {"So", "Sk", "Cs"} and not 0xFE00 <= ord(ch) <= 0xFE0F)


def clean_title(text: str) -> str:
    text = _HASHTAG.sub(" ", _strip_emoji(text))
    text = _BRACKETS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(_TRAILING_PUNCT + "。.!！?？")


def hard_cut(text: str, limit: int) -> str:
    """Cut to ``limit`` visible characters without splitting a Latin word."""

    kept: list[str] = []
    count = 0
    for character in text:
        if not character.isspace():
            if count == limit:
                break
            count += 1
        kept.append(character)
    result = "".join(kept)
    rest = text[len(result):]
    if rest and re.match(r"[A-Za-z0-9]", rest) and re.search(r"[A-Za-z0-9]$", result):
        boundary = re.search(r"^(.*[^A-Za-z0-9])[A-Za-z0-9]+$", result)
        if boundary and char_count(boundary.group(1)) >= limit // 2:
            result = boundary.group(1)
    return result.strip(_TRAILING_PUNCT)


def _segments(text: str) -> list[str]:
    return [part for part in _SEPARATORS.split(text) if part and part.strip()]


def compress_title(text: str, limit: int = TITLE_LIMIT) -> str:
    cleaned = clean_title(text)
    if char_count(cleaned) <= limit:
        return cleaned
    kept = ""
    for segment in _segments(cleaned):
        candidate = f"{kept}，{segment}" if kept else segment
        if char_count(candidate) > limit:
            break
        kept = candidate
    return kept if kept else hard_cut(cleaned, limit)


def _first_sentence(chinese_cues: Sequence[str], limit: int) -> str:
    for sentence in _sentences(chinese_cues):
        sentence = clean_title(sentence)
        if 4 <= char_count(sentence) <= limit:
            return sentence
    return ""


def regenerate_title(text: str, chinese_cues: Sequence[str], limit: int = TITLE_LIMIT) -> str:
    cleaned = clean_title(text)
    segments = _segments(cleaned)
    if segments and 4 <= char_count(segments[0]) <= limit:
        return segments[0]
    from_subtitles = _first_sentence(chinese_cues, limit)
    if from_subtitles:
        return from_subtitles
    return hard_cut(cleaned, limit)


def fit_title(chinese_title: str, chinese_cues: Sequence[str] = ()) -> tuple[str, str]:
    """Apply the <=30 / 31-40 / >40 character rules."""

    title = re.sub(r"\s+", " ", chinese_title).strip()
    length = char_count(title)
    if not title:
        fallback = _first_sentence(chinese_cues, TITLE_LIMIT)
        return (fallback, "generated_from_subtitles") if fallback else ("", "unavailable")
    if length <= TITLE_LIMIT:
        return title, "direct"
    if length <= COMPRESS_LIMIT:
        return compress_title(title), "compressed"
    return regenerate_title(title, chinese_cues), "regenerated"


_ANNOTATION = re.compile(r"\[[^\]]*\]|【[^】]*】|（(?:音乐|掌声|笑声|笑|鼓掌|唱歌|歌声|欢呼)[^）]*）|[♪♫]")
_REPEAT = re.compile(r"(.{4,}?)\1+")


def _normalize_cue(text: str) -> str:
    """Drop caption annotations ([音乐], ♪), line breaks and stuttered repeats."""

    text = _ANNOTATION.sub("", text)
    text = re.sub(r"\s*\n\s*", "", text).strip()
    text = _REPEAT.sub(r"\1", text)
    return text.strip(" ，,、")


def _dedupe_clauses(text: str) -> str:
    """Keep each clause once; a clause that only starts the next one is dropped.

    Song lyrics and stuttered speech repeat the same clause across cues.
    """

    kept: list[list[str]] = []
    seen: set[str] = set()
    for token in re.findall(r"[^，。！？!?]+[，。！？!?]?", text):
        body = token.rstrip("，。！？!?").strip()
        punct = token[len(token.rstrip("，。！？!?")):] or "，"
        if not body or body in seen:
            continue
        seen.add(body)
        if kept and body.startswith(kept[-1][0]):
            kept[-1] = [body, punct]
        elif kept and kept[-1][0].startswith(body):
            continue
        else:
            kept.append([body, punct])
    if not kept:
        return ""
    if kept[-1][1] == "，":
        kept[-1][1] = "。"
    return "".join(body + punct for body, punct in kept)


def _sentences(chinese_cues: Sequence[str]) -> list[str]:
    pieces: list[str] = []
    for raw in chinese_cues:
        text = _normalize_cue(raw or "")
        if not text or (pieces and pieces[-1].rstrip("，。！？!?") == text.rstrip("，。！？!?")):
            continue
        pieces.append(text if text[-1] in "。！？!?，" else text + "，")
    joined = _dedupe_clauses("".join(pieces))
    parts = [part.strip() for part in _SENTENCE_END.split(joined)]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)
    return result


def build_copy(chinese_cues: Sequence[str], *, title: str = "", limit: int = COPY_LIMIT) -> tuple[str, str]:
    """Extract a short Chinese blurb from the opening subtitles."""

    text = ""
    for sentence in _sentences(chinese_cues):
        if char_count(sentence) < 4:
            continue
        candidate = text + sentence
        if char_count(candidate) > limit:
            if not text:
                text = hard_cut(sentence, limit - 1) + "…"
            break
        text = candidate
    if text:
        return text, "subtitle_extract"
    if title:
        return title, "title_only"
    return "", "unavailable"


def apple_title_translator(helper: Path, work_dir: Path) -> TitleTranslator:
    """Translate one title with the Stage 3 Apple Translation helper."""

    def translate(title: str, source_language: str) -> str | None:
        preflight = translation_preflight(helper, source_language=source_language, target_language="zh-Hans")
        if preflight.get("status") != "ready":
            return None
        cue = Cue("title", 0.0, 1.0, title, source_language)
        work_dir.mkdir(parents=True, exist_ok=True)
        request_path = work_dir / "title-request.json"
        response_path = work_dir / "title-response.json"
        request_path.write_text(json.dumps(make_translation_request([cue], source_language=source_language), ensure_ascii=False), encoding="utf-8")
        outcome = translate_cues(helper, request_path=request_path, response_path=response_path, source_language=source_language)
        if outcome.get("status") != "success":
            return None
        translated = apply_translation_response([cue], outcome)
        return (translated[0].translated_text or "").strip() or None

    return translate


def _chinese_cue_texts(temp_dir: Path, stage3: Mapping[str, Any]) -> list[str]:
    candidates: list[Path] = sorted(temp_dir.glob("stage3/translation/source-*/translated-cues.json"))
    translation = stage3.get("translation") if isinstance(stage3.get("translation"), Mapping) else {}
    subtitle = stage3.get("subtitle_discovery") if isinstance(stage3.get("subtitle_discovery"), Mapping) else {}
    if translation.get("status") == "skipped_source_is_zh_hans" and isinstance(subtitle.get("primary_cues_path"), str):
        candidates.append(temp_dir / subtitle["primary_cues_path"])
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cues = value.get("cues") if isinstance(value, Mapping) else value
        if not isinstance(cues, list):
            continue
        texts = [str(cue.get("translated_text") or cue.get("text") or "") for cue in cues if isinstance(cue, Mapping)]
        if any(texts):
            return texts
    return []


_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|ly|me|gg|co|tv)/", re.IGNORECASE)
_TIMESTAMP = re.compile(r"^\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s")
_PROMO = re.compile(
    r"subscribe|follow (?:us|me)|sponsor|affiliate|newsletter|patreon|merch|discount|promo code|use code|"
    r"instagram|tiktok|twitter|facebook|threads|linkedin|discord|listen (?:to|on)|available (?:on|wherever)|"
    r"all rights reserved|©|credits?:|music:|#\w",
    re.IGNORECASE,
)


def description_lead(description: str, limit: int = DESCRIPTION_SOURCE_LIMIT) -> str:
    """The creator's own summary: leading prose lines without links, promos or chapters."""

    kept: list[str] = []
    for raw in (description or "").splitlines():
        line = _strip_emoji(raw).strip()
        if not line:
            if kept:
                break  # the first paragraph is the summary; later ones are usually promos
            continue
        if _URL.search(line) or _TIMESTAMP.match(line) or _PROMO.search(line) or len(line) < 15:
            if kept:
                break
            continue
        kept.append(line)
        if sum(len(item) for item in kept) >= limit:
            break
    text = " ".join(kept)
    if len(text) > limit:
        cut = max(text.rfind(mark, 0, limit) for mark in (". ", "! ", "? "))
        text = text[:cut + 1] if cut > limit // 3 else text[:limit]
    return text.strip()


def strip_series_suffix(title: str, author: str = "") -> str:
    """Drop trailing ``| Show name`` / ``- Channel`` segments from a source title."""

    parts = [part.strip() for part in re.split(r"\s+[|｜]\s+", title) if part.strip()]
    if len(parts) > 1:
        return parts[0]
    if author and title.lower().endswith(author.lower()):
        trimmed = re.sub(r"\s*[-–—:]\s*$", "", title[: -len(author)])
        return trimmed.strip() or title
    return title


def _copy_from_description(text: str, limit: int = COPY_LIMIT) -> str:
    sentences = [part.strip() for part in _SENTENCE_END.split(re.sub(r"\s+", " ", text)) if part.strip()]
    copy = ""
    for sentence in sentences:
        if char_count(copy + sentence) > limit:
            break
        copy += sentence
    if not copy and sentences:
        copy = hard_cut(sentences[0], limit - 1) + "…"
    return copy


def _subtitle_excerpt(chinese_cues: Sequence[str], limit: int = SUBTITLE_EXCERPT_LIMIT) -> str:
    text = ""
    for sentence in _sentences(chinese_cues):
        if len(text) + len(sentence) > limit:
            break
        text += sentence
    return text


def build_publish_assist(
    *,
    original_title: str,
    original_language: str | None,
    temp_dir: Path,
    stage3: Mapping[str, Any] | None,
    translator: TitleTranslator | None,
    author: str = "",
    source_details: Mapping[str, Any] | None = None,
    duration_seconds: float | None = None,
    writer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the persisted publish-assist record; never raises."""

    record: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "engine": "rule-based"}
    details = dict(source_details or {})
    try:
        cue_texts = _chinese_cue_texts(temp_dir, stage3 or {})
    except Exception:  # publishing assist must never block delivery
        cue_texts = []

    if writer is not None:
        context = {
            "original_title": original_title,
            "author": author,
            "original_language": original_language,
            "duration_seconds": duration_seconds,
            "description": str(details.get("description") or "")[:3000],
            "tags": list(details.get("tags") or [])[:20],
            "chinese_subtitles_excerpt": _subtitle_excerpt(cue_texts),
        }
        try:
            written = dict(writer(context))
            record.update({
                "engine": str(written.get("engine") or "claude"),
                "title": written["title"], "title_rule": "llm",
                "copy": written["copy"], "copy_rule": "llm",
                "hashtags": list(written.get("hashtags") or []),
            })
            return record
        except Exception as error:  # fall back to the rule-based path below
            record["llm_error"] = f"{type(error).__name__}: {error}"[:300]

    try:
        language = normalize_language_code(original_language) if original_language else None
        source_title = strip_series_suffix(original_title.strip(), author)
        is_chinese = bool(language and is_zh_hans_language(language))
        chinese_title = ""
        if source_title:
            if is_chinese:
                chinese_title = source_title
                record["title_translation"] = "skipped_source_is_zh_hans"
            elif language and translator is not None:
                try:
                    chinese_title = translator(source_title, language) or ""
                    record["title_translation"] = "apple_translation" if chinese_title else "unavailable"
                except (TranslationRuntimeError, TranslationProtocolError, OSError, ValueError) as error:
                    record["title_translation"] = f"failed:{type(error).__name__}"
            else:
                record["title_translation"] = "unavailable"
        record["translated_title"] = chinese_title
        title, title_rule = fit_title(chinese_title, cue_texts)

        blurb, copy_rule = "", "unavailable"
        lead = description_lead(str(details.get("description") or ""))
        if lead:
            translated = lead if is_chinese else ""
            if not is_chinese and language and translator is not None:
                try:
                    translated = translator(lead, language) or ""
                except (TranslationRuntimeError, TranslationProtocolError, OSError, ValueError):
                    translated = ""
            if translated:
                blurb, copy_rule = _copy_from_description(translated), "description_translation"
        if not blurb:
            blurb, copy_rule = build_copy(cue_texts, title=title)
        record.update({"title": title, "title_rule": title_rule, "copy": blurb, "copy_rule": copy_rule, "hashtags": []})
    except Exception as error:  # publishing assist must never block delivery
        record.update({"title": "", "title_rule": "unavailable", "copy": "", "copy_rule": "unavailable",
                       "hashtags": [], "error": type(error).__name__})
    return record


__all__ = [
    "COPY_LIMIT", "TITLE_LIMIT", "apple_title_translator", "build_copy", "build_publish_assist",
    "char_count", "clean_title", "compress_title", "description_lead", "fit_title", "hard_cut",
    "regenerate_title", "strip_series_suffix",
]
