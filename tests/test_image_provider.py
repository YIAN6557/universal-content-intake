from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.core.job import ContentType, Job, JobState
from src.output.paths import OutputWorkspace
from src.providers.base import ProviderFailure, ProviderFailureKind
from src.providers.image import ImageProvider


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/FioAAAAASUVORK5CYII="
)


def gallery_payload(*, count: int = 1, names: tuple[str, ...] | None = None) -> str:
    names = names or tuple(f"photo-{index}.png" for index in range(1, count + 1))
    data: list[list[Any]] = [[2, {
        "category": "fixture",
        "subcategory": "gallery",
        "title": "Fixture album",
        "count_photos": count,
        "user": {"realname": "Fixture Author", "username": "fixture-account"},
    }]]
    for order, name in enumerate(names, start=1):
        stem, extension = name.rsplit(".", 1)
        data.append([3, f"https://cdn.example.test/{order}.png", {
            "category": "fixture",
            "subcategory": "image",
            "num": order,
            "id": f"item-{order}",
            "filename": stem,
            "extension": extension,
            "width": 5 + order,
            "height": 9 + order,
            "title": f"Item {order}",
            "description": f"Caption {order}",
            "user": {"realname": "Fixture Author", "username": "fixture-account"},
        }])
    return json.dumps(data)


def gallery_error(error: str, message: str) -> str:
    return json.dumps([[-1, {"error": error, "message": message}]])


def make_job(
    root: Path,
    url: str = "https://gallery.example.test/album/one",
    declared_type: ContentType | None = None,
) -> Job:
    workspace = OutputWorkspace.for_job(root, "image-test")
    paths = workspace.prepare()
    return Job(
        job_id="image-test",
        source_url=url,
        declared_content_type=declared_type,
        resolved_content_type=declared_type or ContentType.UNKNOWN,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
    )


class CommandHarness:
    """A complete subprocess boundary fixture; assertions target Provider outcomes."""

    def __init__(self) -> None:
        self.probes: dict[str, str] = {}
        self.head: dict[str, tuple[int, str]] = {}
        self.failed_media: set[str] = set()
        self.media_bytes: dict[str, bytes] = {}
        self.downloads: dict[str, int] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        url = args[-1]
        if "--dump-json" in args:
            return subprocess.CompletedProcess(args, 0, self.probes.get(url, gallery_error("NoExtractorError", "No extractor found")), "")
        if "--head" in args:
            status, content_type = self.head.get(url, (200, "image/png"))
            out = f"__UCI_STATUS__:{status}\n__UCI_CONTENT_TYPE__:{content_type}\n"
            return subprocess.CompletedProcess(args, 0, out, "")
        self.downloads[url] = self.downloads.get(url, 0) + 1
        if url in self.failed_media:
            return subprocess.CompletedProcess(args, 1, "", "ERROR: connection timed out")
        if args[0].endswith("curl"):
            output = Path(args[args.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(self.media_bytes.get(url, PNG_BYTES))
            return subprocess.CompletedProcess(args, 0, "__UCI_STATUS__:200\n__UCI_CONTENT_TYPE__:image/png\n", "")
        directory = Path(args[args.index("--directory") + 1])
        filename = args[args.index("--filename") + 1].replace("{extension}", "png")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_bytes(self.media_bytes.get(url, PNG_BYTES))
        return subprocess.CompletedProcess(args, 0, "", "")


class ImageProviderTests(unittest.TestCase):
    def root(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def provider(self, runner: CommandHarness) -> ImageProvider:
        return ImageProvider(gallery_dl_path="gallery-dl", curl_path="/usr/bin/curl", runner=runner)

    def test_probe_normalizes_single_image_and_leaves_job_state_to_core(self) -> None:
        source = "https://gallery.example.test/album/one"
        runner = CommandHarness()
        runner.probes[source] = gallery_payload()
        job = make_job(self.root(), source)

        result = self.provider(runner).probe(job)

        self.assertEqual(result.metadata["probe"]["content_type"], "IMAGE")
        self.assertEqual(result.metadata["probe"]["extractor"], "fixture/gallery")
        self.assertEqual(result.metadata["probe"]["item_count"], 1)
        self.assertEqual(result.metadata["probe"]["items"][0]["source_order"], 1)
        self.assertEqual(result.metadata["probe"]["items"][0]["dimensions"], {"width": 6, "height": 10})
        self.assertEqual(result.metadata["probe"]["items"][0]["author"], "Fixture Author")
        self.assertEqual(job.current_state, JobState.QUEUED)
        self.assertEqual(job.source_metadata, {})

    def test_probe_normalizes_image_set_in_source_order_and_records_original_quality_policy(self) -> None:
        source = "https://gallery.example.test/album/many"
        runner = CommandHarness()
        runner.probes[source] = gallery_payload(count=3)
        job = make_job(self.root(), source, ContentType.IMAGE_SET)

        result = self.provider(runner).probe(job)
        probe = result.metadata["probe"]

        self.assertEqual(probe["content_type"], "IMAGE_SET")
        self.assertEqual(probe["item_count"], 3)
        self.assertEqual([item["source_order"] for item in probe["items"]], [1, 2, 3])
        self.assertEqual([item["original_filename"] for item in probe["items"]], ["photo-1.png", "photo-2.png", "photo-3.png"])
        self.assertEqual(probe["quality_policy"], "gallery-dl native original; no resizing or recompression")

    def test_probe_uses_provider_blog_metadata_for_author_and_account(self) -> None:
        source = "https://gallery.example.test/tumblr/post"
        payload = json.loads(gallery_payload())
        payload[0][1]["blog"] = {"name": "fixture-blog", "title": "Fixture Photographer"}
        payload[0][1].pop("user", None)
        payload[1][2]["blog"] = payload[0][1]["blog"]
        payload[1][2].pop("user", None)
        runner = CommandHarness()
        runner.probes[source] = json.dumps(payload)
        job = make_job(self.root(), source)

        result = self.provider(runner).probe(job)

        self.assertEqual(result.metadata["probe"]["items"][0]["author"], "Fixture Photographer")
        self.assertEqual(result.metadata["probe"]["items"][0]["account"], "fixture-blog")

    def test_repeated_post_directories_do_not_shrink_total_gallery_count(self) -> None:
        source = "https://gallery.example.test/tumblr/blog"
        payload = [
            [2, {"category": "tumblr", "subcategory": "user", "count": 1}],
            [3, "https://cdn.example.test/1.png", {"category": "tumblr", "subcategory": "post", "id": "post-1", "filename": "one.png", "extension": "png"}],
            [2, {"category": "tumblr", "subcategory": "user", "count": 1}],
            [3, "https://cdn.example.test/2.png", {"category": "tumblr", "subcategory": "post", "id": "post-2", "filename": "two.png", "extension": "png"}],
        ]
        runner = CommandHarness()
        runner.probes[source] = json.dumps(payload)
        job = make_job(self.root(), source)

        probe = self.provider(runner).probe(job).metadata["probe"]

        self.assertEqual(probe["item_count"], 2)
        self.assertEqual(probe["enumerated_item_count"], 2)
        self.assertEqual(probe["content_type"], "IMAGE_SET")

    def test_fetch_records_sha256_identity_and_contains_artifact_under_job_temp(self) -> None:
        source = "https://gallery.example.test/album/one"
        runner = CommandHarness()
        runner.probes[source] = gallery_payload()
        job = make_job(self.root(), source)
        provider = self.provider(runner)
        probe = provider.probe(job)

        result = provider.fetch(job, resume_token=probe.resume_token)
        artifact = result.metadata["fetch"]["artifacts"][0]
        path = Path(job.workspace_path) / artifact["path"]

        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(Path(job.temp_path).resolve()))
        self.assertEqual(artifact["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(artifact["size"], path.stat().st_size)
        self.assertTrue(artifact["artifact_id"].startswith("img-"))
        self.assertEqual(artifact["provider"], "gallery-dl")
        self.assertEqual(artifact["source_order"], 1)
        self.assertFalse(Path(job.output_path, "content").exists())
        self.assertEqual(job.current_state, JobState.QUEUED)

    def test_image_set_manifest_preserves_order_and_hash_for_every_item(self) -> None:
        source = "https://gallery.example.test/album/many"
        runner = CommandHarness()
        runner.probes[source] = gallery_payload(count=3)
        job = make_job(self.root(), source, ContentType.IMAGE_SET)
        provider = self.provider(runner)
        probe = provider.probe(job)

        result = provider.fetch(job, resume_token=probe.resume_token)
        manifest_path = Path(job.workspace_path) / result.metadata["fetch"]["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["expected_count"], 3)
        self.assertEqual([item["source_order"] for item in manifest["items"]], [1, 2, 3])
        self.assertEqual([item["sha256"] for item in manifest["items"]], [item["sha256"] for item in result.metadata["fetch"]["artifacts"]])

    def test_direct_image_uses_curl_only_after_gallery_dl_rejects_and_head_confirms_image(self) -> None:
        source = "https://images.example.test/file"
        runner = CommandHarness()
        runner.probes[source] = gallery_error("NoExtractorError", "No extractor found")
        runner.head[source] = (200, "image/png")
        job = make_job(self.root(), source)
        provider = self.provider(runner)

        probe = provider.probe(job)
        result = provider.fetch(job, resume_token=probe.resume_token)

        self.assertTrue(probe.metadata["probe"]["curl_fallback"])
        self.assertEqual(result.metadata["fetch"]["artifacts"][0]["provider"], "curl")
        self.assertEqual(runner.downloads[source], 1)

    def test_html_url_is_not_accepted_as_direct_image_or_downloaded_by_curl(self) -> None:
        source = "https://example.com/article"
        runner = CommandHarness()
        runner.probes[source] = gallery_error("NoExtractorError", "No extractor found")
        runner.head[source] = (200, "text/html; charset=utf-8")
        job = make_job(self.root(), source)

        with self.assertRaises(ProviderFailure) as caught:
            self.provider(runner).probe(job)

        self.assertEqual(caught.exception.kind, ProviderFailureKind.UNSUPPORTED)
        self.assertFalse(any(args[0].endswith("curl") and "--output" in args for args in runner.calls))

    def test_curl_rejects_non_image_signature_even_when_server_claims_image_mime(self) -> None:
        source = "https://images.example.test/not-really-image"
        runner = CommandHarness()
        runner.probes[source] = gallery_error("NoExtractorError", "No extractor found")
        runner.head[source] = (200, "image/png")
        runner.media_bytes[source] = b"<html>not an image</html>"
        job = make_job(self.root(), source)
        provider = self.provider(runner)
        probe = provider.probe(job)

        with self.assertRaises(ProviderFailure) as caught:
            provider.fetch(job, resume_token=probe.resume_token)

        self.assertEqual(caught.exception.kind, ProviderFailureKind.FAILED)
        self.assertEqual(list(Path(job.temp_path).rglob("*.part")), [])

    def test_untrusted_filename_is_sanitized_and_collision_never_overwrites(self) -> None:
        source = "https://gallery.example.test/album/one"
        runner = CommandHarness()
        runner.probes[source] = gallery_payload(names=("../../same.png",))
        job = make_job(self.root(), source)
        provider = self.provider(runner)
        probe = provider.probe(job)
        identity = probe.metadata["probe"]["source_identity"]
        files = Path(job.temp_path) / "uci_images" / identity / "files"
        files.mkdir(parents=True)
        existing = files / "0001_same.png"
        existing.write_bytes(b"do not overwrite")

        result = provider.fetch(job, resume_token=probe.resume_token)
        artifact = result.metadata["fetch"]["artifacts"][0]

        self.assertEqual(existing.read_bytes(), b"do not overwrite")
        self.assertEqual(artifact["filename"], "0001_same-2.png")
        self.assertTrue((Path(job.workspace_path) / artifact["path"]).resolve().is_relative_to(Path(job.temp_path).resolve()))

    def test_resume_reuses_verified_artifact_without_second_download_or_duplicate_id(self) -> None:
        source = "https://gallery.example.test/album/many"
        runner = CommandHarness()
        runner.probes[source] = gallery_payload(count=2)
        job = make_job(self.root(), source, ContentType.IMAGE_SET)
        provider = self.provider(runner)
        first_probe = provider.probe(job)
        first = provider.fetch(job, resume_token=first_probe.resume_token)
        first_count = dict(runner.downloads)

        resumed_probe = provider.probe(job)
        resumed = provider.fetch(job, resume_token=resumed_probe.resume_token)

        self.assertEqual(runner.downloads, first_count)
        self.assertEqual([a["artifact_id"] for a in first.metadata["fetch"]["artifacts"]], [a["artifact_id"] for a in resumed.metadata["fetch"]["artifacts"]])
        self.assertEqual(len({a["path"] for a in resumed.metadata["fetch"]["artifacts"]}), 2)
        self.assertEqual(resumed.metadata["fetch"]["reused_count"], 2)

    def test_partial_failure_retains_success_and_resume_downloads_only_missing_item(self) -> None:
        source = "https://gallery.example.test/album/many"
        runner = CommandHarness()
        runner.probes[source] = gallery_payload(count=2)
        runner.failed_media.add("https://cdn.example.test/2.png")
        job = make_job(self.root(), source, ContentType.IMAGE_SET)
        provider = self.provider(runner)
        first_probe = provider.probe(job)

        with self.assertRaises(ProviderFailure) as caught:
            provider.fetch(job, resume_token=first_probe.resume_token)
        self.assertEqual(caught.exception.kind, ProviderFailureKind.PARTIAL)
        partial = caught.exception.details["image_partial"]
        self.assertEqual(partial["expected_count"], 2)
        self.assertEqual(len(partial["successful_items"]), 1)
        self.assertEqual(len(partial["failed_items"]), 1)
        self.assertTrue(partial["manifest_path"].startswith("temp/"))

        runner.failed_media.clear()
        resumed_probe = provider.probe(job)
        resumed = provider.fetch(job, resume_token=resumed_probe.resume_token)

        self.assertEqual(runner.downloads["https://cdn.example.test/1.png"], 1)
        self.assertEqual(runner.downloads["https://cdn.example.test/2.png"], 2)
        self.assertEqual([item["source_order"] for item in resumed.metadata["fetch"]["artifacts"]], [1, 2])
        self.assertEqual(resumed.metadata["fetch"]["reused_count"], 1)

    def test_reported_gallery_count_mismatch_cannot_be_marked_complete(self) -> None:
        source = "https://gallery.example.test/album/incomplete"
        payload = json.loads(gallery_payload(count=2))
        payload[0][1]["count_photos"] = 3
        runner = CommandHarness()
        runner.probes[source] = json.dumps(payload)
        job = make_job(self.root(), source, ContentType.IMAGE_SET)
        provider = self.provider(runner)
        probe = provider.probe(job)

        with self.assertRaises(ProviderFailure) as caught:
            provider.fetch(job, resume_token=probe.resume_token)

        self.assertEqual(caught.exception.kind, ProviderFailureKind.PARTIAL)
        self.assertEqual(caught.exception.details["image_partial"]["expected_count"], 3)
        self.assertEqual(len(caught.exception.details["image_partial"]["successful_items"]), 2)
        self.assertEqual(caught.exception.details["image_partial"]["failed_items"][0]["source_order"], 3)

    def test_source_identity_mismatch_does_not_reuse_old_artifacts(self) -> None:
        runner = CommandHarness()
        original = "https://gallery.example.test/album/original"
        changed = "https://gallery.example.test/album/changed"
        runner.probes[original] = gallery_payload()
        runner.probes[changed] = gallery_payload()
        job = make_job(self.root(), original)
        provider = self.provider(runner)
        first_probe = provider.probe(job)
        first = provider.fetch(job, resume_token=first_probe.resume_token)
        job.source_url = changed

        changed_probe = provider.probe(job)
        second = provider.fetch(job, resume_token=changed_probe.resume_token)

        self.assertNotEqual(first_probe.resume_token, changed_probe.resume_token)
        self.assertNotEqual(first.metadata["fetch"]["manifest_path"], second.metadata["fetch"]["manifest_path"])
        self.assertEqual(runner.downloads["https://cdn.example.test/1.png"], 2)
        self.assertEqual(second.metadata["fetch"]["reused_count"], 0)

    def test_error_classification_maps_invalid_auth_network_and_unknown_provider_failures(self) -> None:
        cases = (
            ("NotFoundError", "Requested user or post could not be found", ProviderFailureKind.INVALID_URL),
            ("AuthorizationError", "account authorization is required", ProviderFailureKind.AUTH_REQUIRED),
            ("AuthenticationError", "login required; unauthorized", ProviderFailureKind.AUTH_REQUIRED),
            ("HTTPError", "HTTP 403 Permission required; sign in to continue", ProviderFailureKind.AUTH_REQUIRED),
            ("HTTPError", "HTTP 403 Access denied; request access from the owner", ProviderFailureKind.AUTH_REQUIRED),
            ("HTTPError", "HTTP 403 Automated requests blocked by anti-bot challenge", ProviderFailureKind.FAILED),
            ("HTTPError", "HTTP 403 Forbidden", ProviderFailureKind.FAILED),
            ("HTTPError", "HTTP 429 Too Many Requests", ProviderFailureKind.NETWORK),
            ("ConnectionError", "connection timed out after retries", ProviderFailureKind.NETWORK),
            ("RuntimeError", "unexpected extractor failure", ProviderFailureKind.FAILED),
        )
        for error, message, expected in cases:
            with self.subTest(error=error):
                source = f"https://gallery.example.test/{error}"
                runner = CommandHarness()
                runner.probes[source] = gallery_error(error, message)
                job = make_job(self.root(), source)
                with self.assertRaises(ProviderFailure) as caught:
                    self.provider(runner).probe(job)
                self.assertEqual(caught.exception.kind, expected)

    def test_invalid_scheme_is_structured_url_invalid_failure(self) -> None:
        job = make_job(self.root(), "file:///tmp/not-a-gallery")
        with self.assertRaises(ProviderFailure) as caught:
            self.provider(CommandHarness()).probe(job)
        self.assertEqual(caught.exception.kind, ProviderFailureKind.INVALID_URL)


if __name__ == "__main__":
    unittest.main()
