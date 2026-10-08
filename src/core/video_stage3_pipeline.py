"""Core-owned Stage 3 subtitle/ASR/translation/render orchestration."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.core.errors import CoreError, ErrorCode
from src.core.job import Job, JobState
from src.core.state_machine import RECOVERABLE_ERROR_STATES, StateMachine
from src.core.video_pipeline import save_job
from src.media.asr import ASRLanguageUndeterminedError, ASRRuntimeError, run_silero_vad, transcribe_with_whisper_cpp
from src.media.ffmpeg_tools import MediaToolError, extract_asr_audio, probe_media, validate_rendered_video
from src.media.render import BurnInError, build_ass_document, burn_in_subtitles
from src.media.stage3_workspace import Stage3Workspace, Stage3WorkspaceError, identity_hash, sha256_file
from src.media.subtitle_pipeline import SubtitleStageFailure, discover_and_select_subtitles
from src.media.subtitles import Cue, is_zh_hans_language, normalize_language_code
from src.media.translation import TranslationProtocolError, apply_translation_response, make_translation_request
from src.media.translation_runtime import TranslationRuntimeError, translate_cues, translation_preflight
from src.providers.video import FFMPEG_PATH, FFPROBE_PATH, VideoProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WHISPER_ROOT = PROJECT_ROOT / "tools" / "whisper.cpp"
WHISPER_CLI = WHISPER_ROOT / "bin" / "whisper-cli"
VAD_CLI = WHISPER_ROOT / "bin" / "whisper-vad-speech-segments"
# Project-owned copy (identical SHA-256 to the model validated in Stage 3), so
# an update or removal of another app cannot break ASR.
SMALL_MODEL = Path(os.environ.get(
    "UCI_WHISPER_SMALL_MODEL",
    str(PROJECT_ROOT / "models" / "ggml-small.bin"),
)).expanduser()
VAD_MODEL = PROJECT_ROOT / "models" / "ggml-silero-v6.2.0.bin"
TRANSLATION_HELPER = PROJECT_ROOT / "apple-helper" / "Translation" / "bin" / "uci-translation"
FONT_DIRECTORY = PROJECT_ROOT / "fonts"
FONT_FILE = FONT_DIRECTORY / "NotoSansCJKsc-Medium.otf"


@dataclass(frozen=True)
class Stage3Outcome:
    job: Job
    job_file: Path
    status: str
    result: dict[str, Any] | None = None
    error: CoreError | None = None

    @property
    def success(self) -> bool:
        return self.status == "validated" and self.error is None


def _write_json(workspace: Stage3Workspace, relative: str, value: Mapping[str, Any] | Sequence[Any]) -> Path:
    path = workspace.path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    pending = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Stage 3 JSON artifact is unreadable") from error
    if not isinstance(value, dict):
        raise ValueError("Stage 3 JSON artifact must be an object")
    return value


def _cues_from_json(value: Any) -> tuple[Cue, ...]:
    if not isinstance(value, list):
        raise ValueError("Stage 3 cue artifact must be a list")
    cues: list[Cue] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("Stage 3 cue artifact contains a malformed entry")
        cues.append(Cue(
            str(raw["cue_id"]), float(raw["start"]), float(raw["end"]),
            str(raw["text"]), str(raw["source_language"]),
            raw.get("translated_text") if isinstance(raw.get("translated_text"), str) else None,
        ))
    if not cues:
        raise ValueError("Stage 3 cue artifact is empty")
    return tuple(cues)


def _job_relative_temp_path(temp_relative: str) -> str:
    return temp_relative if temp_relative.startswith("temp/") else f"temp/{temp_relative}"


def _advance(job: Job, state: JobState) -> None:
    if job.current_state is not state:
        StateMachine.transition(job, state)


def _failure(
    job: Job,
    job_file: Path,
    code: ErrorCode,
    *,
    cause: str | None,
    provider: str,
    status: str = "failed",
    extra: Mapping[str, Any] | None = None,
) -> Stage3Outcome:
    error = CoreError.for_code(code, provider=provider, cause=cause)
    StateMachine.apply_error(job, error)
    job.source_metadata["stage3"] = {
        "schema_version": 1,
        "status": status,
        "failure_code": code.value,
        **dict(extra or {}),
    }
    save_job(job, job_file)
    return Stage3Outcome(job, job_file, status, error=error)


def _stage2_source(job: Job, workspace: Stage3Workspace, *, ffprobe: Path) -> tuple[Path, str, dict[str, Any]]:
    provider_metadata = job.provider_metadata("yt-dlp") or {}
    artifact = provider_metadata.get("validated_artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("Stage 2 did not persist a validated SOURCE_VIDEO artifact")
    relative_path = artifact.get("path")
    expected_hash = artifact.get("sha256")
    expected_size = artifact.get("size_bytes")
    if not isinstance(relative_path, str) or not isinstance(expected_hash, str) or not isinstance(expected_size, int):
        raise ValueError("Stage 2 SOURCE_VIDEO identity is incomplete")
    if relative_path.startswith("temp/"):
        relative_path = relative_path.removeprefix("temp/")
    source = workspace.path(relative_path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size != expected_size or sha256_file(source) != expected_hash:
        raise ValueError("Stage 2 SOURCE_VIDEO identity no longer matches its validated artifact")
    facts = probe_media(source, ffprobe=ffprobe)
    videos = [item for item in facts["streams"] if isinstance(item, Mapping) and item.get("codec_type") == "video"]
    audios = [item for item in facts["streams"] if isinstance(item, Mapping) and item.get("codec_type") == "audio"]
    if not videos or not audios:
        raise ValueError("Stage 2 SOURCE_VIDEO does not have readable video and audio streams")
    return source, expected_hash, facts


def _cached_or_prepare_audio(
    source: Path,
    source_sha: str,
    workspace: Stage3Workspace,
    *,
    ffmpeg: Path,
    ffprobe: Path,
) -> tuple[Path, dict[str, Any]]:
    relative = f"stage3/audio/source-{source_sha[:16]}/mono-16khz-pcm-s16le.wav"
    identity = {"source_sha256": source_sha, "format": "mono-16000-pcm_s16le-wav", "ffmpeg": str(ffmpeg)}
    cache = workspace.load_cache("audio", identity)
    path = workspace.path(relative)
    if cache is not None:
        return path, dict(cache.get("metadata") or {})
    audio_facts = extract_asr_audio(source, path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    workspace.write_cache("audio", identity, [relative], audio_facts)
    return path, audio_facts


def _cached_or_vad(
    audio_path: Path,
    workspace: Stage3Workspace,
    *,
    audio_sha: str,
    vad_binary: Path,
    vad_model: Path,
) -> dict[str, Any]:
    model_hash = sha256_file(vad_model)
    relative = f"stage3/vad/{audio_sha[:16]}/result.json"
    identity = {
        "audio_sha256": audio_sha,
        "vad_model_sha256": model_hash,
        "threshold": 0.5,
        "minimum_speech_duration_ms": 250,
        "minimum_silence_duration_ms": 100,
        "engine": "whisper.cpp-vad-speech-segments",
    }
    cache = workspace.load_cache("vad", identity)
    if cache is not None:
        return _read_json(workspace.path(relative))
    result = run_silero_vad(audio_path, vad_binary=vad_binary, vad_model=vad_model)
    data = result.to_dict()
    _write_json(workspace, relative, data)
    workspace.write_cache("vad", identity, [relative], data)
    return data


def _cached_or_transcribe(
    audio_path: Path,
    workspace: Stage3Workspace,
    *,
    audio_sha: str,
    language_hint: str | None,
    whisper_binary: Path,
    model_path: Path,
) -> dict[str, Any]:
    model_hash = sha256_file(model_path)
    relative = f"stage3/asr/source-{audio_sha[:16]}/transcript.json"
    raw_relative = f"stage3/asr/source-{audio_sha[:16]}/whisper-output.json"
    hint = language_hint.split("-", 1)[0] if language_hint else None
    identity = {
        "audio_sha256": audio_sha,
        "model_sha256": model_hash,
        "language_hint": hint or "auto",
        "engine": "whisper.cpp",
    }
    cache = workspace.load_cache("asr", identity)
    if cache is not None:
        return _read_json(workspace.path(relative))
    output_base = workspace.path(raw_relative).with_suffix("")
    output_base.parent.mkdir(parents=True, exist_ok=True)
    result = transcribe_with_whisper_cpp(
        audio_path,
        whisper_binary=whisper_binary,
        model_path=model_path,
        output_base=output_base,
        source_language_hint=hint,
    )
    raw_path = Path(str(result.pop("raw_json_path")))
    if raw_path != workspace.path(raw_relative):
        raise ASRRuntimeError("whisper.cpp output escaped the Job temp directory")
    _write_json(workspace, relative, result)
    workspace.write_cache("asr", identity, [relative, raw_relative], {
        "detected_language": result["detected_language"],
        "language_confidence": result["language_confidence"],
        "cue_count": len(result["cues"]),
        "model_sha256": model_hash,
    })
    return result


def _translate(
    cues: Sequence[Cue],
    source_language: str,
    source_sha: str,
    workspace: Stage3Workspace,
    *,
    helper: Path,
    preflight: Mapping[str, Any] | None = None,
) -> tuple[tuple[Cue, ...] | None, dict[str, Any]]:
    source = normalize_language_code(source_language)
    if source is None:
        raise ValueError("translation source language is not a normalized language code")
    source_cues_sha = identity_hash({"cues": [cue.to_dict() for cue in cues]})
    helper_sha = sha256_file(helper)
    identity = {
        "source_video_sha256": source_sha,
        "source_cues_sha256": source_cues_sha,
        "helper_sha256": helper_sha,
        "source_language": source,
        "target_language": "zh-Hans",
    }
    cues_relative = f"stage3/translation/source-{source_sha[:16]}/translated-cues.json"
    cache = workspace.load_cache("translation", identity)
    if cache is not None:
        metadata = dict(cache.get("metadata") or {})
        return _cues_from_json(_read_json(workspace.path(cues_relative))["cues"]), {**metadata, "resumed": True}

    preflight = dict(preflight) if preflight is not None else translation_preflight(helper, source_language=source)
    if preflight.get("status") in {"unsupported", "resource_unavailable"}:
        error = preflight.get("error")
        return None, {
            "status": preflight.get("status"),
            "source_language": source,
            "target_language": "zh-Hans",
            "preflight": preflight,
            "error_code": error.get("code") if isinstance(error, Mapping) else None,
            "error_message": error.get("message") if isinstance(error, Mapping) else None,
        }
    if preflight.get("status") != "ready":
        raise TranslationRuntimeError("Apple Translation preflight returned an unknown status")

    request = make_translation_request(cues, source_language=source, target_language="zh-Hans")
    request_relative = f"stage3/translation/source-{source_sha[:16]}/request.json"
    response_relative = f"stage3/translation/source-{source_sha[:16]}/response.json"
    request_path = _write_json(workspace, request_relative, request)
    response_path = workspace.path(response_relative)
    response = translate_cues(
        helper,
        request_path=request_path,
        response_path=response_path,
        source_language=source,
        target_language="zh-Hans",
    )
    status = response.get("status")
    if status in {"unsupported", "resource_unavailable"}:
        error = response.get("error")
        error_code = error.get("code") if isinstance(error, Mapping) else None
        return None, {
            "status": status,
            "source_language": source,
            "target_language": "zh-Hans",
            "preflight": dict(preflight),
            "error_code": error_code,
            "error_message": error.get("message") if isinstance(error, Mapping) else None,
        }
    if status != "success":
        raise TranslationRuntimeError("Apple Translation returned a non-success cue result")
    translated = apply_translation_response(cues, response)
    _write_json(workspace, cues_relative, {"cues": [cue.to_dict() for cue in translated]})
    metadata = {
        "status": "translated",
        "source_language": source,
        "target_language": "zh-Hans",
        "translation_engine": "Apple Translation / lowLatency",
        "language_pair_supported": True,
        "language_resources_installed": True,
        "preflight": dict(preflight),
        "cue_count": len(translated),
        "resumed": False,
    }
    workspace.write_cache("translation", identity, [request_relative, response_relative, cues_relative], metadata)
    return translated, metadata


def _cached_or_render(
    source: Path,
    source_sha: str,
    source_facts: Mapping[str, Any],
    original_cues: Sequence[Cue] | None,
    chinese_cues: Sequence[Cue],
    workspace: Stage3Workspace,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    fonts_dir: Path,
    font_file: Path,
) -> dict[str, Any]:
    video = next((item for item in source_facts["streams"] if item.get("codec_type") == "video"), None)
    if not isinstance(video, Mapping):
        raise BurnInError("SOURCE_VIDEO is missing frame dimensions")
    subtitle_identity = identity_hash({
        "original": [cue.to_dict() for cue in original_cues] if original_cues is not None else None,
        "chinese": [cue.to_dict() for cue in chinese_cues],
    })
    identity = {
        "source_video_sha256": source_sha,
        "subtitle_identity_sha256": subtitle_identity,
        "font_sha256": sha256_file(font_file),
        "layout_contract": "short-edge-4.5%-one-two-reduced-three-lines;safe-area-v1",
        "renderer": "ffmpeg-libass-libx264-crf18-audio-copy",
    }
    relative = f"stage3/render/source-{source_sha[:16]}/validated-subtitled-video.mp4"
    ass_relative = f"stage3/render/source-{source_sha[:16]}/captions.ass"
    layout_relative = f"stage3/render/source-{source_sha[:16]}/layout.json"
    cached = workspace.load_cache("render", identity)
    if cached is not None:
        output = workspace.path(relative)
        rendered_facts = probe_media(output, ffprobe=ffprobe)
        verification = validate_rendered_video(
            source_facts, rendered_facts,
            expected_width=int(video["width"]), expected_height=int(video["height"]),
        )
        metadata = dict(cached.get("metadata") or {})
        return {
            **metadata,
            "path": relative,
            "size_bytes": output.stat().st_size,
            "sha256": sha256_file(output),
            "ffprobe": verification,
            "resumed": True,
        }
    ass, layout = build_ass_document(
        original_cues,
        chinese_cues,
        frame_width=int(video["width"]),
        frame_height=int(video["height"]),
    )
    workspace.path(ass_relative).parent.mkdir(parents=True, exist_ok=True)
    workspace.path(ass_relative).write_text(ass, encoding="utf-8")
    _write_json(workspace, layout_relative, layout)
    output = workspace.path(relative)
    facts = burn_in_subtitles(source, workspace.path(ass_relative), output, ffmpeg=ffmpeg, ffprobe=ffprobe, fonts_dir=fonts_dir)
    metadata = {
        **facts,
        "layout_path": layout_relative,
        "ass_path": ass_relative,
        "path": relative,
        "size_bytes": output.stat().st_size,
        "sha256": sha256_file(output),
        "resumed": False,
        "transcode": {"video": True, "audio": False, "reason": "hard subtitle rendering requires video re-encode", "audio_mode": "stream-copy"},
    }
    workspace.write_cache("render", identity, [ass_relative, layout_relative, relative], metadata)
    return metadata


def run_video_stage3_pipeline(
    job: Job,
    *,
    job_file: Path,
    video_provider: VideoProvider | None = None,
    ffmpeg: Path = FFMPEG_PATH,
    ffprobe: Path = FFPROBE_PATH,
    whisper_binary: Path = WHISPER_CLI,
    vad_binary: Path = VAD_CLI,
    whisper_model: Path = SMALL_MODEL,
    vad_model: Path = VAD_MODEL,
    translation_helper: Path = TRANSLATION_HELPER,
    fonts_dir: Path = FONT_DIRECTORY,
    font_file: Path = FONT_FILE,
) -> Stage3Outcome:
    """Resume the existing Stage 2 artifact through Stage 3; never starts acquisition."""
    provider = video_provider or VideoProvider()
    try:
        workspace = Stage3Workspace(job)
        source, source_sha, source_facts = _stage2_source(job, workspace, ffprobe=ffprobe)
    except (Stage3WorkspaceError, MediaToolError, OSError, ValueError) as error:
        if job.current_state in {JobState.DOWNLOADING, JobState.TRANSCRIBING, JobState.TRANSLATING, JobState.RENDERING}:
            return _failure(job, job_file, ErrorCode.PROVIDER_FAILED, cause=str(error), provider="Stage 3 SOURCE_VIDEO validation")
        return Stage3Outcome(job, job_file, "blocked", error=job.error)

    if job.current_state in RECOVERABLE_ERROR_STATES:
        StateMachine.resume(job)
        save_job(job, job_file)
    if job.error is not None:
        return Stage3Outcome(job, job_file, "failed", error=job.error)
    if job.current_state not in {JobState.DOWNLOADING, JobState.TRANSCRIBING, JobState.TRANSLATING, JobState.RENDERING}:
        return Stage3Outcome(job, job_file, "blocked", error=job.error)
    if job.current_state is JobState.DOWNLOADING:
        _advance(job, JobState.TRANSCRIBING)
        save_job(job, job_file)

    stage_record: dict[str, Any] = {
        "schema_version": 1,
        "source_video_sha256": source_sha,
        "source_video_path": "temp/source_video/source_video.mp4",
        "status": "processing",
    }
    save_job(job, job_file)
    try:
        subtitles = discover_and_select_subtitles(job, provider, workspace, source_sha)
    except SubtitleStageFailure as error:
        code = ErrorCode.NETWORK_PAUSED if error.provider_kind is not None and error.provider_kind.value == "NETWORK" else ErrorCode.SUBTITLE_FAILED
        return _failure(
            job, job_file, code, cause=error.cause or str(error), provider="yt-dlp subtitles",
            extra={"subtitle_extraction": {"status": "failed", "authentication": error.authentication}},
        )
    except ProviderFailure as error:
        code = ErrorCode.NETWORK_PAUSED if error.kind.value == "NETWORK" else ErrorCode.SUBTITLE_FAILED
        return _failure(job, job_file, code, cause=str(error), provider="yt-dlp subtitles")
    except (Stage3WorkspaceError, OSError, ValueError) as error:
        return _failure(job, job_file, ErrorCode.SUBTITLE_FAILED, cause=str(error), provider="Subtitle discovery")

    stage_record["subtitle_discovery"] = {key: value for key, value in subtitles.items() if key != "cues"}
    original_cues: tuple[Cue, ...] | None = None
    chinese_cues: tuple[Cue, ...] | None = None
    source_language: str | None = None
    speech_status = "not_required"
    asr_facts: dict[str, Any] | None = None

    if subtitles.get("status") == "selected":
        original_cues = tuple(subtitles["cues"])
        source_language = normalize_language_code(str(subtitles.get("primary_language_code") or ""))
        if source_language is None:
            return _failure(job, job_file, ErrorCode.SUBTITLE_FAILED, cause="Primary subtitle language is unknown.", provider="Subtitle selection")
        if is_zh_hans_language(source_language):
            chinese_cues = tuple(Cue(cue.cue_id, cue.start, cue.end, cue.text, "zh-Hans", cue.text) for cue in original_cues)
            stage_record["translation"] = {"status": "skipped_source_is_zh_hans", "source_language": source_language, "target_language": "zh-Hans"}
            original_cues = None
        else:
            if job.current_state is JobState.TRANSCRIBING:
                _advance(job, JobState.TRANSLATING)
                save_job(job, job_file)
            try:
                chinese_cues, translation = _translate(
                    original_cues, source_language, source_sha, workspace,
                    helper=translation_helper,
                )
            except (TranslationRuntimeError, TranslationProtocolError, OSError, ValueError) as error:
                return _failure(job, job_file, ErrorCode.PROVIDER_FAILED, cause=str(error), provider="Apple Translation")
            if chinese_cues is None:
                code = translation.get("error_code")
                if code == ErrorCode.TRANSLATION_UNSUPPORTED.value:
                    return _failure(job, job_file, ErrorCode.TRANSLATION_UNSUPPORTED, cause=str(translation.get("error_message") or "Unsupported language pair."), provider="Apple Translation")
                if translation.get("status") != "resource_unavailable":
                    return _failure(job, job_file, ErrorCode.PROVIDER_FAILED, cause="Apple Translation did not return translated cues.", provider="Apple Translation")
                stage_record["status"] = "paused_translation_resource_unavailable"
                stage_record["translation"] = translation
                job.source_metadata["stage3"] = stage_record
                save_job(job, job_file)
                return Stage3Outcome(job, job_file, "paused", result=stage_record)
            stage_record["translation"] = translation

    else:
        try:
            audio_path, audio_facts = _cached_or_prepare_audio(source, source_sha, workspace, ffmpeg=ffmpeg, ffprobe=ffprobe)
            audio_sha = sha256_file(audio_path)
            vad_facts = _cached_or_vad(audio_path, workspace, audio_sha=audio_sha, vad_binary=vad_binary, vad_model=vad_model)
        except (ASRRuntimeError, MediaToolError, OSError, ValueError) as error:
            return _failure(job, job_file, ErrorCode.ASR_FAILED, cause=str(error), provider="Audio preparation / Silero VAD")
        stage_record["audio_preparation"] = audio_facts
        stage_record["vad"] = vad_facts
        speech_status = "speech_detected" if vad_facts.get("speech_detected") is True else "no_speech"
        if speech_status == "no_speech":
            _advance(job, JobState.RENDERING)
            no_render_identity = {"source_video_sha256": source_sha, "vad_result": "no_speech", "subtitle_status": "no_subtitles"}
            cache = workspace.load_cache("render", no_render_identity)
            if cache is None:
                workspace.write_cache("render", no_render_identity, [], {"status": "no_caption_passthrough", "burn_in_required": False})
            stage_record.update({
                "status": "validated_no_speech_no_subtitles",
                "speech_status": "no_speech",
                "render": {"status": "not_required", "reason": "VAD found no valid speech; no empty subtitle was created"},
                "validated_artifact": {
                    "path": "temp/source_video/source_video.mp4",
                    "sha256": source_sha,
                    "size_bytes": source.stat().st_size,
                    "role": "SOURCE_VIDEO_PASSTHROUGH",
                },
            })
            job.source_metadata["stage3"] = stage_record
            save_job(job, job_file)
            return Stage3Outcome(job, job_file, "validated", result=stage_record)
        original_language = (job.provider_metadata("yt-dlp") or {}).get("probe", {}).get("original_language")
        normalized_hint = normalize_language_code(str(original_language)) if original_language else None
        try:
            asr_facts = _cached_or_transcribe(
                audio_path, workspace, audio_sha=audio_sha, language_hint=normalized_hint,
                whisper_binary=whisper_binary, model_path=whisper_model,
            )
            source_language = normalize_language_code(str(asr_facts.get("detected_language") or ""))
            if source_language in {None, "und"}:
                raise ASRLanguageUndeterminedError("whisper.cpp could not reliably determine the transcript language")
            original_cues = _cues_from_json(asr_facts.get("cues"))
        except ASRLanguageUndeterminedError as error:
            stage_record.update({"status": "paused_asr_language_undetermined", "speech_status": speech_status, "asr": {"status": "language_undetermined", "cause": str(error)}})
            job.source_metadata["stage3"] = stage_record
            save_job(job, job_file)
            return Stage3Outcome(job, job_file, "paused", result=stage_record)
        except (ASRRuntimeError, OSError, ValueError) as error:
            return _failure(job, job_file, ErrorCode.ASR_FAILED, cause=str(error), provider="whisper.cpp")
        stage_record["asr"] = {key: value for key, value in asr_facts.items() if key != "cues"}
        if is_zh_hans_language(source_language):
            chinese_cues = tuple(Cue(cue.cue_id, cue.start, cue.end, cue.text, "zh-Hans", cue.text) for cue in original_cues)
            original_cues = None
            stage_record["translation"] = {"status": "skipped_source_is_zh_hans", "source_language": source_language, "target_language": "zh-Hans"}
        else:
            if job.current_state is JobState.TRANSCRIBING:
                _advance(job, JobState.TRANSLATING)
                save_job(job, job_file)
            try:
                chinese_cues, translation = _translate(
                    original_cues, source_language, source_sha, workspace,
                    helper=translation_helper,
                )
            except (TranslationRuntimeError, TranslationProtocolError, OSError, ValueError) as error:
                return _failure(job, job_file, ErrorCode.PROVIDER_FAILED, cause=str(error), provider="Apple Translation")
            if chinese_cues is None:
                if translation.get("error_code") == ErrorCode.TRANSLATION_UNSUPPORTED.value:
                    return _failure(job, job_file, ErrorCode.TRANSLATION_UNSUPPORTED, cause=str(translation.get("error_message") or "Unsupported language pair."), provider="Apple Translation")
                if translation.get("status") != "resource_unavailable":
                    return _failure(job, job_file, ErrorCode.PROVIDER_FAILED, cause="Apple Translation did not return translated cues.", provider="Apple Translation")
                stage_record.update({"status": "paused_translation_resource_unavailable", "speech_status": speech_status, "translation": translation})
                job.source_metadata["stage3"] = stage_record
                save_job(job, job_file)
                return Stage3Outcome(job, job_file, "paused", result=stage_record)
            stage_record["translation"] = translation

    if not chinese_cues:
        return _failure(job, job_file, ErrorCode.SUBTITLE_FAILED, cause="Subtitle preparation returned no Chinese cues.", provider="Stage 3")
    if job.current_state in {JobState.TRANSCRIBING, JobState.TRANSLATING}:
        _advance(job, JobState.RENDERING)
        save_job(job, job_file)
    try:
        render_facts = _cached_or_render(
            source, source_sha, source_facts, original_cues, chinese_cues, workspace,
            ffmpeg=ffmpeg, ffprobe=ffprobe, fonts_dir=fonts_dir, font_file=font_file,
        )
    except (BurnInError, MediaToolError, OSError, ValueError, Stage3WorkspaceError) as error:
        return _failure(job, job_file, ErrorCode.SUBTITLE_FAILED, cause=str(error), provider="ffmpeg/libass render")
    stage_record.update({
        "status": "validated",
        "speech_status": speech_status,
        "render": render_facts,
        "validated_artifact": {
            # Stage 5 requires Job-relative paths ("temp/..."), the same form
            # the no-speech passthrough uses; the render cache stays temp-relative.
            "path": _job_relative_temp_path(str(render_facts["path"])),
            "sha256": render_facts["sha256"],
            "size_bytes": render_facts["size_bytes"],
            "role": "STAGE3_VALIDATED_RENDERED_ARTIFACT",
        },
    })
    job.source_metadata["stage3"] = stage_record
    save_job(job, job_file)
    return Stage3Outcome(job, job_file, "validated", result=stage_record)
