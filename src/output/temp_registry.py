"""Explicit ownership registry and fail-closed deletion for a Job's temp tree."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


class TempRegistryError(ValueError):
    def __init__(self, message: str, *, failures: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.failures = failures or []


@dataclass(frozen=True)
class TempEntry:
    path: str
    kind: str
    size: int | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "kind": self.kind, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "TempEntry":
        return cls(
            path=str(value["path"]),
            kind=str(value["kind"]),
            size=int(value["size"]) if value.get("size") is not None else None,
            sha256=str(value["sha256"]) if value.get("sha256") is not None else None,
        )


def snapshot_temp(job_root: Path, temp_root: Path) -> list[TempEntry]:
    """List every owned path without following symlinks or guessing file roles."""
    root = job_root.resolve(strict=True)
    temp = temp_root
    if temp.is_symlink():
        raise TempRegistryError("Job temp root is a symlink")
    resolved_temp = temp.resolve(strict=True)
    if resolved_temp != root / "temp" or not resolved_temp.is_dir():
        raise TempRegistryError("Job temp root is not the canonical workspace/temp directory")

    entries: list[TempEntry] = []

    def visit(directory: Path) -> None:
        for child in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            mode = child.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                entries.append(TempEntry(relative, "symlink"))
            elif stat.S_ISREG(mode):
                entries.append(TempEntry(relative, "file", child.stat(follow_symlinks=False).st_size))
            elif stat.S_ISDIR(mode):
                entries.append(TempEntry(relative, "directory"))
                visit(path)
            else:
                entries.append(TempEntry(relative, "special"))

    visit(resolved_temp)
    return entries


def _validated_registered_path(job_root: Path, temp_root: Path, entry: TempEntry) -> Path:
    relative = Path(entry.path)
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "temp" or any(part in {".", ".."} for part in relative.parts):
        raise TempRegistryError("Temp registry contains an unsafe path", failures=[{"path": entry.path, "cause": "unsafe_registry_path"}])
    candidate = job_root.joinpath(*relative.parts)
    cursor = job_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TempRegistryError("Symlink found in registered temp path", failures=[{"path": entry.path, "cause": "symlink_not_allowed"}])
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(temp_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise TempRegistryError("Registered temp path escaped the Job temp boundary", failures=[{"path": entry.path, "cause": "path_escape"}]) from error
    return candidate


def cleanup_temp_registry(
    job_root: Path,
    temp_root: Path,
    entries: Iterable[TempEntry],
    *,
    remove_file: Callable[[Path], None],
) -> list[dict[str, str]]:
    """Delete only registered entries, deepest-first; never follow symlinks."""
    root = job_root.resolve(strict=True)
    temp = temp_root
    if temp.is_symlink():
        return [{"path": "temp", "cause": "symlink_not_allowed"}]
    if not temp.exists():
        return []
    try:
        if temp.resolve(strict=True) != root / "temp" or not temp.is_dir():
            return [{"path": "temp", "cause": "wrong_temp_root"}]
    except OSError:
        return [{"path": "temp", "cause": "missing_or_unavailable_temp_root"}]

    registered = list(entries)
    expected = {entry.path: entry for entry in registered}
    try:
        current = snapshot_temp(root, temp)
    except (TempRegistryError, OSError) as error:
        return error.failures if isinstance(error, TempRegistryError) and error.failures else [{"path": "temp", "cause": type(error).__name__}]
    current_by_path = {entry.path: entry for entry in current}

    symlink_entries = [entry for entry in current if entry.kind == "symlink"]
    special_entries = [entry for entry in current if entry.kind == "special"]
    if symlink_entries or special_entries:
        return [
            {"path": entry.path, "cause": "symlink_not_allowed" if entry.kind == "symlink" else "special_file_not_allowed"}
            for entry in (*symlink_entries, *special_entries)
        ]

    unknown = sorted(set(current_by_path) - set(expected))
    if unknown:
        return [{"path": path, "cause": "unregistered_temp_path"} for path in unknown]

    mismatches: list[dict[str, str]] = []
    for path, entry in current_by_path.items():
        old = expected[path]
        if old.kind != entry.kind or (old.kind == "file" and old.size != entry.size):
            mismatches.append({"path": path, "cause": "registry_identity_changed"})
    if mismatches:
        return mismatches

    failures: list[dict[str, str]] = []
    ordered = sorted(registered, key=lambda item: (len(Path(item.path).parts), item.path), reverse=True)
    for entry in ordered:
        try:
            path = _validated_registered_path(root, temp, entry)
        except TempRegistryError as error:
            failures.extend(error.failures or [{"path": entry.path, "cause": "registry_validation_failed"}])
            continue
        if not os.path.lexists(path):
            continue
        try:
            mode = path.lstat().st_mode
            if entry.kind == "file" and stat.S_ISREG(mode):
                remove_file(path)
            elif entry.kind == "directory" and stat.S_ISDIR(mode):
                path.rmdir()
            else:
                failures.append({"path": entry.path, "cause": "registered_type_changed"})
        except OSError as error:
            failures.append({"path": entry.path, "cause": type(error).__name__})

    if failures:
        return failures
    try:
        if temp.is_symlink() or temp.resolve(strict=True) != root / "temp":
            return [{"path": "temp", "cause": "temp_root_changed"}]
        temp.rmdir()
    except OSError as error:
        return [{"path": "temp", "cause": type(error).__name__}]
    return []


__all__ = ["TempEntry", "TempRegistryError", "cleanup_temp_registry", "snapshot_temp"]
