from __future__ import annotations

import os

os.environ["UCI_PUBLISH_WRITER"] = "off"  # never call an LLM API (Claude or Gemini) from tests

import hashlib
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from src import cli
from src.core.job import ContentType, Job, JobState
from src.core.output_pipeline import run_output_pipeline
from src.core.state_machine import StateMachine
from src.output.paths import OutputWorkspace


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _job(root: Path, content_type: ContentType, job_id: str = "stage5-test") -> tuple[Job, Path]:
    paths = OutputWorkspace.for_job(root, job_id).prepare()
    job = Job(
        job_id=job_id,
        source_url="https://example.test/source",
        declared_content_type=content_type,
        resolved_content_type=content_type,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"platform": "example.test"},
    )
    for state in (JobState.CLAIMED, JobState.PROBING, JobState.DOWNLOADING):
        StateMachine.transition(job, state)
    job_file = paths.job_dir / "job.json"
    job_file.write_text(job.to_json(), encoding="utf-8")
    return job, job_file


def _artifact(job: Job, relative: str, data: bytes, **fields: object) -> dict[str, object]:
    path = Path(job.temp_path) / relative.removeprefix("temp/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": f"temp/{relative.removeprefix('temp/')}", "size": len(data), "sha256": _sha(data), **fields}


def _docx_bytes(text: str) -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>课堂协作</w:t></w:r></w:p>
<w:p><w:r><w:t>{text}</w:t></w:r></w:p>
</w:body></w:document>""".encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def _empty_docx_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )
    return output.getvalue()


class Stage5OutputPipelineTests(unittest.TestCase):
    def test_video_uses_stage3_validated_render_and_records_video_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.VIDEO)
            payload = b"validated-final-video"
            record = _artifact(job, "stage3/rendered.mp4", payload)
            record["size_bytes"] = record.pop("size")
            job.source_metadata["providers"] = {"yt-dlp": {"probe": {"width": 1920, "height": 1080, "original_language": "en"}, "selection": {"width": 1280, "height": 720}}}
            job.source_metadata["stage3"] = {
                "status": "validated",
                "validated_artifact": {**record, "role": "STAGE3_VALIDATED_RENDERED_ARTIFACT"},
                "subtitle_discovery": {"selected": {"language": "en"}},
                "translation": {"engine": "Apple Translation"},
                "asr": {"engine": "whisper.cpp"},
                "render": {"status": "validated"},
            }
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            final = Path(job.output_path) / "final.mp4"
            self.assertEqual(final.read_bytes(), payload)
            info = (Path(job.output_path) / "info.md").read_text(encoding="utf-8")
            self.assertIn("- Original Resolution: 1920x1080", info)
            self.assertIn("- Downloaded Resolution: 1280x720", info)
            self.assertIn("- Translation Engine: Apple Translation", info)
            self.assertIn("- First Seen: ", info)
            self.assertIn("- Queue ID: ", info)
            self.assertFalse(Path(job.temp_path).exists())

    def test_paused_stage3_video_does_not_fall_back_to_stage2_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.VIDEO, "paused-stage3-video")
            source = _artifact(job, "source_video/source_video.mp4", b"stage2-source-video")
            job.store_provider_metadata("yt-dlp", {"fetch": {"validated_artifact": source}})
            job.source_metadata["stage3"] = {"status": "paused", "checkpoint": {"reason": "network"}}
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.job.current_state, JobState.PROVIDER_FAILED)
            self.assertFalse((Path(job.output_path) / "content/final.mp4").exists())
            self.assertTrue(Path(job.temp_path, "source_video/source_video.mp4").is_file())
            self.assertEqual(Job.from_json(job_file.read_text(encoding="utf-8")).current_state, JobState.PROVIDER_FAILED)

    def test_image_set_preserves_source_order_and_independent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.IMAGE_SET)
            records = [
                _artifact(job, "images/second.jpg", b"image-1", source_order=1, filename="second.jpg", media_type="image/jpeg"),
                _artifact(job, "images/first.jpg", b"image-2", source_order=2, filename="first.jpg", media_type="image/jpeg"),
            ]
            job.store_provider_metadata("gallery-dl", {"probe": {"title": "Gallery"}, "fetch": {"status": "complete", "artifacts": records}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            expected = ("content/second.jpg", "content/first.jpg")
            self.assertEqual(outcome.result.content_paths, expected)
            self.assertEqual([(Path(job.output_path) / path).read_bytes() for path in expected], [b"image-1", b"image-2"])
            self.assertFalse(any(path.suffix.lower() in {".zip", ".rar", ".7z"} for path in Path(job.output_path).rglob("*")))

    def test_webpage_promotes_exact_html_markdown_pdf_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.WEBPAGE)
            records = [
                _artifact(job, "article.html", b"<html>offline</html>", role="html", filename="article.html"),
                _artifact(job, "article.md", b"# Offline\n\nBody", role="markdown", filename="article.md"),
                _artifact(job, "article.pdf", b"%PDF-1.4\nvalid fixture", role="pdf", filename="article.pdf"),
                _artifact(job, "webpage-manifest.json", b"{}", role="manifest", filename="webpage-manifest.json"),
            ]
            job.store_provider_metadata("singlefile", {"probe": {"hostname": "example.test"}, "fetch": {"title": "Offline", "artifacts": records}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            self.assertEqual(outcome.result.content_paths, ("content/article.html", "content/article.md", "content/article.pdf"))
            self.assertTrue((Path(job.output_path) / "content/article.html").is_file())
            self.assertTrue((Path(job.output_path) / "content/article.md").is_file())
            self.assertTrue((Path(job.output_path) / "content/article.pdf").is_file())
            self.assertFalse(Path(job.temp_path).exists())

    def test_article_info_md_has_stable_base_fields_and_redacts_query_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE)
            job.source_url = "https://example.test/story?token=private-value"
            record = _artifact(job, "articles/article.md", b"# Story\n\nA readable article body.", role="article")
            job.store_provider_metadata("trafilatura", {"probe": {"platform": "example.test"}, "fetch": {
                "artifact": record,
                "metadata": {"title": "Story title", "author": "Example Author", "author_url": "https://example.test/author", "published_at": "2026-09-24", "main_text": "A readable article body."},
            }})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            info = (Path(job.output_path) / outcome.result.info_path).read_text(encoding="utf-8")
            for field in ("Type: ARTICLE", "Platform: example.test", "Source URL: https://example.test/story?[redacted]", "Original Title: Story title", "Author: Example Author", "Author URL:", "Published At: 2026-09-24", "Main Content: # Story A readable article body.", "Content Deliverable: content/article.md"):
                self.assertIn(field, info)
            self.assertNotIn("private-value", info)

    def test_formalization_crash_resumes_partial_atomic_promotions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.WEBPAGE)
            records = [
                _artifact(job, "article.html", b"<html>offline</html>", role="html", filename="article.html"),
                _artifact(job, "article.md", b"# Offline", role="markdown", filename="article.md"),
                _artifact(job, "article.pdf", b"%PDF-1.4\nvalid fixture", role="pdf", filename="article.pdf"),
                _artifact(job, "webpage-manifest.json", b"{}", role="manifest", filename="webpage-manifest.json"),
            ]
            job.store_provider_metadata("singlefile", {"fetch": {"artifacts": records}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            import src.core.output_pipeline as output_pipeline
            promote = output_pipeline._promote_staged

            def crash_on_pdf(staged: Path, destination: Path, record: object) -> None:
                if isinstance(record, dict) and record.get("role") == "pdf":
                    raise SystemExit("controlled crash after partial promotion")
                promote(staged, destination, record)

            with patch("src.core.output_pipeline._promote_staged", side_effect=crash_on_pdf):
                with self.assertRaises(SystemExit):
                    run_output_pipeline(job, job_file=job_file)

            partial_paths = {path.name for path in (Path(job.output_path) / "content").iterdir()}
            self.assertEqual(partial_paths, {"article.html", "article.md"})
            self.assertEqual(Job.from_json(job_file.read_text(encoding="utf-8")).current_state, JobState.DOCUMENTING)

            resumed = run_output_pipeline(Job.from_json(job_file.read_text(encoding="utf-8")), job_file=job_file)

            self.assertTrue(resumed.success, resumed.error)
            self.assertEqual(set(path.name for path in (Path(job.output_path) / "content").iterdir()), {"article.html", "article.md", "article.pdf"})
            self.assertFalse(Path(job.temp_path).exists())

    def test_cleanup_registry_removes_nested_runtime_files_without_touching_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE)
            record = _artifact(job, "article.md", b"# Runtime");
            runtime = Path(job.temp_path) / ".webpage-runtime/chrome/Profile/Cache"
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"runtime cache")
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": record}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            self.assertFalse(Path(job.temp_path).exists())
            self.assertTrue((Path(job.output_path) / "content/article.md").is_file())
            self.assertTrue((Path(job.workspace_path) / ".stage5-manifest.json").is_file())

    def test_document_summary_extracts_chinese_text_and_parse_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.DOCUMENT, "docx-summary")
            record = _artifact(job, "documents/lesson.docx", _docx_bytes("学生可以共同编辑课堂材料，并在文档中及时反馈。"), role="document_file", filename="lesson.docx")
            job.store_provider_metadata("rclone", {"probe": {"platform": "drive"}, "fetch": {"artifacts": [record], "engine": "rclone"}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            info = (Path(job.output_path) / outcome.result.info_path).read_text(encoding="utf-8")
            self.assertIn("学生可以共同编辑课堂材料", info)
            self.assertIn("Document Summary zh-Hans:", info)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.DOCUMENT, "bad-docx-summary")
            record = _artifact(job, "documents/empty.docx", _empty_docx_bytes(), role="document_file", filename="empty.docx")
            job.store_provider_metadata("rclone", {"fetch": {"artifacts": [record]}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            info = (Path(job.output_path) / outcome.result.info_path).read_text(encoding="utf-8")
            self.assertIn("Document Summary zh-Hans: ", info)
            self.assertEqual(outcome.result.summary["status"], "empty_parse_or_unsupported_format")

    def test_single_image_is_preserved_without_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.IMAGE)
            original = b"\xff\xd8\xffvalidated-image"
            record = _artifact(job, "images/original.jpg", original, source_order=1, filename="original.jpg", media_type="image/jpeg")
            job.store_provider_metadata("gallery-dl", {"fetch": {"status": "complete", "expected_count": 1, "artifacts": [record]}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertTrue(outcome.success, outcome.error)
            output = Path(job.output_path) / "content/original.jpg"
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(outcome.result.content_paths, ("content/original.jpg",))
            self.assertFalse(Path(job.temp_path).exists())

    def test_finalize_job_cli_runs_core_finalizer_on_persisted_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE)
            record = _artifact(job, "article.md", b"# CLI finalized")
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": record}})
            job_file.write_text(job.to_json(), encoding="utf-8")
            output = StringIO()

            with redirect_stdout(output):
                exit_code = cli.main(["finalize-job", "--resume-job", str(job_file)])

            self.assertEqual(exit_code, 0)
            response = json.loads(output.getvalue())
            self.assertEqual(response["job"]["current_state"], "COMPLETED")
            self.assertEqual(response["output_result"]["content_paths"], ["content/article.md"])

    def test_collision_never_overwrites_and_resume_keeps_the_allocated_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.DOCUMENT)
            record = _artifact(job, "source/report.pdf", b"validated pdf", role="document_file", filename="report.pdf")
            job.store_provider_metadata("curl", {"fetch": {"artifacts": [record]}})
            (Path(job.output_path) / "content").mkdir()
            existing = Path(job.output_path) / "content/report.pdf"
            existing.write_bytes(b"user file")
            existing_info = Path(job.output_path) / "info.md"
            existing_info.write_text("user-owned info", encoding="utf-8")
            job_file.write_text(job.to_json(), encoding="utf-8")

            first = run_output_pipeline(job, job_file=job_file)
            before = sorted(path.relative_to(Path(job.output_path)).as_posix() for path in Path(job.output_path).rglob("*"))
            second_job = Job.from_json(job_file.read_text(encoding="utf-8"))
            second = run_output_pipeline(second_job, job_file=job_file)
            after = sorted(path.relative_to(Path(job.output_path)).as_posix() for path in Path(job.output_path).rglob("*"))

            self.assertTrue(first.success, first.error)
            self.assertTrue(second.success, second.error)
            self.assertEqual(existing.read_bytes(), b"user file")
            self.assertEqual(first.result.content_paths, ("content/report-2.pdf",))
            self.assertEqual(first.result.info_path, "info-2.md")
            self.assertEqual(existing_info.read_text(encoding="utf-8"), "user-owned info")
            self.assertEqual(second.result.content_paths, first.result.content_paths)
            self.assertEqual(before, after)

    def test_hash_mismatch_fails_before_output_and_preserves_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE)
            record = _artifact(job, "article.md", b"real bytes", size=9, sha256="0" * 64)
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": record}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(job.current_state, JobState.PROVIDER_FAILED)
            self.assertEqual(list(Path(job.output_path).iterdir()), [])
            self.assertTrue(Path(job.temp_path, "article.md").is_file())

    def test_path_traversal_artifact_is_rejected_without_touching_unrelated_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE)
            outside = root / "outside.md"
            outside.write_text("unrelated", encoding="utf-8")
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": {
                "path": "temp/../outside.md", "size": outside.stat().st_size,
                "sha256": _sha(outside.read_bytes()), "role": "article",
            }}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(job.current_state, JobState.PROVIDER_FAILED)
            self.assertEqual(outside.read_text(encoding="utf-8"), "unrelated")
            self.assertEqual(list(Path(job.output_path).iterdir()), [])

    def test_wrong_job_file_path_is_refused_without_mutating_job_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE, "wrong-job-file")
            record = _artifact(job, "article.md", b"# Valid article")
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": record}})
            job_file.write_text(job.to_json(), encoding="utf-8")
            original = job_file.read_bytes()
            outside_job_file = root / "outside-job.json"
            outside_job_file.write_text("do not touch", encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=outside_job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(job.current_state, JobState.DOWNLOADING)
            self.assertEqual(job_file.read_bytes(), original)
            self.assertEqual(outside_job_file.read_text(encoding="utf-8"), "do not touch")
            self.assertFalse((Path(job.output_path) / "content/article.md").exists())
            self.assertTrue(Path(job.temp_path, "article.md").is_file())

    def test_cleanup_failure_persists_details_and_resume_only_retries_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE)
            record = _artifact(job, "article.md", b"# Persisted\n\nKeep me.", size=21, sha256=_sha(b"# Persisted\n\nKeep me."))
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": record, "metadata": {"title": "Persisted"}}})
            job_file.write_text(job.to_json(), encoding="utf-8")

            with patch("src.core.output_pipeline._unlink_registered_file", side_effect=PermissionError("fixture lock")):
                first = run_output_pipeline(job, job_file=job_file)

            self.assertFalse(first.success)
            self.assertEqual(job.current_state, JobState.CLEANUP_FAILED)
            self.assertTrue((Path(job.output_path) / "content/article.md").is_file())
            self.assertTrue(Path(job.temp_path).exists())
            persisted = Job.from_json(job_file.read_text(encoding="utf-8"))
            self.assertIn("temp/article.md", persisted.source_metadata["stage5"]["cleanup"]["unremoved_paths"])

            resumed = run_output_pipeline(persisted, job_file=job_file)

            self.assertTrue(resumed.success, resumed.error)
            self.assertEqual(resumed.job.current_state, JobState.COMPLETED)
            self.assertEqual(resumed.result.content_paths, first.result.content_paths)
            self.assertEqual(len(list((Path(job.output_path) / "content").glob("article*.md"))), 1)
            self.assertFalse(Path(job.temp_path).exists())

    def test_malicious_temp_symlink_is_not_followed_and_cleanup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "unrelated.txt"
            outside.write_text("preserve", encoding="utf-8")
            job, job_file = _job(root, ContentType.ARTICLE)
            record = _artifact(job, "article.md", b"# safe")
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": record}})
            (Path(job.temp_path) / "escape").symlink_to(outside)
            job_file.write_text(job.to_json(), encoding="utf-8")

            outcome = run_output_pipeline(job, job_file=job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(job.current_state, JobState.CLEANUP_FAILED)
            self.assertTrue((Path(job.output_path) / "content/article.md").is_file())
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
            self.assertTrue((Path(job.temp_path) / "escape").is_symlink())

    def test_parallel_finalization_is_idempotent_under_job_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, job_file = _job(root, ContentType.ARTICLE)
            record = _artifact(job, "article.md", b"# Concurrent", size=12, sha256=_sha(b"# Concurrent"))
            job.store_provider_metadata("trafilatura", {"fetch": {"artifact": record}})
            job_file.write_text(job.to_json(), encoding="utf-8")
            jobs = [Job.from_json(job_file.read_text(encoding="utf-8")) for _ in range(2)]

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda candidate: run_output_pipeline(candidate, job_file=job_file), jobs))

            self.assertTrue(all(outcome.success for outcome in outcomes))
            self.assertEqual(len(list((Path(job.output_path) / "content").glob("article*.md"))), 1)
            self.assertEqual(Job.from_json(job_file.read_text(encoding="utf-8")).current_state, JobState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
