"""Stage 4 SingleFile + Trafilatura + Chrome Webpage Provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import certifi

from src.core import compat
from src.core.job import ContentType, Job
from src.providers.article import _is_login_wall, extract_trafilatura_markdown, parse_html
from src.providers.base import (
    ContentProvider,
    ProducedArtifact,
    ProviderCapability,
    ProviderFailure,
    ProviderFailureKind,
    ProviderResult,
    classify_http_access_denial,
)


PROVIDER_NAME = "singlefile-webpage"
WEBPAGE_CONTENT_TYPES = frozenset({ContentType.WEBPAGE})
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
MAX_HTML_BYTES = 128 * 1024 * 1024
SINGLE_FILE_PACKAGE = compat.data_dir() / "runtime" / "single-file-cli" / "node_modules" / "single-file-cli"
# The package's Deno entry runs by its shebang on macOS; Windows has no shebangs, so Node runs its Node entry.
DEFAULT_SINGLE_FILE_PATH = SINGLE_FILE_PACKAGE / ("single-file-node.js" if compat.WINDOWS else "single-file")
DEFAULT_CHROME_PATH = compat.chrome_path()
USER_AGENT = "UniversalContentIntake/1.0 (anonymous webpage; no browser session)"
ARTICLE_HTML = "article.html"
ARTICLE_MARKDOWN = "article.md"
ARTICLE_PDF = "article.pdf"
MANIFEST_NAME = "webpage-manifest.json"

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path, "[redacted]" if parts.query else "", ""))
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
    return _sha256_bytes(b"uci-webpage-source-v1\0" + url.encode("utf-8"))


def _relative_to_job(job: Job, path: Path) -> str:
    return path.resolve().relative_to(Path(job.workspace_path).resolve()).as_posix()


def _safe_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    pending = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    except BaseException:
        try:
            pending.unlink()
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class _JobPaths:
    workspace: Path
    temp: Path
    html: Path
    markdown: Path
    pdf: Path
    manifest: Path
    runtime: Path


class WebpageProvider(ContentProvider):
    """Archive one anonymous webpage and derive Markdown and PDF artifacts."""

    name = PROVIDER_NAME
    capability = ProviderCapability(WEBPAGE_CONTENT_TYPES, True, True, True)

    def __init__(
        self,
        *,
        single_file_executable: Path | str | None = None,
        chrome_executable: Path | str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0 or retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("invalid WebpageProvider execution limits")
        configured_singlefile = single_file_executable or os.environ.get("UCI_SINGLE_FILE_PATH")
        configured_chrome = chrome_executable or os.environ.get("UCI_CHROME_PATH")
        self.single_file_executable = Path(configured_singlefile or DEFAULT_SINGLE_FILE_PATH).expanduser()
        if configured_singlefile is None and not self.single_file_executable.is_file():
            located = None if compat.WINDOWS else shutil.which("single-file")
            if located:
                self.single_file_executable = Path(located)
        self.chrome_executable = Path(configured_chrome or DEFAULT_CHROME_PATH).expanduser()
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.command_runner = command_runner
        self._single_file_version_cache: str | None = None
        self._chrome_version_cache: str | None = None
        try:
            self.trafilatura_version = package_version("trafilatura")
        except PackageNotFoundError:
            self.trafilatura_version = "unknown"
        self._pending: dict[str, str] = {}

    def probe(self, job: Job) -> ProviderResult:
        """Validate and identify the source without network access or Job mutation."""
        source_url = job.source_url.strip()
        if not _valid_http_url(source_url):
            raise ProviderFailure(
                ProviderFailureKind.INVALID_URL,
                self.name,
                f"Webpage source must be an absolute HTTP(S) URL: {_safe_url(source_url)}",
            )
        if job.declared_content_type not in {None, ContentType.WEBPAGE}:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "WebpageProvider requires a WEBPAGE Job.")
        if job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.WEBPAGE}:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "WebpageProvider cannot handle the resolved content type.")
        identity = _source_identity(source_url)
        self._pending[job.job_id] = identity
        host = urlsplit(source_url).hostname or ""
        return ProviderResult(
            metadata={
                "probe": {
                    "content_type": ContentType.WEBPAGE.value,
                    "source_url": source_url,
                    "source_identity": identity,
                    "hostname": host,
                    "platform": host,
                    "network_fetch_performed": False,
                    "provider": self.name,
                }
            },
            resume_token=identity,
        )

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        source_url = job.source_url.strip()
        identity = self._pending.get(job.job_id)
        if not identity or identity != resume_token or identity != _source_identity(source_url):
            raise ProviderFailure(
                ProviderFailureKind.FAILED,
                self.name,
                "Webpage fetch requires a matching fresh Probe identity.",
                resume_token=resume_token,
            )
        try:
            paths = self._job_paths(job)
        except (OSError, ValueError) as error:
            raise ProviderFailure(
                ProviderFailureKind.FAILED,
                self.name,
                f"Unsafe Webpage Job temp workspace: {error}",
                resume_token=identity,
            ) from error

        manifest = self._read_manifest(paths, source_url, identity)
        previous_stages = manifest["stages"]
        reuse: dict[str, str] = {}

        html_record = self._verified_artifact(job, paths, previous_stages.get("html"), "html")
        if html_record is not None:
            reuse["html"] = "verified"
        else:
            self._require_single_file()
            self._preflight(source_url, identity)
            old_html_hash = self._stage_hash(previous_stages.get("html"))
            self._capture_html(paths, source_url, identity)
            html_record = self._artifact_record(job, paths.html, "html", source_url, "single-file-cli", self._single_file_version(paths))
            if old_html_hash != html_record["sha256"]:
                self._unlink_if_regular(paths.markdown)
                self._unlink_if_regular(paths.pdf)
                previous_stages.pop("markdown", None)
                previous_stages.pop("pdf", None)
            manifest["stages"]["html"] = {"artifact": html_record, "validated_at": _now()}
            self._update_manifest(paths, manifest)
            reuse["html"] = "generated"

        html_hash = str(html_record["sha256"])
        md_stage = previous_stages.get("markdown")
        md_record = self._verified_artifact(job, paths, md_stage, "markdown", input_html_sha256=html_hash)
        if md_record is not None:
            reuse["markdown"] = "verified"
            md_metadata = dict(md_stage.get("metadata") or {})
        else:
            extraction = self._extract_markdown(paths.html, source_url, identity)
            image_references = self._saved_page_image_references(paths.html, source_url)
            markdown = self._append_image_references(extraction.markdown, image_references)
            md_bytes = markdown.encode("utf-8")
            self._write_bytes(paths.markdown, md_bytes)
            md_record = self._artifact_record(job, paths.markdown, "markdown", source_url, "trafilatura", self.trafilatura_version)
            md_metadata = {
                "title": extraction.title,
                "canonical_url": extraction.page_metadata.get("canonical_url") or extraction.extracted.get("url") or source_url,
                "author": extraction.extracted.get("author") or extraction.page_metadata.get("author"),
                "published_at": extraction.extracted.get("date") or extraction.page_metadata.get("published_at"),
                "language": extraction.extracted.get("language") or extraction.page_metadata.get("language"),
                "content_length": len(extraction.main_text),
                "image_references": list(dict.fromkeys([*extraction.image_references, *(item["url"] for item in image_references)])),
                "input_html_sha256": html_hash,
            }
            manifest["stages"]["markdown"] = {
                "artifact": md_record,
                "input_html_sha256": html_hash,
                "metadata": md_metadata,
                "validated_at": _now(),
            }
            self._update_manifest(paths, manifest)
            reuse["markdown"] = "generated"

        pdf_stage = previous_stages.get("pdf")
        pdf_record = self._verified_artifact(job, paths, pdf_stage, "pdf", input_html_sha256=html_hash)
        if pdf_record is not None:
            reuse["pdf"] = "verified"
            pdf_page_count = int(pdf_stage.get("page_count", 0))
        else:
            self._require_chrome()
            self._render_pdf(paths, identity)
            pdf_record = self._artifact_record(job, paths.pdf, "pdf", source_url, "google-chrome-headless", self._chrome_version())
            pdf_page_count = self._pdf_page_count(paths.pdf)
            if pdf_page_count < 1:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Chrome produced a PDF with no readable pages.", resume_token=identity)
            manifest["stages"]["pdf"] = {
                "artifact": pdf_record,
                "input_html_sha256": html_hash,
                "page_count": pdf_page_count,
                "validated_at": _now(),
            }
            self._update_manifest(paths, manifest)
            reuse["pdf"] = "generated"

        self._update_manifest(paths, manifest)
        artifact_records = [
            manifest["stages"][stage]["artifact"]
            for stage in ("html", "markdown", "pdf")
        ]
        manifest_bytes = paths.manifest.read_bytes()
        manifest_record = {
            "role": "manifest",
            "path": _relative_to_job(job, paths.manifest),
            "size": len(manifest_bytes),
            "sha256": _sha256_bytes(manifest_bytes),
            "source_url": source_url,
            "engine": self.name,
            "engine_version": MANIFEST_SCHEMA_VERSION,
        }
        page_facts = self._page_resource_facts(paths.html)
        fetch_metadata = {
            "source_url": source_url,
            "source_identity": identity,
            "title": md_metadata.get("title"),
            "canonical_url": md_metadata.get("canonical_url"),
            "content_length": md_metadata.get("content_length"),
            "image_references": md_metadata.get("image_references", []),
            "html_resources": page_facts,
            "pdf_page_count": pdf_page_count,
            "resume": reuse,
            "resume_dependencies": {"markdown": ["html"], "pdf": ["html"]},
            "manifest": manifest_record,
            "artifact_hashes": {
                "html": html_record["sha256"],
                "markdown": md_record["sha256"],
                "pdf": pdf_record["sha256"],
            },
            "artifacts": artifact_records,
        }
        return ProviderResult(
            metadata={
                "probe": {
                    "content_type": ContentType.WEBPAGE.value,
                    "source_url": source_url,
                    "source_identity": identity,
                    "hostname": urlsplit(source_url).hostname or "",
                    "provider": self.name,
                },
                "fetch": fetch_metadata,
            },
            artifacts=(
                ProducedArtifact(_relative_to_job(job, paths.html), "html", "text/html; charset=utf-8"),
                ProducedArtifact(_relative_to_job(job, paths.markdown), "markdown", "text/markdown; charset=utf-8"),
                ProducedArtifact(_relative_to_job(job, paths.pdf), "pdf", "application/pdf"),
                ProducedArtifact(_relative_to_job(job, paths.manifest), "manifest", "application/json; charset=utf-8"),
            ),
            resume_token=identity,
        )

    def _job_paths(self, job: Job) -> _JobPaths:
        workspace = Path(job.workspace_path).expanduser()
        temp = Path(job.temp_path).expanduser()
        if workspace.is_symlink() or temp.is_symlink():
            raise ValueError("workspace and temp root cannot be symlinks")
        resolved_workspace = workspace.resolve(strict=True)
        resolved_temp = temp.resolve(strict=True)
        if resolved_temp != resolved_workspace / "temp":
            raise ValueError("temp root must be the canonical workspace/temp directory")
        runtime = resolved_temp / ".webpage-runtime"
        for path in (runtime,):
            if path.is_symlink():
                raise ValueError("Webpage runtime directory cannot be a symlink")
        runtime.mkdir(parents=True, exist_ok=True)
        resolved_runtime = runtime.resolve(strict=True)
        if not resolved_runtime.is_relative_to(resolved_temp):
            raise ValueError("Webpage runtime path escapes Job temp")
        paths = _JobPaths(
            workspace=resolved_workspace,
            temp=resolved_temp,
            html=resolved_temp / ARTICLE_HTML,
            markdown=resolved_temp / ARTICLE_MARKDOWN,
            pdf=resolved_temp / ARTICLE_PDF,
            manifest=resolved_temp / MANIFEST_NAME,
            runtime=resolved_runtime,
        )
        for path in (paths.html, paths.markdown, paths.pdf, paths.manifest):
            if path.is_symlink():
                raise ValueError(f"Webpage artifact cannot be a symlink: {path.name}")
            if path.exists() and not path.resolve(strict=True).is_relative_to(resolved_temp):
                raise ValueError(f"Webpage artifact escapes Job temp: {path.name}")
        return paths

    def _read_manifest(self, paths: _JobPaths, source_url: str, identity: str) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "provider": self.name,
            "source": {"identity": identity, "url": source_url},
            "source_identity": identity,
            "source_url": source_url,
            "created_at": _now(),
            "updated_at": _now(),
            "stages": {},
        }
        if paths.manifest.is_symlink() or not paths.manifest.is_file():
            return base
        try:
            old = json.loads(paths.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return base
        if (
            old.get("schema_version") != MANIFEST_SCHEMA_VERSION
            or old.get("source_identity") != identity
            or old.get("source_url") != source_url
            or not isinstance(old.get("stages"), dict)
        ):
            return base
        base["created_at"] = old.get("created_at") or base["created_at"]
        base["stages"] = dict(old["stages"])
        return base

    @staticmethod
    def _stage_hash(stage: Any) -> str | None:
        artifact = stage.get("artifact") if isinstance(stage, dict) else None
        digest = artifact.get("sha256") if isinstance(artifact, dict) else None
        return str(digest) if isinstance(digest, str) else None

    def _verified_artifact(
        self,
        job: Job,
        paths: _JobPaths,
        stage: Any,
        role: str,
        *,
        input_html_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(stage, dict) or not isinstance(stage.get("artifact"), dict):
            return None
        artifact = dict(stage["artifact"])
        filename = {"html": ARTICLE_HTML, "markdown": ARTICLE_MARKDOWN, "pdf": ARTICLE_PDF}.get(role)
        candidate = paths.temp / filename if filename else None
        try:
            if (
                candidate is None
                or candidate.is_symlink()
                or not candidate.is_file()
                or artifact.get("role") != role
                or artifact.get("path") != _relative_to_job(job, candidate)
                or artifact.get("source_url") != job.source_url.strip()
                or (input_html_sha256 is not None and stage.get("input_html_sha256") != input_html_sha256)
            ):
                return None
            size = candidate.stat().st_size
            digest = _sha256_file(candidate)
            if size <= 0 or size != int(artifact.get("size", -1)) or digest != artifact.get("sha256"):
                return None
            if role == "html":
                self._validate_html(candidate, job.source_url.strip(), self._source_identity_for_job(job))
            elif role == "markdown":
                text = candidate.read_text(encoding="utf-8")
                if not text.strip():
                    return None
            elif role == "pdf":
                if self._pdf_page_count(candidate) < 1:
                    return None
            return artifact
        except (OSError, ValueError, TypeError, KeyError):
            return None

    @staticmethod
    def _source_identity_for_job(job: Job) -> str:
        return _source_identity(job.source_url.strip())

    def _update_manifest(self, paths: _JobPaths, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = _now()
        manifest["artifact_hashes"] = {
            stage: entry["artifact"]["sha256"]
            for stage, entry in manifest["stages"].items()
            if isinstance(entry, dict) and isinstance(entry.get("artifact"), dict)
        }
        manifest["logical_content_deliverable"] = {
            "content_type": ContentType.WEBPAGE.value,
            "roles": ["html", "markdown", "pdf"],
            "complete": all(role in manifest["stages"] for role in ("html", "markdown", "pdf")),
        }
        _safe_json_write(paths.manifest, manifest)

    def _artifact_record(self, job: Job, path: Path, role: str, source_url: str, engine: str, engine_version: str) -> dict[str, Any]:
        return {
            "role": role,
            "path": _relative_to_job(job, path),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
            "source_url": source_url,
            "engine": engine,
            "engine_version": engine_version,
        }

    def _single_file_needs_node(self) -> bool:
        return self.single_file_executable.suffix.lower() in {".js", ".mjs"}

    def _single_file_command(self) -> list[str]:
        if self._single_file_needs_node():
            from src.providers.video import locate_tool

            return [str(locate_tool("node", "UCI_NODE_PATH")), str(self.single_file_executable)]
        return [str(self.single_file_executable)]

    def _require_single_file(self) -> None:
        if not self.single_file_executable.is_file() or not (
                self._single_file_needs_node() or compat.is_executable(self.single_file_executable)):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile CLI is missing or not executable.")

    def _require_chrome(self) -> None:
        if not compat.is_executable(self.chrome_executable):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "The configured Google Chrome executable is missing or not executable.")

    def _run(self, args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return self.command_runner(
                args,
                cwd=str(cwd) if cwd else None,
                env=env,
                timeout=timeout or self.timeout_seconds,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise error

    def _single_file_version(self, paths: _JobPaths) -> str:
        if self._single_file_version_cache is not None:
            return self._single_file_version_cache
        try:
            result = self._run([*self._single_file_command(), "--version"], cwd=paths.runtime, env=self._child_env(paths))
            version = (result.stdout or result.stderr).strip().splitlines()
            if result.returncode == 0 and version:
                self._single_file_version_cache = version[-1].strip()
                return self._single_file_version_cache
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._single_file_version_cache = "unknown"
        return self._single_file_version_cache

    def _chrome_version(self) -> str:
        if self._chrome_version_cache is not None:
            return self._chrome_version_cache
        if compat.WINDOWS:
            # chrome.exe --version opens a browser window on Windows; the install keeps one folder per version instead.
            versions = sorted((entry.name for entry in self.chrome_executable.parent.glob("[0-9]*.*") if entry.is_dir()),
                              key=lambda name: tuple(int(part) for part in name.split(".") if part.isdigit()))
            self._chrome_version_cache = f"Google Chrome {versions[-1]}" if versions else "unknown"
            return self._chrome_version_cache
        try:
            result = self.command_runner(
                [str(self.chrome_executable), "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=min(self.timeout_seconds, 10),
            )
            value = (result.stdout or result.stderr).strip()
            if result.returncode == 0 and value:
                self._chrome_version_cache = value
                return value
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._chrome_version_cache = "unknown"
        return self._chrome_version_cache

    @staticmethod
    def _child_env(paths: _JobPaths) -> dict[str, str]:
        work_tmp = paths.runtime / "tmp"
        deno_cache = paths.runtime / "deno-cache"
        chrome_cache = paths.runtime / "chrome-cache"
        for path in (work_tmp, deno_cache, chrome_cache):
            if path.is_symlink():
                raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, "Webpage runtime temp path cannot be a symlink.")
            path.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["TMPDIR"] = str(work_tmp)
        env["TMP"] = str(work_tmp)
        env["TEMP"] = str(work_tmp)
        env["DENO_DIR"] = str(deno_cache)
        env["XDG_CACHE_HOME"] = str(chrome_cache)
        deno_bin = Path.home() / ".deno" / "bin"
        if deno_bin.is_dir():
            env["PATH"] = f"{deno_bin}{os.pathsep}{env.get('PATH', '')}"
        return env

    def _preflight(self, source_url: str, identity: str) -> None:
        request = urllib.request.Request(source_url, headers={"User-Agent": USER_AGENT}, method="HEAD")
        context = ssl.create_default_context(cafile=certifi.where())
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                    status = int(getattr(response, "status", 200))
                    final_url = response.geturl()
                    response_headers = response.headers
                if status in {408, 425, 429} or status >= 500:
                    last_error = OSError(f"HTTP {status}")
                    if attempt < self.retries:
                        self._backoff(attempt)
                        continue
                    raise ProviderFailure(
                        ProviderFailureKind.NETWORK,
                        self.name,
                        f"Webpage network retries exhausted (HTTP {status}): {_safe_url(source_url)}",
                        resume_token=identity,
                    ) from last_error
                self._check_http_status(status, final_url, identity, headers=response_headers)
                return
            except urllib.error.HTTPError as error:
                status = int(error.code)
                final_url = error.geturl() or source_url
                body = error.read(32768) if status == 403 else b""
                headers = error.headers
                error.close()
                if status in {405, 501}:
                    return
                if status == 403:
                    diagnostic_status, diagnostic_url, diagnostic_headers, diagnostic_body = self._forbidden_diagnostic(source_url)
                    if diagnostic_status:
                        if diagnostic_status < 400:
                            if _is_login_wall(diagnostic_body, diagnostic_url):
                                raise ProviderFailure(ProviderFailureKind.AUTH_REQUIRED, self.name, f"Webpage requires authorization: {_safe_url(diagnostic_url)}", resume_token=identity)
                            return
                        self._check_http_status(
                            diagnostic_status,
                            diagnostic_url,
                            identity,
                            message=diagnostic_body[:32768].decode("utf-8", errors="ignore"),
                            headers=diagnostic_headers,
                        )
                    self._check_http_status(status, final_url, identity, message=body.decode("utf-8", errors="ignore"), headers=headers)
                    return
                if status in {408, 425, 429} or status >= 500:
                    last_error = error
                    if attempt < self.retries:
                        self._backoff(attempt)
                        continue
                    raise ProviderFailure(
                        ProviderFailureKind.NETWORK,
                        self.name,
                        f"Webpage network retries exhausted (HTTP {status}): {_safe_url(source_url)}",
                        resume_token=identity,
                    ) from error
                self._check_http_status(status, final_url, identity, message=body.decode("utf-8", errors="ignore"), headers=headers)
                return
            except ProviderFailure:
                raise
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
                last_error = error
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise ProviderFailure(
                    ProviderFailureKind.NETWORK,
                    self.name,
                    f"Webpage network retries exhausted: {_safe_url(source_url)}",
                    resume_token=identity,
                ) from error
        raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, "Webpage network retries exhausted.", resume_token=identity) from last_error

    def _forbidden_diagnostic(self, url: str) -> tuple[int, str, Any, bytes]:
        """Read a small anonymous response body only when HEAD returns 403."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                return (
                    int(getattr(response, "status", 200)),
                    response.geturl(),
                    response.headers,
                    response.read(32768),
                )
        except urllib.error.HTTPError as error:
            try:
                return int(error.code), error.geturl() or url, error.headers, error.read(32768)
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            return 0, url, {}, b""

    def _check_http_status(
        self,
        status: int,
        url: str,
        identity: str,
        *,
        message: str = "",
        headers: Any = None,
    ) -> None:
        access_failure = classify_http_access_denial(status, message, headers)
        if access_failure is not None:
            raise ProviderFailure(access_failure, self.name, f"Webpage access was denied (HTTP {status}): {_safe_url(url)}", resume_token=identity)
        if status in {404, 410}:
            raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Webpage URL returned HTTP {status}: {_safe_url(url)}", resume_token=identity)
        if status in {408, 425, 429} or status >= 500:
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"Webpage network retries exhausted (HTTP {status}): {_safe_url(url)}", resume_token=identity)
        if status >= 400:
            raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Webpage URL returned HTTP {status}: {_safe_url(url)}", resume_token=identity)

    def _backoff(self, attempt: int) -> None:
        delay = min(self.retry_backoff_seconds * (2**attempt), 2.0)
        if delay:
            time.sleep(delay)

    def _capture_html(self, paths: _JobPaths, source_url: str, identity: str) -> None:
        scratch = paths.runtime / f"singlefile-{os.getpid()}-{time.time_ns()}.html"
        errors_file = paths.runtime / "singlefile-errors.json"
        env = self._child_env(paths)
        args = [
            *self._single_file_command(),
            source_url,
            str(scratch),
            "--browser-headless=true",
            f"--browser-executable-path={self.chrome_executable}",
            "--browser-wait-until=networkIdle",
            "--browser-load-max-time=60000",
            "--browser-capture-max-time=60000",
            "--filename-conflict-action=overwrite",
            "--insert-single-file-comment=true",
            "--insert-canonical-link=true",
            "--insert-meta-csp=true",
            "--save-original-urls=true",
            "--include-infobar=false",
            f"--errors-file={errors_file}",
        ]
        if errors_file.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile error log cannot be a symlink.", resume_token=identity)
        last_failure: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                result = self._run(args, cwd=paths.runtime, env=env, timeout=max(self.timeout_seconds, 90))
            except subprocess.TimeoutExpired as error:
                last_failure = error
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"SingleFile capture network retries exhausted: {_safe_url(source_url)}", resume_token=identity) from error
            except OSError as error:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile CLI could not be started.", resume_token=identity) from error
            if result.returncode != 0:
                diagnostics = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
                if self._looks_transient_network(diagnostics):
                    last_failure = RuntimeError("transient SingleFile network failure")
                    if attempt < self.retries:
                        self._backoff(attempt)
                        continue
                    raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"SingleFile network retries exhausted: {_safe_url(source_url)}", resume_token=identity) from last_failure
                detail = " ".join((result.stderr or result.stdout or "").split())[-300:]
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name,
                                      "SingleFile CLI failed to save the webpage." + (f" {detail}" if detail else ""), resume_token=identity)
            if not scratch.is_file() or scratch.stat().st_size == 0:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile CLI returned success without an HTML artifact.", resume_token=identity)
            if scratch.stat().st_size > MAX_HTML_BYTES:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile HTML exceeds the configured size limit.", resume_token=identity)
            try:
                self._validate_html(scratch, source_url, identity)
            except ProviderFailure as failure:
                try:
                    scratch.unlink()
                except OSError:
                    pass
                if failure.kind is ProviderFailureKind.NETWORK and attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise
            self._replace_file(scratch, paths.html)
            return
        raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, "SingleFile network retries exhausted.", resume_token=identity) from last_failure

    @staticmethod
    def _looks_transient_network(message: str) -> bool:
        markers = (
            "net::err_name_not_resolved",
            "net::err_connection_refused",
            "net::err_connection_reset",
            "net::err_connection_timed_out",
            "net::err_internet_disconnected",
            "net::err_network_changed",
            "net::err_address_unreachable",
            "net::err_timed_out",
            "load timeout",
            "network retries exhausted",
        )
        return any(marker in message for marker in markers)

    def _validate_html(self, path: Path, source_url: str, identity: str) -> None:
        try:
            data = path.read_bytes()
            tree = parse_html(data)
        except (OSError, ValueError, TypeError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile output is not readable HTML.", resume_token=identity) from error
        if _is_login_wall(data, source_url):
            raise ProviderFailure(ProviderFailureKind.AUTH_REQUIRED, self.name, f"Webpage requires a login: {_safe_url(source_url)}", resume_token=identity)
        title = " ".join(" ".join(tree.xpath("//title[1]//text()") or []).split()).lower()
        visible_text = " ".join(" ".join(tree.xpath("//body//text()") or tree.xpath("//text()" )).split())
        lower_text = visible_text.lower()
        if title.startswith("404") or re.search(r"\b404\s+(?:not found|error)\b", lower_text):
            raise ProviderFailure(ProviderFailureKind.INVALID_URL, self.name, f"Webpage returned a not-found page: {_safe_url(source_url)}", resume_token=identity)
        if re.search(r"this site can(?:not|'t) be reached|webpage not available|chrome-error://|err_[a-z_]+", lower_text):
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, f"Chrome could not load the webpage: {_safe_url(source_url)}", resume_token=identity)
        if not visible_text.strip():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile saved an empty browser page.", resume_token=identity)

    def _extract_markdown(self, html_path: Path, source_url: str, identity: str):
        try:
            extraction = extract_trafilatura_markdown(html_path.read_bytes(), source_url)
        except Exception as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, f"Trafilatura extraction failed: {type(error).__name__}", resume_token=identity) from error
        if extraction.form_only or not extraction.main_text.strip() or not extraction.markdown.strip():
            if _is_login_wall(html_path.read_bytes(), source_url):
                raise ProviderFailure(ProviderFailureKind.AUTH_REQUIRED, self.name, f"Webpage requires a login: {_safe_url(source_url)}", resume_token=identity)
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Trafilatura could not extract readable webpage content.", resume_token=identity)
        normalized_text = " ".join(extraction.main_text.casefold().split())
        placeholder_shell = re.fullmatch(
            r"(?:loading(?:\.{1,3})?|please wait(?:\.{1,3})?|javascript (?:is )?required|enable javascript to (?:view|continue)[^.]*)",
            normalized_text,
        )
        if placeholder_shell and not extraction.page_metadata.get("has_article_container"):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "SingleFile captured a JavaScript loading shell without rendered webpage content.", resume_token=identity)
        return extraction

    def _render_pdf(self, paths: _JobPaths, identity: str) -> None:
        profile = paths.runtime / f"chrome-profile-{os.getpid()}-{time.time_ns()}"
        scratch = paths.runtime / f"chrome-print-{os.getpid()}-{time.time_ns()}.pdf"
        profile.mkdir(parents=True, exist_ok=False)
        env = self._child_env(paths)
        args = [
            str(self.chrome_executable),
            "--headless",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--disable-default-apps",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={scratch}",
            paths.html.resolve().as_uri(),
        ]
        try:
            result = self._execute_chrome_pdf(args, paths.runtime, env, scratch, max(self.timeout_seconds, 90))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Chrome Headless could not render the saved webpage to PDF.", resume_token=identity) from error
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        if result.returncode != 0 or not scratch.is_file():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Chrome Headless failed to create the webpage PDF.", resume_token=identity)
        try:
            self._validate_pdf(scratch, identity)
            self._replace_file(scratch, paths.pdf)
        finally:
            try:
                scratch.unlink()
            except OSError:
                pass

    def _execute_chrome_pdf(
        self,
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        pdf_path: Path,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        # Injected runners keep unit tests hermetic. In production, macOS Chrome
        # may remain resident after writing the PDF, so observe the complete PDF
        # and stop only this dedicated Headless process group.
        if self.command_runner is not subprocess.run:
            return self._run(args, cwd=cwd, env=env, timeout=timeout_seconds)
        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **compat.new_process_group(),
        )
        deadline = time.monotonic() + timeout_seconds
        previous_stamp: tuple[int, int] | None = None
        stable_count = 0
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                return subprocess.CompletedProcess(args, return_code, stdout="", stderr="")
            stamp = self._complete_pdf_stamp(pdf_path)
            if stamp is not None and stamp == previous_stamp:
                stable_count += 1
                if stable_count >= 2:
                    self._stop_process_group(process)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            elif stamp is not None:
                previous_stamp = stamp
                stable_count = 0
            else:
                previous_stamp = None
                stable_count = 0
            time.sleep(0.25)
        self._stop_process_group(process)
        raise subprocess.TimeoutExpired(args, timeout_seconds)

    @staticmethod
    def _complete_pdf_stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            if stat.st_size < 64:
                return None
            with path.open("rb") as stream:
                header = stream.read(8)
                stream.seek(max(0, stat.st_size - 2048))
                tail = stream.read()
            if not header.startswith(b"%PDF-") or b"%%EOF" not in tail:
                return None
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _stop_process_group(process: subprocess.Popen[str]) -> None:
        compat.stop_process_group(process)

    def _validate_pdf(self, path: Path, identity: str) -> None:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Chrome PDF output could not be read.", resume_token=identity) from error
        if len(data) < 64 or not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Chrome output is not a complete readable PDF.", resume_token=identity)
        if self._pdf_page_count(path) < 1:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Chrome produced a PDF with no pages.", resume_token=identity)

    @staticmethod
    def _pdf_page_count(path: Path) -> int:
        pdfinfo = shutil.which("pdfinfo")
        if pdfinfo:
            try:
                result = subprocess.run([pdfinfo, str(path)], capture_output=True, text=True, check=False, timeout=10)
                match = re.search(r"(?m)^Pages:\s+(\d+)$", result.stdout or "")
                if result.returncode == 0 and match:
                    return int(match.group(1))
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            data = path.read_bytes()
        except OSError:
            return 0
        return len(re.findall(rb"/Type\s*/Page(?!s\b)", data))

    @staticmethod
    def _page_resource_facts(path: Path) -> dict[str, int]:
        try:
            tree = parse_html(path.read_bytes())
        except (OSError, ValueError, TypeError):
            return {"image_count": 0, "embedded_image_count": 0, "external_image_count": 0, "unresolved_image_count": 0}
        image_sources = [str(src).strip() for src in tree.xpath("//img/@src") if str(src).strip()]
        image_sources.extend(str(src).strip() for src in tree.xpath("//source/@src") if str(src).strip())
        embedded = sum(src.lower().startswith(("data:image/", "blob:")) for src in image_sources)
        external = sum(src.lower().startswith(("http://", "https://")) for src in image_sources)
        return {
            "image_count": len(image_sources),
            "embedded_image_count": embedded,
            "external_image_count": external,
            "unresolved_image_count": max(0, len(image_sources) - embedded - external),
        }

    @staticmethod
    def _saved_page_image_references(path: Path, source_url: str) -> list[dict[str, str]]:
        """Read image URLs SingleFile retained in its saved DOM, without fetching them."""
        try:
            tree = parse_html(path.read_bytes())
        except (OSError, ValueError, TypeError):
            return []
        containers = tree.xpath("//article[1]") or tree.xpath("//main[1]") or [tree]
        references: list[dict[str, str]] = []
        seen: set[str] = set()
        for image in containers[0].xpath(".//img"):
            target = str(image.get("data-sf-original-src") or image.get("src") or "").strip()
            target = urljoin(source_url, target)
            if urlsplit(target).scheme.lower() not in {"http", "https"} or target in seen:
                continue
            seen.add(target)
            alt = " ".join(str(image.get("alt") or "image").split()) or "image"
            references.append({"alt": alt, "url": target})
        return references

    @staticmethod
    def _append_image_references(markdown: str, references: list[dict[str, str]]) -> str:
        additions: list[str] = []
        for image in references:
            alt = image["alt"].replace("\\", "\\\\").replace("]", "\\]")
            target = quote(image["url"], safe=":/?#[]@!$&'()*+,;=%-._~")
            reference = f"![{alt}]({target})"
            if reference not in markdown:
                additions.append(reference)
        if not additions:
            return markdown
        return markdown.rstrip() + "\n\n### Page image references\n\n" + "\n\n".join(additions) + "\n"

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
        pending = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        except BaseException:
            try:
                pending.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _replace_file(source: Path, destination: Path) -> None:
        if destination.is_symlink():
            raise OSError("artifact destination cannot be a symlink")
        if not source.resolve().is_relative_to(destination.parent.resolve()):
            raise OSError("staging artifact escaped Job temp")
        os.replace(source, destination)

    @staticmethod
    def _unlink_if_regular(path: Path) -> None:
        if path.is_symlink():
            raise OSError("downstream artifact cannot be a symlink")
        if path.exists():
            path.unlink()


__all__ = ["WebpageProvider"]
