from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from src import cli
from src.core.errors import CoreError, ErrorCode, map_provider_failure
from src.core.job import AttemptMetadata, ContentType, Job, JobState, OutputResult
from src.core.policies import PolicyLoadError, load_default_policies
from src.core.router import ResolutionOrigin, provider_failure_for_locked_type, resolve_content_type, unknown_content_error
from src.core.state_machine import RECOVERABLE_ERROR_STATES, StateMachine, StateTransitionError
from src.output.paths import InfoRecord, OutputContractError, OutputWorkspace
from src.providers.base import (
    ContentProvider,
    ProducedArtifact,
    ProviderCapability,
    ProviderFailure,
    ProviderFailureKind,
    ProviderResult,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = PROJECT_ROOT / "config" / "defaults.yaml"


def make_job(tmp_path: Path, *, declared: ContentType | None = ContentType.VIDEO) -> Job:
    workspace = OutputWorkspace.for_job(tmp_path, "contract-test")
    paths = workspace.prepare()
    resolution = resolve_content_type(declared=declared)
    return Job(
        job_id="contract-test",
        source_url="https://example.invalid/resource",
        declared_content_type=declared,
        resolved_content_type=resolution.content_type,
        attempts=AttemptMetadata(checkpoint={"bytes": 12}),
        source_metadata={"title": "测试来源"},
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        output_result=OutputResult(content_paths=("final.mp4",), info_path="info.md"),
    )


class JobContractTests(unittest.TestCase):
    def test_job_json_round_trip_preserves_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = make_job(Path(directory))
            restored = Job.from_json(original.to_json())
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored.schema_version, 1)
        self.assertEqual(restored.attempts.checkpoint, {"bytes": 12})

    def test_job_preserves_user_requested_options_and_namespaced_provider_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            job.requested_options = {"video": {"quality": "2160p"}}
            job.store_provider_metadata("yt-dlp", {"video_id": "sample-id"})
            restored = Job.from_json(job.to_json())
        self.assertEqual(restored.requested_options["video"]["quality"], "2160p")
        self.assertEqual(restored.provider_metadata("yt-dlp"), {"video_id": "sample-id"})

    def test_job_rejects_non_json_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = OutputWorkspace.for_job(directory, "unsafe-meta")
            paths = workspace.prepare()
            with self.assertRaisesRegex(ValueError, "JSON-serializable"):
                Job(
                    job_id="unsafe-meta",
                    source_url="https://example.invalid",
                    declared_content_type=None,
                    workspace_path=str(paths.job_dir),
                    temp_path=str(paths.temp_dir),
                    output_path=str(paths.output_dir),
                    source_metadata={"set": {"not-json"}},
                )

    def test_job_rejects_unknown_as_declared_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = OutputWorkspace.for_job(directory, "declared-unknown")
            paths = workspace.prepare()
            with self.assertRaisesRegex(ValueError, "declared"):
                Job(
                    job_id="declared-unknown",
                    source_url="https://example.invalid",
                    declared_content_type=ContentType.UNKNOWN,
                    workspace_path=str(paths.job_dir),
                    temp_path=str(paths.temp_dir),
                    output_path=str(paths.output_dir),
                )

    def test_declared_type_cannot_be_silently_re_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = OutputWorkspace.for_job(directory, "locked-type")
            paths = workspace.prepare()
            with self.assertRaisesRegex(ValueError, "declared content type"):
                Job(
                    job_id="locked-type",
                    source_url="https://example.invalid",
                    declared_content_type=ContentType.VIDEO,
                    resolved_content_type=ContentType.IMAGE,
                    workspace_path=str(paths.job_dir),
                    temp_path=str(paths.temp_dir),
                    output_path=str(paths.output_dir),
                )


class ContentTypeTests(unittest.TestCase):
    def test_declared_type_has_priority_and_is_locked(self) -> None:
        result = resolve_content_type(declared=ContentType.VIDEO, platform=ContentType.IMAGE, probe=ContentType.ARTICLE)
        self.assertEqual(result.content_type, ContentType.VIDEO)
        self.assertEqual(result.origin, ResolutionOrigin.DECLARED)
        self.assertTrue(result.declared_locked)

    def test_platform_type_precedes_probe(self) -> None:
        result = resolve_content_type(platform=ContentType.IMAGE_SET, probe=ContentType.IMAGE)
        self.assertEqual(result.content_type, ContentType.IMAGE_SET)
        self.assertEqual(result.origin, ResolutionOrigin.PLATFORM)

    def test_probe_type_is_used_when_higher_priorities_absent(self) -> None:
        result = resolve_content_type(probe=ContentType.DOCUMENT)
        self.assertEqual(result.content_type, ContentType.DOCUMENT)
        self.assertEqual(result.origin, ResolutionOrigin.PROBE)

    def test_unknown_resolution_maps_to_unknown_content(self) -> None:
        result = resolve_content_type()
        self.assertEqual(result.content_type, ContentType.UNKNOWN)
        self.assertEqual(result.state, JobState.UNKNOWN_CONTENT)
        self.assertEqual(unknown_content_error().code, ErrorCode.UNKNOWN_CONTENT)

    def test_locked_type_provider_failure_does_not_reroute(self) -> None:
        error = provider_failure_for_locked_type("fake-video", "not supported")
        self.assertEqual(error.code, ErrorCode.PROVIDER_FAILED)
        self.assertEqual(error.provider, "fake-video")


class StateMachineTests(unittest.TestCase):
    def _advance_to(self, job: Job, state: JobState) -> None:
        StateMachine.transition(job, state)

    def test_legal_minimal_path_skips_optional_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            for state in (
                JobState.CLAIMED,
                JobState.PROBING,
                JobState.DOWNLOADING,
                JobState.DOCUMENTING,
                JobState.CLEANING,
                JobState.COMPLETED,
            ):
                self._advance_to(job, state)
        self.assertEqual(job.current_state, JobState.COMPLETED)
        self.assertTrue(StateMachine.is_terminal(job.current_state))

    def test_legal_optional_processing_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            for state in (
                JobState.CLAIMED, JobState.PROBING, JobState.DOWNLOADING,
                JobState.TRANSCRIBING, JobState.TRANSLATING, JobState.RENDERING,
                JobState.DOCUMENTING, JobState.CLEANING, JobState.COMPLETED,
            ):
                self._advance_to(job, state)
        self.assertEqual(job.current_state, JobState.COMPLETED)

    def test_unknown_content_is_an_explicit_probe_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory), declared=None)
            self._advance_to(job, JobState.CLAIMED)
            self._advance_to(job, JobState.PROBING)
            StateMachine.apply_error(job, unknown_content_error())
        self.assertEqual(job.current_state, JobState.UNKNOWN_CONTENT)
        self.assertTrue(StateMachine.is_terminal(job.current_state))

    def test_invalid_transition_has_deterministic_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            with self.assertRaisesRegex(StateTransitionError, r"invalid transition: QUEUED -> COMPLETED"):
                self._advance_to(job, JobState.COMPLETED)

    def test_documenting_requires_a_download_or_fetch_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            self._advance_to(job, JobState.CLAIMED)
            self._advance_to(job, JobState.PROBING)
            with self.assertRaisesRegex(StateTransitionError, r"PROBING -> DOCUMENTING"):
                self._advance_to(job, JobState.DOCUMENTING)

    def test_cleaning_requires_documenting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            self._advance_to(job, JobState.CLAIMED)
            self._advance_to(job, JobState.PROBING)
            self._advance_to(job, JobState.DOWNLOADING)
            with self.assertRaisesRegex(StateTransitionError, r"DOWNLOADING -> CLEANING"):
                self._advance_to(job, JobState.CLEANING)

    def test_cleanup_failed_can_never_become_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            for state in (JobState.CLAIMED, JobState.PROBING, JobState.DOWNLOADING, JobState.DOCUMENTING, JobState.CLEANING):
                self._advance_to(job, state)
            StateMachine.apply_error(job, CoreError.for_code(ErrorCode.CLEANUP_FAILED))
            with self.assertRaisesRegex(StateTransitionError, r"CLEANUP_FAILED -> COMPLETED"):
                self._advance_to(job, JobState.COMPLETED)
            with self.assertRaisesRegex(StateTransitionError, "cleanup failures may only resume cleanup"):
                StateMachine.resume(job)
            with self.assertRaisesRegex(StateTransitionError, "cleanup failures may only resume cleanup"):
                StateMachine.resume(job, JobState.DOWNLOADING)
            StateMachine.resume(job, JobState.CLEANING)
            self.assertEqual(job.current_state, JobState.CLEANING)
        self.assertFalse(StateMachine.is_terminal(JobState.CLEANUP_FAILED))

    def test_terminal_completed_cannot_return_to_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            for state in (JobState.CLAIMED, JobState.PROBING, JobState.DOWNLOADING, JobState.DOCUMENTING, JobState.CLEANING, JobState.COMPLETED):
                self._advance_to(job, state)
            with self.assertRaisesRegex(StateTransitionError, r"COMPLETED -> PROBING"):
                self._advance_to(job, JobState.PROBING)

    def test_recoverable_network_error_requires_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            self._advance_to(job, JobState.CLAIMED)
            self._advance_to(job, JobState.PROBING)
            StateMachine.apply_error(job, CoreError.for_code(ErrorCode.NETWORK_PAUSED))
            self.assertIn(job.current_state, RECOVERABLE_ERROR_STATES)
            with self.assertRaises(StateTransitionError):
                self._advance_to(job, JobState.DOWNLOADING)
            StateMachine.resume(job)
        self.assertEqual(job.current_state, JobState.PROBING)
        self.assertIsNone(job.error)
        self.assertEqual(job.attempts.count, 1)

    def test_partial_failure_can_resume_from_its_original_processing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            self._advance_to(job, JobState.CLAIMED)
            self._advance_to(job, JobState.PROBING)
            self._advance_to(job, JobState.DOWNLOADING)
            StateMachine.apply_error(job, CoreError.for_code(ErrorCode.PARTIAL_FAILURE))
            self.assertIn(JobState.PARTIAL_FAILURE, RECOVERABLE_ERROR_STATES)
            StateMachine.resume(job)
        self.assertEqual(job.current_state, JobState.DOWNLOADING)
        self.assertIsNone(job.error)


class PolicyTests(unittest.TestCase):
    def test_canonical_defaults_load_from_yaml(self) -> None:
        policy = load_default_policies(DEFAULTS)
        self.assertEqual(policy.target_translation_language, "zh-Hans")
        self.assertEqual(policy.video_target_resolution, "1080p")
        self.assertEqual(policy.video_container, "mp4")
        self.assertEqual(policy.video_preferred_video_codec, "h264")
        self.assertEqual(policy.video_preferred_audio_codec, "aac")
        self.assertEqual(policy.audio_output, "mp3")
        self.assertEqual(policy.image_quality, "highest_available")
        self.assertTrue(policy.preserve_source_aspect_ratio)
        self.assertEqual(policy.file_conflict, "deterministic_rename")
        self.assertFalse(policy.archive_by_default)
        self.assertFalse(policy.unnecessary_user_interruption)

    def test_invalid_archive_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bad.yaml"
            config.write_text(DEFAULTS.read_text(encoding="utf-8").replace("archive_by_default: false", "archive_by_default: true"), encoding="utf-8")
            with self.assertRaisesRegex(PolicyLoadError, "archive"):
                load_default_policies(config)


class OutputContractTests(unittest.TestCase):
    def test_workspace_paths_are_canonical_and_contained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = OutputWorkspace.for_job(directory, "safe-id")
            paths = workspace.prepare()
            self.assertTrue(paths.temp_dir.is_dir())
            self.assertTrue(paths.output_dir.is_dir())
            self.assertEqual(workspace.output_path("content/image-001.jpg"), paths.output_dir / "content" / "image-001.jpg")
            self.assertEqual(workspace.temp_path("metadata.json"), paths.temp_dir / "metadata.json")

    def test_path_traversal_and_absolute_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = OutputWorkspace.for_job(directory, "safe-id")
            workspace.prepare()
            for unsafe in ("../escape.mp4", "/tmp/escape.mp4", "content/../../escape.mp4"):
                with self.subTest(unsafe=unsafe), self.assertRaises(OutputContractError):
                    workspace.output_path(unsafe)

    def test_content_deliverable_can_be_a_file_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = OutputWorkspace.for_job(directory, "image-set")
            workspace.prepare()
            deliverable = workspace.plan_content_deliverable(["content/001.jpg", "content/002.jpg"])
        self.assertEqual(deliverable.relative_paths, ("content/001.jpg", "content/002.jpg"))

    def test_info_record_has_fixed_formal_fields_without_writing_output(self) -> None:
        record = InfoRecord(
            content_type="DOCUMENT",
            platform="Direct HTTP",
            source_url="https://example.invalid/report.pdf",
            document_summary_zh="摘要",
        )
        rendered = record.to_markdown()
        self.assertIn("- Type: DOCUMENT", rendered)
        self.assertIn("- Source URL: https://example.invalid/report.pdf", rendered)
        self.assertIn("- Document Summary zh-Hans: 摘要", rendered)

    def test_info_record_rejects_overlong_document_summary(self) -> None:
        with self.assertRaisesRegex(OutputContractError, "500"):
            InfoRecord(content_type="DOCUMENT", platform=None, source_url="https://example.invalid", document_summary_zh="字" * 501)

    def test_deterministic_collision_rename_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = OutputWorkspace.for_job(directory, "collision")
            paths = workspace.prepare()
            (paths.output_dir / "final.mp4").write_bytes(b"existing")
            self.assertEqual(workspace.deterministic_name("final.mp4").name, "final-2.mp4")
            (paths.output_dir / "final-2.mp4").write_bytes(b"existing")
            self.assertEqual(workspace.deterministic_name("final.mp4").name, "final-3.mp4")
            self.assertEqual((paths.output_dir / "final.mp4").read_bytes(), b"existing")

    def test_existing_job_workspace_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            OutputWorkspace.for_job(directory, "same").prepare()
            with self.assertRaisesRegex(OutputContractError, "already exists"):
                OutputWorkspace.for_job(directory, "same").prepare()


class FakeProvider(ContentProvider):
    name = "fake-provider"
    capability = ProviderCapability(
        content_types=frozenset({ContentType.VIDEO}),
        supports_probe=True,
        supports_fetch=True,
        supports_resume=True,
    )

    def probe(self, job: Job) -> ProviderResult:
        return ProviderResult(metadata={"provider": self.name, "source_url": job.source_url})

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        return ProviderResult(
            metadata={"resume_token": resume_token},
            artifacts=(ProducedArtifact("final.mp4", "content", "video/mp4"),),
            resume_token="checkpoint-2",
        )


class ProviderContractTests(unittest.TestCase):
    def test_fake_provider_implements_structured_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory))
            provider = FakeProvider()
            probe = provider.probe(job)
            fetched = provider.fetch(job, resume_token="checkpoint-1")
        self.assertTrue(provider.capability.supports_resume)
        self.assertEqual(probe.metadata["provider"], "fake-provider")
        self.assertEqual(fetched.artifacts[0].relative_path, "final.mp4")
        self.assertEqual(fetched.resume_token, "checkpoint-2")
        self.assertEqual(job.current_state, JobState.QUEUED)

    def test_provider_failure_has_no_job_state_side_effect(self) -> None:
        failure = ProviderFailure(ProviderFailureKind.NETWORK, "fake-provider", "connection reset", 30)
        error = map_provider_failure(
            failure.kind.value,
            provider=failure.provider,
            cause=str(failure),
            retry_after_seconds=failure.retry_after_seconds,
        )
        self.assertEqual(error.code, ErrorCode.NETWORK_PAUSED)
        self.assertTrue(error.recoverable)
        self.assertEqual(error.provider, "fake-provider")
        self.assertEqual(error.retry.retry_after_seconds, 30)

    def test_structured_error_mapping_separates_terminal_and_recoverable(self) -> None:
        recoverable = CoreError.for_code(ErrorCode.AUTH_REQUIRED)
        terminal = CoreError.for_code(ErrorCode.PROVIDER_FAILED)
        partial = CoreError.for_code(ErrorCode.PARTIAL_FAILURE)
        restored = CoreError.from_dict(recoverable.to_dict())
        self.assertTrue(recoverable.recoverable)
        self.assertFalse(recoverable.terminal)
        self.assertFalse(terminal.recoverable)
        self.assertTrue(terminal.terminal)
        self.assertTrue(partial.recoverable)
        self.assertFalse(partial.terminal)
        self.assertEqual(restored, recoverable)

    def test_error_json_cannot_override_stable_retry_or_terminal_contract(self) -> None:
        payload = CoreError.for_code(ErrorCode.PROVIDER_FAILED).to_dict()
        payload["recoverable"] = True
        with self.assertRaisesRegex(ValueError, "stable code"):
            CoreError.from_dict(payload)


class CliContractTests(unittest.TestCase):
    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "src.cli", *args],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_inspect_config_is_offline_core_invocation(self) -> None:
        result = self._run_cli("inspect-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["target_translation_language"], "zh-Hans")
        self.assertEqual(payload["video_container"], "mp4")

    def test_create_and_show_job_do_not_fetch_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            created = self._run_cli(
                "create-job", "--url", "https://example.invalid/video", "--type", "VIDEO",
                "--workspace-root", directory, "--job-id", "cli-contract",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            created_payload = json.loads(created.stdout)
            job_file = Path(created_payload["job_file"])
            self.assertTrue(job_file.is_file())
            self.assertTrue((job_file.parent / "temp").is_dir())
            self.assertTrue((job_file.parent / "output").is_dir())
            shown = self._run_cli("show-job", str(job_file))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["current_state"], "QUEUED")
        self.assertEqual(json.loads(shown.stdout)["resolved_content_type"], "VIDEO")

    def test_provider_failure_is_mapped_before_it_can_cross_cli_boundary(self) -> None:
        stderr = StringIO()
        failure = ProviderFailure(ProviderFailureKind.NETWORK, "fake-provider", "connection reset", 45)
        with patch("src.cli._create_job", side_effect=failure), redirect_stderr(stderr):
            exit_code = cli.main(["create-job", "--url", "https://example.invalid/video"])
        payload = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["error"]["code"], "NETWORK_PAUSED")
        self.assertEqual(payload["error"]["provider"], "fake-provider")


if __name__ == "__main__":
    unittest.main()
