from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.queue.client import QueueApiError
from src.queue.status_cli import build_report


def status(**overrides):
    base = {
        "healthy": True,
        "day": "2026-09-30",
        "checks": [{"name": "config", "ok": True}, {"name": "videos", "ok": True}, {"name": "snapshots", "ok": True}, {"name": "queue", "ok": True}],
        "config": {
            "production_timezone": "Asia/Shanghai", "daily_selection_enabled": True,
            "last_discovery_slot": "2026-09-30 03:50", "last_final_sweep_day": "2026-09-30", "last_daily_selection_day": "2026-09-30",
        },
        "videos": [{
            "creator_name": "Example Tech", "title": "Protests at OpenAI DevDay", "published_at": "2026-09-29T17:21:39.000Z",
            "lifecycle_state": "NORMAL", "selection_result": "", "snapshots": [
                {"snapshot_stage_minutes": 30, "view_count": 423, "baseline_final_views_median": 33750},
                {"snapshot_stage_minutes": 60, "view_count": 764, "baseline_final_views_median": 33750},
            ],
        }],
        "queue": [],
        "queue_total": 4,
    }
    base.update(overrides)
    return base


LAUNCHCTL_RUNNING = "PID\tStatus\tLabel\n54192\t0\tlocal.universal-content-intake.worker\n"


class StatusReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "worker.log"
        self.workspace = Path(self.tmp.name) / "content-intake"
        self.workspace.mkdir()
        patcher = patch("src.queue.status_cli.subprocess.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.return_value.stdout = LAUNCHCTL_RUNNING

    def write_log(self, *lines: str) -> None:
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def report(self, payload, now="2026-09-30T04:40:00+00:00", error=None):
        return build_report(payload, error, now=datetime.fromisoformat(now), day=None, log_path=self.log, workspace_root=self.workspace)

    def test_a_normal_zero_download_batch_is_healthy_and_explains_the_thresholds(self) -> None:
        self.write_log("2026-09-30 12:38:00,000 INFO queue_id=- local_job_id=- operation=claim state=empty attempt=None error_code=-")
        report = self.report(status())
        text = "\n".join(report.lines)
        self.assertEqual(report.problems, [])
        self.assertIn("结论：正常", text)
        self.assertIn("T+30 423 / 需 506", text)
        self.assertIn("2026-09-30 没有新的 Job", text)

    def test_the_2026_09_29_outage_is_reported_as_problems(self) -> None:
        self.write_log(*[
            f"2026-09-30 12:3{i}:00,000 INFO queue_id=- local_job_id=- operation=claim state=retry attempt=None error_code=INVALID_STATE detail=http=200 server:You do not have permission to call SpreadsheetApp.openById"
            for i in range(4)
        ])
        stale = status(config={**status()["config"], "last_final_sweep_day": "2026-09-29", "last_daily_selection_day": "2026-09-29"})
        report = self.report(stale)
        joined = "\n".join(report.problems)
        self.assertIn("08:10 补扫没有执行", joined)
        self.assertIn("10:00 选片没有执行", joined)
        self.assertIn("连续 4 次重试失败：http=200 server:You do not have permission", joined)

    def test_unreachable_cloud_and_failed_reads_are_problems(self) -> None:
        self.write_log("2026-09-30 12:38:00,000 INFO queue_id=- local_job_id=- operation=claim state=empty attempt=None error_code=-")
        report = self.report(None, error=QueueApiError("INVALID_STATE", "x", detail="server:permission denied"))
        self.assertIn("云端状态接口失败：INVALID_STATE（server:permission denied）", report.problems)
        broken = status(checks=[{"name": "snapshots", "ok": False, "error": "Missing required sheet Snapshots."}])
        self.assertIn("云端读取 snapshots 失败：Missing required sheet Snapshots.", self.report(broken).problems)

    def test_a_video_stuck_in_watch_and_a_paused_task_are_flagged(self) -> None:
        self.write_log("2026-09-30 12:38:00,000 INFO queue_id=- local_job_id=- operation=claim state=empty attempt=None error_code=-")
        stuck = status(
            videos=[{"creator_name": "Example Reviews", "title": "Stuck", "published_at": "2026-09-29T17:00:00Z", "lifecycle_state": "WATCH", "snapshots": []}],
            queue=[{"video_id": "v1", "status": "PAUSED", "selection_rank": 1, "attempts": 2, "last_error_code": "NETWORK_PAUSED"}],
        )
        joined = "\n".join(self.report(stuck).problems)
        self.assertIn("「Stuck」发布已超过 150 分钟仍在观察中", joined)
        self.assertIn("队列任务 v1 状态为 PAUSED（NETWORK_PAUSED）", joined)

    def test_new_jobs_are_listed_with_their_output(self) -> None:
        self.write_log("2026-09-30 12:38:00,000 INFO queue_id=- local_job_id=- operation=claim state=empty attempt=None error_code=-")
        job = self.workspace / "job-abc"
        (job / "output").mkdir(parents=True)
        (job / "job.json").write_text(json.dumps({"current_state": "COMPLETED"}), encoding="utf-8")
        (job / "output" / "final.mp4").write_bytes(b"0" * 2_000_000)
        (job / "output" / "info.md").write_text("- Publish Title: 星舰第十四次试飞\n", encoding="utf-8")
        text = "\n".join(self.report(status(), now=datetime.now(timezone.utc).isoformat()).lines)
        self.assertIn("job-abc｜COMPLETED｜2 MB｜发布标题：星舰第十四次试飞", text)


    def test_a_paused_monitor_is_reported_as_a_problem(self) -> None:
        self.write_log("2026-09-30 12:38:00,000 INFO queue_id=- local_job_id=- operation=claim state=empty attempt=None error_code=-")
        payload = status()
        payload["config"] = {**payload["config"], "monitor_status": "API_FAILED",
                             "last_api_error_class": "API_OR_APPS_SCRIPT_ERROR", "last_api_error_at": "2026-10-04T16:25:10Z"}
        report = self.report(payload)
        self.assertIn("监控已暂停", "\n".join(report.lines))
        self.assertTrue(any("monitor_status=API_FAILED" in problem for problem in report.problems))

    def test_past_day_with_later_markers_is_not_reported_as_missed(self) -> None:
        self.write_log("2026-09-30 12:38:00,000 INFO queue_id=- local_job_id=- operation=claim state=empty attempt=None error_code=-")
        payload = status(day="2026-09-29")
        report = self.report(payload, now="2026-09-30T04:40:00+00:00")
        self.assertFalse(any("没有执行" in problem for problem in report.problems), report.problems)


    def test_custom_cloud_schedule_drives_the_missed_run_checks(self) -> None:
        self.write_log("2026-09-30 12:38:00,000 INFO queue_id=- local_job_id=- operation=claim state=empty attempt=None error_code=-")
        config = {**status()["config"], "final_sweep_time": "'05:10", "daily_selection_time": "07:00",
                  "last_final_sweep_day": "2026-09-29", "last_daily_selection_day": "2026-09-29",
                  "cold_start_checkpoint_30_ratio": 0.02}
        # 07:10 Asia/Shanghai: the 05:10 sweep is overdue, the 07:00 selection is not yet.
        report = self.report(status(config=config), now="2026-09-29T23:10:00+00:00")
        joined = "\n".join(report.problems)
        self.assertIn("05:10 补扫没有执行", joined)
        self.assertNotIn("选片没有执行", joined)
        self.assertIn("T+30 423 / 需 675", "\n".join(report.lines))


if __name__ == "__main__":
    unittest.main()
