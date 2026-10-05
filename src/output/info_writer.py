"""Core-owned, deterministic metadata and summary rendering for formal info.md."""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from src.core.job import ContentType, Job
from src.output.paths import InfoRecord


_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SPACE_RE = re.compile(r"\s+")


def _safe_source_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() in {"http", "https"}:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            query = "[redacted]" if parts.query else ""
            return urlunsplit((parts.scheme.lower(), host, parts.path, query, ""))
        return value.split("#", 1)[0]
    except ValueError:
        return "[invalid-source-url]"


def _clean(value: Any, *, multiline: bool = False) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", " ").strip()
    return "\n".join(_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()).strip() if multiline else _SPACE_RE.sub(" ", text)


def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk_mappings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk_mappings(child)


def _subtitle_source(subtitle: Mapping[str, Any], selected: Mapping[str, Any], *, speech: Any) -> str:
    """Describe where the Chinese subtitle came from, using Stage 3's own keys."""

    source = subtitle.get("primary_source") or selected.get("source") or subtitle.get("source")
    language = subtitle.get("primary_language_code")
    if source:
        return f"{source} subtitle track ({language})" if language else f"{source} subtitle track"
    if speech == "speech_detected":
        return "ASR transcript (whisper.cpp)"
    if speech == "no_speech":
        return "none (no subtitle track, no speech)"
    return ""


def _metadata_roots(job: Job) -> list[Mapping[str, Any]]:
    roots: list[Mapping[str, Any]] = [job.source_metadata]
    providers = job.source_metadata.get("providers")
    if isinstance(providers, Mapping):
        roots.extend(providers.values())
    stage3 = job.source_metadata.get("stage3")
    if isinstance(stage3, Mapping):
        roots.insert(1, stage3)
    monitor = job.source_metadata.get("monitor")
    if isinstance(monitor, Mapping):
        # Queue/Cloud HOT evidence wins over similarly named Provider facts.
        roots.insert(0, monitor)
    return [mapping for root in roots for mapping in _walk_mappings(root)]


def _first_value(job: Job, *keys: str) -> Any:
    wanted = {key.lower().replace("-", "_") for key in keys}
    for mapping in _metadata_roots(job):
        for key, value in mapping.items():
            if str(key).lower().replace("-", "_") in wanted and value not in (None, "", [], {}):
                return value
    return None


def _resolution(width: Any, height: Any) -> str:
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError, OverflowError):
        return ""
    return f"{w}x{h}" if w > 0 and h > 0 else ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-16")
        except (OSError, UnicodeDecodeError):
            return ""
    except OSError:
        return ""


def _docx_text(path: Path) -> tuple[str, list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("word/document.xml")
        root = ElementTree.fromstring(raw)
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return "", []
    paragraphs: list[str] = []
    headings: list[str] = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        value = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")).strip()
        if not value:
            continue
        paragraphs.append(value)
        style = paragraph.find(f"{_WORD_NS}pPr/{_WORD_NS}pStyle")
        if style is not None and str(style.attrib.get(f"{_WORD_NS}val", "")).lower().startswith("heading"):
            headings.append(value)
    return "\n".join(paragraphs), headings


def _xlsx_text(path: Path) -> tuple[str, list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")) for item in root]
            rows: list[str] = []
            for name in names:
                root = ElementTree.fromstring(archive.read(name))
                for row in root.iter():
                    if not row.tag.endswith("}row"):
                        continue
                    values: list[str] = []
                    for cell in row:
                        if not cell.tag.endswith("}c"):
                            continue
                        value_node = next((child for child in cell if child.tag.endswith("}v")), None)
                        if value_node is None or value_node.text is None:
                            continue
                        value = value_node.text
                        if cell.attrib.get("t") == "s":
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                        values.append(value)
                    if values:
                        rows.append(" | ".join(values))
            return "\n".join(rows), []
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return "", []


def _pptx_text(path: Path) -> tuple[str, list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(
                (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda name: int(re.search(r"slide(\d+)", name).group(1)),
            )
            slides: list[str] = []
            for name in names:
                root = ElementTree.fromstring(archive.read(name))
                parts = [node.text or "" for node in root.iter() if node.tag.endswith("}t") and node.text]
                if parts:
                    slides.append(" ".join(parts))
            return "\n".join(slides), []
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, AttributeError):
        return "", []


def extract_document_text(path: Path) -> tuple[str, list[str], str]:
    """Read common text-bearing document formats with existing local runtimes."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text, headings = _docx_text(path)
        return text, headings, "python-zipfile/docx-xml" if text else "unavailable"
    if suffix == ".xlsx":
        text, headings = _xlsx_text(path)
        return text, headings, "python-zipfile/xlsx-xml" if text else "unavailable"
    if suffix == ".pptx":
        text, headings = _pptx_text(path)
        return text, headings, "python-zipfile/pptx-xml" if text else "unavailable"
    if suffix in {".txt", ".md", ".markdown", ".csv", ".html", ".htm"}:
        text = _read_text(path)
        return text, [], "utf-text" if text else "unavailable"
    if suffix in {".rtf", ".doc", ".odt", ".ods", ".odp"}:
        executable = shutil.which("textutil")
        if executable:
            try:
                result = subprocess.run([executable, "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True, timeout=20, check=False)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout, [], "macOS-textutil"
            except (OSError, subprocess.SubprocessError):
                pass
        if suffix == ".rtf":
            value = _read_text(path)
            value = re.sub(r"\\'[0-9a-fA-F]{2}", " ", value)
            value = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", value)
            value = value.replace("{", " ").replace("}", " ")
            return _SPACE_RE.sub(" ", value).strip(), [], "deterministic-rtf-fallback" if value.strip() else "unavailable"
    if suffix == ".pdf":
        executable = shutil.which("pdftotext")
        if executable:
            try:
                result = subprocess.run([executable, str(path), "-"], capture_output=True, text=True, timeout=30, check=False)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout, [], "pdftotext"
            except (OSError, subprocess.SubprocessError):
                pass
    return "", [], "unavailable"


def _deterministic_chinese_summary(text: str, headings: Sequence[str], title: str) -> str:
    normalized = _clean(text, multiline=True)
    paragraphs = [part.strip() for part in re.split(r"\n+", normalized) if part.strip()]
    if not paragraphs:
        return ""
    if _CJK_RE.search(normalized):
        compact = "。".join(part.strip("。；; ") for part in paragraphs[:3])
        return compact[:500].rstrip("。；; ")
    title_text = _clean(title) or "未命名文档"
    sections = [_clean(item) for item in headings if _clean(item)]
    if sections:
        section_text = "、".join(sections[:5])
        summary = f"文档《{title_text}》围绕以下章节组织内容：{section_text}。正文共 {len(paragraphs)} 个文本段落，完整内容见正式文档。"
    else:
        excerpt = _clean(paragraphs[0])[:100]
        summary = f"文档《{title_text}》包含 {len(paragraphs)} 个文本段落；开篇内容为“{excerpt}”。正文已完整保留在正式文档中。"
    return summary[:500]


def _markdown_content(source_files: Sequence[Path]) -> str:
    markdown_path = next((path for path in source_files if path.suffix.lower() in {".md", ".markdown"}), None)
    return _read_text(markdown_path) if markdown_path else ""


def _main_content(job: Job, source_files: Sequence[Path]) -> tuple[str, str, str]:
    if job.resolved_content_type in {ContentType.ARTICLE, ContentType.WEBPAGE}:
        markdown = _markdown_content(source_files)
        return markdown, "", "not_applicable"
    if job.resolved_content_type is ContentType.DOCUMENT and source_files:
        text, _headings, engine = extract_document_text(source_files[0])
        return text, text, engine
    if job.resolved_content_type in {ContentType.IMAGE, ContentType.IMAGE_SET}:
        items = _first_value(job, "items")
        if isinstance(items, list):
            captions = []
            for item in items:
                if isinstance(item, Mapping):
                    value = item.get("title") or item.get("caption") or item.get("description")
                    if value:
                        captions.append(_clean(value))
            return "\n".join(captions), "", "not_applicable"
        return _clean(_first_value(job, "caption", "description", "title")), "", "not_applicable"
    if job.resolved_content_type is ContentType.VIDEO:
        return _clean(_first_value(job, "description", "transcript", "transcription")), "", "not_applicable"
    return "", "", "not_applicable"


def render_info_markdown(
    job: Job,
    *,
    source_files: Sequence[Path],
    output_paths: Sequence[str],
) -> tuple[str, dict[str, Any]]:
    """Build stable info.md fields without fetching or changing Job state."""
    main_content, document_text, document_engine = _main_content(job, source_files)
    title = _clean(_first_value(job, "title", "original_title"))
    if job.resolved_content_type is ContentType.DOCUMENT and source_files and not title:
        title = source_files[0].name
    platform = _clean(_first_value(job, "platform", "hostname", "remote_type"))
    if not platform:
        platform = urlsplit(job.source_url).hostname or ""
    published = _clean(_first_value(job, "published_at", "upload_date", "date_published", "release_date"))
    extra: dict[str, Any] = {"Content Deliverable": ", ".join(output_paths)}
    summary = ""
    summary_status = "not_applicable"
    summary_engine = "not_applicable"

    if job.resolved_content_type is ContentType.VIDEO:
        probe = job.provider_metadata("yt-dlp") or {}
        probe_data = probe.get("probe") if isinstance(probe.get("probe"), Mapping) else {}
        selection = probe.get("selection") if isinstance(probe.get("selection"), Mapping) else {}
        stage3 = job.source_metadata.get("stage3") if isinstance(job.source_metadata.get("stage3"), Mapping) else {}
        render = stage3.get("render") if isinstance(stage3.get("render"), Mapping) else {}
        subtitle = stage3.get("subtitle_discovery") if isinstance(stage3.get("subtitle_discovery"), Mapping) else {}
        translation = stage3.get("translation") if isinstance(stage3.get("translation"), Mapping) else {}
        asr = stage3.get("asr") if isinstance(stage3.get("asr"), Mapping) else {}
        original_resolution = _resolution(probe_data.get("width"), probe_data.get("height"))
        downloaded_resolution = (
            _resolution(render.get("width") or render.get("output_width"), render.get("height") or render.get("output_height"))
            or _resolution(selection.get("width"), selection.get("height"))
        )
        selected_subtitle = subtitle.get("selected") if isinstance(subtitle.get("selected"), Mapping) else {}
        values = {
            "Original Resolution": original_resolution,
            "Downloaded Resolution": downloaded_resolution,
            "Original Language": _clean(probe_data.get("original_language")),
            "Subtitle Source": _clean(_subtitle_source(subtitle, selected_subtitle, speech=stage3.get("speech_status"))),
            "Translation Engine": _clean(translation.get("translation_engine") or translation.get("engine")),
            "ASR Engine": _clean(asr.get("engine")),
            "Processing Result": _clean(stage3.get("status") or probe.get("fetch", {}).get("status") if isinstance(probe.get("fetch"), Mapping) else stage3.get("status")),
        }
        for label, value in values.items():
            extra[label] = value
        optional_video_fields = (
            ("First Seen", ("first_seen", "firstSeen")),
            ("HOT At", ("hot_at", "hotAt")),
            ("Views At HOT", ("views_at_hot", "viewsAtHot")),
            ("Like Rate", ("like_rate", "likeRate")),
            ("Velocity", ("velocity",)),
            ("HOT Reason", ("hot_reason", "hotReason")),
            ("Queue ID", ("queue_id", "queueId")),
        )
        all_values = _metadata_roots(job)
        for label, keys in optional_video_fields:
            value = ""
            wanted = {key.lower().replace("_", "") for key in keys}
            for mapping in all_values:
                match = next((item for key, item in mapping.items() if str(key).lower().replace("_", "") in wanted and item not in (None, "", [], {})), None)
                if match is not None:
                    value = _clean(match)
                    break
            extra[label] = value
        publish = job.source_metadata.get("publish_assist")
        publish = publish if isinstance(publish, Mapping) else {}
        extra["Publish Title"] = _clean(publish.get("title"))
        extra["Publish Copy"] = _clean(publish.get("copy"))
        original_language = _clean(probe_data.get("original_language"))
    else:
        original_language = ""

    if job.resolved_content_type is ContentType.DOCUMENT:
        extracted_text, headings, engine = extract_document_text(source_files[0]) if source_files else ("", [], "unavailable")
        summary = _deterministic_chinese_summary(extracted_text, headings, title) if extracted_text.strip() else ""
        summary_engine = "deterministic-extractive" if summary else "unavailable"
        summary_status = "generated" if summary else "empty_parse_or_unsupported_format"
        main_content = extracted_text
        document_engine = engine

    record = InfoRecord(
        content_type=job.resolved_content_type.value,
        platform=platform,
        source_url=_safe_source_url(job.source_url),
        original_title=title,
        author=_clean(_first_value(job, "author", "creator", "uploader")),
        author_url=_clean(_first_value(job, "author_url", "creator_url", "uploader_url")),
        published_at=published,
        main_content=_clean(main_content),
        document_summary_zh=summary if job.resolved_content_type is ContentType.DOCUMENT else None,
        extra_fields=extra,
    )
    summary_details = {
        "status": summary_status,
        "engine": summary_engine,
        "document_text_engine": document_engine,
        "summary_characters": len(summary),
    }
    return record.to_markdown(), summary_details


__all__ = ["extract_document_text", "render_info_markdown"]
