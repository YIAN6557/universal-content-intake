"""Canonical workspace and safe output-path contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Iterable


class OutputContractError(ValueError):
    pass


def _require_relative_path(value: str) -> PurePath:
    path = PurePath(value)
    if not value or path.is_absolute() or any(part in {"..", "."} for part in path.parts):
        raise OutputContractError(f"unsafe relative path: {value!r}")
    return path


def _contained(root: Path, candidate: Path) -> Path:
    root_resolved = root.expanduser().resolve()
    candidate_resolved = candidate.expanduser().resolve()
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as error:
        raise OutputContractError(f"path escapes contract boundary: {candidate}") from error
    return candidate_resolved


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    job_dir: Path
    temp_dir: Path
    output_dir: Path
    info_path: Path


@dataclass(frozen=True)
class ContentDeliverable:
    """One logical deliverable that may contain one file or many independent files."""

    relative_paths: tuple[str, ...]

    @classmethod
    def from_paths(cls, paths: Iterable[str]) -> "ContentDeliverable":
        normalized = tuple(str(_require_relative_path(path)) for path in paths)
        if not normalized:
            raise OutputContractError("a Content Deliverable must contain at least one file")
        return cls(normalized)


@dataclass(frozen=True)
class InfoRecord:
    """The fixed formal-record contract for `output/info.md`.

    Stage 1 models and renders the deterministic record text only. Stage 5 owns
    writing it after a real Provider has produced a validated deliverable.
    """

    content_type: str
    platform: str | None
    source_url: str
    original_title: str | None = None
    author: str | None = None
    author_url: str | None = None
    published_at: str | None = None
    main_content: str | None = None
    document_summary_zh: str | None = None
    extra_fields: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.content_type or not self.source_url:
            raise OutputContractError("info.md requires content_type and source_url")
        if self.document_summary_zh is not None and len(self.document_summary_zh) > 500:
            raise OutputContractError("document summary must be at most 500 Chinese characters")

    def to_markdown(self) -> str:
        fields: list[tuple[str, str | None]] = [
            ("Type", self.content_type),
            ("Platform", self.platform),
            ("Source URL", self.source_url),
            ("Original Title", self.original_title),
            ("Author", self.author),
            ("Author URL", self.author_url),
            ("Published At", self.published_at),
            ("Main Content", self.main_content),
        ]
        if self.document_summary_zh is not None:
            fields.append(("Document Summary zh-Hans", self.document_summary_zh))
        if self.extra_fields:
            fields.extend((str(key), str(value)) for key, value in sorted(self.extra_fields.items()))
        return "".join(f"- {key}: {value or ''}\n" for key, value in fields)


class OutputWorkspace:
    """Confinement and collision operations only, without content processing."""

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths

    @classmethod
    def for_job(cls, output_root: Path | str, job_id: str) -> "OutputWorkspace":
        if not job_id or any(part in job_id for part in ("/", "\\")):
            raise OutputContractError("job_id must be a non-empty path-safe identifier")
        root = Path(output_root).expanduser().resolve()
        job_dir = root / f"job-{job_id}"
        paths = WorkspacePaths(
            root=root,
            job_dir=job_dir,
            temp_dir=job_dir / "temp",
            output_dir=job_dir / "output",
            info_path=job_dir / "output" / "info.md",
        )
        _contained(paths.root, paths.job_dir)
        _contained(paths.job_dir, paths.temp_dir)
        _contained(paths.job_dir, paths.output_dir)
        return cls(paths)

    def prepare(self) -> WorkspacePaths:
        """Create only the formal directories; fail rather than reuse another Job."""

        if self.paths.job_dir.exists():
            raise OutputContractError(f"job workspace already exists: {self.paths.job_dir}")
        self.paths.temp_dir.mkdir(parents=True, exist_ok=False)
        self.paths.output_dir.mkdir(parents=True, exist_ok=False)
        return self.paths

    def temp_path(self, relative_path: str) -> Path:
        return _contained(self.paths.temp_dir, self.paths.temp_dir / _require_relative_path(relative_path))

    def output_path(self, relative_path: str) -> Path:
        return _contained(self.paths.output_dir, self.paths.output_dir / _require_relative_path(relative_path))

    def plan_content_deliverable(self, relative_paths: Iterable[str]) -> ContentDeliverable:
        deliverable = ContentDeliverable.from_paths(relative_paths)
        for relative_path in deliverable.relative_paths:
            self.output_path(relative_path)
        return deliverable

    def deterministic_name(self, desired_name: str, *, directory: Path | None = None) -> Path:
        """Return name, name-2, name-3... without overwriting an existing formal file."""

        candidate_directory = directory or self.paths.output_dir
        _contained(self.paths.output_dir, candidate_directory)
        pure_name = _require_relative_path(desired_name)
        if len(pure_name.parts) != 1:
            raise OutputContractError("collision naming accepts a file name, not a subpath")
        base = pure_name.stem
        suffix = pure_name.suffix
        candidate = candidate_directory / pure_name.name
        number = 2
        while candidate.exists():
            candidate = candidate_directory / f"{base}-{number}{suffix}"
            number += 1
        return candidate
