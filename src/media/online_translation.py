"""Subtitle translation through an online model (any OpenAI-compatible chat API).

Windows has no Apple Translation, so this is how subtitles get translated
there; on macOS the user may pick a model instead of Apple Translation. The
response has the same shape as the Apple helper's, so the rest of the subtitle
pipeline does not care which engine translated.

The user's choice lives in the ``translation`` section of the user config;
the API key lives in the Keychain / Windows Credential Manager (service
``UCI Translation API``, account = provider key), or in the environment
variable UCI_TRANSLATION_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from src.core import compat

SECRET_SERVICE = "UCI Translation API"
KEY_ENV = "UCI_TRANSLATION_API_KEY"
BATCH_CUES = 40
BATCH_CHARS = 6000
TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    base_url: str
    model: str
    key_url: str
    cost: str
    steps: tuple[str, ...]
    # Turns off "thinking" on models that have it: translation does not need it and it is slower and dearer.
    extra_body: Mapping[str, Any] | None = None


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        "deepseek", "DeepSeek（深度求索）", "https://api.deepseek.com", "deepseek-flash",
        "https://platform.deepseek.com/api_keys",
        "很便宜：翻译一条 10 分钟的视频不到 1 毛钱；需要先充值（最少 10 元，能用很久）",
        (
            "打开 https://platform.deepseek.com ，用手机号注册并登录。",
            "左侧点“充值”，充 10 元就够用很久。",
            "左侧点“API keys”→“创建 API key”，名字随便填（例如 UCI），复制生成的 key（sk- 开头）。这个 key 只显示一次，先别关页面。",
        ),
    ),
    "qwen": Provider(
        "qwen", "通义千问（阿里云百炼）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
        "https://bailian.console.aliyun.com/?tab=model#/api-key",
        "新用户有免费额度（每个模型约 100 万 tokens，90 天内有效，够翻译几百条视频），之后按量付费，也很便宜",
        (
            "打开 https://bailian.console.aliyun.com ，用阿里云账号登录（没有就用手机号注册，需要实名认证）。",
            "第一次进入会提示开通“模型服务”，点开通（开通免费）。",
            "点“API Key”（在右上角或左侧“密钥管理”里）→“创建 API Key”，复制（sk- 开头）。",
        ),
        {"enable_thinking": False},
    ),
    "doubao": Provider(
        "doubao", "豆包（字节跳动·火山方舟）", "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2-0-lite-260428",
        "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "每个模型开通时送 50 万 tokens 免费额度，之后按量付费；步骤比前两个多一步（要先开通模型）",
        (
            "打开 https://console.volcengine.com/ark ，注册火山引擎账号并完成实名认证。",
            "左侧“开通管理”，找到 Doubao-Seed 的 lite 模型，点“开通服务”。",
            "左侧“API Key 管理”→“创建 API Key”，复制。",
            "在“模型广场”点开刚开通的模型，看它的“模型 ID”（形如 doubao-seed-2-0-lite-260428）。如果和这里的默认值不一样，告诉我，或配置时加 --model <模型 ID>。",
        ),
        {"thinking": {"type": "disabled"}},
    ),
    "glm": Provider(
        "glm", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash",
        "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "完全免费（GLM-4.7-Flash 是智谱的免费模型）；翻译质量够用，但不如前三个，高峰期可能变慢",
        (
            "打开 https://bigmodel.cn ，用手机号注册并登录（需要实名认证）。",
            "右上角点“API Key”→“添加新的 API Key”，复制。",
        ),
        {"thinking": {"type": "disabled"}},
    ),
    "custom": Provider(
        "custom", "其他：任何兼容 OpenAI 接口的服务（OpenAI、Gemini、OpenRouter、Kimi 等）", "", "",
        "", "按那家服务的价格",
        (
            "按那家服务的文档拿到三样东西：API 地址（base URL，通常以 /v1 结尾）、模型名、API Key。",
            "配置时加 --base-url <API 地址> --model <模型名>。",
        ),
    ),
}
RECOMMENDED = ("deepseek", "qwen", "doubao", "glm", "custom")


class TranslationError(RuntimeError):
    """Base class; ``status`` matches the Apple helper's statuses where it can."""

    status = "failed"


class TranslationNotConfigured(TranslationError):
    status = "resource_unavailable"


class TranslationAccountError(TranslationError):
    """Wrong key, no balance, or the model is not available to this account: the user has to act."""

    status = "resource_unavailable"


class TranslationNetworkError(TranslationError):
    status = "network"


@dataclass(frozen=True)
class Settings:
    engine: str  # "apple" or "online"
    provider: str
    model: str
    base_url: str

    @property
    def label(self) -> str:
        if self.engine != "online":
            return "Apple Translation"
        name = PROVIDERS[self.provider].name if self.provider in PROVIDERS else self.provider
        return f"{name} / {self.model}"


def load_settings(config: Mapping[str, Any] | None = None) -> Settings:
    if config is None:
        from src.core.policies import PROJECT_DEFAULTS_PATH, load_config

        config = load_config(PROJECT_DEFAULTS_PATH)
    section = config.get("translation") or {}
    provider = str(section.get("provider") or "")
    preset = PROVIDERS.get(provider)
    # Windows has no Apple Translation, so an online model is the only engine there.
    engine = str(section.get("engine") or ("online" if compat.WINDOWS else "apple"))
    return Settings(
        engine="online" if compat.WINDOWS else engine,
        provider=provider,
        model=str(section.get("model") or (preset.model if preset else "")),
        base_url=str(section.get("base_url") or (preset.base_url if preset else "")).rstrip("/"),
    )


def api_key(provider: str) -> str | None:
    override = os.environ.get(KEY_ENV, "").strip()
    if override:
        return override
    from src.queue.secrets import read_secret

    return read_secret(SECRET_SERVICE, provider)


def save_api_key(provider: str, value: str) -> None:
    from src.queue.secrets import write_secret

    write_secret(SECRET_SERVICE, provider, value.strip())


def configured(settings: Settings) -> str:
    """Empty when online translation is ready to use; otherwise what is missing."""

    if not settings.provider:
        return "还没有选择翻译模型"
    if not settings.base_url or not settings.model:
        return "还没有填写 API 地址或模型名"
    if not api_key(settings.provider):
        return "还没有保存 API Key"
    return ""


# --- HTTP ------------------------------------------------------------------

Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]


def _http(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    import certifi

    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()


def _error_text(payload: bytes) -> str:
    try:
        data = json.loads(payload)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "")[:300]
        if isinstance(data, dict):
            return str(data.get("message") or data.get("msg") or "")[:300]
    except ValueError:
        pass
    return payload.decode("utf-8", errors="replace")[:300]


def chat(settings: Settings, messages: list[dict[str, str]], *, key: str, json_mode: bool = True,
         transport: Transport = _http, retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> str:
    """One chat completion; returns the reply text. Retries rate limits and server errors."""

    preset = PROVIDERS.get(settings.provider)
    body: dict[str, Any] = {"model": settings.model, "messages": messages, "temperature": 0.3, "stream": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    extras = dict(preset.extra_body or {}) if preset else {}
    body.update(extras)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "User-Agent": "universal-content-intake"}
    url = f"{settings.base_url}/chat/completions"
    attempt = 0
    while True:
        try:
            status, payload = transport(url, headers, json.dumps(body, ensure_ascii=False).encode("utf-8"), TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            if attempt < retries:
                attempt += 1
                sleep(2 ** attempt)
                continue
            raise TranslationNetworkError(f"连不上翻译服务：{getattr(error, 'reason', error)}") from None
        if status == 200:
            try:
                return str(json.loads(payload)["choices"][0]["message"]["content"] or "")
            except (ValueError, KeyError, IndexError, TypeError):
                raise TranslationError("翻译服务返回的内容看不懂") from None
        message = _error_text(payload)
        lowered = message.lower()
        if status == 400 and (extras or "response_format" in body) and any(
                word in lowered for word in ("response_format", "thinking", "enable_thinking", "unknown", "unrecognized", "not support", "invalid param")):
            # An older or different model rejects an optional parameter: try once more with plain settings.
            body = {key_: value for key_, value in body.items() if key_ not in {"response_format", *extras}}
            extras = {}
            continue
        if status in {401, 403}:
            raise TranslationKeyRejected(f"API Key 不对或没有权限（HTTP {status}）：{message}")
        if status == 402 or any(word in lowered for word in ("insufficient", "balance", "余额", "欠费", "quota")):
            raise TranslationAccountError(f"账户余额或额度不足：{message}")
        if status == 404 or ("model" in lowered and any(word in lowered for word in ("not exist", "not found", "does not exist", "不存在", "未开通"))):
            raise TranslationAccountError(f"找不到模型 {settings.model}（可能名字变了，或这个账号还没开通它）：{message}")
        if status in {408, 409, 425, 429} or status >= 500:
            if attempt < retries:
                attempt += 1
                sleep(2 ** attempt * (3 if status == 429 else 1))
                continue
            raise TranslationNetworkError(f"翻译服务暂时不可用（HTTP {status}）：{message}")
        raise TranslationError(f"翻译服务拒绝了请求（HTTP {status}）：{message}")


# --- subtitles ---------------------------------------------------------------

LANGUAGE_NAMES = {"en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语", "de": "德语", "es": "西班牙语", "ru": "俄语",
                  "pt": "葡萄牙语", "it": "意大利语", "zh-Hant": "繁体中文", "ar": "阿拉伯语", "hi": "印地语"}
SYSTEM_PROMPT = """你是专业的视频字幕翻译。把用户给出的每一行字幕从{source}翻译成简体中文。
要求：
1. 一行对一行：不合并、不拆分、不漏行，id 原样返回。
2. 口语、简洁、自然，像中文视频的字幕；不要加解释或注释。
3. 人名、产品名、公司名用通行的中文译名；没有通行译名的保留原文。
4. 只输出 JSON，格式：{{"lines": [{{"id": "…", "zh": "…"}}]}}"""


def _batches(cues: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    size = 0
    for cue in cues:
        length = len(str(cue["source_text"]))
        if current and (len(current) >= BATCH_CUES or size + length > BATCH_CHARS):
            batches.append(current)
            current, size = [], 0
        current.append(cue)
        size += length
    if current:
        batches.append(current)
    return batches


def _parse_lines(reply: str) -> dict[str, str]:
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    lines = data.get("lines") if isinstance(data, dict) else None
    result: dict[str, str] = {}
    for item in lines if isinstance(lines, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("zh"), str):
            result[item["id"]] = item["zh"].strip()
    return result


def _translate_batch(settings: Settings, key: str, source: str, batch: Sequence[Mapping[str, Any]],
                     transport: Transport, sleep: Callable[[float], None]) -> dict[str, str]:
    """Translate one batch; when the model drops or merges lines, retry once, then split the batch."""

    wanted = [str(cue["cue_id"]) for cue in batch]
    payload = {"lines": [{"id": str(cue["cue_id"]), "text": str(cue["source_text"])} for cue in batch]}
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(source=LANGUAGE_NAMES.get(source, source))},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    for _ in range(2):
        try:
            got = _parse_lines(chat(settings, messages, key=key, transport=transport, sleep=sleep))
        except ValueError:
            got = {}
        if all(got.get(cue_id) for cue_id in wanted):
            return {cue_id: got[cue_id] for cue_id in wanted}
    if len(batch) == 1:
        raise TranslationError(f"模型没有返回第 {wanted[0]} 行字幕的翻译")
    middle = len(batch) // 2
    return {**_translate_batch(settings, key, source, batch[:middle], transport, sleep),
            **_translate_batch(settings, key, source, batch[middle:], transport, sleep)}


def translate_request(request: Mapping[str, Any], settings: Settings | None = None, *,
                      transport: Transport = _http, sleep: Callable[[float], None] = time.sleep,
                      progress: Callable[[int, int], None] | None = None, key: str | None = None) -> dict[str, Any]:
    """Translate a request made by ``make_translation_request``; returns the Apple helper's response shape.

    ``key`` tries a key before it is saved; normally the saved key is used."""

    settings = settings or load_settings()
    if key is None:
        missing = configured(settings)
        if missing:
            raise TranslationNotConfigured(f"在线翻译还没配置好：{missing}。运行 bin/uci setup translation")
        key = api_key(settings.provider) or ""
    source = str(request["source_language"])
    cues = list(request["cues"])
    translated: dict[str, str] = {}
    batches = _batches(cues)
    for index, batch in enumerate(batches, start=1):
        translated.update(_translate_batch(settings, key, source, batch, transport, sleep))
        if progress:
            progress(index, len(batches))
    return {
        "schema_version": 1,
        "status": "success",
        "source_language": source,
        "target_language": "zh-Hans",
        "engine": settings.label,
        "cues": [{**cue, "translated_text": translated[str(cue["cue_id"])]} for cue in cues],
    }


class TranslationKeyRejected(TranslationAccountError):
    """The service says the key itself is wrong (HTTP 401/403)."""


def test_connection(settings: Settings, *, transport: Transport = _http, key: str | None = None) -> str:
    """Translate one sentence; returns the translation or raises TranslationError with the reason."""

    request = {"source_language": "en", "cues": [{"cue_id": "test", "start": 0.0, "end": 1.0,
                                                  "source_text": "The rocket landed safely on the drone ship."}]}
    response = translate_request(request, settings, transport=transport, sleep=lambda _: None, key=key)
    text = response["cues"][0]["translated_text"]
    if not re.search(r"[一-鿿]", text):
        raise TranslationError(f"模型回复的不是中文：{text[:80]}")
    return text
