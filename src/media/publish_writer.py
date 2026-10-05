"""Optional LLM-written publish title and copy for VIDEO deliverables.

``publish_writer()`` picks the first writer that is set up:

1. Claude: the ``anthropic`` package is installed and an API key is in the
   login Keychain (service ``UCI Anthropic API``, account ``api-key``)::

       security add-generic-password -s "UCI Anthropic API" -a api-key -w

2. Gemini: an API key is in the login Keychain (service ``UCI Gemini API``,
   account ``api-key``); no extra package is needed::

       security add-generic-password -s "UCI Gemini API" -a api-key -w

   The model defaults to ``gemini-3.5-flash-lite`` (the one Cloud Stage 8
   verified live on 2026-10-02); ``UCI_GEMINI_MODEL`` overrides it.

Without either, it returns ``None`` and publish assist uses its rule-based path
(``UCI_PUBLISH_WRITER=off`` forces that). Any API failure also falls back;
nothing here may block delivery.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

import certifi

KEYCHAIN_SERVICE = "UCI Anthropic API"
GEMINI_KEYCHAIN_SERVICE = "UCI Gemini API"
KEYCHAIN_ACCOUNT = "api-key"
MODEL = "claude-opus-5-5"
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
TITLE_MAX = 30
COPY_MAX = 200

PublishWriter = Callable[[Mapping[str, Any]], dict[str, Any]]

SYSTEM_PROMPT = """你是中文短视频平台（抖音、小红书、B站、视频号）的运营编辑。用户会给你一个海外视频的原始信息（JSON）：原标题、作者、原简介、标签、时长，以及已翻译成中文的字幕片段。视频已经烧录了中文字幕，会被搬运到中文平台发布。

请写：
- title：中文发布标题，不超过30个字。准确反映视频内容，有吸引力但不标题党、不夸大、不编造视频里没有的信息。专有名词（人名、产品名、公司名）保留常用中文译名或原文。
- copy：中文发布文案，80到200字。用一两句话讲清视频讲了什么、亮点在哪，语气自然口语化，适合直接发布。不要出现"本视频""该视频"这类生硬说法，不要加原链接。
- hashtags：3到5个中文话题标签，不带#号。

只依据给出的信息写，信息不足时写得保守一些。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "copy": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "copy", "hashtags"],
    "additionalProperties": False,
}


def _keychain_api_key(service: str = KEYCHAIN_SERVICE) -> str | None:
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w"],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    key = result.stdout.strip()
    return key if result.returncode == 0 and key else None


def claude_publish_writer() -> PublishWriter | None:
    """Return a Claude-backed writer, or ``None`` when it is not set up."""

    if os.environ.get("UCI_PUBLISH_WRITER", "").lower() == "off":  # tests and offline runs
        return None
    try:
        import anthropic
    except ImportError:
        return None
    api_key = _keychain_api_key()
    if not api_key:
        return None
    client = anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)

    def write(context: Mapping[str, Any]) -> dict[str, Any]:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(dict(context), ensure_ascii=False, indent=1)}],
        )
        if response.stop_reason != "end_turn":
            raise ValueError(f"stop_reason={response.stop_reason}")
        text = next(block.text for block in response.content if block.type == "text")
        return parse_writer_output(json.loads(text), model=str(response.model))

    return write


def gemini_publish_writer() -> PublishWriter | None:
    """Return a Gemini-backed writer, or ``None`` when no key is in the Keychain."""

    if os.environ.get("UCI_PUBLISH_WRITER", "").lower() == "off":
        return None
    api_key = _keychain_api_key(GEMINI_KEYCHAIN_SERVICE)
    if not api_key:
        return None
    model = os.environ.get("UCI_GEMINI_MODEL", "").strip() or GEMINI_MODEL

    def write(context: Mapping[str, Any]) -> dict[str, Any]:
        # Same request shape Cloud Stage 8 verified live: responseMimeType + responseJsonSchema.
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(dict(context), ensure_ascii=False, indent=1)}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": OUTPUT_SCHEMA},
        }
        request = urllib.request.Request(
            GEMINI_API_BASE + urllib.parse.quote(model, safe="") + ":generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            tls_context = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(request, timeout=120, context=tls_context) as response:
                envelope = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:  # never echo the request (it carries the key header)
            raise ValueError(f"gemini HTTP {error.code}") from None
        candidate = (envelope.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, Mapping) and not part.get("thought"))
        if not text:
            raise ValueError(f"gemini returned no text (finishReason={candidate.get('finishReason')})")
        return parse_writer_output(json.loads(text), model=model)

    return write


def publish_writer() -> PublishWriter | None:
    """Return the first configured writer (Claude, then Gemini), or ``None``."""

    return claude_publish_writer() or gemini_publish_writer()


def parse_writer_output(value: Mapping[str, Any], *, model: str = MODEL) -> dict[str, Any]:
    title = " ".join(str(value.get("title") or "").split()).strip("#＃ ")
    copy = str(value.get("copy") or "").strip()
    hashtags = [str(tag).strip().lstrip("#＃").strip() for tag in value.get("hashtags") or []]
    hashtags = [tag for tag in dict.fromkeys(hashtags) if tag][:5]
    if not title or not copy:
        raise ValueError("writer returned an empty title or copy")
    if len(title) > TITLE_MAX + 5 or len(copy) > COPY_MAX * 2:
        raise ValueError("writer output is far over the length limits")
    return {"title": title, "copy": copy, "hashtags": hashtags, "engine": model}


__all__ = ["GEMINI_MODEL", "MODEL", "PublishWriter", "claude_publish_writer", "gemini_publish_writer",
           "parse_writer_output", "publish_writer"]
