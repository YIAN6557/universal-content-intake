"""Machine-decidable error contract for the provider-neutral Core."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping


class ErrorCategory(StrEnum):
    VALIDATION = "VALIDATION"
    CONTENT = "CONTENT"
    AUTH = "AUTH"
    SUBTITLE = "SUBTITLE"
    ASR = "ASR"
    TRANSLATION = "TRANSLATION"
    NETWORK = "NETWORK"
    PROVIDER = "PROVIDER"
    CLEANUP = "CLEANUP"
    API = "API"


class ErrorCode(StrEnum):
    UNKNOWN_CONTENT = "UNKNOWN_CONTENT"
    URL_INVALID = "URL_INVALID"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SUBTITLE_FAILED = "SUBTITLE_FAILED"
    ASR_FAILED = "ASR_FAILED"
    TRANSLATION_UNSUPPORTED = "TRANSLATION_UNSUPPORTED"
    NETWORK_PAUSED = "NETWORK_PAUSED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    API_FAILED = "API_FAILED"


@dataclass(frozen=True)
class ErrorSpec:
    category: ErrorCategory
    state: str
    recoverable: bool
    terminal: bool
    default_message: str


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.UNKNOWN_CONTENT: ErrorSpec(ErrorCategory.CONTENT, "UNKNOWN_CONTENT", False, True, "Content type could not be determined."),
    ErrorCode.URL_INVALID: ErrorSpec(ErrorCategory.VALIDATION, "URL_INVALID", False, True, "The source URL is invalid or unavailable."),
    ErrorCode.AUTH_REQUIRED: ErrorSpec(ErrorCategory.AUTH, "AUTH_REQUIRED", True, False, "The source requires authorization."),
    ErrorCode.SUBTITLE_FAILED: ErrorSpec(ErrorCategory.SUBTITLE, "SUBTITLE_FAILED", True, False, "A confirmed subtitle track could not be retrieved."),
    ErrorCode.ASR_FAILED: ErrorSpec(ErrorCategory.ASR, "ASR_FAILED", True, False, "Automatic speech recognition failed."),
    ErrorCode.TRANSLATION_UNSUPPORTED: ErrorSpec(ErrorCategory.TRANSLATION, "TRANSLATION_UNSUPPORTED", False, True, "The requested translation path is unavailable."),
    ErrorCode.NETWORK_PAUSED: ErrorSpec(ErrorCategory.NETWORK, "NETWORK_PAUSED", True, False, "Network work is paused after retry handling."),
    ErrorCode.PROVIDER_FAILED: ErrorSpec(ErrorCategory.PROVIDER, "PROVIDER_FAILED", False, True, "The selected Provider could not handle this source."),
    ErrorCode.PARTIAL_FAILURE: ErrorSpec(ErrorCategory.PROVIDER, "PARTIAL_FAILURE", True, False, "Only part of a multi-file result was produced; verified items are retained for explicit resume."),
    ErrorCode.CLEANUP_FAILED: ErrorSpec(ErrorCategory.CLEANUP, "CLEANUP_FAILED", True, False, "Temporary files could not be completely removed."),
    ErrorCode.API_FAILED: ErrorSpec(ErrorCategory.API, "API_FAILED", True, False, "An external API or queue operation failed."),
}


@dataclass(frozen=True)
class RetryMetadata:
    """Optional retry information, expressed without scheduling any retry."""

    attempt: int = 0
    max_attempts: int | None = None
    retry_after_seconds: int | None = None
    resume_from_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "RetryMetadata | None":
        if value is None:
            return None
        return cls(
            attempt=int(value.get("attempt", 0)),
            max_attempts=value.get("max_attempts"),
            retry_after_seconds=value.get("retry_after_seconds"),
            resume_from_state=value.get("resume_from_state"),
        )


@dataclass(frozen=True)
class CoreError:
    """Stable error data that can cross Core, Provider, JSON, and CLI boundaries."""

    code: ErrorCode
    category: ErrorCategory
    state: str
    recoverable: bool
    terminal: bool
    message: str
    provider: str | None = None
    cause: str | None = None
    retry: RetryMetadata | None = None

    def __post_init__(self) -> None:
        spec = ERROR_SPECS[self.code]
        if (
            self.category != spec.category
            or self.state != spec.state
            or self.recoverable != spec.recoverable
            or self.terminal != spec.terminal
        ):
            raise ValueError(f"error metadata does not match stable code: {self.code.value}")

    @classmethod
    def for_code(
        cls,
        code: ErrorCode | str,
        *,
        message: str | None = None,
        provider: str | None = None,
        cause: str | None = None,
        retry: RetryMetadata | None = None,
    ) -> "CoreError":
        normalized = ErrorCode(code)
        spec = ERROR_SPECS[normalized]
        return cls(
            code=normalized,
            category=spec.category,
            state=spec.state,
            recoverable=spec.recoverable,
            terminal=spec.terminal,
            message=message or spec.default_message,
            provider=provider,
            cause=cause,
            retry=retry,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "category": self.category.value,
            "state": self.state,
            "recoverable": self.recoverable,
            "terminal": self.terminal,
            "message": self.message,
            "provider": self.provider,
            "cause": self.cause,
            "retry": self.retry.to_dict() if self.retry else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "CoreError | None":
        if value is None:
            return None
        code = ErrorCode(value["code"])
        spec = ERROR_SPECS[code]
        supplied_category = ErrorCategory(value.get("category", spec.category.value))
        if supplied_category != spec.category or value.get("state", spec.state) != spec.state:
            raise ValueError(f"error metadata does not match stable code: {code.value}")
        return cls(
            code=code,
            category=spec.category,
            state=spec.state,
            recoverable=bool(value.get("recoverable", spec.recoverable)),
            terminal=bool(value.get("terminal", spec.terminal)),
            message=str(value.get("message", spec.default_message)),
            provider=value.get("provider"),
            cause=value.get("cause"),
            retry=RetryMetadata.from_dict(value.get("retry")),
        )


def map_provider_failure(
    failure_kind: str,
    *,
    provider: str,
    cause: str | None = None,
    retry_after_seconds: int | None = None,
) -> CoreError:
    """Translate a Provider-neutral failure kind into the stable Core error set."""

    mapping = {
        "INVALID_URL": ErrorCode.URL_INVALID,
        "AUTH_REQUIRED": ErrorCode.AUTH_REQUIRED,
        "NETWORK": ErrorCode.NETWORK_PAUSED,
        "UNSUPPORTED": ErrorCode.PROVIDER_FAILED,
        "FAILED": ErrorCode.PROVIDER_FAILED,
        "PARTIAL": ErrorCode.PARTIAL_FAILURE,
    }
    try:
        code = mapping[failure_kind]
    except KeyError as error:
        raise ValueError(f"unknown Provider failure kind: {failure_kind}") from error
    retry = None
    if code in {ErrorCode.AUTH_REQUIRED, ErrorCode.NETWORK_PAUSED}:
        retry = RetryMetadata(retry_after_seconds=retry_after_seconds)
    return CoreError.for_code(code, provider=provider, cause=cause, retry=retry)
