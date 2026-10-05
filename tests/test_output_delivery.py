from __future__ import annotations

import os

os.environ["UCI_PUBLISH_WRITER"] = "off"  # never call an LLM API (Claude or Gemini) from tests

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from src.core.job import ContentType, Job, JobState
from src.core.output_pipeline import run_output_pipeline
from src.output.delivery import DELIVERY_RECORD_NAME, deliver_completed_videos, deliver_job, load_delivery_record
from src.queue.worker import QueueWorker, SubprocessCoreRunner, WorkerConfig, WorkerState, WorkerStateStore
from tests.test_stage5_output_pipeline import _artifact, _job


PAYLOAD = b"validated-final-video"


def _completed_video(root: Path, job_id: str = "delivery-test", title: str = "Hello: World/Test") -> Job:
    job, job_file = _job(root, ContentType.VIDEO, job_id)
    record = _artifact(job, "stage3/rendered.mp4", PAYLOAD)
    record["size_bytes"] = record.pop("size")
    job.source_metadata["providers"] = {"yt-dlp": {"probe": {"title": title, "original_language": "en"}}}
    job.source_metadata["stage3"] = {
        "status": "validated",
        "validated_artifact": {**record, "role": "STAGE3_VALIDATED_RENDERED_ARTIFACT"},
        "render": {"status": "validated"},
    }
    job_file.write_text(job.to_json(), encoding="utf-8")
    outcome = run_output_pipeline(job, job_file=job_file)
    assert outcome.success, outcome.error
    return outcome.job


class OutputDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "content-intake"
        self.workspace.mkdir()
        self.delivery = Path(self.tmp.name) / "Movies" / "Delivery"

    def test_completed_video_is_moved_with_dated_title_and_job_still_verifies(self) -> None:
        job = _completed_video(self.workspace)
        job_dir = Path(job.workspace_path)

        outcomes = deliver_completed_videos(self.workspace, self.delivery)

        self.assertEqual([item.status for item in outcomes], ["delivered"])
        destination = outcomes[0].destination
        self.assertRegex(destination.name, r"^\d{4}-\d{2}-\d{2} Hello World Test\.mp4$")
        self.assertEqual(destination.read_bytes(), PAYLOAD)
        self.assertFalse((job_dir / "output" / "final.mp4").exists())
        self.assertTrue((job_dir / "output" / "info.md").exists())
        info = destination.with_suffix(".md")
        sheet = info.read_text(encoding="utf-8")
        self.assertTrue(sheet.startswith("# 发布标题"))
        self.assertIn("- 原标题：Hello: World/Test", sheet)
        self.assertIn("- 原视频链接：https://example.test/source", sheet)
        self.assertEqual(load_delivery_record(job_dir)["status"], "delivered")
        # Re-running finalize on the COMPLETED Job accepts the handed-off video.
        again = run_output_pipeline(Job.from_json((job_dir / "job.json").read_text(encoding="utf-8")), job_file=job_dir / "job.json")
        self.assertTrue(again.success, again.error)
        self.assertEqual(deliver_job(job_dir, self.delivery).status, "already_delivered")
        # Deleting the delivered files is the user's business, not a Job failure.
        destination.unlink()
        info.unlink()
        again = run_output_pipeline(Job.from_json((job_dir / "job.json").read_text(encoding="utf-8")), job_file=job_dir / "job.json")
        self.assertTrue(again.success, again.error)

    def test_name_collision_never_overwrites(self) -> None:
        first = _completed_video(self.workspace, "delivery-one", title="Same")
        second = _completed_video(self.workspace, "delivery-two", title="Same")

        outcomes = deliver_completed_videos(self.workspace, self.delivery)

        names = sorted(item.destination.name for item in outcomes)
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].endswith("Same (2).mp4") or names[1].endswith("Same (2).mp4"))
        self.assertEqual(len(list(self.delivery.glob("*Same*.md"))), 2)
        self.assertTrue(all((self.delivery / name).read_bytes() == PAYLOAD for name in names))
        self.assertFalse((Path(first.output_path) / "final.mp4").exists())
        self.assertFalse((Path(second.output_path) / "final.mp4").exists())

    def test_interrupted_move_is_finished_on_the_next_run(self) -> None:
        job = _completed_video(self.workspace)
        job_dir = Path(job.workspace_path)
        source = job_dir / "output" / "final.mp4"
        self.delivery.mkdir(parents=True)
        destination = self.delivery / "2026-10-02 interrupted.mp4"
        manifest = json.loads((job_dir / ".stage5-manifest.json").read_text(encoding="utf-8"))
        artifact = manifest["artifacts"][0]
        (job_dir / DELIVERY_RECORD_NAME).write_text(json.dumps({
            "schema_version": 1, "job_id": job.job_id, "status": "moving",
            "artifacts": [{"output_path": "final.mp4", "sha256": artifact["sha256"], "size": artifact["size"],
                           "destination": str(destination)}],
        }), encoding="utf-8")
        os.link(source, destination)  # crashed after linking, before unlinking the source

        outcome = deliver_job(job_dir, self.delivery)

        self.assertEqual(outcome.status, "delivered")
        self.assertEqual(outcome.destination, destination)
        self.assertFalse(source.exists())
        self.assertEqual(sorted(path.name for path in self.delivery.iterdir()),
                         ["2026-10-02 interrupted.md", "2026-10-02 interrupted.mp4"])
        self.assertTrue(run_output_pipeline(Job.from_json((job_dir / "job.json").read_text(encoding="utf-8")),
                                            job_file=job_dir / "job.json").success)

    def test_publish_sheet_is_not_added_after_the_user_removed_the_video(self) -> None:
        job = _completed_video(self.workspace)
        job_dir = Path(job.workspace_path)
        destination = deliver_job(job_dir, self.delivery).destination
        record = load_delivery_record(job_dir)
        record.pop("publish_sheet")
        (job_dir / DELIVERY_RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
        destination.with_suffix(".md").unlink()
        destination.unlink()

        self.assertEqual(deliver_job(job_dir, self.delivery).status, "already_delivered")
        self.assertEqual(list(self.delivery.iterdir()), [])

    def test_tampered_or_unfinished_jobs_are_not_moved(self) -> None:
        job = _completed_video(self.workspace)
        final = Path(job.output_path) / "final.mp4"
        final.write_bytes(b"tampered")
        unfinished, unfinished_file = _job(self.workspace, ContentType.VIDEO, "still-downloading")

        outcomes = {item.job_id: item for item in deliver_completed_videos(self.workspace, self.delivery)}

        self.assertEqual(outcomes["delivery-test"].status, "failed")
        self.assertEqual(outcomes["still-downloading"].status, "skipped")
        self.assertTrue(final.exists())
        self.assertFalse(self.delivery.exists() and any(self.delivery.iterdir()))

    def test_worker_reports_the_delivered_path_for_an_already_delivered_job(self) -> None:
        job = _completed_video(self.workspace)
        destination = deliver_job(Path(job.workspace_path), self.delivery).destination
        runner = SubprocessCoreRunner(
            project_root=Path(__file__).resolve().parents[1],
            workspace_root=self.workspace,
            python_executable=Path("/usr/bin/python3"),
            caffeinate_executable=None,
        )
        state = WorkerState(
            worker_state="PROCESSING", claim_request_id="request-1", queue_id="queue-1", claim_token="token-1",
            lease_until="2099-01-01T00:00:00Z", local_job_id=job.job_id, url=job.source_url,
        )

        result = runner.run(state, cancel_event=threading.Event(), on_started=lambda _pid: None)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.result_path, str(destination))
        self.assertIs(Job.from_json((Path(job.workspace_path) / "job.json").read_text(encoding="utf-8")).current_state, JobState.COMPLETED)


    def test_idle_worker_sweep_delivers_but_waits_while_a_task_is_active(self) -> None:
        job = _completed_video(self.workspace)
        state_dir = Path(self.tmp.name) / "worker"
        config = WorkerConfig(queue_poll_seconds=120, queue_heartbeat_seconds=300, queue_lease_seconds=900,
                              state_dir=state_dir, workspace_root=self.workspace, delivery_root=self.delivery)
        store = WorkerStateStore(state_dir)
        notes: list[tuple[str, str]] = []
        worker = QueueWorker(client=None, core_runner=None, config=config, state_store=store,
                             notifier=lambda title, message: notes.append((title, message)))
        store.save(WorkerState(
            worker_state="PROCESSING", claim_request_id="request-1", queue_id="queue-1", claim_token="token-1",
            lease_until="2099-01-01T00:00:00Z", local_job_id="other", url="https://example.test/other",
        ))

        worker.deliver_videos()
        self.assertTrue((Path(job.output_path) / "final.mp4").exists())

        store.clear()
        worker.deliver_videos()
        self.assertFalse((Path(job.output_path) / "final.mp4").exists())
        self.assertEqual(len(list(self.delivery.glob("*.mp4"))), 1)
        self.assertEqual(notes[0][0], "UCI：视频已保存")


if __name__ == "__main__":
    unittest.main()
