"""Move completed VIDEO deliverables out of the Job workspace into the delivery folder.

A COMPLETED VIDEO Job's ``output/final.mp4`` is moved (not copied) to
``output.delivery_root`` as ``<YYYY-MM-DD> <original title>.mp4``, with a Chinese
publish sheet (``.md``, see ``publish_sheet.py``) written beside it. The Job keeps
``info.md`` and its manifests; ``delivery.json`` in the Job directory records
where each committed artifact went and its Stage 5 SHA-256, so later integrity
checks treat a delivered artifact as handed off rather than missing. After the
hand-off the file belongs to the user: renaming or deleting it there never
fails the Job.

The move is crash-safe: the record is written as ``moving`` before each file is
hard-linked into place (no-overwrite) and its source unlinked; a rerun finishes
an interrupted move. Delivery holds the Job's Stage 5 lock (non-blocking) so it
never races ``finalize-job``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from src.core import compat
from src.output.publish_sheet import render_publish_sheet


DELIVERY_RECORD_NAME = "delivery.json"
DELIVERY_SCHEMA_VERSION = 1
DELIVERY_TIMEZONE = ZoneInfo("Asia/Shanghai")
STAGE5_MANIFEST_NAME = ".stage5-manifest.json"
STAGE5_LOCK_NAME = ".stage5.lock"
_COMPLETED_PHASES = frozenset({"cleanup_complete", "completed"})
_MAX_TITLE_CHARS = 80
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]+")


@dataclass(frozen=True)
class DeliveryOutcome:
    job_id: str
    status: str  # delivered | already_delivered | skipped | failed
    destination: Path | None = None
    reason: str | None = None


def load_delivery_record(job_dir: Path) -> dict[str, Any] | None:
    path = Path(job_dir) / DELIVERY_RECORD_NAME
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != DELIVERY_SCHEMA_VERSION:
        return None
    return value


def delivered_artifacts(job_dir: Path) -> dict[str, Mapping[str, Any]]:
    """Return ``{output_path: entry}`` for artifacts whose hand-off completed."""

    record = load_delivery_record(job_dir)
    if not record or record.get("status") != "delivered":
        return {}
    entries = record.get("artifacts")
    return {
        str(entry["output_path"]): entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("output_path") and entry.get("sha256")
        and _entry_state(record, entry) == "delivered"
    } if isinstance(entries, list) else {}


def deliver_completed_videos(
    workspace_root: Path | str,
    delivery_root: Path | str,
    *,
    exclude_job_ids: Iterable[str] = (),
) -> list[DeliveryOutcome]:
    """Deliver every COMPLETED VIDEO Job under ``workspace_root``; idempotent."""

    root = Path(workspace_root).expanduser()
    if not root.is_dir():
        return []
    excluded = {f"job-{job_id}" for job_id in exclude_job_ids if job_id}
    outcomes = []
    for job_dir in sorted(root.glob("job-*")):
        if job_dir.name in excluded or job_dir.is_symlink() or not job_dir.is_dir():
            continue
        outcomes.append(deliver_job(job_dir, delivery_root))
    return outcomes


def deliver_job(job_dir: Path | str, delivery_root: Path | str) -> DeliveryOutcome:
    job_dir = Path(job_dir)
    job_id = job_dir.name.removeprefix("job-")
    lock_path = job_dir / STAGE5_LOCK_NAME
    if lock_path.is_symlink() or not lock_path.is_file():
        return DeliveryOutcome(job_id, "skipped", reason="not_finalized")
    try:
        descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return DeliveryOutcome(job_id, "skipped", reason="lock_unavailable")
    try:
        try:
            compat.lock(descriptor, blocking=False)
        except BlockingIOError:
            return DeliveryOutcome(job_id, "skipped", reason="busy")
        try:
            return _deliver_locked(job_dir, job_id, Path(delivery_root).expanduser())
        except (OSError, ValueError, KeyError, TypeError) as error:
            return DeliveryOutcome(job_id, "failed", reason=f"{type(error).__name__}: {error}")
    finally:
        os.close(descriptor)


def _deliver_locked(job_dir: Path, job_id: str, delivery_root: Path) -> DeliveryOutcome:
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    if job.get("current_state") != "COMPLETED" or job.get("resolved_content_type") != "VIDEO":
        return DeliveryOutcome(job_id, "skipped", reason="not_completed_video")
    content_paths = (job.get("output_result") or {}).get("content_paths") or []
    if len(content_paths) != 1:
        return DeliveryOutcome(job_id, "skipped", reason="unexpected_output_result")
    manifest = json.loads((job_dir / STAGE5_MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("phase") not in _COMPLETED_PHASES:
        return DeliveryOutcome(job_id, "skipped", reason="stage5_incomplete")
    video = next((item for item in manifest.get("artifacts", [])
                  if isinstance(item, Mapping) and item.get("output_path") == content_paths[0]), None)
    if video is None or not str(video.get("output_path", "")).endswith(".mp4"):
        return DeliveryOutcome(job_id, "skipped", reason="no_video_artifact")
    planned = [(video, ".mp4")]
    for item, _suffix in planned:
        relative = str(item["output_path"])
        if "/" in relative or relative in {".", ".."}:
            raise ValueError("unexpected nested output path")

    record = load_delivery_record(job_dir)
    moved = False
    if record is None:
        for item, _suffix in planned:
            source = job_dir / "output" / str(item["output_path"])
            if source.is_symlink() or not source.is_file():
                return DeliveryOutcome(job_id, "skipped", reason=f"output_missing:{item['output_path']}")
            if source.stat().st_size != int(item["size"]) or _sha256(source) != str(item["sha256"]):
                return DeliveryOutcome(job_id, "failed", reason=f"output_does_not_match_manifest:{item['output_path']}")
        delivery_root.mkdir(parents=True, exist_ok=True)
        stem = _free_stem(delivery_root, _delivery_stem(job_dir, job), [".mp4", ".md"])
        record = {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "job_id": job_id,
            "status": "moving",
            "started_at": _utc_now(),
            "artifacts": [_entry(item, delivery_root / f"{stem}{suffix}") for item, suffix in planned],
        }
        _write_json_atomic(job_dir / DELIVERY_RECORD_NAME, record)
    for entry in record["artifacts"]:
        if _entry_state(record, entry) == "delivered":
            continue
        problem = _finish_entry(job_dir, entry)
        if problem:
            return DeliveryOutcome(job_id, "failed", _record_destination(record), problem)
        entry["state"] = "delivered"
        moved = True
        _write_json_atomic(job_dir / DELIVERY_RECORD_NAME, record)
    if record.get("status") != "delivered":
        record = {**record, "status": "delivered", "delivered_at": _utc_now()}
        _write_json_atomic(job_dir / DELIVERY_RECORD_NAME, record)
    # The Chinese publish sheet goes next to the video, while the video is still there.
    video_destination = _record_destination(record)
    if not record.get("publish_sheet") and video_destination and video_destination.is_file():
        sheet = video_destination.with_suffix(".md")
        info_path = job_dir / "output" / "info.md"
        info_text = info_path.read_text(encoding="utf-8") if info_path.is_file() else ""
        if _create_text_no_overwrite(sheet, render_publish_sheet(job, info_text)):
            record = {**record, "publish_sheet": str(sheet)}
            _write_json_atomic(job_dir / DELIVERY_RECORD_NAME, record)
            moved = True
    return DeliveryOutcome(job_id, "delivered" if moved else "already_delivered", video_destination)


def _create_text_no_overwrite(path: Path, text: str) -> bool:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(temporary)


def _entry(item: Mapping[str, Any], destination: Path) -> dict[str, Any]:
    return {
        "output_path": str(item["output_path"]),
        "sha256": str(item["sha256"]),
        "size": int(item["size"]),
        "destination": str(destination),
    }


def _entry_state(record: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    return str(entry.get("state") or ("delivered" if record.get("status") == "delivered" else "moving"))


def _free_stem(directory: Path, stem: str, suffixes: list[str]) -> str:
    for attempt in range(1, 1000):
        candidate = stem if attempt == 1 else f"{stem} ({attempt})"
        if not any(os.path.lexists(directory / f"{candidate}{suffix}") for suffix in suffixes):
            return candidate
    raise ValueError("could not find a free delivery file name")


def _finish_entry(job_dir: Path, entry: Mapping[str, Any]) -> str | None:
    """Move one recorded artifact; safe to repeat after an interruption."""

    source = job_dir / "output" / str(entry["output_path"])
    destination = Path(str(entry["destination"]))
    if not os.path.lexists(source):
        return None if destination.is_file() else f"source_and_destination_missing:{entry['output_path']}"
    if os.path.lexists(destination):
        if _same_file(source, destination):
            source.unlink()
            return None
        return f"destination_taken:{destination.name}"
    if source.is_symlink() or source.stat().st_size != int(entry["size"]) or _sha256(source) != str(entry["sha256"]):
        return f"output_does_not_match_manifest:{entry['output_path']}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _move_no_overwrite(source, destination, int(entry["size"]))
    return None


def _move_no_overwrite(source: Path, destination: Path, expected_size: int) -> None:
    try:
        os.link(source, destination)
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK}:
            raise
        partial = destination.with_name(f".{destination.name}.uci-partial")
        shutil.copy2(source, partial)
        if partial.stat().st_size != expected_size:
            partial.unlink()
            raise ValueError("copied delivery size mismatch") from None
        try:
            os.link(partial, destination)
        finally:
            partial.unlink()
    source.unlink()


def _delivery_stem(job_dir: Path, job: Mapping[str, Any]) -> str:
    timestamps = job.get("timestamps") or {}
    try:
        finished = datetime.fromisoformat(str(timestamps.get("updated_at")).replace("Z", "+00:00"))
        day = finished.astimezone(DELIVERY_TIMEZONE).strftime("%Y-%m-%d")
    except ValueError:
        day = datetime.now(DELIVERY_TIMEZONE).strftime("%Y-%m-%d")
    title = ""
    try:
        info = (job_dir / "output" / "info.md").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^- Original Title: (.*)$", info, re.MULTILINE)
        title = match.group(1) if match else ""
    except OSError:
        pass
    title = _safe_title(title) or str(job.get("job_id", job_dir.name))[:8]
    return f"{day} {title}"


def _safe_title(value: str) -> str:
    text = unicodedata.normalize("NFC", value)
    text = _UNSAFE_NAME_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().lstrip(".").strip()
    return text[:_MAX_TITLE_CHARS].rstrip()


def _record_destination(record: Mapping[str, Any]) -> Path | None:
    entries = record.get("artifacts")
    if isinstance(entries, list) and entries and isinstance(entries[0], Mapping) and entries[0].get("destination"):
        return Path(str(entries[0]["destination"]))
    return None


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        compat.make_private(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DELIVERY_RECORD_NAME",
    "DeliveryOutcome",
    "deliver_completed_videos",
    "deliver_job",
    "delivered_artifacts",
    "load_delivery_record",
]
