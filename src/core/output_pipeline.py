"""Core-owned Stage 5 finalization, durable resume, info.md and temp cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from src.core import compat
from src.core.errors import CoreError, ErrorCode, RetryMetadata
from src.core.job import ContentType, Job, JobState, OutputResult
from src.core.state_machine import StateMachine
from src.core.video_pipeline import save_job
from src.output.delivery import delivered_artifacts
from src.output.info_writer import render_info_markdown
from src.output.temp_registry import TempEntry, TempRegistryError, cleanup_temp_registry, snapshot_temp


MANIFEST_NAME = ".stage5-manifest.json"
LOCK_NAME = ".stage5.lock"
STAGING_ROOT = ".stage5-staging"
OUTPUT_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class OutputFinalizationError(ValueError):
    pass


@dataclass(frozen=True)
class OutputPipelineOutcome:
    job: Job
    job_file: Path
    result: OutputResult | None
    error: CoreError | None

    @property
    def success(self) -> bool:
        return self.error is None and self.job.current_state is JobState.COMPLETED and self.result is not None


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OutputFinalizationError("artifact is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _job_context(job: Job, job_file: Path | None) -> tuple[Path, Path, Path, Path]:
    workspace = Path(job.workspace_path).expanduser()
    if workspace.is_symlink():
        raise OutputFinalizationError("Job workspace cannot be a symlink")
    try:
        root = workspace.resolve(strict=True)
    except OSError as error:
        raise OutputFinalizationError("Job workspace is unavailable") from error
    if root.name != f"job-{job.job_id}" or not root.is_dir():
        raise OutputFinalizationError("Job workspace does not match its job_id")

    expected_temp = root / "temp"
    expected_output = root / "output"
    temp = Path(job.temp_path).expanduser()
    output = Path(job.output_path).expanduser()
    if temp.is_symlink() or output.is_symlink():
        raise OutputFinalizationError("Job temp/output roots cannot be symlinks")
    if temp.resolve(strict=False) != expected_temp or output.resolve(strict=False) != expected_output:
        raise OutputFinalizationError("Job temp/output paths do not match the canonical workspace children")
    if not output.exists() or not output.is_dir():
        raise OutputFinalizationError("Job output directory is unavailable")
    if temp.exists() and (not temp.is_dir() or temp.resolve(strict=True) != expected_temp):
        raise OutputFinalizationError("Job temp directory is unavailable or escaped its workspace")

    destination = (job_file or root / "job.json").expanduser()
    if destination.is_symlink() or destination.resolve(strict=False) != root / "job.json":
        raise OutputFinalizationError("Job contract file must be the non-symlink workspace/job.json")
    if not destination.is_file():
        raise OutputFinalizationError("Persisted Job contract is missing")
    return root, temp, output, destination


@contextmanager
def _job_lock(root: Path) -> Iterator[None]:
    lock_path = root / LOCK_NAME
    if lock_path.is_symlink():
        raise OutputFinalizationError("Stage 5 lock file cannot be a symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise OutputFinalizationError("Could not open the Stage 5 Job lock") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OutputFinalizationError("Stage 5 lock is not a regular file")
        compat.lock(descriptor)
        yield
    finally:
        try:
            compat.unlock(descriptor)
        finally:
            os.close(descriptor)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise OutputFinalizationError("Stage 5 manifest cannot be a symlink")
    descriptor, pending_name = tempfile.mkstemp(prefix=".stage5-manifest.", suffix=".pending", dir=path.parent)
    pending = Path(pending_name)
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    except BaseException:
        try:
            pending.unlink()
        except OSError:
            pass
        raise


def _load_manifest(root: Path) -> dict[str, Any] | None:
    path = root / MANIFEST_NAME
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise OutputFinalizationError("Stage 5 manifest is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OutputFinalizationError("Stage 5 manifest is unreadable") from error
    if not isinstance(value, dict) or value.get("schema_version") != OUTPUT_SCHEMA_VERSION:
        raise OutputFinalizationError("Stage 5 manifest schema is invalid")
    return value


def _record_groups(job: Job) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any]]]:
    providers = job.source_metadata.get("providers")
    if not isinstance(providers, Mapping):
        return []
    groups: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    for name, value in providers.items():
        if not isinstance(value, Mapping):
            continue
        fetch = value.get("fetch")
        if isinstance(fetch, Mapping):
            groups.append((str(name), value, fetch))
    return groups


def _raw_artifact_records(job: Job) -> list[dict[str, Any]]:
    kind = job.resolved_content_type
    groups = _record_groups(job)

    if kind is ContentType.VIDEO:
        stage3 = job.source_metadata.get("stage3")
        if isinstance(stage3, Mapping):
            if stage3.get("status") not in {"validated", "validated_no_speech_no_subtitles"}:
                raise OutputFinalizationError("VIDEO Stage 3 has not reached a validated terminal result")
            record = stage3.get("validated_artifact")
            provider_name = "Stage 3"
        else:
            if job.current_state not in {JobState.DOWNLOADING, JobState.DOCUMENTING}:
                raise OutputFinalizationError("VIDEO source passthrough is only legal before Stage 3 starts")
            provider = job.provider_metadata("yt-dlp") or {}
            record = provider.get("validated_artifact")
            provider_name = "yt-dlp"
        if not isinstance(record, Mapping):
            raise OutputFinalizationError("VIDEO Job has no validated Stage 2/3 artifact")
        return [{**dict(record), "role": "video", "engine": provider_name, "name": "final.mp4"}]

    if kind in {ContentType.IMAGE, ContentType.IMAGE_SET}:
        candidates: list[tuple[str, Mapping[str, Any], list[Any]]] = []
        for provider_name, _metadata, fetch in groups:
            items = fetch.get("artifacts")
            if isinstance(items, list) and items and fetch.get("status") == "complete":
                candidates.append((provider_name, fetch, items))
        if not candidates:
            raise OutputFinalizationError("IMAGE Job has no complete validated artifact set")
        provider_name, fetch, items = candidates[-1]
        if kind is ContentType.IMAGE and len(items) != 1:
            raise OutputFinalizationError("IMAGE requires exactly one validated image")
        if kind is ContentType.IMAGE_SET and len(items) < 2:
            raise OutputFinalizationError("IMAGE_SET requires at least two validated images")
        records = [dict(item) for item in items if isinstance(item, Mapping)]
        if len(records) != len(items):
            raise OutputFinalizationError("Image artifact records are malformed")
        records.sort(key=lambda item: int(item.get("source_order", 1_000_000)))
        if fetch.get("expected_count") is not None and int(fetch["expected_count"]) != len(records):
            raise OutputFinalizationError("Image artifact count does not match the completed provider set")
        for record in records:
            record.setdefault("role", "image")
            record.setdefault("engine", provider_name)
        return records

    if kind is ContentType.ARTICLE:
        for provider_name, _metadata, fetch in groups:
            record = fetch.get("artifact")
            if isinstance(record, Mapping):
                return [{**dict(record), "role": "article", "engine": record.get("provider") or provider_name, "name": "article.md"}]
            metadata = fetch.get("metadata")
            if isinstance(metadata, Mapping):
                path = metadata.get("artifact_path")
                if path:
                    return [{"path": path, "size": metadata.get("artifact_size"), "sha256": metadata.get("artifact_sha256"), "role": "article", "engine": metadata.get("extraction_engine") or provider_name, "name": "article.md"}]
        raise OutputFinalizationError("ARTICLE Job has no validated Markdown artifact")

    if kind is ContentType.WEBPAGE:
        for provider_name, _metadata, fetch in groups:
            items = fetch.get("artifacts")
            if not isinstance(items, list):
                continue
            selected = [dict(item) for item in items if isinstance(item, Mapping) and item.get("role") in {"html", "markdown", "pdf"}]
            if {str(item.get("role")) for item in selected} == {"html", "markdown", "pdf"}:
                for item in selected:
                    item.setdefault("engine", provider_name)
                    item.setdefault("name", {"html": "article.html", "markdown": "article.md", "pdf": "article.pdf"}[str(item["role"])])
                return sorted(selected, key=lambda item: ("html", "markdown", "pdf").index(str(item["role"])))
        raise OutputFinalizationError("WEBPAGE requires validated HTML, Markdown and PDF artifacts")

    if kind is ContentType.DOCUMENT:
        for provider_name, _metadata, fetch in groups:
            items = fetch.get("artifacts")
            if isinstance(items, list):
                selected = [dict(item) for item in items if isinstance(item, Mapping) and item.get("role", "document_file") == "document_file"]
                if selected:
                    for item in selected:
                        item.setdefault("engine", fetch.get("engine") or provider_name)
                        item.setdefault("name", item.get("filename") or Path(str(item.get("path") or "document")).name)
                    return selected
        raise OutputFinalizationError("DOCUMENT Job has no validated document artifact")

    raise OutputFinalizationError(f"Stage 5 does not have a formal deliverable contract for {kind.value}")


def _validate_temp_artifact(job: Job, job_root: Path, temp_root: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    raw_path = record.get("path") or record.get("relative_path")
    if not isinstance(raw_path, str):
        raise OutputFinalizationError("Validated artifact record has no relative path")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts) or not relative.parts or relative.parts[0] != "temp":
        raise OutputFinalizationError("Validated artifact path is not inside the Job temp directory")
    source = job_root.joinpath(*relative.parts)
    cursor = job_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise OutputFinalizationError("Validated artifact path contains a symlink")
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(temp_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise OutputFinalizationError("Validated artifact is missing or outside Job temp") from error
    if resolved == temp_root / STAGING_ROOT or (temp_root / STAGING_ROOT) in resolved.parents:
        raise OutputFinalizationError("Stage 5 runtime files cannot be promoted as content")
    mode = resolved.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise OutputFinalizationError("Validated artifact is not a regular file")
    size = record.get("size", record.get("size_bytes"))
    sha256 = str(record.get("sha256") or "").lower()
    if not isinstance(size, int) or size <= 0 or not _SHA256_RE.fullmatch(sha256):
        raise OutputFinalizationError("Validated artifact record has an invalid size or SHA-256")
    actual_size = resolved.stat().st_size
    actual_hash = _sha256_file(resolved)
    if actual_size != size or actual_hash != sha256:
        raise OutputFinalizationError("Validated artifact size or SHA-256 no longer matches Job metadata")
    name = record.get("name") or record.get("filename") or record.get("original_filename") or resolved.name
    return {
        "role": str(record.get("role") or "content"),
        "source_path": relative.as_posix(),
        "size": actual_size,
        "sha256": actual_hash,
        "name": str(name),
        "media_type": record.get("media_type") or record.get("mime"),
        "engine": str(record.get("engine") or record.get("provider") or "unknown"),
        "engine_version": record.get("engine_version"),
        "source_order": record.get("source_order"),
        "source_url": record.get("source_url") or job.source_url,
    }


def _normalized_filename(value: str, fallback: str) -> str:
    value = unicodedata.normalize("NFC", Path(value).name)
    value = "".join(character if character.isalnum() or character in "-_. ()" else "_" for character in value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    if value in {"", ".", ".."}:
        value = fallback
    if len(value) > 180:
        suffix = Path(value).suffix
        value = value[:180 - len(suffix)].rstrip(" ._") + suffix
    return value


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _choose_name(directory: Path, desired: str, reserved: set[str]) -> str:
    desired = _normalized_filename(desired, "content")
    candidate = desired
    index = 2
    while candidate.casefold() in reserved or _lexists(directory / candidate):
        pure = Path(desired)
        candidate = f"{pure.stem}-{index}{pure.suffix}"
        index += 1
    reserved.add(candidate.casefold())
    return candidate


def _plan_output_names(job: Job, output_root: Path, records: list[dict[str, Any]], stage_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kind = job.resolved_content_type
    reserved: set[str] = set()
    if kind is ContentType.VIDEO:
        planned = [{**record, "output_path": _choose_name(output_root, "final.mp4", reserved)} for record in records]
    else:
        content_dir = output_root / "content"
        if content_dir.is_symlink():
            raise OutputFinalizationError("output/content cannot be a symlink")
        if _lexists(content_dir) and not content_dir.is_dir():
            raise OutputFinalizationError("output/content already exists and is not a directory")
        content_dir.mkdir(exist_ok=True)
        if kind in {ContentType.ARTICLE, ContentType.WEBPAGE}:
            names = {"article": ["article.md"]} if kind is ContentType.ARTICLE else {"article": ["article.html", "article.md", "article.pdf"]}
            base = "article"
            suffix = 2
            while any(_lexists(content_dir / name) or name.casefold() in reserved for name in names[base]):
                names[base] = [f"article-{suffix}{Path(name).suffix}" for name in ("article.md",) if kind is ContentType.ARTICLE] if kind is ContentType.ARTICLE else [f"article-{suffix}{Path(name).suffix}" for name in ("article.html", "article.md", "article.pdf")]
                suffix += 1
            for name in names[base]:
                reserved.add(name.casefold())
            if kind is ContentType.ARTICLE:
                planned = [{**records[0], "output_path": f"content/{names[base][0]}"}]
            else:
                role_name = {"html": "article.html", "markdown": "article.md", "pdf": "article.pdf"}
                planned = [{**record, "output_path": "content/" + names[base][("article.html", "article.md", "article.pdf").index(role_name[str(record["role"])])]} for record in records]
        else:
            planned = []
            for record in records:
                desired = _normalized_filename(str(record.get("name") or "content"), "content")
                allocated = _choose_name(content_dir, desired, reserved)
                planned.append({**record, "output_path": f"content/{allocated}"})

    for index, record in enumerate(planned):
        record["stage_path"] = f"temp/{STAGING_ROOT}/{stage_id}/{index:04d}.pending"
    info_desired = "info.md"
    info_name = _choose_name(output_root, info_desired, reserved)
    info = {
        "output_path": info_name,
        "stage_path": f"temp/{STAGING_ROOT}/{stage_id}/info.pending",
    }
    return planned, info


def _source_identity(job: Job, records: Sequence[Mapping[str, Any]]) -> str:
    value = {
        "job_id": job.job_id,
        "content_type": job.resolved_content_type.value,
        "source_url": job.source_url,
        "artifacts": [{key: record.get(key) for key in ("role", "source_path", "size", "sha256", "source_order")} for record in records],
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _ensure_output_path(output_root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise OutputFinalizationError("Stage 5 manifest contains an unsafe output path")
    cursor = output_root
    for part in path.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise OutputFinalizationError("Output path contains a symlink directory")
        if not cursor.exists():
            cursor.mkdir()
        if not cursor.is_dir():
            raise OutputFinalizationError("Output path parent is not a directory")
    candidate = output_root.joinpath(*path.parts)
    try:
        candidate.resolve(strict=False).relative_to(output_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise OutputFinalizationError("Output path escaped the Job output directory") from error
    return candidate


def _safe_temp_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or path.parts[0] != "temp" or any(part in {".", ".."} for part in path.parts):
        raise OutputFinalizationError("Stage 5 manifest contains an unsafe temp path")
    candidate = root.joinpath(*path.parts)
    cursor = root
    for part in path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise OutputFinalizationError("Stage 5 temp path contains a symlink")
    try:
        candidate.resolve(strict=False).relative_to((root / "temp").resolve(strict=False))
    except (OSError, ValueError) as error:
        raise OutputFinalizationError("Stage 5 temp path escaped the Job temp directory") from error
    return candidate


def _stage_hardlink(source: Path, staged: Path, record: Mapping[str, Any]) -> None:
    if source.is_symlink() or not source.is_file():
        raise OutputFinalizationError("Validated source changed or is unsafe before staging")
    if staged.is_symlink():
        raise OutputFinalizationError("Stage 5 staging path cannot be a symlink")
    staged.parent.mkdir(parents=True, exist_ok=True)
    if _lexists(staged):
        if not staged.is_file() or staged.stat().st_size != record["size"] or _sha256_file(staged) != record["sha256"]:
            raise OutputFinalizationError("Existing Stage 5 staged artifact failed verification")
        return
    os.link(source, staged, follow_symlinks=False)
    source_stat = source.lstat()
    staged_stat = staged.lstat()
    if (not stat.S_ISREG(staged_stat.st_mode) or staged_stat.st_dev != source_stat.st_dev or staged_stat.st_ino != source_stat.st_ino
            or staged_stat.st_size != record["size"] or _sha256_file(staged) != record["sha256"]):
        staged.unlink(missing_ok=True)
        raise OutputFinalizationError("Staged artifact failed size or SHA-256 verification")


def _promote_staged(staged: Path, destination: Path, record: Mapping[str, Any]) -> None:
    if staged.is_symlink() or not staged.is_file():
        raise OutputFinalizationError("Verified Stage 5 staged artifact is missing or unsafe")
    if staged.stat().st_size != record["size"] or _sha256_file(staged) != record["sha256"]:
        raise OutputFinalizationError("Stage 5 staged artifact changed before promotion")
    if _lexists(destination):
        if destination.is_symlink() or not destination.is_file():
            raise OutputFinalizationError("Destination collision is not a regular file")
        if destination.stat().st_size == record["size"] and _sha256_file(destination) == record["sha256"]:
            staged.unlink(missing_ok=True)
            return
        raise OutputFinalizationError("A destination file appeared after collision planning; no overwrite was attempted")
    os.link(staged, destination)
    staged_stat = staged.lstat()
    destination_stat = destination.lstat()
    if (not stat.S_ISREG(destination_stat.st_mode) or destination_stat.st_dev != staged_stat.st_dev
            or destination_stat.st_ino != staged_stat.st_ino or destination_stat.st_size != record["size"]):
        destination.unlink(missing_ok=True)
        raise OutputFinalizationError("Promoted output failed size or SHA-256 verification")
    staged.unlink()


def _unlink_registered_file(path: Path) -> None:
    path.unlink()


def _remove_empty_staging_dirs(temp_root: Path, stage_id: str) -> None:
    stage_directory = temp_root / STAGING_ROOT / stage_id
    if stage_directory.is_symlink():
        raise OutputFinalizationError("Stage 5 staging directory cannot be a symlink")
    if stage_directory.exists():
        stage_directory.rmdir()
    staging_root = temp_root / STAGING_ROOT
    if staging_root.exists() and not any(staging_root.iterdir()):
        staging_root.rmdir()


def _result_from_manifest(manifest: Mapping[str, Any]) -> OutputResult:
    artifacts = manifest.get("artifacts")
    info = manifest.get("info")
    if not isinstance(artifacts, list) or not isinstance(info, Mapping):
        raise OutputFinalizationError("Stage 5 manifest has no committed output result")
    return OutputResult(
        content_paths=tuple(str(item["output_path"]) for item in artifacts),
        info_path=str(info["output_path"]),
        summary=dict(manifest.get("summary") or {}),
    )


def _verify_output_manifest(output_root: Path, manifest: Mapping[str, Any]) -> None:
    result = _result_from_manifest(manifest)
    by_path: dict[str, Mapping[str, Any]] = {}
    for item in manifest.get("artifacts", []):
        if not isinstance(item, Mapping):
            raise OutputFinalizationError("Stage 5 manifest artifact record is invalid")
        by_path[str(item["output_path"])] = item
    info = manifest["info"]
    by_path[result.info_path] = info
    # Files moved to the delivery folder are handed off to the user; their
    # delivery.json entry must carry the same committed SHA-256.
    delivered = delivered_artifacts(output_root.parent)
    for relative, record in by_path.items():
        path = _ensure_output_path(output_root, relative)
        handed_off = delivered.get(relative)
        if handed_off and handed_off.get("sha256") == record.get("sha256") and not os.path.lexists(path):
            continue
        if path.is_symlink() or not path.is_file():
            raise OutputFinalizationError("A committed output file is missing or unsafe")
        if path.stat().st_size != int(record.get("size", -1)) or _sha256_file(path) != record.get("sha256"):
            raise OutputFinalizationError("A committed output file failed size or SHA-256 verification")


def _persist_job_stage5(job: Job, manifest: Mapping[str, Any]) -> None:
    cleanup = manifest.get("cleanup") if isinstance(manifest.get("cleanup"), Mapping) else {}
    job.source_metadata["stage5"] = {
        "manifest": MANIFEST_NAME,
        "phase": manifest.get("phase"),
        "source_identity": manifest.get("source_identity"),
        "content_hashes": {str(item.get("output_path")): item.get("sha256") for item in manifest.get("artifacts", []) if isinstance(item, Mapping)},
        "info_path": (manifest.get("info") or {}).get("output_path") if isinstance(manifest.get("info"), Mapping) else None,
        "info_sha256": (manifest.get("info") or {}).get("sha256") if isinstance(manifest.get("info"), Mapping) else None,
        "cleanup": {
            "status": cleanup.get("status"),
            "attempts": cleanup.get("attempts", 0),
            "unremoved_paths": list(cleanup.get("unremoved_paths") or []),
            "failures": list(cleanup.get("failures") or []),
        },
    }
    if manifest.get("phase") in {"output_committed", "cleanup_registered", "cleaning", "cleanup_failed", "cleanup_complete", "completed"}:
        job.output_result = _result_from_manifest(manifest)
    job.touch()


def _collect_current_records(job: Job, root: Path, temp_root: Path) -> list[dict[str, Any]]:
    raw = _raw_artifact_records(job)
    records = [_validate_temp_artifact(job, root, temp_root, item) for item in raw]
    if job.resolved_content_type is ContentType.WEBPAGE and {item["role"] for item in records} != {"html", "markdown", "pdf"}:
        raise OutputFinalizationError("WEBPAGE content roles are incomplete")
    if job.resolved_content_type is ContentType.VIDEO and len(records) != 1:
        raise OutputFinalizationError("VIDEO finalization requires exactly one validated video artifact")
    return records


def _create_manifest(job: Job, root: Path, temp_root: Path, output_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    stage_id = uuid.uuid4().hex
    artifacts, info_paths = _plan_output_names(job, output_root, records, stage_id)
    output_paths = [str(item["output_path"]) for item in artifacts]
    source_files = [root / item["source_path"] for item in artifacts]
    info_markdown, summary_details = render_info_markdown(job, source_files=source_files, output_paths=output_paths)
    info_bytes = info_markdown.encode("utf-8")
    info_record = {
        **info_paths,
        "size": len(info_bytes),
        "sha256": hashlib.sha256(info_bytes).hexdigest(),
        "engine": "uci-info-writer",
        "engine_version": "1",
    }
    manifest = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "job_id": job.job_id,
        "content_type": job.resolved_content_type.value,
        "source_identity": _source_identity(job, artifacts),
        "phase": "planned",
        "artifacts": artifacts,
        "info": info_record,
        "summary": summary_details,
        "temp_registry": [],
        "cleanup": {"status": "pending", "attempts": 0, "unremoved_paths": [], "failures": []},
    }
    info_stage = _safe_temp_path(root, str(info_record["stage_path"]))
    info_stage.parent.mkdir(parents=True, exist_ok=True)
    if info_stage.is_symlink():
        raise OutputFinalizationError("info.md staging path cannot be a symlink")
    info_stage.write_bytes(info_bytes)
    _write_json_atomic(root / MANIFEST_NAME, manifest)
    return manifest


def _validate_manifest_binding(job: Job, manifest: Mapping[str, Any]) -> None:
    if manifest.get("job_id") != job.job_id or manifest.get("content_type") != job.resolved_content_type.value:
        raise OutputFinalizationError("Stage 5 manifest belongs to a different Job or content type")


def _promote_content(job: Job, root: Path, temp_root: Path, output_root: Path, job_file: Path, manifest: dict[str, Any]) -> None:
    current = _collect_current_records(job, root, temp_root)
    if _source_identity(job, current) != manifest.get("source_identity"):
        raise OutputFinalizationError("Validated source artifact identity changed after Stage 5 planning")
    planned = manifest.get("artifacts")
    if not isinstance(planned, list) or len(planned) != len(current):
        raise OutputFinalizationError("Stage 5 manifest artifact plan no longer matches the Job")
    for current_item, planned_item in zip(current, planned):
        if any(current_item.get(key) != planned_item.get(key) for key in ("source_path", "size", "sha256", "role", "source_order")):
            raise OutputFinalizationError("Validated source artifact changed after Stage 5 planning")

    manifest["phase"] = "promoting"
    _write_json_atomic(root / MANIFEST_NAME, manifest)
    _persist_job_stage5(job, manifest)
    save_job(job, job_file)
    for artifact in planned:
        source = root / str(artifact["source_path"])
        staged = _safe_temp_path(root, str(artifact["stage_path"]))
        destination = _ensure_output_path(output_root, str(artifact["output_path"]))
        _stage_hardlink(source, staged, artifact)
        _promote_staged(staged, destination, artifact)
        manifest["promoted_count"] = int(manifest.get("promoted_count", 0)) + 1
        _write_json_atomic(root / MANIFEST_NAME, manifest)

    info = manifest["info"]
    info_stage = _safe_temp_path(root, str(info["stage_path"]))
    info_destination = _ensure_output_path(output_root, str(info["output_path"]))
    if not info_stage.exists():
        markdown, summary_details = render_info_markdown(
            job,
            source_files=[root / str(item["source_path"]) for item in planned],
            output_paths=[str(item["output_path"]) for item in planned],
        )
        info_bytes = markdown.encode("utf-8")
        if len(info_bytes) != info["size"] or hashlib.sha256(info_bytes).hexdigest() != info["sha256"]:
            raise OutputFinalizationError("Regenerated info.md no longer matches the persisted Stage 5 plan")
        info_stage.parent.mkdir(parents=True, exist_ok=True)
        info_stage.write_bytes(info_bytes)
        manifest["summary"] = summary_details
    _promote_staged(info_stage, info_destination, info)
    _remove_empty_staging_dirs(temp_root, str(PurePosixPath(str(info["stage_path"])).parts[2]))
    _verify_output_manifest(output_root, manifest)
    result = _result_from_manifest(manifest)
    job.output_result = result
    manifest["phase"] = "output_committed"
    _write_json_atomic(root / MANIFEST_NAME, manifest)
    _persist_job_stage5(job, manifest)
    save_job(job, job_file)


def _register_cleanup(job: Job, root: Path, temp_root: Path, job_file: Path, manifest: dict[str, Any]) -> None:
    if not temp_root.exists():
        entries: list[TempEntry] = []
    else:
        try:
            entries = snapshot_temp(root, temp_root)
        except (TempRegistryError, OSError) as error:
            entries = []
            manifest.setdefault("cleanup", {})["registry_error"] = type(error).__name__
    manifest["temp_registry"] = [entry.to_dict() for entry in entries]
    manifest["phase"] = "cleanup_registered"
    manifest.setdefault("cleanup", {})["status"] = "registered"
    _write_json_atomic(root / MANIFEST_NAME, manifest)
    _persist_job_stage5(job, manifest)
    StateMachine.transition(job, JobState.CLEANING)
    save_job(job, job_file)


def _run_cleanup(job: Job, root: Path, temp_root: Path, output_root: Path, job_file: Path, manifest: dict[str, Any]) -> OutputPipelineOutcome:
    _verify_output_manifest(output_root, manifest)
    cleanup = manifest.setdefault("cleanup", {})
    if cleanup.get("registry_error"):
        try:
            manifest["temp_registry"] = [entry.to_dict() for entry in snapshot_temp(root, temp_root)] if temp_root.exists() else []
            cleanup.pop("registry_error", None)
            _write_json_atomic(root / MANIFEST_NAME, manifest)
        except (TempRegistryError, OSError) as error:
            failures = [{"path": "temp", "cause": type(error).__name__}]
            cleanup["attempts"] = int(cleanup.get("attempts", 0)) + 1
            cleanup["status"] = "failed"
            cleanup["unremoved_paths"] = ["temp"]
            cleanup["failures"] = failures
            manifest["phase"] = "cleanup_failed"
            _write_json_atomic(root / MANIFEST_NAME, manifest)
            _persist_job_stage5(job, manifest)
            StateMachine.apply_error(job, CoreError.for_code(
                ErrorCode.CLEANUP_FAILED,
                message="Could not establish a safe temp ownership registry.",
                cause=type(error).__name__,
                retry=RetryMetadata(attempt=int(cleanup["attempts"]), resume_from_state=JobState.CLEANING.value),
            ))
            save_job(job, job_file)
            return OutputPipelineOutcome(job, job_file, job.output_result, job.error)

    raw_entries = manifest.get("temp_registry")
    if not isinstance(raw_entries, list):
        raise OutputFinalizationError("Cleanup cannot run without a persisted temp ownership registry")
    entries = [TempEntry.from_dict(item) for item in raw_entries if isinstance(item, dict)]
    cleanup["attempts"] = int(cleanup.get("attempts", 0)) + 1
    cleanup["status"] = "running"
    cleanup["unremoved_paths"] = []
    cleanup["failures"] = []
    manifest["phase"] = "cleaning"
    _write_json_atomic(root / MANIFEST_NAME, manifest)
    _persist_job_stage5(job, manifest)
    save_job(job, job_file)

    failures = cleanup_temp_registry(root, temp_root, entries, remove_file=_unlink_registered_file)
    if failures:
        cleanup["status"] = "failed"
        cleanup["unremoved_paths"] = sorted({item.get("path", "temp") for item in failures})
        cleanup["failures"] = failures
        manifest["phase"] = "cleanup_failed"
        _write_json_atomic(root / MANIFEST_NAME, manifest)
        _persist_job_stage5(job, manifest)
        attempts = int(cleanup["attempts"])
        error = CoreError.for_code(
            ErrorCode.CLEANUP_FAILED,
            message="Job output is committed, but registered temporary artifacts could not all be removed.",
            cause=json.dumps(failures, ensure_ascii=False, sort_keys=True),
            retry=RetryMetadata(attempt=attempts, resume_from_state=JobState.CLEANING.value),
        )
        StateMachine.apply_error(job, error)
        save_job(job, job_file)
        return OutputPipelineOutcome(job, job_file, job.output_result, error)

    cleanup["status"] = "complete"
    cleanup["unremoved_paths"] = []
    cleanup["failures"] = []
    manifest["phase"] = "cleanup_complete"
    _write_json_atomic(root / MANIFEST_NAME, manifest)
    _persist_job_stage5(job, manifest)
    StateMachine.transition(job, JobState.COMPLETED)
    save_job(job, job_file)
    return OutputPipelineOutcome(job, job_file, job.output_result, None)


def _failure(job: Job, job_file: Path, error: Exception) -> OutputPipelineOutcome:
    if job.current_state is not JobState.COMPLETED and JobState.PROVIDER_FAILED in StateMachine.allowed_next(job.current_state):
        core_error = CoreError.for_code(ErrorCode.PROVIDER_FAILED, message="Stage 5 could not safely finalize this Job.", cause=type(error).__name__)
        StateMachine.apply_error(job, core_error)
        try:
            resolved_root = Path(job.workspace_path).expanduser().resolve(strict=False)
            destination = Path(job_file).expanduser()
            if (not Path(job.workspace_path).is_symlink() and resolved_root.name == f"job-{job.job_id}"
                    and not destination.is_symlink() and destination.resolve(strict=False) == resolved_root / "job.json"):
                save_job(job, destination)
        except (OSError, ValueError):
            pass
        return OutputPipelineOutcome(job, job_file, None, core_error)
    core_error = CoreError.for_code(ErrorCode.PROVIDER_FAILED, message="Stage 5 could not safely finalize this Job.", cause=type(error).__name__)
    return OutputPipelineOutcome(job, job_file, job.output_result, core_error)


def _run_locked(job: Job, root: Path, temp_root: Path, output_root: Path, job_file: Path) -> OutputPipelineOutcome:
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = _load_manifest(root)
        if job.current_state is JobState.COMPLETED:
            if manifest is None or manifest.get("phase") not in {"cleanup_complete", "completed"}:
                raise OutputFinalizationError("COMPLETED Job has no completed Stage 5 manifest")
            _validate_manifest_binding(job, manifest)
            _verify_output_manifest(output_root, manifest)
            return OutputPipelineOutcome(job, job_file, _result_from_manifest(manifest), None)

        if job.current_state is JobState.CLEANUP_FAILED:
            if manifest is None:
                raise OutputFinalizationError("CLEANUP_FAILED Job has no resumable Stage 5 manifest")
            _validate_manifest_binding(job, manifest)
            StateMachine.resume(job, JobState.CLEANING)
            save_job(job, job_file)
            return _run_cleanup(job, root, temp_root, output_root, job_file, manifest)

        if job.current_state is JobState.CLEANING:
            if manifest is None:
                raise OutputFinalizationError("CLEANING Job has no persisted Stage 5 manifest")
            _validate_manifest_binding(job, manifest)
            if manifest.get("phase") not in {"cleanup_registered", "cleaning", "cleanup_failed", "cleanup_complete"}:
                raise OutputFinalizationError("CLEANING Job manifest is not ready for cleanup")
            if manifest.get("phase") == "cleanup_complete":
                _verify_output_manifest(output_root, manifest)
                StateMachine.transition(job, JobState.COMPLETED)
                save_job(job, job_file)
                return OutputPipelineOutcome(job, job_file, _result_from_manifest(manifest), None)
            return _run_cleanup(job, root, temp_root, output_root, job_file, manifest)

        if job.current_state not in {JobState.DOWNLOADING, JobState.TRANSCRIBING, JobState.TRANSLATING, JobState.RENDERING, JobState.DOCUMENTING}:
            raise OutputFinalizationError(f"Job state {job.current_state.value} is not ready for Stage 5")

        if manifest is None and job.resolved_content_type is ContentType.VIDEO and "publish_assist" not in job.source_metadata:
            # Plan 12.4: computed once before output is planned, persisted so a
            # resumed finalization reuses it; it never blocks delivery.
            job.source_metadata["publish_assist"] = _publish_assist(job, temp_root)
            save_job(job, job_file)

        if manifest is None:
            records = _collect_current_records(job, root, temp_root)
            if job.current_state is not JobState.DOCUMENTING:
                StateMachine.transition(job, JobState.DOCUMENTING)
                save_job(job, job_file)
            manifest = _create_manifest(job, root, temp_root, output_root, records)
        else:
            _validate_manifest_binding(job, manifest)
            if manifest.get("phase") in {"output_committed", "cleanup_registered", "cleaning", "cleanup_failed", "cleanup_complete"}:
                _verify_output_manifest(output_root, manifest)
            elif manifest.get("phase") in {"planned", "promoting"}:
                _collect_current_records(job, root, temp_root)
            else:
                raise OutputFinalizationError("Stage 5 manifest has an unknown phase")

        if manifest.get("phase") in {"planned", "promoting"}:
            if job.current_state is not JobState.DOCUMENTING:
                StateMachine.transition(job, JobState.DOCUMENTING)
                save_job(job, job_file)
            _promote_content(job, root, temp_root, output_root, job_file, manifest)
        if manifest.get("phase") == "output_committed":
            _verify_output_manifest(output_root, manifest)
            _register_cleanup(job, root, temp_root, job_file, manifest)
            manifest = _load_manifest(root) or manifest
        if manifest.get("phase") in {"cleanup_registered", "cleaning", "cleanup_failed"}:
            if job.current_state is JobState.DOCUMENTING:
                StateMachine.transition(job, JobState.CLEANING)
                save_job(job, job_file)
            return _run_cleanup(job, root, temp_root, output_root, job_file, manifest)
        if manifest.get("phase") == "cleanup_complete":
            if job.current_state is JobState.CLEANING:
                StateMachine.transition(job, JobState.COMPLETED)
                save_job(job, job_file)
            return OutputPipelineOutcome(job, job_file, _result_from_manifest(manifest), None)
        raise OutputFinalizationError("Stage 5 did not reach a resumable output phase")
    except (OutputFinalizationError, TempRegistryError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return _failure(job, job_file, error)


def _publish_assist(job: Job, temp_root: Path) -> dict[str, Any]:
    from src.core.video_stage3_pipeline import TRANSLATION_HELPER
    from src.media.publish_assist import apple_title_translator, build_publish_assist

    probe = (job.provider_metadata("yt-dlp") or {}).get("probe")
    probe = probe if isinstance(probe, Mapping) else {}
    stage3 = job.source_metadata.get("stage3")
    stage3 = stage3 if isinstance(stage3, Mapping) else {}
    subtitle = stage3.get("subtitle_discovery") if isinstance(stage3.get("subtitle_discovery"), Mapping) else {}
    asr = stage3.get("asr") if isinstance(stage3.get("asr"), Mapping) else {}
    language = probe.get("original_language") or subtitle.get("primary_language_code") or asr.get("detected_language")
    from src.media.publish_writer import publish_writer

    return build_publish_assist(
        original_title=str(probe.get("title") or ""),
        original_language=str(language) if language else None,
        temp_dir=temp_root,
        stage3=stage3,
        translator=apple_title_translator(TRANSLATION_HELPER, temp_root / "publish_assist"),
        author=str(probe.get("author") or ""),
        source_details=probe.get("source_details") if isinstance(probe.get("source_details"), Mapping) else None,
        duration_seconds=probe.get("duration_seconds"),
        writer=publish_writer(),
    )


def run_output_pipeline(job: Job, *, job_file: Path | None = None) -> OutputPipelineOutcome:
    """Finalize persisted validated artifacts and clean only this Job's registry."""
    try:
        root, temp_root, output_root, destination = _job_context(job, job_file)
        with _job_lock(root):
            persisted = Job.from_json(destination.read_text(encoding="utf-8"))
            if persisted.job_id != job.job_id or persisted.source_url != job.source_url:
                raise OutputFinalizationError("Persisted Job identity does not match the requested Job")
            job.__dict__.update(persisted.__dict__)
            return _run_locked(job, root, temp_root, output_root, destination)
    except (OutputFinalizationError, OSError, ValueError, json.JSONDecodeError) as error:
        destination = Path(job_file or Path(job.workspace_path) / "job.json")
        core_error = CoreError.for_code(ErrorCode.PROVIDER_FAILED, message="Stage 5 could not safely finalize this Job.", cause=type(error).__name__)
        return OutputPipelineOutcome(job, destination, job.output_result, core_error)


__all__ = ["OutputFinalizationError", "OutputPipelineOutcome", "run_output_pipeline"]
