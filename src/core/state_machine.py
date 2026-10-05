"""Explicit local Job transition contract."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CoreError
from .job import Job, JobState


OPTIONAL_PROCESSING_STATES = frozenset({
    JobState.TRANSCRIBING,
    JobState.TRANSLATING,
    JobState.RENDERING,
})

SUCCESS_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.CLAIMED}),
    JobState.CLAIMED: frozenset({JobState.PROBING}),
    JobState.PROBING: frozenset({JobState.DOWNLOADING}),
    JobState.DOWNLOADING: frozenset({
        JobState.TRANSCRIBING,
        JobState.TRANSLATING,
        JobState.RENDERING,
        JobState.DOCUMENTING,
    }),
    JobState.TRANSCRIBING: frozenset({JobState.TRANSLATING, JobState.RENDERING, JobState.DOCUMENTING}),
    JobState.TRANSLATING: frozenset({JobState.RENDERING, JobState.DOCUMENTING}),
    JobState.RENDERING: frozenset({JobState.DOCUMENTING}),
    JobState.DOCUMENTING: frozenset({JobState.CLEANING}),
    JobState.CLEANING: frozenset({JobState.COMPLETED}),
    JobState.COMPLETED: frozenset(),
}

ERROR_DESTINATIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.URL_INVALID, JobState.API_FAILED}),
    JobState.CLAIMED: frozenset({JobState.URL_INVALID, JobState.API_FAILED, JobState.NETWORK_PAUSED}),
    JobState.PROBING: frozenset({
        JobState.UNKNOWN_CONTENT, JobState.URL_INVALID, JobState.AUTH_REQUIRED,
        JobState.NETWORK_PAUSED, JobState.PROVIDER_FAILED, JobState.API_FAILED,
    }),
    JobState.DOWNLOADING: frozenset({
        JobState.URL_INVALID, JobState.AUTH_REQUIRED, JobState.NETWORK_PAUSED,
        JobState.PROVIDER_FAILED, JobState.PARTIAL_FAILURE,
    }),
    JobState.TRANSCRIBING: frozenset({JobState.SUBTITLE_FAILED, JobState.ASR_FAILED, JobState.NETWORK_PAUSED, JobState.PROVIDER_FAILED, JobState.PARTIAL_FAILURE}),
    JobState.TRANSLATING: frozenset({JobState.TRANSLATION_UNSUPPORTED, JobState.NETWORK_PAUSED, JobState.PROVIDER_FAILED, JobState.PARTIAL_FAILURE}),
    JobState.RENDERING: frozenset({JobState.SUBTITLE_FAILED, JobState.PARTIAL_FAILURE, JobState.PROVIDER_FAILED}),
    JobState.DOCUMENTING: frozenset({JobState.PARTIAL_FAILURE, JobState.PROVIDER_FAILED}),
    JobState.CLEANING: frozenset({JobState.CLEANUP_FAILED, JobState.PROVIDER_FAILED}),
}

RECOVERABLE_ERROR_STATES = frozenset({
    JobState.AUTH_REQUIRED,
    JobState.SUBTITLE_FAILED,
    JobState.ASR_FAILED,
    JobState.NETWORK_PAUSED,
    JobState.PARTIAL_FAILURE,
    JobState.CLEANUP_FAILED,
    JobState.API_FAILED,
})

ERROR_STATES = frozenset(set(JobState) - set(SUCCESS_TRANSITIONS))
TERMINAL_STATES = frozenset({JobState.COMPLETED}) | (ERROR_STATES - RECOVERABLE_ERROR_STATES)


@dataclass(frozen=True)
class StateTransitionError(ValueError):
    from_state: JobState
    to_state: JobState
    detail: str = ""

    def __str__(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"invalid transition: {self.from_state.value} -> {self.to_state.value}{suffix}"


class StateMachine:
    """Only Core applies state changes; Provider outputs are converted to CoreError first."""

    @staticmethod
    def allowed_next(state: JobState) -> frozenset[JobState]:
        return SUCCESS_TRANSITIONS.get(state, frozenset()) | ERROR_DESTINATIONS.get(state, frozenset())

    @staticmethod
    def is_terminal(state: JobState) -> bool:
        return state in TERMINAL_STATES

    @staticmethod
    def transition(job: Job, to_state: JobState) -> None:
        if to_state not in StateMachine.allowed_next(job.current_state):
            raise StateTransitionError(job.current_state, to_state)
        job.current_state = to_state
        job.touch()

    @staticmethod
    def apply_error(job: Job, error: CoreError) -> None:
        try:
            error_state = JobState(error.state)
        except ValueError as error_value:
            raise ValueError(f"error does not map to a Job state: {error.state}") from error_value
        resume_from = job.current_state if error.recoverable else None
        StateMachine.transition(job, error_state)
        job.error = error
        job.attempts.resume_from_state = resume_from
        job.touch()

    @staticmethod
    def resume(job: Job, to_state: JobState | None = None) -> None:
        """Explicit recovery path; ordinary transitions cannot leave error states."""

        if job.current_state not in RECOVERABLE_ERROR_STATES:
            destination = to_state or job.current_state
            raise StateTransitionError(job.current_state, destination, "state is not explicitly recoverable")
        if job.current_state is JobState.CLEANUP_FAILED and to_state is not JobState.CLEANING:
            raise StateTransitionError(job.current_state, to_state or job.current_state, "cleanup failures may only resume cleanup")
        destination = to_state or job.attempts.resume_from_state
        if destination is None or destination in ERROR_STATES or destination is JobState.COMPLETED:
            raise StateTransitionError(job.current_state, destination or job.current_state, "missing valid resume target")
        job.current_state = destination
        job.error = None
        job.attempts.count += 1
        job.attempts.resume_from_state = None
        job.touch()
