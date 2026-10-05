"""Chinese publish sheet delivered next to each finished video.

Top: what to paste when re-posting (title, copy, hashtags). Below: every
original fact about the source video (author, original link, times, sizes,
counts, the creator's description and tags). Built from the Job contract and
the Job's own ``info.md`` (which stays in the Job workspace as the system record).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Asia/Shanghai")
_LANGUAGES = {"en": "英语", "zh": "中文", "ja": "日语", "ko": "韩语", "es": "西班牙语", "fr": "法语", "de": "德语",
              "ru": "俄语", "pt": "葡萄牙语", "it": "意大利语", "ar": "阿拉伯语", "hi": "印地语"}
_HOT_REASONS = {"cold_views": "早期播放量超过该作者历史水平", "warm_relative_velocity": "播放速度超过该作者同时段历史水平",
                "like_rate": "点赞率高", "acceleration": "播放在加速"}


def _info_fields(info_markdown: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip()
            for match in re.finditer(r"^- ([A-Za-z][A-Za-z ]+): ?(.*)$", info_markdown or "", re.MULTILINE)}


def _local_time(value: Any) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        return ""
    return moment.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M（北京时间）")


def _duration(seconds: Any) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return (f"{hours}小时" if hours else "") + (f"{minutes}分" if minutes or hours else "") + f"{secs}秒"


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if value >= 10000:
        return f"{value / 10000:.1f}万".replace(".0万", "万")
    return f"{int(value):,}"


def _language(code: str) -> str:
    if not code:
        return ""
    name = _LANGUAGES.get(code.split("-")[0].lower())
    return f"{name}（{code}）" if name else code


def _hot_reason(reason: str) -> str:
    parts = [text for key, text in _HOT_REASONS.items() if key in reason]
    return "、".join(parts) or reason


def render_publish_sheet(job: Mapping[str, Any], info_markdown: str) -> str:
    metadata = job.get("source_metadata") or {}
    providers = metadata.get("providers") or {}
    probe = (providers.get("yt-dlp") or {}).get("probe") or {}
    details = probe.get("source_details") or {}
    publish = metadata.get("publish_assist") or {}
    info = _info_fields(info_markdown)

    title = str(publish.get("title") or info.get("Publish Title") or "").strip()
    copy = str(publish.get("copy") or info.get("Publish Copy") or "").strip()
    hashtags = [str(tag).strip() for tag in publish.get("hashtags") or [] if str(tag).strip()]
    lines = ["# 发布标题", "", title or "（未生成，请参考下方原标题）", "", "# 发布文案", "", copy or "（未生成）", ""]
    if hashtags:
        lines += [" ".join(f"#{tag}" for tag in hashtags), ""]

    facts: list[tuple[str, str]] = [
        ("原标题", str(probe.get("title") or info.get("Original Title") or "")),
        ("作者", str(probe.get("author") or info.get("Author") or "")),
        ("作者主页", str(probe.get("author_url") or info.get("Author URL") or "")),
        ("原视频链接", str(probe.get("canonical_url") or job.get("source_url") or "")),
        ("平台", str(probe.get("platform") or info.get("Platform") or "")),
        ("发布时间", _local_time(probe.get("published_at") or info.get("Published At"))),
        ("时长", _duration(probe.get("duration_seconds"))),
        ("原始分辨率", info.get("Original Resolution", "").replace("x", "×")),
        ("下载分辨率", info.get("Downloaded Resolution", "").replace("x", "×")),
        ("原始语言", _language(str(probe.get("original_language") or info.get("Original Language") or ""))),
        ("中文字幕来源", info.get("Subtitle Source", "")),
        ("翻译引擎", info.get("Translation Engine", "")),
    ]
    counts = "，".join(f"{label} {_number(details.get(key))}" for label, key in (
        ("播放", "view_count"), ("点赞", "like_count"), ("评论", "comment_count"), ("频道订阅", "channel_follower_count"))
        if _number(details.get(key)))
    if counts:
        facts.append(("下载时数据", counts))
    if info.get("HOT Reason"):
        facts.append(("入选原因", _hot_reason(info["HOT Reason"])))
    if info.get("Views At HOT"):
        facts.append(("入选时播放量", info["Views At HOT"]))
    facts.append(("下载完成", _local_time((job.get("timestamps") or {}).get("updated_at"))))
    lines += ["---", "", "## 视频信息", ""]
    lines += [f"- {label}：{value}" for label, value in facts if value]

    description = str(details.get("description") or "").strip()
    if description:
        lines += ["", "## 原视频简介", "", description]
    tags = [str(tag) for tag in details.get("tags") or []]
    if tags:
        lines += ["", "## 原视频标签", "", "、".join(tags)]
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_publish_sheet"]
