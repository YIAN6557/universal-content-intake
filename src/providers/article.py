"""Stage 4 Trafilatura adapter for readable ARTICLE extraction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import trafilatura
import certifi
from lxml import html as lxml_html

from src.core.job import ContentType, Job
from src.providers.base import (
    ContentProvider,
    ProducedArtifact,
    ProviderCapability,
    ProviderFailure,
    ProviderFailureKind,
    ProviderResult,
    classify_http_access_denial,
)


PROVIDER_NAME = "trafilatura"
ARTICLE_CONTENT_TYPES = frozenset({ContentType.ARTICLE})
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_RETRIES = 2
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
USER_AGENT = "UniversalContentIntake/1.0 (article extraction; no browser session)"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_url(url: str) -> str:
    """Remove query/fragment data from diagnostic URLs without changing stored source identity."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname or ""
        if parts.port:
            hostname = f"{hostname}:{parts.port}"
        return urlunsplit((parts.scheme.lower(), hostname, parts.path, "[redacted]" if parts.query else "", ""))
    except ValueError:
        return "[invalid-url]"


def _valid_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
        return (
            parts.scheme.lower() in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and (port is None or 1 <= port <= 65535)
        )
    except ValueError:
        return False


def _source_identity(url: str) -> str:
    return _sha256_bytes(b"uci-article-source-v1\0" + url.encode("utf-8"))


def _probe_metadata(source_url: str, identity: str, engine_version: str) -> dict[str, Any]:
    hostname = urlsplit(source_url).hostname or ""
    return {
        "content_type": ContentType.ARTICLE.value,
        "source_url": source_url,
        "hostname": hostname,
        "platform": hostname,
        "extraction_engine": PROVIDER_NAME,
        "extraction_engine_version": engine_version,
        "source_identity": identity,
        "network_fetch_performed": False,
    }


def _string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates: Iterable[Any] = re.split(r"[,;|]", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = (value,)
    result: list[str] = []
    for item in candidates:
        texts = re.split(r"[,;|]", item) if isinstance(item, str) else (item,)
        for candidate in texts:
            text = _string(candidate)
            if text and text not in result:
                result.append(text)
    return result


def _html_metadata(html: bytes, final_url: str) -> dict[str, Any]:
    """Read only explicit page metadata that Trafilatura does not expose."""
    try:
        tree = lxml_html.fromstring(html)
    except (ValueError, TypeError):
        return {}
    values: dict[str, list[str]] = {}
    for element in tree.xpath("//meta"):
        key = _string(element.get("property") or element.get("name") or element.get("itemprop"))
        content = _string(element.get("content"))
        if key and content:
            values.setdefault(key.lower(), []).append(content)
    links: list[str] = []
    for element in tree.xpath("//link[@href]"):
        rel = {part.lower() for part in str(element.get("rel") or "").split()}
        href = _string(element.get("href"))
        if "canonical" in rel and href:
            links.append(href)
    canonical = links[0] if links else None
    if canonical:
        from urllib.parse import urljoin
        canonical = urljoin(final_url, canonical)
    headline_parts = tree.xpath("(//article//h1)[1]//text()") or tree.xpath("(//main//h1)[1]//text()")
    headline = " ".join(" ".join(headline_parts).split()) if headline_parts else None
    language = next(iter(tree.xpath("/html/@lang")), None)
    if not language:
        language = next(iter(tree.xpath("//html[1]/@lang")), None)
    tags = values.get("article:tag", [])
    if not tags:
        tags = [part for item in values.get("keywords", []) for part in re.split(r"[,;|]", item)]
    categories = values.get("article:section", []) or values.get("section", [])
    return {
        "canonical_url": canonical,
        "headline": headline,
        "language": _string(language),
        "title": next(iter(values.get("og:title", []) + values.get("twitter:title", [])), None),
        "description": next(iter(values.get("description", []) + values.get("og:description", [])), None),
        "site_name": next(iter(values.get("og:site_name", [])), None),
        "author": next(iter(values.get("author", []) + values.get("article:author", [])), None),
        "published_at": next(iter(values.get("article:published_time", []) + values.get("datepublished", []) + values.get("date", [])), None),
        "tags": _string_list(tags),
        "categories": _string_list(categories),
        "has_article_container": bool(tree.xpath("//article|//main|//*[@itemprop='articleBody']")),
        "has_form": bool(tree.xpath("//form")),
    }


def _form_only_extraction(document: Any, page_metadata: dict[str, Any]) -> bool:
    """Reject form-control labels mistaken for article text, without a length floor."""
    if not page_metadata.get("has_form") or page_metadata.get("has_article_container"):
        return False
    body = getattr(document, "body", None)
    if body is None:
        return True
    prose_nodes = []
    for node in body.iter():
        tag = str(node.tag).lower() if isinstance(node.tag, str) else ""
        if tag not in {"p", "quote", "item"}:
            continue
        text = " ".join("".join(node.itertext()).split())
        if len(text) >= 30 and re.search(r"[.!?][\"'”’)]?(?:\s|$)", text):
            prose_nodes.append(text)
    return not prose_nodes


def _normalized_date(value: Any) -> str | None:
    raw = _string(value)
    if not raw:
        return None
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return parsed.date().isoformat()
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            pass
    return raw


def _inline_text(node: Any) -> str:
    """Render Trafilatura's structured extraction nodes as Markdown inline text."""
    node_tag = str(node.tag).lower() if isinstance(node.tag, str) else ""
    if node_tag in {"graphic", "image"}:
        target = _string(node.get("src") or node.get("target") or node.get("url"))
        alt = _string(node.get("alt") or node.get("title")) or "image"
        if target and urlsplit(target).scheme.lower() in {"http", "https"}:
            return f"![{alt}]({target.replace(' ', '%20')})"
    chunks: list[str] = []
    if node.text:
        chunks.append(node.text)
    for child in node:
        tag = str(child.tag).lower() if isinstance(child.tag, str) else ""
        inner = _inline_text(child)
        raw_inner = " ".join("".join(child.itertext()).split())
        split_word = tag == "hi" and raw_inner[-1:].isupper() and (child.tail or "")[:1].islower()
        if tag in {"ref", "a"}:
            target = _string(child.get("target") or child.get("href"))
            if target and urlsplit(target).scheme.lower() in {"http", "https", "mailto"}:
                inner = f"[{inner}]({target.replace(' ', '%20')})"
        elif tag in {"hi", "em", "i"}:
            rend = (child.get("rend") or "").lower()
            if not split_word and (rend in {"bold", "strong", "#b"} or tag == "strong"):
                inner = f"**{inner}**"
            elif not split_word and (rend in {"italic", "emphasis", "#i"} or tag in {"em", "i"}):
                inner = f"*{inner}*"
        elif tag in {"code", "verbatim"}:
            inner = f"`{inner}`"
        elif tag in {"graphic", "image"}:
            target = _string(child.get("src") or child.get("target") or child.get("url"))
            alt = _string(child.get("alt") or child.get("title")) or inner or "image"
            if target and urlsplit(target).scheme.lower() in {"http", "https"}:
                inner = f"![{alt}]({target.replace(' ', '%20')})"
        elif tag in {"lb", "br"}:
            inner = "  \n"
        chunks.append(inner)
        if child.tail:
            needs_separator = tag in {"ref", "a", "graphic", "image"} or (
                tag == "hi" and not split_word
            )
            if needs_separator and not child.tail[:1].isspace():
                chunks.append(" ")
            chunks.append(child.tail)
    return re.sub(r"[\t\r\n ]+", " ", "".join(chunks)).strip()


def _render_blocks(nodes: Iterable[Any]) -> list[str]:
    blocks: list[str] = []
    for node in nodes:
        tag = str(node.tag).lower() if isinstance(node.tag, str) else ""
        rend = (node.get("rend") or "").lower()
        if tag == "head":
            text = _inline_text(node)
            level_match = re.fullmatch(r"h([1-6])", rend)
            if text and level_match:
                blocks.append(f"{'#' * int(level_match.group(1))} {text}")
            elif text:
                blocks.append(text)
        elif tag in {"p", "item"}:
            text = _inline_text(node)
            if text:
                blocks.append(text)
        elif tag == "list":
            items = [child for child in node if str(child.tag).lower() in {"item", "li"}]
            ordered = rend in {"ol", "ordered", "numbered"}
            rendered_items: list[str] = []
            for index, item in enumerate(items, 1):
                text = _inline_text(item)
                if text:
                    rendered_items.append(f"{index}. {text}" if ordered else f"- {text}")
            if rendered_items:
                blocks.append("\n".join(rendered_items))
        elif tag in {"quote", "blockquote"}:
            inner = _render_blocks(list(node))
            if not inner:
                text = _inline_text(node)
                inner = [text] if text else []
            quoted = "\n\n".join(inner)
            if quoted:
                blocks.append("\n".join(f"> {line}" if line else ">" for line in quoted.splitlines()))
        elif tag in {"graphic", "image"}:
            text = _inline_text(node)
            if text:
                blocks.append(text)
        elif tag in {"table", "row", "cell", "tr", "td", "th"}:
            # Table semantics are retained as readable rows without inventing a header row.
            if tag in {"cell", "td", "th"}:
                text = _inline_text(node)
                if text:
                    blocks.append(text)
            elif tag in {"row", "tr"}:
                cells = [_inline_text(child) for child in node if str(child.tag).lower() in {"cell", "td", "th"}]
                cells = [cell for cell in cells if cell]
                if cells:
                    blocks.append(" | ".join(cells))
            else:
                blocks.extend(_render_blocks(list(node)))
        elif tag in {"pre", "codeblock"}:
            text = "\n".join(line.rstrip() for line in "".join(node.itertext()).strip().splitlines())
            if text:
                blocks.append(f"```\n{text}\n```")
        else:
            # Containers are traversed only where Trafilatura supplied structure;
            # unknown leaf content is retained as a paragraph, not discarded.
            if len(node):
                blocks.extend(_render_blocks(list(node)))
            else:
                text = _inline_text(node)
                if text:
                    blocks.append(text)
        if node.tail and tag not in {"list", "quote", "blockquote"}:
            tail = re.sub(r"[\t\r\n ]+", " ", node.tail).strip()
            if tail:
                blocks.append(tail)
    return blocks


def _markdown(document: Any, metadata: dict[str, Any]) -> tuple[str, str, list[str]]:
    body = getattr(document, "body", None)
    if body is None:
        return "", "", []
    body_text = " ".join("".join(body.itertext()).split())
    blocks = _render_blocks(list(body))
    title = _string(metadata.get("title"))
    if title and not any(
        block.lstrip("# ").strip() == title
        for block in blocks
        if block.startswith("#")
    ):
        blocks.insert(0, f"# {title}")
    markdown = "\n\n".join(block for block in blocks if block.strip()).strip()
    if markdown:
        markdown += "\n"
    image_references: list[str] = []
    for match in re.finditer(r"!\[[^\]]*\]\((https?://[^)]+)\)", markdown):
        if match.group(1) not in image_references:
            image_references.append(match.group(1))
    return markdown, body_text, image_references


@dataclass(frozen=True)
class TrafilaturaExtraction:
    """Task 03's shared Trafilatura and Markdown contract for saved page HTML."""

    document: Any
    extracted: dict[str, Any]
    page_metadata: dict[str, Any]
    title: str | None
    markdown: str
    main_text: str
    image_references: list[str]
    form_only: bool


def extract_trafilatura_markdown(html_bytes: bytes, page_url: str) -> TrafilaturaExtraction:
    """Extract one already-fetched HTML document with the ARTICLE Task 03 rules.

    WEBPAGE calls this after SingleFile has captured and stored its self-contained
    HTML. The parser options and structure-preserving Markdown renderer remain
    shared with ARTICLE instead of growing a second extraction implementation.
    """

    document = trafilatura.bare_extraction(
        html_bytes,
        url=page_url,
        include_comments=False,
        include_tables=True,
        include_images=True,
        include_formatting=True,
        include_links=True,
        favor_precision=False,
        with_metadata=True,
    )
    extracted = document.as_dict() if document is not None else {}
    page_metadata = _html_metadata(html_bytes, page_url)
    title = _string(page_metadata.get("headline")) or _string(extracted.get("title")) or _string(page_metadata.get("title"))
    markdown, main_text, image_references = _markdown(document, {**extracted, "title": title}) if document is not None else ("", "", [])
    return TrafilaturaExtraction(
        document=document,
        extracted=extracted,
        page_metadata=page_metadata,
        title=title,
        markdown=markdown,
        main_text=main_text,
        image_references=image_references,
        form_only=_form_only_extraction(document, page_metadata),
    )


def _is_login_wall(html: bytes, final_url: str) -> bool:
    prefix = html[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace").lower()
    path = urlsplit(final_url).path.lower().rstrip("/")
    if re.search(r"/(?:login|log-in|signin|sign-in|authenticate|auth)$", path):
        return True
    strong_phrases = (
        "sign in to continue",
        "log in to continue",
        "please sign in to continue reading",
        "please log in to continue reading",
        "authentication required",
        "login required",
        "you must be logged in to read",
        "you need to sign in to read",
    )
    if any(phrase in prefix for phrase in strong_phrases):
        return True
    has_auth_form = bool(re.search(r"<form\b[^>]*(?:login|signin|sign-in|auth)|<input\b[^>]*type=[\"']password", prefix))
    return has_auth_form and not re.search(r"<(?:article|main)\b", prefix)


@dataclass(frozen=True)
class _FetchedPage:
    source_url: str
    final_url: str
    content_type: str
    body: bytes
    fetched_at: str


class ArticleProvider(ContentProvider):
    """Fetch one public article page and extract it without a browser session."""

    name = PROVIDER_NAME
    capability = ProviderCapability(ARTICLE_CONTENT_TYPES, True, True, True)

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        retry_backoff_seconds: float = 0.25,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or retries < 0 or retry_backoff_seconds < 0 or max_response_bytes <= 0:
            raise ValueError("invalid ArticleProvider fetch limits")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_response_bytes = max_response_bytes
        try:
            self.version = package_version("trafilatura")
        except PackageNotFoundError:
            self.version = str(getattr(trafilatura, "__version__", "unknown"))
        self._pending: dict[str, str] = {}

    def probe(self, job: Job) -> ProviderResult:
        source_url = job.source_url.strip()
        if not _valid_http_url(source_url):
            raise ProviderFailure(
                ProviderFailureKind.INVALID_URL,
                self.name,
                f"Article source must be an absolute HTTP(S) URL: {_safe_url(source_url)}",
            )
        if job.declared_content_type not in {None, ContentType.ARTICLE}:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "ArticleProvider requires an ARTICLE Job.")
        if job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.ARTICLE}:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "ArticleProvider cannot handle the resolved content type.")
        identity = _source_identity(source_url)
        self._pending[job.job_id] = identity
        return ProviderResult(
            metadata={"probe": _probe_metadata(source_url, identity, self.version)},
            resume_token=identity,
        )

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        source_url = job.source_url.strip()
        identity = self._pending.get(job.job_id)
        if not identity or identity != resume_token or identity != _source_identity(source_url):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Article fetch requires a matching fresh Probe identity.", resume_token=resume_token)
        try:
            temp_root, article_dir = self._article_paths(job, identity)
        except (OSError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"Unsafe Article Job temp workspace: {error}", resume_token=identity) from error

        cached = self._verified_cached_result(job, temp_root, article_dir, identity)
        if cached is not None:
            return cached

        page = self._fetch_page(source_url, identity)
        if _is_login_wall(page.body, page.final_url):
            raise ProviderFailure(ProviderFailureKind.AUTH_REQUIRED, self.name, f"The article page requires a login: {_safe_url(page.final_url)}", resume_token=identity)
        try:
            extraction = extract_trafilatura_markdown(page.body, page.final_url)
        except Exception as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"Trafilatura extraction failed: {type(error).__name__}", resume_token=identity) from error
        extracted = extraction.extracted
        page_metadata = extraction.page_metadata
        title = extraction.title
        markdown = extraction.markdown
        main_text = extraction.main_text
        image_references = extraction.image_references
        form_only = extraction.form_only
        if not main_text.strip() or not markdown.strip() or form_only:
            kind = ProviderFailureKind.AUTH_REQUIRED if _is_login_wall(page.body, page.final_url) else ProviderFailureKind.FAILED
            reason = "The article page is a login wall." if kind is ProviderFailureKind.AUTH_REQUIRED else "Trafilatura could not reliably extract main article text."
            extraction_status = "form_only" if form_only else "empty"
            raise ProviderFailure(kind, self.name, f"{reason} Source: {_safe_url(page.final_url)}", resume_token=identity, details={"extraction_status": extraction_status})

        canonical_url = _string(page_metadata.get("canonical_url")) or _string(extracted.get("url")) or page.final_url
        if not _valid_http_url(canonical_url):
            canonical_url = page.final_url
        canonical_host = urlsplit(canonical_url).hostname or (urlsplit(page.final_url).hostname or "")
        author = _string(extracted.get("author")) or _string(page_metadata.get("author"))
        language = _string(extracted.get("language")) or _string(page_metadata.get("language"))
        if language:
            language = language.strip().lower().replace("_", "-")
        metadata = {
            "source_url": source_url,
            "canonical_url": canonical_url,
            "hostname": canonical_host,
            "platform": canonical_host,
            "site_name": _string(extracted.get("sitename")) or _string(page_metadata.get("site_name")),
            "title": title,
            "author": author,
            "published_at": _normalized_date(extracted.get("date") or page_metadata.get("published_at")),
            "language": language,
            "description": _string(extracted.get("description")) or _string(page_metadata.get("description")),
            "excerpt": None,
            "tags": _string_list(extracted.get("tags")) or _string_list(page_metadata.get("tags")),
            "categories": _string_list(extracted.get("categories")) or _string_list(page_metadata.get("categories")),
            "extraction_engine": self.name,
            "extraction_engine_version": self.version,
            "content_length": len(main_text),
            "fetched_at": page.fetched_at,
            "source_identity": identity,
            "image_references": image_references,
        }
        markdown_bytes = markdown.encode("utf-8")
        markdown_sha256 = _sha256_bytes(markdown_bytes)
        try:
            article_path = self._write_article(article_dir, markdown_bytes)
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Could not safely persist Article Markdown under Job temp.", resume_token=identity) from error
        relative_article = article_path.relative_to(Path(job.workspace_path).resolve()).as_posix()
        artifact = {
            "path": relative_article,
            "size": len(markdown_bytes),
            "sha256": markdown_sha256,
            "provider": self.name,
            "source_url": source_url,
            "canonical_url": canonical_url,
            "extraction_engine": self.name,
            "extraction_engine_version": self.version,
        }
        metadata["artifact_path"] = relative_article
        metadata["artifact_sha256"] = markdown_sha256
        metadata["artifact_size"] = len(markdown_bytes)
        metadata_path = article_dir / "article-metadata.json"
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "source_identity": identity,
            "source_url": source_url,
            "article_metadata": metadata,
            "artifact": artifact,
        }
        try:
            self._write_json_atomic(metadata_path, manifest)
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Could not safely persist Article metadata under Job temp.", resume_token=identity) from error
        metadata_relative = metadata_path.relative_to(Path(job.workspace_path).resolve()).as_posix()
        metadata_bytes = metadata_path.read_bytes()
        metadata_artifact = {
            "path": metadata_relative,
            "size": len(metadata_bytes),
            "sha256": _sha256_bytes(metadata_bytes),
        }
        return ProviderResult(
            metadata={
                "probe": _probe_metadata(source_url, identity, self.version),
                "fetch": {
                    "metadata": metadata,
                    "artifact": artifact,
                    "metadata_artifact": metadata_artifact,
                    "metadata_path": metadata_relative,
                    "reuse": "downloaded",
                }
            },
            artifacts=(
                ProducedArtifact(relative_path=relative_article, role="article", media_type="text/markdown; charset=utf-8"),
                ProducedArtifact(relative_path=metadata_relative, role="metadata", media_type="application/json; charset=utf-8"),
            ),
            resume_token=identity,
        )

    def _article_paths(self, job: Job, identity: str) -> tuple[Path, Path]:
        workspace = Path(job.workspace_path).expanduser()
        temp = Path(job.temp_path).expanduser()
        if workspace.is_symlink() or temp.is_symlink():
            raise ValueError("workspace and temp root cannot be symlinks")
        resolved_workspace = workspace.resolve(strict=True)
        resolved_temp = temp.resolve(strict=True)
        if resolved_temp != resolved_workspace / "temp":
            raise ValueError("temp root must be the canonical workspace/temp directory")
        articles_root = resolved_temp / "articles"
        article_dir = articles_root / identity
        for path in (articles_root, article_dir):
            if path.is_symlink():
                raise ValueError("article output directories cannot be symlinks")
        articles_root.mkdir(parents=True, exist_ok=True)
        article_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = article_dir.resolve(strict=True)
        if not resolved_dir.is_relative_to(resolved_temp):
            raise ValueError("article output path escapes Job temp")
        return resolved_temp, resolved_dir

    def _verified_cached_result(self, job: Job, temp_root: Path, article_dir: Path, identity: str) -> ProviderResult | None:
        metadata_path = article_dir / "article-metadata.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            return None
        try:
            manifest = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
                or manifest.get("source_identity") != identity
                or manifest.get("source_url") != job.source_url.strip()
            ):
                return None
            artifact = manifest["artifact"]
            relative = str(artifact["path"])
            artifact_path = Path(job.workspace_path).resolve() / relative
            if artifact_path.is_symlink() or not artifact_path.is_file():
                return None
            if not artifact_path.resolve().is_relative_to(temp_root):
                return None
            size = artifact_path.stat().st_size
            digest = _sha256_file(artifact_path)
            if size <= 0 or size != int(artifact["size"]) or digest != artifact["sha256"]:
                return None
            metadata = dict(manifest["article_metadata"])
            if metadata.get("source_identity") != identity or metadata.get("artifact_sha256") != digest:
                return None
            metadata_relative = metadata_path.relative_to(Path(job.workspace_path).resolve()).as_posix()
            metadata_bytes = metadata_path.read_bytes()
            metadata_artifact = {
                "path": metadata_relative,
                "size": len(metadata_bytes),
                "sha256": _sha256_bytes(metadata_bytes),
            }
            artifact = dict(artifact)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return ProviderResult(
            metadata={
                "probe": _probe_metadata(job.source_url.strip(), identity, self.version),
                "fetch": {
                    "metadata": metadata,
                    "artifact": artifact,
                    "metadata_artifact": metadata_artifact,
                    "metadata_path": metadata_relative,
                    "reuse": "verified",
                }
            },
            artifacts=(
                ProducedArtifact(relative_path=artifact["path"], role="article", media_type="text/markdown; charset=utf-8"),
                ProducedArtifact(relative_path=metadata_relative, role="metadata", media_type="application/json; charset=utf-8"),
            ),
            resume_token=identity,
        )

    def _fetch_page(self, source_url: str, identity: str) -> _FetchedPage:
        request = urllib.request.Request(
            source_url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"},
            method="GET",
        )
        tls_context = ssl.create_default_context(cafile=certifi.where())
        last_network_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=tls_context) as response:
                    status = int(getattr(response, "status", 200))
                    final_url = response.geturl()
                    if not _valid_http_url(final_url):
                        raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Article redirect left HTTP(S).", resume_token=identity)
                    content_type = response.headers.get_content_type().lower()
                    response_headers = response.headers
                    body = response.read(self.max_response_bytes + 1)
                if status in {404, 410}:
                    raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Article URL is unavailable (HTTP {status}): {_safe_url(final_url)}", resume_token=identity)
                if status in {401, 403}:
                    kind = classify_http_access_denial(status, body[:32768].decode("utf-8", errors="ignore"), response_headers)
                    raise ProviderFailure(kind or ProviderFailureKind.AUTH_REQUIRED, self.name, f"Article source access was denied (HTTP {status}): {_safe_url(final_url)}", resume_token=identity)
                if status >= 500 or status in {408, 425, 429}:
                    last_network_error = OSError(f"HTTP {status}")
                    if attempt < self.retries:
                        self._backoff(attempt)
                        continue
                    raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"Article fetch retries exhausted (HTTP {status}): {_safe_url(source_url)}", resume_token=identity) from last_network_error
                if status >= 400:
                    raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Article URL returned HTTP {status}: {_safe_url(final_url)}", resume_token=identity)
                if len(body) > self.max_response_bytes:
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Article response exceeded the configured size limit.", resume_token=identity)
                if not body:
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Article response was empty.", resume_token=identity)
                if content_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/octet-stream"}:
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"Article URL did not return an HTML document (Content-Type: {content_type}).", resume_token=identity)
                if content_type in {"text/plain", "application/octet-stream"} and not re.search(rb"(?is)<(?:!doctype\s+html|html|head|body|article|main)\b", body[:8192]):
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Article response is not an HTML document.", resume_token=identity)
                return _FetchedPage(source_url, final_url, content_type, body, _now())
            except urllib.error.HTTPError as error:
                status = int(error.code)
                final_url = error.geturl() or source_url
                body = error.read(32768) if status == 403 else b""
                headers = error.headers
                error.close()
                if status in {404, 410}:
                    raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Article URL is unavailable (HTTP {status}): {_safe_url(final_url)}", resume_token=identity) from error
                if status in {401, 403}:
                    kind = classify_http_access_denial(status, body.decode("utf-8", errors="ignore"), headers)
                    raise ProviderFailure(kind or ProviderFailureKind.AUTH_REQUIRED, self.name, f"Article source access was denied (HTTP {status}): {_safe_url(final_url)}", resume_token=identity) from error
                if status in {408, 425, 429} or status >= 500:
                    last_network_error = error
                    if attempt < self.retries:
                        self._backoff(attempt)
                        continue
                    retry_after = self._retry_after(error)
                    raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"Article fetch retries exhausted (HTTP {status}): {_safe_url(source_url)}", retry_after_seconds=retry_after, resume_token=identity) from error
                raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Article URL returned HTTP {status}: {_safe_url(final_url)}", resume_token=identity) from error
            except ProviderFailure:
                raise
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
                last_network_error = error
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"Article fetch retries exhausted: {_safe_url(source_url)}", resume_token=identity) from error
        raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"Article fetch retries exhausted: {_safe_url(source_url)}", resume_token=identity) from last_network_error

    def _backoff(self, attempt: int) -> None:
        delay = min(self.retry_backoff_seconds * (2**attempt), 2.0)
        if delay:
            time.sleep(delay)

    @staticmethod
    def _retry_after(error: urllib.error.HTTPError) -> int | None:
        value = error.headers.get("Retry-After") if error.headers else None
        if not value:
            return None
        try:
            return max(0, min(int(value), 3600))
        except ValueError:
            try:
                return max(0, min(int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()), 3600))
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _write_article(article_dir: Path, content: bytes) -> Path:
        candidate = article_dir / "article.md"
        index = 2
        while candidate.exists() or candidate.is_symlink():
            candidate = article_dir / f"article-{index}.md"
            index += 1
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(candidate, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise
        return candidate

    @staticmethod
    def _write_json_atomic(destination: Path, payload: dict[str, Any]) -> None:
        descriptor, pending_name = tempfile.mkstemp(prefix=".article-metadata.", suffix=".pending", dir=destination.parent)
        pending = Path(pending_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, destination)
        except BaseException:
            try:
                pending.unlink()
            except OSError:
                pass
            raise


__all__ = ["ArticleProvider", "PROVIDER_NAME"]
