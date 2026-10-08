from __future__ import annotations

import os

os.environ["UCI_PUBLISH_WRITER"] = "off"  # never call an LLM API (Claude or Gemini) from tests

import io
import json
import logging
import plistlib
import tempfile
import threading
import time
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path

from src.core.errors import CoreError, ErrorCode
from src.core.job import Job, JobState, OutputResult
from src.queue.client import ClaimedTask, QueueApiError, QueueOperationResult
from src.queue.worker import (
    CoreRunResult,
    QueueWorker,
    SubprocessCoreRunner,
    WorkerConfig,
    WorkerFileLock,
    WorkerResult,
    WorkerState,
    WorkerStateStore,
    render_launch_agent_plist,
)


FUTURE_LEASE = "2030-01-01T00:15:00Z"
EXTENDED_LEASE = "2030-01-01T00:30:00Z"
TASK = ClaimedTask(
    queue_id="queue-001",
    video_id="video-001",
    url="https://youtube.example/watch?v=video-001",
    claim_token="sensitive-claim-token",
    lease_until=FUTURE_LEASE,
    attempts=2,
    claim_request_id="request-000000000001",
    claimed_at="2030-01-01T00:00:00Z",
)


class FakeQueueClient:
    def __init__(self, claims=(), heartbeats=(), completes=(), failures=(), core_starts=()) -> None:
        self.claims = deque(claims)
        self.heartbeats = deque(heartbeats)
        self.completes = deque(completes)
        self.failures = deque(failures)
        self.core_starts = deque(core_starts)
        self.claim_request_ids: list[str] = []
        self.heartbeat_calls: list[tuple[str, str]] = []
        self.complete_calls: list[tuple[str, str, str, str]] = []
        self.fail_calls: list[tuple[str, str, str, CoreError]] = []
        self.core_started_calls: list[tuple[str, str]] = []

    def claim(self, claim_request_id: str):
        self.claim_request_ids.append(claim_request_id)
        value = self.claims.popleft() if self.claims else None
        if isinstance(value, BaseException):
            raise value
        return value

    def heartbeat(self, queue_id: str, claim_token: str):
        self.heartbeat_calls.append((queue_id, claim_token))
        value = self.heartbeats.popleft() if self.heartbeats else QueueOperationResult(
            queue_id=queue_id, status="PROCESSING", lease_until=FUTURE_LEASE
        )
        if isinstance(value, BaseException):
            raise value
        return value

    def core_started(self, queue_id: str, claim_token: str):
        self.core_started_calls.append((queue_id, claim_token))
        value = self.core_starts.popleft() if self.core_starts else QueueOperationResult(
            queue_id=queue_id, status="PROCESSING", core_started_at="2030-01-01T00:00:00Z"
        )
        if isinstance(value, BaseException):
            raise value
        return value

    def complete(self, queue_id: str, claim_token: str, local_job_id: str, result_path: str):
        self.complete_calls.append((queue_id, claim_token, local_job_id, result_path))
        value = self.completes.popleft() if self.completes else QueueOperationResult(
            queue_id=queue_id, status="COMPLETED", local_job_id=local_job_id, result_path=result_path
        )
        if isinstance(value, Exception):
            raise value
        return value

    def fail(self, queue_id: str, claim_token: str, local_job_id: str, error: CoreError):
        self.fail_calls.append((queue_id, claim_token, local_job_id, error))
        value = self.failures.popleft() if self.failures else QueueOperationResult(
            queue_id=queue_id,
            status="PAUSED" if error.recoverable else "FAILED",
            local_job_id=local_job_id,
            last_error=error.code.value,
        )
        if isinstance(value, Exception):
            raise value
        return value


class FakeCoreRunner:
    def __init__(self, result=None, *, callback=None, exception=None) -> None:
        self.result = result or CoreRunResult(success=True, result_path="/jobs/job-001")
        self.callback = callback
        self.exception = exception
        self.calls: list[WorkerState] = []
        self.stop_calls: list[WorkerState] = []

    def run(self, state, *, cancel_event: threading.Event, on_started):
        self.calls.append(state)
        if self.callback:
            self.callback(state, cancel_event, on_started)
        if self.exception:
            raise self.exception
        return replace(self.result, local_job_id=state.local_job_id)

    def stop(self, state):
        self.stop_calls.append(state)


class _DoneProcess:
    pid = 12345

    def __init__(self, return_code: int = 0) -> None:
        self.return_code = return_code

    def poll(self):
        return self.return_code


def _job_for(arguments) -> tuple[Path, Job]:
    job_file = Path(arguments[arguments.index("--resume-job") + 1])
    return job_file, Job.from_json(job_file.read_text(encoding="utf-8"))


def _advance_fake_job(arguments) -> _DoneProcess:
    """Simulate one successful Core CLI step by persisting its Job progress."""

    job_file, job = _job_for(arguments)
    step = arguments[3]
    job.error = None
    if step == "video-run":
        job.store_provider_metadata("yt-dlp", {"validated_artifact": {"path": "temp/source_video/source_video.mp4"}})
        job.current_state = JobState.DOWNLOADING
    elif step == "stage3-run":
        job.source_metadata["stage3"] = {"status": "validated"}
        job.current_state = JobState.RENDERING
    elif step == "finalize-job":
        (Path(job.output_path) / "final.mp4").write_bytes(b"validated formal video")
        (Path(job.output_path) / "info.md").write_text("# E2E\n", encoding="utf-8")
        job.current_state = JobState.COMPLETED
        # Stage 5 records paths relative to the formal output directory.
        job.output_result = OutputResult(content_paths=("final.mp4",), info_path="info.md")
    job_file.write_text(job.to_json(), encoding="utf-8")
    return _DoneProcess(0)


def _fail_fake_job(arguments, code: ErrorCode) -> _DoneProcess:
    job_file, job = _job_for(arguments)
    job.current_state = JobState(code.value)
    job.error = CoreError.for_code(code, cause="HTTP Error 429: Too Many Requests")
    job_file.write_text(job.to_json(), encoding="utf-8")
    return _DoneProcess(2)


class QueueWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state_dir = root / "state"
        self.workspace_root = root / "jobs"
        self.store = WorkerStateStore(self.state_dir)
        self.config = WorkerConfig(
            queue_poll_seconds=120,
            queue_heartbeat_seconds=1,
            queue_lease_seconds=900,
            state_dir=self.state_dir,
            workspace_root=self.workspace_root,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def worker(self, client, core=None, *, request_ids=None, logger=None, config=None):
        ids = iter(request_ids or ["request-000000000001", "request-000000000002"])
        return QueueWorker(
            client=client,
            core_runner=core or FakeCoreRunner(),
            config=config or self.config,
            state_store=self.store,
            request_id_factory=lambda: next(ids),
            logger=logger,
        )

    def test_defaults_and_poll_heartbeat_config_are_reloadable(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"UCI_CONFIG": "none"}):  # the shipped defaults, not this machine's overlay
            defaults = WorkerConfig.from_defaults(Path(__file__).resolve().parents[1] / "config" / "defaults.yaml")
        self.assertEqual(defaults.queue_poll_seconds, 120)
        self.assertEqual(defaults.queue_heartbeat_seconds, 300)
        self.assertEqual(defaults.queue_lease_seconds, 900)
        self.assertEqual(defaults.production_timezone, "Asia/Shanghai")
        self.assertEqual(defaults.rank2_start_cutoff, "12:00")
        self.assertEqual(defaults.processing_concurrency, 1)

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "defaults.yaml"
            config_path.write_text(
                "schema_version: 1\noutput:\n  root: ~/jobs\nqueue_poll_seconds: 17\n"
                "queue_heartbeat_seconds: 23\nqueue_lease_seconds: 120\n"
                "production_timezone: Asia/Shanghai\nrank2_start_cutoff: \"08:00\"\nprocessing_concurrency: 1\n"
                "worker_state_dir: ~/worker-state\n",
                encoding="utf-8",
            )
            changed = WorkerConfig.from_defaults(config_path)
            self.assertEqual(changed.queue_poll_seconds, 17)
            self.assertEqual(changed.queue_heartbeat_seconds, 23)
            self.assertEqual(changed.queue_lease_seconds, 120)

    def test_heartbeat_must_be_shorter_than_lease(self) -> None:
        with self.assertRaises(ValueError):
            WorkerConfig(120, 900, 900, self.state_dir, self.workspace_root)

    def test_processing_concurrency_is_serial(self) -> None:
        with self.assertRaises(ValueError):
            WorkerConfig(120, 1, 900, self.state_dir, self.workspace_root, processing_concurrency=2)

    def test_rank_two_server_cutoff_rejects_core_start_and_clears_local_claim(self) -> None:
        rank_two = replace(TASK, selection_day="2030-01-01", selection_rank=2,
                           rank2_start_cutoff_at="2030-01-01T00:00:00Z")
        client = FakeQueueClient(
            claims=[rank_two],
            core_starts=[QueueOperationResult(queue_id=TASK.queue_id, status="FAILED")],
        )
        core = FakeCoreRunner()
        result = self.worker(client, core).poll_once()
        self.assertEqual(result.status, "ELIMINATED")
        self.assertEqual(result.error_code, "RANK2_START_CUTOFF")
        self.assertEqual(core.calls, [])
        self.assertIsNone(self.store.load())

    def test_rank_two_start_authorized_before_cutoff_runs_serial_core(self) -> None:
        rank_two = replace(TASK, selection_day="2030-01-01", selection_rank=2,
                           rank2_start_cutoff_at="2030-01-01T08:00:00+08:00")
        client = FakeQueueClient(claims=[rank_two])
        core = FakeCoreRunner(result=CoreRunResult(success=True, result_path="/jobs/rank2"))
        result = self.worker(client, core).poll_once()
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(len(core.calls), 1)

    def test_rank_one_can_start_after_rank_two_cutoff_and_finish(self) -> None:
        rank_one = replace(TASK, selection_day="2030-01-01", selection_rank=1,
                           rank2_start_cutoff_at="2020-01-01T00:00:00Z")
        client = FakeQueueClient(claims=[rank_one])
        core = FakeCoreRunner(result=CoreRunResult(success=True, result_path="/jobs/rank1"))
        result = self.worker(client, core).poll_once()
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(len(core.calls), 1)

    def test_empty_queue_is_normal_and_next_poll_gets_a_new_request_id(self) -> None:
        client = FakeQueueClient(claims=[None, None])
        worker = self.worker(client)
        self.assertEqual(worker.poll_once().status, "EMPTY")
        self.assertEqual(worker.poll_once().status, "EMPTY")
        self.assertEqual(client.claim_request_ids, ["request-000000000001", "request-000000000002"])
        self.assertIsNone(self.store.load())

    def test_run_forever_sleeps_for_configured_poll_interval_then_repolls(self) -> None:
        worker = self.worker(FakeQueueClient())
        outcomes = deque((WorkerResult("EMPTY"), WorkerResult("RETRY")))
        worker.poll_once = lambda: outcomes.popleft()

        class StopAfterTwoWaits:
            def __init__(self) -> None:
                self.waits: list[float] = []
                self.stopped = False

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, seconds: float) -> bool:
                self.waits.append(seconds)
                if len(self.waits) == 2:
                    self.stopped = True
                return self.stopped

        stop_event = StopAfterTwoWaits()
        result = worker.run_forever(stop_event)  # type: ignore[arg-type]
        self.assertEqual(result.status, "STOPPED")
        self.assertEqual(stop_event.waits, [120, 120])

    def test_temporary_queue_api_failure_keeps_same_poll_for_safe_retry(self) -> None:
        client = FakeQueueClient(
            claims=[QueueApiError("TRANSPORT_FAILED", "temporary", retryable=True), None]
        )
        worker = self.worker(client)
        self.assertEqual(worker.poll_once().status, "RETRY")
        self.assertEqual(self.store.load().claim_request_id, "request-000000000001")
        self.assertEqual(worker.poll_once().status, "EMPTY")
        self.assertEqual(client.claim_request_ids, ["request-000000000001", "request-000000000001"])

    def test_uncertain_claim_keeps_poll_id_for_restart_retry(self) -> None:
        client = FakeQueueClient(claims=[QueueApiError("TRANSPORT_FAILED", "offline", claim_request_id="request-000000000001"), None])
        first = self.worker(client)
        self.assertEqual(first.poll_once().status, "RETRY")
        persisted = self.store.load()
        self.assertEqual(persisted.worker_state, "POLLING")
        self.assertEqual(persisted.claim_request_id, "request-000000000001")

        restarted = self.worker(client, request_ids=["request-000000000099"])
        self.assertEqual(restarted.poll_once().status, "EMPTY")
        self.assertEqual(client.claim_request_ids, ["request-000000000001", "request-000000000001"])

    def test_claim_is_persisted_before_core_and_uses_processing_heartbeat(self) -> None:
        client = FakeQueueClient(claims=[TASK])
        seen = []

        def inspect_state(state, _cancel, on_started):
            persisted = self.store.load()
            seen.append(persisted)
            self.assertEqual(persisted.worker_state, "PROCESSING")
            self.assertEqual(persisted.queue_id, TASK.queue_id)
            self.assertEqual(persisted.claim_token, TASK.claim_token)
            self.assertTrue(persisted.local_job_id)
            on_started(4567)

        core = FakeCoreRunner(callback=inspect_state)
        output = io.StringIO()
        logger = logging.getLogger(f"worker-test-{id(self)}")
        logger.handlers = [logging.StreamHandler(output)]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        result = self.worker(client, core, logger=logger).poll_once()

        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(client.heartbeat_calls, [(TASK.queue_id, TASK.claim_token)])
        self.assertEqual(client.complete_calls[0][0], TASK.queue_id)
        self.assertEqual(len(seen), 1)
        self.assertNotIn(TASK.claim_token, output.getvalue())

    def test_crash_after_claim_before_core_resumes_claim_without_new_claim(self) -> None:
        client = FakeQueueClient(
            claims=[TASK],
            heartbeats=[
                SystemExit(92),
                QueueOperationResult(queue_id=TASK.queue_id, status="PROCESSING", lease_until=FUTURE_LEASE),
            ],
        )
        never_started = FakeCoreRunner()
        with self.assertRaises(SystemExit):
            self.worker(client, never_started).poll_once()

        claimed = self.store.load()
        self.assertEqual(claimed.worker_state, "CLAIMED")
        self.assertEqual(claimed.claim_request_id, TASK.claim_request_id)
        self.assertEqual(never_started.calls, [])

        resumed = FakeCoreRunner(result=CoreRunResult(success=True, result_path="/jobs/result"))
        self.assertEqual(self.worker(client, resumed).poll_once().status, "COMPLETED")
        self.assertEqual(client.claim_request_ids, ["request-000000000001"])
        self.assertEqual(resumed.calls[0].local_job_id, claimed.local_job_id)

    def test_background_heartbeat_extends_persisted_lease_while_core_runs(self) -> None:
        class SignalingClient(FakeQueueClient):
            second_heartbeat = threading.Event()

            def heartbeat(self, queue_id: str, claim_token: str):
                result = super().heartbeat(queue_id, claim_token)
                if len(self.heartbeat_calls) >= 2:
                    self.second_heartbeat.set()
                return result

        client = SignalingClient(
            claims=[TASK],
            heartbeats=[
                QueueOperationResult(queue_id=TASK.queue_id, status="PROCESSING", lease_until=FUTURE_LEASE),
                QueueOperationResult(queue_id=TASK.queue_id, status="PROCESSING", lease_until=EXTENDED_LEASE),
            ],
        )
        config = WorkerConfig(120, 0.01, 900, self.state_dir, self.workspace_root)

        def wait_for_extension(_state, _cancel, _on_started):
            self.assertTrue(client.second_heartbeat.wait(1))
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                current = self.store.load()
                if current and current.lease_until == EXTENDED_LEASE:
                    break
                time.sleep(0.002)
            self.assertEqual(self.store.load().lease_until, EXTENDED_LEASE)

        core = FakeCoreRunner(
            result=CoreRunResult(success=True, result_path="/jobs/result"),
            callback=wait_for_extension,
        )
        result = self.worker(client, core, config=config).poll_once()
        self.assertEqual(result.status, "COMPLETED")
        self.assertGreaterEqual(len(client.heartbeat_calls), 2)

    def test_worker_restart_reuses_persisted_job_identity_and_never_reclaims(self) -> None:
        client = FakeQueueClient(claims=[TASK])
        crashing_core = FakeCoreRunner(exception=SystemExit(91))
        with self.assertRaises(SystemExit):
            self.worker(client, crashing_core).poll_once()

        active = self.store.load()
        self.assertEqual(active.worker_state, "PROCESSING")
        stable_id = active.local_job_id
        self.assertTrue(active.core_pid is None or active.core_pid > 0)

        resumed_core = FakeCoreRunner(result=CoreRunResult(success=True, result_path="/jobs/resumed"))
        restarted = self.worker(client, resumed_core, request_ids=["request-000000000099"])
        self.assertEqual(restarted.poll_once().status, "COMPLETED")
        self.assertEqual(len(client.claim_request_ids), 1)
        self.assertEqual(resumed_core.calls[0].local_job_id, stable_id)

    def test_complete_response_loss_retries_complete_without_rerunning_core(self) -> None:
        client = FakeQueueClient(
            claims=[TASK],
            completes=[
                QueueApiError("TRANSPORT_FAILED", "lost response"),
                QueueOperationResult(queue_id=TASK.queue_id, status="COMPLETED", local_job_id="job-001", result_path="/jobs/result"),
            ],
        )
        core = FakeCoreRunner(result=CoreRunResult(success=True, result_path="/jobs/result"))
        first = self.worker(client, core)
        self.assertEqual(first.poll_once().status, "RETRY")
        pending = self.store.load()
        self.assertEqual(pending.worker_state, "COMPLETING")
        local_job_id = pending.local_job_id
        result_path = pending.result_path

        restarted = self.worker(client, FakeCoreRunner(), request_ids=["request-000000000099"])
        self.assertEqual(restarted.poll_once().status, "COMPLETED")
        self.assertEqual(len(core.calls), 1)
        self.assertEqual(len(client.complete_calls), 2)
        self.assertEqual(client.complete_calls[0], client.complete_calls[1])
        self.assertEqual(client.complete_calls[1][2:], (local_job_id, result_path))

    def test_recoverable_and_terminal_core_failures_map_through_stage1_contract(self) -> None:
        cases = (
            (ErrorCode.NETWORK_PAUSED, "PAUSED"),
            (ErrorCode.URL_INVALID, "FAILED"),
        )
        for code, expected_status in cases:
            with self.subTest(code=code.value):
                client = FakeQueueClient(claims=[TASK])
                core = FakeCoreRunner(result=CoreRunResult(success=False, error=CoreError.for_code(code)))
                result = self.worker(client, core).poll_once()
                self.assertEqual(result.status, expected_status)
                self.assertEqual(client.fail_calls[0][3].code, code)
                self.assertIsNone(self.store.load())

    def test_fail_response_loss_retries_same_error_without_rerunning_core(self) -> None:
        client = FakeQueueClient(
            claims=[TASK],
            failures=[
                QueueApiError("TRANSPORT_FAILED", "lost response"),
                QueueOperationResult(queue_id=TASK.queue_id, status="PAUSED", local_job_id="job-001"),
            ],
        )
        core = FakeCoreRunner(result=CoreRunResult(success=False, error=CoreError.for_code(ErrorCode.NETWORK_PAUSED)))
        self.assertEqual(self.worker(client, core).poll_once().status, "RETRY")
        self.assertEqual(self.store.load().worker_state, "FAILING")

        restarted = self.worker(client, FakeCoreRunner(), request_ids=["request-000000000099"])
        self.assertEqual(restarted.poll_once().status, "PAUSED")
        self.assertEqual(len(core.calls), 1)
        self.assertEqual(len(client.fail_calls), 2)
        self.assertEqual(client.fail_calls[0][3].to_dict(), client.fail_calls[1][3].to_dict())

    def test_invalid_claim_token_during_heartbeat_stops_core_and_never_completes(self) -> None:
        heartbeats = [
            QueueOperationResult(queue_id=TASK.queue_id, status="PROCESSING", lease_until=FUTURE_LEASE),
            QueueApiError("CLAIM_TOKEN_INVALID", "lost ownership"),
        ]
        client = FakeQueueClient(claims=[TASK], heartbeats=heartbeats)
        cancelled = threading.Event()

        def wait_for_cancel(_state, cancel_event, _on_started):
            cancelled.wait(1)
            self.assertTrue(cancel_event.is_set())

        core = FakeCoreRunner(result=CoreRunResult(success=False, cancelled=True), callback=wait_for_cancel)
        config = WorkerConfig(120, 0.01, 900, self.state_dir, self.workspace_root)
        result = self.worker(client, core, config=config).poll_once()

        self.assertEqual(result.status, "OWNERSHIP_LOST")
        self.assertEqual(len(client.heartbeat_calls), 2)
        self.assertEqual(client.complete_calls, [])
        self.assertEqual(client.fail_calls, [])
        self.assertIsNone(self.store.load())

    def test_expired_lease_on_restart_stops_existing_core_before_clearing_owner(self) -> None:
        active = WorkerState(
            worker_state="PROCESSING",
            claim_request_id=TASK.claim_request_id,
            queue_id=TASK.queue_id,
            claim_token=TASK.claim_token,
            lease_until=FUTURE_LEASE,
            local_job_id="stable-job-after-restart",
            url=TASK.url,
            attempts=TASK.attempts,
            core_pid=2468,
        )
        self.store.save(active)
        client = FakeQueueClient(heartbeats=[QueueApiError("LEASE_EXPIRED", "new owner reclaimed")])
        core = FakeCoreRunner()
        result = self.worker(client, core).poll_once()

        self.assertEqual(result.status, "OWNERSHIP_LOST")
        self.assertEqual(core.stop_calls, [active])
        self.assertEqual(core.calls, [])
        self.assertIsNone(self.store.load())

    def test_transient_heartbeat_failure_stops_core_but_preserves_claim_for_retry(self) -> None:
        heartbeats = [
            QueueOperationResult(queue_id=TASK.queue_id, status="PROCESSING", lease_until=FUTURE_LEASE),
            QueueApiError("TRANSPORT_FAILED", "temporary"),
        ]
        client = FakeQueueClient(claims=[TASK], heartbeats=heartbeats)

        def wait_for_cancel(_state, cancel_event, _on_started):
            cancel_event.wait(1)
            self.assertTrue(cancel_event.is_set())

        core = FakeCoreRunner(result=CoreRunResult(success=False, cancelled=True), callback=wait_for_cancel)
        config = WorkerConfig(120, 0.01, 900, self.state_dir, self.workspace_root)
        result = self.worker(client, core, config=config).poll_once()

        self.assertEqual(result.status, "RETRY")
        self.assertEqual(self.store.load().worker_state, "PROCESSING")
        self.assertEqual(client.complete_calls, [])
        self.assertEqual(client.fail_calls, [])

    def test_claim_request_expired_discards_old_poll_before_generating_a_new_id(self) -> None:
        client = FakeQueueClient(claims=[QueueApiError("CLAIM_REQUEST_EXPIRED", "expired"), None])
        self.store.save(WorkerState(worker_state="POLLING", claim_request_id="request-000000000000"))
        worker = self.worker(client, request_ids=["request-000000000001", "request-000000000002"])
        self.assertEqual(worker.poll_once().status, "EMPTY")
        self.assertEqual(client.claim_request_ids, ["request-000000000000", "request-000000000001"])

    def test_worker_state_is_private_atomic_and_contains_required_claim_identity(self) -> None:
        record = WorkerState(
            worker_state="PROCESSING",
            claim_request_id="request-000000000001",
            queue_id=TASK.queue_id,
            claim_token=TASK.claim_token,
            lease_until=FUTURE_LEASE,
            local_job_id="job-stable",
            url=TASK.url,
        )
        self.store.save(record)
        self.assertEqual(self.store.load(), record)
        mode = self.store.path.stat().st_mode & 0o777
        if os.name != "nt":  # Windows has no owner-only mode bits; the user's profile folder is private already
            self.assertEqual(mode, 0o600)
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["queue_id"], TASK.queue_id)
        self.assertEqual(payload["worker_state"], "PROCESSING")

    def test_second_local_worker_cannot_acquire_lock(self) -> None:
        first = WorkerFileLock(self.state_dir)
        second = WorkerFileLock(self.state_dir)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()

    @unittest.skipIf(os.name == "nt", "LaunchAgents are macOS only; tests/test_compat.py covers Task Scheduler")
    def test_launch_agent_is_a_resident_login_worker_and_not_a_two_minute_cron(self) -> None:
        encoded = render_launch_agent_plist(
            project_root=Path("/Applications/UniversalContentIntake"),
            python_executable=Path("/usr/bin/python3"),
            config_path=Path("/Applications/UniversalContentIntake/config/defaults.yaml"),
            log_dir=Path("~/Library/Logs/UCI").expanduser(),
        )
        value = plistlib.loads(encoded.encode("utf-8"))
        self.assertEqual(value["Label"], "local.universal-content-intake.worker")
        self.assertTrue(value["RunAtLoad"])
        self.assertEqual(value["KeepAlive"], {"SuccessfulExit": False})
        self.assertNotIn("StartInterval", value)
        self.assertEqual(value["ProgramArguments"][0], "/usr/bin/python3")

    def _runner(self, **kwargs) -> SubprocessCoreRunner:
        options = {"caffeinate_executable": None, "network_retry_delays": (0.0, 0.0, 0.0)}
        options.update(kwargs)
        return SubprocessCoreRunner(
            project_root=Path(__file__).resolve().parents[1],
            workspace_root=self.workspace_root,
            python_executable=Path("/usr/bin/python3"),
            **options,
        )

    def _state(self, local_job_id: str, **kwargs) -> WorkerState:
        return WorkerState(
            worker_state="PROCESSING",
            claim_request_id="request-000000000001",
            queue_id=TASK.queue_id,
            claim_token=TASK.claim_token,
            lease_until=FUTURE_LEASE,
            local_job_id=local_job_id,
            url=TASK.url,
            **kwargs,
        )

    def test_core_runner_runs_acquisition_stage3_and_output_in_order(self) -> None:
        # The process is mocked: this verifies the spawn boundary without downloading content.
        runner = self._runner()
        state = self._state("stable-job-001", monitor={"hot_reason": "cold_views_like_rate", "views_at_hot": 1200})
        started = []
        commands: list[list[str]] = []

        def fake_popen(arguments, **kwargs):
            commands.append(arguments)
            return _advance_fake_job(arguments)

        from unittest.mock import patch

        with patch("src.queue.worker.subprocess.Popen", side_effect=fake_popen):
            result = runner.run(state, cancel_event=threading.Event(), on_started=started.append)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.local_job_id, "stable-job-001")
        self.assertEqual(result.result_path, str((self.workspace_root / "job-stable-job-001" / "output" / "final.mp4").resolve()))
        self.assertEqual([command[3] for command in commands], ["video-run", "stage3-run", "finalize-job"])
        self.assertTrue(all(command[4] == "--resume-job" for command in commands))
        self.assertEqual(started, [12345, 12345, 12345])
        job = Job.from_json(Path(commands[0][5]).read_text(encoding="utf-8"))
        self.assertEqual(job.source_metadata["monitor"]["queue_id"], TASK.queue_id)
        self.assertEqual(job.source_metadata["monitor"]["hot_reason"], "cold_views_like_rate")

    def test_core_runner_resumes_at_stage3_when_source_video_is_already_validated(self) -> None:
        runner = self._runner()
        state = self._state("stage3-resume-job")
        from unittest.mock import patch

        commands: list[list[str]] = []
        with patch("src.queue.worker.subprocess.Popen", side_effect=lambda args, **_: commands.append(args) or _advance_fake_job(args)):
            job_file = runner._ensure_job_file(state)
            job = Job.from_json(job_file.read_text(encoding="utf-8"))
            job.store_provider_metadata("yt-dlp", {"validated_artifact": {"path": "temp/source_video/source_video.mp4"}})
            job.current_state = JobState.NETWORK_PAUSED
            job.error = CoreError.for_code(ErrorCode.NETWORK_PAUSED, cause="HTTP Error 429")
            job_file.write_text(job.to_json(), encoding="utf-8")
            result = runner.run(state, cancel_event=threading.Event(), on_started=lambda _pid: None)
        self.assertTrue(result.success, result.error)
        self.assertEqual([command[3] for command in commands], ["stage3-run", "finalize-job"])

    def test_core_runner_retries_transient_network_failure_inside_the_claim(self) -> None:
        runner = self._runner()
        state = self._state("network-retry-job")
        failures = {"stage3-run": 2}
        commands: list[str] = []

        def fake_popen(arguments, **kwargs):
            commands.append(arguments[3])
            if failures.get(arguments[3], 0) > 0:
                failures[arguments[3]] -= 1
                return _fail_fake_job(arguments, ErrorCode.NETWORK_PAUSED)
            return _advance_fake_job(arguments)

        from unittest.mock import patch

        with patch("src.queue.worker.subprocess.Popen", side_effect=fake_popen):
            result = runner.run(state, cancel_event=threading.Event(), on_started=lambda _pid: None)
        self.assertTrue(result.success, result.error)
        self.assertEqual(commands, ["video-run", "stage3-run", "stage3-run", "stage3-run", "finalize-job"])

    def test_core_runner_reports_network_pause_after_retries_are_exhausted(self) -> None:
        runner = self._runner(network_retry_delays=(0.0,))
        state = self._state("network-exhausted-job")
        from unittest.mock import patch

        def fake_popen(arguments, **kwargs):
            if arguments[3] == "stage3-run":
                return _fail_fake_job(arguments, ErrorCode.NETWORK_PAUSED)
            return _advance_fake_job(arguments)

        with patch("src.queue.worker.subprocess.Popen", side_effect=fake_popen):
            result = runner.run(state, cancel_event=threading.Event(), on_started=lambda _pid: None)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, ErrorCode.NETWORK_PAUSED)
        self.assertTrue(result.error.recoverable)

    def test_stage3_pause_becomes_a_recoverable_queue_error(self) -> None:
        runner = self._runner()
        state = self._state("stage3-paused-job")
        from unittest.mock import patch

        def fake_popen(arguments, **kwargs):
            if arguments[3] == "stage3-run":
                job_file = Path(arguments[arguments.index("--resume-job") + 1])
                job = Job.from_json(job_file.read_text(encoding="utf-8"))
                job.source_metadata["stage3"] = {"status": "paused_translation_resource_unavailable"}
                job_file.write_text(job.to_json(), encoding="utf-8")
                return _DoneProcess(3)
            return _advance_fake_job(arguments)

        with patch("src.queue.worker.subprocess.Popen", side_effect=fake_popen):
            result = runner.run(state, cancel_event=threading.Event(), on_started=lambda _pid: None)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, ErrorCode.API_FAILED)
        self.assertEqual(result.error.cause, "APPLE_TRANSLATION_RESOURCE_UNAVAILABLE")
        self.assertTrue(result.error.recoverable)

    def test_core_runner_stops_when_a_step_makes_no_progress(self) -> None:
        runner = self._runner()
        state = self._state("no-progress-job")
        from unittest.mock import patch

        with patch("src.queue.worker.subprocess.Popen", return_value=_DoneProcess(0)):
            result = runner.run(state, cancel_event=threading.Event(), on_started=lambda _pid: None)
        self.assertFalse(result.success)
        self.assertEqual(result.error.cause, "CORE_EXIT_0")

    @unittest.skipIf(os.name == "nt", "caffeinate is macOS only; the Windows worker comes with automatic monitoring")
    def test_core_runner_starts_and_releases_caffeinate_only_for_active_core(self) -> None:
        runner = self._runner(caffeinate_executable="caffeinate")
        state = self._state("stable-job-caffeinate")

        class PowerProcess:
            def __init__(self) -> None:
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        power_processes: list[PowerProcess] = []
        popen_calls = []

        def fake_popen(arguments, **kwargs):
            popen_calls.append(arguments)
            if arguments[0] == "/usr/bin/caffeinate":
                process = PowerProcess()
                power_processes.append(process)
                return process
            return _advance_fake_job(arguments)

        from unittest.mock import patch

        with patch("src.queue.worker.shutil.which", return_value="/usr/bin/caffeinate"), patch(
            "src.queue.worker.subprocess.Popen", side_effect=fake_popen
        ):
            result = runner.run(state, cancel_event=threading.Event(), on_started=lambda _pid: None)

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(popen_calls), 6)
        for index in (1, 3, 5):
            self.assertEqual(popen_calls[index], ["/usr/bin/caffeinate", "-i", "-w", "12345"])
        self.assertTrue(all(process.terminated for process in power_processes))

    def test_worker_notifies_outcomes_and_a_retry_streak_once(self) -> None:
        notices: list[tuple[str, str]] = []
        worker = QueueWorker(
            client=FakeQueueClient(),
            core_runner=FakeCoreRunner(),
            config=self.config,
            state_store=self.store,
            notifier=lambda title, message: notices.append((title, message)),
        )
        for _ in range(5):
            worker._observe(WorkerResult("RETRY", error_code="API_RESPONSE_INVALID"))
        worker._observe(WorkerResult("EMPTY"))
        worker._observe(WorkerResult("COMPLETED", "queue-001"))
        worker._observe(WorkerResult("PAUSED", "queue-002", error_code="NETWORK_PAUSED"))
        titles = [title for title, _ in notices]
        self.assertEqual(titles, ["UCI：连接云端 Queue 失败", "UCI：视频已完成", "UCI：任务已暂停"])
        self.assertIn("NETWORK_PAUSED", notices[2][1])

    def test_claim_retry_log_includes_http_status_and_body_summary(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("uci.queue.worker.detail-test")
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            error = QueueApiError("API_RESPONSE_INVALID", "Queue API returned invalid JSON.", http_status=200,
                                  detail="html:Error Service invoked too many times for one day")
            worker = QueueWorker(client=FakeQueueClient(claims=[error]), core_runner=FakeCoreRunner(),
                                 config=self.config, state_store=self.store, logger=logger)
            result = worker.poll_once()
        finally:
            logger.removeHandler(handler)
        self.assertEqual(result.status, "RETRY")
        self.assertIn("detail=http=200 html:Error Service invoked too many times", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
