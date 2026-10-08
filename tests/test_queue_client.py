from __future__ import annotations

import json
import os
import ssl
import urllib.request
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.queue.client import (
    _AppsScriptRedirectHandler,
    ClaimedTask,
    QueueApiError,
    QueueClient,
    QueueOperationResult,
    QueueResponse,
    _default_transport,
    canonical_payload,
    sign_payload,
)
from src.queue.secrets import KeychainSecretProvider, SecretProviderError


ROOT = Path(__file__).resolve().parents[1]
FAKE_SECRET = "test-only-queue-client-secret"
FIXTURE_PAYLOAD = (ROOT / "tests" / "fixtures" / "queue_hmac_payload.json").read_text(encoding="utf-8").rstrip("\r\n")
FIXTURE_TIMESTAMP = "1790323200"


class FakeSecretProvider:
    def __init__(self, value: str = FAKE_SECRET) -> None:
        self.value = value
        self.calls = 0

    def get_secret(self) -> str:
        self.calls += 1
        return self.value


def response(data, *, status: int = 200) -> QueueResponse:
    return QueueResponse(status=status, body=json.dumps({"ok": True, "data": data}).encode("utf-8"))


class QueueClientTests(unittest.TestCase):
    def client(self, transport, **overrides) -> QueueClient:
        options = {
            "api_url": "https://script.google.com/macros/s/example/exec",
            "secret_provider": FakeSecretProvider(),
            "transport": transport,
            "clock": lambda: 1790323200,
            "sleep": lambda _seconds: None,
        }
        options.update(overrides)
        return QueueClient(**options)

    def test_signing_uses_exact_cloud_envelope_contract(self) -> None:
        mapping = json.loads(FIXTURE_PAYLOAD)
        self.assertEqual(canonical_payload(mapping), FIXTURE_PAYLOAD)
        first = sign_payload(FAKE_SECRET, FIXTURE_TIMESTAMP, FIXTURE_PAYLOAD)
        changed_body = sign_payload(FAKE_SECRET, FIXTURE_TIMESTAMP, FIXTURE_PAYLOAD + " ")
        changed_timestamp = sign_payload(FAKE_SECRET, str(int(FIXTURE_TIMESTAMP) + 1), FIXTURE_PAYLOAD)
        self.assertNotEqual(first, changed_body)
        self.assertNotEqual(first, changed_timestamp)

    def test_empty_queue_is_none_not_an_exception(self) -> None:
        sent = []
        client = self.client(lambda url, body, timeout: (sent.append((url, body, timeout)) or response(None)))
        self.assertIsNone(client.claim("00000000-0000-4000-8000-000000000001"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], 20)

    def test_successful_claim_returns_typed_claimed_task(self) -> None:
        payload = {
            "queue_id": "q-1", "video_id": "v-1", "url": "https://example.invalid/v-1",
            "claim_token": "t" * 64, "lease_until": "2026-09-25T08:15:00.000Z", "attempts": 1,
        }
        client = self.client(lambda _url, _body, _timeout: response(payload))
        task = client.claim("00000000-0000-4000-8000-000000000002")
        self.assertIsInstance(task, ClaimedTask)
        self.assertEqual(task.queue_id, "q-1")
        self.assertEqual(task.attempts, 1)

    def test_claim_transport_retry_reuses_same_request_id_and_payload(self) -> None:
        sent = []
        claim_data = {
            "queue_id": "q-1", "video_id": "v-1", "url": "https://example.invalid/v-1",
            "claim_token": "t" * 64, "lease_until": "2026-09-25T08:15:00.000Z", "attempts": 1,
        }

        def transport(_url, body, _timeout):
            sent.append(json.loads(body))
            if len(sent) == 1:
                raise ConnectionResetError("test transport reset")
            return response(claim_data)

        client = self.client(transport, max_transport_attempts=2)
        task = client.claim("00000000-0000-4000-8000-000000000003")
        self.assertEqual(task.queue_id, "q-1")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["payload"], sent[1]["payload"])
        self.assertEqual(json.loads(sent[0]["payload"])["claim_request_id"], "00000000-0000-4000-8000-000000000003")

    def test_exhausted_claim_retry_exposes_request_id_for_a_later_same_poll_retry(self) -> None:
        sent = []
        data = {"queue_id": "q-1", "video_id": "v-1", "url": "https://example.invalid/v-1", "claim_token": "t" * 64, "lease_until": "2026-09-25T08:15:00Z", "attempts": 1}

        def unavailable(_url, body, _timeout):
            sent.append(json.loads(json.loads(body)["payload"])["claim_request_id"])
            raise ConnectionResetError("reset")

        client = self.client(unavailable, max_transport_attempts=1)
        with self.assertRaises(QueueApiError) as caught:
            client.claim()
        request_id = caught.exception.claim_request_id
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], request_id)

        retry_payloads = []
        retry_client = self.client(lambda _url, body, _timeout: (retry_payloads.append(json.loads(json.loads(body)["payload"])) or response(data)))
        claimed = retry_client.claim(request_id)
        self.assertEqual(claimed.claim_request_id, request_id)
        self.assertEqual(retry_payloads[0]["claim_request_id"], request_id)

    def test_claim_generates_new_request_id_for_a_new_poll(self) -> None:
        sent = []
        client = self.client(lambda _url, body, _timeout: (sent.append(json.loads(body)["payload"]) or response(None)))
        client.claim()
        client.claim()
        ids = [json.loads(payload)["claim_request_id"] for payload in sent]
        self.assertEqual(len(set(ids)), 2)

    def test_invalid_claim_request_id_is_rejected_without_transport(self) -> None:
        calls = []
        client = self.client(lambda *_args: (calls.append(1) or response(None)))
        with self.assertRaises(QueueApiError) as caught:
            client.claim("")
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertEqual(calls, [])

    def test_heartbeat_complete_and_fail_return_typed_results(self) -> None:
        responses = [
            response({"queue_id": "q-1", "status": "PROCESSING", "lease_until": "2026-09-25T08:15:00Z"}),
            response({"queue_id": "q-1", "status": "COMPLETED", "local_job_id": "job-1", "result_path": "/out/job-1", "completed_at": "2026-09-25T08:01:00Z"}),
            response({"queue_id": "q-1", "status": "PAUSED", "local_job_id": "job-1", "last_error": "{}", "attempts": 1}),
        ]
        sent = []

        def transport(_url, body, _timeout):
            sent.append(json.loads(json.loads(body)["payload"]))
            return responses.pop(0)

        client = self.client(transport)
        heartbeat = client.heartbeat("q-1", "t" * 64)
        complete = client.complete("q-1", "t" * 64, "job-1", "/out/job-1")
        fail = client.fail("q-1", "t" * 64, "job-1", {"code": "NETWORK_PAUSED", "message": "offline"})
        self.assertIsInstance(heartbeat, QueueOperationResult)
        self.assertEqual(heartbeat.status, "PROCESSING")
        self.assertEqual(complete.status, "COMPLETED")
        self.assertEqual(fail.status, "PAUSED")
        self.assertEqual([item["action"] for item in sent], ["heartbeat", "complete", "fail"])
        self.assertTrue(all(item["claim_token"] == "t" * 64 for item in sent))

    def test_each_mutation_retries_with_the_same_operation_identity_and_payload(self) -> None:
        cases = [
            (
                "heartbeat",
                lambda client: client.heartbeat("q-1", "t" * 64),
                {"queue_id": "q-1", "status": "PROCESSING", "lease_until": "2026-09-25T08:15:00Z"},
            ),
            (
                "complete",
                lambda client: client.complete("q-1", "t" * 64, "job-1", "/out/job-1"),
                {"queue_id": "q-1", "status": "COMPLETED", "local_job_id": "job-1", "result_path": "/out/job-1", "completed_at": "2026-09-25T08:01:00Z"},
            ),
            (
                "fail",
                lambda client: client.fail("q-1", "t" * 64, "job-1", {"code": "NETWORK_PAUSED", "message": "offline"}),
                {"queue_id": "q-1", "status": "PAUSED", "local_job_id": "job-1", "last_error": "{}", "attempts": 1},
            ),
        ]
        for action, operation, result in cases:
            with self.subTest(action=action):
                sent = []

                def transport(_url, body, _timeout):
                    sent.append(json.loads(json.loads(body)["payload"]))
                    if len(sent) == 1:
                        raise TimeoutError("temporary timeout")
                    return response(result)

                operation(self.client(transport, max_transport_attempts=2))
                self.assertEqual(sent[0], sent[1])
                self.assertEqual(sent[0]["queue_id"], "q-1")
                self.assertEqual(sent[0]["claim_token"], "t" * 64)

    def test_api_failures_keep_structured_error_codes(self) -> None:
        for code in ("AUTH_FAILED", "QUEUE_NOT_FOUND", "INVALID_STATE", "CLAIM_TOKEN_INVALID", "CLAIM_REQUEST_EXPIRED", "LEASE_EXPIRED", "CONFIG_INVALID"):
            with self.subTest(code=code):
                body = json.dumps({"ok": False, "error": {"code": code, "message": "safe message"}}).encode()
                client = self.client(lambda _url, _body, _timeout: QueueResponse(status=200, body=body))
                with self.assertRaises(QueueApiError) as caught:
                    client.claim("00000000-0000-4000-8000-000000000004")
                self.assertEqual(caught.exception.code, code)

    def test_empty_error_code_is_also_normal_empty_result(self) -> None:
        body = json.dumps({"ok": False, "error": {"code": "QUEUE_EMPTY", "message": "No tasks."}}).encode()
        client = self.client(lambda _url, _body, _timeout: QueueResponse(status=200, body=body))
        self.assertIsNone(client.claim("00000000-0000-4000-8000-000000000005"))

    def test_retries_temporary_5xx_but_not_auth_errors(self) -> None:
        sent = []
        data = {"queue_id": "q-1", "video_id": "v-1", "url": "https://example.invalid/v-1", "claim_token": "t" * 64, "lease_until": "2026-09-25T08:15:00Z", "attempts": 1}

        def transport(_url, body, _timeout):
            sent.append(json.loads(body)["payload"])
            if len(sent) == 1:
                return QueueResponse(status=503, body=b"temporary")
            return response(data)

        client = self.client(transport, max_transport_attempts=2)
        self.assertEqual(client.claim("00000000-0000-4000-8000-000000000006").queue_id, "q-1")
        self.assertEqual(sent[0], sent[1])

        auth_body = json.dumps({"ok": False, "error": {"code": "AUTH_FAILED", "message": "no"}}).encode()
        calls = []
        auth_client = self.client(lambda *_args: (calls.append(1) or QueueResponse(status=200, body=auth_body)))
        with self.assertRaises(QueueApiError):
            auth_client.claim("00000000-0000-4000-8000-000000000006")
        self.assertEqual(len(calls), 1)

    def test_transport_exhaustion_is_structured_and_does_not_expose_transport_text(self) -> None:
        client = self.client(lambda *_args: (_ for _ in ()).throw(TimeoutError(f"leak-{FAKE_SECRET}")), max_transport_attempts=2)
        with self.assertRaises(QueueApiError) as caught:
            client.claim("00000000-0000-4000-8000-000000000007")
        self.assertEqual(caught.exception.code, "TRANSPORT_FAILED")
        self.assertNotIn(FAKE_SECRET, str(caught.exception))
        self.assertEqual(caught.exception.claim_request_id, "00000000-0000-4000-8000-000000000007")

    def test_timeout_error_mapping_and_secret_provider_dependency_injection(self) -> None:
        secret_provider = FakeSecretProvider()
        client = self.client(lambda *_args: response(None), secret_provider=secret_provider)
        client.claim("00000000-0000-4000-8000-000000000008")
        self.assertEqual(secret_provider.calls, 1)

    def test_user_config_overlays_the_shipped_defaults(self) -> None:
        import os
        from unittest.mock import patch
        from src.core.policies import load_config

        with tempfile.TemporaryDirectory() as directory:
            overlay = Path(directory) / "config.yaml"
            overlay.write_text("queue_api_url: https://example.test/exec\noutput:\n  delivery_root: ~/Videos/UCI/\n", encoding="utf-8")
            with patch.dict(os.environ, {"UCI_CONFIG": str(overlay)}):
                merged = load_config(ROOT / "config" / "defaults.yaml")
                elsewhere = load_config(overlay)
            with patch.dict(os.environ, {"UCI_CONFIG": "none"}):
                plain = load_config(ROOT / "config" / "defaults.yaml")
        self.assertEqual(merged["queue_api_url"], "https://example.test/exec")
        self.assertEqual(merged["output"]["delivery_root"], "~/Videos/UCI/")
        self.assertEqual(merged["output"]["file_conflict"], "deterministic_rename")
        self.assertNotIn("schema_version", elsewhere)
        self.assertEqual(plain["queue_api_url"], "")

    def test_defaults_config_has_endpoint_slot_but_no_plaintext_secret(self) -> None:
        from src.core.policies import load_simple_yaml

        config = load_simple_yaml(ROOT / "config" / "defaults.yaml")
        # Shipped defaults carry no installation endpoint; setup writes it to the user config.
        self.assertEqual(config.get("queue_api_url"), "")
        self.assertNotIn("secret", (ROOT / "config" / "defaults.yaml").read_text(encoding="utf-8").lower())
        from src.core.policies import load_default_policies

        self.assertEqual(load_default_policies(ROOT / "config" / "defaults.yaml").schema_version, 1)
        with tempfile.TemporaryDirectory() as directory:
            missing_endpoint = Path(directory) / "defaults.yaml"
            missing_endpoint.write_text("queue_api_url: null\n", encoding="utf-8")
            with self.assertRaises(QueueApiError) as caught:
                QueueClient.from_defaults(missing_endpoint, secret_provider=FakeSecretProvider(), transport=lambda *_: response(None))
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertNotIn("secret", QueueClient.__init__.__code__.co_varnames)

    def test_default_transport_uses_certifi_with_tls_verification_enabled(self) -> None:
        fake_response = MagicMock()
        fake_response.status = 200
        fake_response.read.return_value = b'{"ok":true,"data":null}'
        fake_response.__enter__.return_value = fake_response
        opener = MagicMock()
        opener.open.return_value = fake_response
        with patch("src.queue.client.urllib.request.build_opener", return_value=opener) as build_opener:
            result = _default_transport("https://example.test/api", b"{}", 3)
        handlers = build_opener.call_args.args
        https = next(handler for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        context = https._context
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(any(isinstance(handler, _AppsScriptRedirectHandler) for handler in handlers))
        self.assertEqual(result, QueueResponse(status=200, body=b'{"ok":true,"data":null}'))

    def test_redirect_to_web_app_exec_keeps_post_but_echo_redirect_becomes_get(self) -> None:
        handler = _AppsScriptRedirectHandler()
        original = urllib.request.Request(
            "https://script.google.com/macros/s/id/exec", data=b'{"signed":1}',
            headers={"Content-Type": "application/json"}, method="POST",
        )
        to_exec = handler.redirect_request(original, None, 302, "Found", {}, "https://script.google.com/macros/s/id/exec?x=1")
        self.assertEqual(to_exec.get_method(), "POST")
        self.assertEqual(to_exec.data, b'{"signed":1}')
        to_echo = handler.redirect_request(original, None, 302, "Found", {}, "https://script.googleusercontent.com/macros/echo?user_content_key=k")
        self.assertEqual(to_echo.get_method(), "GET")

    def test_html_error_page_is_retried_before_failing_the_request(self) -> None:
        html = QueueResponse(status=200, body=b"<!DOCTYPE html><html><title>Error</title><body>Script function not found: doGet</body></html>")
        replies = [html, response(None)]
        client = QueueClient(
            api_url="https://script.google.com/macros/s/test-only/exec",
            secret_provider=FakeSecretProvider(),
            transport=lambda *_: replies.pop(0),
            sleep=lambda _seconds: None,
        )
        self.assertIsNone(client.claim("request-000000000001"))
        self.assertEqual(replies, [])

    def test_configured_endpoint_is_loaded_without_embedding_a_deployment_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "defaults.yaml"
            path.write_text("queue_api_url: https://script.google.com/macros/s/test-only/exec\n", encoding="utf-8")
            client = QueueClient.from_defaults(path, secret_provider=FakeSecretProvider(), transport=lambda *_: response(None))
        self.assertEqual(client.api_url, "https://script.google.com/macros/s/test-only/exec")

    @unittest.skipIf(os.name == "nt", "the Keychain adapter is macOS only")
    def test_keychain_adapter_reads_only_from_security_cli_without_exposing_stderr(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": f"{FAKE_SECRET}\n", "stderr": ""})()
        with patch("src.queue.secrets.subprocess.run", return_value=completed) as run:
            provider = KeychainSecretProvider()
            self.assertEqual(provider.get_secret(), FAKE_SECRET)
            self.assertEqual(provider.get_secret(), FAKE_SECRET)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["/usr/bin/security", "find-generic-password", "-s"])
        self.assertEqual(args[3:6], ["UCI Queue API HMAC", "-a", "queue-api"])
        self.assertNotIn(FAKE_SECRET, args)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertEqual(run.call_count, 1)

        failure = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": f"{FAKE_SECRET} not found"})()
        with patch("src.queue.secrets.subprocess.run", return_value=failure):
            with self.assertRaises(SecretProviderError) as caught:
                KeychainSecretProvider(service="uci-test-queue", account="missing").get_secret()
        self.assertNotIn(FAKE_SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class QueueClientStatusAndDetailTests(unittest.TestCase):
    def client(self, transport) -> QueueClient:
        return QueueClient(
            api_url="https://script.google.com/macros/s/example/exec",
            secret_provider=FakeSecretProvider(),
            transport=transport,
            clock=lambda: 1790323200,
            sleep=lambda _seconds: None,
        )

    def test_status_sends_a_signed_status_action_and_returns_the_report(self) -> None:
        sent = []
        report = {"healthy": True, "day": "2026-09-30", "checks": []}
        status = self.client(lambda url, body, timeout: (sent.append(json.loads(body)) or response(report))).status("2026-09-30")
        self.assertEqual(status["day"], "2026-09-30")
        self.assertEqual(json.loads(sent[0]["payload"]), {"action": "status", "day": "2026-09-30"})
        with self.assertRaises(QueueApiError) as raised:
            self.client(lambda *_: response(report)).status("30/09/2026")
        self.assertEqual(raised.exception.code, "INVALID_REQUEST")

    def test_server_exception_detail_reaches_the_error_without_secrets_or_urls(self) -> None:
        body = {"ok": False, "error": {
            "code": "INVALID_STATE", "message": "Queue item is in an invalid state for this operation.",
            "detail": f"You do not have permission to call SpreadsheetApp.openById. Required permissions: https://www.googleapis.com/auth/spreadsheets {FAKE_SECRET}",
        }}
        client = self.client(lambda *_: QueueResponse(status=200, body=json.dumps(body).encode("utf-8")))
        with self.assertRaises(QueueApiError) as raised:
            client.claim("00000000-0000-4000-8000-000000000001")
        detail = raised.exception.detail or ""
        self.assertTrue(detail.startswith("server:You do not have permission to call SpreadsheetApp.openById"))
        self.assertNotIn(FAKE_SECRET, detail)
        self.assertNotIn("https://", detail)
