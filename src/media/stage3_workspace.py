"""Job-temp confinement and content-addressed Stage 3 resume manifests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from src.core.job import Job


class Stage3WorkspaceError(ValueError):
    """A Stage 3 path or cached artifact violates the Job temp contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def identity_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Stage3Workspace:
    def __init__(self, job: Job):
        self.job = job
        workspace_input = Path(job.workspace_path).expanduser()
        temp_input = Path(job.temp_path).expanduser()
        output_input = Path(job.output_path).expanduser()
        if any(path.is_symlink() for path in (workspace_input, temp_input, output_input)):
            raise Stage3WorkspaceError("Job workspace directories cannot be symlinks")
        try:
            workspace = workspace_input.resolve(strict=True)
            temp = temp_input.resolve(strict=True)
            output = output_input.resolve(strict=True)
        except OSError as error:
            raise Stage3WorkspaceError("Job workspace is unavailable") from error
        if (
            not workspace.is_dir()
            or not temp.is_dir()
            or not output.is_dir()
            or workspace.name != f"job-{job.job_id}"
            or temp != workspace / "temp"
            or output != workspace / "output"
            or workspace != workspace_input
            or temp != temp_input
            or output != output_input
        ):
            raise Stage3WorkspaceError("Job paths do not match the canonical job-<id>/temp and output boundaries")
        self.root = temp

    def path(self, relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if not relative_path or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise Stage3WorkspaceError("Stage 3 paths must be safe relative paths inside Job temp")
        candidate = self.root.joinpath(*relative.parts)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise Stage3WorkspaceError("Symlinks are not allowed in Stage 3 Job temp paths")
        try:
            candidate.resolve(strict=False).relative_to(self.root)
        except (OSError, ValueError) as error:
            raise Stage3WorkspaceError("Stage 3 path escapes Job temp") from error
        return candidate

    def mkdir(self, relative_path: str) -> Path:
        path = self.path(relative_path)
        path.mkdir(parents=True, exist_ok=True)
        return self.path(relative_path)

    def stage_dir(self, stage: str) -> Path:
        if not stage or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in stage):
            raise Stage3WorkspaceError("Invalid Stage 3 stage identifier")
        return self.mkdir(f"stage3/{stage}")

    def load_cache(self, stage: str, input_identity: Mapping[str, Any]) -> dict[str, Any] | None:
        directory = self.stage_dir(stage)
        manifest_path = self.path(f"stage3/{stage}/manifest.json")
        if not manifest_path.exists():
            return None
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise Stage3WorkspaceError("Stage 3 manifest is not a regular Job-temp file")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise Stage3WorkspaceError("Stage 3 manifest is unreadable or invalid") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("stage") != stage
            or manifest.get("input_identity_sha256") != identity_hash(input_identity)
        ):
            return None
        outputs = manifest.get("outputs")
        if not isinstance(outputs, list):
            return None
        for output in outputs:
            if not isinstance(output, dict):
                return None
            relative = output.get("path")
            if not isinstance(relative, str):
                return None
            artifact = self.path(relative)
            if (
                not artifact.is_file()
                or artifact.is_symlink()
                or artifact.stat().st_size != output.get("size_bytes")
                or sha256_file(artifact) != output.get("sha256")
            ):
                return None
        return manifest

    def write_cache(
        self,
        stage: str,
        input_identity: Mapping[str, Any],
        outputs: Sequence[str],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.stage_dir(stage)
        output_facts: list[dict[str, Any]] = []
        for relative in sorted(set(outputs)):
            artifact = self.path(relative)
            if artifact.is_symlink() or not artifact.is_file():
                raise Stage3WorkspaceError("Stage 3 output must be a regular file within Job temp")
            output_facts.append({
                "path": relative,
                "size_bytes": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            })
        manifest = {
            "schema_version": 1,
            "stage": stage,
            "input_identity_sha256": identity_hash(input_identity),
            "outputs": output_facts,
            "metadata": dict(metadata),
        }
        destination = self.path(f"stage3/{stage}/manifest.json")
        descriptor, pending_name = tempfile.mkstemp(prefix=".manifest.", suffix=".pending", dir=destination.parent)
        pending = Path(pending_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, destination)
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
        return manifest
