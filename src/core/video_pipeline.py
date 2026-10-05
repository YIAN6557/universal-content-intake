"""Core-owned orchestration for the Stage 2 VIDEO acquisition boundary."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from src.core.errors import CoreError, RetryMetadata, map_provider_failure
from src.core.job import Job, JobState
from src.core.state_machine import RECOVERABLE_ERROR_STATES, StateMachine
from src.providers.base import ContentProvider, ProviderFailure, ProviderResult


@dataclass(frozen=True)
class VideoPipelineOutcome:
    job: Job
    job_file: Path
    result: ProviderResult | None
    error: CoreError | None

    @property
    def success(self) -> bool:
        return self.result is not None and self.error is None


def _job_file_for(job: Job, job_file: Path | None) -> Path:
    workspace = Path(job.workspace_path).expanduser()
    expected = workspace / "job.json"
    candidate = (job_file or expected).expanduser()
    if workspace.is_symlink() or candidate.is_symlink():
        raise ValueError("Job contract workspace and file cannot be symlinks")
    try:
        resolved_workspace = workspace.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=False)
    except OSError as error:
        raise ValueError("Job contract workspace is unavailable") from error
    if resolved_candidate != resolved_workspace / "job.json":
        raise ValueError("Job contract file must be workspace/job.json")
    return resolved_workspace / "job.json"


def save_job(job: Job, job_file: Path) -> None:
    """Atomically persist Core state without following a pending-file symlink."""

    destination = _job_file_for(job, job_file)
    if destination.is_symlink():
        raise ValueError("Job contract file cannot be a symlink")
    descriptor, pending_name = tempfile.mkstemp(prefix=".job.json.", suffix=".pending", dir=destination.parent)
    pending = Path(pending_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(job.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, destination)
    except BaseException:
        try:
            pending.unlink()
        except OSError:
            pass
        raise


def _provider_checkpoint(job: Job, provider: str) -> dict[str, object]:
    providers = job.attempts.checkpoint.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("Job provider checkpoints must be a mapping")
    value = providers.setdefault(provider, {})
    if not isinstance(value, dict):
        raise ValueError("Job Provider checkpoint must be a mapping")
    return value


def _persist_probe(job: Job, provider: ContentProvider, result: ProviderResult) -> None:
    job.store_provider_metadata(provider.name, result.metadata)
    checkpoint = _provider_checkpoint(job, provider.name)
    checkpoint["resume_token"] = result.resume_token
    checkpoint["probe_persisted"] = True


def _safe_authentication_metadata(failure: ProviderFailure) -> dict[str, object] | None:
    """Persist only the credential-free auth outcome, never arbitrary failure details."""
    raw = failure.details.get("authentication")
    if not isinstance(raw, dict):
        return None
    attempted = raw.get("auth_attempted")
    source = raw.get("auth_source")
    retry_succeeded = raw.get("authenticated_retry")
    result = raw.get("auth_result")
    if attempted is False and source is None and retry_succeeded is False and result == "not_attempted":
        return {
            "auth_attempted": False,
            "auth_source": None,
            "authenticated_retry": False,
            "auth_result": "not_attempted",
        }
    if attempted is True and source == "chrome" and retry_succeeded is True and result == "success":
        return {
            "auth_attempted": True,
            "auth_source": "chrome",
            "authenticated_retry": True,
            "auth_result": "success",
        }
    if attempted is True and source == "chrome" and retry_succeeded is False and result == "failure":
        return {
            "auth_attempted": True,
            "auth_source": "chrome",
            "authenticated_retry": False,
            "auth_result": "failure",
        }
    return None


def run_video_pipeline(
    job: Job,
    provider: ContentProvider,
    *,
    job_file: Path | None = None,
) -> VideoPipelineOutcome:
    """Probe and acquire one VIDEO source while Core exclusively owns Job state."""

    destination = _job_file_for(job, job_file)
    if job.current_state in RECOVERABLE_ERROR_STATES:
        StateMachine.resume(job)
        save_job(job, destination)
    elif job.error is not None:
        return VideoPipelineOutcome(job, destination, None, job.error)

    if job.current_state is JobState.QUEUED:
        StateMachine.transition(job, JobState.CLAIMED)
        save_job(job, destination)
    if job.current_state is JobState.CLAIMED:
        StateMachine.transition(job, JobState.PROBING)
        save_job(job, destination)

    if job.current_state is JobState.PROBING:
        metadata = job.provider_metadata(provider.name)
        checkpoint = _provider_checkpoint(job, provider.name)
        if metadata and metadata.get("probe") and metadata.get("selection") and checkpoint.get("probe_persisted"):
            probe_result = ProviderResult(metadata=metadata, resume_token=checkpoint.get("resume_token"))
        else:
            try:
                probe_result = provider.probe(job)
            except ProviderFailure as failure:
                return _apply_provider_failure(job, provider, failure, destination)
            _persist_probe(job, provider, probe_result)
            save_job(job, destination)
        StateMachine.transition(job, JobState.DOWNLOADING)
        save_job(job, destination)

    if job.current_state is not JobState.DOWNLOADING:
        return VideoPipelineOutcome(job, destination, None, job.error)

    checkpoint = _provider_checkpoint(job, provider.name)
    resume_token = checkpoint.get("resume_token")
    if not isinstance(resume_token, str) or not resume_token:
        try:
            probe_result = provider.probe(job)
        except ProviderFailure as failure:
            return _apply_provider_failure(job, provider, failure, destination)
        _persist_probe(job, provider, probe_result)
        save_job(job, destination)
        resume_token = probe_result.resume_token
    try:
        result = provider.fetch(job, resume_token=resume_token)
    except ProviderFailure as failure:
        return _apply_provider_failure(job, provider, failure, destination)
    job.store_provider_metadata(provider.name, result.metadata)
    _provider_checkpoint(job, provider.name)["resume_token"] = result.resume_token or resume_token
    save_job(job, destination)
    return VideoPipelineOutcome(job, destination, result, None)


def _apply_provider_failure(
    job: Job,
    provider: ContentProvider,
    failure: ProviderFailure,
    destination: Path,
) -> VideoPipelineOutcome:
    authentication = _safe_authentication_metadata(failure)
    if authentication is not None:
        provider_metadata = job.provider_metadata(provider.name) or {}
        provider_metadata["authentication"] = authentication
        job.store_provider_metadata(provider.name, provider_metadata)
    checkpoint = _provider_checkpoint(job, provider.name)
    if failure.resume_token:
        checkpoint["resume_token"] = failure.resume_token
    error = map_provider_failure(
        failure.kind.value,
        provider=provider.name,
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
    return VideoPipelineOutcome(job, destination, None, error)
