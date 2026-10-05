from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from src import cli
from src.core.job import ContentType, Job, JobState
from src.core.video_pipeline import run_video_pipeline
from src.output.paths import OutputWorkspace
from src.providers.base import ContentProvider, ProducedArtifact, ProviderCapability, ProviderFailure, ProviderFailureKind, ProviderResult


def make_job(root: Path) -> tuple[Job, Path]:
    workspace = OutputWorkspace.for_job(root, "pipeline-test")
    paths = workspace.prepare()
    job = Job(
        job_id="pipeline-test",
        source_url="https://example.com/video",
        declared_content_type=ContentType.VIDEO,
        resolved_content_type=ContentType.VIDEO,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
    )
    job_file = paths.job_dir / "job.json"
    job_file.write_text(job.to_json(), encoding="utf-8")
    return job, job_file


class RetryOnceProvider(ContentProvider):
    name = "retry-once"
    capability = ProviderCapability(frozenset({ContentType.VIDEO}), True, True, True)

    def __init__(self) -> None:
        self.probe_count = 0
        self.fetch_count = 0

    def probe(self, job: Job) -> ProviderResult:
        self.probe_count += 1
        return ProviderResult(metadata={"probe": {"video_id": "fixture"}, "selection": {"selector": "1+2"}}, resume_token="durable-token")

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        self.fetch_count += 1
        if self.fetch_count == 1:
            raise ProviderFailure(ProviderFailureKind.NETWORK, self.name, "network interruption", resume_token=resume_token)
        return ProviderResult(
            metadata={"probe": {"video_id": "fixture"}, "fetch": {"verified": True}},
            artifacts=(ProducedArtifact("temp/source_video/source_video.mp4", "SOURCE_VIDEO", "video/mp4"),),
            resume_token=resume_token,
        )


class AuthFailureProvider(ContentProvider):
    name = "auth-failure"
    capability = ProviderCapability(frozenset({ContentType.VIDEO}), True, True, True)

    def probe(self, job: Job) -> ProviderResult:
        raise ProviderFailure(
            ProviderFailureKind.AUTH_REQUIRED,
            self.name,
            "anonymous probe and Chrome retry both require authorization",
            details={"authentication": {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": False,
                "auth_result": "failure",
                "cookie_value": "must-not-persist",
            }},
        )

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        raise AssertionError("fetch must not run after AUTH_REQUIRED Probe failure")


class VideoPipelineTests(unittest.TestCase):
    def test_core_persists_only_allowlisted_auth_outcome_and_keeps_auth_required_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, job_file = make_job(Path(directory))

            outcome = run_video_pipeline(job, AuthFailureProvider(), job_file=job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.job.current_state, JobState.AUTH_REQUIRED)
            self.assertEqual(outcome.job.error.code.value, "AUTH_REQUIRED")
            metadata = Job.from_json(job_file.read_text(encoding="utf-8")).provider_metadata("auth-failure")
            self.assertEqual(metadata, {"authentication": {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": False,
                "auth_result": "failure",
            }})
            self.assertNotIn("must-not-persist", job_file.read_text(encoding="utf-8"))

    def test_network_pause_is_persisted_and_resume_reuses_probe_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, job_file = make_job(Path(directory))
            provider = RetryOnceProvider()
            paused = run_video_pipeline(job, provider, job_file=job_file)
            self.assertFalse(paused.success)
            self.assertEqual(paused.job.current_state, JobState.NETWORK_PAUSED)
            stored = Job.from_json(job_file.read_text(encoding="utf-8"))
            provider_state = stored.attempts.checkpoint["providers"][provider.name]
            self.assertEqual(provider_state["resume_token"], "durable-token")
            self.assertEqual(stored.error.code.value, "NETWORK_PAUSED")
            self.assertEqual(stored.error.retry.resume_from_state, "DOWNLOADING")

            resumed = run_video_pipeline(stored, provider, job_file=job_file)
            self.assertTrue(resumed.success)
            self.assertEqual(resumed.job.current_state, JobState.DOWNLOADING)
            self.assertEqual(resumed.job.attempts.count, 1)
            self.assertEqual(provider.probe_count, 1)
            self.assertEqual(provider.fetch_count, 2)
            final = Job.from_json(job_file.read_text(encoding="utf-8"))
            self.assertEqual(final.provider_metadata(provider.name)["fetch"], {"verified": True})
            self.assertEqual(final.current_state, JobState.DOWNLOADING)

    def test_invalid_source_maps_to_stable_cli_error_without_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main([
                    "video-run",
                    "--url", "file:///private/tmp/not-a-video",
                    "--workspace-root", directory,
                    "--job-id", "invalid-url-test",
                ])
            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue(), "")
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["error"]["code"], "URL_INVALID")
            self.assertEqual(output["job"]["current_state"], "URL_INVALID")
            self.assertNotIn("Traceback", stdout.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
