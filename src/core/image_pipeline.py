"""Core-owned orchestration for Stage 4 IMAGE and IMAGE_SET acquisition."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.core.errors import CoreError, RetryMetadata, map_provider_failure
from src.core.job import ContentType, Job, JobState
from src.core.router import resolve_content_type
from src.core.state_machine import RECOVERABLE_ERROR_STATES, StateMachine
from src.core.video_pipeline import _job_file_for, save_job
from src.providers.base import ContentProvider, ProviderFailure, ProviderFailureKind, ProviderResult


IMAGE_CONTENT_TYPES = frozenset({ContentType.IMAGE, ContentType.IMAGE_SET})


@dataclass(frozen=True)
class ImagePipelineOutcome:
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


def _normalized_type(result: ProviderResult, provider_name: str) -> ContentType:
    probe = result.metadata.get("probe")
    value = probe.get("content_type") if isinstance(probe, dict) else None
    try:
        content_type = ContentType(str(value))
    except ValueError as error:
        raise ProviderFailure(
            kind=ProviderFailureKind.FAILED,
            provider=provider_name,
            message="Image Probe returned an invalid normalized content type.",
        ) from error
    if content_type not in IMAGE_CONTENT_TYPES:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "Image Probe did not resolve IMAGE or IMAGE_SET.")
    return content_type


def _store_probe(job: Job, provider_name: str, result: ProviderResult) -> None:
    checkpoint = _provider_checkpoint(job, provider_name)
    old = job.provider_metadata(provider_name) or {}
    old_probe = old.get("probe") if isinstance(old.get("probe"), dict) else {}
    new_probe = result.metadata.get("probe") if isinstance(result.metadata.get("probe"), dict) else {}
    old_identity = old_probe.get("source_identity")
    new_identity = new_probe.get("source_identity")
    same_identity = bool(old_identity and new_identity and old_identity == new_identity)
    updated: dict[str, Any] = {"probe": dict(new_probe)}
    if same_identity:
        for key in ("fetch", "partial"):
            if key in old:
                updated[key] = old[key]
    job.store_provider_metadata(provider_name, updated)
    checkpoint["resume_token"] = result.resume_token
    checkpoint["source_identity"] = new_identity
    checkpoint["probe_persisted"] = True
    if not same_identity:
        checkpoint.pop("fetch_manifest", None)


def _resolve_probe_type(job: Job, provider_name: str, result: ProviderResult) -> ContentType:
    probed_type = _normalized_type(result, provider_name)
    resolution = resolve_content_type(declared=job.declared_content_type, probe=probed_type)
    if resolution.content_type is not probed_type:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "Declared content type does not match the image count returned by Probe.")
    job.resolved_content_type = resolution.content_type
    job.source_metadata["resolution_origin"] = resolution.origin.value
    job.source_metadata["declared_type_locked"] = resolution.declared_locked
    return resolution.content_type


def _apply_failure(job: Job, provider_name: str, failure: ProviderFailure, destination: Path) -> ImagePipelineOutcome:
    checkpoint = _provider_checkpoint(job, provider_name)
    if failure.resume_token:
        checkpoint["resume_token"] = failure.resume_token
    if failure.kind.value == "PARTIAL":
        details = failure.details.get("image_partial")
        if isinstance(details, dict):
            partial: dict[str, Any] = {}
            for key in ("expected_count", "successful_items", "failed_items", "manifest_path"):
                if key in details:
                    partial[key] = details[key]
            metadata = job.provider_metadata(provider_name) or {}
            metadata["partial"] = partial
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
    return ImagePipelineOutcome(job, destination, None, error)


def run_image_pipeline(
    job: Job,
    provider: ContentProvider,
    *,
    job_file: Path | None = None,
) -> ImagePipelineOutcome:
    """Probe/fetch an image source while leaving every Job transition in Core."""

    destination = _job_file_for(job, job_file)
    if job.current_state in RECOVERABLE_ERROR_STATES:
        StateMachine.resume(job)
        save_job(job, destination)
    elif job.error is not None:
        return ImagePipelineOutcome(job, destination, None, job.error)

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
            _resolve_probe_type(job, provider.name, probe_result)
        except ProviderFailure as failure:
            return _apply_failure(job, provider.name, failure, destination)
        _store_probe(job, provider.name, probe_result)
        save_job(job, destination)
        StateMachine.transition(job, JobState.DOWNLOADING)
        save_job(job, destination)
        initial_probe = True

    if job.current_state is not JobState.DOWNLOADING:
        return ImagePipelineOutcome(job, destination, None, job.error)

    # A new process cannot hold Provider-local signed media URLs. Re-probe on
    # every resume, then compare the canonical source identity before fetching.
    if not initial_probe:
        try:
            probe_result = provider.probe(job)
            _resolve_probe_type(job, provider.name, probe_result)
        except ProviderFailure as failure:
            return _apply_failure(job, provider.name, failure, destination)
        _store_probe(job, provider.name, probe_result)
        save_job(job, destination)

    checkpoint = _provider_checkpoint(job, provider.name)
    resume_token = checkpoint.get("resume_token")
    try:
        result = provider.fetch(job, resume_token=resume_token if isinstance(resume_token, str) else None)
    except ProviderFailure as failure:
        return _apply_failure(job, provider.name, failure, destination)
    job.store_provider_metadata(provider.name, result.metadata)
    checkpoint["resume_token"] = result.resume_token or resume_token
    checkpoint["source_identity"] = result.resume_token or checkpoint.get("source_identity")
    checkpoint["probe_persisted"] = True
    save_job(job, destination)
    return ImagePipelineOutcome(job, destination, result, None)


__all__ = ["ImagePipelineOutcome", "run_image_pipeline"]
