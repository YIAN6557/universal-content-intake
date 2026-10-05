from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.core.job import ContentType, Job
from src.core.policies import load_default_policies
from src.output.paths import OutputWorkspace
from src.providers.video import VideoProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Stage3SubtitleProviderTests(unittest.TestCase):
    def test_subtitle_extracts_into_job_temp_and_reuses_owned_track_without_exporting_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = OutputWorkspace.for_job(root / "jobs", "subtitle-provider").prepare()
            call_log = root / "calls.jsonl"
            script = "\n".join((
                "import json, pathlib, sys",
                f"pathlib.Path({str(call_log)!r}).open('a').write(json.dumps(sys.argv[1:]) + '\\n')",
                "args=sys.argv[1:]",
                "out=pathlib.Path(args[args.index('--output')+1].replace('%(ext)s','en.vtt'))",
                "out.parent.mkdir(parents=True, exist_ok=True)",
                "out.write_text('WEBVTT\\n\\n00:00:00.000 --> 00:00:01.000\\nhello\\n', encoding='utf-8')",
            ))
            provider = VideoProvider(
                policies=load_default_policies(PROJECT_ROOT / "config" / "defaults.yaml"),
                yt_dlp_command=(sys.executable, "-c", script),
            )
            job = Job(
                job_id="subtitle-provider",
                source_url="https://youtube.com/watch?v=video-id",
                declared_content_type=ContentType.VIDEO,
                resolved_content_type=ContentType.VIDEO,
                workspace_path=str(paths.job_dir),
                temp_path=str(paths.temp_dir),
                output_path=str(paths.output_dir),
            )
            job.store_provider_metadata("yt-dlp", {
                "probe": {"video_id": "video-id", "canonical_url": job.source_url},
                "authentication": {"auth_attempted": False, "auth_source": None, "authenticated_retry": False, "auth_result": "not_attempted"},
            })
            track = {
                "language_code": "en",
                "source": "manual",
                "track_identifier": "manual:en:vtt",
                "source_sha256": "a" * 64,
            }
            first = provider.download_subtitle_track(job, track)
            second = provider.download_subtitle_track(job, track)
            extracted = Path(job.temp_path) / first["file_path"]
            self.assertTrue(extracted.is_file())
            self.assertTrue(extracted.resolve().is_relative_to(Path(job.temp_path).resolve()))
            self.assertEqual(first["extraction_status"], "downloaded")
            self.assertEqual(second["extraction_status"], "reused_existing_job_temp_file")
            calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertNotIn("--cookies-from-browser", calls[0])
            self.assertNotIn("--cookies", calls[0])
            self.assertFalse(list(Path(job.temp_path).rglob("cookies*")))
            self.assertFalse(list(paths.job_dir.rglob("cookies*")))


if __name__ == "__main__":
    unittest.main()
