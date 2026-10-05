"""Deterministic content-type resolution; Providers never re-route a locked Job."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import CoreError, ErrorCode
from .job import ContentType, JobState


class ResolutionOrigin(StrEnum):
    DECLARED = "DECLARED"
    PLATFORM = "PLATFORM"
    PROBE = "PROBE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ContentResolution:
    content_type: ContentType
    origin: ResolutionOrigin
    declared_locked: bool

    @property
    def state(self) -> JobState | None:
        return JobState.UNKNOWN_CONTENT if self.content_type is ContentType.UNKNOWN else None


def _normalize(value: ContentType | str | None, *, allow_unknown: bool) -> ContentType | None:
    if value is None:
        return None
    normalized = ContentType(value)
    if normalized is ContentType.UNKNOWN and not allow_unknown:
        return None
    return normalized


def resolve_content_type(
    *,
    declared: ContentType | str | None = None,
    platform: ContentType | str | None = None,
    probe: ContentType | str | None = None,
) -> ContentResolution:
    """Apply the Canonical priority: declared > platform > probe > UNKNOWN."""

    declared_type = _normalize(declared, allow_unknown=False)
    if declared_type:
        return ContentResolution(declared_type, ResolutionOrigin.DECLARED, True)
    platform_type = _normalize(platform, allow_unknown=False)
    if platform_type:
        return ContentResolution(platform_type, ResolutionOrigin.PLATFORM, False)
    probe_type = _normalize(probe, allow_unknown=False)
    if probe_type:
        return ContentResolution(probe_type, ResolutionOrigin.PROBE, False)
    return ContentResolution(ContentType.UNKNOWN, ResolutionOrigin.UNKNOWN, False)


def unknown_content_error() -> CoreError:
    return CoreError.for_code(ErrorCode.UNKNOWN_CONTENT)


def provider_failure_for_locked_type(provider: str, cause: str | None = None) -> CoreError:
    """A locked declared type fails as that type; it never triggers re-resolution."""

    return CoreError.for_code(ErrorCode.PROVIDER_FAILED, provider=provider, cause=cause)
