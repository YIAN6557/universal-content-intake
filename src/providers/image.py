"""Stage 4 gallery-dl adapter for IMAGE and IMAGE_SET Jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import site
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

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


PROVIDER_NAME = "gallery-dl"
IMAGE_CONTENT_TYPES = frozenset({ContentType.IMAGE, ContentType.IMAGE_SET})
_IMAGE_MIMES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "avif": "image/avif",
}
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
    (b"II*\x00", "tif", "image/tiff"),
    (b"MM\x00*", "tif", "image/tiff"),
)


def _safe_url(value: str) -> str:
    """Keep stable URL identity while never persisting query/fragment credentials."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))
    except ValueError:
        return "[invalid-url]"


def _safe_message(value: str, limit: int = 1200) -> str:
    value = re.sub(r"https?://[^\s\"']+", lambda match: _safe_url(match.group(0)), value, flags=re.IGNORECASE)
    value = re.sub(r"(?i)(token|secret|password|cookie|authorization)=([^\s&]+)", r"\1=[redacted]", value)
    return value[-limit:]


def classify_gallery_error(error_name: str, message: str) -> ProviderFailureKind:
    """Convert gallery-dl's structured error record into a stable Core kind."""
    text = f"{error_name} {message}".lower()
    if any(term in text for term in ("noextractorerror", "no suitable extractor", "unsupported url", "no extractor found")):
        return ProviderFailureKind.UNSUPPORTED
    if re.search(r"\b403\b", text) or "forbidden" in text:
        access_failure = classify_http_access_denial(403, text)
        if access_failure is not None:
            return access_failure
    if re.search(r"\b(404|410)\b", text) or any(term in text for term in ("not found", "notfounderror", "deleted", "does not exist", "removed")):
        return ProviderFailureKind.INVALID_URL
    if any(term in text for term in ("login", "authentication", "authorizationerror", "unauthorized", "forbidden", "http 401", "http 403", "requires account")):
        return ProviderFailureKind.AUTH_REQUIRED
    if re.search(r"\b(408|425|429|5\d\d)\b", text) or any(term in text for term in ("timeout", "timed out", "connection", "network", "temporarily unavailable", "sslerror", "remote end closed")):
        return ProviderFailureKind.NETWORK
    return ProviderFailureKind.FAILED


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _image_dimensions(path: Path, image_format: str) -> dict[str, int] | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(32)
            if image_format == "png" and len(header) >= 24:
                width = int.from_bytes(header[16:20], "big")
                height = int.from_bytes(header[20:24], "big")
            elif image_format == "gif" and len(header) >= 10:
                width = int.from_bytes(header[6:8], "little")
                height = int.from_bytes(header[8:10], "little")
            elif image_format == "bmp" and len(header) >= 26:
                width = int.from_bytes(header[18:22], "little", signed=True)
                height = int.from_bytes(header[22:26], "little", signed=True)
                width, height = abs(width), abs(height)
            elif image_format == "webp" and len(header) >= 30 and header[12:16] == b"VP8X":
                width = 1 + int.from_bytes(header[24:27], "little")
                height = 1 + int.from_bytes(header[27:30], "little")
            elif image_format in {"jpg", "jpeg"}:
                stream.seek(2)
                while True:
                    marker_prefix = stream.read(1)
                    if not marker_prefix:
                        return None
                    if marker_prefix != b"\xff":
                        continue
                    marker = stream.read(1)
                    while marker == b"\xff":
                        marker = stream.read(1)
                    if not marker:
                        return None
                    code = marker[0]
                    if code in {0xD8, 0xD9} or 0xD0 <= code <= 0xD7:
                        continue
                    length_data = stream.read(2)
                    if len(length_data) != 2:
                        return None
                    segment_length = int.from_bytes(length_data, "big")
                    if segment_length < 2:
                        return None
                    if 0xC0 <= code <= 0xCF and code not in {0xC4, 0xC8, 0xCC}:
                        segment = stream.read(5)
                        if len(segment) != 5:
                            return None
                        height = int.from_bytes(segment[1:3], "big")
                        width = int.from_bytes(segment[3:5], "big")
                        break
                    stream.seek(segment_length - 2, os.SEEK_CUR)
                
            elif image_format == "webp" and len(header) >= 25 and header[12:16] == b"VP8L" and header[20] == 0x2F:
                b1, b2, b3, b4 = header[21:25]
                width = 1 + b1 + ((b2 & 0x3F) << 8)
                height = 1 + ((b2 >> 6) | (b3 << 2) | ((b4 & 0x0F) << 10))
            elif image_format == "webp" and len(header) >= 30 and header[12:16] == b"VP8 ":
                start = header.find(b"\x9d\x01\x2a", 20, 30)
                if start < 0 or len(header) < start + 7:
                    return None
                width = int.from_bytes(header[start + 3:start + 5], "little") & 0x3FFF
                height = int.from_bytes(header[start + 5:start + 7], "little") & 0x3FFF
            else:
                return None
        return {"width": width, "height": height} if width > 0 and height > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _detect_image(path: Path) -> tuple[str, str, dict[str, int] | None] | None:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            return None
        with path.open("rb") as stream:
            header = stream.read(32)
        for signature, extension, mime in _SIGNATURES:
            if header.startswith(signature):
                return extension, mime, _image_dimensions(path, extension)
        if header.startswith(b"\xff\xd8\xff"):
            return "jpg", "image/jpeg", _image_dimensions(path, "jpg")
        if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "webp", "image/webp", _image_dimensions(path, "webp")
        if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
            return "avif", "image/avif", None
    except OSError:
        return None
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_component(value: str, default: str) -> str:
    normalized = unicodedata.normalize("NFKC", Path(value).name)
    chars = [character if character.isalnum() or character in "-_. " else "_" for character in normalized]
    result = re.sub(r"\s+", "_", "".join(chars)).strip(" ._")
    result = re.sub(r"_+", "_", result)[:100].rstrip(" ._")
    return result or default


def _record_url(media_url: str) -> str | None:
    safe = _safe_url(media_url)
    parts = urlsplit(safe)
    return safe if parts.scheme in {"http", "https"} and parts.netloc else None


def _normalize_message_data(value: Any) -> list[list[Any]]:
    if isinstance(value, list):
        if len(value) > 1 and isinstance(value[0], int):
            return [value]
        rows = [row for row in value if isinstance(row, list) and row]
        # gallery-dl normally writes a JSON array of messages. Accept the
        # equivalent JSON Lines form as well for version compatibility.
        if rows and isinstance(rows[0][0], list):
            rows = [row for batch in rows for row in batch if isinstance(row, list) and row]
        return rows
    if isinstance(value, str):
        payload = value.strip()
        if payload:
            try:
                return _normalize_message_data(json.loads(payload))
            except json.JSONDecodeError:
                pass
        rows: list[list[Any]] = []
        for line in value.splitlines():
            line = line.strip()
            if line:
                parsed = json.loads(line)
                if isinstance(parsed, list) and parsed:
                    if isinstance(parsed[0], list):
                        rows.extend(row for row in parsed if isinstance(row, list) and row)
                    else:
                        rows.append(parsed)
        return rows
    raise ValueError("gallery-dl JSON output is not a message list")


@dataclass(frozen=True)
class _ImageItem:
    source_order: int
    media_url: str
    identity: str
    original_filename: str
    extension: str
    dimensions: dict[str, int] | None
    author: str | None
    account: str | None
    post_id: str | None
    title: str | None
    caption: str | None
    extractor: str | None

    def public_metadata(self) -> dict[str, Any]:
        return {
            "source_order": self.source_order,
            "item_id": self.post_id,
            "item_identity": self.identity,
            "original_filename": _safe_message(self.original_filename),
            "extension": self.extension,
            "dimensions": self.dimensions,
            "author": _safe_message(self.author) if self.author else None,
            "account": _safe_message(self.account) if self.account else None,
            "post_id": self.post_id,
            "title": _safe_message(self.title) if self.title else None,
            "caption": _safe_message(self.caption) if self.caption else None,
            "media_url": _record_url(self.media_url),
            "extractor": self.extractor,
            "extraction_status": "available",
        }


class ImageProvider(ContentProvider):
    """Provider adapter; Core remains solely responsible for Job state changes."""

    name = PROVIDER_NAME
    capability = ProviderCapability(IMAGE_CONTENT_TYPES, True, True, True)

    def __init__(
        self,
        *,
        gallery_dl_path: str | None = None,
        curl_path: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        command_timeout: int = 180,
    ) -> None:
        self.gallery_dl_path = gallery_dl_path or shutil.which("gallery-dl")
        self.curl_path = curl_path or shutil.which("curl")
        self._runner = runner or subprocess.run
        self.command_timeout = command_timeout
        self._pending: dict[str, tuple[str, tuple[_ImageItem, ...], dict[str, Any]]] = {}

    def probe(self, job: Job) -> ProviderResult:
        source_url = job.source_url.strip()
        self._validate_source_url(source_url)
        if job.declared_content_type is not None and job.declared_content_type not in IMAGE_CONTENT_TYPES:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "ImageProvider requires an IMAGE or IMAGE_SET Job.")
        if job.resolved_content_type not in IMAGE_CONTENT_TYPES | {ContentType.UNKNOWN}:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "ImageProvider requires an IMAGE or IMAGE_SET Job.")

        try:
            payload = self._gallery_json(job, source_url)
            items, directory_metadata = self._parse_gallery_items(payload)
            if not items:
                raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "gallery-dl did not identify any image files.")
        except ProviderFailure as failure:
            if failure.kind is not ProviderFailureKind.UNSUPPORTED:
                raise
            direct = self._probe_direct_image(job, source_url)
            if direct is None:
                raise failure
            _status, mime = direct
            item = self._direct_item(source_url, mime)
            items = (item,)
            directory_metadata = {"category": "direct-image", "subcategory": "image"}
            curl_fallback = True
            direct_type = mime
        else:
            curl_fallback = False
            direct_type = None

        if not items:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "gallery-dl did not identify any image files.")
        reported_count = _positive_int(directory_metadata.get("count_photos"))
        candidate_count = _positive_int(directory_metadata.get("count"))
        item_ids = {item.post_id for item in items if item.post_id}
        if reported_count is None and candidate_count and len(item_ids) == 1 and candidate_count >= len(items):
            reported_count = candidate_count
        expected_count = reported_count or len(items)
        actual_type = ContentType.IMAGE if expected_count == 1 else ContentType.IMAGE_SET
        category = directory_metadata.get("category")
        subcategory = directory_metadata.get("subcategory")
        extractor = f"{category}/{subcategory}" if category and subcategory else str(category or items[0].extractor or "unknown")
        source_identity = self._source_identity(source_url, extractor, items, expected_count)
        probe_facts = {
            "curl_fallback": curl_fallback,
            "direct_content_type": direct_type,
            "extractor": extractor,
            "source_subcategory": str(subcategory) if subcategory is not None else None,
            "expected_count": expected_count,
        }
        self._pending[job.job_id] = (source_identity, items, probe_facts)
        normalized_items = [item.public_metadata() for item in items]
        metadata = {
            "probe": {
                "source_url": _safe_url(source_url),
                "extractor": extractor,
                "source_subcategory": str(subcategory) if subcategory is not None else None,
                "content_type": actual_type.value,
                "item_count": expected_count,
                "enumerated_item_count": len(items),
                "items": normalized_items,
                "quality_policy": "gallery-dl native original; no resizing or recompression",
                "curl_fallback": curl_fallback,
                "direct_content_type": direct_type if curl_fallback else None,
                "source_identity": source_identity,
            }
        }
        return ProviderResult(metadata=metadata, resume_token=source_identity)

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        self._validate_source_url(job.source_url)
        pending = self._pending.get(job.job_id)
        if pending is None:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image fetch requires a fresh structured Probe result.", resume_token=resume_token)
        source_identity, items, probe_facts = pending
        if not resume_token or resume_token != source_identity:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image source identity changed; cached artifacts will not be reused.", resume_token=source_identity)

        temp_root = self._job_temp_root(job)
        source_root = self._contained_path(temp_root, f"uci_images/{source_identity}")
        files_root = self._contained_path(temp_root, f"uci_images/{source_identity}/files")
        runtime_root = self._contained_path(temp_root, f"uci_images/{source_identity}/runtime")
        self._ensure_directory(source_root)
        self._ensure_directory(files_root)
        self._ensure_directory(runtime_root)
        manifest_path = self._contained_path(temp_root, f"uci_images/{source_identity}/manifest.json")
        expected_count = int(probe_facts.get("expected_count") or len(items))
        manifest = self._load_manifest(manifest_path, source_identity, job.source_url, expected_count)
        previous_by_identity = {
            str(row.get("item_identity")): row
            for row in manifest["items"]
            if isinstance(row, dict) and row.get("item_identity")
        }

        artifacts: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        reused_count = 0
        downloaded_count = 0
        manifest["items"] = []
        manifest["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._write_manifest(manifest_path, manifest)

        for item in items:
            old = previous_by_identity.get(item.identity)
            reused = self._verified_existing_artifact(temp_root, old) if old else None
            if reused is not None:
                record = self._artifact_record(job, item, reused, provider=str(old.get("provider") or ("curl" if probe_facts["curl_fallback"] else self.name)))
                reused_count += 1
            else:
                try:
                    staged = self._download_item(job, item, probe_facts, runtime_root, source_root)
                    detected = _detect_image(staged)
                    if detected is None:
                        raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Downloaded file is empty or its image signature is not recognized.", resume_token=source_identity)
                    extension, mime_type, parsed_dimensions = detected
                    desired = self._desired_filename(item, extension)
                    final = self._move_without_overwrite(staged, files_root, desired)
                    record = self._artifact_record(job, item, final, provider="curl" if probe_facts["curl_fallback"] else self.name)
                    record["extension"] = extension
                    record["media_type"] = mime_type
                    record["dimensions"] = item.dimensions or parsed_dimensions
                    downloaded_count += 1
                except (ProviderFailure, OSError) as error:
                    if isinstance(error, ProviderFailure):
                        failure = error
                    else:
                        failure = ProviderFailure(
                            ProviderFailureKind.FAILED,
                            self.name,
                            f"Image item {item.source_order} could not be stored in Job temp.",
                            details={"cause": type(error).__name__},
                        )
                    failures.append({
                        "source_order": item.source_order,
                        "item_id": item.post_id,
                        "kind": failure.kind.value,
                        "reason": _safe_message(failure.message),
                    })
                    manifest["items"].append({
                        "source_order": item.source_order,
                        "item_identity": item.identity,
                        "status": "failed",
                        "provider": "curl" if probe_facts["curl_fallback"] else self.name,
                        "failure_kind": failure.kind.value,
                        "failure_reason": _safe_message(failure.message),
                    })
                    manifest["failed_items"] = list(failures)
                    self._write_manifest(manifest_path, manifest)
                    continue
            artifacts.append(record)
            manifest["items"].append({
                "source_order": item.source_order,
                "item_identity": item.identity,
                "item_id": item.post_id,
                "status": "verified",
                "artifact_id": record["artifact_id"],
                "path": record["path"],
                "filename": record["filename"],
                "extension": record["extension"],
                "size": record["size"],
                "sha256": record["sha256"],
                "dimensions": record["dimensions"],
                "provider": record["provider"],
                "source_url": _safe_url(job.source_url),
                "media_url": _record_url(item.media_url),
            })
            manifest["successful_items"] = [row for row in manifest["items"] if row.get("status") == "verified"]
            manifest["failed_items"] = list(failures)
            self._write_manifest(manifest_path, manifest)

        if expected_count > len(items):
            failures.extend({
                "source_order": order,
                "item_id": None,
                "kind": ProviderFailureKind.PARTIAL.value,
                "reason": f"gallery-dl reported {expected_count} images but returned only {len(items)} structured image records.",
            } for order in range(len(items) + 1, expected_count + 1))

        artifacts.sort(key=lambda item: int(item["source_order"]))
        successful_items = [item for item in artifacts]
        if failures:
            partial = {
                "expected_count": expected_count,
                "successful_items": successful_items,
                "failed_items": failures,
                "manifest_path": manifest_path.relative_to(Path(job.workspace_path).resolve()).as_posix(),
            }
            if successful_items:
                manifest["status"] = "partial"
                manifest["successful_items"] = successful_items
                manifest["failed_items"] = failures
                self._write_manifest(manifest_path, manifest)
                raise ProviderFailure(
                    ProviderFailureKind.PARTIAL,
                    self.name,
                    f"Image set partially downloaded: {len(successful_items)}/{len(items)} items verified.",
                    resume_token=source_identity,
                    details={"image_partial": partial},
                )
            first_kind = ProviderFailureKind(failures[0]["kind"])
            raise ProviderFailure(
                first_kind,
                self.name,
                failures[0]["reason"],
                resume_token=source_identity,
                details={"image_partial": partial},
            )

        manifest["status"] = "complete"
        manifest["successful_items"] = successful_items
        manifest["failed_items"] = []
        self._write_manifest(manifest_path, manifest)
        produced = tuple(ProducedArtifact(item["path"], "IMAGE", item["media_type"]) for item in artifacts)
        return ProviderResult(
            metadata={"probe": self._safe_probe_from_items(job, items, source_identity, probe_facts), "fetch": {
                "status": "complete",
                "expected_count": expected_count,
                "successful_count": len(artifacts),
                "failed_count": 0,
                "downloaded_count": downloaded_count,
                "reused_count": reused_count,
                "manifest_path": manifest_path.relative_to(Path(job.workspace_path).resolve()).as_posix(),
                "artifacts": artifacts,
            }},
            artifacts=produced,
            resume_token=source_identity,
        )

    def _gallery_json(self, job: Job, source_url: str) -> list[list[Any]]:
        if not self.gallery_dl_path:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "gallery-dl executable is unavailable.")
        command = [self.gallery_dl_path, "--ignore-config", "--quiet", "--no-colors", "--dump-json", "--simulate", source_url]
        try:
            runtime_root = self._probe_runtime_root(job)
            result = self._runner(command, capture_output=True, text=True, timeout=self.command_timeout, env=self._runtime_environment(runtime_root))
        except subprocess.TimeoutExpired as error:
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, "gallery-dl Probe timed out after its retry window.") from error
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "gallery-dl Probe could not be started.", details={"cause": type(error).__name__}) from error
        try:
            rows = _normalize_message_data(result.stdout)
        except (ValueError, json.JSONDecodeError):
            if result.returncode != 0:
                kind = classify_gallery_error("ProcessError", result.stderr or result.stdout)
                raise ProviderFailure(kind, self.name, _safe_message(result.stderr or "gallery-dl Probe failed."))
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "gallery-dl returned invalid structured JSON.")
        for row in rows:
            if row[0] == -1:
                details = row[1] if len(row) > 1 and isinstance(row[1], dict) else {}
                error_name = str(details.get("error") or "GalleryError")
                message = str(details.get("message") or "gallery-dl extractor failed")
                kind = classify_gallery_error(error_name, message)
                raise ProviderFailure(kind, self.name, _safe_message(f"{error_name}: {message}"), details={"extractor_error": error_name})
        if result.returncode != 0:
            kind = classify_gallery_error("ProcessError", result.stderr or result.stdout)
            raise ProviderFailure(kind, self.name, _safe_message(result.stderr or "gallery-dl Probe failed."))
        return rows

    def _parse_gallery_items(self, rows: Sequence[Sequence[Any]]) -> tuple[tuple[_ImageItem, ...], dict[str, Any]]:
        items: list[_ImageItem] = []
        directory_metadata: dict[str, Any] = {}
        for row in rows:
            try:
                message_type = int(row[0])
            except (TypeError, ValueError):
                continue
            if message_type == 2 and len(row) >= 2 and isinstance(row[1], dict):
                directory_metadata = dict(row[1])
                continue
            if message_type != 3 or len(row) < 3 or not isinstance(row[2], dict):
                continue
            media_url = str(row[1])
            parts = urlsplit(media_url)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                continue
            data = {**directory_metadata, **row[2]}
            category = str(data.get("category") or directory_metadata.get("category") or "unknown")
            subcategory = str(data.get("subcategory") or directory_metadata.get("subcategory") or "unknown")
            extension = str(data.get("extension") or Path(parts.path).suffix.lstrip(".") or "bin").lower()
            if not re.fullmatch(r"[a-z0-9]{1,10}", extension):
                extension = "bin"
            raw_filename = str(data.get("filename") or Path(parts.path).name or f"image-{len(items) + 1}")
            original_filename = raw_filename if Path(raw_filename).suffix else f"{raw_filename}.{extension}"
            user = data.get("user") if isinstance(data.get("user"), dict) else {}
            owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
            blog = data.get("blog") if isinstance(data.get("blog"), dict) else {}
            author = (data.get("author") or user.get("realname") or user.get("username")
                      or owner.get("realname") or owner.get("username")
                      or blog.get("title") or blog.get("name"))
            account = (data.get("account") or data.get("blog_name")
                       or user.get("path_alias") or user.get("username")
                       or owner.get("path_alias") or owner.get("username")
                       or blog.get("name") or blog.get("url"))
            post_id = data.get("id") or data.get("post_id")
            source_order = len(items) + 1
            locator = _safe_url(media_url)
            identity_material = json.dumps({
                "order": source_order,
                "id": str(post_id) if post_id is not None else None,
                "locator": locator,
                "filename": original_filename,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            item_identity = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
            items.append(_ImageItem(
                source_order=source_order,
                media_url=media_url,
                identity=item_identity,
                original_filename=original_filename,
                extension=extension,
                dimensions={"width": _positive_int(data.get("width")), "height": _positive_int(data.get("height"))}
                    if _positive_int(data.get("width")) and _positive_int(data.get("height")) else None,
                author=str(author) if author else None,
                account=str(account) if account else None,
                post_id=str(post_id) if post_id is not None else None,
                title=str(data.get("title") or data.get("summary")) if data.get("title") or data.get("summary") else None,
                caption=str(data.get("description") or data.get("caption")) if data.get("description") or data.get("caption") else None,
                extractor=f"{category}/{subcategory}",
            ))
        return tuple(items), directory_metadata

    def _probe_direct_image(self, job: Job, source_url: str) -> tuple[str, str] | None:
        if not self.curl_path:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "curl executable is unavailable for direct-image fallback.")
        command = [
            self.curl_path, "--head", "--location", "--silent", "--show-error",
            "--max-time", "20", "--max-redirs", "8",
            "--write-out", "\\n__UCI_STATUS__:%{http_code}\\n__UCI_CONTENT_TYPE__:%{content_type}\\n",
            source_url,
        ]
        try:
            runtime_root = self._probe_runtime_root(job)
            response = self._runner(command, capture_output=True, text=True, timeout=25, env=self._runtime_environment(runtime_root))
        except subprocess.TimeoutExpired as error:
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, "Direct-image HTTP metadata check timed out.") from error
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "curl could not inspect the direct-image response.", details={"cause": type(error).__name__}) from error
        status_match = re.search(r"__UCI_STATUS__:(\d{3})", response.stdout or "")
        mime_match = re.search(r"__UCI_CONTENT_TYPE__:([^\r\n]+)", response.stdout or "")
        status = int(status_match.group(1)) if status_match else 0
        mime = mime_match.group(1).strip().lower().split(";", 1)[0] if mime_match else ""
        if response.returncode != 0 and status == 0:
            kind = classify_gallery_error("CurlProbe", response.stderr or "network error")
            if kind is ProviderFailureKind.FAILED:
                kind = ProviderFailureKind.NETWORK
            raise ProviderFailure(kind, self.name, _safe_message(response.stderr or "Direct-image HTTP metadata check failed."))
        access_failure = classify_http_access_denial(status, (response.stdout or "") + " " + (response.stderr or ""))
        if access_failure is not None:
            raise ProviderFailure(access_failure, self.name, f"Direct image access failed (HTTP {status}).")
        if status in {404, 410}:
            raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Direct image is unavailable (HTTP {status}).")
        if status == 429 or status >= 500:
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"Direct image is temporarily unavailable (HTTP {status}).")
        if 200 <= status < 300 and mime.startswith("image/") and mime in set(_IMAGE_MIMES.values()):
            return str(status), mime
        return None

    def _download_item(
        self,
        job: Job,
        item: _ImageItem,
        probe_facts: Mapping[str, Any],
        runtime_root: Path,
        source_root: Path,
    ) -> Path:
        stage_dir = source_root / "staging" / f"item-{item.source_order:05d}"
        self._ensure_directory(stage_dir)
        if probe_facts.get("curl_fallback"):
            return self._download_direct(job, item, stage_dir, runtime_root, str(probe_facts.get("direct_content_type") or ""))
        if not self.gallery_dl_path:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "gallery-dl executable is unavailable.")
        command = [
            self.gallery_dl_path,
            "--ignore-config", "--no-colors",
            "--retries", "4",
            "--directory", str(stage_dir),
            "--filename", f"download.{{extension}}",
            item.media_url,
        ]
        try:
            result = self._runner(command, capture_output=True, text=True, timeout=self.command_timeout, env=self._runtime_environment(runtime_root))
        except subprocess.TimeoutExpired as error:
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"gallery-dl timed out for image {item.source_order}.") from error
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"gallery-dl could not start for image {item.source_order}.", details={"cause": type(error).__name__}) from error
        if result.returncode != 0:
            kind = classify_gallery_error("DownloadError", result.stderr or result.stdout)
            raise ProviderFailure(kind, self.name, _safe_message(result.stderr or f"gallery-dl failed for image {item.source_order}."))
        candidates = [path for path in stage_dir.iterdir() if path.is_file() and not path.is_symlink() and not path.name.endswith(".part")]
        if len(candidates) != 1:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"gallery-dl produced {len(candidates)} files for image {item.source_order}; expected exactly one.")
        if _detect_image(candidates[0]) is None:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"gallery-dl output for image {item.source_order} is not a recognized image.")
        return candidates[0]

    def _download_direct(self, job: Job, item: _ImageItem, stage_dir: Path, runtime_root: Path, expected_mime: str) -> Path:
        if not self.curl_path:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "curl executable is unavailable for direct-image fallback.")
        staged = stage_dir / "download.part"
        if staged.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Direct-image partial path is unsafe.")
        command = [
            self.curl_path, "--location", "--silent", "--show-error", "--fail",
            "--max-time", str(self.command_timeout), "--max-redirs", "8",
            "--write-out", "\\n__UCI_STATUS__:%{http_code}\\n__UCI_CONTENT_TYPE__:%{content_type}\\n",
            "--output", str(staged), item.media_url,
        ]
        if staged.exists() and staged.stat().st_size > 0:
            command[1:1] = ["--continue-at", "-"]
        try:
            result = self._runner(command, capture_output=True, text=True, timeout=self.command_timeout + 10, env=self._runtime_environment(runtime_root))
        except subprocess.TimeoutExpired as error:
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, "curl direct-image download timed out.", resume_token=None) from error
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "curl could not start the direct-image download.", details={"cause": type(error).__name__}) from error
        if result.returncode != 0:
            status = self._curl_status(result.stdout)
            access_failure = classify_http_access_denial(status, result.stderr or "")
            if access_failure is not None:
                kind = access_failure
            elif status in {404, 410}:
                kind = ProviderFailureKind.INVALID_URL
            else:
                kind = ProviderFailureKind.NETWORK if status == 0 or status == 429 or status >= 500 else ProviderFailureKind.FAILED
            raise ProviderFailure(kind, self.name, _safe_message(result.stderr or "curl direct-image download failed."))
        status = self._curl_status(result.stdout)
        mime = self._curl_mime(result.stdout)
        detected = _detect_image(staged)
        if status < 200 or status >= 300 or not detected or not mime.startswith("image/") or mime not in set(_IMAGE_MIMES.values()):
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "curl response failed image MIME, signature, or non-empty-file validation.")
        extension, detected_mime, _ = detected
        if expected_mime and expected_mime != detected_mime:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Direct-image MIME changed between Probe and Fetch.")
        if mime != detected_mime:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "curl Content-Type does not match the downloaded image signature.")
        return staged

    @staticmethod
    def _curl_status(stdout: str) -> int:
        match = re.search(r"__UCI_STATUS__:(\d{3})", stdout or "")
        return int(match.group(1)) if match else 0

    @staticmethod
    def _curl_mime(stdout: str) -> str:
        match = re.search(r"__UCI_CONTENT_TYPE__:([^\r\n]+)", stdout or "")
        return match.group(1).strip().lower().split(";", 1)[0] if match else ""

    def _direct_item(self, source_url: str, mime: str) -> _ImageItem:
        extension = next((suffix for suffix, candidate in _IMAGE_MIMES.items() if candidate == mime), "jpg")
        basename = Path(urlsplit(source_url).path).name or "image"
        raw_suffix = Path(basename).suffix.lower().lstrip(".")
        if raw_suffix in _IMAGE_MIMES:
            extension = raw_suffix
        original = basename if Path(basename).suffix else f"{basename}.{extension}"
        identity = hashlib.sha256(json.dumps({"direct": _safe_url(source_url), "extension": extension}, sort_keys=True).encode()).hexdigest()
        return _ImageItem(1, source_url, identity, original, extension, None, None, None, None, None, None, "direct-image")

    def _source_identity(self, source_url: str, extractor: str, items: Sequence[_ImageItem], expected_count: int) -> str:
        payload = {
            # Hash the exact locator (including any transient query) without
            # persisting credentials or signed URL values in Job metadata.
            "source_url_fingerprint": hashlib.sha256(source_url.encode("utf-8")).hexdigest(),
            "extractor": extractor,
            "expected_count": expected_count,
            "items": [{"order": item.source_order, "identity": item.identity} for item in items],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _safe_probe_from_items(
        self,
        job: Job,
        items: Sequence[_ImageItem],
        source_identity: str,
        probe_facts: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "source_url": _safe_url(job.source_url),
            "extractor": probe_facts.get("extractor"),
            "source_subcategory": probe_facts.get("source_subcategory"),
            "content_type": ContentType.IMAGE.value if int(probe_facts.get("expected_count") or len(items)) == 1 else ContentType.IMAGE_SET.value,
            "item_count": int(probe_facts.get("expected_count") or len(items)),
            "enumerated_item_count": len(items),
            "items": [item.public_metadata() for item in items],
            "quality_policy": "gallery-dl native original; no resizing or recompression",
            "source_identity": source_identity,
            "curl_fallback": bool(probe_facts.get("curl_fallback")),
            "direct_content_type": probe_facts.get("direct_content_type"),
        }

    def _artifact_record(self, job: Job, item: _ImageItem, path: Path, *, provider: str) -> dict[str, Any]:
        temp_root = self._job_temp_root(job)
        self._assert_contained(temp_root, path)
        detected = _detect_image(path)
        if detected is None:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"Verified artifact for image {item.source_order} is no longer a readable image.")
        extension, mime, parsed_dimensions = detected
        relative_path = path.relative_to(Path(job.workspace_path).resolve()).as_posix()
        sha256 = _sha256_file(path)
        return {
            "artifact_id": f"img-{hashlib.sha256((item.identity + sha256).encode()).hexdigest()[:24]}",
            "source_order": item.source_order,
            "path": relative_path,
            "filename": path.name,
            "extension": extension,
            "size": path.stat().st_size,
            "sha256": sha256,
            "provider": provider,
            "source_url": _safe_url(job.source_url),
            "media_url": _record_url(item.media_url),
            "dimensions": item.dimensions or parsed_dimensions,
            "media_type": mime,
            "original_filename": item.original_filename,
            "item_id": item.post_id,
        }

    def _verified_existing_artifact(self, temp_root: Path, row: Mapping[str, Any]) -> Path | None:
        relative = row.get("path")
        expected_hash = row.get("sha256")
        expected_size = row.get("size")
        if not isinstance(relative, str) or not isinstance(expected_hash, str) or not isinstance(expected_size, int):
            return None
        try:
            relative_path = PurePosixPath(relative)
            if relative_path.is_absolute() or not relative_path.parts or relative_path.parts[0] != "temp":
                return None
            lexical_path = temp_root.parent.joinpath(*relative_path.parts)
            self._assert_contained(temp_root, lexical_path)
            if any(part.is_symlink() for part in (lexical_path, *lexical_path.parents) if part != temp_root.parent):
                return None
            path = lexical_path.resolve(strict=True)
            self._assert_contained(temp_root, path)
            if not path.is_file() or path.stat().st_size != expected_size:
                return None
            if _sha256_file(path) != expected_hash or _detect_image(path) is None:
                return None
            return path
        except (OSError, ValueError):
            return None

    def _load_manifest(self, path: Path, source_identity: str, source_url: str, expected_count: int) -> dict[str, Any]:
        if path.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image resume manifest is a symlink and cannot be trusted.")
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image resume manifest is unreadable.", details={"cause": type(error).__name__}) from error
            if not isinstance(value, dict) or value.get("source_identity") != source_identity:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image resume manifest identity does not match this source.")
            if value.get("source_url") != _safe_url(source_url):
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image resume manifest source URL does not match this Job.")
            if not isinstance(value.get("items"), list):
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image resume manifest item list is invalid.")
            value["expected_count"] = expected_count
            value["status"] = "downloading"
            return value
        return {
            "schema_version": 1,
            "provider": self.name,
            "source_url": _safe_url(source_url),
            "source_identity": source_identity,
            "expected_count": expected_count,
            "status": "downloading",
            "items": [],
            "successful_items": [],
            "failed_items": [],
        }

    def _write_manifest(self, path: Path, value: Mapping[str, Any]) -> None:
        if path.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image resume manifest path became a symlink.")
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        descriptor, pending_name = tempfile.mkstemp(prefix=".manifest-", suffix=".pending", dir=path.parent)
        pending = Path(pending_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        except BaseException as error:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(error, OSError):
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image resume manifest could not be written.", details={"cause": type(error).__name__}) from error
            raise

    def _move_without_overwrite(self, staged: Path, destination_root: Path, desired_name: str) -> Path:
        base = Path(desired_name).stem
        extension = Path(desired_name).suffix
        number = 1
        while number < 10000:
            name = desired_name if number == 1 else f"{base}-{number}{extension}"
            target = destination_root / name
            self._assert_contained(destination_root, target)
            try:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                number += 1
                continue
            try:
                with os.fdopen(descriptor, "wb") as destination, staged.open("rb") as source:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                staged.unlink()
            except BaseException:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return target
        raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Unable to choose a non-colliding image filename.")

    def _desired_filename(self, item: _ImageItem, extension: str) -> str:
        raw = Path(item.original_filename).name
        stem = Path(raw).stem if Path(raw).suffix else raw
        safe = _safe_component(stem, f"image-{item.source_order:04d}")
        return f"{item.source_order:04d}_{safe}.{extension}"

    @staticmethod
    def _validate_source_url(source_url: str) -> None:
        try:
            parts = urlsplit(source_url)
            valid = parts.scheme.lower() in {"http", "https"} and bool(parts.hostname) and parts.username is None and parts.password is None
        except ValueError:
            valid = False
        if not valid:
            raise ProviderFailure(ProviderFailureKind.INVALID_URL, PROVIDER_NAME, "Image source must be an HTTP or HTTPS URL without embedded credentials.")

    def _job_temp_root(self, job: Job) -> Path:
        workspace = Path(job.workspace_path).expanduser()
        temp = Path(job.temp_path).expanduser()
        if workspace.is_symlink() or temp.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image Job workspace paths cannot be symlinks.")
        try:
            root = workspace.resolve(strict=True)
            temp_root = temp.resolve(strict=True)
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image Job temp directory is unavailable.", details={"cause": type(error).__name__}) from error
        if temp_root != root / "temp" or not temp_root.is_dir():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image downloads must remain inside the Job temp directory.")
        return temp_root

    def _contained_path(self, root: Path, relative: str) -> Path:
        candidate_relative = PurePosixPath(relative)
        if candidate_relative.is_absolute() or any(part in {"", ".", ".."} for part in candidate_relative.parts):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Unsafe image temp-relative path.")
        candidate = root.joinpath(*candidate_relative.parts)
        self._assert_contained(root, candidate)
        for part in (candidate, *candidate.parents):
            if part == root:
                break
            if part.is_symlink():
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image temp path contains a symlink.")
        return candidate

    def _assert_contained(self, root: Path, candidate: Path) -> None:
        try:
            candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image artifact path escapes the Job temp directory.") from error

    def _ensure_directory(self, path: Path) -> None:
        if path.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image temp directory cannot be a symlink.")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image Job temp directory could not be created.", details={"cause": type(error).__name__}) from error
        if not path.is_dir():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Image temp path is not a directory.")
        self._assert_contained(self._job_temp_root_for_path(path), path)

    @staticmethod
    def _job_temp_root_for_path(path: Path) -> Path:
        # Every managed provider path includes this fixed temp child.
        current = path.resolve(strict=True)
        while current.name != "temp" and current.parent != current:
            current = current.parent
        if current.name != "temp":
            raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, "Image temp directory root could not be identified.")
        return current

    @staticmethod
    def _runtime_environment(runtime_root: Path) -> dict[str, str]:
        home = runtime_root / "home"
        cache = runtime_root / "cache"
        try:
            home.mkdir(parents=True, exist_ok=True)
            cache.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, "Isolated gallery-dl runtime paths could not be created.", details={"cause": type(error).__name__}) from error
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(cache),
            # Keep the explicitly installed gallery-dl user package importable
            # while HOME/XDG remain isolated from browser/config state.
            "PYTHONUSERBASE": site.getuserbase(),
        })
        return environment

    def _probe_runtime_root(self, job: Job) -> Path:
        temp_root = self._job_temp_root(job)
        runtime_root = self._contained_path(temp_root, "uci_images/probe-runtime")
        self._ensure_directory(runtime_root)
        return runtime_root


__all__ = ["ImageProvider", "classify_gallery_error"]
