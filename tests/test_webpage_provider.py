from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import patch

from src import cli
from src.core.job import ContentType, Job, JobState
from src.core.webpage_pipeline import run_webpage_pipeline
from src.output.paths import OutputWorkspace
from src.providers.base import ProviderFailure, ProviderFailureKind
from src.providers.webpage import ARTICLE_HTML, ARTICLE_MARKDOWN, ARTICLE_PDF, MANIFEST_NAME, WebpageProvider


PAGE_HTML = b"""<!doctype html><html lang="en"><head><title>Sample Webpage Capture</title><meta property="og:title" content="Sample Webpage Capture"><link rel="canonical" href="/canonical/sample"><style>body{font-family:Arial}</style></head><body><nav>Home</nav><main><article><h1>Sample Webpage Capture</h1><p>This public fixture contains enough readable prose to exercise the shared extraction path and preserve its original article content.</p><h2>Evidence</h2><p>The complete page is archived before Trafilatura extracts these paragraphs for Markdown output.</p><blockquote><p>Keep the saved source attached to every converted artifact.</p></blockquote><ul><li>First item with a useful detail.</li><li>Second item with another detail.</li></ul><p><a href="https://example.org/evidence">Evidence link</a></p><img data-sf-original-src="https://images.example.test/diagram.png" src="data:image/png;base64,aGVsbG8=" alt="Embedded fixture image"></article></main></body></html>"""
CHANGED_PAGE_HTML = PAGE_HTML.replace(b"Second item with another detail", b"Changed item with updated detail")
LOGIN_HTML = b"<!doctype html><html><head><title>Sign in</title></head><body><form><input type='password'><p>Please sign in to continue reading.</p></form></body></html>"
JS_SHELL_HTML = b"<!doctype html><html><head><title>Loading page</title></head><body><div id='root'>Loading...</div><script>document.querySelector('#root').textContent = 'The browser should have rendered this';</script></body></html>"
PDF_TEMPLATE = b"%PDF-1.4\n% capture-hash:{hash}\n1 0 obj << /Type /Catalog /Pages 2 0 R >>\n2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >>\n3 0 obj << /Type /Page /Parent 2 0 R >>\n%%EOF\n"


class _FixtureHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    counts: dict[tuple[str, str], int] = {}

    def _respond(self, method: str) -> None:
        route = self.routes.get(self.path, (404, b"not found"))
        self.counts[(method, self.path)] = self.counts.get((method, self.path), 0) + 1
        status, body = route
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method == "GET":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self._respond("HEAD")

    def do_GET(self) -> None:
        self._respond("GET")

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _Server:
    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.server.daemon_threads = True
        self.server.routes = routes
        self.server.counts = {}
        _FixtureHandler.routes = self.server.routes
        _FixtureHandler.counts = self.server.counts
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}", self.server.counts

    def __exit__(self, *_args) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _FakeRuntime:
    def __init__(self, root: Path, *, html: bytes = PAGE_HTML) -> None:
        self.single_file = root / "single-file-fixture"
        self.chrome = root / "chrome-fixture"
        self.single_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.chrome.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.single_file.chmod(0o755)
        self.chrome.chmod(0o755)
        self.html = html
        self.singlefile_count = 0
        self.chrome_count = 0
        self.singlefile_failure = False
        self.chrome_failure = False

    def run(self, args: list[str], **_kwargs: Any) -> CompletedProcess[str]:
        if args[0] == str(self.single_file):
            if "--version" in args:
                return CompletedProcess(args, 0, stdout="2.15.7\n", stderr="")
            self.singlefile_count += 1
            if self.singlefile_failure:
                return CompletedProcess(args, 1, stdout="", stderr="capture failed")
            output = Path(args[2])
            output.write_bytes(self.html)
            return CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == str(self.chrome):
            if "--version" in args:
                return CompletedProcess(args, 0, stdout="Google Chrome 154.0.8037.57\n", stderr="")
            self.chrome_count += 1
            if self.chrome_failure:
                return CompletedProcess(args, 1, stdout="", stderr="print failed")
            output_arg = next(argument for argument in args if argument.startswith("--print-to-pdf="))
            output = Path(output_arg.split("=", 1)[1])
            file_uri = args[-1]
            html_path = Path(file_uri.removeprefix("file://"))
            output.write_bytes(PDF_TEMPLATE.replace(b"{hash}", hashlib.sha256(html_path.read_bytes()).hexdigest().encode("ascii")))
            return CompletedProcess(args, 0, stdout="", stderr="")
        return CompletedProcess(args, 127, stdout="", stderr="unexpected executable")


def make_job(root: Path, url: str, job_id: str = "webpage-test", declared: ContentType | None = ContentType.WEBPAGE) -> tuple[Job, Path]:
    paths = OutputWorkspace.for_job(root, job_id).prepare()
    job = Job(
        job_id=job_id,
        source_url=url,
        declared_content_type=declared,
        resolved_content_type=declared or ContentType.UNKNOWN,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
    )
    job_file = paths.job_dir / "job.json"
    job_file.write_text(job.to_json(), encoding="utf-8")
    return job, job_file


def provider_for(root: Path, runtime: _FakeRuntime, *, retries: int = 0) -> WebpageProvider:
    return WebpageProvider(
        single_file_executable=runtime.single_file,
        chrome_executable=runtime.chrome,
        timeout_seconds=1,
        retries=retries,
        retry_backoff_seconds=0,
        command_runner=runtime.run,
    )


def fetch(provider: WebpageProvider, job: Job):
    probe = provider.probe(job)
    return provider.fetch(job, resume_token=probe.resume_token)


class WebpageProviderTests(unittest.TestCase):
    def test_probe_is_offline_and_provider_never_mutates_job_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                before = job.to_json()
                provider = provider_for(root, runtime)

                probe = provider.probe(job)

                self.assertEqual(job.to_json(), before)
                self.assertEqual(job.current_state, JobState.QUEUED)
                self.assertEqual(probe.metadata["probe"]["content_type"], "WEBPAGE")
                self.assertEqual(probe.metadata["probe"]["network_fetch_performed"], False)
                self.assertEqual(counts, {})

    def test_writes_three_artifacts_and_manifest_with_hashes_and_temp_containment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                result = fetch(provider_for(root, runtime), job)
                temp = Path(job.temp_path)
                paths = {ARTICLE_HTML: temp / ARTICLE_HTML, ARTICLE_MARKDOWN: temp / ARTICLE_MARKDOWN, ARTICLE_PDF: temp / ARTICLE_PDF}
                roles = {artifact.role: artifact for artifact in result.artifacts}
                manifest = json.loads((temp / MANIFEST_NAME).read_text(encoding="utf-8"))

                self.assertEqual(set(roles), {"html", "markdown", "pdf", "manifest"})
                self.assertTrue(manifest["logical_content_deliverable"]["complete"])
                self.assertEqual(manifest["source"]["url"], job.source_url)
                self.assertEqual(set(manifest["artifact_hashes"]), {"html", "markdown", "pdf"})
                self.assertIn("Sample Webpage Capture", paths[ARTICLE_MARKDOWN].read_text(encoding="utf-8"))
                self.assertIn("## Evidence", paths[ARTICLE_MARKDOWN].read_text(encoding="utf-8"))
                self.assertIn("![Embedded fixture image](https://images.example.test/diagram.png)", paths[ARTICLE_MARKDOWN].read_text(encoding="utf-8"))
                stage_for_file = {ARTICLE_HTML: "html", ARTICLE_MARKDOWN: "markdown", ARTICLE_PDF: "pdf"}
                for role, path in paths.items():
                    record = manifest["stages"][stage_for_file[role]]
                    record = record["artifact"]
                    data = path.read_bytes()
                    self.assertTrue(path.resolve().is_relative_to(temp.resolve()))
                    self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])
                    self.assertEqual(len(data), record["size"])
                    self.assertEqual(record["source_url"], job.source_url)
                    self.assertTrue(record["engine_version"])
                self.assertEqual(manifest["stages"]["pdf"]["page_count"], 1)
                self.assertFalse(list(Path(job.output_path).iterdir()))
                self.assertEqual([path.name for path in (root / "workspace").iterdir()], ["job-webpage-test"])
                runtime_files = list((temp / ".webpage-runtime").rglob("*"))
                self.assertTrue(all(path.resolve().is_relative_to(temp.resolve()) for path in runtime_files))

    def test_same_job_resume_reuses_verified_html_markdown_and_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                first = run_webpage_pipeline(job, provider, job_file=job_file)
                second = run_webpage_pipeline(first.job, provider, job_file=job_file)

                self.assertTrue(first.success)
                self.assertTrue(second.success)
                self.assertEqual(runtime.singlefile_count, 1)
                self.assertEqual(runtime.chrome_count, 1)
                self.assertEqual(second.result.metadata["fetch"]["resume"], {"html": "verified", "markdown": "verified", "pdf": "verified"})
                self.assertEqual(second.job.current_state, JobState.DOWNLOADING)

    def test_html_resume_skips_singlefile_and_regenerates_missing_downstream_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                first = fetch(provider, job)
                Path(job.temp_path, ARTICLE_MARKDOWN).unlink()
                Path(job.temp_path, ARTICLE_PDF).unlink()
                second = fetch(provider, job)

                self.assertEqual(runtime.singlefile_count, 1)
                self.assertEqual(runtime.chrome_count, 2)
                self.assertEqual(second.metadata["fetch"]["resume"], {"html": "verified", "markdown": "generated", "pdf": "generated"})
                self.assertEqual(first.metadata["fetch"]["artifact_hashes"]["html"], second.metadata["fetch"]["artifact_hashes"]["html"])

    def test_markdown_resume_skips_html_and_extraction_when_markdown_hash_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                first = fetch(provider, job)
                markdown_hash = first.metadata["fetch"]["artifact_hashes"]["markdown"]
                Path(job.temp_path, ARTICLE_PDF).unlink()
                with patch("src.providers.webpage.extract_trafilatura_markdown", wraps=__import__("src.providers.webpage", fromlist=["extract_trafilatura_markdown"]).extract_trafilatura_markdown) as extract:
                    second = fetch(provider, job)

                self.assertEqual(extract.call_count, 0)
                self.assertEqual(runtime.singlefile_count, 1)
                self.assertEqual(runtime.chrome_count, 2)
                self.assertEqual(second.metadata["fetch"]["resume"], {"html": "verified", "markdown": "verified", "pdf": "generated"})
                self.assertEqual(second.metadata["fetch"]["artifact_hashes"]["markdown"], markdown_hash)

    def test_pdf_resume_reuses_both_upstream_artifacts_and_detects_modified_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                first = fetch(provider, job)
                first_pdf_hash = first.metadata["fetch"]["artifact_hashes"]["pdf"]
                Path(job.temp_path, ARTICLE_PDF).write_bytes(b"corrupted")
                second = fetch(provider, job)

                self.assertEqual(runtime.singlefile_count, 1)
                self.assertEqual(runtime.chrome_count, 2)
                self.assertEqual(second.metadata["fetch"]["resume"], {"html": "verified", "markdown": "verified", "pdf": "generated"})
                self.assertEqual(second.metadata["fetch"]["artifact_hashes"]["pdf"], first_pdf_hash)

    def test_changed_html_hash_invalidates_markdown_and_pdf_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                first = fetch(provider, job)
                runtime.html = CHANGED_PAGE_HTML
                Path(job.temp_path, ARTICLE_HTML).write_bytes(b"tampered html; reject cache")
                second = fetch(provider, job)

                self.assertEqual(runtime.singlefile_count, 2)
                self.assertEqual(runtime.chrome_count, 2)
                self.assertNotEqual(first.metadata["fetch"]["artifact_hashes"]["html"], second.metadata["fetch"]["artifact_hashes"]["html"])
                self.assertNotEqual(first.metadata["fetch"]["artifact_hashes"]["markdown"], second.metadata["fetch"]["artifact_hashes"]["markdown"])
                self.assertNotEqual(first.metadata["fetch"]["artifact_hashes"]["pdf"], second.metadata["fetch"]["artifact_hashes"]["pdf"])
                manifest = json.loads(Path(job.temp_path, MANIFEST_NAME).read_text(encoding="utf-8"))
                self.assertEqual(manifest["stages"]["pdf"]["input_html_sha256"], second.metadata["fetch"]["artifact_hashes"]["html"])

    def test_modified_markdown_invalidates_only_markdown_not_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                first = fetch(provider, job)
                Path(job.temp_path, ARTICLE_MARKDOWN).write_text("tampered", encoding="utf-8")
                second = fetch(provider, job)

                self.assertEqual(runtime.singlefile_count, 1)
                self.assertEqual(runtime.chrome_count, 1)
                self.assertNotEqual(first.metadata["fetch"]["artifact_hashes"]["markdown"], hashlib.sha256(b"tampered").hexdigest())
                self.assertEqual(second.metadata["fetch"]["resume"], {"html": "verified", "markdown": "generated", "pdf": "verified"})

    def test_404_maps_to_url_invalid_and_auth_wall_maps_to_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            routes = {
                "/missing": (404, b"missing"),
                "/wall": (200, LOGIN_HTML),
                "/permission403": (403, b"<html><body>Permission required. Please sign in.</body></html>"),
                "/antibot403": (403, b"<html><body>Automated requests blocked. Complete the anti-bot challenge.</body></html>"),
                "/forbidden403": (403, b"Forbidden"),
                "/throttled": (429, b"Too many requests"),
            }
            with _Server(routes) as (base_url, counts):
                missing, missing_file = make_job(root / "workspace", f"{base_url}/missing", "missing-job")
                invalid = run_webpage_pipeline(missing, provider_for(root, runtime), job_file=missing_file)
                runtime.html = LOGIN_HTML
                wall, wall_file = make_job(root / "workspace", f"{base_url}/wall", "wall-job")
                auth = run_webpage_pipeline(wall, provider_for(root, runtime), job_file=wall_file)
                permission, permission_file = make_job(root / "workspace", f"{base_url}/permission403", "permission-job")
                permission_result = run_webpage_pipeline(permission, provider_for(root, runtime), job_file=permission_file)
                antibot, antibot_file = make_job(root / "workspace", f"{base_url}/antibot403", "antibot-job")
                antibot_result = run_webpage_pipeline(antibot, provider_for(root, runtime), job_file=antibot_file)
                forbidden, forbidden_file = make_job(root / "workspace", f"{base_url}/forbidden403", "forbidden-job")
                forbidden_result = run_webpage_pipeline(forbidden, provider_for(root, runtime), job_file=forbidden_file)
                throttled, throttled_file = make_job(root / "workspace", f"{base_url}/throttled", "throttled-job")
                throttle_result = run_webpage_pipeline(throttled, provider_for(root, runtime), job_file=throttled_file)

                self.assertFalse(invalid.success)
                self.assertEqual(invalid.error.code.value, "URL_INVALID")
                self.assertFalse(auth.success)
                self.assertEqual(auth.error.code.value, "AUTH_REQUIRED")
                self.assertEqual(permission_result.error.code.value, "AUTH_REQUIRED")
                self.assertEqual(antibot_result.error.code.value, "PROVIDER_FAILED")
                self.assertEqual(forbidden_result.error.code.value, "PROVIDER_FAILED")
                self.assertEqual(throttle_result.error.code.value, "NETWORK_PAUSED")
                self.assertEqual(runtime.singlefile_count, 1)
                self.assertEqual(counts[("GET", "/permission403")], 1)
                self.assertEqual(counts[("GET", "/antibot403")], 1)
                self.assertEqual(counts[("GET", "/forbidden403")], 1)
                self.assertFalse(Path(missing.temp_path, ARTICLE_HTML).exists())

    def test_network_retry_exhaustion_maps_to_network_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({}) as (_base_url, _counts):
                # A just-closed ephemeral port provides a real anonymous connection failure.
                import socket
                probe_socket = socket.socket()
                probe_socket.bind(("127.0.0.1", 0))
                port = probe_socket.getsockname()[1]
                probe_socket.close()
                job, job_file = make_job(root / "workspace", f"http://127.0.0.1:{port}/unavailable")
                outcome = run_webpage_pipeline(job, provider_for(root, runtime, retries=1), job_file=job_file)

                self.assertFalse(outcome.success)
                self.assertEqual(outcome.error.code.value, "NETWORK_PAUSED")
                self.assertEqual(runtime.singlefile_count, 0)
                self.assertNotIn(ARTICLE_PDF, {path.name for path in Path(job.temp_path).iterdir()})

    def test_singlefile_markdown_and_chrome_failures_never_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML), "/shell": (200, JS_SHELL_HTML)}) as (base_url, _counts):
                singlefile_job, singlefile_file = make_job(root / "workspace", f"{base_url}/page", "singlefile-fails")
                runtime.singlefile_failure = True
                singlefile_outcome = run_webpage_pipeline(singlefile_job, provider_for(root, runtime), job_file=singlefile_file)
                self.assertEqual(singlefile_outcome.error.code.value, "PROVIDER_FAILED")
                self.assertFalse(singlefile_outcome.success)

                runtime.singlefile_failure = False
                runtime.html = JS_SHELL_HTML
                shell_job, shell_file = make_job(root / "workspace", f"{base_url}/shell", "dynamic-shell")
                shell_outcome = run_webpage_pipeline(shell_job, provider_for(root, runtime), job_file=shell_file)
                self.assertEqual(shell_outcome.error.code.value, "PROVIDER_FAILED")
                self.assertFalse(Path(shell_job.temp_path, ARTICLE_PDF).exists())

                runtime.html = PAGE_HTML
                runtime.chrome_failure = True
                pdf_job, pdf_file = make_job(root / "workspace", f"{base_url}/page", "chrome-fails")
                pdf_outcome = run_webpage_pipeline(pdf_job, provider_for(root, runtime), job_file=pdf_file)
                self.assertEqual(pdf_outcome.error.code.value, "PROVIDER_FAILED")
                self.assertEqual(pdf_outcome.job.current_state, JobState.PROVIDER_FAILED)
                self.assertFalse(pdf_outcome.success)
                manifest = json.loads(Path(pdf_job.temp_path, MANIFEST_NAME).read_text(encoding="utf-8"))
                self.assertFalse(manifest["logical_content_deliverable"]["complete"])
                self.assertNotIn("pdf", manifest["stages"])

    def test_invalid_pdf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            runtime.run = lambda args, **kwargs: CompletedProcess(args, 0, stdout="", stderr="") if args[0] == str(runtime.single_file) and "--version" in args else CompletedProcess(args, 0, stdout="", stderr="") if args[0] == str(runtime.single_file) else CompletedProcess(args, 0, stdout="", stderr="")
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                original_run = runtime.run

                def invalid_pdf(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
                    if args[0] == str(runtime.chrome) and "--version" not in args:
                        output = next(argument.split("=", 1)[1] for argument in args if argument.startswith("--print-to-pdf="))
                        Path(output).write_bytes(b"not a pdf")
                        return CompletedProcess(args, 0, stdout="", stderr="")
                    return original_run(args, **kwargs)

                provider.command_runner = invalid_pdf
                with self.assertRaises(ProviderFailure) as caught:
                    fetch(provider, job)
                self.assertEqual(caught.exception.kind, ProviderFailureKind.FAILED)
                self.assertFalse(Path(job.temp_path, ARTICLE_PDF).exists())

    def test_source_identity_change_does_not_reuse_previous_job_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML), "/other": (200, CHANGED_PAGE_HTML)}) as (base_url, _counts):
                job, job_file = make_job(root / "workspace", f"{base_url}/page")
                provider = provider_for(root, runtime)
                first = run_webpage_pipeline(job, provider, job_file=job_file)
                job.source_url = f"{base_url}/other"
                runtime.html = CHANGED_PAGE_HTML
                second = run_webpage_pipeline(first.job, provider, job_file=job_file)

                self.assertTrue(second.success)
                self.assertEqual(runtime.singlefile_count, 2)
                self.assertNotEqual(first.result.metadata["fetch"]["source_identity"], second.result.metadata["fetch"]["source_identity"])
                manifest = json.loads(Path(job.temp_path, MANIFEST_NAME).read_text(encoding="utf-8"))
                self.assertEqual(manifest["source_url"], job.source_url)

    def test_temp_path_escape_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                job, _job_file = make_job(root / "workspace", f"{base_url}/page")
                job.temp_path = str(root / "outside")
                with self.assertRaises(ProviderFailure) as caught:
                    fetch(provider_for(root, runtime), job)
                self.assertEqual(caught.exception.kind, ProviderFailureKind.FAILED)

    def test_cli_creates_scoped_job_and_returns_all_four_artifact_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _FakeRuntime(root)
            with _Server({"/page": (200, PAGE_HTML)}) as (base_url, _counts):
                stdout = __import__("io").StringIO()
                stderr = __import__("io").StringIO()
                with patch("src.cli.WebpageProvider", return_value=provider_for(root, runtime)):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = cli.main(["webpage-run", "--url", f"{base_url}/page", "--workspace-root", str(root / "workspace"), "--job-id", "cli-webpage"])

                payload = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(payload["job"]["resolved_content_type"], "WEBPAGE")
                self.assertEqual({item["role"] for item in payload["artifacts"]}, {"html", "markdown", "pdf", "manifest"})
                self.assertTrue(all(Path(payload["job"]["workspace"]["path"], item["relative_path"]).exists() for item in payload["artifacts"]))


if __name__ == "__main__":
    unittest.main()
