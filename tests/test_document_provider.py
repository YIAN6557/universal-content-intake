from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from src.core.document_pipeline import run_document_pipeline
from src.core.job import ContentType, Job, JobState
from src.output.paths import OutputWorkspace
from src.providers.base import ProviderFailure, ProviderFailureKind
from src.providers.document import DocumentProvider, MANIFEST_NAME, _rclone_url, _source_from_google_url, validate_document


def _pdf_bytes(text: str = "Document Sample") -> bytes:
    body = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii")
    stream = b"BT /F1 12 Tf 72 720 Td (" + body + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    return bytes(output)


def _docx_bytes(text: str = "Office fixture") -> bytes:
    from io import BytesIO

    target = BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'></Types>")
        archive.writestr("word/document.xml", f"<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
        archive.writestr("_rels/.rels", "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'></Relationships>")
    return target.getvalue()


def _ooxml_bytes(kind: str) -> bytes:
    from io import BytesIO

    main_xml = {
        "docx": ("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body/></w:document>"),
        "xlsx": ("xl/workbook.xml", "<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheets/></workbook>"),
        "pptx": ("ppt/presentation.xml", "<p:presentation xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main'><p:sldIdLst/></p:presentation>"),
    }[kind]
    target = BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'></Types>")
        archive.writestr(*main_xml)
        archive.writestr("_rels/.rels", "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'></Relationships>")
    return target.getvalue()


PDF_BYTES = _pdf_bytes()
DOCX_BYTES = _docx_bytes()


class _Routes:
    def __init__(self) -> None:
        self.values: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    routes: _Routes

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond(body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._respond(body=True)

    def _respond(self, *, body: bool) -> None:
        route = self.path.split("?", 1)[0]
        with self.routes.lock:
            self.routes.counts[route] = self.routes.counts.get(route, 0) + 1
            status, payload, headers = self.routes.values.get(route, (404, b"missing", {"Content-Type": "text/plain"}))
        total_size = len(payload)
        response_status = status
        range_header = self.headers.get("Range")
        if body and range_header and status == 200:
            start = int(range_header.removeprefix("bytes=").split("-", 1)[0])
            if start < len(payload):
                end = total_size - 1
                payload = payload[start:]
                response_status = 206
        self.send_response(response_status)
        for key, value in headers.items():
            if key.lower() != "content-length":
                self.send_header(key, value)
        if response_status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body and response_status not in {204, 304}:
            disconnect_after = int(headers.get("X-Disconnect-After", "0"))
            if disconnect_after and len(payload) > disconnect_after:
                self.wfile.write(payload[:disconnect_after])
                self.close_connection = True
            else:
                self.wfile.write(payload)


class _Server:
    def __init__(self, values: dict[str, tuple[int, bytes, dict[str, str]]]) -> None:
        self.routes = _Routes()
        self.routes.values.update(values)
        handler = type("RouteHandler", (_Handler,), {"routes": self.routes})
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> tuple[str, _Routes]:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}", self.routes

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _FakeTools:
    def __init__(self, root: Path) -> None:
        self.bin = root / "fake-bin"
        self.bin.mkdir(parents=True)
        # Exercise real curl HTTP/header behavior; cloud CLIs remain isolated fakes.
        self.curl = Path(shutil.which("curl") or "/usr/bin/curl")
        self.aria2 = self._exe("aria2c")
        self.rclone = self._exe("rclone")
        self.gdown = self._exe("gdown")
        self.calls: list[list[str]] = []
        self.remote_lines = ""
        self.remote_stat: dict[str, Any] = {}
        self.remote_listing: list[dict[str, Any]] = []
        self.google_listing: list[dict[str, str]] = []
        self.google_names: dict[str, str] = {}
        self.google_payloads: dict[str, bytes] = {}
        self.fail_google_ids: set[str] = set()
        self.always_fail_google_ids: set[str] = set()
        self.failed_once_ids: set[str] = set()
        self.aria2_payload = PDF_BYTES
        self.aria2_partial_mode = False
        self.aria2_calls: list[list[str]] = []

    def _exe(self, name: str) -> Path:
        path = self.bin / name
        path.touch()
        return path

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(part) for part in args]
        self.calls.append(argv)
        executable = Path(argv[0]).stem  # curl.exe on Windows
        if executable == "curl":
            return subprocess.run(args, **kwargs)
        if executable == "aria2c":
            self.aria2_calls.append(argv)
            if "--version" in argv:
                return subprocess.CompletedProcess(args, 0, "aria2 version test-1\n", "")
            destination = Path(argv[argv.index("--dir") + 1]) / argv[argv.index("--out") + 1]
            if self.aria2_partial_mode and len(self.aria2_calls) == 1:
                destination.write_bytes(self.aria2_payload[: len(self.aria2_payload) // 2])
                destination.with_name(destination.name + ".aria2").write_text("control", encoding="utf-8")
                return subprocess.CompletedProcess(args, 1, "", "interrupted")
            if "--continue=false" in argv:
                destination.write_bytes(self.aria2_payload)
            else:
                with destination.open("ab") as stream:
                    stream.write(self.aria2_payload[destination.stat().st_size :])
            destination.with_name(destination.name + ".aria2").unlink(missing_ok=True)
            return subprocess.CompletedProcess(args, 0, "complete\n", "")
        if executable == "rclone":
            command = argv[1]
            if command == "version":
                return subprocess.CompletedProcess(args, 0, "rclone v-test\n", "")
            if command == "listremotes":
                return subprocess.CompletedProcess(args, 0, self.remote_lines, "")
            if command == "lsjson" and "--stat" in argv:
                stat = {key: value for key, value in self.remote_stat.items() if key != "payload"}
                return subprocess.CompletedProcess(args, 0, json.dumps(stat), "")
            if command == "lsjson":
                listing = [{key: value for key, value in item.items() if key != "payload"} for item in self.remote_listing]
                return subprocess.CompletedProcess(args, 0, json.dumps(listing), "")
            if command == "backend" and "copyid" in argv:
                file_id = argv[argv.index("copyid") + 2]
                destination = Path(argv[argv.index("copyid") + 3])
                destination.write_bytes(self.google_payloads.get(file_id, DOCX_BYTES))
                return subprocess.CompletedProcess(args, 0, "", "")
            if command == "copyto":
                destination = Path(argv[3])
                item = next((value for value in self.remote_listing if str(value.get("Path")) in argv[2]), self.remote_stat)
                destination.write_bytes(item.get("payload", self.remote_stat.get("payload", PDF_BYTES)))
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 2, "", f"unexpected rclone command: {command}")
        if executable == "gdown":
            if "--version" in argv:
                return subprocess.CompletedProcess(args, 0, "gdown 6.4.0 at fake\n", "")
            url = argv[1]
            if "--json" in argv:
                if "/folders/" in url:
                    value = self.google_listing
                else:
                    file_id = (parse_qs(urlsplit(url).query).get("id") or [None])[0]
                    if not file_id:
                        parts = [part for part in urlsplit(url).path.split("/") if part]
                        if "d" in parts and parts.index("d") + 1 < len(parts):
                            file_id = parts[parts.index("d") + 1]
                    value = [{"url": url, "path": self.google_names.get(file_id or "", f"{file_id or 'public'}.pdf")}]
                return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
            file_id = (parse_qs(urlsplit(url).query).get("id") or [None])[0]
            if not file_id:
                parts = [part for part in urlsplit(url).path.split("/") if part]
                file_id = parts[parts.index("d") + 1] if "d" in parts and parts.index("d") + 1 < len(parts) else ""
            if file_id in self.fail_google_ids:
                if file_id not in self.failed_once_ids:
                    self.failed_once_ids.add(file_id)
                    return subprocess.CompletedProcess(args, 1, "", "download temporarily unavailable")
            if file_id in self.always_fail_google_ids:
                return subprocess.CompletedProcess(args, 1, "", "HTTPSConnectionPool: SSL EOF; Max retries exceeded")
            target = Path(argv[argv.index("--output") + 1])
            target.write_bytes(self.google_payloads.get(file_id or "", PDF_BYTES))
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 127, "", f"unexpected executable: {executable}")


def _job(root: Path, url: str, job_id: str = "document-test") -> tuple[Job, Path]:
    paths = OutputWorkspace.for_job(root, job_id).prepare()
    job = Job(
        job_id=job_id,
        source_url=url,
        declared_content_type=ContentType.DOCUMENT,
        resolved_content_type=ContentType.DOCUMENT,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"resolution_origin": "DECLARED", "declared_type_locked": True},
    )
    job_file = paths.job_dir / "job.json"
    job_file.write_text(job.to_json(), encoding="utf-8")
    return job, job_file


def _provider(fake: _FakeTools, **options: Any) -> DocumentProvider:
    return DocumentProvider(
        curl_executable=fake.curl,
        aria2_executable=fake.aria2,
        rclone_executable=fake.rclone,
        gdown_executable=fake.gdown,
        timeout_seconds=5,
        retries=0,
        retry_backoff_seconds=0,
        command_runner=fake.run,
        **options,
    )


class DocumentProviderTests(unittest.TestCase):
    def test_http_pdf_redirect_generates_manifest_and_hashes_inside_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            routes = {
                "/redirect": (302, b"", {"Location": "/report", "Content-Length": "0"}),
                "/report": (200, PDF_BYTES, {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="weekly report.pdf"', "ETag": '"revision-1"', "Accept-Ranges": "bytes"}),
            }
            with _Server(routes) as (base_url, counts):
                job, job_file = _job(root / "workspace", f"{base_url}/redirect")
                outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)

                self.assertTrue(outcome.success)
                self.assertEqual(outcome.job.current_state, JobState.DOWNLOADING)
                artifacts = {item.role: item for item in outcome.result.artifacts}
                self.assertEqual(set(artifacts), {"document_file", "manifest"})
                document = Path(job.workspace_path) / artifacts["document_file"].relative_path
                manifest_path = Path(job.workspace_path) / artifacts["manifest"].relative_path
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                record = manifest["artifacts"][0]
                self.assertEqual(document.name, "weekly report.pdf")
                self.assertEqual(document.stat().st_size, len(PDF_BYTES))
                self.assertEqual(record["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
                self.assertEqual(record["source_url"], job.source_url)
                self.assertEqual(record["resolved_url"], f"{base_url}/report")
                self.assertEqual(record["validation"]["page_count"], 1)
                self.assertTrue(document.resolve().is_relative_to(Path(job.temp_path).resolve()))
                self.assertEqual(counts.counts.get("/report"), 2)  # HEAD and GET
                self.assertFalse(list(Path(job.output_path).iterdir()))
                self.assertEqual(sorted(path.name for path in (root / "workspace").iterdir()), ["job-document-test"])

    def test_completed_artifact_is_reused_after_hash_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            with _Server({"/file.pdf": (200, PDF_BYTES, {"Content-Type": "application/pdf", "ETag": "stable"})}) as (base_url, counts):
                job, job_file = _job(root / "workspace", f"{base_url}/file.pdf")
                provider = _provider(fake)
                first = run_document_pipeline(job, provider, job_file=job_file)
                gets_after_first = counts.counts["/file.pdf"]
                second = run_document_pipeline(first.job, provider, job_file=job_file)

                self.assertTrue(second.success)
                self.assertEqual(counts.counts["/file.pdf"], gets_after_first + 1)  # only a fresh HEAD
                self.assertEqual(second.result.metadata["fetch"]["resume"], "verified")
                self.assertEqual(second.result.metadata["fetch"]["reuse"], "verified")
                self.assertTrue(second.result.metadata["fetch"]["remote_identity"]["freshness_verified"])

    def test_rclone_same_file_id_and_revision_reuses_only_after_freshness_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "drive: drive\n"
            fake.remote_stat = {
                "Name": "source.pdf",
                "Size": len(PDF_BYTES),
                "ModTime": "2026-09-24T00:00:00Z",
                "MimeType": "application/pdf",
                "ID": "same-file-id",
                "RevisionID": "revision-1",
                "payload": PDF_BYTES,
            }
            job, job_file = _job(root / "workspace", "rclone://drive/docs/source.pdf")
            provider = _provider(fake)
            first = run_document_pipeline(job, provider, job_file=job_file)
            copy_count = sum(1 for call in fake.calls if Path(call[0]).name == "rclone" and len(call) > 1 and call[1] == "copyto")
            second = run_document_pipeline(first.job, provider, job_file=job_file)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(sum(1 for call in fake.calls if Path(call[0]).name == "rclone" and len(call) > 1 and call[1] == "copyto"), copy_count)
            self.assertEqual(second.result.metadata["fetch"]["reuse"], "verified")
            identity = second.result.metadata["fetch"]["artifacts"][0]["remote_identity"]
            self.assertEqual(identity["fields"]["id"], "same-file-id")
            self.assertEqual(identity["fields"]["revision_id"], "revision-1")
            self.assertEqual(identity["freshness_fields"], ["revision_id"])

    def test_rclone_changed_revision_for_same_file_id_refetches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "drive: drive\n"
            fake.remote_stat = {"Name": "source.pdf", "Size": len(PDF_BYTES), "ModTime": "2026-09-24T00:00:00Z", "MimeType": "application/pdf", "ID": "same-file-id", "RevisionID": "revision-1", "payload": PDF_BYTES}
            job, job_file = _job(root / "workspace", "rclone://drive/docs/source.pdf")
            provider = _provider(fake)
            first = run_document_pipeline(job, provider, job_file=job_file)
            first_path = first.result.metadata["fetch"]["artifacts"][0]["path"]
            fake.remote_stat["RevisionID"] = "revision-2"
            second = run_document_pipeline(first.job, provider, job_file=job_file)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(second.result.metadata["fetch"]["reuse"], "downloaded")
            self.assertNotEqual(second.result.metadata["fetch"]["artifacts"][0]["path"], first_path)
            self.assertEqual(second.result.metadata["fetch"]["artifacts"][0]["remote_identity"]["fields"]["revision_id"], "revision-2")

    def test_rclone_changed_modified_time_for_same_file_id_refetches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "vault: local\n"
            fake.remote_stat = {"Name": "source.pdf", "Size": len(PDF_BYTES), "ModTime": "2026-09-24T00:00:00Z", "MimeType": "application/pdf", "ID": "same-file-id", "payload": PDF_BYTES}
            job, job_file = _job(root / "workspace", "rclone://vault/docs/source.pdf")
            provider = _provider(fake)
            first = run_document_pipeline(job, provider, job_file=job_file)
            first_hash = first.result.metadata["fetch"]["artifacts"][0]["sha256"]
            copy_count = sum(1 for call in fake.calls if Path(call[0]).name == "rclone" and len(call) > 1 and call[1] == "copyto")
            fake.remote_stat["ModTime"] = "2026-09-24T00:01:00Z"
            second = run_document_pipeline(first.job, provider, job_file=job_file)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(second.result.metadata["fetch"]["artifacts"][0]["sha256"], first_hash)
            self.assertEqual(sum(1 for call in fake.calls if Path(call[0]).name == "rclone" and len(call) > 1 and call[1] == "copyto"), copy_count + 1)
            self.assertEqual(second.result.metadata["fetch"]["artifacts"][0]["remote_identity"]["freshness_fields"], ["modified_time", "size"])

    def test_corrupt_cached_artifact_is_invalidated_and_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            with _Server({"/file.pdf": (200, PDF_BYTES, {"Content-Type": "application/pdf", "ETag": "stable"})}) as (base_url, counts):
                job, job_file = _job(root / "workspace", f"{base_url}/file.pdf")
                provider = _provider(fake)
                first = run_document_pipeline(job, provider, job_file=job_file)
                first_artifact = next(item for item in first.result.artifacts if item.role == "document_file")
                first_path = Path(job.workspace_path) / first_artifact.relative_path
                first_path.write_bytes(b"tampered")
                second = run_document_pipeline(first.job, provider, job_file=job_file)

                self.assertTrue(second.success)
                self.assertEqual(counts.counts["/file.pdf"], 4)  # two HEAD/GET pairs
                self.assertNotEqual(Path(job.workspace_path, next(item for item in second.result.artifacts if item.role == "document_file").relative_path), first_path)

    def test_html_error_and_wrong_extension_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            html = b"<!doctype html><html><body>Sign in to continue</body></html>"
            routes = {
                "/login.pdf": (200, html, {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="login.pdf"'}),
                "/wrong": (200, PDF_BYTES, {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="report.docx"'}),
            }
            with _Server(routes) as (base_url, _counts):
                login, login_file = _job(root / "workspace", f"{base_url}/login.pdf", "login-job")
                login_result = run_document_pipeline(login, _provider(fake), job_file=login_file)
                wrong, wrong_file = _job(root / "workspace", f"{base_url}/wrong", "wrong-job")
                wrong_result = run_document_pipeline(wrong, _provider(fake), job_file=wrong_file)

                self.assertEqual(login_result.error.code.value, "AUTH_REQUIRED")
                self.assertEqual(wrong_result.error.code.value, "PROVIDER_FAILED")
                self.assertFalse(list(Path(login.temp_path).rglob("*.pdf")))

    def test_content_disposition_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            headers = {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="../../escape.pdf"'}
            with _Server({"/file": (200, PDF_BYTES, headers)}) as (base_url, _counts):
                job, job_file = _job(root / "workspace", f"{base_url}/file")
                outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)
                self.assertFalse(outcome.success)
                self.assertEqual(outcome.error.code.value, "PROVIDER_FAILED")
                self.assertFalse((root / "escape.pdf").exists())

    def test_404_auth_wall_and_network_failure_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            with _Server({
                "/missing": (404, b"deleted", {"Content-Type": "text/plain"}),
                "/login": (200, b"<html><body>Please sign in to view this document</body></html>", {"Content-Type": "text/html"}),
                "/throttled.pdf": (429, b"Too many requests", {"Content-Type": "application/pdf"}),
            }) as (base_url, _counts):
                missing, missing_file = _job(root / "workspace", f"{base_url}/missing", "missing-job")
                missing_result = run_document_pipeline(missing, _provider(fake), job_file=missing_file)
                login, login_file = _job(root / "workspace", f"{base_url}/login", "login-job")
                login_result = run_document_pipeline(login, _provider(fake), job_file=login_file)
                throttled, throttled_file = _job(root / "workspace", f"{base_url}/throttled.pdf", "throttled-job")
                throttle_result = run_document_pipeline(throttled, _provider(fake), job_file=throttled_file)
            probe_socket = socket.socket()
            probe_socket.bind(("127.0.0.1", 0))
            unused_port = probe_socket.getsockname()[1]
            probe_socket.close()
            unavailable, unavailable_file = _job(root / "workspace", f"http://127.0.0.1:{unused_port}/file.pdf", "network-job")
            unavailable_result = run_document_pipeline(unavailable, _provider(fake), job_file=unavailable_file)

            self.assertEqual(missing_result.error.code.value, "URL_INVALID")
            self.assertEqual(login_result.error.code.value, "AUTH_REQUIRED")
            self.assertEqual(throttle_result.error.code.value, "NETWORK_PAUSED")
            self.assertEqual(unavailable_result.error.code.value, "NETWORK_PAUSED")

    def test_403_permission_wall_and_antibot_refusal_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            with _Server({
                "/permission": (403, b"<html><body>Permission required. Please sign in.</body></html>", {"Content-Type": "text/html"}),
                "/antibot": (403, b"<html><body>Automated requests blocked. Complete the anti-bot challenge.</body></html>", {"Content-Type": "text/html"}),
                "/forbidden": (403, b"Forbidden", {"Content-Type": "text/plain"}),
            }) as (base_url, _counts):
                permission, permission_file = _job(root / "workspace", f"{base_url}/permission", "permission-job")
                permission_result = run_document_pipeline(permission, _provider(fake), job_file=permission_file)
                refused, refused_file = _job(root / "workspace", f"{base_url}/antibot", "antibot-job")
                refused_result = run_document_pipeline(refused, _provider(fake), job_file=refused_file)
                forbidden, forbidden_file = _job(root / "workspace", f"{base_url}/forbidden", "forbidden-job")
                forbidden_result = run_document_pipeline(forbidden, _provider(fake), job_file=forbidden_file)

            self.assertEqual(permission_result.error.code.value, "AUTH_REQUIRED")
            self.assertEqual(refused_result.error.code.value, "PROVIDER_FAILED")
            self.assertEqual(forbidden_result.error.code.value, "PROVIDER_FAILED")

    def test_http_partial_symlink_is_rejected_without_writing_outside_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            outside = root / "outside.pdf"
            outside.write_bytes(b"must not change")
            with _Server({"/file.pdf": (200, PDF_BYTES, {"Content-Type": "application/pdf"})}) as (base_url, _counts):
                job, _job_file = _job(root / "workspace", f"{base_url}/file.pdf")
                provider = _provider(fake)
                probe = provider.probe(job)
                state_dir = Path(job.temp_path) / ".document-state" / probe.resume_token
                state_dir.mkdir(parents=True, exist_ok=True)
                (state_dir / "file.pdf.partial").symlink_to(outside)
                with self.assertRaises(ProviderFailure) as caught:
                    provider.fetch(job, resume_token=probe.resume_token)

            self.assertEqual(caught.exception.kind, ProviderFailureKind.FAILED)
            self.assertEqual(outside.read_bytes(), b"must not change")

    def test_html_wrong_mime_and_signature_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "real.pdf"
            path.write_bytes(PDF_BYTES)
            self.assertEqual(validate_document(path, content_type="application/pdf")["page_count"], 1)
            with self.assertRaises(ValueError):
                validate_document(path, content_type="text/html")
            path.write_bytes(b"<html><body>Login</body></html>")
            with self.assertRaises(PermissionError):
                validate_document(path, content_type="application/pdf")

    def test_aria2_partial_resume_uses_its_control_file_and_same_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.aria2_partial_mode = True
            with _Server({"/large.pdf": (200, PDF_BYTES, {"Content-Type": "application/pdf", "ETag": "resume-1", "Accept-Ranges": "bytes"})}) as (base_url, _counts):
                job, job_file = _job(root / "workspace", f"{base_url}/large.pdf")
                provider = _provider(fake, large_file_threshold_bytes=0)
                interrupted = run_document_pipeline(job, provider, job_file=job_file)
                self.assertEqual(interrupted.error.code.value, "NETWORK_PAUSED")
                state_dir = Path(job.temp_path) / ".document-state"
                control = next(state_dir.rglob("*.aria2"))
                partial = control.with_name(control.name.removesuffix(".aria2"))
                partial_hash = hashlib.sha256(partial.read_bytes()).hexdigest()
                resumed = run_document_pipeline(interrupted.job, provider, job_file=job_file)

                self.assertTrue(resumed.success)
                self.assertEqual(fake.aria2_calls[0][1], "--continue=false")
                self.assertIn("--continue=true", fake.aria2_calls[1])
                self.assertNotEqual(partial_hash, hashlib.sha256(PDF_BYTES).hexdigest())
                self.assertEqual(hashlib.sha256(next(Path(job.temp_path).rglob("*.pdf")).read_bytes()).hexdigest(), hashlib.sha256(PDF_BYTES).hexdigest())

    def test_curl_interruption_switches_to_aria2_and_persists_resume_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.aria2_partial_mode = True
            with _Server({"/interrupted.pdf": (200, PDF_BYTES, {"Content-Type": "application/pdf", "ETag": "switch-to-aria2", "X-Disconnect-After": "100"})}) as (base_url, _counts):
                job, job_file = _job(root / "workspace", f"{base_url}/interrupted.pdf")
                provider = _provider(fake, large_file_threshold_bytes=len(PDF_BYTES) + 1)
                interrupted = run_document_pipeline(job, provider, job_file=job_file)
                state_file = next((Path(job.temp_path) / ".document-state").rglob("transfer-state.json"))
                state = json.loads(state_file.read_text(encoding="utf-8"))
                control = next((Path(job.temp_path) / ".document-state").rglob("*.aria2"))
                control_before_resume = control.is_file()
                resumed = run_document_pipeline(interrupted.job, provider, job_file=job_file)

                self.assertEqual(interrupted.error.code.value, "NETWORK_PAUSED")
                self.assertEqual(state["transfer_engine"], "aria2")
                self.assertTrue(control_before_resume)
                self.assertTrue(resumed.success)
                self.assertEqual(fake.aria2_calls[0][1], "--continue=false")
                self.assertEqual(fake.aria2_calls[1][1], "--continue=true")

    def test_stale_aria2_control_identity_is_discarded_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            with _Server({"/large.pdf": (200, PDF_BYTES, {"Content-Type": "application/pdf", "ETag": "same"})}) as (base_url, _counts):
                job, _job_file = _job(root / "workspace", f"{base_url}/large.pdf")
                provider = _provider(fake, large_file_threshold_bytes=0)
                probe = provider.probe(job)
                pending_paths = Path(job.temp_path) / ".document-state" / probe.resume_token
                pending_paths.mkdir(parents=True)
                partial = pending_paths / "large.pdf.partial"
                partial.write_bytes(PDF_BYTES[:30])
                partial.with_name(partial.name + ".aria2").write_text("stale", encoding="utf-8")
                (pending_paths / "transfer-state.json").write_text(json.dumps({"source_identity": "different-source"}), encoding="utf-8")
                result = provider.fetch(job, resume_token=probe.resume_token)

                self.assertEqual(fake.aria2_calls[0][1], "--continue=false")
                self.assertTrue(any(item.role == "document_file" for item in result.artifacts))
                self.assertFalse(partial.with_name(partial.name + ".aria2").exists())

    def test_source_etag_change_creates_new_transfer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            with _Server({"/file.pdf": (200, PDF_BYTES, {"Content-Type": "application/pdf", "ETag": "revision-a"})}) as (base_url, routes):
                job, _job_file = _job(root / "workspace", f"{base_url}/file.pdf")
                provider = _provider(fake)
                first = provider.probe(job)
                provider.fetch(job, resume_token=first.resume_token)
                routes.values["/file.pdf"] = (200, PDF_BYTES, {"Content-Type": "application/pdf", "ETag": "revision-b"})
                second = provider.probe(job)
                self.assertNotEqual(first.resume_token, second.resume_token)

    def test_google_url_and_export_mapping(self) -> None:
        file_id = "abcdefghijklmnopqrstuvwx123456"
        self.assertEqual(_source_from_google_url(f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"), (file_id, None, False))
        self.assertEqual(_source_from_google_url(f"https://drive.google.com/drive/folders/{file_id}"), (file_id, None, True))
        self.assertEqual(_source_from_google_url(f"https://docs.google.com/document/d/{file_id}/edit"), (file_id, "document", False))
        self.assertEqual(_source_from_google_url(f"https://docs.google.com/spreadsheets/d/{file_id}/edit"), (file_id, "spreadsheets", False))
        self.assertEqual(_source_from_google_url(f"https://docs.google.com/presentation/d/{file_id}/edit"), (file_id, "presentation", False))
        self.assertEqual(_rclone_url("rclone://drive/Folder/Example.pdf"), ("drive", "Folder/Example.pdf"))
        self.assertIsNone(_rclone_url("rclone://drive/../../outside.pdf"))

    def test_public_google_file_falls_back_to_gdown_without_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            file_id = "publicFileId1234567890"
            source = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
            fake.google_names[file_id] = "public report.pdf"
            fake.google_payloads[file_id] = PDF_BYTES
            job, job_file = _job(root / "workspace", source)
            outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)

            self.assertTrue(outcome.success)
            gdown_calls = [call for call in fake.calls if Path(call[0]).name == "gdown" and "--version" not in call]
            self.assertTrue(gdown_calls)
            self.assertTrue(all("--no-cookies" in call for call in gdown_calls))
            self.assertFalse(any("--cookies-from-browser" in part for call in gdown_calls for part in call))
            manifest = json.loads(next(Path(job.temp_path).rglob(MANIFEST_NAME)).read_text(encoding="utf-8"))
            self.assertEqual(manifest["engine"]["name"], "gdown")
            self.assertEqual(manifest["artifacts"][0]["provider"], "gdown")
            self.assertFalse(any(path.name == "cookies.txt" for path in Path(job.temp_path).rglob("*")))

    def test_google_same_file_id_without_freshness_metadata_never_blindly_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            file_id = "sameGoogleIdWithoutRevision123"
            source = f"https://drive.google.com/file/d/{file_id}/view"
            fake.google_payloads[file_id] = PDF_BYTES
            job, job_file = _job(root / "workspace", source)
            provider = _provider(fake)
            first = run_document_pipeline(job, provider, job_file=job_file)
            first_record = first.result.metadata["fetch"]["artifacts"][0]
            first_hash = first_record["sha256"]
            first_downloads = sum(1 for call in fake.calls if Path(call[0]).name == "gdown" and "--output" in call)
            second = run_document_pipeline(first.job, provider, job_file=job_file)
            second_record = second.result.metadata["fetch"]["artifacts"][0]
            second_downloads = sum(1 for call in fake.calls if Path(call[0]).name == "gdown" and "--output" in call)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(second_downloads, first_downloads + 1)
            self.assertEqual(second_record["sha256"], first_hash)
            self.assertEqual(second.result.metadata["fetch"]["reuse"], "refetched-weak-identity")
            self.assertEqual(second_record["remote_identity"]["strength"], "weak")
            self.assertEqual(second_record["remote_identity"]["fields"]["file_id"], file_id)

    def test_weak_google_identity_refetch_compares_same_and_changed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            file_id = "sameGoogleIdChangingContent123"
            source = f"https://drive.google.com/file/d/{file_id}/view"
            fake.google_payloads[file_id] = PDF_BYTES
            job, job_file = _job(root / "workspace", source)
            provider = _provider(fake)
            first = run_document_pipeline(job, provider, job_file=job_file)
            first_record = first.result.metadata["fetch"]["artifacts"][0]
            first_hash = first_record["sha256"]

            same_bytes = run_document_pipeline(first.job, provider, job_file=job_file)
            same_record = same_bytes.result.metadata["fetch"]["artifacts"][0]
            self.assertTrue(same_bytes.success)
            self.assertEqual(same_record["sha256"], first_hash)
            self.assertEqual(same_bytes.result.metadata["fetch"]["reuse"], "refetched-weak-identity")
            self.assertEqual(same_record["path"], first_record["path"])
            self.assertEqual(same_bytes.result.metadata["fetch"]["content_comparison"], "same_sha256_after_refetch")

            fake.google_payloads[file_id] = _pdf_bytes("Updated remote revision")
            changed = run_document_pipeline(same_bytes.job, provider, job_file=job_file)
            changed_record = changed.result.metadata["fetch"]["artifacts"][0]
            manifest_path = Path(job.workspace_path) / changed.result.metadata["fetch"]["manifest"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertTrue(changed.success)
            self.assertNotEqual(changed_record["sha256"], first_hash)
            self.assertEqual(manifest["artifacts"][0]["sha256"], changed_record["sha256"])
            self.assertEqual(changed.result.metadata["fetch"]["artifact_hashes"][changed_record["path"]], changed_record["sha256"])

    def test_google_native_export_without_revision_metadata_refetches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "drive: drive\n"
            file_id = "nativeGoogleRevisionAbsent123"
            fake.google_payloads[file_id] = DOCX_BYTES
            job, job_file = _job(root / "workspace", f"https://docs.google.com/document/d/{file_id}/edit")
            provider = _provider(fake)
            first = run_document_pipeline(job, provider, job_file=job_file)
            first_exports = sum(1 for call in fake.calls if Path(call[0]).name == "rclone" and "copyid" in call)
            second = run_document_pipeline(first.job, provider, job_file=job_file)
            second_exports = sum(1 for call in fake.calls if Path(call[0]).name == "rclone" and "copyid" in call)

            self.assertTrue(first.success)
            self.assertTrue(second.success)
            self.assertEqual(second_exports, first_exports + 1)
            self.assertEqual(second.result.metadata["fetch"]["reuse"], "refetched-weak-identity")
            self.assertEqual(second.result.metadata["fetch"]["artifacts"][0]["export_format"], "docx")
            self.assertEqual(second.result.metadata["fetch"]["artifacts"][0]["remote_identity"]["strength"], "weak")

    def test_google_network_retry_exhaustion_maps_to_network_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            file_id = "networkFailurePublicId123"
            fake.always_fail_google_ids.add(file_id)
            job, job_file = _job(root / "workspace", f"https://drive.google.com/file/d/{file_id}/view")
            outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)

            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error.code.value, "NETWORK_PAUSED")
            self.assertTrue(all("--no-cookies" in call for call in fake.calls if Path(call[0]).name == "gdown"))

    def test_google_native_gdown_exports_docx_and_records_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            file_id = "publicNativeDocId123456"
            source = f"https://docs.google.com/document/d/{file_id}/edit?usp=sharing"
            fake.google_names[file_id] = "Native Notes.docx"
            fake.google_payloads[file_id] = DOCX_BYTES
            job, job_file = _job(root / "workspace", source)
            outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)

            self.assertTrue(outcome.success)
            download = next(call for call in fake.calls if Path(call[0]).name == "gdown" and "--output" in call)
            self.assertIn("--format", download)
            self.assertEqual(download[download.index("--format") + 1], "docx")
            self.assertIn("--no-cookies", download)
            record = outcome.result.metadata["fetch"]["artifacts"][0]
            self.assertEqual(record["export_format"], "docx")
            self.assertEqual(record["extension"], ".docx")

    def test_rclone_native_google_export_matrix(self) -> None:
        formats = (
            ("document", "docx", DOCX_BYTES),
            ("spreadsheets", "xlsx", _ooxml_bytes("xlsx")),
            ("presentation", "pptx", _ooxml_bytes("pptx")),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "drive: drive\n"
            for index, (kind, export_format, payload) in enumerate(formats):
                file_id = f"nativeExportMatrix{index:02d}abcdefgh"
                fake.google_payloads[file_id] = payload
                job, job_file = _job(root / "workspace", f"https://docs.google.com/{kind}/d/{file_id}/edit", f"native-{kind}")
                outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)
                self.assertTrue(outcome.success, outcome.error)
                command = next(call for call in fake.calls if Path(call[0]).name == "rclone" and "copyid" in call and file_id in call)
                self.assertIn(f"--drive-export-formats={export_format}", command)
                record = outcome.result.metadata["fetch"]["artifacts"][0]
                self.assertEqual(record["export_format"], export_format)
                self.assertEqual(record["extension"], f".{export_format}")

    def test_rclone_remote_mapping_is_read_only_and_normalizes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "vault: local\n"
            fake.remote_stat = {"Name": "statement.pdf", "Size": len(PDF_BYTES), "ModTime": "2026-09-24T00:00:00Z", "MimeType": "application/pdf", "ID": "object-1", "Hashes": {"MD5": "remote-md5-fixture"}, "payload": PDF_BYTES}
            fake.remote_listing = [fake.remote_stat]
            job, job_file = _job(root / "workspace", "rclone://vault/reports/statement.pdf")
            outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)

            self.assertTrue(outcome.success)
            verbs = [call[1] for call in fake.calls if Path(call[0]).name == "rclone" and len(call) > 1]
            self.assertIn("lsjson", verbs)
            self.assertIn("copyto", verbs)
            self.assertTrue(set(verbs).isdisjoint({"sync", "delete", "deletefile", "move", "moveto", "purge", "cleanup", "rmdirs"}))
            record = outcome.result.metadata["fetch"]["artifacts"][0]
            self.assertEqual(record["remote_identity"]["fields"]["remote"], "vault")
            self.assertEqual(record["remote_identity"]["freshness_fields"], ["remote_checksum"])
            self.assertEqual(record["remote_identity"]["fields"]["remote_checksum"], "remote-md5-fixture")
            self.assertEqual(record["mime"], "application/pdf")
            self.assertEqual(record["size"], len(PDF_BYTES))

    def test_missing_rclone_remote_maps_to_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "other: drive\n"
            job, _job_file = _job(root / "workspace", "rclone://missing/docs/file.pdf")
            provider = _provider(fake)
            with self.assertRaises(ProviderFailure) as caught:
                provider.probe(job)
            self.assertEqual(caught.exception.kind, ProviderFailureKind.AUTH_REQUIRED)

    def test_rclone_google_primary_and_native_export_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            fake.remote_lines = "drive: drive\n"
            file_id = "googleNativeFileId123456"
            fake.google_payloads[file_id] = DOCX_BYTES
            job, job_file = _job(root / "workspace", f"https://docs.google.com/document/d/{file_id}/edit?resourcekey=public-key-123")
            outcome = run_document_pipeline(job, _provider(fake), job_file=job_file)

            self.assertTrue(outcome.success)
            rclone_copy = next(call for call in fake.calls if Path(call[0]).name == "rclone" and "copyid" in call)
            self.assertIn("--drive-export-formats=docx", rclone_copy)
            self.assertIn("--drive-resource-key=public-key-123", rclone_copy)
            self.assertFalse(any(Path(call[0]).name == "gdown" and "--json" in call for call in fake.calls))
            record = outcome.result.metadata["fetch"]["artifacts"][0]
            self.assertEqual(record["provider"], "rclone")
            self.assertEqual(record["export_format"], "docx")

    def test_gdown_folder_partial_resume_refetches_items_without_freshness_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            folder_id = "publicFolderId12345678"
            first_id = "firstFolderFile12345678"
            second_id = "secondFolderFile1234567"
            fake.google_listing = [
                {"url": f"https://drive.google.com/file/d/{second_id}/view", "path": "nested/second.pdf"},
                {"url": f"https://drive.google.com/file/d/{first_id}/view", "path": "nested/first.pdf"},
            ]
            fake.google_names.update({first_id: "first.pdf", second_id: "second.pdf"})
            fake.google_payloads.update({first_id: PDF_BYTES, second_id: _pdf_bytes("Second")})
            fake.fail_google_ids.add(second_id)
            job, job_file = _job(root / "workspace", f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing")
            provider = _provider(fake)
            first = run_document_pipeline(job, provider, job_file=job_file)

            self.assertFalse(first.success)
            self.assertEqual(first.error.code.value, "PARTIAL_FAILURE")
            manifest_path = next(Path(job.temp_path).rglob(MANIFEST_NAME))
            partial_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(partial_manifest["complete"])
            self.assertEqual(len(partial_manifest["artifacts"]), 1)
            self.assertIn("nested/first.pdf", partial_manifest["artifacts"][0]["path"])
            download_count_before_resume = sum(1 for call in fake.calls if Path(call[0]).name == "gdown" and "--output" in call)

            second = run_document_pipeline(first.job, provider, job_file=job_file)

            self.assertTrue(second.success)
            self.assertEqual(second.job.current_state, JobState.DOWNLOADING)
            download_count_after_resume = sum(1 for call in fake.calls if Path(call[0]).name == "gdown" and "--output" in call)
            self.assertEqual(download_count_after_resume, download_count_before_resume + 2)
            final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(final_manifest["complete"])
            self.assertEqual([item["remote_path"] for item in final_manifest["artifacts"]], ["nested/first.pdf", "nested/second.pdf"])
            self.assertTrue(all(Path(job.workspace_path, item["path"]).resolve().is_relative_to(Path(job.temp_path).resolve()) for item in final_manifest["artifacts"]))

    def test_provider_refuses_non_document_job_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _FakeTools(root)
            job, _job_file = _job(root / "workspace", "https://example.invalid/file.pdf")
            job.declared_content_type = ContentType.WEBPAGE
            before = job.to_json()
            with self.assertRaises(ProviderFailure):
                _provider(fake).probe(job)
            self.assertEqual(job.to_json(), before)
            self.assertEqual(job.current_state, JobState.QUEUED)


if __name__ == "__main__":
    unittest.main()
