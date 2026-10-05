"""Provider-neutral Queue task and HTTP response contracts.

The Apps Script Queue is the only persistence/state-machine owner. These
dataclasses define the stable boundary consumed by a future local client; they
do not implement Worker polling or Queue transitions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping

from src.core.errors import CoreError, ErrorCode, ERROR_SPECS


class QueueStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PROCESSING = "PROCESSING"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class QueueApiErrorCode(StrEnum):
    AUTH_FAILED = "AUTH_FAILED"
    INVALID_REQUEST = "INVALID_REQUEST"
    QUEUE_EMPTY = "QUEUE_EMPTY"
    QUEUE_NOT_FOUND = "QUEUE_NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    CLAIM_TOKEN_INVALID = "CLAIM_TOKEN_INVALID"
    CLAIM_REQUEST_EXPIRED = "CLAIM_REQUEST_EXPIRED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    CONFIG_INVALID = "CONFIG_INVALID"


QUEUE_FIELDS = (
    "queue_id",
    "video_id",
    "url",
    "status",
    "claim_token",
    "claimed_at",
    "lease_until",
    "attempts",
    "last_error",
    "local_job_id",
    "completed_at",
    "result_path",
    "claim_request_id",
    "selection_day",
    "selection_rank",
    "rank2_start_cutoff_at",
    "core_started_at",
)


@dataclass(frozen=True)
class QueueTask:
    queue_id: str
    video_id: str
    url: str
    status: QueueStatus = QueueStatus.PENDING
    claim_token: str | None = None
    claimed_at: str | None = None
    lease_until: str | None = None
    attempts: int = 0
    last_error: str | None = None
    local_job_id: str | None = None
    completed_at: str | None = None
    result_path: str | None = None
    claim_request_id: str | None = None
    selection_day: str | None = None
    selection_rank: int | None = None
    rank2_start_cutoff_at: str | None = None
    core_started_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True)
class QueueApiResponse:
    ok: bool
    data: Mapping[str, Any] | None = None
    error: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "data": dict(self.data) if self.data is not None else None}
        if self.error is None:
            raise ValueError("failed Queue response requires an error object")
        return {"ok": False, "error": dict(self.error)}

    @classmethod
    def success(cls, data: Mapping[str, Any] | None) -> "QueueApiResponse":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, code: QueueApiErrorCode | str, message: str) -> "QueueApiResponse":
        return cls(ok=False, error={"code": QueueApiErrorCode(code).value, "message": str(message)})


def queue_status_for_error(error: CoreError | Mapping[str, Any]) -> QueueStatus:
    """Map the existing Stage 1 error contract to Queue PAUSED or FAILED.

    Recoverability is always derived from ``ERROR_SPECS``. A caller-supplied
    ``recoverable`` flag is never treated as a second source of truth.
    """

    normalized = error if isinstance(error, CoreError) else CoreError.from_dict(error)
    if normalized is None:
        raise ValueError("Queue failure requires a Stage 1 CoreError")
    # Resolve by the Stage 1 code table so additions remain coupled to Core.
    spec = ERROR_SPECS[ErrorCode(normalized.code)]
    return QueueStatus.PAUSED if spec.recoverable else QueueStatus.FAILED
