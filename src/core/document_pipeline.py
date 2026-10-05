"""Core-owned orchestration for Stage 4 DOCUMENT acquisition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.core.errors import CoreError, RetryMetadata, map_provider_failure
from src.core.job import ContentType, Job, JobState
from src.core.router import resolve_content_type
from src.core.state_machine import RECOVERABLE_ERROR_STATES, StateMachine
from src.core.video_pipeline import _job_file_for, save_job
from src.providers.base import ContentProvider, ProducedArtifact, ProviderFailure, ProviderFailureKind, ProviderResult


DOCUMENT_ROLES = frozenset({"document_file", "manifest"})


@dataclass(frozen=True)
class DocumentPipelineOutcome:
    job: Job
    job_file: Path
    result: ProviderResult | None
    error: CoreError | None

    @property
    def success(self) -> bool:
        return self.result is not None and self.error is None


def _provider_checkpoint(job: Job, provider_name: str) -> dict[str, Any]:
    providers = job.attempts.checkpoint.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("Job provider checkpoints must be a mapping")
    checkpoint = providers.setdefault(provider_name, {})
    if not isinstance(checkpoint, dict):
        raise ValueError("Job Provider checkpoint must be a mapping")
    return checkpoint


def _store_probe(job: Job, provider_name: str, result: ProviderResult) -> None:
    probe = result.metadata.get("probe")
    if not isinstance(probe, dict):
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document Probe returned no normalized metadata.")
    try:
        probed_type = ContentType(str(probe.get("content_type")))
    except ValueError as error:
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document Probe returned an invalid content type.") from error
    if probed_type is not ContentType.DOCUMENT:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "Document Provider did not resolve DOCUMENT.")
    resolution = resolve_content_type(declared=job.declared_content_type, probe=probed_type)
    if resolution.content_type is not ContentType.DOCUMENT:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "The locked Job content type is not DOCUMENT.")
    job.resolved_content_type = resolution.content_type
    job.source_metadata["resolution_origin"] = resolution.origin.value
    job.source_metadata["declared_type_locked"] = resolution.declared_locked

    checkpoint = _provider_checkpoint(job, provider_name)
    previous = job.provider_metadata(provider_name) or {}
    old_probe = previous.get("probe") if isinstance(previous.get("probe"), dict) else {}
    new_identity = probe.get("source_identity")
    same_identity = bool(new_identity and old_probe.get("source_identity") == new_identity)
    updated: dict[str, Any] = {"probe": dict(probe)}
    if same_identity:
        for key in ("fetch", "partial"):
            if key in previous:
                updated[key] = previous[key]
    job.store_provider_metadata(provider_name, updated)
    checkpoint["resume_token"] = result.resume_token
    checkpoint["source_identity"] = new_identity
    checkpoint["probe_persisted"] = True
    if not same_identity:
        checkpoint.pop("fetch_manifest", None)
        checkpoint.pop("artifact_hashes", None)


def _validate_result(job: Job, provider_name: str, result: ProviderResult) -> None:
    workspace = Path(job.workspace_path).expanduser().resolve(strict=True)
    temp = Path(job.temp_path).expanduser().resolve(strict=True)
    if temp != workspace / "temp":
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document temp root is not the canonical Job temp directory.")
    roles: dict[str, list[Path]] = {"document_file": []}
    for artifact in result.artifacts:
        if artifact.role not in DOCUMENT_ROLES or (artifact.role == "manifest" and "manifest" in roles):
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document Provider returned an invalid artifact role set.")
        relative = Path(artifact.relative_path)
        if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document artifact path is unsafe.")
        candidate = workspace / relative
        if candidate.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document artifact cannot be a symlink.")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(temp)
        except (OSError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document artifact escaped Job temp or is missing.") from error
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document artifact is empty or not a regular file.")
        if artifact.role == "document_file":
            roles["document_file"].append(resolved)
        else:
            roles["manifest"] = [resolved]
    if "manifest" not in roles or not roles["document_file"]:
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "A complete Document deliverable requires one or more files and a manifest.")

    fetch = result.metadata.get("fetch")
    if not isinstance(fetch, dict):
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document Provider returned no fetch metadata.")
    hashes = fetch.get("artifact_hashes")
    records = fetch.get("artifacts")
    if not isinstance(hashes, dict) or not isinstance(records, list) or not records:
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document Provider returned no artifact hash records.")
    if len(records) != len(roles["document_file"]):
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document manifest count does not match produced artifacts.")
    manifest = roles["manifest"][0]
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document manifest is not readable JSON.") from error
    source = manifest_data.get("source") if isinstance(manifest_data, dict) else None
    if not isinstance(source, dict) or source.get("identity") != fetch.get("source_identity") or manifest_data.get("complete") is not True:
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document manifest does not bind the complete source identity.")
    if fetch.get("manifest_sha256") != hashlib.sha256(manifest.read_bytes()).hexdigest():
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document manifest hash mismatch.")

    manifest_records = manifest_data.get("artifacts")
    if not isinstance(manifest_records, list) or len(manifest_records) != len(records):
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document manifest artifact set is inconsistent.")
    manifest_by_path = {item.get("path"): item for item in manifest_records if isinstance(item, dict)}
    for artifact in result.artifacts:
        if artifact.role != "document_file":
            continue
        path = (workspace / artifact.relative_path).resolve(strict=True)
        relative = path.relative_to(workspace).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        record = manifest_by_path.get(relative)
        if not isinstance(record, dict) or record.get("sha256") != digest or hashes.get(relative) != digest:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document artifact SHA-256 does not match its manifest.")
        if int(record.get("size", -1)) != path.stat().st_size:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Document artifact size does not match its manifest.")


def _apply_failure(job: Job, provider_name: str, failure: ProviderFailure, destination: Path) -> DocumentPipelineOutcome:
    checkpoint = _provider_checkpoint(job, provider_name)
    if failure.resume_token:
        checkpoint["resume_token"] = failure.resume_token
    partial = failure.details.get("document_partial")
    if isinstance(partial, dict):
        metadata = job.provider_metadata(provider_name) or {}
        metadata["partial"] = {
            key: partial[key]
            for key in ("successful_items", "failed_items", "manifest_path")
            if key in partial
        }
        job.store_provider_metadata(provider_name, metadata)
    error = map_provider_failure(
        failure.kind.value,
        provider=provider_name,
        cause=failure.message,
        retry_after_seconds=failure.retry_after_seconds,
    )
    if error.retry is not None:
        error = replace(error, retry=RetryMetadata(
            attempt=job.attempts.count + 1,
            retry_after_seconds=failure.retry_after_seconds,
            resume_from_state=job.current_state.value,
        ))
    StateMachine.apply_error(job, error)
    save_job(job, destination)
    return DocumentPipelineOutcome(job, destination, None, error)


def run_document_pipeline(
    job: Job,
    provider: ContentProvider,
    *,
    job_file: Path | None = None,
) -> DocumentPipelineOutcome:
    """Probe/fetch one DOCUMENT Job; Providers never mutate persisted state."""
    destination = _job_file_for(job, job_file)
    if job.current_state in RECOVERABLE_ERROR_STATES:
        StateMachine.resume(job)
        save_job(job, destination)
    elif job.error is not None:
        return DocumentPipelineOutcome(job, destination, None, job.error)

    initial_probe = False
    if job.current_state is JobState.QUEUED:
        StateMachine.transition(job, JobState.CLAIMED)
        save_job(job, destination)
    if job.current_state is JobState.CLAIMED:
        StateMachine.transition(job, JobState.PROBING)
        save_job(job, destination)
    if job.current_state is JobState.PROBING:
        try:
            probe_result = provider.probe(job)
            _store_probe(job, provider.name, probe_result)
        except ProviderFailure as failure:
            return _apply_failure(job, provider.name, failure, destination)
        save_job(job, destination)
        StateMachine.transition(job, JobState.DOWNLOADING)
        save_job(job, destination)
        initial_probe = True

    if job.current_state is not JobState.DOWNLOADING:
        return DocumentPipelineOutcome(job, destination, None, job.error)

    if not initial_probe:
        try:
            probe_result = provider.probe(job)
            _store_probe(job, provider.name, probe_result)
        except ProviderFailure as failure:
            return _apply_failure(job, provider.name, failure, destination)
        save_job(job, destination)

    checkpoint = _provider_checkpoint(job, provider.name)
    resume_token = checkpoint.get("resume_token")
    try:
        result = provider.fetch(job, resume_token=resume_token if isinstance(resume_token, str) else None)
        _validate_result(job, provider.name, result)
    except ProviderFailure as failure:
        return _apply_failure(job, provider.name, failure, destination)
    job.store_provider_metadata(provider.name, result.metadata)
    checkpoint["resume_token"] = result.resume_token or resume_token
    checkpoint["source_identity"] = result.resume_token or checkpoint.get("source_identity")
    checkpoint["probe_persisted"] = True
    fetch = result.metadata.get("fetch")
    if isinstance(fetch, dict):
        checkpoint["fetch_manifest"] = fetch.get("manifest")
        checkpoint["artifact_hashes"] = fetch.get("artifact_hashes")
    save_job(job, destination)
    return DocumentPipelineOutcome(job, destination, result, None)


__all__ = ["DocumentPipelineOutcome", "run_document_pipeline"]
