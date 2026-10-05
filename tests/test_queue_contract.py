from __future__ import annotations

import unittest

from src.core.errors import CoreError, ErrorCode
from src.queue.contract import (
    QUEUE_FIELDS,
    QueueApiErrorCode,
    QueueApiResponse,
    QueueStatus,
    QueueTask,
    queue_status_for_error,
)


class QueueContractTests(unittest.TestCase):
    def test_formal_queue_statuses_and_schema_fields(self) -> None:
        self.assertEqual(
            [status.value for status in QueueStatus],
            ["PENDING", "CLAIMED", "PROCESSING", "PAUSED", "FAILED", "COMPLETED"],
        )
        self.assertEqual(
            QUEUE_FIELDS,
            (
                "queue_id",
                "video_id",
                "url",
                "status",
                "claim_token",
                "claimed_at",
                "lease_until",
                "attempts",
                "last_error",
                "local_job_id",
                "completed_at",
                "result_path",
                "claim_request_id",
                "selection_day",
                "selection_rank",
                "rank2_start_cutoff_at",
                "core_started_at",
            ),
        )

    def test_queue_fail_status_uses_stage1_error_recoverability(self) -> None:
        recoverable = {
            ErrorCode.AUTH_REQUIRED,
            ErrorCode.SUBTITLE_FAILED,
            ErrorCode.ASR_FAILED,
            ErrorCode.NETWORK_PAUSED,
            ErrorCode.PARTIAL_FAILURE,
            ErrorCode.CLEANUP_FAILED,
            ErrorCode.API_FAILED,
        }
        for code in ErrorCode:
            with self.subTest(code=code.value):
                expected = QueueStatus.PAUSED if code in recoverable else QueueStatus.FAILED
                self.assertEqual(queue_status_for_error(CoreError.for_code(code)), expected)

    def test_api_response_has_one_stable_envelope_shape(self) -> None:
        self.assertEqual(QueueApiResponse.success(None).to_dict(), {"ok": True, "data": None})
        self.assertEqual(
            QueueApiResponse.failure(QueueApiErrorCode.AUTH_FAILED, "Request authentication failed.").to_dict(),
            {
                "ok": False,
                "error": {"code": "AUTH_FAILED", "message": "Request authentication failed."},
            },
        )

    def test_queue_task_serializes_the_same_named_sheet_contract(self) -> None:
        task = QueueTask(queue_id="q-1", video_id="v-1", url="https://example.invalid/v-1")
        serialized = task.to_dict()
        self.assertEqual(tuple(serialized), QUEUE_FIELDS)
        self.assertEqual(serialized["status"], "PENDING")
        self.assertEqual(serialized["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
