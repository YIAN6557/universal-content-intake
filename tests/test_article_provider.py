from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from src import cli
from src.core.article_pipeline import run_article_pipeline
from src.core.errors import ErrorCode
from src.core.job import ContentType, Job, JobState
from src.output.paths import OutputWorkspace
from src.providers.article import ArticleProvider
from src.providers.base import ProviderFailure, ProviderFailureKind


ARTICLE_HTML = b"""<!doctype html>
<html lang="en"><head>
<title>How the Sample System Changed</title>
<meta name="author" content="Ada Example">
<meta property="article:published_time" content="2025-01-02T03:04:05Z">
<meta name="description" content="A concise description of the reporting.">
<meta name="keywords" content="systems, reporting">
<meta property="og:site_name" content="Fixture Press">
<link rel="canonical" href="/canonical/sample-story">
</head><body>
<nav>Home | Latest stories | Sign in</nav>
<article>
<h1>How the Sample System Changed</h1>
<p>The first paragraph explains the main development and includes a
<a href="https://source.example.test/evidence">source link</a>. It gives
enough context to establish the subject before the details that follow.</p>
<h2>What changed</h2>
<p>The second paragraph provides the central factual details and explains
why the change matters to people who use the system every day.</p>
<ul><li>The first reported change was documented and independently checked.</li>
<li>The second reported change affects the service's availability.</li></ul>
<blockquote><p>"The evidence should remain attached to the original report,"
said the researcher.</p></blockquote>
<p>The final paragraph explains what readers should expect next and gives
the report a clear ending without adding any new interpretation.</p>
<img src="https://images.example.test/diagram.png" alt="System diagram">
</article>
<footer>Cookie banner | Recommended articles | All rights reserved</footer>
</body></html>"""

SHORT_ARTICLE_HTML = b"""<html lang="en"><head><title>Short report</title>
<meta name="author" content="Pat Reporter"></head><body><main>
<h1>Short report</h1><p>A brief but complete local report says the bridge
reopened today after inspectors confirmed that the repairs were safe.</p>
</main></body></html>"""

LOGIN_HTML = b"""<html><head><title>Sign in to continue</title></head><body>
<form action="/login"><label>Email</label><input name="email">
<button>Sign in</button></form><p>Please sign in to continue reading.</p>
</body></html>"""

SHELL_HTML = b"""<!doctype html><html><head><title>Loading...</title></head><body>
<div id="root"></div><script>window.__APP__ = {};</script>
</body></html>"""

FORM_ONLY_HTML = b"""<html><body><form>
<label>Customer name:</label><input name="name">
<label>Telephone:</label><input name="phone">
<label>Small</label><label>Medium</label><label>Large</label>
<button>Submit order</button></form></body></html>"""


@contextmanager
def article_server(
    routes: dict[str, tuple[int, bytes] | list[tuple[int, bytes]]],
) -> Iterator[tuple[str, dict[str, int]]]:
    counts: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            route_path = urlsplit(self.path).path
            counts[route_path] = counts.get(route_path, 0) + 1
            response = routes.get(route_path, (404, b"Not found"))
            if isinstance(response, list):
                index = min(counts[route_path] - 1, len(response) - 1)
                status, body = response[index]
            else:
                status, body = response
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url, counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def make_job(root: Path, url: str, job_id: str = "article-test") -> Job:
    workspace = OutputWorkspace.for_job(root, job_id)
    paths = workspace.prepare()
    return Job(
        job_id=job_id,
        source_url=url,
        declared_content_type=ContentType.ARTICLE,
        resolved_content_type=ContentType.ARTICLE,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
    )


class ArticleProviderTests(unittest.TestCase):
    def test_probe_is_offline_structured_and_does_not_mutate_job_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/story")
                before = job.to_json()

                result = ArticleProvider().probe(job)

                self.assertEqual(job.to_json(), before)
                self.assertEqual(job.current_state, JobState.QUEUED)
                self.assertEqual(result.metadata["probe"]["content_type"], "ARTICLE")
                self.assertEqual(counts.get("/story", 0), 0)
                self.assertEqual(len(result.resume_token or ""), 64)

    def test_article_extracts_normalized_metadata_and_writes_a_temp_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/story?campaign=test")
                provider = ArticleProvider()
                probe = provider.probe(job)

                result = provider.fetch(job, resume_token=probe.resume_token)

                metadata = result.metadata["fetch"]["metadata"]
                artifact = result.metadata["fetch"]["artifact"]
                article_path = Path(job.workspace_path) / artifact["path"]
                self.assertEqual(metadata["source_url"], job.source_url)
                self.assertEqual(metadata["canonical_url"], f"{base_url}/canonical/sample-story")
                self.assertEqual(metadata["hostname"], "127.0.0.1")
                self.assertEqual(metadata["title"], "How the Sample System Changed")
                self.assertEqual(metadata["author"], "Ada Example")
                self.assertEqual(metadata["published_at"], "2025-01-02")
                self.assertEqual(metadata["language"], "en")
                self.assertEqual(metadata["image_references"], ["https://images.example.test/diagram.png"])
                self.assertEqual(metadata["site_name"], "Fixture Press")
                self.assertIn("systems", metadata["tags"])
                self.assertEqual(metadata["extraction_engine"], "trafilatura")
                self.assertEqual(metadata["extraction_engine_version"], provider.version)
                self.assertGreater(metadata["content_length"], 100)
                self.assertTrue(article_path.is_file())
                self.assertTrue(article_path.resolve().is_relative_to(Path(job.temp_path).resolve()))
                self.assertFalse(Path(job.output_path, "content").exists())
                self.assertEqual(artifact["sha256"], hashlib.sha256(article_path.read_bytes()).hexdigest())
                self.assertEqual(artifact["size"], article_path.stat().st_size)
                self.assertEqual(artifact["canonical_url"], metadata["canonical_url"])

    def test_markdown_preserves_headings_paragraphs_lists_quotes_links_and_image_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/story")
                provider = ArticleProvider()
                probe = provider.probe(job)
                result = provider.fetch(job, resume_token=probe.resume_token)
                artifact = result.metadata["fetch"]["artifact"]
                markdown = (Path(job.workspace_path) / artifact["path"]).read_text(encoding="utf-8")

                self.assertIn("# How the Sample System Changed", markdown)
                self.assertIn("## What changed", markdown)
                self.assertIn("- The first reported change", markdown)
                self.assertIn('> "The evidence should remain attached', markdown)
                self.assertIn("[source link](https://source.example.test/evidence)", markdown)
                self.assertIn("![System diagram](https://images.example.test/diagram.png)", markdown)
                self.assertNotIn("Cookie banner", markdown)
                self.assertNotIn("Recommended articles", markdown)
                self.assertNotIn("Sign in", markdown)

    def test_short_legitimate_article_is_not_rejected_by_length_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/brief": (200, SHORT_ARTICLE_HTML)}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/brief")
                provider = ArticleProvider()
                probe = provider.probe(job)

                result = provider.fetch(job, resume_token=probe.resume_token)

                metadata = result.metadata["fetch"]["metadata"]
                artifact = result.metadata["fetch"]["artifact"]
                markdown = (Path(job.workspace_path) / artifact["path"]).read_text(encoding="utf-8")
                self.assertEqual(metadata["title"], "Short report")
                self.assertIn("bridge", markdown)
                self.assertGreater(metadata["content_length"], 0)

    def test_malformed_html_extracts_readable_main_text_without_template_content(self) -> None:
        malformed = b"<html><body><nav>Home</nav><article><h1>Broken markup</h1><p>A real paragraph remains readable despite omitted closing tags and an incomplete source document." 
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/malformed": (200, malformed)}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/malformed")
                provider = ArticleProvider()
                probe = provider.probe(job)

                result = provider.fetch(job, resume_token=probe.resume_token)

                article = result.metadata["fetch"]["artifact"]
                markdown = (Path(job.workspace_path) / article["path"]).read_text(encoding="utf-8")
                self.assertIn("Broken markup", markdown)
                self.assertIn("real paragraph", markdown)
                self.assertNotIn("Home", markdown)

    def test_javascript_shell_and_empty_extraction_map_to_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/shell": (200, SHELL_HTML)}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/shell")
                provider = ArticleProvider()
                probe = provider.probe(job)

                with self.assertRaises(ProviderFailure) as caught:
                    provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(caught.exception.kind, ProviderFailureKind.FAILED)
                self.assertIn("extract", caught.exception.message.lower())
                self.assertFalse(list(Path(job.temp_path).rglob("article.md")))

    def test_form_controls_are_not_mistaken_for_a_short_article(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/form": (200, FORM_ONLY_HTML)}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/form")
                provider = ArticleProvider()
                probe = provider.probe(job)

                with self.assertRaises(ProviderFailure) as caught:
                    provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(caught.exception.kind, ProviderFailureKind.FAILED)
                self.assertEqual(caught.exception.details["extraction_status"], "form_only")

    def test_login_wall_maps_to_auth_required_without_login_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/login": (200, LOGIN_HTML)}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/login")
                provider = ArticleProvider()
                probe = provider.probe(job)

                with self.assertRaises(ProviderFailure) as caught:
                    provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(caught.exception.kind, ProviderFailureKind.AUTH_REQUIRED)
                self.assertEqual(counts["/login"], 1)

    def test_404_maps_to_url_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/missing": (404, b"not found")}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/missing")
                provider = ArticleProvider()
                probe = provider.probe(job)

                with self.assertRaises(ProviderFailure) as caught:
                    provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(caught.exception.kind, ProviderFailureKind.INVALID_URL)

    def test_finite_retry_succeeds_after_transient_server_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            route = [
                (503, b"temporary"),
                (503, b"temporary"),
                (200, ARTICLE_HTML),
            ]
            with article_server({"/retry": route}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/retry")
                provider = ArticleProvider(retries=2, retry_backoff_seconds=0)
                probe = provider.probe(job)

                result = provider.fetch(job, resume_token=probe.resume_token)

                self.assertTrue(result.metadata["fetch"]["artifact"]["sha256"])
                self.assertEqual(counts["/retry"], 3)

    def test_retry_exhaustion_maps_to_network_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/down": (503, b"temporarily unavailable")}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/down")
                provider = ArticleProvider(retries=2, retry_backoff_seconds=0)
                probe = provider.probe(job)

                with self.assertRaises(ProviderFailure) as caught:
                    provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(caught.exception.kind, ProviderFailureKind.NETWORK)
                self.assertEqual(counts["/down"], 3)

    def test_existing_verified_artifact_is_reused_without_fetch_or_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/story")
                provider = ArticleProvider()
                probe = provider.probe(job)
                first = provider.fetch(job, resume_token=probe.resume_token)
                fetched_once = counts["/story"]

                second = provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(counts["/story"], fetched_once)
                self.assertEqual(second.metadata["fetch"]["reuse"], "verified")
                self.assertEqual(
                    first.metadata["fetch"]["artifact"]["sha256"],
                    second.metadata["fetch"]["artifact"]["sha256"],
                )
                self.assertEqual(first.metadata["fetch"]["artifact"]["path"], second.metadata["fetch"]["artifact"]["path"])

    def test_source_identity_change_does_not_reuse_previous_article(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/one": (200, ARTICLE_HTML), "/two": (200, SHORT_ARTICLE_HTML)}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/one")
                provider = ArticleProvider()
                first_probe = provider.probe(job)
                first = provider.fetch(job, resume_token=first_probe.resume_token)
                first_artifact = first.metadata["fetch"]["artifact"]

                job.source_url = f"{base_url}/two"
                second_probe = provider.probe(job)
                second = provider.fetch(job, resume_token=second_probe.resume_token)

                second_artifact = second.metadata["fetch"]["artifact"]
                self.assertNotEqual(first_probe.resume_token, second_probe.resume_token)
                self.assertNotEqual(first_artifact["path"], second_artifact["path"])
                self.assertNotEqual(first_artifact["sha256"], second_artifact["sha256"])
                self.assertEqual(counts["/one"], 1)
                self.assertEqual(counts["/two"], 1)

    def test_modified_existing_hash_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/story")
                provider = ArticleProvider()
                probe = provider.probe(job)
                first = provider.fetch(job, resume_token=probe.resume_token)
                article = Path(job.workspace_path) / first.metadata["fetch"]["artifact"]["path"]
                article.write_text("tampered", encoding="utf-8")

                resumed = provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(counts["/story"], 2)
                self.assertEqual(resumed.metadata["fetch"]["reuse"], "downloaded")
                self.assertNotEqual(resumed.metadata["fetch"]["artifact"]["sha256"], hashlib.sha256(b"tampered").hexdigest())

    def test_output_is_confined_to_temp_and_metadata_does_not_contain_raw_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, _counts):
                job = make_job(Path(directory), f"{base_url}/story")
                provider = ArticleProvider()
                probe = provider.probe(job)
                result = provider.fetch(job, resume_token=probe.resume_token)
                fetch = result.metadata["fetch"]
                metadata_file = Path(job.workspace_path) / fetch["metadata_path"]
                serialized = metadata_file.read_text(encoding="utf-8")
                for artifact in (fetch["artifact"],):
                    resolved = (Path(job.workspace_path) / artifact["path"]).resolve()
                    self.assertTrue(resolved.is_relative_to(Path(job.temp_path).resolve()))
                self.assertTrue(metadata_file.resolve().is_relative_to(Path(job.temp_path).resolve()))
                self.assertNotIn("<article>", serialized)
                self.assertNotIn("Recommended articles", serialized)
                self.assertFalse(list(Path(job.output_path).iterdir()))

    def test_invalid_scheme_and_job_path_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = make_job(Path(directory), "file:///etc/passwd")
            with self.assertRaises(ProviderFailure) as invalid:
                ArticleProvider().probe(job)
            self.assertEqual(invalid.exception.kind, ProviderFailureKind.INVALID_URL)

            job.source_url = "https://example.test/story"
            outside = Path(directory) / "outside"
            outside.mkdir()
            job.temp_path = str(outside)
            provider = ArticleProvider()
            probe = provider.probe(job)
            with self.assertRaises(ProviderFailure) as escaped:
                provider.fetch(job, resume_token=probe.resume_token)
            self.assertEqual(escaped.exception.kind, ProviderFailureKind.FAILED)

    def test_core_pipeline_owns_state_and_same_job_resume_reuses_article(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/story")
                job_file = Path(job.workspace_path) / "job.json"
                job_file.write_text(job.to_json(), encoding="utf-8")

                first = run_article_pipeline(job, ArticleProvider(), job_file=job_file)
                self.assertTrue(first.success)
                self.assertEqual(first.job.current_state, JobState.DOWNLOADING)
                self.assertEqual(counts["/story"], 1)

                resumed_job = Job.from_json(job_file.read_text(encoding="utf-8"))
                second = run_article_pipeline(resumed_job, ArticleProvider(), job_file=job_file)

                self.assertTrue(second.success)
                self.assertEqual(second.job.current_state, JobState.DOWNLOADING)
                self.assertEqual(second.result.metadata["fetch"]["reuse"], "verified")
                self.assertEqual(second.result.metadata["fetch"]["artifact"]["sha256"], first.result.metadata["fetch"]["artifact"]["sha256"])
                self.assertEqual(second.result.metadata["fetch"]["artifact"]["path"], first.result.metadata["fetch"]["artifact"]["path"])
                self.assertEqual(counts["/story"], 1)

    def test_core_maps_invalid_auth_network_and_extraction_failures(self) -> None:
        routes = {
            "/missing": (404, b"Not found"),
            "/private": (403, LOGIN_HTML),
            "/antibot": (403, b"<html><body>Automated requests blocked. Complete the anti-bot challenge.</body></html>"),
            "/forbidden": (403, b"Forbidden"),
            "/throttled": (429, b"Too many requests"),
            "/down": (503, b"temporarily unavailable"),
            "/shell": (200, SHELL_HTML),
        }
        expected = {
            "/missing": (ErrorCode.URL_INVALID, JobState.URL_INVALID),
            "/private": (ErrorCode.AUTH_REQUIRED, JobState.AUTH_REQUIRED),
            "/antibot": (ErrorCode.PROVIDER_FAILED, JobState.PROVIDER_FAILED),
            "/forbidden": (ErrorCode.PROVIDER_FAILED, JobState.PROVIDER_FAILED),
            "/throttled": (ErrorCode.NETWORK_PAUSED, JobState.NETWORK_PAUSED),
            "/down": (ErrorCode.NETWORK_PAUSED, JobState.NETWORK_PAUSED),
            "/shell": (ErrorCode.PROVIDER_FAILED, JobState.PROVIDER_FAILED),
        }
        with tempfile.TemporaryDirectory() as directory:
            with article_server(routes) as (base_url, _counts):
                for index, (path, (expected_code, expected_state)) in enumerate(expected.items()):
                    with self.subTest(path=path):
                        job = make_job(Path(directory), f"{base_url}{path}", f"article-error-{index}")
                        job_file = Path(job.workspace_path) / "job.json"
                        job_file.write_text(job.to_json(), encoding="utf-8")

                        outcome = run_article_pipeline(job, ArticleProvider(retries=0), job_file=job_file)

                        self.assertFalse(outcome.success)
                        self.assertEqual(outcome.error.code, expected_code)
                        self.assertEqual(outcome.job.current_state, expected_state)

    def test_core_reprobe_after_source_change_fetches_new_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/one": (200, ARTICLE_HTML), "/two": (200, SHORT_ARTICLE_HTML)}) as (base_url, counts):
                job = make_job(Path(directory), f"{base_url}/one")
                job_file = Path(job.workspace_path) / "job.json"
                job_file.write_text(job.to_json(), encoding="utf-8")
                first = run_article_pipeline(job, ArticleProvider(), job_file=job_file)
                first_artifact = first.result.metadata["fetch"]["artifact"]

                first.job.source_url = f"{base_url}/two"
                job_file.write_text(first.job.to_json(), encoding="utf-8")
                second = run_article_pipeline(first.job, ArticleProvider(), job_file=job_file)

                second_artifact = second.result.metadata["fetch"]["artifact"]
                self.assertTrue(second.success)
                self.assertNotEqual(first_artifact["path"], second_artifact["path"])
                self.assertNotEqual(first_artifact["sha256"], second_artifact["sha256"])
                self.assertEqual(counts["/one"], 1)
                self.assertEqual(counts["/two"], 1)

    def test_article_cli_creates_a_scoped_job_and_returns_structured_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with article_server({"/story": (200, ARTICLE_HTML)}) as (base_url, counts):
                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = cli.main([
                        "article-run",
                        "--url", f"{base_url}/story",
                        "--workspace-root", directory,
                        "--job-id", "article-cli-test",
                    ])

                payload = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(payload["job"]["resolved_content_type"], "ARTICLE")
                self.assertEqual(payload["job"]["current_state"], "DOWNLOADING")
                self.assertEqual([artifact["role"] for artifact in payload["artifacts"]], ["article", "metadata"])
                self.assertEqual(counts["/story"], 1)


if __name__ == "__main__":
    unittest.main()
