"""Resolve creator handles/URLs to YouTube channel IDs and summarize recent uploads (yt-dlp, metadata only)."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

from src.providers.video import DENO_PATH, YTDLP_PATH

CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
RECENT_VIDEOS = 20

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CreatorCandidate:
    query: str
    creator_name: str
    channel_id: str
    handle: str
    subscribers: int | None
    recent_count: int
    median_minutes: float | None
    share_within_limit: float | None
    uploads_per_week: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def videos_url(query: str) -> str:
    text = query.strip()
    if CHANNEL_ID.match(text):
        return f"https://www.youtube.com/channel/{text}/videos"
    if text.startswith("@"):
        return f"https://www.youtube.com/{text}/videos"
    if re.match(r"^(https?://)?(www\.|m\.)?youtube\.com/", text):
        url = text if text.startswith("http") else "https://" + text
        url = re.sub(r"[?#].*$", "", url).rstrip("/")
        url = re.sub(r"/(featured|videos|shorts|streams|playlists|community|about)$", "", url)
        return url + "/videos"
    raise ValueError(f"无法识别：{query!r}。请给出 @handle、频道链接或 UC 开头的频道 ID。")


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, timeout=180, check=False)


def resolve(query: str, *, max_minutes: int = 15, runner: Runner = _default_runner) -> CreatorCandidate:
    command = [str(YTDLP_PATH), "--flat-playlist", "--playlist-end", str(RECENT_VIDEOS), "-J",
               "--extractor-args", "youtubetab:approximate_date", videos_url(query)]
    if DENO_PATH.is_file():
        command[1:1] = ["--js-runtimes", f"deno:{DENO_PATH}"]
    result = runner(command)
    if result.returncode != 0:
        last = (result.stderr or "").strip().splitlines()[-1:] or ["未知错误"]
        raise ValueError(f"{query}：yt-dlp 读取失败（{last[0][:200]}）")
    data = json.loads(result.stdout)
    channel_id = str(data.get("channel_id") or "")
    if not CHANNEL_ID.match(channel_id):
        raise ValueError(f"{query}：没有找到频道 ID")
    return summarize(query, data, max_minutes=max_minutes)


def summarize(query: str, data: Mapping[str, Any], *, max_minutes: int = 15) -> CreatorCandidate:
    entries = [entry for entry in data.get("entries") or [] if isinstance(entry, Mapping)]
    durations = [float(entry["duration"]) / 60 for entry in entries if isinstance(entry.get("duration"), (int, float))]
    stamps = sorted(int(entry["timestamp"]) for entry in entries if isinstance(entry.get("timestamp"), (int, float)))
    per_week = None
    if len(stamps) >= 2 and stamps[-1] > stamps[0]:
        per_week = round((len(stamps) - 1) / ((stamps[-1] - stamps[0]) / 604800), 1)
    followers = data.get("channel_follower_count")
    return CreatorCandidate(
        query=query,
        creator_name=str(data.get("channel") or data.get("uploader") or "").strip(),
        channel_id=str(data.get("channel_id") or ""),
        handle=str(data.get("uploader_id") or ""),
        subscribers=int(followers) if isinstance(followers, (int, float)) else None,
        recent_count=len(entries),
        median_minutes=round(statistics.median(durations), 1) if durations else None,
        share_within_limit=round(sum(1 for value in durations if value <= max_minutes) / len(durations), 2) if durations else None,
        uploads_per_week=per_week,
    )


def advice(candidate: CreatorCandidate, *, max_minutes: int = 15) -> str:
    notes = []
    if candidate.share_within_limit is not None and candidate.share_within_limit < 0.3:
        notes.append(f"最近视频大多超过 {max_minutes} 分钟，很少会入选")
    if candidate.uploads_per_week is not None and candidate.uploads_per_week < 0.5:
        notes.append("更新很慢")
    if candidate.recent_count < 5:
        notes.append("近期视频太少，冷启动基线会不准")
    return "；".join(notes) or "合适"
