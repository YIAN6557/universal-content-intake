"""Anonymous yt-dlp VIDEO Provider with workspace-owned resume and ffprobe checks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from src.core import compat
from src.core.job import ContentType, Job
from src.core.policies import DefaultPolicies, load_default_policies
from src.media.subtitles import normalize_subtitle_tracks
from src.output.paths import OutputContractError, OutputWorkspace
from src.providers.base import (
    ContentProvider,
    ProducedArtifact,
    ProviderCapability,
    ProviderFailure,
    ProviderFailureKind,
    ProviderResult,
)


def locate_tool(name: str, env_var: str, *extra: Path) -> Path:
    """Find an external tool: env override, PATH, then common install places.

    Background jobs (LaunchAgent, Task Scheduler) run with a minimal PATH, so
    Homebrew, winget, pip --user and ~/bin installs are probed explicitly. A
    missing tool resolves to its bare name and fails later with a clear "not
    found" error.
    """

    override = os.environ.get(env_var, "").strip()
    if override:
        return Path(override).expanduser()
    found = shutil.which(name)
    if found:
        return Path(found)
    for directory in (*compat.tool_dirs(), *extra):
        for candidate_name in compat.executable_names(name):
            candidate = directory / candidate_name
            if compat.is_executable(candidate):
                return candidate
    return Path(name)


YTDLP_PATH = locate_tool("yt-dlp", "UCI_YTDLP_PATH")
FFMPEG_PATH = locate_tool("ffmpeg", "UCI_FFMPEG_PATH")
FFPROBE_PATH = locate_tool("ffprobe", "UCI_FFPROBE_PATH")
# yt-dlp cannot find deno on the Worker's minimal PATH, and YouTube extraction
# without a JS runtime is deprecated upstream, so pass it explicitly.
DENO_PATH = locate_tool("deno", "UCI_DENO_PATH", Path.home() / ".deno" / "bin")
PROVIDER_NAME = "yt-dlp"
RESUME_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 64 * 1024 * 1024


def _default_yt_dlp_command() -> tuple[str, ...]:
    command = [str(YTDLP_PATH)]
    if compat.is_executable(DENO_PATH):
        command += ["--js-runtimes", f"deno:{DENO_PATH}"]
    return tuple(command)


def _safe_diagnostic(value: str, limit: int = 4000) -> str:
    """Keep useful diagnostic text while removing query strings and fragments."""

    def redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw.rstrip(".,);]"))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "[redacted]" if parts.query else "", ""))
        except ValueError:
            return "[redacted-url]"

    cleaned = re.sub(r"https?://[^\s\"']+", redact_url, value, flags=re.IGNORECASE)
    return cleaned[-limit:]


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _iso_date(info: Mapping[str, Any]) -> str | None:
    timestamp = info.get("release_timestamp") or info.get("timestamp")
    if isinstance(timestamp, (int, float)) and math.isfinite(float(timestamp)):
        return datetime.fromtimestamp(float(timestamp), UTC).isoformat().replace("+00:00", "Z")
    raw = info.get("release_date") or info.get("upload_date")
    if isinstance(raw, str) and re.fullmatch(r"\d{8}", raw):
        try:
            return datetime.strptime(raw, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class VideoFormat:
    format_id: str
    ext: str
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    tbr: float | None
    filesize: int | None
    language: str | None = None
    # yt-dlp: 10 for the original audio track, -1 for YouTube auto-dubbed tracks.
    language_preference: int | None = None
    format_note: str | None = None

    @property
    def is_dubbed(self) -> bool:
        note = (self.format_note or "").lower()
        return "dubbed" in note or (self.language_preference is not None and self.language_preference < 0)

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec and self.video_codec.lower() != "none" and self.width and self.height)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec and self.audio_codec.lower() != "none")

    @property
    def quality_dimension(self) -> int | None:
        if self.width and self.height:
            # The shorter edge treats portrait and landscape versions of the same
            # 1080p class consistently (e.g. 1080x1920 is still 1080p).
            return min(self.width, self.height)
        return self.height or self.width

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "VideoFormat | None":
        format_id = value.get("format_id")
        if not isinstance(format_id, (str, int)):
            return None
        format_id = str(format_id)
        # Format IDs become a yt-dlp selector argument. Reject syntax-bearing IDs.
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", format_id):
            return None
        ext = str(value.get("ext") or "").lower()
        return cls(
            format_id=format_id,
            ext=ext,
            video_codec=str(value.get("vcodec")) if value.get("vcodec") is not None else None,
            audio_codec=str(value.get("acodec")) if value.get("acodec") is not None else None,
            width=_positive_int(value.get("width")),
            height=_positive_int(value.get("height")),
            fps=_finite_float(value.get("fps")),
            tbr=_finite_float(value.get("tbr")),
            filesize=_positive_int(value.get("filesize") or value.get("filesize_approx")),
            language=str(value.get("language")) if value.get("language") else None,
            language_preference=value.get("language_preference") if isinstance(value.get("language_preference"), int) else None,
            format_note=str(value.get("format_note")) if value.get("format_note") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedVideoMetadata:
    platform: str
    video_id: str
    canonical_url: str
    title: str
    author: str | None
    author_url: str | None
    published_at: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    original_language: str | None
    formats: tuple[VideoFormat, ...]
    subtitle_languages: tuple[str, ...]
    automatic_subtitle_languages: tuple[str, ...]
    subtitle_tracks: tuple[dict[str, Any], ...]
    availability: str | None
    live_status: str | None
    is_live: bool
    source_type: str
    playlist_facts: dict[str, Any]
    # Creator-written facts used for publishing: description, tags, counts.
    source_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "video_id": self.video_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "author": self.author,
            "author_url": self.author_url,
            "published_at": self.published_at,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "original_language": self.original_language,
            "available_formats": [item.to_dict() for item in self.formats],
            "subtitle_availability": {
                "manual_languages": list(self.subtitle_languages),
                "automatic_languages": list(self.automatic_subtitle_languages),
            },
            "subtitle_tracks": [dict(track) for track in self.subtitle_tracks],
            "provider_facts": {
                "availability": self.availability,
                "live_status": self.live_status,
                "is_live": self.is_live,
                "source_type": self.source_type,
                **self.playlist_facts,
            },
            "source_details": dict(self.source_details),
        }


def normalize_yt_dlp_info(info: Mapping[str, Any], source_url: str) -> NormalizedVideoMetadata:
    """Convert yt-dlp JSON into a small Provider-neutral video record."""

    source_type = str(info.get("_type") or "video")
    if source_type in {"playlist", "multi_video"} or "entries" in info:
        raise ProviderFailure(
            ProviderFailureKind.UNSUPPORTED,
            PROVIDER_NAME,
            "Stage 2 accepts a single video URL; playlist or collection URLs are rejected.",
            details={"source_type": source_type, "playlist_count": info.get("playlist_count")},
        )

    availability = str(info.get("availability") or "") or None
    if availability in {"needs_auth", "subscriber_only", "premium_only", "private"}:
        raise ProviderFailure(
            ProviderFailureKind.AUTH_REQUIRED,
            PROVIDER_NAME,
            f"The source requires authorization ({availability}).",
            details={"availability": availability},
        )
    if availability in {"unavailable", "removed", "deleted"} or info.get("is_unavailable") is True:
        raise ProviderFailure(
            ProviderFailureKind.INVALID_URL,
            PROVIDER_NAME,
            "The source is unavailable or has been removed.",
            details={"availability": availability},
        )

    live_status = str(info.get("live_status") or "") or None
    is_live = bool(info.get("is_live")) or live_status == "is_live"
    if live_status == "is_upcoming":
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, PROVIDER_NAME, "Upcoming live streams are not downloadable video sources.")

    formats = tuple(
        item
        for raw in info.get("formats", [])
        if isinstance(raw, Mapping)
        if (item := VideoFormat.from_mapping(raw)) is not None
    )
    subtitle_languages = tuple(sorted(str(key) for key in (info.get("subtitles") or {}).keys()))
    automatic_languages = tuple(sorted(str(key) for key in (info.get("automatic_captions") or {}).keys()))
    subtitle_tracks = tuple(
        track.to_dict()
        for track in normalize_subtitle_tracks(info.get("subtitles"), info.get("automatic_captions"))
    )
    video_id = str(info.get("id") or "").strip()
    canonical_url = str(info.get("webpage_url") or info.get("original_url") or source_url)
    if not video_id or not str(info.get("title") or "").strip():
        raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, "yt-dlp JSON lacks the required video ID or title.")

    playlist_facts = {
        key: info.get(key)
        for key in ("playlist_id", "playlist_title", "playlist_count", "playlist_index")
        if info.get(key) is not None
    }
    return NormalizedVideoMetadata(
        platform=str(info.get("extractor_key") or info.get("extractor") or "unknown"),
        video_id=video_id,
        canonical_url=canonical_url,
        title=str(info["title"]),
        author=str(info.get("channel") or info.get("uploader") or "") or None,
        author_url=str(info.get("channel_url") or info.get("uploader_url") or "") or None,
        published_at=_iso_date(info),
        duration_seconds=_finite_float(info.get("duration")),
        width=_positive_int(info.get("width")),
        height=_positive_int(info.get("height")),
        original_language=str(info.get("language") or "").strip() or None,
        formats=formats,
        subtitle_languages=subtitle_languages,
        automatic_subtitle_languages=automatic_languages,
        subtitle_tracks=subtitle_tracks,
        availability=availability,
        live_status=live_status,
        is_live=is_live,
        source_type=source_type,
        playlist_facts=playlist_facts,
        source_details=_source_details(info),
    )


_DESCRIPTION_LIMIT = 5000
_TAG_LIMIT = 30


def _source_details(info: Mapping[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    description = str(info.get("description") or "").strip()
    if description:
        details["description"] = description[:_DESCRIPTION_LIMIT]
    tags = [str(tag).strip() for tag in info.get("tags") or [] if str(tag).strip()]
    if tags:
        details["tags"] = tags[:_TAG_LIMIT]
    categories = [str(item).strip() for item in info.get("categories") or [] if str(item).strip()]
    if categories:
        details["categories"] = categories
    for key in ("view_count", "like_count", "comment_count", "channel_follower_count"):
        value = info.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            details[key] = value
    handle = str(info.get("uploader_id") or "").strip()
    if handle:
        details["uploader_id"] = handle
    return details


def _requested_resolution(job: Job, policies: DefaultPolicies) -> int | None:
    video_options = job.requested_options.get("video", {})
    raw = video_options.get("quality") if isinstance(video_options, dict) else None
    raw = raw or policies.video_target_resolution
    normalized = str(raw).strip().lower().replace(" ", "")
    aliases = {"2k": 1440, "qhd": 1440, "4k": 2160, "uhd": 2160, "highest": None, "best": None}
    if normalized in aliases:
        return aliases[normalized]
    match = re.fullmatch(r"(\d{3,4})p?", normalized)
    if not match:
        raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, f"Invalid video quality request: {raw!r}.")
    resolution = int(match.group(1))
    if resolution < 144 or resolution > 4320:
        raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, f"Unsupported video quality request: {raw!r}.")
    return resolution


@dataclass(frozen=True)
class FormatSelection:
    format_ids: tuple[str, ...]
    selector: str
    requested_resolution: int | None
    selected_resolution: int
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    input_containers: tuple[str, ...]
    requires_merge: bool
    direct_compatible_mp4: bool

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_ids": list(self.format_ids),
            "selector": self.selector,
            "requested_resolution": self.requested_resolution,
            "selected_resolution": self.selected_resolution,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "input_containers": list(self.input_containers),
            "requires_merge": self.requires_merge,
            "direct_compatible_mp4": self.direct_compatible_mp4,
            "transcode": False,
        }


def _is_h264(codec: str | None) -> bool:
    value = (codec or "").lower()
    return value.startswith(("avc1", "avc3", "h264"))


def _is_aac(codec: str | None) -> bool:
    value = (codec or "").lower()
    return value.startswith(("mp4a", "aac"))


def select_video_format(
    formats: Sequence[VideoFormat],
    *,
    requested_resolution: int | None,
    preferred_video_codec: str = "h264",
    preferred_audio_codec: str = "aac",
) -> FormatSelection:
    """Select the highest source quality at or below the request, without upscaling."""

    videos = [item for item in formats if item.has_video and item.quality_dimension is not None]
    if requested_resolution is not None:
        videos = [item for item in videos if item.quality_dimension <= requested_resolution]
    if not videos:
        raise ProviderFailure(
            ProviderFailureKind.UNSUPPORTED,
            PROVIDER_NAME,
            "No available source format is at or below the requested resolution; Stage 2 does not upscale or transcode.",
        )
    selected_resolution = max(int(item.quality_dimension) for item in videos)
    videos = [item for item in videos if item.quality_dimension == selected_resolution]
    audios = [item for item in formats if item.has_audio and not item.has_video]
    # Videos with YouTube auto-dubbing list one audio track per language; keep
    # the original soundtrack (highest language preference, never a dub).
    originals = [item for item in audios if not item.is_dubbed]
    if originals:
        audios = originals
    if any(item.language_preference is not None for item in audios):
        top = max(item.language_preference if item.language_preference is not None else -100 for item in audios)
        audios = [item for item in audios if (item.language_preference if item.language_preference is not None else -100) == top]
    combined = [item for item in videos if item.has_audio]
    source_has_audio = bool(audios or combined or any(item.has_audio for item in formats))
    candidates: list[tuple[tuple[float, ...], FormatSelection]] = []

    def rank(video: VideoFormat, audio: VideoFormat | None, *, merged: bool) -> tuple[float, ...]:
        vcodec = video.video_codec or ""
        acodec = (audio.audio_codec if audio else video.audio_codec) or ""
        is_preferred_v = _is_h264(vcodec) if preferred_video_codec.lower() == "h264" else vcodec.lower().startswith(preferred_video_codec.lower())
        is_preferred_a = _is_aac(acodec) if preferred_audio_codec.lower() == "aac" else acodec.lower().startswith(preferred_audio_codec.lower())
        containers = (video.ext, audio.ext) if audio else (video.ext,)
        mp4_score = sum(1 for ext in containers if ext in {"mp4", "m4a"})
        bitrate = (video.tbr or 0.0) + (audio.tbr or 0.0 if audio else 0.0)
        fps = video.fps or 0.0
        return (float(is_preferred_v), float(is_preferred_a), float(mp4_score), float(not merged), bitrate, fps)

    for item in combined:
        direct_compatible = item.ext == "mp4" and _is_h264(item.video_codec) and _is_aac(item.audio_codec)
        selection = FormatSelection(
            format_ids=(item.format_id,),
            selector=item.format_id,
            requested_resolution=requested_resolution,
            selected_resolution=selected_resolution,
            width=int(item.width),
            height=int(item.height),
            video_codec=item.video_codec or "unknown",
            audio_codec=item.audio_codec,
            input_containers=(item.ext,),
            requires_merge=False,
            direct_compatible_mp4=direct_compatible,
        )
        candidates.append((rank(item, None, merged=False), selection))

    for video in videos:
        if video.has_audio:
            continue
        if not source_has_audio:
            selection = FormatSelection(
                format_ids=(video.format_id,),
                selector=video.format_id,
                requested_resolution=requested_resolution,
                selected_resolution=selected_resolution,
                width=int(video.width),
                height=int(video.height),
                video_codec=video.video_codec or "unknown",
                audio_codec=None,
                input_containers=(video.ext,),
                requires_merge=False,
                direct_compatible_mp4=video.ext == "mp4" and _is_h264(video.video_codec),
            )
            candidates.append((rank(video, None, merged=False), selection))
        else:
            for audio in audios:
                selector = f"{video.format_id}+{audio.format_id}"
                selection = FormatSelection(
                    format_ids=(video.format_id, audio.format_id),
                    selector=selector,
                    requested_resolution=requested_resolution,
                    selected_resolution=selected_resolution,
                    width=int(video.width),
                    height=int(video.height),
                    video_codec=video.video_codec or "unknown",
                    audio_codec=audio.audio_codec,
                    input_containers=(video.ext, audio.ext),
                    requires_merge=True,
                    direct_compatible_mp4=False,
                )
                candidates.append((rank(video, audio, merged=True), selection))

    if not candidates:
        reason = "No compatible audio stream is available for the selected video." if source_has_audio else "No usable video format is available."
        raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, PROVIDER_NAME, reason)
    return max(candidates, key=lambda value: value[0])[1]


@dataclass(frozen=True)
class VideoVerification:
    container: str
    duration_seconds: float
    width: int
    height: int
    quality_dimension: int
    video_codec: str
    audio_codec: str | None
    has_audio: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_ffprobe_result(raw: Mapping[str, Any]) -> VideoVerification:
    """Validate and normalize the essential ffprobe JSON facts."""

    format_data = raw.get("format")
    streams = raw.get("streams")
    if not isinstance(format_data, Mapping) or not isinstance(streams, list):
        raise ValueError("ffprobe result must contain format and streams")
    names = {item.strip().lower() for item in str(format_data.get("format_name") or "").split(",") if item.strip()}
    if "mp4" not in names:
        raise ValueError("verified source container is not MP4")
    duration = _finite_float(format_data.get("duration"))
    if duration is None:
        raise ValueError("ffprobe did not report a valid positive duration")
    video_streams = [
        item for item in streams
        if isinstance(item, Mapping)
        and item.get("codec_type") == "video"
        and not (isinstance(item.get("disposition"), Mapping) and item["disposition"].get("attached_pic"))
    ]
    if not video_streams:
        raise ValueError("ffprobe found no video stream")
    video = video_streams[0]
    width = _positive_int(video.get("width"))
    height = _positive_int(video.get("height"))
    codec = str(video.get("codec_name") or "")
    if not width or not height or not codec:
        raise ValueError("ffprobe video stream lacks width, height, or codec")
    audio_streams = [item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"]
    audio_codec = str(audio_streams[0].get("codec_name") or "unknown") if audio_streams else None
    return VideoVerification(
        container="mp4",
        duration_seconds=duration,
        width=width,
        height=height,
        quality_dimension=min(width, height),
        video_codec=codec,
        audio_codec=audio_codec,
        has_audio=bool(audio_streams),
    )


def verify_downloaded_video(
    raw: Mapping[str, Any],
    selection: FormatSelection,
    *,
    expected_audio: bool,
) -> VideoVerification:
    result = normalize_ffprobe_result(raw)
    if result.width != selection.width or result.height != selection.height:
        raise ValueError(
            f"downloaded dimensions {result.width}x{result.height} do not match selected format "
            f"{selection.width}x{selection.height}"
        )
    if result.quality_dimension != selection.selected_resolution:
        raise ValueError("downloaded resolution does not match the selected source format")
    if selection.requested_resolution is not None and result.quality_dimension > selection.requested_resolution:
        raise ValueError("downloaded resolution exceeds the requested maximum")
    if expected_audio and not result.has_audio:
        raise ValueError("the source exposes audio but the downloaded file has no audio stream")
    return result


def _validate_source_url(source_url: str) -> None:
    try:
        parsed = urlsplit(source_url)
    except ValueError as error:
        raise ProviderFailure(ProviderFailureKind.INVALID_URL, PROVIDER_NAME, "Invalid source URL.") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderFailure(ProviderFailureKind.INVALID_URL, PROVIDER_NAME, "A valid HTTP or HTTPS source URL is required.")


def _classify_process_failure(returncode: int, text: str) -> ProviderFailureKind:
    """Use exit status and several recognized diagnostic categories, not one phrase."""

    if returncode == 0:
        raise ValueError("cannot classify a successful process")
    lower = text.lower()
    auth_markers = (
        "sign in to confirm", "sign in required", "login_required", "login required", "authentication required", "http error 401", "members-only",
        "members only", "age-restricted", "age restricted", "subscriber-only", "private video",
    )
    invalid_markers = (
        "http error 404", "404 not found", "video has been removed", "video was removed",
        "video unavailable", "video is unavailable", "no longer available", "has been deleted", "does not exist",
        "private video", "unsupported url",
    )
    network_markers = (
        "timed out", "timeout", "connection reset", "connection refused", "temporary failure",
        "name or service not known", "could not resolve", "network is unreachable", "http error 429",
        "http error 500", "http error 502", "http error 503", "http error 504", "incomplete read",
        "unable to download webpage", "tls", "ssl:",
    )
    if any(marker in lower for marker in auth_markers):
        return ProviderFailureKind.AUTH_REQUIRED
    if any(marker in lower for marker in invalid_markers):
        if "unsupported url" in lower:
            return ProviderFailureKind.UNSUPPORTED
        return ProviderFailureKind.INVALID_URL
    if any(marker in lower for marker in network_markers):
        return ProviderFailureKind.NETWORK
    return ProviderFailureKind.FAILED


def _is_youtube_source(source_url: str) -> bool:
    try:
        hostname = (urlsplit(source_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "youtube.com" or hostname.endswith(".youtube.com") or hostname == "youtu.be"


def _with_chrome_session(command: Sequence[str]) -> list[str]:
    """Use yt-dlp's browser-session reader without creating a cookie export."""
    if not command:
        raise ValueError("yt-dlp command cannot be empty")
    return [*command[:-1], "--cookies-from-browser", "chrome", command[-1]]


def _safe_json_loads(data: str, *, description: str) -> dict[str, Any]:
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError(f"{description} JSON exceeded the size limit")
    decoded = json.loads(data)
    if not isinstance(decoded, dict):
        raise ValueError(f"{description} JSON must be an object")
    return decoded


def _json_safe_process_error(returncode: int, stderr: str, stdout: str = "") -> tuple[ProviderFailureKind, str]:
    combined = f"{stderr}\n{stdout}"
    kind = _classify_process_failure(returncode, combined)
    return kind, _safe_diagnostic(combined.strip() or f"yt-dlp exited with code {returncode}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("resume manifest must be a JSON object")
    return decoded


def _file_manifest(download_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    allowed = re.compile(r"^source_video(?:\.f?[A-Za-z0-9_.-]+)?\.[A-Za-z0-9]{2,8}(?:\.part(?:-Frag\d+)?|\.ytdl|\.temp)?$")
    for entry in sorted(download_dir.iterdir(), key=lambda item: item.name):
        if entry.is_symlink() or not entry.is_file() or not allowed.fullmatch(entry.name):
            raise ValueError(f"unknown or unsafe partial artifact in Provider workspace: {entry.name}")
        files.append({"name": entry.name, "size": entry.stat().st_size})
    return files


def _safe_temp_path(workspace: OutputWorkspace, relative_path: str) -> Path:
    """Resolve a Job-temp path only after rejecting symlink components."""

    root = workspace.paths.temp_dir
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, "Job temp directory is missing, redirected, or unsafe.")
    try:
        candidate = workspace.temp_path(relative_path)
    except OutputContractError as error:
        raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, "Provider path escaped the Job temp boundary.") from error
    current = root
    for part in Path(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, PROVIDER_NAME, "Symlink paths are not allowed inside Job temp.")
    return candidate


class VideoProvider(ContentProvider):
    """Stage 2 single-video provider. It validates source artifacts but never completes a Job."""

    name = PROVIDER_NAME
    capability = ProviderCapability(
        content_types=frozenset({ContentType.VIDEO}),
        supports_probe=True,
        supports_fetch=True,
        supports_resume=True,
    )

    def __init__(
        self,
        *,
        policies: DefaultPolicies | None = None,
        defaults_path: Path | None = None,
        yt_dlp_command: Sequence[str] | None = None,
        ffprobe_command: Sequence[str] | None = None,
        ffmpeg_path: Path = FFMPEG_PATH,
        command_timeout_seconds: int = 900,
    ) -> None:
        self.policies = policies or load_default_policies(defaults_path or Path(__file__).resolve().parents[2] / "config" / "defaults.yaml")
        self.yt_dlp_command = tuple(str(part) for part in (yt_dlp_command or _default_yt_dlp_command()))
        self.ffprobe_command = tuple(str(part) for part in (ffprobe_command or (FFPROBE_PATH,)))
        self.ffmpeg_path = Path(ffmpeg_path)
        self.command_timeout_seconds = command_timeout_seconds
        if not self.yt_dlp_command or not self.ffprobe_command or command_timeout_seconds <= 0:
            raise ValueError("Provider commands and positive timeout are required")

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise ProviderFailure(
                ProviderFailureKind.NETWORK,
                self.name,
                f"Provider command exceeded {self.command_timeout_seconds} seconds.",
                details={"timeout_seconds": self.command_timeout_seconds},
            ) from error
        except OSError as error:
            raise ProviderFailure(
                ProviderFailureKind.FAILED,
                self.name,
                f"Provider executable could not be started: {error.__class__.__name__}.",
                details={"executable": str(command[0])},
            ) from error

    @staticmethod
    def _authentication_state(value: Any = None) -> dict[str, Any]:
        """Accept and emit only the credential-free authentication outcome fields."""
        if not isinstance(value, Mapping):
            return {
                "auth_attempted": False,
                "auth_source": None,
                "authenticated_retry": False,
                "auth_result": "not_attempted",
            }
        attempted = value.get("auth_attempted")
        source = value.get("auth_source")
        retry_succeeded = value.get("authenticated_retry")
        result = value.get("auth_result")
        if attempted is False and source is None and retry_succeeded is False and result == "not_attempted":
            return {
                "auth_attempted": False,
                "auth_source": None,
                "authenticated_retry": False,
                "auth_result": "not_attempted",
            }
        if attempted is True and source == "chrome" and retry_succeeded is True and result == "success":
            return {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": True,
                "auth_result": "success",
            }
        if attempted is True and source == "chrome" and retry_succeeded is False and result == "failure":
            return {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": False,
                "auth_result": "failure",
            }
        return {
            "auth_attempted": False,
            "auth_source": None,
            "authenticated_retry": False,
            "auth_result": "not_attempted",
        }

    def _run_ytdlp_with_auth_fallback(
        self,
        command: Sequence[str],
        *,
        source_url: str,
        phase: str,
        authentication: Mapping[str, Any] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        """Retry a YouTube yt-dlp operation with Chrome only after an auth error."""
        auth = self._authentication_state(authentication)
        chrome_already_selected = (
            _is_youtube_source(source_url)
            and auth["auth_attempted"]
            and auth["auth_source"] == "chrome"
        )
        active_command = _with_chrome_session(command) if chrome_already_selected else list(command)
        process = self._run(active_command)
        if process.returncode == 0:
            return process, auth

        kind, cause = _json_safe_process_error(process.returncode, process.stderr, process.stdout)
        if chrome_already_selected:
            if kind is ProviderFailureKind.AUTH_REQUIRED:
                auth = {
                    "auth_attempted": True,
                    "auth_source": "chrome",
                    "authenticated_retry": False,
                    "auth_result": "failure",
                }
            raise ProviderFailure(
                kind,
                self.name,
                f"yt-dlp {phase} failed after its authorized session attempt.",
                details={"returncode": process.returncode, "cause": cause, "authentication": auth},
            )

        if kind is ProviderFailureKind.AUTH_REQUIRED and _is_youtube_source(source_url) and not self.policies.video_allow_browser_cookies:
            raise ProviderFailure(
                kind,
                self.name,
                "YouTube requires sign-in; the browser-session retry is disabled (video.allow_browser_cookies).",
                details={"returncode": process.returncode, "cause": cause, "authentication": auth},
            )
        if kind is not ProviderFailureKind.AUTH_REQUIRED or not _is_youtube_source(source_url):
            raise ProviderFailure(
                kind,
                self.name,
                f"yt-dlp could not complete the {phase}.",
                details={
                    "returncode": process.returncode,
                    "cause": cause,
                    "authentication": auth,
                },
            )

        attempted_auth = {
            "auth_attempted": True,
            "auth_source": "chrome",
            "authenticated_retry": False,
            "auth_result": "failure",
        }
        try:
            retry = self._run(_with_chrome_session(command))
        except ProviderFailure as failure:
            raise ProviderFailure(
                ProviderFailureKind.AUTH_REQUIRED,
                self.name,
                f"YouTube requires authentication; the Chrome session retry could not complete ({failure.kind.value}).",
                details={"authentication": attempted_auth},
            ) from failure
        if retry.returncode == 0:
            return retry, {
                "auth_attempted": True,
                "auth_source": "chrome",
                "authenticated_retry": True,
                "auth_result": "success",
            }
        _, retry_cause = _json_safe_process_error(retry.returncode, retry.stderr, retry.stdout)
        raise ProviderFailure(
            ProviderFailureKind.AUTH_REQUIRED,
            self.name,
            "YouTube still requires authentication after the Chrome session retry.",
            details={
                "returncode": retry.returncode,
                "cause": retry_cause,
                "authentication": attempted_auth,
            },
        )

    def _assert_video_job(self, job: Job) -> None:
        if job.resolved_content_type is not ContentType.VIDEO:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "VideoProvider requires a VIDEO Job.")
        if job.declared_content_type is not ContentType.VIDEO:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "VIDEO Job must retain the declared VIDEO type lock.")
        _validate_source_url(job.source_url)

    def _workspace(self, job: Job) -> OutputWorkspace:
        try:
            workspace_input = Path(job.workspace_path).expanduser()
            temp_input = Path(job.temp_path).expanduser()
            output_input = Path(job.output_path).expanduser()
            if any(path.is_symlink() for path in (workspace_input, temp_input, output_input)):
                raise OutputContractError("Job workspace directories cannot be symlinks")
            workspace_path = workspace_input.resolve(strict=True)
            root = workspace_path.parent
            workspace = OutputWorkspace.for_job(root, job.job_id)
            expected = workspace.paths
            actual_temp = temp_input.resolve(strict=True)
            actual_output = output_input.resolve(strict=True)
            if workspace_path != expected.job_dir.resolve() or actual_temp != expected.temp_dir.resolve() or actual_output != expected.output_dir.resolve():
                raise OutputContractError("Job workspace paths do not match the canonical job-<id>/temp and output boundaries")
            if not workspace_path.is_dir() or not actual_temp.is_dir() or not actual_output.is_dir():
                raise OutputContractError("Job workspace directories must already exist")
            return workspace
        except (OSError, OutputContractError, RuntimeError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Job workspace is invalid or unavailable.", details={"cause": str(error)}) from error

    def _quality_request(self, job: Job) -> int | None:
        return _requested_resolution(job, self.policies)

    def _selection(self, job: Job, video: NormalizedVideoMetadata) -> FormatSelection:
        return select_video_format(
            video.formats,
            requested_resolution=self._quality_request(job),
            preferred_video_codec=self.policies.video_preferred_video_codec,
            preferred_audio_codec=self.policies.video_preferred_audio_codec,
        )

    @staticmethod
    def _resume_token(job: Job, video: NormalizedVideoMetadata, selection: FormatSelection) -> str:
        identity = {
            "job_id": job.job_id,
            "video_id": video.video_id,
            "source_url": video.canonical_url,
            "selection": selection.to_dict(),
        }
        stable = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def probe(self, job: Job) -> ProviderResult:
        self._assert_video_job(job)
        previous_metadata = job.provider_metadata(self.name) or {}
        authentication = self._authentication_state(previous_metadata.get("authentication"))
        command = [
            *self.yt_dlp_command,
            "--ignore-config",
            "--no-cache-dir",
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-items", "1",
            "--no-warnings",
            job.source_url,
        ]
        process, authentication = self._run_ytdlp_with_auth_fallback(
            command,
            source_url=job.source_url,
            phase="Probe",
            authentication=authentication,
        )
        try:
            raw = _safe_json_loads(process.stdout, description="yt-dlp probe")
        except (json.JSONDecodeError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "yt-dlp returned invalid structured probe JSON.", details={"cause": _safe_diagnostic(str(error))}) from error
        try:
            video = normalize_yt_dlp_info(raw, job.source_url)
            if not any(item.has_video for item in video.formats):
                source_facts = self._ffprobe_remote_source(video.canonical_url)
                streams = source_facts.get("streams", [])
                source_video = next((item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"), None)
                source_audio = next((item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"), None)
                if not isinstance(source_video, Mapping):
                    raise ValueError("ffprobe found no video stream in this direct media source")
                enriched = tuple(
                    replace(
                        item,
                        video_codec=item.video_codec if item.has_video else str(source_video.get("codec_name") or "unknown"),
                        audio_codec=item.audio_codec if item.has_audio else (str(source_audio.get("codec_name") or "unknown") if source_audio else "none"),
                        width=item.width or _positive_int(source_video.get("width")),
                        height=item.height or _positive_int(source_video.get("height")),
                    )
                    for item in video.formats
                )
                source_format = source_facts.get("format")
                source_duration = _finite_float(source_format.get("duration")) if isinstance(source_format, Mapping) else None
                first_video = next((item for item in enriched if item.has_video), None)
                video = replace(
                    video,
                    formats=enriched,
                    width=video.width or (first_video.width if first_video else None),
                    height=video.height or (first_video.height if first_video else None),
                    duration_seconds=video.duration_seconds or source_duration,
                )
            selection = self._selection(job, video)
        except ProviderFailure:
            raise
        except (TypeError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Video metadata could not be normalized.", details={"cause": _safe_diagnostic(str(error))}) from error
        if video.is_live:
            raise ProviderFailure(ProviderFailureKind.UNSUPPORTED, self.name, "Active live streams are not bounded source-video downloads in Stage 2.", details={"live_status": video.live_status})
        token = self._resume_token(job, video, selection)
        return ProviderResult(
            metadata={
                "probe": video.to_dict(),
                "selection": selection.to_dict(),
                "provider": {"name": self.name, "version": "external-cli"},
                "authentication": authentication,
            },
            resume_token=token,
        )

    def _ffprobe_remote_source(self, source_url: str) -> dict[str, Any]:
        command = [
            *self.ffprobe_command,
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            source_url,
        ]
        process = self._run(command)
        if process.returncode != 0:
            kind = _classify_process_failure(process.returncode, f"{process.stderr}\n{process.stdout}")
            raise ProviderFailure(kind, self.name, "Direct video metadata could not be read by ffprobe.", details={"cause": _safe_diagnostic(process.stderr)})
        return _safe_json_loads(process.stdout, description="source ffprobe")

    def fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        try:
            return self._fetch(job, resume_token=resume_token)
        except ProviderFailure:
            raise
        except OSError as error:
            raise ProviderFailure(
                ProviderFailureKind.FAILED,
                self.name,
                "Video Provider workspace operation failed.",
                resume_token=resume_token,
                details={"cause": error.__class__.__name__},
            ) from error

    def _fetch(self, job: Job, *, resume_token: str | None = None) -> ProviderResult:
        self._assert_video_job(job)
        workspace = self._workspace(job)
        provider_metadata = job.provider_metadata(self.name)
        if not provider_metadata or not isinstance(provider_metadata.get("probe"), dict) or not isinstance(provider_metadata.get("selection"), dict):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "A successful Core-persisted yt-dlp Probe result is required before Fetch.")
        authentication = self._authentication_state(provider_metadata.get("authentication"))
        probe_data = provider_metadata["probe"]
        selection_data = provider_metadata["selection"]
        try:
            formats = tuple(VideoFormat(**item) for item in probe_data["available_formats"])
            video = NormalizedVideoMetadata(
                platform=probe_data["platform"],
                video_id=probe_data["video_id"],
                canonical_url=probe_data["canonical_url"],
                title=probe_data["title"],
                author=probe_data.get("author"),
                author_url=probe_data.get("author_url"),
                published_at=probe_data.get("published_at"),
                duration_seconds=probe_data.get("duration_seconds"),
                width=probe_data.get("width"),
                height=probe_data.get("height"),
                original_language=probe_data.get("original_language"),
                formats=formats,
                subtitle_languages=tuple(probe_data["subtitle_availability"]["manual_languages"]),
                automatic_subtitle_languages=tuple(probe_data["subtitle_availability"]["automatic_languages"]),
                subtitle_tracks=tuple(dict(track) for track in probe_data.get("subtitle_tracks", [])),
                availability=probe_data["provider_facts"].get("availability"),
                live_status=probe_data["provider_facts"].get("live_status"),
                is_live=bool(probe_data["provider_facts"].get("is_live")),
                source_type=probe_data["provider_facts"].get("source_type", "video"),
                playlist_facts={key: probe_data["provider_facts"][key] for key in ("playlist_id", "playlist_title", "playlist_count", "playlist_index") if key in probe_data["provider_facts"]},
                source_details=dict(probe_data.get("source_details") or {}),
            )
            selection = FormatSelection(
                format_ids=tuple(selection_data["format_ids"]),
                selector=str(selection_data["selector"]),
                requested_resolution=selection_data.get("requested_resolution"),
                selected_resolution=int(selection_data["selected_resolution"]),
                width=int(selection_data["width"]),
                height=int(selection_data["height"]),
                video_codec=str(selection_data["video_codec"]),
                audio_codec=selection_data.get("audio_codec"),
                input_containers=tuple(selection_data["input_containers"]),
                requires_merge=bool(selection_data["requires_merge"]),
                direct_compatible_mp4=bool(selection_data["direct_compatible_mp4"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Persisted Provider Probe metadata is incomplete or invalid.", details={"cause": str(error)}) from error

        expected_token = self._resume_token(job, video, selection)
        if not resume_token or resume_token != expected_token:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "A matching Core-persisted resume token is required.")

        temp_root = workspace.paths.temp_dir
        video_key = hashlib.sha256(video.video_id.encode("utf-8")).hexdigest()[:20]
        transfer_root = _safe_temp_path(workspace, f"uci_video/{video_key}")
        download_dir = _safe_temp_path(workspace, f"uci_video/{video_key}/download")
        manifest_path = _safe_temp_path(workspace, f"uci_video/{video_key}/transfer.json")
        pending_manifest = _safe_temp_path(workspace, f"uci_video/{video_key}/transfer.json.pending")
        lock_path = _safe_temp_path(workspace, f"uci_video/{video_key}/provider.lock")
        artifact_dir = _safe_temp_path(workspace, "source_video")
        artifact_path = _safe_temp_path(workspace, "source_video/source_video.mp4")
        for path in (transfer_root, download_dir, artifact_dir):
            try:
                path.relative_to(temp_root)
            except ValueError as error:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Video Provider path escaped Job temp workspace.") from error

        expected_identity = {
            "schema_version": RESUME_SCHEMA_VERSION,
            "job_id": job.job_id,
            "video_id": video.video_id,
            "source_url_sha256": hashlib.sha256(video.canonical_url.encode("utf-8")).hexdigest(),
            "selection_sha256": hashlib.sha256(json.dumps(selection.to_dict(), sort_keys=True).encode("utf-8")).hexdigest(),
            "resume_token": expected_token,
            "artifact_relative_path": "temp/source_video/source_video.mp4",
        }
        if not transfer_root.exists():
            transfer_root.mkdir(parents=True, exist_ok=False)
            download_dir.mkdir()
            self._write_manifest(manifest_path, {**expected_identity, "state": "downloading", "partial_files": []})
        elif transfer_root.is_symlink() or not transfer_root.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Stale or unowned partial Provider state was found; it will not be reused.")
        else:
            allowed = {"download", "transfer.json", "transfer.json.pending", "provider.lock"}
            unexpected = sorted(entry.name for entry in transfer_root.iterdir() if entry.name not in allowed)
            if unexpected:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Unknown files were found in the Provider resume area; none were reused.", details={"entries": unexpected})

        if lock_path.is_symlink():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Video Provider workspace lock is unsafe.")

        try:
            lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            lock_handle = os.fdopen(lock_descriptor, "a+")
        except OSError as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Video Provider workspace lock could not be opened.", details={"cause": str(error)}) from error
        with lock_handle:
            try:
                compat.lock(lock_handle.fileno(), blocking=False)
            except BlockingIOError as error:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Another VIDEO attempt is already using this Job workspace.") from error
            try:
                manifest = self._validated_manifest(manifest_path, expected_identity)
                if pending_manifest.exists():
                    if pending_manifest.is_symlink() or not pending_manifest.is_file():
                        raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Unsafe pending resume metadata was found.")
                    pending_manifest.unlink()
                if manifest["state"] == "complete":
                    return self._return_completed_artifact(
                        job, workspace, video, selection, manifest, artifact_path,
                        resume_token=expected_token, authentication=authentication,
                    )
                if manifest["state"] == "rejected":
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "A previous VIDEO artifact failed verification and is not reusable.", resume_token=expected_token)
                if manifest["state"] == "verified_pending_publish":
                    return self._publish_verified(
                        job, workspace, video, selection, manifest, None,
                        download_dir, manifest_path, expected_identity, expected_token,
                        authentication=authentication,
                    )
                if manifest["state"] not in {"downloading", "interrupted"}:
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Resume manifest has an unsupported state.", resume_token=expected_token)
                if not download_dir.is_dir() or download_dir.is_symlink():
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Resume download directory is missing or unsafe.", resume_token=expected_token)
                try:
                    _file_manifest(download_dir)
                except (OSError, ValueError) as error:
                    raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Unknown or stale partial files were found; none were reused.", resume_token=expected_token, details={"cause": str(error)}) from error
                return self._download_and_verify(
                    job, workspace, video, selection, transfer_root, download_dir,
                    manifest_path, expected_identity, expected_token,
                    authentication=authentication,
                )
            finally:
                compat.unlock(lock_handle.fileno())

    @staticmethod
    def _write_manifest(path: Path, value: Mapping[str, Any]) -> None:
        _atomic_json(path, value)

    def _validated_manifest(self, path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("resume manifest must be a regular file")
            manifest = _load_json(path)
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise ValueError("resume identity does not match this Job, video, source, or selected format")
            if not isinstance(manifest.get("state"), str):
                raise ValueError("resume manifest lacks a state")
            return manifest
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Stale or invalid resume metadata was rejected.", details={"cause": str(error)}) from error

    def _download_and_verify(
        self,
        job: Job,
        workspace: OutputWorkspace,
        video: NormalizedVideoMetadata,
        selection: FormatSelection,
        transfer_root: Path,
        download_dir: Path,
        manifest_path: Path,
        expected_identity: Mapping[str, Any],
        resume_token: str,
        *,
        authentication: Mapping[str, Any],
    ) -> ProviderResult:
        command = [
            *self.yt_dlp_command,
            "--ignore-config",
            "--no-cache-dir",
            "--no-warnings",
            "--no-playlist",
            "--format", selection.selector,
            "--merge-output-format", self.policies.video_container,
            "--remux-video", self.policies.video_container,
            "--ffmpeg-location", str(self.ffmpeg_path),
            "--retries", str(self.policies.video_download_retries),
            "--fragment-retries", str(self.policies.video_fragment_retries),
            "--extractor-retries", str(self.policies.video_extractor_retries),
            "--file-access-retries", "1",
            "--continue",
            "--no-overwrites",
            "--no-write-info-json",
            "--no-write-subs",
            "--no-write-auto-subs",
            "--no-write-thumbnail",
            "--no-embed-metadata",
            "--paths", f"temp:{download_dir}",
            "--output", str(download_dir / "source_video.%(ext)s"),
            video.canonical_url,
        ]
        try:
            process, authentication = self._run_ytdlp_with_auth_fallback(
                command,
                source_url=video.canonical_url,
                phase="Fetch",
                authentication=authentication,
            )
        except ProviderFailure as failure:
            self._record_interrupted(manifest_path, expected_identity, download_dir)
            raise ProviderFailure(
                ProviderFailureKind.NETWORK if failure.kind is ProviderFailureKind.NETWORK else failure.kind,
                self.name,
                failure.message,
                resume_token=resume_token,
                details=failure.details,
            ) from failure
        if process.returncode != 0:
            kind, cause = _json_safe_process_error(process.returncode, process.stderr, process.stdout)
            self._record_interrupted(manifest_path, expected_identity, download_dir)
            raise ProviderFailure(
                kind,
                self.name,
                "yt-dlp failed after its configured retry policy.",
                resume_token=resume_token,
                details={"returncode": process.returncode, "cause": cause},
            )
        try:
            files = _file_manifest(download_dir)
            candidates = [download_dir / item["name"] for item in files if not re.search(r"\.part(?:-Frag\d+)?$|\.ytdl$|\.temp$", item["name"])]
            if len(candidates) != 1:
                raise ValueError(f"expected one completed source file, found {len(candidates)}")
            staged = candidates[0]
            if staged.stat().st_size <= 0:
                raise ValueError("downloaded source file is empty")
            ffprobe_raw = self._ffprobe(staged)
            verification = verify_downloaded_video(ffprobe_raw, selection, expected_audio=selection.has_audio)
            if staged.suffix.lower() != ".mp4":
                raise ValueError(f"Stage 2 source must be MP4 after merge/remux, got {staged.suffix or 'no extension'}")
            artifact = {
                "path": "temp/source_video/source_video.mp4",
                "size_bytes": staged.stat().st_size,
                "sha256": _sha256_file(staged),
                "ffprobe": verification.to_dict(),
                "source_video_id": video.video_id,
                "selected_format": selection.to_dict(),
            }
            manifest = {
                **expected_identity,
                "state": "verified_pending_publish",
                "artifact": artifact,
                "operations": self._operation_facts(selection, verification, f"{process.stderr}\n{process.stdout}"),
            }
            self._write_manifest(manifest_path, manifest)
            return self._publish_verified(
                job, workspace, video, selection, manifest, staged,
                download_dir, manifest_path, expected_identity, resume_token,
                authentication=authentication,
            )
        except ProviderFailure:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            rejected = {**expected_identity, "state": "rejected", "cause": _safe_diagnostic(str(error))}
            self._write_manifest(manifest_path, rejected)
            raise ProviderFailure(
                ProviderFailureKind.FAILED,
                self.name,
                "yt-dlp reported success, but the downloaded source failed artifact or ffprobe verification.",
                resume_token=resume_token,
                details={"cause": _safe_diagnostic(str(error))},
            ) from error

    def _ffprobe(self, path: Path) -> dict[str, Any]:
        command = [
            *self.ffprobe_command,
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            str(path),
        ]
        process = self._run(command)
        if process.returncode != 0:
            raise ValueError(f"ffprobe exited with code {process.returncode}: {_safe_diagnostic(process.stderr)}")
        return _safe_json_loads(process.stdout, description="ffprobe")

    @staticmethod
    def _operation_facts(selection: FormatSelection, verification: VideoVerification, process_output: str) -> dict[str, Any]:
        return {
            "selected_input_video_codec": selection.video_codec,
            "selected_input_audio_codec": selection.audio_codec,
            "verified_output_video_codec": verification.video_codec,
            "verified_output_audio_codec": verification.audio_codec,
            "final_stage2_source_container": "mp4",
            "merge": bool(selection.requires_merge and verification.has_audio),
            "merge_log_observed": bool(re.search(r"\[Merger\].*Merging formats", process_output, flags=re.IGNORECASE)),
            "remux": bool(re.search(r"\[VideoRemuxer\].*Remuxing video", process_output, flags=re.IGNORECASE)),
            "transcode": False,
        }

    def _record_interrupted(self, manifest_path: Path, identity: Mapping[str, Any], download_dir: Path) -> None:
        try:
            partial_files = _file_manifest(download_dir)
        except (OSError, ValueError) as error:
            partial_files = [{"error": _safe_diagnostic(str(error))}]
        self._write_manifest(manifest_path, {**identity, "state": "interrupted", "partial_files": partial_files})

    def _publish_verified(
        self,
        job: Job,
        workspace: OutputWorkspace,
        video: NormalizedVideoMetadata,
        selection: FormatSelection,
        manifest: dict[str, Any],
        staged: Path | None,
        download_dir: Path,
        manifest_path: Path,
        identity: Mapping[str, Any],
        resume_token: str,
        *,
        authentication: Mapping[str, Any],
    ) -> ProviderResult:
        artifact_info = manifest.get("artifact")
        if not isinstance(artifact_info, dict):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Verified resume manifest lacks artifact identity.", resume_token=resume_token)
        final_path = _safe_temp_path(workspace, "source_video/source_video.mp4")
        artifact_dir = _safe_temp_path(workspace, "source_video")
        if artifact_dir.exists() and (artifact_dir.is_symlink() or not artifact_dir.is_dir()):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Validated source artifact directory is unsafe.", resume_token=resume_token)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        unexpected = sorted(entry.name for entry in artifact_dir.iterdir() if entry.name != final_path.name)
        if unexpected:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Unknown files are present beside the stable SOURCE_VIDEO artifact.", resume_token=resume_token, details={"entries": unexpected})
        if final_path.exists():
            candidate = final_path
        elif staged is not None:
            candidate = staged
        else:
            candidates = []
            try:
                for item in _file_manifest(download_dir):
                    path = download_dir / item["name"]
                    if item["size"] == int(artifact_info.get("size_bytes", -1)) and _sha256_file(path) == artifact_info.get("sha256"):
                        candidates.append(path)
            except (OSError, ValueError) as error:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Verified staged source could not be recovered safely.", resume_token=resume_token, details={"cause": _safe_diagnostic(str(error))}) from error
            if len(candidates) != 1:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Verified staged source is missing or ambiguous.", resume_token=resume_token)
            candidate = candidates[0]
        if candidate.is_symlink() or not candidate.is_file():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Validated source file is missing or unsafe.", resume_token=resume_token)
        if candidate.stat().st_size != int(artifact_info.get("size_bytes", -1)) or _sha256_file(candidate) != artifact_info.get("sha256"):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Validated source file identity changed; it will not be reused or overwritten.", resume_token=resume_token)
        if candidate != final_path:
            if final_path.exists():
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Stable SOURCE_VIDEO path already exists and will not be overwritten.", resume_token=resume_token)
            os.replace(candidate, final_path)
        manifest = {**identity, "state": "complete", "artifact": artifact_info, "operations": manifest.get("operations", {})}
        self._write_manifest(manifest_path, manifest)
        return self._result(
            video, selection, artifact_info, manifest["operations"], resume_token,
            resumed=False, authentication=authentication,
        )

    def _return_completed_artifact(
        self,
        job: Job,
        workspace: OutputWorkspace,
        video: NormalizedVideoMetadata,
        selection: FormatSelection,
        manifest: dict[str, Any],
        artifact_path: Path,
        *,
        resume_token: str,
        authentication: Mapping[str, Any],
    ) -> ProviderResult:
        artifact_info = manifest.get("artifact")
        artifact_dir = _safe_temp_path(workspace, "source_video")
        if artifact_dir.is_dir():
            unexpected = sorted(entry.name for entry in artifact_dir.iterdir() if entry.name != artifact_path.name)
            if unexpected:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Unknown files are present beside the stable SOURCE_VIDEO artifact.", resume_token=resume_token)
        if not isinstance(artifact_info, dict) or artifact_path.is_symlink() or not artifact_path.is_file():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Completed SOURCE_VIDEO artifact is missing or unsafe.", resume_token=resume_token)
        if artifact_path.stat().st_size != int(artifact_info.get("size_bytes", -1)) or _sha256_file(artifact_path) != artifact_info.get("sha256"):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Completed SOURCE_VIDEO artifact identity changed.", resume_token=resume_token)
        ffprobe_raw = self._ffprobe(artifact_path)
        verified = verify_downloaded_video(ffprobe_raw, selection, expected_audio=selection.has_audio)
        if verified.to_dict() != artifact_info.get("ffprobe"):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Completed SOURCE_VIDEO ffprobe facts changed.", resume_token=resume_token)
        return self._result(
            video, selection, artifact_info, manifest.get("operations", {}),
            resume_token, resumed=True, authentication=authentication,
        )

    def download_subtitle_track(self, job: Job, track: Mapping[str, Any]) -> dict[str, Any]:
        """Download one catalogued subtitle track into this Job's temp area only."""
        self._assert_video_job(job)
        workspace = self._workspace(job)
        language = str(track.get("language_code") or "")
        source = str(track.get("source") or "")
        track_id = str(track.get("track_identifier") or "")
        source_sha256 = str(track.get("source_sha256") or "")
        video_id = str((job.provider_metadata(self.name) or {}).get("probe", {}).get("video_id") or "")
        if not language or source not in {"manual", "automatic"} or not track_id or not video_id or not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Subtitle track identity is incomplete.")
        auth_metadata = self._authentication_state(
            (job.provider_metadata(self.name) or {}).get("authentication")
        )
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", track_id).strip("._") or "track"
        relative_dir = f"stage3/subtitles/source-{source_sha256[:16]}/{source}/{safe_id}"
        output_dir = _safe_temp_path(workspace, relative_dir)
        if output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Subtitle output directory is unsafe.")
        output_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(output_dir.iterdir(), key=lambda item: item.name)
        if existing:
            if len(existing) != 1 or existing[0].is_symlink() or not existing[0].is_file() or existing[0].suffix.lower().lstrip(".") not in {"vtt", "srt", "json3", "ttml"}:
                raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Subtitle output directory contains an unsafe or ambiguous partial artifact.")
            existing_path = existing[0]
            return {
                "file_path": existing_path.relative_to(workspace.paths.temp_dir).as_posix(),
                "format": existing_path.suffix.lower().lstrip("."),
                "size_bytes": existing_path.stat().st_size,
                "sha256": _sha256_file(existing_path),
                "extraction_status": "reused_existing_job_temp_file",
                "authentication": auth_metadata,
            }
        source_url = str((job.provider_metadata(self.name) or {}).get("probe", {}).get("canonical_url") or job.source_url)
        command = [
            *self.yt_dlp_command,
            "--ignore-config", "--no-cache-dir", "--no-warnings", "--no-progress",
            "--skip-download", "--write-subs" if source == "manual" else "--write-auto-subs",
            "--sub-langs", language, "--sub-format", "vtt/srt/json3/ttml/best",
            "--no-embed-subs", "--no-write-info-json", "--no-write-thumbnail",
            "--output", str(output_dir / f"{video_id}.%(ext)s"), source_url,
        ]
        process, authentication = self._run_ytdlp_with_auth_fallback(
            command,
            source_url=source_url,
            phase="subtitle extraction",
            authentication=auth_metadata,
        )
        if process.returncode != 0:
            kind, cause = _json_safe_process_error(process.returncode, process.stderr, process.stdout)
            raise ProviderFailure(kind, self.name, "yt-dlp could not extract the catalogued subtitle track.", details={
                "returncode": process.returncode,
                "cause": cause,
                "authentication": authentication,
            })
        produced = sorted(output_dir.iterdir(), key=lambda item: item.name)
        if len(produced) != 1 or produced[0].is_symlink() or not produced[0].is_file():
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Subtitle extraction did not produce exactly one safe file.")
        file_path = produced[0]
        actual_format = file_path.suffix.lower().lstrip(".")
        if actual_format not in {"vtt", "srt", "json3", "ttml"}:
            raise ProviderFailure(ProviderFailureKind.FAILED, self.name, "Subtitle extraction produced an unsupported file format.", details={"format": actual_format})
        relative_path = file_path.relative_to(workspace.paths.temp_dir).as_posix()
        return {
            "file_path": relative_path,
            "format": actual_format,
            "size_bytes": file_path.stat().st_size,
            "sha256": _sha256_file(file_path),
            "extraction_status": "downloaded",
            "authentication": self._authentication_state(authentication),
        }

    def _result(
        self,
        video: NormalizedVideoMetadata,
        selection: FormatSelection,
        artifact_info: Mapping[str, Any],
        operations: Mapping[str, Any],
        resume_token: str,
        *,
        resumed: bool,
        authentication: Mapping[str, Any],
    ) -> ProviderResult:
        metadata = {
            "probe": video.to_dict(),
            "selection": selection.to_dict(),
            "fetch": {
                "source_container": "mp4",
                "operations": dict(operations),
                "resumed": resumed,
                "completed_artifact_reused": resumed,
            },
            "authentication": self._authentication_state(authentication),
            "validated_artifact": dict(artifact_info),
        }
        return ProviderResult(
            metadata=metadata,
            artifacts=(ProducedArtifact("temp/source_video/source_video.mp4", "SOURCE_VIDEO", "video/mp4"),),
            resume_token=resume_token,
        )
