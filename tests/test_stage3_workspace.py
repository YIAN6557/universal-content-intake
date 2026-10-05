from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.job import ContentType, Job
from src.media.stage3_workspace import Stage3Workspace, Stage3WorkspaceError
from src.output.paths import OutputWorkspace


def make_job(root: Path) -> Job:
    paths = OutputWorkspace.for_job(root, "stage3-test").prepare()
    return Job(
        job_id="stage3-test",
        source_url="https://youtube.com/watch?v=fixture",
        declared_content_type=ContentType.VIDEO,
        resolved_content_type=ContentType.VIDEO,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
    )


class Stage3WorkspaceTests(unittest.TestCase):
    def test_job_temp_is_canonical_and_rejects_escape_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Stage3Workspace(make_job(Path(directory)))
            self.assertTrue(workspace.stage_dir("subtitles").is_relative_to(workspace.root))
            for path in ("../outside", "/tmp/outside", "stage3/../../outside"):
                with self.subTest(path=path), self.assertRaises(Stage3WorkspaceError):
                    workspace.path(path)

    def test_cache_reuses_only_matching_input_identity_and_verified_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Stage3Workspace(make_job(Path(directory)))
            artifact = workspace.path("stage3/asr/transcript.json")
            artifact.parent.mkdir(parents=True)
            artifact.write_text('{"cues":[]}', encoding="utf-8")
            inputs = {"source_sha256": "source-a", "model_sha256": "model-a"}
            workspace.write_cache("asr", inputs, ["stage3/asr/transcript.json"], {"engine": "whisper.cpp"})
            self.assertIsNotNone(workspace.load_cache("asr", inputs))
            self.assertIsNone(workspace.load_cache("asr", {**inputs, "source_sha256": "source-b"}))
            artifact.write_text('{"cues":["mutated"]}', encoding="utf-8")
            self.assertIsNone(workspace.load_cache("asr", inputs))

    def test_manifest_and_outputs_cannot_escape_through_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Stage3Workspace(make_job(root))
            outside = root / "outside"
            outside.mkdir()
            (workspace.root / "stage3").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(Stage3WorkspaceError):
                workspace.stage_dir("render")


if __name__ == "__main__":
    unittest.main()
