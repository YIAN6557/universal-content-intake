"""Stage 3 subtitle extraction, deterministic selection, and cue preparation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from src.core.job import Job
from src.media.stage3_workspace import Stage3Workspace, identity_hash
from src.media.subtitles import (
    Cue,
    PrimaryTrackDecision,
    SubtitleTrack,
    exclude_machine_translated_tracks,
    parse_subtitle_text,
    select_primary_track,
)
from src.providers.base import ProviderFailure, ProviderFailureKind


class SubtitleStageFailure(ValueError):
    def __init__(
        self,
        message: str,
        *,
        cause: str | None = None,
        provider_kind: ProviderFailureKind | None = None,
        authentication: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.cause = cause
        self.provider_kind = provider_kind
        self.authentication = dict(authentication or {})


def _track_from_mapping(value: Mapping[str, Any]) -> SubtitleTrack:
    return SubtitleTrack(
        language_code=str(value.get("language_code") or ""),
        language_name=value.get("language_name") if isinstance(value.get("language_name"), str) else None,
        source=str(value.get("source") or ""),
        provider=str(value.get("provider") or "yt-dlp"),
        format=str(value.get("format") or ""),
        track_identifier=str(value.get("track_identifier") or ""),
        available_formats=tuple(str(item) for item in value.get("available_formats", []) if isinstance(item, str)),
        file_path=value.get("file_path") if isinstance(value.get("file_path"), str) else None,
        extraction_status=str(value.get("extraction_status") or "pending"),
    )


def _cue_from_mapping(value: Mapping[str, Any]) -> Cue:
    return Cue(
        cue_id=str(value["cue_id"]),
        start=float(value["start"]),
        end=float(value["end"]),
        text=str(value["text"]),
        source_language=str(value["source_language"]),
        translated_text=value.get("translated_text") if isinstance(value.get("translated_text"), str) else None,
    )


def _read_cues(path: Path) -> tuple[Cue, ...]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, list) or not decoded:
        raise SubtitleStageFailure("The selected subtitle cue manifest is empty or invalid.")
    try:
        return tuple(_cue_from_mapping(item) for item in decoded if isinstance(item, Mapping))
    except (KeyError, TypeError, ValueError) as error:
        raise SubtitleStageFailure("The selected subtitle cue manifest is malformed.") from error


def discover_and_select_subtitles(
    job: Job,
    provider: Any,
    workspace: Stage3Workspace,
    source_sha256: str,
) -> dict[str, Any]:
    """Download the primary track first, then the other genuine tracks, and cache by source identity.

    Provider machine translations of automatic captions are excluded before
    selection; see ``exclude_machine_translated_tracks``.
    """
    provider_metadata = job.provider_metadata("yt-dlp") or {}
    probe = provider_metadata.get("probe")
    if not isinstance(probe, Mapping):
        raise SubtitleStageFailure("Stage 2 normalized video metadata is missing.")
    raw_tracks = probe.get("subtitle_tracks")
    if not isinstance(raw_tracks, list):
        raise SubtitleStageFailure("Stage 2 subtitle catalog is malformed.")
    tracks = tuple(_track_from_mapping(item) for item in raw_tracks if isinstance(item, Mapping))
    catalog_hash = identity_hash({"tracks": [track.to_dict() for track in tracks]})
    input_identity = {"source_sha256": source_sha256, "catalog_sha256": catalog_hash}
    cache = workspace.load_cache("subtitles", input_identity)
    if cache is not None:
        metadata = cache.get("metadata")
        if not isinstance(metadata, dict):
            raise SubtitleStageFailure("Subtitle resume metadata is malformed.")
        cue_path = metadata.get("primary_cues_path")
        cues = _read_cues(workspace.path(cue_path)) if isinstance(cue_path, str) else ()
        return {**metadata, "cues": cues, "resumed": True}

    if not tracks:
        metadata = {
            "status": "no_subtitles",
            "tracks": [],
            "primary_track": None,
            "selection_reason": "no_extractable_tracks_in_provider_catalog",
            "cues": (),
            "resumed": False,
        }
        workspace.write_cache("subtitles", input_identity, [], {key: value for key, value in metadata.items() if key != "cues"})
        return metadata

    original_language = probe.get("original_language")
    original_hint = str(original_language) if original_language else None
    candidate_tracks, excluded_tracks = exclude_machine_translated_tracks(tracks, original_hint)
    decision: PrimaryTrackDecision = select_primary_track(candidate_tracks, original_hint)
    ordered = [decision.track] + [
        track for track in candidate_tracks if track.track_identifier != decision.track.track_identifier
    ]

    downloaded: list[dict[str, Any]] = []
    authentication: list[dict[str, Any]] = []
    network_failed = False
    for index, track in enumerate(ordered):
        if network_failed:
            downloaded.append({**track.to_dict(), "file_path": None, "extraction_status": "skipped_after_network_failure"})
            continue
        try:
            facts = provider.download_subtitle_track(job, {**track.to_dict(), "source_sha256": source_sha256})
        except ProviderFailure as error:
            if index > 0:
                # Only the primary track is a confirmed dependency. A secondary
                # track failure is recorded and does not block the Job; after a
                # network failure (e.g. HTTP 429) stop sending more requests.
                downloaded.append({
                    **track.to_dict(),
                    "file_path": None,
                    "extraction_status": "failed_non_primary",
                    "cause": str(error.details.get("cause") or error)[:500],
                })
                network_failed = error.kind is ProviderFailureKind.NETWORK
                continue
            raw_auth = error.details.get("authentication")
            safe_auth = {
                key: raw_auth[key]
                for key in ("auth_attempted", "auth_source", "authenticated_retry", "auth_result")
                if isinstance(raw_auth, Mapping) and key in raw_auth
            }
            raise SubtitleStageFailure(
                "A confirmed subtitle track could not be extracted.",
                cause=str(error.details.get("cause") or error),
                provider_kind=error.kind,
                authentication=safe_auth,
            ) from error
        downloaded.append({**track.to_dict(), **facts})
        auth = facts.get("authentication")
        if isinstance(auth, Mapping):
            authentication.append({
                key: auth[key]
                for key in ("auth_attempted", "auth_source", "authenticated_retry", "auth_result")
                if key in auth
            })

    chosen = downloaded[0]
    relative_path = chosen.get("file_path")
    actual_format = chosen.get("format")
    if not isinstance(relative_path, str) or not isinstance(actual_format, str):
        raise SubtitleStageFailure("The selected subtitle track has no extracted file identity.")
    subtitle_path = workspace.path(relative_path)
    try:
        cues = parse_subtitle_text(subtitle_path.read_text(encoding="utf-8-sig"), actual_format, source_language=decision.track.language_code)
    except (OSError, UnicodeError, ValueError) as error:
        raise SubtitleStageFailure("The selected subtitle track could not be parsed.", cause=str(error)) from error

    cues_relative = f"stage3/subtitles/source-{source_sha256[:16]}/primary-cues.json"
    cues_path = workspace.path(cues_relative)
    cues_path.parent.mkdir(parents=True, exist_ok=True)
    cues_path.write_text(json.dumps([cue.to_dict() for cue in cues], ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    manifest_tracks = [{key: value for key, value in track.items() if key != "authentication"} for track in downloaded]
    metadata = {
        "status": "selected",
        "tracks": manifest_tracks,
        "primary_track": chosen["track_identifier"],
        "primary_language_code": decision.track.language_code,
        "primary_language_name": decision.track.language_name,
        "primary_source": decision.track.source,
        "primary_format": actual_format,
        "primary_track_identifier": decision.track.track_identifier,
        "primary_file_path": relative_path,
        "primary_cues_path": cues_relative,
        "selection_reason": decision.reason,
        "deterministic_tie_break": decision.tie_break,
        "authentication_outcomes": authentication,
        "cue_count": len(cues),
        "excluded_machine_translated_languages": [track.language_code for track in excluded_tracks],
    }
    outputs = [str(item["file_path"]) for item in downloaded if isinstance(item.get("file_path"), str)] + [cues_relative]
    workspace.write_cache("subtitles", input_identity, outputs, metadata)
    return {**metadata, "cues": cues, "resumed": False}
