"""Core-owned orchestration for Stage 4 WEBPAGE acquisition."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.core.errors import CoreError, RetryMetadata, map_provider_failure
from src.core.job import ContentType, Job, JobState
from src.core.router import resolve_content_type
from src.core.state_machine import RECOVERABLE_ERROR_STATES, StateMachine
from src.core.video_pipeline import _job_file_for, save_job
from src.providers.base import ContentProvider, ProviderFailure, ProviderFailureKind, ProviderResult


WEBPAGE_ROLES = frozenset({"html", "markdown", "pdf", "manifest"})
CONTENT_ROLES = frozenset({"html", "markdown", "pdf"})


@dataclass(frozen=True)
class WebpagePipelineOutcome:
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
    value = providers.setdefault(provider_name, {})
    if not isinstance(value, dict):
        raise ValueError("Job Provider checkpoint must be a mapping")
    return value


def _store_probe(job: Job, provider_name: str, result: ProviderResult) -> None:
    probe = result.metadata.get("probe")
    if not isinstance(probe, dict):
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage Probe returned no normalized probe metadata.")
    try:
        content_type = ContentType(str(probe.get("content_type")))
    except ValueError as error:
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage Probe returned an invalid content type.") from error
    if content_type is not ContentType.WEBPAGE:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "Webpage Provider did not resolve WEBPAGE.")
    resolution = resolve_content_type(declared=job.declared_content_type, probe=content_type)
    if resolution.content_type is not ContentType.WEBPAGE:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "The locked Job content type is not WEBPAGE.")
    job.resolved_content_type = resolution.content_type
    job.source_metadata["resolution_origin"] = resolution.origin.value
    job.source_metadata["declared_type_locked"] = resolution.declared_locked

    checkpoint = _provider_checkpoint(job, provider_name)
    old = job.provider_metadata(provider_name) or {}
    old_probe = old.get("probe") if isinstance(old.get("probe"), dict) else {}
    old_identity = old_probe.get("source_identity")
    new_identity = probe.get("source_identity")
    same_identity = bool(old_identity and new_identity and old_identity == new_identity)
    updated: dict[str, Any] = {"probe": dict(probe)}
    if same_identity and isinstance(old.get("fetch"), dict):
        updated["fetch"] = old["fetch"]
    job.store_provider_metadata(provider_name, updated)
    checkpoint["resume_token"] = result.resume_token
    checkpoint["source_identity"] = new_identity
    checkpoint["probe_persisted"] = True
    if not same_identity:
        checkpoint.pop("fetch_manifest", None)


def _validate_complete_deliverable(job: Job, provider_name: str, result: ProviderResult) -> None:
    roles: dict[str, Path] = {}
    workspace = Path(job.workspace_path).expanduser().resolve(strict=True)
    temp = Path(job.temp_path).expanduser().resolve(strict=True)
    if temp != workspace / "temp":
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage temp root is not the canonical Job temp directory.")
    for artifact in result.artifacts:
        if artifact.role not in WEBPAGE_ROLES or artifact.role in roles:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage Provider returned an invalid artifact role set.")
        relative = Path(artifact.relative_path)
        if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage artifact path is not a safe Job-relative path.")
        candidate = workspace / relative
        if candidate.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage artifact cannot be a symlink.")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(temp)
        except (OSError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage artifact escaped Job temp or is missing.") from error
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage artifact is empty or not a regular file.")
        roles[artifact.role] = resolved
    if set(roles) != WEBPAGE_ROLES or not CONTENT_ROLES.issubset(roles):
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "The Webpage logical deliverable requires HTML, Markdown, PDF, and a manifest.")
    fetch = result.metadata.get("fetch")
    hashes = fetch.get("artifact_hashes") if isinstance(fetch, dict) else None
    if not isinstance(hashes, dict):
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Webpage Provider returned no artifact hash manifest.")
    for role in CONTENT_ROLES:
        try:
            digest = hashlib.sha256(roles[role].read_bytes()).hexdigest()
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, f"Webpage {role} artifact could not be re-read for hash validation.") from error
        if hashes.get(role) != digest:
            raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, f"Webpage {role} hash does not match the manifest.")


def _apply_failure(job: Job, provider_name: str, failure: ProviderFailure, destination: Path) -> WebpagePipelineOutcome:
    checkpoint = _provider_checkpoint(job, provider_name)
    if failure.resume_token:
        checkpoint["resume_token"] = failure.resume_token
    error = map_provider_failure(
        failure.kind.value,
        provider=provider_name,
        cause=failure.message,
        retry_after_seconds=failure.retry_after_seconds,
    )
    if error.retry is not None:
        error = replace(
            error,
            retry=RetryMetadata(
                attempt=job.attempts.count + 1,
                retry_after_seconds=failure.retry_after_seconds,
                resume_from_state=job.current_state.value,
            ),
        )
    StateMachine.apply_error(job, error)
    save_job(job, destination)
    return WebpagePipelineOutcome(job, destination, None, error)


def run_webpage_pipeline(
    job: Job,
    provider: ContentProvider,
    *,
    job_file: Path | None = None,
) -> WebpagePipelineOutcome:
    """Probe and produce the complete three-artifact Webpage deliverable.

    Provider operations return data and files only. Core owns Job state changes,
    checkpoints, error mapping, and validation that the logical deliverable is
    complete before reporting success.
    """

    destination = _job_file_for(job, job_file)
    if job.current_state in RECOVERABLE_ERROR_STATES:
        StateMachine.resume(job)
        save_job(job, destination)
    elif job.error is not None:
        return WebpagePipelineOutcome(job, destination, None, job.error)

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
        return WebpagePipelineOutcome(job, destination, None, job.error)

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
        _validate_complete_deliverable(job, provider.name, result)
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
    return WebpagePipelineOutcome(job, destination, result, None)


__all__ = ["WebpagePipelineOutcome", "run_webpage_pipeline"]
