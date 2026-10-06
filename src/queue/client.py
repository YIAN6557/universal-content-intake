"""Authenticated local client for the Stage 7 Queue API.

This module implements bounded HTTP operations only. It does not poll in a
loop, start Jobs, invoke Providers, or manage Worker heartbeats in the
background.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import certifi

from src.core.errors import CoreError
from src.core.policies import load_config
from src.queue.secrets import KeychainSecretProvider, SecretProviderError


class SecretProvider(Protocol):
    def get_secret(self) -> str: ...


@dataclass(frozen=True)
class QueueResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class ClaimedTask:
    queue_id: str
    video_id: str
    url: str
    claim_token: str
    lease_until: str
    attempts: int
    claim_request_id: str | None = None
    claimed_at: str | None = None
    local_job_id: str | None = None
    selection_day: str | None = None
    selection_rank: int | None = None
    rank2_start_cutoff_at: str | None = None
    core_started_at: str | None = None
    monitor: Mapping[str, Any] | None = None


# Optional Creator-monitor evidence that Cloud attaches to a claim so the local
# info.md can record why the video was selected.
MONITOR_FIELDS = (
    "video_id", "title", "creator_name", "channel_id", "published_at", "first_seen",
    "hot_at", "views_at_hot", "like_rate", "velocity", "hot_reason", "hot_mode",
    "hot_checkpoint", "selection_hot_strength", "selection_day", "selection_rank", "queue_id",
)


SETUP_ACTIONS = frozenset({"setup_inspect", "setup_config_set", "setup_creators_upsert", "setup_monitoring"})


@dataclass(frozen=True)
class QueueOperationResult:
    queue_id: str
    status: str
    lease_until: str | None = None
    local_job_id: str | None = None
    result_path: str | None = None
    completed_at: str | None = None
    last_error: str | None = None
    attempts: int | None = None
    core_started_at: str | None = None


class QueueApiError(RuntimeError):
    """Structured Queue API, transport, or client configuration failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        claim_request_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.http_status = http_status
        self.retryable = bool(retryable)
        self.claim_request_id = claim_request_id
        # Short, secret-free description of an unexpected response body
        # (for example the title of a Google error page) for the Worker log.
        self.detail = detail
        super().__init__(self.message)


def describe_unexpected_body(body: bytes, *, secret: str = "") -> str:
    """Summarize a non-JSON response for logs without echoing request data."""

    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return "empty"
    head = text[:2000].lower()
    kind = "html" if head.lstrip().startswith(("<!doctype", "<html")) or "<body" in head else "text"
    if kind == "html":
        title = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
        body_text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        body_text = re.sub(r"<title[^>]*>.*?</title>", " ", body_text, flags=re.IGNORECASE | re.DOTALL)
        body_text = re.sub(r"<[^>]+>", " ", body_text)
        summary = " ".join(filter(None, [title.group(1).strip() if title else "", body_text]))
    else:
        summary = text
    if secret:
        summary = summary.replace(secret, "[redacted]")
    summary = re.sub(r"https?://\S+", "[url]", summary)
    summary = re.sub(r"\s+", " ", summary).strip()
    return f"{kind}:{summary[:160]}"


def _server_detail(value: Any, *, secret: str = "") -> str | None:
    """Unexpected-exception text the Web App returns to signed callers."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.replace(secret, "[redacted]") if secret else value
    text = re.sub(r"https?://\S+", "[url]", text)
    return "server:" + re.sub(r"\s+", " ", text).strip()[:200]


Transport = Callable[[str, bytes, float], QueueResponse]


def canonical_payload(value: Mapping[str, Any]) -> str:
    """Serialize API payloads deterministically; the resulting string is signed verbatim."""

    try:
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise QueueApiError("INVALID_REQUEST", "Queue request payload cannot be serialized.") from None


def sign_payload(secret: str, timestamp: str, payload: str) -> str:
    """Return the Base64 HMAC-SHA256 used by Apps Script: timestamp + LF + raw payload."""

    timestamp = str(timestamp)
    if not secret or not timestamp.isdigit() or not payload:
        raise QueueApiError("AUTH_FAILED", "Queue request signing inputs are incomplete.")
    signing_text = f"{timestamp}\n{payload}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signing_text, hashlib.sha256).digest()
    return base64.b64encode(signature).decode("ascii")


class _AppsScriptRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep POST semantics when Apps Script redirects back to a Web App /exec.

    The normal flow is POST /exec -> 302 -> GET script.googleusercontent.com
    /macros/echo, and that GET must stay a GET. Occasionally Google redirects
    to an /exec URL instead; urllib would then turn the POST into a GET, which
    runs the (absent) doGet and returns an HTML error page. Every Queue action
    is idempotent, so re-sending the same signed body is safe.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if req.get_method() == "POST" and urlsplit(newurl).path.endswith("/exec"):
            return urllib.request.Request(
                newurl,
                data=req.data,
                headers={key: value for key, value in req.header_items() if key.lower() not in {"content-length", "host"}},
                method="POST",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_transport(url: str, body: bytes, timeout_seconds: float) -> QueueResponse:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        tls_context = ssl.create_default_context(cafile=certifi.where())
        opener = urllib.request.build_opener(_AppsScriptRedirectHandler(), urllib.request.HTTPSHandler(context=tls_context))
        with opener.open(request, timeout=timeout_seconds) as response:
            return QueueResponse(status=int(response.status), body=response.read())
    except urllib.error.HTTPError as error:
        return QueueResponse(status=int(error.code), body=error.read())


def _is_transient_html(response: QueueResponse) -> bool:
    """An HTML page instead of the JSON envelope is a Google-side hiccup."""

    head = response.body[:2000].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html")) or b"<body" in head


class QueueClient:
    def __init__(
        self,
        *,
        api_url: str,
        secret_provider: SecretProvider,
        transport: Transport | None = None,
        timeout_seconds: float = 20,
        max_transport_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_url = self._validate_api_url(api_url)
        if secret_provider is None or not callable(getattr(secret_provider, "get_secret", None)):
            raise QueueApiError("CONFIG_INVALID", "Queue API requires a Keychain-backed secret provider.")
        try:
            timeout_value = float(timeout_seconds)
            backoff_value = float(retry_backoff_seconds)
        except (TypeError, ValueError, OverflowError):
            raise QueueApiError("CONFIG_INVALID", "Queue API transport configuration is invalid.") from None
        if not math.isfinite(timeout_value) or timeout_value <= 0 or (
            isinstance(max_transport_attempts, bool)
            or not isinstance(max_transport_attempts, int)
            or max_transport_attempts < 1
        ) or not math.isfinite(backoff_value) or backoff_value < 0:
            raise QueueApiError("CONFIG_INVALID", "Queue API transport configuration is invalid.")
        self._secret_provider = secret_provider
        self._transport = transport or _default_transport
        self.timeout_seconds = float(timeout_seconds)
        self.max_transport_attempts = int(max_transport_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._clock = clock
        self._sleep = sleep

    @classmethod
    def from_defaults(
        cls,
        defaults_path: Path | str,
        *,
        secret_provider: SecretProvider | None = None,
        transport: Transport | None = None,
        **kwargs: Any,
    ) -> "QueueClient":
        try:
            defaults = load_config(Path(defaults_path))
        except (OSError, ValueError):
            raise QueueApiError("CONFIG_INVALID", "Queue API defaults could not be loaded.") from None
        api_url = defaults.get("queue_api_url")
        if not isinstance(api_url, str) or not api_url.strip():
            raise QueueApiError("CONFIG_INVALID", "queue_api_url is not configured; run bin/uci setup first.")
        return cls(
            api_url=api_url,
            secret_provider=KeychainSecretProvider() if secret_provider is None else secret_provider,
            transport=transport,
            **kwargs,
        )

    @staticmethod
    def _validate_api_url(value: str) -> str:
        normalized = str(value or "").strip()
        try:
            parsed = urlsplit(normalized)
        except ValueError:
            raise QueueApiError("CONFIG_INVALID", "queue_api_url must be an HTTPS endpoint without embedded credentials.") from None
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise QueueApiError("CONFIG_INVALID", "queue_api_url must be an HTTPS endpoint without embedded credentials.")
        return normalized

    def claim(self, claim_request_id: str | None = None) -> ClaimedTask | None:
        request_id = str(uuid.uuid4()) if claim_request_id is None else claim_request_id
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", request_id.strip()):
            raise QueueApiError("INVALID_REQUEST", "claim_request_id must be a unique identifier between 16 and 128 characters.")
        request_id = request_id.strip()
        data = self._request({"action": "claim", "claim_request_id": request_id}, claim_request_id=request_id)
        if data is None:
            return None
        record = self._require_mapping(data, "claim")
        queue_id = self._required_text(record, "queue_id")
        video_id = self._required_text(record, "video_id")
        url = self._required_text(record, "url")
        claim_token = self._required_text(record, "claim_token")
        lease_until = self._required_text(record, "lease_until")
        attempts = record.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise QueueApiError("API_RESPONSE_INVALID", "Queue claim response contains invalid attempts.", claim_request_id=request_id)
        if record.get("claim_request_id") not in (None, "", request_id):
            raise QueueApiError("API_RESPONSE_INVALID", "Queue claim response request ID does not match.", claim_request_id=request_id)
        return ClaimedTask(
            queue_id=queue_id,
            video_id=video_id,
            url=url,
            claim_token=claim_token,
            lease_until=lease_until,
            attempts=attempts,
            claim_request_id=request_id,
            claimed_at=self._optional_text(record.get("claimed_at")),
            local_job_id=self._optional_text(record.get("local_job_id")),
            selection_day=self._optional_text(record.get("selection_day")),
            selection_rank=self._optional_rank(record.get("selection_rank")),
            rank2_start_cutoff_at=self._optional_text(record.get("rank2_start_cutoff_at")),
            core_started_at=self._optional_text(record.get("core_started_at")),
            monitor=self._optional_monitor(record.get("monitor")),
        )

    def status(self, day: str | None = None) -> Mapping[str, Any]:
        """Read-only cloud health and batch report (Config markers, videos, snapshots, Queue)."""

        payload: dict[str, Any] = {"action": "status"}
        if day is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
                raise QueueApiError("INVALID_REQUEST", "status day must be YYYY-MM-DD.")
            payload["day"] = str(day)
        return self._require_mapping(self._request(payload), "status")

    def setup(self, action: str, **fields: Any) -> Mapping[str, Any]:
        """Signed first-run setup call used by bin/uci setup (see cloud setup.gs)."""

        if action not in SETUP_ACTIONS:
            raise QueueApiError("INVALID_REQUEST", f"Unknown setup action {action}.")
        return self._require_mapping(self._request({"action": action, **fields}), action)

    def heartbeat(self, queue_id: str, claim_token: str) -> QueueOperationResult:
        return self._operation({"action": "heartbeat", "queue_id": queue_id, "claim_token": claim_token}, "heartbeat", {"PROCESSING"})

    def core_started(self, queue_id: str, claim_token: str) -> QueueOperationResult:
        return self._operation(
            {"action": "core_started", "queue_id": queue_id, "claim_token": claim_token},
            "core_started",
            {"PROCESSING", "FAILED"},
        )

    def complete(self, queue_id: str, claim_token: str, local_job_id: str, result_path: str) -> QueueOperationResult:
        return self._operation({
            "action": "complete", "queue_id": queue_id, "claim_token": claim_token,
            "local_job_id": local_job_id, "result_path": result_path,
        }, "complete", {"COMPLETED"})

    def fail(
        self,
        queue_id: str,
        claim_token: str,
        local_job_id: str,
        error: CoreError | Mapping[str, Any],
    ) -> QueueOperationResult:
        normalized_error = error.to_dict() if isinstance(error, CoreError) else dict(error)
        return self._operation({
            "action": "fail", "queue_id": queue_id, "claim_token": claim_token,
            "local_job_id": local_job_id, "error": normalized_error,
        }, "fail", {"PAUSED", "FAILED"})

    def _operation(self, payload: Mapping[str, Any], action: str, valid_statuses: set[str]) -> QueueOperationResult:
        data = self._request(payload)
        record = self._require_mapping(data, action)
        queue_id = self._required_text(record, "queue_id")
        status = self._required_text(record, "status")
        if status not in valid_statuses:
            raise QueueApiError("API_RESPONSE_INVALID", f"Queue {action} response contains an invalid status.")
        attempts = record.get("attempts")
        if attempts is not None and (isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0):
            raise QueueApiError("API_RESPONSE_INVALID", "Queue response contains invalid attempts.")
        return QueueOperationResult(
            queue_id=queue_id,
            status=status,
            lease_until=self._optional_text(record.get("lease_until")),
            local_job_id=self._optional_text(record.get("local_job_id")),
            result_path=self._optional_text(record.get("result_path")),
            completed_at=self._optional_text(record.get("completed_at")),
            last_error=self._optional_text(record.get("last_error")),
            attempts=attempts,
            core_started_at=self._optional_text(record.get("core_started_at")),
        )

    def _request(self, payload: Mapping[str, Any], *, claim_request_id: str | None = None) -> Mapping[str, Any] | None:
        payload_text = canonical_payload(payload)
        try:
            secret = self._secret_provider.get_secret()
        except SecretProviderError:
            raise QueueApiError("SECRET_UNAVAILABLE", "Queue API Keychain credential is unavailable.", claim_request_id=claim_request_id) from None
        except Exception:
            raise QueueApiError("SECRET_UNAVAILABLE", "Queue API Keychain credential is unavailable.", claim_request_id=claim_request_id) from None
        if not isinstance(secret, str) or not secret:
            raise QueueApiError("SECRET_UNAVAILABLE", "Queue API Keychain credential is unavailable.", claim_request_id=claim_request_id)

        last_status: int | None = None
        for attempt_index in range(self.max_transport_attempts):
            timestamp = str(int(self._clock()))
            signature = sign_payload(secret, timestamp, payload_text)
            envelope = json.dumps(
                {"timestamp": timestamp, "payload": payload_text, "signature": signature},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                response = self._transport(self.api_url, envelope, self.timeout_seconds)
            except (OSError, TimeoutError, socket.timeout, urllib.error.URLError):
                if attempt_index + 1 >= self.max_transport_attempts:
                    raise QueueApiError(
                        "TRANSPORT_FAILED",
                        "Queue API transport failed after retrying.",
                        retryable=True,
                        claim_request_id=claim_request_id,
                    ) from None
                self._sleep(self.retry_backoff_seconds * (2**attempt_index))
                continue
            response = self._normalize_response(response)
            last_status = response.status
            if _is_transient_html(response) and attempt_index + 1 < self.max_transport_attempts:
                self._sleep(self.retry_backoff_seconds * (2**attempt_index))
                continue
            if 500 <= response.status <= 599:
                if attempt_index + 1 >= self.max_transport_attempts:
                    raise QueueApiError(
                        "TRANSPORT_FAILED",
                        "Queue API returned a temporary server failure after retrying.",
                        http_status=response.status,
                        retryable=True,
                        claim_request_id=claim_request_id,
                    )
                self._sleep(self.retry_backoff_seconds * (2**attempt_index))
                continue
            return self._decode_response(response, secret=secret, claim_request_id=claim_request_id)
        raise QueueApiError("TRANSPORT_FAILED", "Queue API transport failed after retrying.", http_status=last_status, retryable=True, claim_request_id=claim_request_id)

    @staticmethod
    def _normalize_response(response: QueueResponse | tuple[int, bytes] | Any) -> QueueResponse:
        if isinstance(response, QueueResponse):
            return response
        try:
            return QueueResponse(status=int(response[0]), body=bytes(response[1]))
        except Exception:
            raise QueueApiError("API_RESPONSE_INVALID", "Queue API transport returned an invalid response.") from None

    @staticmethod
    def _decode_response(response: QueueResponse, *, secret: str, claim_request_id: str | None) -> Mapping[str, Any] | None:
        try:
            body = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise QueueApiError(
                "API_RESPONSE_INVALID",
                "Queue API returned invalid JSON.",
                http_status=response.status,
                claim_request_id=claim_request_id,
                detail=describe_unexpected_body(response.body, secret=secret),
            ) from None
        if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
            raise QueueApiError("API_RESPONSE_INVALID", "Queue API response envelope is invalid.", http_status=response.status, claim_request_id=claim_request_id)
        if response.status < 200 or response.status >= 300:
            error = body.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                code = error["code"]
                message = str(error.get("message") or "Queue API request failed.").replace(secret, "[redacted]")
                raise QueueApiError(code, message, http_status=response.status, claim_request_id=claim_request_id)
            raise QueueApiError("HTTP_ERROR", "Queue API returned an unsuccessful HTTP status.", http_status=response.status, claim_request_id=claim_request_id)
        if body["ok"] is False:
            error = body.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise QueueApiError("API_RESPONSE_INVALID", "Queue API error envelope is invalid.", http_status=response.status, claim_request_id=claim_request_id)
            code = error["code"]
            message = str(error.get("message") or "Queue API request failed.").replace(secret, "[redacted]")
            if code == "QUEUE_EMPTY" and claim_request_id:
                return None
            raise QueueApiError(
                code,
                message,
                http_status=response.status,
                claim_request_id=claim_request_id,
                detail=_server_detail(error.get("detail"), secret=secret),
            )
        data = body.get("data")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise QueueApiError("API_RESPONSE_INVALID", "Queue API data must be an object or null.", http_status=response.status, claim_request_id=claim_request_id)
        return data

    @staticmethod
    def _require_mapping(data: Mapping[str, Any] | None, action: str) -> Mapping[str, Any]:
        if data is None:
            raise QueueApiError("API_RESPONSE_INVALID", f"Queue {action} response unexpectedly contained null data.")
        return data

    @staticmethod
    def _required_text(record: Mapping[str, Any], field: str) -> str:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise QueueApiError("API_RESPONSE_INVALID", f"Queue response is missing {field}.")
        return value

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None or value == "":
            return None
        return str(value)

    @staticmethod
    def _optional_monitor(value: Any) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        kept = {
            key: item
            for key, item in value.items()
            if key in MONITOR_FIELDS and item not in (None, "") and isinstance(item, (str, int, float)) and not isinstance(item, bool)
        }
        return kept or None

    @staticmethod
    def _optional_rank(value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):
            raise QueueApiError("API_RESPONSE_INVALID", "Queue claim response contains invalid Selection rank.")
        return value
