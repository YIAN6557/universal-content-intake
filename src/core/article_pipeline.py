"""Core-owned orchestration for Stage 4 ARTICLE acquisition."""

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


@dataclass(frozen=True)
class ArticlePipelineOutcome:
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
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Article Probe returned no normalized probe metadata.")
    try:
        content_type = ContentType(str(probe.get("content_type")))
    except ValueError as error:
        raise ProviderFailure(ProviderFailureKind.FAILED, provider_name, "Article Probe returned an invalid content type.") from error
    if content_type is not ContentType.ARTICLE:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "Article Provider did not resolve ARTICLE.")
    resolution = resolve_content_type(declared=job.declared_content_type, probe=content_type)
    if resolution.content_type is not ContentType.ARTICLE:
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, provider_name, "The locked Job content type is not ARTICLE.")
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


def _apply_failure(job: Job, provider_name: str, failure: ProviderFailure, destination: Path) -> ArticlePipelineOutcome:
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
        error = replace(error, retry=RetryMetadata(
            attempt=job.attempts.count + 1,
            retry_after_seconds=failure.retry_after_seconds,
            resume_from_state=job.current_state.value,
        ))
    StateMachine.apply_error(job, error)
    save_job(job, destination)
    return ArticlePipelineOutcome(job, destination, None, error)


def run_article_pipeline(
    job: Job,
    provider: ContentProvider,
    *,
    job_file: Path | None = None,
) -> ArticlePipelineOutcome:
    """Probe/fetch an Article source; Providers never change Job state."""
    destination = _job_file_for(job, job_file)
    if job.current_state in RECOVERABLE_ERROR_STATES:
        StateMachine.resume(job)
        save_job(job, destination)
    elif job.error is not None:
        return ArticlePipelineOutcome(job, destination, None, job.error)

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
        return ArticlePipelineOutcome(job, destination, None, job.error)

    # Probe is intentionally local/offline; repeating it on resume detects a
    # changed source URL without fetching or extracting the page a second time.
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
    except ProviderFailure as failure:
        return _apply_failure(job, provider.name, failure, destination)
    job.store_provider_metadata(provider.name, result.metadata)
    checkpoint["resume_token"] = result.resume_token or resume_token
    checkpoint["source_identity"] = result.resume_token or checkpoint.get("source_identity")
    checkpoint["probe_persisted"] = True
    save_job(job, destination)
    return ArticlePipelineOutcome(job, destination, result, None)


__all__ = ["ArticlePipelineOutcome", "run_article_pipeline"]
