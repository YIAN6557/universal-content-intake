"""Local Queue Worker lifecycle, durable state, and Core subprocess boundary."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.core import compat
from src.core.errors import CoreError, ErrorCode
from src.core.job import ContentType, Job, JobState
from src.core.policies import load_config
from src.output.delivery import deliver_completed_videos, delivered_artifacts
from src.output.paths import OutputWorkspace
from .client import ClaimedTask, QueueApiError, QueueClient, QueueOperationResult


WORKER_STATE_SCHEMA_VERSION = 1
WORKER_INTERNAL_STATES = frozenset({"POLLING", "CLAIMED", "PROCESSING", "COMPLETING", "FAILING"})
OWNERSHIP_LOST_CODES = frozenset({
    "CLAIM_TOKEN_INVALID",
    "CLAIM_REQUEST_EXPIRED",
    "LEASE_EXPIRED",
    "QUEUE_NOT_FOUND",
})
Notifier = Callable[[str, str], None]
RETRY_STREAK_NOTIFY_AFTER = 3


def macos_notifier(title: str, message: str) -> None:
    """Post a desktop notification (Notification Center on macOS, a toast on Windows); never raises into the Worker."""

    compat.notify(title, message)


def _error_detail(error: QueueApiError) -> str | None:
    parts = []
    if error.http_status is not None:
        parts.append(f"http={error.http_status}")
    if getattr(error, "detail", None):
        parts.append(str(error.detail))
    return " ".join(parts) or None


class WorkerConfigError(ValueError):
    pass


class WorkerStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    queue_poll_seconds: float
    queue_heartbeat_seconds: float
    queue_lease_seconds: int
    state_dir: Path
    workspace_root: Path
    production_timezone: str = "Asia/Shanghai"
    rank2_start_cutoff: str = "08:00"
    processing_concurrency: int = 1
    delivery_root: Path | None = None

    def __post_init__(self) -> None:
        poll = _positive_number(self.queue_poll_seconds, "queue_poll_seconds")
        heartbeat = _positive_number(self.queue_heartbeat_seconds, "queue_heartbeat_seconds")
        lease = _positive_integer(self.queue_lease_seconds, "queue_lease_seconds")
        if heartbeat >= lease:
            raise WorkerConfigError("queue_heartbeat_seconds must be shorter than queue_lease_seconds")
        try:
            ZoneInfo(str(self.production_timezone))
        except (ZoneInfoNotFoundError, ValueError):
            raise WorkerConfigError("production_timezone must be a valid IANA timezone") from None
        if not isinstance(self.rank2_start_cutoff, str) or not re.fullmatch(r"\d{2}:\d{2}", self.rank2_start_cutoff):
            raise WorkerConfigError("rank2_start_cutoff must be HH:mm")
        cutoff_hour, cutoff_minute = (int(part) for part in self.rank2_start_cutoff.split(":"))
        if cutoff_hour > 23 or cutoff_minute > 59:
            raise WorkerConfigError("rank2_start_cutoff must be a valid time")
        if self.processing_concurrency != 1:
            raise WorkerConfigError("processing_concurrency is fixed at 1 for V1")
        state_dir = Path(self.state_dir).expanduser().resolve()
        workspace_root = Path(self.workspace_root).expanduser().resolve()
        object.__setattr__(self, "queue_poll_seconds", poll)
        object.__setattr__(self, "queue_heartbeat_seconds", heartbeat)
        object.__setattr__(self, "queue_lease_seconds", lease)
        object.__setattr__(self, "state_dir", state_dir)
        object.__setattr__(self, "workspace_root", workspace_root)
        if self.delivery_root is not None:
            object.__setattr__(self, "delivery_root", Path(self.delivery_root).expanduser().resolve())

    @classmethod
    def from_defaults(cls, path: Path | str) -> "WorkerConfig":
        try:
            values = load_config(Path(path).expanduser())
            output = values["output"]
            return cls(
                queue_poll_seconds=values["queue_poll_seconds"],
                queue_heartbeat_seconds=values["queue_heartbeat_seconds"],
                queue_lease_seconds=values["queue_lease_seconds"],
                state_dir=Path(str(values["worker_state_dir"])),
                workspace_root=Path(str(output["root"])),
                production_timezone=str(values["production_timezone"]),
                rank2_start_cutoff=str(values["rank2_start_cutoff"]),
                processing_concurrency=values["processing_concurrency"],
                delivery_root=Path(str(output["delivery_root"])) if output.get("delivery_root") else None,
            )
        except (OSError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, WorkerConfigError):
                raise
            raise WorkerConfigError("Worker defaults are missing or contain invalid lifecycle configuration") from None


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise WorkerConfigError(f"{name} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise WorkerConfigError(f"{name} must be a positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise WorkerConfigError(f"{name} must be a positive number")
    return number


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise WorkerConfigError(f"{name} must be a positive integer")
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError):
        raise WorkerConfigError(f"{name} must be a positive integer") from None
    if integer <= 0 or str(integer) != str(value).strip():
        raise WorkerConfigError(f"{name} must be a positive integer")
    return integer


@dataclass(frozen=True)
class WorkerState:
    worker_state: str
    claim_request_id: str
    queue_id: str | None = None
    claim_token: str | None = None
    lease_until: str | None = None
    local_job_id: str | None = None
    url: str | None = None
    attempts: int | None = None
    core_pid: int | None = None
    core_started_at: str | None = None
    result_path: str | None = None
    failure: Mapping[str, Any] | None = None
    selection_day: str | None = None
    selection_rank: int | None = None
    rank2_start_cutoff_at: str | None = None
    monitor: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.worker_state not in WORKER_INTERNAL_STATES:
            raise WorkerStateError("unknown local Worker state")
        if not isinstance(self.claim_request_id, str) or not self.claim_request_id.strip():
            raise WorkerStateError("Worker state requires claim_request_id")
        if self.worker_state != "POLLING":
            required = (self.queue_id, self.claim_token, self.lease_until, self.local_job_id, self.url)
            if any(not isinstance(value, str) or not value.strip() for value in required):
                raise WorkerStateError("active Worker task is missing claim identity fields")
        if self.attempts is not None and (isinstance(self.attempts, bool) or self.attempts < 1):
            raise WorkerStateError("Worker attempts must be a positive integer")
        if self.selection_rank is not None and self.selection_rank not in (1, 2):
            raise WorkerStateError("Worker Selection rank must be 1 or 2")
        if self.selection_rank == 2 and not self.rank2_start_cutoff_at:
            raise WorkerStateError("Rank 2 Worker state requires a start cutoff")
        if self.core_pid is not None and (isinstance(self.core_pid, bool) or self.core_pid <= 0):
            raise WorkerStateError("Core pid must be positive")
        for name, value in (("failure", self.failure), ("monitor", self.monitor)):
            if value is None:
                continue
            try:
                json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                raise WorkerStateError(f"Worker {name} must be JSON-safe") from None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": WORKER_STATE_SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerState":
        if int(value.get("schema_version", -1)) != WORKER_STATE_SCHEMA_VERSION:
            raise WorkerStateError("unsupported Worker state schema version")
        return cls(
            worker_state=str(value["worker_state"]),
            claim_request_id=str(value["claim_request_id"]),
            queue_id=_optional_string(value.get("queue_id")),
            claim_token=_optional_string(value.get("claim_token")),
            lease_until=_optional_string(value.get("lease_until")),
            local_job_id=_optional_string(value.get("local_job_id")),
            url=_optional_string(value.get("url")),
            attempts=int(value["attempts"]) if value.get("attempts") is not None else None,
            core_pid=int(value["core_pid"]) if value.get("core_pid") is not None else None,
            core_started_at=_optional_string(value.get("core_started_at")),
            result_path=_optional_string(value.get("result_path")),
            failure=dict(value["failure"]) if isinstance(value.get("failure"), Mapping) else None,
            selection_day=_optional_string(value.get("selection_day")),
            selection_rank=int(value["selection_rank"]) if value.get("selection_rank") is not None else None,
            rank2_start_cutoff_at=_optional_string(value.get("rank2_start_cutoff_at")),
            monitor=dict(value["monitor"]) if isinstance(value.get("monitor"), Mapping) else None,
        )


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


class WorkerStateStore:
    """One atomically replaced, owner-only active-task record; no database."""

    def __init__(self, state_dir: Path | str) -> None:
        self.directory = Path(state_dir).expanduser().resolve()
        self.path = self.directory / "active.json"
        self._lock = threading.RLock()

    def load(self) -> WorkerState | None:
        with self._lock:
            if self.path.is_symlink():
                raise WorkerStateError("Worker state file cannot be a symlink")
            if not self.path.exists():
                return None
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("state root must be an object")
                os.chmod(self.path, 0o600)
                return WorkerState.from_dict(payload)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                raise WorkerStateError("Worker state file is unreadable or invalid") from None

    def save(self, state: WorkerState) -> None:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.directory.is_symlink() or self.path.is_symlink():
                raise WorkerStateError("Worker state path cannot traverse a symlink")
            os.chmod(self.directory, 0o700)
            payload = (json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            fd, temporary_name = tempfile.mkstemp(prefix=".active.", suffix=".tmp", dir=self.directory)
            temporary = Path(temporary_name)
            try:
                compat.make_private(fd)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
                compat.fsync_dir(self.directory)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def update_lease(self, queue_id: str, claim_token: str, lease_until: str) -> WorkerState:
        with self._lock:
            state = self.load()
            if state is None or state.queue_id != queue_id or state.claim_token != claim_token:
                raise WorkerStateError("heartbeat no longer owns the persisted local task")
            next_state = replace(state, lease_until=lease_until)
            self.save(next_state)
            return next_state

    def clear(self) -> None:
        with self._lock:
            if self.path.is_symlink():
                raise WorkerStateError("Worker state file cannot be a symlink")
            try:
                self.path.unlink()
            except FileNotFoundError:
                return


class WorkerFileLock:
    """Nonblocking per-machine singleton lock, independent from Cloud claims."""

    def __init__(self, state_dir: Path | str) -> None:
        self.directory = Path(state_dir).expanduser().resolve()
        self.path = self.directory / "worker.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or self.path.is_symlink():
            raise WorkerStateError("Worker lock path cannot traverse a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        compat.make_private(fd)
        try:
            compat.lock(fd, blocking=False)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            compat.unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> "WorkerFileLock":
        if not self.acquire():
            raise WorkerStateError("another local Worker is already active")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


@dataclass(frozen=True)
class CoreRunResult:
    success: bool
    result_path: str | None = None
    error: CoreError | None = None
    cancelled: bool = False
    local_job_id: str | None = None


class CoreRunner(Protocol):
    def run(
        self,
        state: WorkerState,
        *,
        cancel_event: threading.Event,
        on_started: Callable[[int], None],
    ) -> CoreRunResult: ...

    def stop(self, state: WorkerState) -> None: ...


STAGE3_DONE_STATUSES = frozenset({"validated", "validated_no_speech_no_subtitles"})
# Stage 3 pauses without a CoreError when it needs an attended action. The
# Queue still needs a recoverable CoreError so the task becomes PAUSED.
STAGE3_PAUSE_ERRORS = {
    "paused_asr_language_undetermined": (ErrorCode.ASR_FAILED, "STAGE3_ASR_LANGUAGE_UNDETERMINED"),
    "paused_translation_resource_unavailable": (ErrorCode.API_FAILED, "APPLE_TRANSLATION_RESOURCE_UNAVAILABLE"),
}
DEFAULT_NETWORK_RETRY_DELAYS = (60.0, 180.0, 600.0)
MAX_CORE_STEPS = 12


def next_core_step(job: Job) -> str | None:
    """Pick the next Core command from persisted Job progress, not from habit.

    A resumed Worker (after a crash, restart, or a retried step) must continue
    where the Job stopped instead of always restarting at acquisition.
    """

    if job.output_result is not None and job.current_state is JobState.COMPLETED:
        return None
    stage3 = job.source_metadata.get("stage3")
    if job.output_result is not None or (isinstance(stage3, Mapping) and stage3.get("status") in STAGE3_DONE_STATUSES):
        return "finalize-job"
    provider = job.provider_metadata("yt-dlp") or {}
    if isinstance(provider.get("validated_artifact"), Mapping):
        return "stage3-run"
    return "video-run"


class SubprocessCoreRunner:
    """Drive one Queue VIDEO Job through acquisition, Stage 3, and Stage 5 output."""

    def __init__(
        self,
        *,
        project_root: Path | str,
        workspace_root: Path | str,
        python_executable: Path | str,
        caffeinate_executable: Path | str | None = "caffeinate",
        process_poll_seconds: float = 0.2,
        network_retry_delays: tuple[float, ...] = DEFAULT_NETWORK_RETRY_DELAYS,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.python_executable = str(Path(python_executable).expanduser().resolve())
        self.caffeinate_executable = shutil.which(str(caffeinate_executable)) if caffeinate_executable else None
        self.process_poll_seconds = _positive_number(process_poll_seconds, "process_poll_seconds")
        self.network_retry_delays = tuple(float(delay) for delay in network_retry_delays)

    def job_file_for(self, local_job_id: str) -> Path:
        return OutputWorkspace.for_job(self.workspace_root, local_job_id).paths.job_dir / "job.json"

    def run(self, state: WorkerState, *, cancel_event: threading.Event, on_started: Callable[[int], None]) -> CoreRunResult:
        job_file = self._ensure_job_file(state)
        existing = self._read_job(job_file, state)
        if existing.error is not None and not existing.error.recoverable:
            return CoreRunResult(success=False, error=existing.error, local_job_id=state.local_job_id)
        if existing.output_result is not None:
            return self._formal_result(existing, state)

        # Next to job.json, not in temp/: finalize-job empties temp/ while this lock is held, and Windows cannot
        # delete a file that is open.
        lock_path = job_file.parent / ".core-process.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        compat.make_private(fd)
        locked = False
        try:
            while not locked:
                if cancel_event.is_set():
                    self._terminate_existing_core(state, job_file)
                    return CoreRunResult(success=False, cancelled=True, local_job_id=state.local_job_id)
                try:
                    compat.lock(fd, blocking=False)
                    locked = True
                except BlockingIOError:
                    time.sleep(self.process_poll_seconds)

            if compat.WINDOWS and state.core_pid is not None:
                # Windows cannot hand the lock to the child, so a Core left running by a Worker that died
                # does not hold it; stop that Core before starting another one for the same Job.
                self._terminate_existing_core(state, job_file)
            job = existing
            network_retries = 0
            previous: tuple[str, str] | None = None
            for _ in range(MAX_CORE_STEPS):
                step = next_core_step(job)
                if step is None:
                    return self._formal_result(job, state)
                progress = (step, job.current_state.value)
                if progress == previous and job.error is None:
                    # The last command exited cleanly without advancing the Job.
                    return self._core_exit_failure(0, state)
                previous = progress
                command = [self.python_executable, "-m", "src.cli", step, "--resume-job", str(job_file)]
                return_code, cancelled = self._run_child(command, fd, state, cancel_event, on_started)
                if cancelled:
                    return CoreRunResult(success=False, cancelled=True, local_job_id=state.local_job_id)
                job = self._read_job(job_file, state)
                if job.error is not None:
                    # A transient network failure (e.g. HTTP 429) is retried
                    # inside this claim before the Queue task is PAUSED; a
                    # PAUSED Rank 1 would otherwise block that day's Rank 2.
                    if (
                        job.error.code is ErrorCode.NETWORK_PAUSED
                        and network_retries < len(self.network_retry_delays)
                    ):
                        delay = self.network_retry_delays[network_retries]
                        network_retries += 1
                        if cancel_event.wait(delay):
                            return CoreRunResult(success=False, cancelled=True, local_job_id=state.local_job_id)
                        continue
                    return CoreRunResult(success=False, error=job.error, local_job_id=state.local_job_id)
                if step == "stage3-run" and return_code == 3:
                    return self._stage3_paused(job, state)
                if return_code != 0:
                    return self._core_exit_failure(return_code, state)
            return CoreRunResult(
                success=False,
                local_job_id=state.local_job_id,
                error=CoreError.for_code(
                    ErrorCode.API_FAILED,
                    message="VIDEO Core did not converge to a formal output.",
                    cause="CORE_STEP_LIMIT",
                ),
            )
        except (OSError, ValueError, WorkerStateError) as error:
            return CoreRunResult(
                success=False,
                result_path=str(job_file.parent),
                error=CoreError.for_code(ErrorCode.API_FAILED, message="Core VIDEO job could not be resumed.", cause=type(error).__name__),
                local_job_id=state.local_job_id,
            )
        finally:
            if locked:
                compat.unlock(fd)
            os.close(fd)

    def _run_child(
        self,
        command: list[str],
        lock_fd: int,
        state: WorkerState,
        cancel_event: threading.Event,
        on_started: Callable[[int], None],
    ) -> tuple[int | None, bool]:
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            # The child keeps the Job lock if the Worker dies (POSIX only; Windows locks belong to one process,
            # so a restarted Worker there stops the recorded Core first, see run()).
            **({} if compat.WINDOWS else {"pass_fds": (lock_fd,)}),
            **compat.new_process_group(),
        )
        try:
            on_started(int(process.pid))
        except Exception:
            self._terminate_process(process)
            raise
        power_process = self._start_caffeinate(int(process.pid))
        compat.keep_awake(True)
        cancelled = False
        return_code: int | None = None
        try:
            while True:
                if cancel_event.is_set():
                    cancelled = True
                    self._terminate_process(process)
                    break
                return_code = process.poll()
                if return_code is not None:
                    break
                time.sleep(self.process_poll_seconds)
        finally:
            compat.keep_awake(False)
            self._stop_caffeinate(power_process)
        return return_code, cancelled

    @staticmethod
    def _stage3_paused(job: Job, state: WorkerState) -> CoreRunResult:
        stage3 = job.source_metadata.get("stage3")
        status = str(stage3.get("status")) if isinstance(stage3, Mapping) else ""
        code, cause = STAGE3_PAUSE_ERRORS.get(status, (ErrorCode.API_FAILED, "STAGE3_PAUSED"))
        return CoreRunResult(
            success=False,
            local_job_id=state.local_job_id,
            error=CoreError.for_code(code, message="Stage 3 paused and needs an attended action.", cause=cause),
        )

    @staticmethod
    def _core_exit_failure(return_code: int | None, state: WorkerState) -> CoreRunResult:
        return CoreRunResult(
            success=False,
            local_job_id=state.local_job_id,
            error=CoreError.for_code(
                ErrorCode.API_FAILED,
                message="Core VIDEO command exited without a persisted CoreError.",
                cause=f"CORE_EXIT_{return_code}",
            ),
        )

    @staticmethod
    def _formal_result(job: Job, state: WorkerState) -> CoreRunResult:
        if job.current_state is not JobState.COMPLETED or job.output_result is None:
            return CoreRunResult(
                success=False,
                local_job_id=state.local_job_id,
                error=CoreError.for_code(
                    ErrorCode.PROVIDER_FAILED,
                    message="VIDEO Core did not produce a completed formal output result.",
                    cause="FORMAL_OUTPUT_MISSING",
                ),
            )
        outputs = job.output_result.content_paths
        info_path = job.output_result.info_path
        if len(outputs) != 1 or not info_path:
            return CoreRunResult(
                success=False,
                local_job_id=state.local_job_id,
                error=CoreError.for_code(
                    ErrorCode.PROVIDER_FAILED,
                    message="VIDEO Core output result does not match the formal output contract.",
                    cause="FORMAL_OUTPUT_INVALID",
                ),
            )
        output_root = Path(job.output_path).resolve()
        content_path = Path(outputs[0])
        metadata_path = Path(info_path)
        if not content_path.is_absolute():
            content_path = output_root / content_path
        if not metadata_path.is_absolute():
            metadata_path = output_root / metadata_path
        handed_off = delivered_artifacts(output_root.parent).get(str(outputs[0]))
        if handed_off and not os.path.lexists(content_path):
            # Already moved to the delivery folder by an earlier run.
            return CoreRunResult(success=True, result_path=str(handed_off.get("destination") or content_path),
                                 local_job_id=state.local_job_id)
        try:
            resolved_content = content_path.resolve(strict=True)
            resolved_metadata = metadata_path.resolve(strict=True)
            resolved_content.relative_to(output_root)
            resolved_metadata.relative_to(output_root)
            if content_path.is_symlink() or metadata_path.is_symlink():
                raise ValueError("formal output cannot be a symlink")
            if not resolved_content.is_file() or resolved_content.stat().st_size <= 0:
                raise ValueError("formal content output is empty")
            if not resolved_metadata.is_file() or resolved_metadata.stat().st_size <= 0:
                raise ValueError("formal info.md is empty")
        except (OSError, ValueError):
            return CoreRunResult(
                success=False,
                local_job_id=state.local_job_id,
                error=CoreError.for_code(
                    ErrorCode.PROVIDER_FAILED,
                    message="VIDEO Core output paths are missing, empty, or outside the formal output directory.",
                    cause="FORMAL_OUTPUT_PATH_INVALID",
                ),
            )
        return CoreRunResult(success=True, result_path=str(resolved_content), local_job_id=state.local_job_id)

    def stop(self, state: WorkerState) -> None:
        """Stop a previously spawned Core process after Cloud ownership is lost."""

        if not state.local_job_id:
            return
        job_file = self.job_file_for(state.local_job_id)
        if job_file.exists() and not job_file.is_symlink():
            self._terminate_existing_core(state, job_file)

    def _ensure_job_file(self, state: WorkerState) -> Path:
        if not state.local_job_id or not state.url:
            raise WorkerStateError("active task has no local job identity")
        workspace = OutputWorkspace.for_job(self.workspace_root, state.local_job_id)
        paths = workspace.paths
        if paths.job_dir.is_symlink():
            raise WorkerStateError("Core Job workspace cannot be a symlink")
        if paths.job_dir.exists() and not paths.job_dir.is_dir():
            raise WorkerStateError("Core Job workspace is not a directory")
        job_file = paths.job_dir / "job.json"
        if job_file.is_symlink():
            raise WorkerStateError("Core Job contract cannot be a symlink")
        if job_file.exists():
            self._read_job(job_file, state)
            return job_file

        paths.job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        known = {"temp", "output"}
        if any(child.name not in known for child in paths.job_dir.iterdir()):
            raise WorkerStateError("incomplete Core Job workspace contains unexpected files")
        for directory in (paths.temp_dir, paths.output_dir):
            if directory.is_symlink():
                raise WorkerStateError("Core Job subdirectories cannot be symlinks")
            directory.mkdir(exist_ok=True, mode=0o700)
        if any(paths.temp_dir.iterdir()) or any(paths.output_dir.iterdir()):
            raise WorkerStateError("Core Job without a contract contains existing artifacts")
        job = Job(
            job_id=state.local_job_id,
            source_url=state.url,
            declared_content_type=ContentType.VIDEO,
            resolved_content_type=ContentType.VIDEO,
            workspace_path=str(paths.job_dir),
            temp_path=str(paths.temp_dir),
            output_path=str(paths.output_dir),
            source_metadata={
                "resolution_origin": "DECLARED",
                "declared_type_locked": True,
                # Queue identity and Cloud HOT evidence for info.md (plan 12.3).
                "monitor": {
                    **dict(state.monitor or {}),
                    "queue_id": state.queue_id,
                    **({"selection_day": state.selection_day} if state.selection_day else {}),
                    **({"selection_rank": state.selection_rank} if state.selection_rank else {}),
                },
            },
        )
        self._atomic_create_job(job_file, job.to_json())
        self._read_job(job_file, state)
        return job_file

    @staticmethod
    def _atomic_create_job(job_file: Path, content: str) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".job.", suffix=".tmp", dir=job_file.parent)
        temporary = Path(temporary_name)
        try:
            compat.make_private(fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, job_file)
            except FileExistsError:
                return
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_job(job_file: Path, state: WorkerState) -> Job:
        try:
            job = Job.from_json(job_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            raise WorkerStateError("Core Job contract is unreadable or invalid") from None
        if job.job_id != state.local_job_id or job.source_url != state.url:
            raise WorkerStateError("Core Job identity does not match the Queue task")
        if job.declared_content_type not in {None, ContentType.VIDEO} or job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.VIDEO}:
            raise WorkerStateError("Core Job is not a VIDEO Job")
        return job

    def _start_caffeinate(self, pid: int) -> subprocess.Popen[bytes] | None:
        if not self.caffeinate_executable or compat.WINDOWS:
            return None
        try:
            return subprocess.Popen(
                [self.caffeinate_executable, "-i", "-w", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            return None

    @staticmethod
    def _stop_caffeinate(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        try:
            compat.stop_process_group(process)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _terminate_existing_core(state: WorkerState, job_file: Path) -> None:
        needle = str(job_file)
        if state.core_pid is not None:
            candidates = [state.core_pid]
        else:
            candidates = [pid for pid, command in compat.command_lines() if needle in command and "src.cli" in command]
        for pid in candidates:
            # Only a Core started for this very Job: a recorded pid may since belong to another program.
            if not any(needle in command and "src.cli" in command for _, command in compat.command_lines(pid)):
                continue
            try:
                compat.stop_pid_group(pid)
            except (OSError, ValueError, ProcessLookupError):
                continue


@dataclass(frozen=True)
class WorkerResult:
    status: str
    queue_id: str | None = None
    local_job_id: str | None = None
    attempt: int | None = None
    error_code: str | None = None


class _HeartbeatLoop:
    def __init__(self, client: QueueClient, store: WorkerStateStore, state: WorkerState, interval: float) -> None:
        self.client = client
        self.store = store
        self.state = state
        self.interval = interval
        self.cancel_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uci-queue-heartbeat", daemon=True)
        self.error: QueueApiError | None = None

    @property
    def ownership_lost(self) -> bool:
        return self.error is not None and self.error.code in OWNERSHIP_LOST_CODES

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                result = self.client.heartbeat(self.state.queue_id or "", self.state.claim_token or "")
                if result.status != "PROCESSING" or not result.lease_until:
                    raise QueueApiError("API_RESPONSE_INVALID", "Heartbeat did not return an active lease.")
                _parse_utc(result.lease_until)
                self.store.update_lease(self.state.queue_id or "", self.state.claim_token or "", result.lease_until)
            except QueueApiError as error:
                self.error = error
                self.cancel_event.set()
                return
            except Exception:
                self.error = QueueApiError("TRANSPORT_FAILED", "Worker heartbeat could not be persisted.", retryable=True)
                self.cancel_event.set()
                return


class QueueWorker:
    def __init__(
        self,
        *,
        client: QueueClient,
        core_runner: CoreRunner,
        config: WorkerConfig,
        state_store: WorkerStateStore | None = None,
        request_id_factory: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.client = client
        self.core_runner = core_runner
        self.config = config
        self.state_store = state_store or WorkerStateStore(config.state_dir)
        self.request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))
        self.logger = logger or logging.getLogger("uci.queue.worker")
        self.notifier = notifier
        self._retry_streak = 0
        self._retry_streak_notified = False
        self._delivery_failures: dict[str, str | None] = {}

    def poll_once(self) -> WorkerResult:
        state = self.state_store.load()
        if state is None or state.worker_state == "POLLING":
            return self._claim_or_empty(state)
        if state.worker_state in {"COMPLETING", "FAILING"}:
            return self._finalize(state)
        return self._run_active(state)

    def run_forever(self, stop_event: threading.Event) -> WorkerResult:
        lock = WorkerFileLock(self.config.state_dir)
        if not lock.acquire():
            self.logger.info("queue_worker operation=lock status=already_running")
            return WorkerResult("ALREADY_RUNNING")
        try:
            while not stop_event.is_set():
                result = self.poll_once()
                self._observe(result)
                if result.status in {"EMPTY", "COMPLETED"}:
                    self.deliver_videos()
                if result.status in {"EMPTY", "RETRY"}:
                    stop_event.wait(self.config.queue_poll_seconds)
            return WorkerResult("STOPPED")
        finally:
            lock.release()

    def deliver_videos(self) -> None:
        """Move finished videos to the delivery folder while no Queue task is active."""

        if self.config.delivery_root is None:
            return
        state = self.state_store.load()
        if state is not None and state.worker_state != "POLLING":
            return
        try:
            outcomes = deliver_completed_videos(self.config.workspace_root, self.config.delivery_root)
        except Exception as error:
            self.logger.warning("queue_worker operation=deliver state=failed error_code=%s", type(error).__name__)
            return
        for outcome in outcomes:
            if outcome.status == "delivered":
                self.logger.info("queue_id=- local_job_id=%s operation=deliver state=DELIVERED destination=%s",
                                 outcome.job_id, outcome.destination)
                self._notify("UCI：视频已保存", f"{outcome.destination.name if outcome.destination else outcome.job_id}")
            elif outcome.status == "failed" and self._delivery_failures.get(outcome.job_id) != outcome.reason:
                self._delivery_failures[outcome.job_id] = outcome.reason
                self.logger.warning("queue_id=- local_job_id=%s operation=deliver state=failed detail=%s",
                                    outcome.job_id, outcome.reason)

    def _notify(self, title: str, message: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier(title, message)
        except Exception:
            self.logger.warning("queue_worker operation=notify state=failed")

    def _observe(self, result: WorkerResult) -> None:
        """Surface outcomes that otherwise only exist in the log or the Sheet."""

        short_id = (result.queue_id or "-")[:8]
        if result.status == "RETRY":
            self._retry_streak += 1
            if self._retry_streak >= RETRY_STREAK_NOTIFY_AFTER and not self._retry_streak_notified:
                self._retry_streak_notified = True
                self._notify("UCI：连接云端 Queue 失败", f"已连续 {self._retry_streak} 次失败（{result.error_code or '未知错误'}），请查看 Worker 日志。")
            return
        self._retry_streak = 0
        self._retry_streak_notified = False
        if result.status == "COMPLETED":
            self._notify("UCI：视频已完成", f"任务 {short_id} 已输出 final.mp4 和 info.md。")
        elif result.status == "PAUSED":
            self._notify("UCI：任务已暂停", f"任务 {short_id} 暂停（{result.error_code or '未知原因'}），需要人工处理。")
        elif result.status == "FAILED":
            self._notify("UCI：任务失败", f"任务 {short_id} 失败（{result.error_code or '未知原因'}）。")
        elif result.status == "ELIMINATED":
            self._notify("UCI：Rank 2 已作废", f"任务 {short_id} 未能在截止时间前开始。")

    def _claim_or_empty(self, state: WorkerState | None) -> WorkerResult:
        current = state or WorkerState("POLLING", self.request_id_factory())
        if state is None:
            self.state_store.save(current)
        try:
            task = self.client.claim(current.claim_request_id)
        except QueueApiError as error:
            if error.code == "CLAIM_REQUEST_EXPIRED":
                self.state_store.clear()
                next_state = WorkerState("POLLING", self.request_id_factory())
                self.state_store.save(next_state)
                try:
                    task = self.client.claim(next_state.claim_request_id)
                except QueueApiError as next_error:
                    if next_error.code == "CLAIM_REQUEST_EXPIRED":
                        self.state_store.clear()
                    self._log("claim", next_state, "retry", next_error.code, detail=_error_detail(next_error))
                    return WorkerResult("RETRY", error_code=next_error.code)
                current = next_state
            else:
                self._log("claim", current, "retry", error.code, detail=_error_detail(error))
                return WorkerResult("RETRY", error_code=error.code)
        if task is None:
            self.state_store.clear()
            self._log("claim", current, "empty")
            return WorkerResult("EMPTY")
        active = self._state_from_claim(task, current.claim_request_id)
        self.state_store.save(active)
        self._log("claim", active, "CLAIMED", attempt=active.attempts)
        return self._run_active(active)

    @staticmethod
    def _state_from_claim(task: ClaimedTask, request_id: str) -> WorkerState:
        if task.claim_request_id not in (None, "", request_id):
            raise WorkerStateError("Claim response request ID does not match the persisted poll")
        local_job_id = task.local_job_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"uci-queue:{task.queue_id}"))
        return WorkerState(
            worker_state="CLAIMED",
            claim_request_id=request_id,
            queue_id=task.queue_id,
            claim_token=task.claim_token,
            lease_until=task.lease_until,
            local_job_id=local_job_id,
            url=task.url,
            attempts=task.attempts,
            core_started_at=task.core_started_at,
            selection_day=task.selection_day,
            selection_rank=task.selection_rank,
            rank2_start_cutoff_at=task.rank2_start_cutoff_at,
            monitor=dict(task.monitor) if task.monitor else None,
        )

    def _run_active(self, state: WorkerState) -> WorkerResult:
        try:
            heartbeat = self.client.heartbeat(state.queue_id or "", state.claim_token or "")
            if heartbeat.status != "PROCESSING" or not heartbeat.lease_until:
                raise QueueApiError("API_RESPONSE_INVALID", "Heartbeat did not return PROCESSING and a lease.")
            lease_expiry = _parse_utc(heartbeat.lease_until)
            if (lease_expiry - datetime.now(UTC)).total_seconds() <= self.config.queue_heartbeat_seconds:
                raise QueueApiError("CONFIG_INVALID", "Current Queue lease is shorter than the configured heartbeat interval.")
        except QueueApiError as error:
            if error.code in OWNERSHIP_LOST_CODES:
                self._stop_core(state)
                self.state_store.clear()
                self._log("heartbeat", state, "ownership_lost", error.code)
                return WorkerResult("OWNERSHIP_LOST", state.queue_id, state.local_job_id, state.attempts, error.code)
            self._log("heartbeat", state, "retry", error.code, detail=_error_detail(error))
            return WorkerResult("RETRY", state.queue_id, state.local_job_id, state.attempts, error.code)

        processing = replace(state, worker_state="PROCESSING", lease_until=heartbeat.lease_until)
        self.state_store.save(processing)
        self._log("heartbeat", processing, "PROCESSING", attempt=processing.attempts)
        if not processing.core_started_at:
            try:
                started = self.client.core_started(processing.queue_id or "", processing.claim_token or "")
                if started.status == "FAILED":
                    self.state_store.clear()
                    self._log("core_start", processing, "ELIMINATED", "RANK2_START_CUTOFF")
                    return WorkerResult("ELIMINATED", processing.queue_id, processing.local_job_id,
                                        processing.attempts, "RANK2_START_CUTOFF")
                if started.status != "PROCESSING" or not started.core_started_at:
                    raise QueueApiError("API_RESPONSE_INVALID", "Core start authorization response is invalid.")
                processing = replace(processing, core_started_at=started.core_started_at)
                self.state_store.save(processing)
            except QueueApiError as error:
                if error.code in OWNERSHIP_LOST_CODES:
                    self.state_store.clear()
                    self._log("core_start", processing, "ownership_lost", error.code)
                    return WorkerResult("OWNERSHIP_LOST", processing.queue_id, processing.local_job_id,
                                        processing.attempts, error.code)
                self._log("core_start", processing, "retry", error.code, detail=_error_detail(error))
                return WorkerResult("RETRY", processing.queue_id, processing.local_job_id,
                                    processing.attempts, error.code)
        loop = _HeartbeatLoop(self.client, self.state_store, processing, self.config.queue_heartbeat_seconds)
        loop.start()

        def on_started(pid: int) -> None:
            latest = self.state_store.load() or processing
            updated = replace(latest, worker_state="PROCESSING", core_pid=pid,
                              core_started_at=latest.core_started_at or _utc_now())
            self.state_store.save(updated)
            self._log("core_start", updated, "PROCESSING", attempt=updated.attempts)

        try:
            result = self.core_runner.run(processing, cancel_event=loop.cancel_event, on_started=on_started)
        except SystemExit:
            loop.stop()
            raise
        except Exception as error:
            result = CoreRunResult(
                success=False,
                local_job_id=processing.local_job_id,
                error=CoreError.for_code(
                    ErrorCode.API_FAILED,
                    message="Core runner failed before returning a Job result.",
                    cause=type(error).__name__,
                ),
            )
        finally:
            if loop._thread.is_alive():
                loop.stop()

        latest = self.state_store.load() or processing
        if result.local_job_id != processing.local_job_id:
            result = CoreRunResult(
                success=False,
                local_job_id=processing.local_job_id,
                error=CoreError.for_code(
                    ErrorCode.PROVIDER_FAILED,
                    message="Core runner returned a result for a different local Job identity.",
                    cause="LOCAL_JOB_ID_MISMATCH",
                ),
            )
        if loop.ownership_lost:
            self.state_store.clear()
            self._log("core_stop", latest, "ownership_lost", loop.error.code if loop.error else None)
            return WorkerResult("OWNERSHIP_LOST", latest.queue_id, latest.local_job_id, latest.attempts, loop.error.code if loop.error else None)
        if result.cancelled:
            error_code = loop.error.code if loop.error else None
            self._log("core_stop", latest, "retry", error_code)
            return WorkerResult("RETRY", latest.queue_id, latest.local_job_id, latest.attempts, error_code)

        if result.success and isinstance(result.result_path, str) and result.result_path.strip():
            pending = replace(latest, worker_state="COMPLETING", result_path=result.result_path)
            self.state_store.save(pending)
            self._log("complete", pending, "COMPLETING", attempt=pending.attempts)
            if loop.error is not None:
                return WorkerResult("RETRY", pending.queue_id, pending.local_job_id, pending.attempts, loop.error.code)
            return self._finalize(pending)

        core_error = result.error or CoreError.for_code(
            ErrorCode.API_FAILED,
            message="Core runner returned no successful Job result.",
            cause="CORE_RESULT_INVALID",
        )
        pending = replace(latest, worker_state="FAILING", failure=core_error.to_dict())
        self.state_store.save(pending)
        self._log("fail", pending, "FAILING", attempt=pending.attempts, error_code=core_error.code.value)
        if loop.error is not None:
            return WorkerResult("RETRY", pending.queue_id, pending.local_job_id, pending.attempts, loop.error.code)
        return self._finalize(pending)

    def _finalize(self, state: WorkerState) -> WorkerResult:
        operation = "complete" if state.worker_state == "COMPLETING" else "fail"
        try:
            if state.worker_state == "COMPLETING":
                result = self.client.complete(
                    state.queue_id or "",
                    state.claim_token or "",
                    state.local_job_id or "",
                    state.result_path or "",
                )
                expected = "COMPLETED"
            else:
                error = CoreError.from_dict(state.failure)
                if error is None:
                    raise QueueApiError("INVALID_REQUEST", "Persisted Worker failure is missing a CoreError.")
                result = self.client.fail(state.queue_id or "", state.claim_token or "", state.local_job_id or "", error)
                expected = "PAUSED" if error.recoverable else "FAILED"
            if result.status != expected:
                raise QueueApiError("API_RESPONSE_INVALID", "Queue finalization returned an unexpected state.")
        except QueueApiError as error:
            if error.code in OWNERSHIP_LOST_CODES:
                self.state_store.clear()
                self._log(operation, state, "ownership_lost", error.code)
                return WorkerResult("OWNERSHIP_LOST", state.queue_id, state.local_job_id, state.attempts, error.code)
            self._log(operation, state, "retry", error.code, detail=_error_detail(error))
            return WorkerResult("RETRY", state.queue_id, state.local_job_id, state.attempts, error.code)
        self.state_store.clear()
        self._log(operation, state, result.status, attempt=state.attempts)
        failure_code = str(state.failure.get("code")) if isinstance(state.failure, Mapping) and state.failure.get("code") else None
        return WorkerResult(result.status, state.queue_id, state.local_job_id, state.attempts, failure_code)

    def _stop_core(self, state: WorkerState) -> None:
        stop = getattr(self.core_runner, "stop", None)
        if callable(stop):
            try:
                stop(state)
            except Exception as error:
                self.logger.warning(
                    "queue_id=%s local_job_id=%s operation=core_stop state=stop_failed attempt=%s error_code=%s",
                    state.queue_id or "-",
                    state.local_job_id or "-",
                    state.attempts,
                    type(error).__name__,
                )

    def _log(
        self,
        operation: str,
        state: WorkerState,
        status: str,
        error_code: str | None = None,
        *,
        attempt: int | None = None,
        detail: str | None = None,
    ) -> None:
        message = "queue_id=%s local_job_id=%s operation=%s state=%s attempt=%s error_code=%s"
        values: list[Any] = [
            state.queue_id or "-",
            state.local_job_id or "-",
            operation,
            status,
            attempt if attempt is not None else state.attempts,
            error_code or "-",
        ]
        if detail:
            message += " detail=%s"
            values.append(detail)
        self.logger.info(message, *values)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise QueueApiError("API_RESPONSE_INVALID", "Queue lease timestamp is invalid.") from None
    if parsed.tzinfo is None:
        raise QueueApiError("API_RESPONSE_INVALID", "Queue lease timestamp must include a timezone.")
    return parsed.astimezone(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def render_launch_agent_plist(
    *,
    project_root: Path | str,
    python_executable: Path | str,
    config_path: Path | str,
    log_dir: Path | str,
) -> str:
    """Build an installable resident LaunchAgent plist without installing it."""

    import plistlib

    project = Path(project_root).expanduser().resolve()
    python = Path(python_executable).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    logs = Path(log_dir).expanduser().resolve()
    payload = {
        "Label": "local.universal-content-intake.worker",
        "ProgramArguments": [str(python), "-m", "src.queue.worker_cli", "--config", str(config)],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / "worker.stdout.log"),
        "StandardErrorPath": str(logs / "worker.stderr.log"),
    }
    data = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    return data.decode("utf-8")
