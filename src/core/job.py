"""Stable, JSON-safe Job model for Universal Content Intake V1."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping

from .errors import CoreError


JOB_SCHEMA_VERSION = 1


class ContentType(StrEnum):
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    IMAGE = "IMAGE"
    IMAGE_SET = "IMAGE_SET"
    ARTICLE = "ARTICLE"
    WEBPAGE = "WEBPAGE"
    DOCUMENT = "DOCUMENT"
    UNKNOWN = "UNKNOWN"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    PROBING = "PROBING"
    DOWNLOADING = "DOWNLOADING"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSLATING = "TRANSLATING"
    RENDERING = "RENDERING"
    DOCUMENTING = "DOCUMENTING"
    CLEANING = "CLEANING"
    COMPLETED = "COMPLETED"
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


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ensure_json_safe(value: Any, field_name: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be JSON-serializable") from error


@dataclass
class AttemptMetadata:
    count: int = 0
    resume_from_state: JobState | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("attempt count cannot be negative")
        _ensure_json_safe(self.checkpoint, "attempt checkpoint")

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "resume_from_state": self.resume_from_state.value if self.resume_from_state else None,
            "checkpoint": self.checkpoint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "AttemptMetadata":
        value = value or {}
        resume_state = value.get("resume_from_state")
        return cls(
            count=int(value.get("count", 0)),
            resume_from_state=JobState(resume_state) if resume_state else None,
            checkpoint=dict(value.get("checkpoint") or {}),
        )


@dataclass(frozen=True)
class OutputResult:
    """Logical content deliverable plus the fixed formal info record."""

    content_paths: tuple[str, ...] = ()
    info_path: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _ensure_json_safe(self.summary, "output summary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_paths": list(self.content_paths),
            "info_path": self.info_path,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "OutputResult | None":
        if value is None:
            return None
        return cls(
            content_paths=tuple(str(path) for path in value.get("content_paths", [])),
            info_path=value.get("info_path"),
            summary=dict(value.get("summary") or {}),
        )


@dataclass
class Job:
    """Provider-neutral job envelope with a stable schema-version boundary."""

    job_id: str
    source_url: str
    declared_content_type: ContentType | None
    resolved_content_type: ContentType = ContentType.UNKNOWN
    current_state: JobState = JobState.QUEUED
    attempts: AttemptMetadata = field(default_factory=AttemptMetadata)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    requested_options: dict[str, Any] = field(default_factory=dict)
    workspace_path: str = ""
    temp_path: str = ""
    output_path: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    error: CoreError | None = None
    output_result: OutputResult | None = None
    schema_version: int = JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != JOB_SCHEMA_VERSION:
            raise ValueError(f"unsupported Job schema version: {self.schema_version}")
        if not self.job_id or any(part in self.job_id for part in ("/", "\\")):
            raise ValueError("job_id must be a non-empty path-safe identifier")
        if not self.source_url.strip():
            raise ValueError("source_url cannot be empty")
        if self.declared_content_type is ContentType.UNKNOWN:
            raise ValueError("UNKNOWN cannot be a declared content type")
        if self.declared_content_type and self.resolved_content_type != self.declared_content_type:
            raise ValueError("a declared content type must remain the resolved content type")
        for name, value in {
            "workspace_path": self.workspace_path,
            "temp_path": self.temp_path,
            "output_path": self.output_path,
        }.items():
            if not value:
                raise ValueError(f"{name} cannot be empty")
        _ensure_json_safe(self.source_metadata, "source metadata")
        _ensure_json_safe(self.requested_options, "requested options")
        if self.error is not None and self.current_state.value != self.error.state:
            raise ValueError("current_state must match the structured error state")

    def provider_metadata(self, provider: str) -> dict[str, Any] | None:
        providers = self.source_metadata.get("providers", {})
        value = providers.get(provider) if isinstance(providers, dict) else None
        return dict(value) if isinstance(value, dict) else None

    def store_provider_metadata(self, provider: str, metadata: Mapping[str, Any]) -> None:
        """Core-owned persistence of a Provider result under its namespace."""

        if not provider.strip():
            raise ValueError("provider name cannot be empty")
        _ensure_json_safe(dict(metadata), "provider metadata")
        providers = self.source_metadata.setdefault("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("source_metadata.providers must be a mapping")
        providers[provider] = dict(metadata)
        self.touch()

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "source_url": self.source_url,
            "declared_content_type": self.declared_content_type.value if self.declared_content_type else None,
            "resolved_content_type": self.resolved_content_type.value,
            "current_state": self.current_state.value,
            "attempts": self.attempts.to_dict(),
            "source_metadata": self.source_metadata,
            "requested_options": self.requested_options,
            "workspace": {
                "path": self.workspace_path,
                "temp_path": self.temp_path,
                "output_path": self.output_path,
            },
            "timestamps": {"created_at": self.created_at, "updated_at": self.updated_at},
            "error": self.error.to_dict() if self.error else None,
            "output_result": self.output_result.to_dict() if self.output_result else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Job":
        if int(value.get("schema_version", -1)) != JOB_SCHEMA_VERSION:
            raise ValueError(f"unsupported Job schema version: {value.get('schema_version')}")
        workspace = value.get("workspace") or {}
        timestamps = value.get("timestamps") or {}
        declared = value.get("declared_content_type")
        return cls(
            schema_version=JOB_SCHEMA_VERSION,
            job_id=str(value["job_id"]),
            source_url=str(value["source_url"]),
            declared_content_type=ContentType(declared) if declared else None,
            resolved_content_type=ContentType(value.get("resolved_content_type", ContentType.UNKNOWN.value)),
            current_state=JobState(value.get("current_state", JobState.QUEUED.value)),
            attempts=AttemptMetadata.from_dict(value.get("attempts")),
            source_metadata=dict(value.get("source_metadata") or {}),
            requested_options=dict(value.get("requested_options") or {}),
            workspace_path=str(workspace["path"]),
            temp_path=str(workspace["temp_path"]),
            output_path=str(workspace["output_path"]),
            created_at=str(timestamps["created_at"]),
            updated_at=str(timestamps["updated_at"]),
            error=CoreError.from_dict(value.get("error")),
            output_result=OutputResult.from_dict(value.get("output_result")),
        )

    @classmethod
    def from_json(cls, payload: str) -> "Job":
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("Job JSON must contain an object")
        return cls.from_dict(decoded)
