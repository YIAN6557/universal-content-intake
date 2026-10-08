"""Stage 4 DOCUMENT acquisition adapters.

HTTP transfers are delegated to curl/aria2, cloud remotes to rclone, and public
Google share links may fall back to gdown. The adapter never mutates Job state.
"""

from __future__ import annotations

import email.message
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import unquote
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qs, unquote as url_unquote, urlsplit
import xml.etree.ElementTree as ET

from src.core.job import ContentType, Job
from src.core.remote_identity import (
    remote_identity,
    remote_identity_is_strong,
    remote_identity_matches,
    verified_remote_identity,
)
from src.providers.base import (
    ContentProvider,
    ProducedArtifact,
    ProviderCapability,
    ProviderFailure,
    ProviderFailureKind,
    ProviderResult,
    classify_http_access_denial,
)


PROVIDER_NAME = "document"
DOCUMENT_CONTENT_TYPES = frozenset({ContentType.DOCUMENT})
MANIFEST_NAME = "document-manifest.json"
STATE_NAME = "transfer-state.json"
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_RETRIES = 2
DEFAULT_LARGE_FILE_THRESHOLD_BYTES = 32 * 1024 * 1024
MAX_FOLDER_DEPTH = 3
MAX_FOLDER_FILES = 100
MAX_VALIDATION_BYTES = 512 * 1024 * 1024
COMMAND_RUNNER = Callable[..., subprocess.CompletedProcess[str]]

GOOGLE_NATIVE = {
    "document": ("application/vnd.google-apps.document", "docx"),
    "spreadsheets": ("application/vnd.google-apps.spreadsheet", "xlsx"),
    "presentation": ("application/vnd.google-apps.presentation", "pptx"),
}
OOXML_REQUIREMENTS = {
    "docx": ("word/document.xml", "[Content_Types].xml"),
    "xlsx": ("xl/workbook.xml", "[Content_Types].xml"),
    "pptx": ("ppt/presentation.xml", "[Content_Types].xml"),
}
MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
}
GOOGLE_EXPORT_BY_KIND = {key: value[1] for key, value in GOOGLE_NATIVE.items()}
SUPPORTED_EXTENSIONS = frozenset(MIME_BY_EXTENSION)


@dataclass(frozen=True)
class _DocumentPaths:
    temp: Path
    root: Path
    state: Path
    manifest: Path


@dataclass(frozen=True)
class _PendingSource:
    identity: str
    kind: str
    source_url: str
    final_url: str | None = None
    status: int | None = None
    headers: dict[str, str] | None = None
    remote: str | None = None
    remote_path: str | None = None
    file_id: str | None = None
    google_kind: str | None = None
    folder: bool = False
    remote_metadata: dict[str, Any] | None = None
    remote_identity: dict[str, Any] | None = None


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
    try:
        parts = urlsplit(url)
        return f"{parts.scheme.lower()}://{parts.hostname or ''}{parts.path}" + ("?[redacted]" if parts.query else "")
    except ValueError:
        return "[invalid-url]"


def _identity(*parts: str | None) -> str:
    value = "\0".join(part or "" for part in parts)
    return _sha256_bytes(b"uci-document-source-v1\0" + value.encode("utf-8"))


def _rclone_remote_identity(remote: str, remote_path: str, metadata: dict[str, Any]) -> dict[str, Any]:
    hashes = metadata.get("Hashes")
    checksum_algorithm: str | None = None
    checksum_value: Any = None
    if isinstance(hashes, dict):
        for candidate in ("SHA-256", "SHA256", "SHA-1", "SHA1", "MD5", "DropboxHash"):
            if hashes.get(candidate):
                checksum_algorithm = candidate.lower().replace("-", "")
                checksum_value = hashes[candidate]
                break
    fields: dict[str, Any] = {
        "remote": remote,
        "remote_path": remote_path,
        "id": metadata.get("ID"),
        "size": metadata.get("Size"),
        "modified_time": metadata.get("ModTime"),
        "revision_id": metadata.get("RevisionID") or metadata.get("RevisionId"),
        "version": metadata.get("Version") or metadata.get("VersionID"),
        "generation": metadata.get("Generation"),
    }
    if checksum_value:
        fields["checksum_algorithm"] = checksum_algorithm
        fields["remote_checksum"] = checksum_value
    return remote_identity("rclone", fields)


def _header_blocks(raw: str) -> list[tuple[int, dict[str, str]]]:
    blocks: list[tuple[int, dict[str, str]]] = []
    status: int | None = None
    headers: dict[str, str] = {}
    for line in raw.replace("\r", "").split("\n"):
        match = re.match(r"^HTTP/\S+\s+(\d{3})", line.strip(), re.IGNORECASE)
        if match:
            if status is not None:
                blocks.append((status, headers))
            status = int(match.group(1))
            headers = {}
        elif ":" in line and status is not None:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        elif not line.strip() and status is not None:
            blocks.append((status, headers))
            status = None
            headers = {}
    if status is not None:
        blocks.append((status, headers))
    return blocks


def _disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    message = email.message.Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    if not filename:
        match = re.search(r"filename\*=([^']*)''([^;]+)", value, re.IGNORECASE)
        filename = url_unquote(match.group(2)) if match else None
    return unquote(filename) if filename else None


def _source_from_google_url(url: str) -> tuple[str, str | None, bool] | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not (host == "drive.google.com" or host.endswith(".drive.google.com") or host == "docs.google.com" or host.endswith(".docs.google.com")):
        return None
    path_parts = [item for item in parts.path.split("/") if item]
    file_id: str | None = None
    folder = False
    google_kind: str | None = None
    if host.endswith("docs.google.com"):
        for kind in GOOGLE_NATIVE:
            if kind in path_parts:
                google_kind = kind
                break
        if google_kind:
            try:
                file_id = path_parts[path_parts.index("d") + 1]
            except (ValueError, IndexError):
                return None
    elif "folders" in path_parts:
        try:
            file_id = path_parts[path_parts.index("folders") + 1]
        except IndexError:
            return None
        folder = True
    elif "file" in path_parts and "d" in path_parts:
        try:
            file_id = path_parts[path_parts.index("d") + 1]
        except IndexError:
            return None
    else:
        query = parse_qs(parts.query)
        file_id = (query.get("id") or [None])[0]
    if not file_id or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", file_id):
        return None
    return file_id, google_kind, folder


def _rclone_url(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() != "rclone":
        return None
    remote = parts.hostname or ""
    path = url_unquote(parts.path.lstrip("/"))
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", remote) or not path:
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {".", ".."} for part in pure.parts):
        return None
    return remote, pure.as_posix()


def _safe_relative_path(value: str, *, max_depth: int = MAX_FOLDER_DEPTH) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or len(path.parts) > max_depth or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("remote path is unsafe or exceeds the configured folder depth")
    if any("\0" in part or ":" in part for part in path.parts):
        raise ValueError("remote path contains a forbidden character")
    return path


def _safe_filename(value: str) -> str:
    """Keep a single sanitized path component; reject any traversal spelling."""
    candidate = value.strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    if not candidate or path.is_absolute() or len(path.parts) != 1 or path.name in {".", ".."}:
        raise ValueError("remote filename attempts path traversal")
    if re.match(r"^[A-Za-z]:", candidate) or "\0" in candidate:
        raise ValueError("remote filename is not a safe file component")
    safe = re.sub(r"[^\w.()\[\] -]", "_", path.name, flags=re.UNICODE).strip(" .")
    if safe in {"", ".", ".."}:
        raise ValueError("remote filename becomes empty after sanitization")
    return safe[:180]


def _unique_path(directory: Path, filename: str) -> Path:
    desired = _safe_filename(filename)
    target = directory / desired
    number = 2
    while target.exists() or target.is_symlink():
        target = directory / f"{Path(desired).stem}-{number}{Path(desired).suffix}"
        number += 1
    return target


def _classify_http(status: int, *, headers: dict[str, str] | None = None, message: str = "") -> ProviderFailureKind | None:
    low = (message + " " + " ".join((headers or {}).values())).lower()
    if status == 404 or status == 410:
        return ProviderFailureKind.INVALID_URL
    if status == 429 or "quota exceeded" in low or "rate limit" in low or "too many requests" in low:
        return ProviderFailureKind.NETWORK
    access_failure = classify_http_access_denial(status, message, headers)
    if access_failure is not None:
        return access_failure
    if 400 <= status < 500:
        return ProviderFailureKind.FAILED
    if status >= 500:
        return ProviderFailureKind.NETWORK
    return None


def _classify_text_failure(value: str) -> ProviderFailureKind:
    low = value.lower()
    if any(term in low for term in (
        "sign in", "login required", "permission", "access denied", "authentication required",
        "http 401", "invalid_grant", "no token", "token not found", "need access",
    )):
        return ProviderFailureKind.AUTH_REQUIRED
    if any(term in low for term in ("404", "not found", "deleted", "does not exist")):
        return ProviderFailureKind.INVALID_URL
    if any(term in low for term in (
        "quota exceeded", "download quota", "rate limit", "too many requests", "http 429",
        "timed out", "timeout", "sslerror", "ssl eof", "tls", "max retries exceeded",
        "connection reset", "connection refused", "connection aborted", "failed to establish",
        "network is unreachable", "temporarily unavailable", "incomplete read",
    )):
        return ProviderFailureKind.NETWORK
    return ProviderFailureKind.FAILED


def _looks_like_html(data: bytes) -> bool:
    prefix = data[:8192].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in prefix[:2048]


def _login_or_error_html(data: bytes) -> bool:
    low = data[:32768].decode("utf-8", errors="ignore").lower()
    return _looks_like_html(data) and any(
        marker in low for marker in ("sign in", "log in", "login", "access denied", "permission required", "you need access", "verify you are", "not found")
    )


def _permission_wall_html(data: bytes) -> bool:
    low = data[:32768].decode("utf-8", errors="ignore").lower()
    return _looks_like_html(data) and any(
        marker in low for marker in (
            "sign in", "sign-in", "login required", "log in", "permission denied",
            "permission required", "you need access", "authentication required", "unauthorized", "request access",
        )
    )


def validate_document(
    path: Path,
    *,
    content_type: str | None = None,
    pdfinfo_executable: str | None = None,
    filename_hint: str | None = None,
) -> dict[str, Any]:
    """Validate real bytes and lightweight readability without extracting archives."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise ValueError("document artifact is missing, empty, or not a regular file")
    size = path.stat().st_size
    if size > MAX_VALIDATION_BYTES:
        raise ValueError("document exceeds the bounded readability-validation size")
    with path.open("rb") as stream:
        head = stream.read(min(32768, size))
    if _looks_like_html(head):
        if _login_or_error_html(head):
            raise PermissionError("HTTP response is a login, permission, or error page, not a document")
        raise ValueError("HTML page is not a DOCUMENT artifact")

    extension = Path(filename_hint).suffix.lower() if filename_hint else path.suffix.lower()
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    signature: str | None = None
    page_count: int | None = None
    metadata: dict[str, Any] = {}
    if head.startswith(b"%PDF-"):
        signature = ".pdf"
        command = [pdfinfo_executable, str(path)] if pdfinfo_executable else None
        info: subprocess.CompletedProcess[str] | None = None
        if command:
            try:
                info = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            except (OSError, subprocess.TimeoutExpired):
                # A missing or hung reader must not crash validation; the byte
                # scan below still requires at least one PDF page object.
                info = None
        if info is not None and info.returncode == 0:
            match = re.search(r"^Pages:\s+(\d+)\s*$", info.stdout, re.MULTILINE)
            if match:
                page_count = int(match.group(1))
            title = re.search(r"^Title:\s*(.*?)\s*$", info.stdout, re.MULTILINE)
            if title and title.group(1):
                metadata["title"] = title.group(1)
        if page_count is None:
            raw = path.read_bytes()
            page_count = len(re.findall(rb"/Type\s*/Page\b", raw))
        if page_count <= 0:
            raise ValueError("PDF signature is valid but no readable pages were found")
        metadata["page_count"] = page_count
    elif zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if len(names) > 4096 or sum(item.file_size for item in archive.infolist()) > MAX_VALIDATION_BYTES:
                    raise ValueError("Office package exceeds bounded validation limits")
                for name, required in OOXML_REQUIREMENTS.items():
                    if set(required).issubset(names):
                        signature = f".{name}"
                        xml_payload = archive.read(required[0])
                        if len(xml_payload) > 16 * 1024 * 1024:
                            raise ValueError("OOXML main document XML exceeds validation limit")
                        ET.fromstring(xml_payload)
                        break
                if signature is None and "content.xml" in names and "mimetype" in names:
                    mime = archive.read("mimetype").decode("ascii", errors="ignore").strip()
                    signature = {
                        "application/vnd.oasis.opendocument.text": ".odt",
                        "application/vnd.oasis.opendocument.spreadsheet": ".ods",
                        "application/vnd.oasis.opendocument.presentation": ".odp",
                    }.get(mime)
                    if signature:
                        ET.fromstring(archive.read("content.xml"))
                if signature is None:
                    raise ValueError("ZIP is not a recognized Office/OpenDocument package")
                corrupt = archive.testzip()
                if corrupt:
                    raise ValueError("Office ZIP package contains a corrupt member")
        except (zipfile.BadZipFile, KeyError, ET.ParseError, UnicodeDecodeError) as error:
            raise ValueError("Office container is not a readable document") from error
        metadata["container"] = "zip"
    elif head.startswith(b"{\\rtf"):
        signature = ".rtf"
        if b"\\par" not in head:
            raise ValueError("RTF document has no readable paragraph content")
    else:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as error:
            raise ValueError("document type is unsupported or text is not readable UTF-8") from error
        if not text.strip() or "\x00" in text or sum(ord(char) < 9 for char in text) > max(1, len(text) // 100):
            raise ValueError("text document is empty or not readable")
        if re.search(r"<\s*(html|!doctype|form)\b", text[:8192], re.IGNORECASE):
            raise ValueError("HTML/login response cannot be accepted as text document")
        signature = extension if extension in {".txt", ".md", ".markdown", ".csv"} else ".txt"
        metadata["text_characters"] = len(text)

    if extension and extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported document extension: {extension}")
    if extension and signature != extension:
        raise ValueError(f"file extension {extension} conflicts with detected content {signature}")
    expected_mime = MIME_BY_EXTENSION.get(signature or "")
    if declared and expected_mime and declared not in {expected_mime, "application/octet-stream", "binary/octet-stream"}:
        # Some servers use text/plain for Markdown and application/zip for OOXML.
        compatible = signature in {".md", ".markdown", ".csv"} and declared in {"text/plain", "application/octet-stream"}
        compatible = compatible or signature in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"} and declared in {"application/zip", "application/octet-stream"}
        if not compatible:
            raise ValueError(f"Content-Type {declared} conflicts with detected document {signature}")
    return {"extension": signature, "content_type": expected_mime, "size": size, **metadata}


class DocumentProvider(ContentProvider):
    """Acquire one direct/remote document or a bounded cloud folder."""

    name = PROVIDER_NAME
    capability = ProviderCapability(DOCUMENT_CONTENT_TYPES, True, True, True)

    def __init__(
        self,
        *,
        curl_executable: str | Path | None = None,
        aria2_executable: str | Path | None = None,
        rclone_executable: str | Path | None = None,
        gdown_executable: str | Path | None = None,
        pdfinfo_executable: str | Path | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        retry_backoff_seconds: float = 0.25,
        large_file_threshold_bytes: int = DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
        max_folder_depth: int = MAX_FOLDER_DEPTH,
        max_folder_files: int = MAX_FOLDER_FILES,
        command_runner: COMMAND_RUNNER = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0 or retries < 0 or retry_backoff_seconds < 0 or large_file_threshold_bytes < 0:
            raise ValueError("invalid DocumentProvider execution limits")
        if max_folder_depth < 1 or max_folder_files < 1:
            raise ValueError("invalid DocumentProvider folder bounds")
        runtime = Path(__file__).resolve().parents[2] / "tools" / "document-runtime" / "bin"
        from src.providers.video import locate_tool

        user_gdown = locate_tool("gdown", "UCI_GDOWN_PATH")
        self.curl = self._resolve_executable(curl_executable or os.environ.get("UCI_CURL_PATH"), shutil.which("curl") or "curl")
        self.aria2 = self._resolve_executable(aria2_executable or os.environ.get("UCI_ARIA2_PATH"), shutil.which("aria2c") or runtime / "aria2c")
        self.rclone = self._resolve_executable(rclone_executable or os.environ.get("UCI_RCLONE_PATH"), shutil.which("rclone") or runtime / "rclone")
        self.gdown = self._resolve_executable(gdown_executable or os.environ.get("UCI_GDOWN_PATH"), shutil.which("gdown") or user_gdown)
        # poppler's pdfinfo when installed, else the bundled PDFKit helper, which
        # prints the same "Pages:" line (no Homebrew/poppler on this machine).
        pdfkit_info = Path(__file__).resolve().parents[2] / "apple-helper" / "PDFInfo" / "bin" / "uci-pdfinfo"
        self.pdfinfo = self._resolve_executable(pdfinfo_executable or os.environ.get("UCI_PDFINFO_PATH"), shutil.which("pdfinfo") or pdfkit_info)
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.large_file_threshold_bytes = large_file_threshold_bytes
        self.max_folder_depth = max_folder_depth
        self.max_folder_files = max_folder_files
        self.command_runner = command_runner
        self._versions: dict[str, str] = {}
        self._pending: dict[str, _PendingSource] = {}

    @staticmethod
    def _resolve_executable(configured: str | Path | None, fallback: str | Path) -> Path:
        candidate = Path(configured).expanduser() if configured else Path(fallback).expanduser()
        if not candidate.is_absolute():
            located = shutil.which(str(candidate))
            if located:
                candidate = Path(located)
        return candidate

    def probe(self, job: Job) -> ProviderResult:
        source_url = job.source_url.strip()
        if job.declared_content_type not in {None, ContentType.DOCUMENT} or job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.DOCUMENT}:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "DocumentProvider requires a DOCUMENT-locked Job.")

        rclone_source = _rclone_url(source_url)
        if source_url.lower().startswith("rclone://") and rclone_source is None:
            raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, "rclone remote URL has an unsafe or invalid path.")
        if rclone_source:
            remote, remote_path = rclone_source
            remotes = self._list_remotes()
            remote_type = remotes.get(remote)
            if remote_type is None:
                raise ProviderFailure(ProviderFailureKind.AUTH_REQUIRED, self.name, "The requested rclone remote is not configured.")
            metadata = self._rclone_stat(remote, remote_path)
            freshness = _rclone_remote_identity(remote, remote_path, metadata)
            if metadata.get("IsDir"):
                pending = _PendingSource(_identity(source_url, remote, remote_path, "folder"), "rclone_folder", source_url, remote=remote, remote_path=remote_path, folder=True, remote_metadata=metadata, remote_identity=freshness)
            else:
                pending = _PendingSource(_identity(source_url, json.dumps(freshness, sort_keys=True, ensure_ascii=False)), "rclone", source_url, remote=remote, remote_path=remote_path, remote_metadata=metadata, remote_identity=freshness)
            self._pending[job.job_id] = pending
            return self._probe_result(pending, remote_type=remote_type)

        google_source = _source_from_google_url(source_url)
        if google_source:
            file_id, google_kind, folder = google_source
            identity = _identity(source_url, "gdrive", file_id, google_kind, "folder" if folder else "file")
            freshness = remote_identity("google-drive", {"file_id": file_id, "kind": google_kind, "folder": folder})
            pending = _PendingSource(identity, "gdrive_folder" if folder else "gdrive", source_url, file_id=file_id, google_kind=google_kind, folder=folder, remote_identity=freshness)
            self._pending[job.job_id] = pending
            return self._probe_result(pending, remote_type="drive")

        parts = urlsplit(source_url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Document source must be an absolute HTTP(S), Google Drive, or configured rclone URL: {_safe_url(source_url)}")
        status, final_url, headers = self._curl_head(source_url)
        kind = _classify_http(status, headers=headers)
        if kind in {ProviderFailureKind.INVALID_URL, ProviderFailureKind.AUTH_REQUIRED, ProviderFailureKind.NETWORK}:
            self._raise_failure(kind, f"HTTP probe returned status {status} for {_safe_url(final_url)}")
        identity = _identity(source_url, final_url, headers.get("etag"), headers.get("last-modified"), headers.get("content-length"), headers.get("content-type"))
        freshness = remote_identity("http", {
            "url": final_url,
            "etag": headers.get("etag"),
            "modified_time": headers.get("last-modified"),
            "size": _optional_int(headers.get("content-length")),
        })
        pending = _PendingSource(identity, "http", source_url, final_url=final_url, status=status, headers=headers, remote_identity=freshness)
        self._pending[job.job_id] = pending
        return self._probe_result(pending)

    def _probe_result(self, pending: _PendingSource, *, remote_type: str | None = None) -> ProviderResult:
        headers = pending.headers or {}
        probe = {
            "content_type": ContentType.DOCUMENT.value,
            "source_url": pending.source_url,
            "resolved_url": pending.final_url,
            "source_identity": pending.identity,
            "provider": pending.kind,
            "provider_type": remote_type,
            "file_id": pending.file_id,
            "remote": pending.remote,
            "remote_path": pending.remote_path,
            "folder": pending.folder,
            "status": pending.status,
            "content_type_header": headers.get("content-type"),
            "content_length": _optional_int(headers.get("content-length")),
            "content_disposition": headers.get("content-disposition"),
            "accept_ranges": headers.get("accept-ranges"),
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "remote_identity": self._remote_identity_for_pending(pending),
            "network_fetch_performed": pending.kind.startswith("http"),
        }
        return ProviderResult(metadata={"probe": probe}, resume_token=pending.identity)

    @staticmethod
    def _remote_identity_for_pending(pending: _PendingSource) -> dict[str, Any]:
        if pending.remote_identity is not None:
            return pending.remote_identity
        if pending.kind.startswith("http"):
            headers = pending.headers or {}
            return remote_identity("http", {
                "url": pending.final_url or pending.source_url,
                "etag": headers.get("etag"),
                "modified_time": headers.get("last-modified"),
                "size": _optional_int(headers.get("content-length")),
            })
        if pending.remote:
            return _rclone_remote_identity(pending.remote, pending.remote_path or "", pending.remote_metadata or {})
        return remote_identity("google-drive", {"file_id": pending.file_id, "kind": pending.google_kind, "folder": pending.folder})

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        pending = self._pending.get(job.job_id)
        if pending is None or pending.identity != resume_token or pending.source_url != job.source_url.strip():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Document fetch requires a matching fresh Probe identity.", resume_token=resume_token)
        try:
            paths = self._job_paths(job, pending.identity)
        except (OSError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"Unsafe Document Job temp workspace: {error}", resume_token=pending.identity) from error

        cached_candidate = self._manifest_has_candidate(paths, pending)
        existing = self._verified_manifest_result(job, paths, pending)
        if existing is not None:
            return existing
        reuse_status = "downloaded"
        if cached_candidate:
            reuse_status = "refetched-weak-identity" if not remote_identity_is_strong(self._remote_identity_for_pending(pending)) else "refetched-invalid-cache"

        if pending.kind == "http":
            record = self._fetch_http(job, paths, pending)
            return self._finish_result(job, paths, pending, [record], engine=record["engine"], reused=[], reuse_status=reuse_status)
        if pending.kind == "rclone":
            record = self._fetch_rclone_file(paths, pending, relative_name=pending.remote_path.rsplit("/", 1)[-1])
            return self._finish_result(job, paths, pending, [record], engine="rclone", reused=[], reuse_status=reuse_status)
        if pending.kind == "rclone_folder":
            records, failed = self._fetch_rclone_folder(paths, pending)
            return self._folder_result(job, paths, pending, records, failed, engine="rclone", reuse_status=reuse_status)
        if pending.kind == "gdrive":
            record = self._fetch_google_file(paths, pending)
            return self._finish_result(job, paths, pending, [record], engine=record["engine"], reused=[], reuse_status=reuse_status)
        if pending.kind == "gdrive_folder":
            records, failed = self._fetch_google_folder(paths, pending)
            return self._folder_result(job, paths, pending, records, failed, engine=records[0]["engine"] if records else "gdown", reuse_status=reuse_status)
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "Unsupported Document source provider.", resume_token=pending.identity)

    def _job_paths(self, job: Job, identity: str) -> _DocumentPaths:
        workspace = Path(job.workspace_path).expanduser()
        temp = Path(job.temp_path).expanduser()
        if workspace.is_symlink() or temp.is_symlink():
            raise ValueError("workspace and temp root cannot be symlinks")
        root = workspace.resolve(strict=True)
        temp = temp.resolve(strict=True)
        if temp != root / "temp":
            raise ValueError("temp root must be the canonical workspace/temp directory")
        documents_root = temp / "documents"
        doc_root = documents_root / identity
        for directory in (documents_root, doc_root):
            if directory.is_symlink():
                raise ValueError("Document output directories cannot be symlinks")
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.resolve(strict=True).is_relative_to(temp):
                raise ValueError("Document output escaped Job temp")
        state_dir = temp / ".document-state" / identity
        if state_dir.is_symlink():
            raise ValueError("Document state directory cannot be a symlink")
        state_dir.mkdir(parents=True, exist_ok=True)
        if not state_dir.resolve(strict=True).is_relative_to(temp):
            raise ValueError("Document transfer state escaped Job temp")
        return _DocumentPaths(temp, doc_root, state_dir / STATE_NAME, doc_root / MANIFEST_NAME)

    def _nested_paths(self, paths: _DocumentPaths, relative_path: str) -> _DocumentPaths:
        safe_relative = _safe_relative_path(relative_path, max_depth=self.max_folder_depth)
        nested_root = paths.root.joinpath(*safe_relative.parts[:-1])
        nested_root.mkdir(parents=True, exist_ok=True)
        if nested_root.is_symlink() or not nested_root.resolve(strict=True).is_relative_to(paths.temp):
            self._raise_failure(ProviderFailureKind.FAILED, "folder path escaped Job temp")
        state_hash = _sha256_bytes(safe_relative.as_posix().encode("utf-8"))[:20]
        state_dir = paths.state.parent / state_hash
        if state_dir.is_symlink():
            self._raise_failure(ProviderFailureKind.FAILED, "folder item state path cannot be a symlink")
        state_dir.mkdir(parents=True, exist_ok=True)
        if not state_dir.resolve(strict=True).is_relative_to(paths.temp):
            self._raise_failure(ProviderFailureKind.FAILED, "folder item state escaped Job temp")
        return _DocumentPaths(paths.temp, nested_root, state_dir / STATE_NAME, paths.manifest)

    def _read_manifest(self, paths: _DocumentPaths) -> dict[str, Any]:
        try:
            if paths.manifest.is_symlink():
                return {}
            value = json.loads(paths.manifest.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _verified_manifest_result(self, job: Job, paths: _DocumentPaths, pending: _PendingSource) -> ProviderResult | None:
        # Folder freshness is established per listed child. A folder stat alone
        # cannot prove that its complete membership and contents are unchanged.
        if pending.folder:
            return None
        manifest = self._read_manifest(paths)
        if manifest.get("source", {}).get("identity") != pending.identity or manifest.get("complete") is not True:
            return None
        entries = manifest.get("artifacts")
        if not isinstance(entries, list) or not entries:
            return None
        current_identity = self._remote_identity_for_pending(pending)
        artifacts: list[ProducedArtifact] = []
        for record in entries:
            if not isinstance(record, dict) or record.get("role") != "document_file":
                return None
            if not remote_identity_matches(current_identity, record.get("remote_identity")):
                return None
            path = self._manifest_path(job, paths, record.get("path"))
            if path is None or not self._record_matches(path, record):
                return None
            try:
                validate_document(path, content_type=record.get("mime"), pdfinfo_executable=str(self.pdfinfo))
            except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
                return None
            artifacts.append(ProducedArtifact(str(record["path"]), "document_file", str(record.get("mime") or "application/octet-stream")))
        manifest_path = paths.manifest
        manifest_hash = _sha256_file(manifest_path)
        relative_manifest = manifest_path.relative_to(Path(job.workspace_path).resolve()).as_posix()
        artifacts.append(ProducedArtifact(relative_manifest, "manifest", "application/json; charset=utf-8"))
        return ProviderResult(
            metadata={"probe": self._probe_result(pending).metadata["probe"], "fetch": {"source_identity": pending.identity, "manifest": relative_manifest, "artifacts": entries, "artifact_hashes": {record["path"]: record["sha256"] for record in entries}, "resume": "verified", "reuse": "verified", "remote_identity": verified_remote_identity(current_identity, verified=True), "manifest_sha256": manifest_hash}},
            artifacts=tuple(artifacts),
            resume_token=pending.identity,
        )

    def _manifest_has_candidate(self, paths: _DocumentPaths, pending: _PendingSource) -> bool:
        manifest = self._read_manifest(paths)
        return (
            isinstance(manifest.get("source"), dict)
            and manifest["source"].get("identity") == pending.identity
            and manifest.get("complete") is True
            and isinstance(manifest.get("artifacts"), list)
            and bool(manifest["artifacts"])
        )

    @staticmethod
    def _manifest_path(job: Job, paths: _DocumentPaths, relative: Any) -> Path | None:
        if not isinstance(relative, str):
            return None
        candidate = Path(job.workspace_path).resolve() / relative
        if candidate.is_symlink():
            return None
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(paths.temp) or not resolved.is_file():
                return None
            return resolved
        except (OSError, ValueError):
            return None

    @staticmethod
    def _record_matches(path: Path, record: dict[str, Any]) -> bool:
        try:
            return path.stat().st_size == int(record.get("size", -1)) and _sha256_file(path) == record.get("sha256")
        except (OSError, TypeError, ValueError):
            return False

    def _curl_head(self, url: str) -> tuple[int, str, dict[str, str]]:
        if not self.curl.is_file():
            self._raise_failure(ProviderFailureKind.FAILED, "curl executable is unavailable")
        args = [str(self.curl), "--silent", "--show-error", "--location", "--head", "--max-redirs", "10", "--connect-timeout", "10", "--max-time", str(int(self.timeout_seconds)), "--write-out", "\nUCI_STATUS=%{http_code}\nUCI_FINAL_URL=%{url_effective}\n", "--", url]
        try:
            result = self.command_runner(args, capture_output=True, text=True, timeout=self.timeout_seconds + 5, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._raise_failure(ProviderFailureKind.NETWORK, f"curl metadata probe failed: {type(error).__name__}")
        blocks = _header_blocks(result.stdout or "")
        status, headers = blocks[-1] if blocks else (0, {})
        final_url = url
        for line in (result.stdout or "").splitlines():
            if line.startswith("UCI_FINAL_URL="):
                final_url = line.partition("=")[2].strip() or url
            elif line.startswith("UCI_STATUS="):
                status = _optional_int(line.partition("=")[2]) or status
        if status == 0 or result.returncode not in {0, 22}:
            self._raise_failure(ProviderFailureKind.NETWORK, f"curl metadata probe failed for {_safe_url(url)}")
        if status in {405, 501}:
            status, headers, final_url = self._curl_range_probe(url)
        return status, final_url, headers

    def _curl_range_probe(self, url: str) -> tuple[int, dict[str, str], str]:
        args = [str(self.curl), "--silent", "--show-error", "--location", "--range", "0-0", "--max-filesize", "1024", "--max-redirs", "10", "--connect-timeout", "10", "--max-time", "10", "--dump-header", "-", "--output", os.devnull, "--write-out", "\nUCI_STATUS=%{http_code}\nUCI_FINAL_URL=%{url_effective}\n", "--", url]
        try:
            result = self.command_runner(args, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._raise_failure(ProviderFailureKind.NETWORK, f"curl range probe failed: {type(error).__name__}")
        blocks = _header_blocks(result.stdout or "")
        status, headers = blocks[-1] if blocks else (0, {})
        final_url = url
        for line in (result.stdout or "").splitlines():
            if line.startswith("UCI_FINAL_URL="):
                final_url = line.partition("=")[2].strip() or url
            if line.startswith("UCI_STATUS="):
                status = _optional_int(line.partition("=")[2]) or status
        if status == 0 or result.returncode not in {0, 63, 22}:
            self._raise_failure(ProviderFailureKind.NETWORK, f"curl range probe failed for {_safe_url(url)}")
        return status, headers, final_url

    def _rclone_exe(self) -> str:
        if self.rclone.is_file():
            return str(self.rclone)
        self._raise_failure(ProviderFailureKind.AUTH_REQUIRED, "rclone is not installed or configured for the requested remote")

    def _gdown_exe(self) -> str:
        if self.gdown.is_file():
            return str(self.gdown)
        self._raise_failure(ProviderFailureKind.FAILED, "gdown executable is unavailable")

    def _list_remotes(self) -> dict[str, str]:
        if not self.rclone.is_file():
            return {}
        result = self._run([str(self.rclone), "listremotes", "--long"], timeout=20)
        if result.returncode != 0:
            return {}
        remotes: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            fields = line.strip().split()
            if len(fields) >= 2 and fields[0].endswith(":"):
                remotes[fields[0][:-1]] = fields[1]
        return remotes

    def _rclone_stat(self, remote: str, remote_path: str) -> dict[str, Any]:
        executable = self._rclone_exe()
        args = [executable, "lsjson", f"{remote}:{remote_path}", "--stat", "--metadata", "--hash"]
        result = self._run(args, timeout=self.timeout_seconds)
        if result.returncode != 0:
            kind = _classify_text_failure((result.stderr or "") + " " + (result.stdout or ""))
            if kind is ProviderFailureKind.FAILED and any(word in (result.stderr or "").lower() for word in ("not found", "doesn't exist")):
                kind = ProviderFailureKind.INVALID_URL
            self._raise_failure(kind, f"rclone could not stat configured remote {remote!r}")
        try:
            value = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            self._raise_failure(ProviderFailureKind.FAILED, "rclone stat returned invalid metadata")
        if not isinstance(value, dict) or not value:
            self._raise_failure(ProviderFailureKind.INVALID_URL, "rclone remote path was not found")
        return value

    def _fetch_http(self, job: Job, paths: _DocumentPaths, pending: _PendingSource) -> dict[str, Any]:
        headers = pending.headers or {}
        content_length = _optional_int(headers.get("content-length"))
        filename = _disposition_filename(headers.get("content-disposition"))
        if not filename:
            url_name = PurePosixPath(urlsplit(pending.final_url or pending.source_url).path).name
            filename = url_name or "document"
        if not Path(filename).suffix:
            extension = self._extension_from_mime(headers.get("content-type"))
            if not extension and (headers.get("content-type") or "").split(";", 1)[0].strip().lower() == "text/html":
                # Download just enough to distinguish an anonymous login wall
                # from arbitrary HTML; the body gate rejects it before artifact
                # finalization, so the placeholder suffix never escapes temp.
                extension = ".txt"
            filename = filename + (extension or "")
        return self._download_http_record(paths, pending, filename, content_length, headers)

    def _download_http_record(
        self,
        paths: _DocumentPaths,
        pending: _PendingSource,
        filename: str,
        expected_size: int | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            safe_name = _safe_filename(filename)
        except ValueError as error:
            self._raise_failure(ProviderFailureKind.FAILED, str(error))
        if Path(safe_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            self._raise_failure(ProviderFailureKind.UNSUPPORTED, "HTTP response does not identify a supported document file type")
        final_path = _unique_path(paths.root, safe_name)
        partial = paths.state.parent / (safe_name + ".partial")
        control = partial.with_name(partial.name + ".aria2")
        if paths.state.is_symlink() or partial.is_symlink() or control.is_symlink():
            self._raise_failure(ProviderFailureKind.FAILED, "document transfer state cannot use symlink paths")
        state = self._read_state(paths.state, pending.identity)
        valid_partial = bool(state and state.get("source_identity") == pending.identity and partial.is_file())
        if not valid_partial and partial.exists():
            self._discard_partial(partial, paths.state, control)
            state = None
        transfer_engine = "aria2" if (
            expected_size is not None and expected_size >= self.large_file_threshold_bytes
            or valid_partial and (state or {}).get("transfer_engine") == "aria2"
            or valid_partial and control.exists()
        ) else "curl"
        if transfer_engine == "aria2" and not self.aria2.is_file():
            self._raise_failure(ProviderFailureKind.FAILED, "aria2 is required for this large HTTP document but is unavailable")
        self._write_json(paths.state, {
            "source_identity": pending.identity,
            "source_url": pending.source_url,
            "resolved_url": pending.final_url,
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "expected_size": expected_size,
            "partial_path": partial.relative_to(paths.temp).as_posix(),
            "transfer_engine": transfer_engine,
            "updated_at": _now(),
        })

        result: subprocess.CompletedProcess[str]
        if transfer_engine == "aria2":
            result = self._aria2_transfer(pending.final_url or pending.source_url, partial, paths, resume=valid_partial)
        else:
            args = [str(self.curl), "--fail-with-body", "--location", "--silent", "--show-error", "--retry", str(self.retries), "--retry-all-errors", "--retry-delay", "1", "--connect-timeout", "10", "--max-time", str(int(self.timeout_seconds)), "--dump-header", str(paths.state.parent / "curl-headers.txt"), "--output", str(partial), "--write-out", "\nUCI_STATUS=%{http_code}\nUCI_FINAL_URL=%{url_effective}\nUCI_CONTENT_TYPE=%{content_type}\nUCI_SIZE=%{size_download}\n", "--", pending.final_url or pending.source_url]
            result = self._run(args, timeout=self.timeout_seconds + 5)
            if result.returncode != 0:
                status, final_headers = self._saved_http_headers(paths.state.parent / "curl-headers.txt")
                response_body = partial.read_bytes()[:32768] if partial.is_file() and not partial.is_symlink() else b""
                if status == 403 and _permission_wall_html(response_body):
                    self._raise_failure(ProviderFailureKind.AUTH_REQUIRED, "HTTP source returned a permission or authentication wall")
                kind = _classify_http(status, headers=final_headers, message=(result.stderr or ""))
                if status >= 400 or kind in {ProviderFailureKind.INVALID_URL, ProviderFailureKind.AUTH_REQUIRED, ProviderFailureKind.NETWORK}:
                    self._raise_failure(kind, f"curl transfer returned HTTP {status or 'network error'}")
                if self.aria2.is_file() and partial.exists() and partial.stat().st_size > 0:
                    transfer_engine = "aria2"
                    # curl partials have no aria2 control file, so initialize an
                    # aria2-owned transfer; later attempts resume its control pair.
                    self._write_json(paths.state, {
                        "source_identity": pending.identity,
                        "source_url": pending.source_url,
                        "resolved_url": pending.final_url,
                        "etag": headers.get("etag"),
                        "last_modified": headers.get("last-modified"),
                        "expected_size": expected_size,
                        "partial_path": partial.relative_to(paths.temp).as_posix(),
                        "transfer_engine": transfer_engine,
                        "updated_at": _now(),
                    })
                    self._discard_if_regular(partial)
                    result = self._aria2_transfer(pending.final_url or pending.source_url, partial, paths, resume=False)
                else:
                    self._raise_failure(ProviderFailureKind.NETWORK, f"curl transfer failed with exit {result.returncode}")
        if result.returncode != 0 or not partial.is_file() or partial.stat().st_size <= 0:
            self._raise_failure(ProviderFailureKind.NETWORK, f"{transfer_engine} did not complete the document transfer")

        status, final_headers = self._saved_http_headers(paths.state.parent / "curl-headers.txt")
        body_head = partial.read_bytes()[:32768]
        if _looks_like_html(body_head):
            if _login_or_error_html(body_head):
                self._raise_failure(ProviderFailureKind.AUTH_REQUIRED, "HTTP source returned a login or access error page")
            self._raise_failure(ProviderFailureKind.FAILED, "HTTP source returned HTML instead of a DOCUMENT")
        response_content_type = final_headers.get("content-type") or headers.get("content-type")
        detected = self._validate_or_raise(partial, response_content_type, filename_hint=filename)
        if expected_size and partial.stat().st_size != expected_size:
            self._raise_failure(ProviderFailureKind.NETWORK, "downloaded size does not match the advertised Content-Length")
        os.replace(partial, final_path)
        return {
            "role": "document_file",
            "path": self._relpath(paths, final_path),
            "filename": final_path.name,
            "extension": detected["extension"],
            "mime": detected["content_type"],
            "size": final_path.stat().st_size,
            "sha256": _sha256_file(final_path),
            "source_url": pending.source_url,
            "resolved_url": self._writeout_value(result.stdout or "", "UCI_FINAL_URL") or pending.final_url,
            "provider": transfer_engine,
            "engine": transfer_engine,
            "engine_version": self._version(transfer_engine),
            "original_filename": filename,
            "export_format": None,
            "remote_identity": remote_identity("http", {
                "url": self._writeout_value(result.stdout or "", "UCI_FINAL_URL") or pending.final_url or pending.source_url,
                "etag": final_headers.get("etag") or headers.get("etag"),
                "modified_time": final_headers.get("last-modified") or headers.get("last-modified"),
                "size": expected_size,
            }),
            "validation": detected,
        }

    def _aria2_transfer(self, url: str, partial: Path, paths: _DocumentPaths, *, resume: bool) -> subprocess.CompletedProcess[str]:
        args = [str(self.aria2), f"--continue={'true' if resume else 'false'}", "--allow-overwrite=false", "--auto-file-renaming=false", "--file-allocation=none", "--max-tries", str(self.retries + 1), "--retry-wait=1", "--connect-timeout=10", "--timeout", str(int(self.timeout_seconds)), "--console-log-level=warn", "--summary-interval=0", "--dir", str(partial.parent), "--out", partial.name, url]
        return self._run(args, timeout=self.timeout_seconds + 10)

    def _fetch_google_file(self, paths: _DocumentPaths, pending: _PendingSource) -> dict[str, Any]:
        google_type = pending.google_kind
        export_format = GOOGLE_EXPORT_BY_KIND.get(google_type or "")
        if self.rclone.is_file():
            remotes = {name: remote_type for name, remote_type in self._list_remotes().items() if remote_type.lower() == "drive"}
            for remote in sorted(remotes):
                attempt = paths.state.parent / f"{remote}-rclone.partial"
                if attempt.is_symlink():
                    self._raise_failure(ProviderFailureKind.FAILED, "rclone output path cannot be a symlink")
                self._discard_if_regular(attempt)
                filename = f"{pending.file_id}.{export_format}" if export_format else f"{pending.file_id}.bin"
                args = [str(self.rclone), "backend", "copyid", f"{remote}:", pending.file_id or "", str(attempt)]
                if export_format:
                    args.append(f"--drive-export-formats={export_format}")
                resource_key = (parse_qs(urlsplit(pending.source_url).query).get("resourcekey") or [None])[0]
                if resource_key:
                    args.append(f"--drive-resource-key={resource_key}")
                result = self._run(args, timeout=self.timeout_seconds)
                if result.returncode == 0 and attempt.is_file() and attempt.stat().st_size:
                    return self._finalize_download(paths, pending, attempt, filename, provider="rclone", engine_version=self._version("rclone"), export_format=export_format, original_filename=filename, content_type=MIME_BY_EXTENSION.get(Path(filename).suffix.lower()))
                failure = _classify_text_failure((result.stderr or "") + " " + (result.stdout or ""))
                if failure not in {ProviderFailureKind.AUTH_REQUIRED, ProviderFailureKind.FAILED, ProviderFailureKind.INVALID_URL}:
                    self._raise_failure(failure, "rclone Google Drive copyid did not complete")
                if failure is ProviderFailureKind.INVALID_URL:
                    self._raise_failure(failure, "rclone reports the Google Drive resource is unavailable")
                self._discard_if_regular(attempt)

        executable = self._gdown_exe()
        listing = self._run([executable, pending.source_url, "--json", "--quiet", "--no-cookies"], timeout=self.timeout_seconds)
        if listing.returncode != 0:
            self._raise_failure(_classify_text_failure((listing.stderr or "") + " " + (listing.stdout or "")), "gdown could not resolve this public Google Drive resource")
        try:
            values = json.loads(listing.stdout or "[]")
            listing_item = values[0] if isinstance(values, list) and values else {}
            original_filename = str(listing_item.get("path") or f"{pending.file_id}.{export_format or 'bin'}")
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            original_filename = f"{pending.file_id}.{export_format or 'bin'}"
        try:
            safe_name = _safe_filename(original_filename)
        except ValueError as error:
            self._raise_failure(ProviderFailureKind.FAILED, str(error))
        if export_format and Path(safe_name).suffix.lower() != f".{export_format}":
            safe_name = Path(safe_name).stem + f".{export_format}"
        elif Path(safe_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            self._raise_failure(ProviderFailureKind.UNSUPPORTED, "Google Drive file name does not identify a supported document format")
        target = paths.state.parent / (safe_name + ".partial")
        if paths.state.is_symlink() or target.is_symlink():
            self._raise_failure(ProviderFailureKind.FAILED, "gdown transfer state cannot use symlink paths")
        state = self._read_state(paths.state, pending.identity)
        resume = bool(state and state.get("source_identity") == pending.identity and target.is_file())
        if target.exists() and not resume:
            self._discard_if_regular(target)
        command = [executable, pending.source_url, "--output", str(target), "--quiet", "--no-cookies", "--retries", str(self.retries)]
        if export_format:
            command.extend(["--format", export_format])
        if resume:
            command.append("--continue")
        self._write_json(paths.state, {"source_identity": pending.identity, "file_id": pending.file_id, "provider": "gdrive", "partial_path": target.relative_to(paths.temp).as_posix(), "updated_at": _now()})
        result = self._run(command, timeout=self.timeout_seconds)
        if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            self._raise_failure(_classify_text_failure((result.stderr or "") + " " + (result.stdout or "")), "gdown public download failed")
        return self._finalize_download(paths, pending, target, safe_name, provider="gdown", engine_version=self._version("gdown"), export_format=export_format, original_filename=original_filename, remote_identity=self._remote_identity_for_pending(pending), content_type=MIME_BY_EXTENSION.get(Path(safe_name).suffix.lower()))

    def _fetch_google_folder(self, paths: _DocumentPaths, pending: _PendingSource) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        remotes = {name: remote_type for name, remote_type in self._list_remotes().items() if remote_type.lower() == "drive"}
        resource_key = (parse_qs(urlsplit(pending.source_url).query).get("resourcekey") or [None])[0]
        for remote in sorted(remotes):
            root_flags = [f"--drive-root-folder-id={pending.file_id}"]
            if resource_key:
                root_flags.append(f"--drive-resource-key={resource_key}")
            list_args = [str(self.rclone), "lsjson", f"{remote}:", "--recursive", "--max-depth", str(self.max_folder_depth), "--metadata", "--hash", *root_flags]
            listing_result = self._run(list_args, timeout=self.timeout_seconds)
            if listing_result.returncode != 0:
                kind = _classify_text_failure((listing_result.stderr or "") + " " + (listing_result.stdout or ""))
                if kind is ProviderFailureKind.NETWORK:
                    self._raise_failure(kind, "rclone Google Drive folder listing was rate-limited or interrupted")
                continue
            try:
                listing = json.loads(listing_result.stdout or "[]")
            except json.JSONDecodeError:
                continue
            if not isinstance(listing, list):
                continue
            files = sorted((item for item in listing if isinstance(item, dict) and not item.get("IsDir")), key=lambda item: str(item.get("Path") or item.get("Name") or "").casefold())
            if len(files) > self.max_folder_files:
                self._raise_failure(ProviderFailureKind.UNSUPPORTED, "Google Drive folder exceeds the configured file-count limit")
            previous = self._read_manifest(paths)
            previous_by_path = {item.get("remote_path"): item for item in previous.get("artifacts", []) if isinstance(item, dict)}
            records: list[dict[str, Any]] = []
            failed: list[dict[str, str]] = []
            for item in files:
                remote_path = str(item.get("Path") or item.get("Name") or "")
                try:
                    relative = _safe_relative_path(remote_path, max_depth=self.max_folder_depth)
                    remote_freshness = _rclone_remote_identity(remote, remote_path, item)
                    old = previous_by_path.get(remote_path)
                    cached = self._manifest_path_from_record(paths, old)
                    if cached and self._record_matches(cached, old or {}) and remote_identity_matches(remote_freshness, (old or {}).get("remote_identity")):
                        records.append(dict(old or {}))
                        continue
                    mime = str(item.get("MimeType") or "")
                    native_kind = next((kind for kind, value in GOOGLE_NATIVE.items() if value[0] == mime), None)
                    item_id = str(item.get("ID") or "")
                    if not item_id:
                        raise ValueError("rclone folder item has no provider ID")
                    item_url = f"https://drive.google.com/uc?id={item_id}"
                    child_identity = _identity(pending.identity, remote_path, item_id, str(item.get("Size")), str(item.get("ModTime")))
                    child = _PendingSource(child_identity, "rclone", pending.source_url, remote=remote, remote_path=remote_path, remote_metadata=item, remote_identity=remote_freshness)
                    child_paths = self._nested_paths(paths, remote_path)
                    try:
                        record = self._fetch_rclone_file(child_paths, child, relative_name=relative.name, extra_flags=tuple(root_flags))
                    except ProviderFailure as rclone_failure:
                        if rclone_failure.kind is ProviderFailureKind.NETWORK:
                            raise
                        fallback = _PendingSource(child_identity, "gdrive", item_url, file_id=item_id, google_kind=native_kind, remote_identity=remote_freshness)
                        record = self._fetch_google_file(child_paths, fallback)
                    record["remote_path"] = remote_path
                    record["remote_identity"] = remote_freshness
                    record.pop("absolute_path", None)
                    records.append(record)
                except ProviderFailure as failure:
                    failed.append({"remote_path": remote_path, "cause": failure.kind.value})
                except (OSError, ValueError) as error:
                    failed.append({"remote_path": remote_path, "cause": type(error).__name__})
            if records or failed:
                return records, failed
            # An empty folder is confirmed by Drive; let the normal gate report it.
            return [], []

        executable = self._gdown_exe()
        result = self._run([executable, pending.source_url, "--json", "--quiet", "--no-cookies"], timeout=self.timeout_seconds)
        if result.returncode != 0:
            self._raise_failure(_classify_text_failure((result.stderr or "") + " " + (result.stdout or "")), "gdown could not list the public Google Drive folder")
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            self._raise_failure(ProviderFailureKind.FAILED, "gdown folder listing is invalid JSON")
        if not isinstance(entries, list) or len(entries) > self.max_folder_files:
            self._raise_failure(ProviderFailureKind.UNSUPPORTED, "Google Drive folder exceeds the configured file-count limit")
        normalized: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("url"), str):
                self._raise_failure(ProviderFailureKind.FAILED, "gdown returned an invalid folder item")
            try:
                relative = _safe_relative_path(entry["path"], max_depth=self.max_folder_depth)
            except ValueError as error:
                self._raise_failure(ProviderFailureKind.FAILED, str(error))
            normalized.append((relative.as_posix(), entry["url"]))
        normalized.sort(key=lambda item: item[0].casefold())
        previous = self._read_manifest(paths)
        previous_by_path = {item.get("remote_path"): item for item in previous.get("artifacts", []) if isinstance(item, dict)}
        records: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for remote_path, item_url in normalized:
            previous_item = previous_by_path.get(remote_path)
            cached_path = self._manifest_path_from_record(paths, previous_item)
            try:
                parsed = _source_from_google_url(item_url)
                if parsed is None or parsed[2]:
                    raise ValueError("folder entry does not identify a Google Drive file")
                file_id, google_kind, _folder = parsed
                child_freshness = remote_identity("google-drive", {"file_id": file_id, "kind": google_kind, "remote_path": remote_path})
                if cached_path and self._record_matches(cached_path, previous_item or {}) and remote_identity_matches(child_freshness, (previous_item or {}).get("remote_identity")):
                    records.append(dict(previous_item or {}))
                    continue
                child = _PendingSource(_identity(pending.identity, remote_path, file_id), "gdrive", item_url, file_id=file_id, google_kind=google_kind, remote_identity=child_freshness)
                child_paths = self._nested_paths(paths, remote_path)
                record = self._fetch_google_file(child_paths, child)
                record["remote_path"] = remote_path
                record.pop("absolute_path", None)
                records.append(record)
            except ProviderFailure as failure:
                failed.append({"remote_path": remote_path, "cause": failure.kind.value})
            except (OSError, ValueError) as error:
                failed.append({"remote_path": remote_path, "cause": type(error).__name__})
        return records, failed

    @staticmethod
    def _manifest_path_from_record(paths: _DocumentPaths, record: dict[str, Any] | None) -> Path | None:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            return None
        candidate = paths.temp / record["path"].removeprefix("temp/")
        try:
            resolved = candidate.resolve(strict=True)
            return resolved if resolved.is_file() and resolved.is_relative_to(paths.temp) and not candidate.is_symlink() else None
        except (OSError, ValueError):
            return None

    def _fetch_rclone_file(self, paths: _DocumentPaths, pending: _PendingSource, *, relative_name: str, extra_flags: tuple[str, ...] = ()) -> dict[str, Any]:
        executable = self._rclone_exe()
        try:
            safe_name = _safe_filename(relative_name)
        except ValueError as error:
            self._raise_failure(ProviderFailureKind.FAILED, str(error))
        metadata = pending.remote_metadata or {}
        extension = Path(safe_name).suffix.lower()
        export_format = extension[1:] if extension in {".docx", ".xlsx", ".pptx"} else None
        partial = paths.state.parent / (safe_name + ".remote-partial")
        if partial.is_symlink():
            self._raise_failure(ProviderFailureKind.FAILED, "rclone output path cannot be a symlink")
        self._discard_if_regular(partial)
        args = [executable, "copyto", f"{pending.remote}:{pending.remote_path}", str(partial)]
        args.extend(extra_flags)
        if export_format:
            args.append(f"--drive-export-formats={export_format}")
        result = self._run(args, timeout=self.timeout_seconds)
        if result.returncode != 0 or not partial.is_file() or not partial.stat().st_size:
            self._raise_failure(_classify_text_failure((result.stderr or "") + " " + (result.stdout or "")), "rclone read-only copyto failed")
        native_mime = str(metadata.get("MimeType") or "")
        if native_mime in {value[0] for value in GOOGLE_NATIVE.values()}:
            native_kind = next((key for key, item in GOOGLE_NATIVE.items() if item[0] == native_mime), None)
            export_format = GOOGLE_EXPORT_BY_KIND.get(native_kind or "")
            if export_format and Path(safe_name).suffix.lower() != f".{export_format}":
                safe_name = Path(safe_name).stem + f".{export_format}"
        detected_content_type = None if native_mime in {value[0] for value in GOOGLE_NATIVE.values()} else native_mime or MIME_BY_EXTENSION.get(Path(safe_name).suffix.lower())
        return self._finalize_download(
            paths,
            pending,
            partial,
            safe_name,
            provider="rclone",
            engine_version=self._version("rclone"),
            export_format=export_format,
            original_filename=str(metadata.get("Name") or relative_name),
            remote_identity=self._remote_identity_for_pending(pending),
            content_type=detected_content_type,
        )

    def _fetch_rclone_folder(self, paths: _DocumentPaths, pending: _PendingSource) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        executable = self._rclone_exe()
        args = [executable, "lsjson", f"{pending.remote}:{pending.remote_path}", "--recursive", "--max-depth", str(self.max_folder_depth), "--metadata", "--hash"]
        result = self._run(args, timeout=self.timeout_seconds)
        if result.returncode != 0:
            self._raise_failure(_classify_text_failure((result.stderr or "") + " " + (result.stdout or "")), "rclone could not list configured folder")
        try:
            listing = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            self._raise_failure(ProviderFailureKind.FAILED, "rclone folder listing is invalid JSON")
        if not isinstance(listing, list):
            self._raise_failure(ProviderFailureKind.FAILED, "rclone folder listing is not an array")
        files = sorted((item for item in listing if isinstance(item, dict) and not item.get("IsDir")), key=lambda item: str(item.get("Path") or item.get("Name") or "").casefold())
        if len(files) > self.max_folder_files:
            self._raise_failure(ProviderFailureKind.UNSUPPORTED, "rclone folder exceeds the configured file-count limit")
        previous = self._read_manifest(paths)
        previous_by_path = {item.get("remote_path"): item for item in previous.get("artifacts", []) if isinstance(item, dict)}
        records: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for item in files:
            remote_path = str(item.get("Path") or item.get("Name") or "")
            try:
                safe_relative = _safe_relative_path(remote_path, max_depth=self.max_folder_depth)
                old = previous_by_path.get(remote_path)
                cached = self._manifest_path_from_record(paths, old)
                remote_freshness = _rclone_remote_identity(pending.remote or "", remote_path, item)
                if cached and self._record_matches(cached, old or {}) and remote_identity_matches(remote_freshness, (old or {}).get("remote_identity")):
                    records.append(dict(old or {}))
                    continue
                child = _PendingSource(_identity(pending.identity, remote_path, str(item.get("ID")), str(item.get("Size")), str(item.get("ModTime"))), "rclone", pending.source_url, remote=pending.remote, remote_path=f"{pending.remote_path.rstrip('/')}/{safe_relative.as_posix()}", remote_metadata=item, remote_identity=remote_freshness)
                child_paths = self._nested_paths(paths, remote_path)
                record = self._fetch_rclone_file(child_paths, child, relative_name=safe_relative.name)
                record["remote_path"] = remote_path
                record["remote_identity"] = remote_freshness
                record.pop("absolute_path", None)
                records.append(record)
            except ProviderFailure as failure:
                failed.append({"remote_path": remote_path, "cause": failure.kind.value})
            except (OSError, ValueError) as error:
                failed.append({"remote_path": remote_path, "cause": type(error).__name__})
        return records, failed

    def _folder_result(
        self,
        job: Job,
        paths: _DocumentPaths,
        pending: _PendingSource,
        records: list[dict[str, Any]],
        failed: list[dict[str, str]],
        *,
        engine: str,
        reuse_status: str,
    ) -> ProviderResult:
        if not records and not failed:
            self._raise_failure(ProviderFailureKind.FAILED, "Cloud folder is empty or contains no supported documents")
        manifest_value = self._manifest_value(pending, records, engine, complete=not failed)
        self._write_json(paths.manifest, manifest_value)
        if failed:
            raise ProviderFailure(ProviderFailureKind.PARTIAL, self.name, f"Folder transfer completed {len(records)} of {len(records) + len(failed)} files.", resume_token=pending.identity, details={"document_partial": {"successful_items": len(records), "failed_items": failed, "manifest_path": self._relpath(paths, paths.manifest)}})
        return self._provider_result(job, paths, pending, records, manifest_value, resume="downloaded", reuse=reuse_status)

    def _finish_result(self, job: Job, paths: _DocumentPaths, pending: _PendingSource, records: list[dict[str, Any]], *, engine: str, reused: list[str], reuse_status: str) -> ProviderResult:
        content_comparison = None
        if len(records) == 1:
            records, same_content = self._preserve_identical_cached_artifact(paths, records)
            if same_content:
                content_comparison = "same_sha256_after_refetch"
        for record in records:
            record.pop("absolute_path", None)
        manifest_value = self._manifest_value(pending, records, engine, complete=True)
        self._write_json(paths.manifest, manifest_value)
        return self._provider_result(job, paths, pending, records, manifest_value, resume="downloaded" if not reused else "partial-reuse", reuse=reuse_status, content_comparison=content_comparison)

    def _preserve_identical_cached_artifact(self, paths: _DocumentPaths, records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        previous = self._read_manifest(paths)
        old_records = previous.get("artifacts")
        if previous.get("complete") is not True or not isinstance(old_records, list) or len(old_records) != 1:
            return records, False
        old = old_records[0]
        new = records[0]
        if not isinstance(old, dict) or not isinstance(new, dict) or old.get("sha256") != new.get("sha256"):
            return records, False
        old_path = self._manifest_path_from_record(paths, old)
        new_path = self._manifest_path_from_record(paths, new)
        if old_path is None or new_path is None or old_path == new_path or not self._record_matches(old_path, old):
            return records, False
        preserved = dict(old)
        preserved["remote_identity"] = new.get("remote_identity")
        preserved["source_url"] = new.get("source_url")
        if new_path.is_file() and not new_path.is_symlink() and new_path.resolve(strict=True).is_relative_to(paths.temp):
            new_path.unlink()
        return [preserved], True

    def _provider_result(self, job: Job, paths: _DocumentPaths, pending: _PendingSource, records: list[dict[str, Any]], manifest_value: dict[str, Any], *, resume: str, reuse: str, content_comparison: str | None = None) -> ProviderResult:
        artifacts = [ProducedArtifact(str(record["path"]), "document_file", str(record.get("mime") or "application/octet-stream")) for record in records]
        manifest_relative = self._relpath(paths, paths.manifest)
        artifacts.append(ProducedArtifact(manifest_relative, "manifest", "application/json; charset=utf-8"))
        hashes = {str(record["path"]): str(record["sha256"]) for record in records}
        return ProviderResult(
            metadata={
                "probe": self._probe_result(pending).metadata["probe"],
                "fetch": {
                    "source_identity": pending.identity,
                    "provider": pending.kind,
                    "engine": manifest_value["engine"],
                    "manifest": manifest_relative,
                    "artifacts": records,
                    "artifact_hashes": hashes,
                    "manifest_sha256": _sha256_file(paths.manifest),
                    "resume": resume,
                    "reuse": reuse,
                    "remote_identity": verified_remote_identity(self._remote_identity_for_pending(pending), verified=reuse == "verified"),
                    "content_comparison": content_comparison,
                },
            },
            artifacts=tuple(artifacts),
            resume_token=pending.identity,
        )

    def _manifest_value(self, pending: _PendingSource, records: list[dict[str, Any]], engine: str, *, complete: bool) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "created_at": _now(),
            "complete": complete,
            "source": {
                "identity": pending.identity,
                "url": pending.source_url,
                "resolved_url": pending.final_url,
                "provider": pending.kind,
                "file_id": pending.file_id,
                "remote": pending.remote,
                "remote_path": pending.remote_path,
                "etag": (pending.headers or {}).get("etag"),
                "last_modified": (pending.headers or {}).get("last-modified"),
                "expected_size": _optional_int((pending.headers or {}).get("content-length")),
                "remote_identity": self._remote_identity_for_pending(pending),
            },
            "engine": {"name": engine, "version": self._version(engine)},
            "artifacts": records,
        }

    def _finalize_download(
        self,
        paths: _DocumentPaths,
        pending: _PendingSource,
        partial: Path,
        filename: str,
        *,
        provider: str,
        engine_version: str,
        export_format: str | None,
        original_filename: str,
        remote_identity: dict[str, Any] | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        if not partial.is_file() or partial.is_symlink():
            self._raise_failure(ProviderFailureKind.FAILED, "download output is missing or unsafe")
        try:
            detected = validate_document(
                partial,
                content_type=content_type or MIME_BY_EXTENSION.get(Path(filename).suffix.lower()),
                pdfinfo_executable=str(self.pdfinfo),
                filename_hint=filename,
            )
        except PermissionError as error:
            self._raise_failure(ProviderFailureKind.AUTH_REQUIRED, str(error))
        except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            self._raise_failure(ProviderFailureKind.FAILED, f"document readability validation failed: {type(error).__name__}")
        final_name = Path(_safe_filename(filename)).stem + str(detected["extension"])
        final_path = _unique_path(paths.root, final_name)
        os.replace(partial, final_path)
        record = {
            "role": "document_file",
            "path": self._relpath(paths, final_path),
            "filename": final_path.name,
            "extension": detected["extension"],
            "mime": detected["content_type"],
            "size": final_path.stat().st_size,
            "sha256": _sha256_file(final_path),
            "source_url": pending.source_url,
            "resolved_url": pending.final_url,
            "provider": provider,
            "engine": provider,
            "engine_version": engine_version,
            "original_filename": original_filename,
            "export_format": export_format,
            "remote_identity": remote_identity or self._remote_identity_for_pending(pending),
            "validation": detected,
        }
        record["absolute_path"] = str(final_path)
        self._discard_if_regular(paths.state)
        self._discard_if_regular(paths.state.parent / (Path(filename).name + ".aria2"))
        return record

    def _validate_or_raise(self, path: Path, content_type: str | None, *, filename_hint: str | None = None) -> dict[str, Any]:
        try:
            return validate_document(path, content_type=content_type, pdfinfo_executable=str(self.pdfinfo), filename_hint=filename_hint)
        except PermissionError as error:
            self._raise_failure(ProviderFailureKind.AUTH_REQUIRED, str(error))
        except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            self._raise_failure(ProviderFailureKind.FAILED, f"document readability validation failed: {type(error).__name__}")

    def _fetch_google_file_record(self, paths: _DocumentPaths, pending: _PendingSource) -> dict[str, Any]:
        return self._fetch_google_file(paths, pending)

    def _relpath(self, paths: _DocumentPaths, path: Path) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(paths.temp):
            self._raise_failure(ProviderFailureKind.FAILED, "document artifact escaped Job temp")
        return resolved.relative_to(Path(paths.temp).parents[0]).as_posix()

    def _read_state(self, path: Path, identity: str) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and value.get("source_identity") == identity else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        if path.is_symlink():
            raise OSError("refusing to overwrite a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        if temporary.is_symlink():
            raise OSError("temporary metadata path cannot be a symlink")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _discard_if_regular(path: Path) -> None:
        if path.is_file() and not path.is_symlink():
            path.unlink()

    def _discard_partial(self, partial: Path, state: Path, control: Path) -> None:
        self._discard_if_regular(partial)
        self._discard_if_regular(control)
        self._discard_if_regular(state)

    def _run(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return self.command_runner(args, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            return subprocess.CompletedProcess(args, 124, stdout="", stderr=f"timeout: {type(error).__name__}")
        except OSError as error:
            return subprocess.CompletedProcess(args, 127, stdout="", stderr=type(error).__name__)

    @staticmethod
    def _saved_http_headers(path: Path) -> tuple[int, dict[str, str]]:
        try:
            text = path.read_text(encoding="iso-8859-1")
        except OSError:
            return 0, {}
        blocks = _header_blocks(text)
        return blocks[-1] if blocks else (0, {})

    @staticmethod
    def _writeout_value(stdout: str, name: str) -> str | None:
        prefix = name + "="
        return next((line.partition("=")[2] for line in stdout.splitlines() if line.startswith(prefix)), None)

    @staticmethod
    def _extension_from_mime(value: str | None) -> str | None:
        mime = (value or "").split(";", 1)[0].strip().lower()
        for extension, known in MIME_BY_EXTENSION.items():
            if mime == known:
                return extension
        guessed = mimetypes.guess_extension(mime) if mime else None
        return guessed if guessed and guessed.lower() in SUPPORTED_EXTENSIONS else None

    def _version(self, engine: str) -> str:
        if engine in self._versions:
            return self._versions[engine]
        executable = {"curl": self.curl, "aria2": self.aria2, "rclone": self.rclone, "gdown": self.gdown}.get(engine)
        if executable is None or not executable.is_file():
            version_value = "unavailable"
        else:
            if engine == "gdown":
                args = [str(executable), "--version"]
            elif engine == "rclone":
                args = [str(executable), "version"]
            else:
                args = [str(executable), "--version"]
            result = self._run(args, timeout=10)
            version_value = next((line.strip() for line in (result.stdout or "").splitlines() if line.strip()), "unknown")
            version_value = version_value[:200]
        self._versions[engine] = version_value
        return version_value

    @staticmethod
    def _raise_failure(kind: ProviderFailureKind, message: str, *, resume_token: str | None = None, retry_after_seconds: int | None = None, details: dict[str, Any] | None = None) -> None:
        raise ProviderFailure(kind, PROVIDER_NAME, message, retry_after_seconds=retry_after_seconds, resume_token=resume_token, details=details or {})


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, "", "-1"} else None
    except (TypeError, ValueError):
        return None


__all__ = ["DocumentProvider", "validate_document", "MANIFEST_NAME"]
