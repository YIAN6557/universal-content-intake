"""Provider result/error protocol shared by acquisition adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from src.core.job import ContentType, Job


class ProviderFailureKind(StrEnum):
    INVALID_URL = "INVALID_URL"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NETWORK = "NETWORK"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


def classify_http_access_denial(
    status: int,
    details: str = "",
    headers: Mapping[str, str] | None = None,
) -> ProviderFailureKind | None:
    """Classify access-denial responses from explicit, provider-neutral evidence.

    A bare 403 without explanatory evidence maps to Provider Failed because HTTP
    alone cannot establish whether it is an account permission wall or an
    automated-request refusal. Clear throttling, login/permission, and
    bot-challenge signals are normalized consistently.
    """
    if status in {401, 407}:
        return ProviderFailureKind.AUTH_REQUIRED
    if status != 403:
        return None

    header_text = " ".join(f"{key}: {value}" for key, value in (headers or {}).items())
    text = f"{details} {header_text}".lower()
    if any(term in text for term in (
        "rate limit", "rate limited", "too many requests", "throttl", "quota exceeded",
        "temporarily blocked", "retry-after",
    )):
        return ProviderFailureKind.NETWORK
    if any(term in text for term in (
        "automated request", "automated access", "anti-bot", "anti bot", "bot challenge",
        "bot detected", "captcha", "unusual traffic", "verify you are human",
        "blocked by security challenge", "blocked as automated",
    )):
        return ProviderFailureKind.FAILED
    if any(term in text for term in (
        "login", "log in", "sign in", "signin", "permission", "unauthorized",
        "authentication", "www-authenticate", "credential", "oauth", "requires account", "need access",
        "request access", "private document", "not authorized", "access denied",
    )):
        return ProviderFailureKind.AUTH_REQUIRED
    return ProviderFailureKind.FAILED


@dataclass(frozen=True)
class ProviderCapability:
    content_types: frozenset[ContentType]
    supports_probe: bool
    supports_fetch: bool
    supports_resume: bool


@dataclass(frozen=True)
class ProducedArtifact:
    relative_path: str
    role: str
    media_type: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    """Provider output only; Core later validates it and chooses the Job state."""

    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[ProducedArtifact, ...] = ()
    resume_token: str | None = None


@dataclass(frozen=True)
class ProviderFailure(Exception):
    kind: ProviderFailureKind
    provider: str
    message: str
    retry_after_seconds: int | None = None
    resume_token: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class ContentProvider(ABC):
    """Stable adapter seam. No method in this interface is allowed to mutate a Job."""

    name: str
    capability: ProviderCapability

    @abstractmethod
    def probe(self, job: Job) -> ProviderResult:
        """Return Provider metadata/artifacts or raise a structured ProviderFailure."""

    @abstractmethod
    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        """Return Provider metadata/artifacts or raise a structured ProviderFailure."""
