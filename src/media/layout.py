"""Deterministic, non-truncating subtitle wrapping and vertical safe-area layout."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Sequence


class SubtitleLayoutError(ValueError):
    """A cue cannot fit the supported three-line layout without truncation."""


@dataclass(frozen=True)
class CueLayout:
    lines: tuple[str, ...]
    font_size: float

    @property
    def line_count(self) -> int:
        return len(self.lines)


@dataclass(frozen=True)
class VerticalLayout:
    order: tuple[str, ...]
    zh_bottom_margin_px: int
    original_bottom_margin_px: int | None
    safe_band_height_px: int
    required_height_px: float
    line_gap_px: float


def _glyph_width(character: str) -> float:
    category = unicodedata.category(character)
    if category.startswith("M") or character == "\u200d" or character in {"\ufe0e", "\ufe0f"}:
        return 0.0
    if character.isspace():
        return 0.3
    east_asian = unicodedata.east_asian_width(character)
    if east_asian in {"W", "F"}:
        return 1.0
    if category.startswith("P"):
        return 0.5
    return 0.55


def _width(text: str) -> float:
    return sum(_glyph_width(character) for character in text)


def _split_oversized_token(token: str, max_width: float) -> list[str]:
    chunks: list[str] = []
    current = ""
    for character in token:
        if current and _width(current + character) > max_width:
            chunks.append(current)
            current = character
        else:
            current += character
    if current:
        chunks.append(current)
    return chunks


def _wrap(text: str, max_width: float, max_lines: int) -> tuple[str, ...] | None:
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for paragraph in paragraphs:
        tokens = paragraph.split(" ")
        current = ""
        for token in tokens:
            if not token:
                continue
            token_parts = _split_oversized_token(token, max_width)
            for part in token_parts:
                candidate = part if not current else f"{current} {part}"
                if current and _width(candidate) > max_width:
                    lines.append(current)
                    current = part
                else:
                    current = candidate
        if current:
            lines.append(current)
        elif not paragraph and paragraphs != [""]:
            lines.append("")
    if not lines:
        return None
    if len(lines) > max_lines:
        return None
    return tuple(lines)


def layout_cue(
    text: str,
    *,
    frame_width: int,
    frame_height: int,
    width_fraction: float = 0.90,
) -> CueLayout:
    """Try one line, then two, slightly smaller two, and finally three lines."""

    if not text.strip() or frame_width <= 0 or frame_height <= 0:
        raise SubtitleLayoutError("subtitle cue and frame dimensions must be valid")
    base_size = min(frame_width, frame_height) * 0.045
    max_width = frame_width * width_fraction
    base_max_units = max_width / base_size
    wrapped = _wrap(text, base_max_units, 1)
    if wrapped is not None:
        return CueLayout(wrapped, round(base_size, 2))
    wrapped = _wrap(text, base_max_units, 2)
    if wrapped is not None:
        return CueLayout(wrapped, round(base_size, 2))
    reduced_size = base_size * 0.94
    reduced_max_units = max_width / reduced_size
    wrapped = _wrap(text, reduced_max_units, 2)
    if wrapped is not None:
        return CueLayout(wrapped, round(reduced_size, 2))
    wrapped = _wrap(text, reduced_max_units, 3)
    if wrapped is not None:
        return CueLayout(wrapped, round(reduced_size, 2))
    raise SubtitleLayoutError("subtitle cue exceeds the three-line safe layout; text was not truncated")


def calculate_vertical_layout(
    *,
    frame_height: int,
    original_line_count: int,
    chinese_line_count: int,
    font_size: float | None = None,
    safe_band_fraction: float = 0.30,
    gap_line_fraction: float = 0.4,
) -> VerticalLayout:
    if frame_height <= 0 or original_line_count < 0 or chinese_line_count < 0:
        raise SubtitleLayoutError("invalid frame height or subtitle line count")
    font_size = font_size or frame_height * 0.045
    line_height = font_size * 1.2
    gap = font_size * gap_line_fraction
    band_height = round(frame_height * safe_band_fraction)
    if not original_line_count:
        return VerticalLayout(
            ("zh-Hans",),
            round(frame_height * 0.07),
            None,
            band_height,
            chinese_line_count * line_height,
            0,
        )
    zh_margin = round(frame_height * 0.05)
    original_margin = round(zh_margin + chinese_line_count * line_height + gap)
    required = (original_line_count + chinese_line_count) * line_height + gap
    if required <= band_height:
        order = ("original", "zh-Hans")
        return VerticalLayout(order, zh_margin, original_margin, band_height, required, gap)
    original_margin = round(frame_height * 0.05)
    zh_margin = round(original_margin + original_line_count * line_height + gap)
    order = ("zh-Hans", "original")
    return VerticalLayout(order, zh_margin, original_margin, band_height, required, gap)


def combine_cue_layouts(texts: Sequence[str], *, frame_width: int, frame_height: int) -> tuple[CueLayout, ...]:
    return tuple(layout_cue(text, frame_width=frame_width, frame_height=frame_height) for text in texts)
