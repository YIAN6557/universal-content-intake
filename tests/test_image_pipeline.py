from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from src import cli
from src.core.job import ContentType, Job, JobState
from src.output.paths import OutputWorkspace
from src.providers.base import ContentProvider, ProviderCapability, ProviderFailure, ProviderFailureKind, ProviderResult
from src.core.image_pipeline import run_image_pipeline


def make_job(root: Path, declared: ContentType | None = None) -> tuple[Job, Path]:
    workspace = OutputWorkspace.for_job(root, "image-core-test")
    paths = workspace.prepare()
    job = Job(
        job_id="image-core-test",
        source_url="https://images.example.test/set/one",
        declared_content_type=declared,
        resolved_content_type=declared or ContentType.UNKNOWN,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
    )
    job_file = paths.job_dir / "job.json"
    job_file.write_text(job.to_json(), encoding="utf-8")
    return job, job_file


class PartialProvider(ContentProvider):
    name = "partial-image-fixture"
    capability = ProviderCapability(frozenset({ContentType.IMAGE, ContentType.IMAGE_SET}), True, True, True)

    def __init__(self) -> None:
        self.probe_count = 0
        self.fetch_count = 0

    def probe(self, job: Job) -> ProviderResult:
        self.probe_count += 1
        return ProviderResult(metadata={"probe": {"content_type": "IMAGE_SET", "item_count": 2}}, resume_token="image-source")

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        self.fetch_count += 1
        if self.fetch_count == 1:
            raise ProviderFailure(
                ProviderFailureKind.PARTIAL,
                self.name,
                "one image failed",
                resume_token=resume_token,
                details={"image_partial": {"expected_count": 2, "successful_items": [{"source_order": 1, "sha256": "abc"}], "failed_items": [{"source_order": 2, "reason": "timeout"}]}},
            )
        return ProviderResult(metadata={"probe": {"content_type": "IMAGE_SET", "item_count": 2}, "fetch": {"reused_count": 1, "downloaded_count": 1}}, resume_token=resume_token)


class OneImageProvider(ContentProvider):
    name = "single-image-fixture"
    capability = ProviderCapability(frozenset({ContentType.IMAGE, ContentType.IMAGE_SET}), True, True, True)

    def probe(self, job: Job) -> ProviderResult:
        return ProviderResult(metadata={"probe": {"content_type": "IMAGE", "item_count": 1}}, resume_token="single-image")

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        return ProviderResult(metadata={"probe": {"content_type": "IMAGE", "item_count": 1}, "fetch": {"successful_count": 1}}, resume_token=resume_token)


class ErrorProvider(ContentProvider):
    name = "image-error-fixture"
    capability = ProviderCapability(frozenset({ContentType.IMAGE, ContentType.IMAGE_SET}), True, True, True)

    def __init__(self, kind: ProviderFailureKind) -> None:
        self.kind = kind

    def probe(self, job: Job) -> ProviderResult:
        raise ProviderFailure(self.kind, self.name, f"fixture {self.kind.value.lower()} failure")

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        raise AssertionError("fetch must not run after Probe failure")


class ImagePipelineTests(unittest.TestCase):
    def test_core_resolves_type_from_probe_and_keeps_provider_state_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, job_file = make_job(Path(directory))

            outcome = run_image_pipeline(job, OneImageProvider(), job_file=job_file)

            self.assertTrue(outcome.success)
            self.assertEqual(outcome.job.resolved_content_type, ContentType.IMAGE)
            self.assertEqual(outcome.job.current_state, JobState.DOWNLOADING)
            self.assertEqual(outcome.job_file, job_file)

    def test_explicit_type_mismatch_fails_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, job_file = make_job(Path(directory), ContentType.IMAGE)
            provider = PartialProvider()

            outcome = run_image_pipeline(job, provider, job_file=job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.job.current_state, JobState.PROVIDER_FAILED)
            self.assertEqual(outcome.error.code.value, "PROVIDER_FAILED")
            self.assertEqual(provider.fetch_count, 0)

    def test_partial_failure_is_persisted_and_same_job_resume_reprobes_without_losing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job, job_file = make_job(Path(directory), ContentType.IMAGE_SET)
            provider = PartialProvider()

            first = run_image_pipeline(job, provider, job_file=job_file)

            self.assertFalse(first.success)
            self.assertEqual(first.job.current_state, JobState.PARTIAL_FAILURE)
            partial_metadata = first.job.provider_metadata(provider.name)["partial"]
            self.assertEqual(partial_metadata["expected_count"], 2)
            self.assertEqual(len(partial_metadata["successful_items"]), 1)

            resumed = run_image_pipeline(first.job, provider, job_file=job_file)

            self.assertTrue(resumed.success)
            self.assertEqual(resumed.job.current_state, JobState.DOWNLOADING)
            self.assertEqual(resumed.job.attempts.count, 1)
            self.assertEqual(provider.probe_count, 2)
            self.assertEqual(provider.fetch_count, 2)
            self.assertEqual(resumed.result.metadata["fetch"]["reused_count"], 1)

    def test_provider_failures_map_to_stable_core_states(self) -> None:
        cases = (
            (ProviderFailureKind.INVALID_URL, JobState.URL_INVALID),
            (ProviderFailureKind.AUTH_REQUIRED, JobState.AUTH_REQUIRED),
            (ProviderFailureKind.NETWORK, JobState.NETWORK_PAUSED),
            (ProviderFailureKind.UNSUPPORTED, JobState.PROVIDER_FAILED),
            (ProviderFailureKind.FAILED, JobState.PROVIDER_FAILED),
        )
        for kind, expected_state in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as directory:
                    job, job_file = make_job(Path(directory))

                    outcome = run_image_pipeline(job, ErrorProvider(kind), job_file=job_file)

                self.assertFalse(outcome.success)
                self.assertEqual(outcome.job.current_state, expected_state)
                self.assertEqual(outcome.error.state, expected_state.value)

    def test_image_cli_invalid_url_uses_stable_url_invalid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main([
                    "image-run",
                    "--url", "file:///private/tmp/not-an-image",
                    "--workspace-root", directory,
                    "--job-id", "invalid-image-url",
                ])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["job"]["current_state"], "URL_INVALID")
        self.assertEqual(payload["error"]["code"], "URL_INVALID")


if __name__ == "__main__":
    unittest.main()
