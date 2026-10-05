"""Local invocation boundary for the Universal Content Intake Core."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from .core.errors import map_provider_failure
from .core.job import ContentType, Job
from .core.article_pipeline import run_article_pipeline
from .core.document_pipeline import run_document_pipeline
from .core.image_pipeline import run_image_pipeline
from .core.webpage_pipeline import run_webpage_pipeline
from .core.output_pipeline import run_output_pipeline
from .core.policies import load_default_policies
from .core.router import resolve_content_type
from .core.video_pipeline import run_video_pipeline, save_job
from .core.video_stage3_pipeline import run_video_stage3_pipeline
from .media.translation_runtime import setup_translation, translation_preflight
from .output.delivery import deliver_completed_videos
from .output.paths import OutputContractError, OutputWorkspace
from .providers.base import ProviderFailure
from .providers.video import VideoProvider
from .providers.image import ImageProvider
from .providers.article import ArticleProvider
from .providers.webpage import WebpageProvider
from .providers.document import DEFAULT_LARGE_FILE_THRESHOLD_BYTES, DocumentProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"


def _content_type(value: str) -> ContentType:
    try:
        content_type = ContentType(value.upper())
    except ValueError as error:
        choices = ", ".join(item.value for item in ContentType if item is not ContentType.UNKNOWN)
        raise argparse.ArgumentTypeError(f"content type must be one of: {choices}") from error
    if content_type is ContentType.UNKNOWN:
        raise argparse.ArgumentTypeError("UNKNOWN is a resolution result, not a declared type")
    return content_type


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uci", description="Universal Content Intake V1 — Stages 1–4 VIDEO / IMAGE / ARTICLE / WEBPAGE / DOCUMENT")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_config = subparsers.add_parser("inspect-config", help="print effective default policies")
    inspect_config.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    create_job = subparsers.add_parser("create-job", help="create an offline Job contract; never fetch content")
    create_job.add_argument("--url", required=True)
    create_job.add_argument("--type", type=_content_type, dest="declared_type")
    create_job.add_argument("--workspace-root", type=Path)
    create_job.add_argument("--job-id")

    show_job = subparsers.add_parser("show-job", help="print a persisted Job contract")
    show_job.add_argument("job_file", type=Path)

    video_run = subparsers.add_parser("video-run", help="anonymously probe and acquire one VIDEO source")
    source = video_run.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="single public video URL; no login or cookies are used")
    source.add_argument("--resume-job", type=Path, help="resume a saved VIDEO Job contract")
    video_run.add_argument("--workspace-root", type=Path)
    video_run.add_argument("--job-id")
    video_run.add_argument("--quality", choices=("720p", "1080p", "1440p", "2160p", "2k", "4k", "highest"))

    image_run = subparsers.add_parser("image-run", help="probe and acquire one IMAGE or IMAGE_SET source")
    image_source = image_run.add_mutually_exclusive_group(required=True)
    image_source.add_argument("--url", help="one public image or gallery URL")
    image_source.add_argument("--resume-job", type=Path, help="resume a saved IMAGE or IMAGE_SET Job contract")
    image_run.add_argument("--type", choices=(ContentType.IMAGE.value, ContentType.IMAGE_SET.value), dest="declared_type")
    image_run.add_argument("--workspace-root", type=Path)
    image_run.add_argument("--job-id")

    article_run = subparsers.add_parser("article-run", help="fetch and extract one public ARTICLE source")
    article_source = article_run.add_mutually_exclusive_group(required=True)
    article_source.add_argument("--url", help="one public HTTP(S) article URL; no browser session is used")
    article_source.add_argument("--resume-job", type=Path, help="resume a saved ARTICLE Job contract")
    article_run.add_argument("--workspace-root", type=Path)
    article_run.add_argument("--job-id")

    webpage_run = subparsers.add_parser("webpage-run", help="save one public webpage as offline HTML, Markdown, and PDF")
    webpage_source = webpage_run.add_mutually_exclusive_group(required=True)
    webpage_source.add_argument("--url", help="one public HTTP(S) webpage URL; no browser session is used")
    webpage_source.add_argument("--resume-job", type=Path, help="resume a saved WEBPAGE Job contract")
    webpage_run.add_argument("--workspace-root", type=Path)
    webpage_run.add_argument("--job-id")

    document_run = subparsers.add_parser("document-run", help="acquire one direct or cloud DOCUMENT source")
    document_source = document_run.add_mutually_exclusive_group(required=True)
    document_source.add_argument("--url", help="HTTP(S), Google Drive, or configured rclone URL; anonymous where applicable")
    document_source.add_argument("--resume-job", type=Path, help="resume a saved DOCUMENT Job contract")
    document_run.add_argument("--workspace-root", type=Path)
    document_run.add_argument("--job-id")
    document_run.add_argument("--large-file-threshold-bytes", type=int, default=DEFAULT_LARGE_FILE_THRESHOLD_BYTES)

    stage3_run = subparsers.add_parser("stage3-run", help="resume a validated Stage 2 VIDEO Job through Stage 3")
    stage3_run.add_argument("--resume-job", type=Path, required=True)

    finalize_job = subparsers.add_parser("finalize-job", help="promote validated artifacts, write info.md, and clean Job temp")
    finalize_job.add_argument("--resume-job", type=Path, required=True, help="persisted Job contract to finalize or resume cleanup")

    subparsers.add_parser("deliver-videos", help="move finished videos and their info.md of COMPLETED Jobs to output.delivery_root")

    translation_check = subparsers.add_parser("translation-preflight", help="inspect Apple Translation support without downloading language resources")
    translation_check.add_argument("--source", required=True)
    translation_check.add_argument("--target", default="zh-Hans", choices=("zh-Hans",))

    translation_prepare = subparsers.add_parser("translation-setup", help="attended Setup: prepare Apple Translation language resources")
    translation_prepare.add_argument("--source", required=True)
    translation_prepare.add_argument("--target", default="zh-Hans", choices=("zh-Hans",))
    return parser


def _policy_payload(config_path: Path) -> dict[str, object]:
    policy = load_default_policies(config_path)
    return {
        "schema_version": policy.schema_version,
        "target_translation_language": policy.target_translation_language,
        "video_target_resolution": policy.video_target_resolution,
        "video_container": policy.video_container,
        "video_preferred_video_codec": policy.video_preferred_video_codec,
        "video_preferred_audio_codec": policy.video_preferred_audio_codec,
        "video_download_retries": policy.video_download_retries,
        "video_fragment_retries": policy.video_fragment_retries,
        "video_extractor_retries": policy.video_extractor_retries,
        "audio_output": policy.audio_output,
        "image_quality": policy.image_quality,
        "preserve_source_aspect_ratio": policy.preserve_source_aspect_ratio,
        "output_root": str(policy.output_root),
        "delivery_root": str(policy.delivery_root) if policy.delivery_root else None,
        "file_conflict": policy.file_conflict,
        "archive_by_default": policy.archive_by_default,
        "unnecessary_user_interruption": policy.unnecessary_user_interruption,
    }


def _create_job(args: argparse.Namespace) -> dict[str, object]:
    policy = load_default_policies(DEFAULT_CONFIG_PATH)
    workspace_root = args.workspace_root or policy.output_root
    job_id = args.job_id or str(uuid.uuid4())
    workspace = OutputWorkspace.for_job(workspace_root, job_id)
    paths = workspace.prepare()
    resolution = resolve_content_type(declared=args.declared_type)
    job = Job(
        job_id=job_id,
        source_url=args.url,
        declared_content_type=args.declared_type,
        resolved_content_type=resolution.content_type,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"resolution_origin": resolution.origin.value, "declared_type_locked": resolution.declared_locked},
    )
    job_file = paths.job_dir / "job.json"
    job_file.write_text(job.to_json(), encoding="utf-8")
    return {"job_file": str(job_file), "job": job.to_dict()}


def _new_video_job(args: argparse.Namespace) -> tuple[Job, Path]:
    policy = load_default_policies(DEFAULT_CONFIG_PATH)
    workspace = OutputWorkspace.for_job(args.workspace_root or policy.output_root, args.job_id or str(uuid.uuid4()))
    paths = workspace.prepare()
    job = Job(
        job_id=paths.job_dir.name.removeprefix("job-"),
        source_url=args.url,
        declared_content_type=ContentType.VIDEO,
        resolved_content_type=ContentType.VIDEO,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"resolution_origin": "DECLARED", "declared_type_locked": True},
        requested_options={"video": {"quality": args.quality}} if args.quality else {},
    )
    job_file = paths.job_dir / "job.json"
    save_job(job, job_file)
    return job, job_file


def _video_run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.resume_job:
        job_file = args.resume_job.expanduser()
        if job_file.is_symlink():
            raise ValueError("Job contract file cannot be a symlink")
        job = Job.from_json(job_file.read_text(encoding="utf-8"))
    else:
        job, job_file = _new_video_job(args)
    outcome = run_video_pipeline(job, VideoProvider(), job_file=job_file)
    if outcome.error is not None:
        return ({"job_file": str(outcome.job_file), "job": outcome.job.to_dict(), "error": outcome.error.to_dict()}, 2)
    return ({
        "job_file": str(outcome.job_file),
        "job": outcome.job.to_dict(),
        "artifacts": [asdict(artifact) for artifact in (outcome.result.artifacts if outcome.result else ())],
        "provider_result": outcome.result.metadata if outcome.result else None,
    }, 0 if outcome.success else 2)


def _new_image_job(args: argparse.Namespace) -> tuple[Job, Path]:
    policy = load_default_policies(DEFAULT_CONFIG_PATH)
    workspace = OutputWorkspace.for_job(args.workspace_root or policy.output_root, args.job_id or str(uuid.uuid4()))
    paths = workspace.prepare()
    declared = ContentType(args.declared_type) if args.declared_type else None
    job = Job(
        job_id=paths.job_dir.name.removeprefix("job-"),
        source_url=args.url,
        declared_content_type=declared,
        resolved_content_type=declared or ContentType.UNKNOWN,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"resolution_origin": "DECLARED" if declared else "PROVIDER_PROBE", "declared_type_locked": declared is not None},
        requested_options={"image": {"quality": "highest_available", "preserve_original": True}},
    )
    job_file = paths.job_dir / "job.json"
    save_job(job, job_file)
    return job, job_file


def _image_run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.resume_job:
        job_file = args.resume_job.expanduser()
        if job_file.is_symlink():
            raise ValueError("Job contract file cannot be a symlink")
        job = Job.from_json(job_file.read_text(encoding="utf-8"))
        if job.declared_content_type not in {None, ContentType.IMAGE, ContentType.IMAGE_SET} or job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.IMAGE, ContentType.IMAGE_SET}:
            raise ValueError("image-run can resume only IMAGE or IMAGE_SET Jobs")
    else:
        job, job_file = _new_image_job(args)
    outcome = run_image_pipeline(job, ImageProvider(), job_file=job_file)
    payload = {
        "job_file": str(outcome.job_file),
        "job": outcome.job.to_dict(),
        "artifacts": [asdict(artifact) for artifact in (outcome.result.artifacts if outcome.result else ())],
        "provider_result": outcome.result.metadata if outcome.result else None,
        "error": outcome.error.to_dict() if outcome.error else None,
    }
    return payload, 0 if outcome.success else 2


def _new_article_job(args: argparse.Namespace) -> tuple[Job, Path]:
    policy = load_default_policies(DEFAULT_CONFIG_PATH)
    workspace = OutputWorkspace.for_job(args.workspace_root or policy.output_root, args.job_id or str(uuid.uuid4()))
    paths = workspace.prepare()
    job = Job(
        job_id=paths.job_dir.name.removeprefix("job-"),
        source_url=args.url,
        declared_content_type=ContentType.ARTICLE,
        resolved_content_type=ContentType.ARTICLE,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"resolution_origin": "DECLARED", "declared_type_locked": True},
        requested_options={"article": {"extraction_engine": "trafilatura"}},
    )
    job_file = paths.job_dir / "job.json"
    save_job(job, job_file)
    return job, job_file


def _article_run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.resume_job:
        job_file = args.resume_job.expanduser()
        if job_file.is_symlink():
            raise ValueError("Job contract file cannot be a symlink")
        job = Job.from_json(job_file.read_text(encoding="utf-8"))
        if job.declared_content_type not in {None, ContentType.ARTICLE} or job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.ARTICLE}:
            raise ValueError("article-run can resume only ARTICLE Jobs")
    else:
        job, job_file = _new_article_job(args)
    outcome = run_article_pipeline(job, ArticleProvider(), job_file=job_file)
    payload = {
        "job_file": str(outcome.job_file),
        "job": outcome.job.to_dict(),
        "artifacts": [asdict(artifact) for artifact in (outcome.result.artifacts if outcome.result else ())],
        "provider_result": outcome.result.metadata if outcome.result else None,
        "error": outcome.error.to_dict() if outcome.error else None,
    }
    return payload, 0 if outcome.success else 2


def _new_webpage_job(args: argparse.Namespace) -> tuple[Job, Path]:
    policy = load_default_policies(DEFAULT_CONFIG_PATH)
    workspace = OutputWorkspace.for_job(args.workspace_root or policy.output_root, args.job_id or str(uuid.uuid4()))
    paths = workspace.prepare()
    job = Job(
        job_id=paths.job_dir.name.removeprefix("job-"),
        source_url=args.url,
        declared_content_type=ContentType.WEBPAGE,
        resolved_content_type=ContentType.WEBPAGE,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"resolution_origin": "DECLARED", "declared_type_locked": True},
        requested_options={"webpage": {"html_engine": "single-file-cli", "markdown_engine": "trafilatura", "pdf_engine": "google-chrome-headless"}},
    )
    job_file = paths.job_dir / "job.json"
    save_job(job, job_file)
    return job, job_file


def _webpage_run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.resume_job:
        job_file = args.resume_job.expanduser()
        if job_file.is_symlink():
            raise ValueError("Job contract file cannot be a symlink")
        job = Job.from_json(job_file.read_text(encoding="utf-8"))
        if job.declared_content_type not in {None, ContentType.WEBPAGE} or job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.WEBPAGE}:
            raise ValueError("webpage-run can resume only WEBPAGE Jobs")
    else:
        job, job_file = _new_webpage_job(args)
    outcome = run_webpage_pipeline(job, WebpageProvider(), job_file=job_file)
    payload = {
        "job_file": str(outcome.job_file),
        "job": outcome.job.to_dict(),
        "artifacts": [asdict(artifact) for artifact in (outcome.result.artifacts if outcome.result else ())],
        "provider_result": outcome.result.metadata if outcome.result else None,
        "error": outcome.error.to_dict() if outcome.error else None,
    }
    return payload, 0 if outcome.success else 2


def _new_document_job(args: argparse.Namespace) -> tuple[Job, Path]:
    policy = load_default_policies(DEFAULT_CONFIG_PATH)
    workspace = OutputWorkspace.for_job(args.workspace_root or policy.output_root, args.job_id or str(uuid.uuid4()))
    paths = workspace.prepare()
    job = Job(
        job_id=paths.job_dir.name.removeprefix("job-"),
        source_url=args.url,
        declared_content_type=ContentType.DOCUMENT,
        resolved_content_type=ContentType.DOCUMENT,
        workspace_path=str(paths.job_dir),
        temp_path=str(paths.temp_dir),
        output_path=str(paths.output_dir),
        source_metadata={"resolution_origin": "DECLARED", "declared_type_locked": True},
        requested_options={"document": {"large_file_threshold_bytes": args.large_file_threshold_bytes, "folder_max_depth": 3, "folder_max_files": 100}},
    )
    job_file = paths.job_dir / "job.json"
    save_job(job, job_file)
    return job, job_file


def _document_run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if args.resume_job:
        job_file = args.resume_job.expanduser()
        if job_file.is_symlink():
            raise ValueError("Job contract file cannot be a symlink")
        job = Job.from_json(job_file.read_text(encoding="utf-8"))
        if job.declared_content_type not in {None, ContentType.DOCUMENT} or job.resolved_content_type not in {ContentType.UNKNOWN, ContentType.DOCUMENT}:
            raise ValueError("document-run can resume only DOCUMENT Jobs")
        threshold = int(job.requested_options.get("document", {}).get("large_file_threshold_bytes", args.large_file_threshold_bytes))
    else:
        job, job_file = _new_document_job(args)
        threshold = args.large_file_threshold_bytes
    outcome = run_document_pipeline(job, DocumentProvider(large_file_threshold_bytes=threshold), job_file=job_file)
    payload = {
        "job_file": str(outcome.job_file),
        "job": outcome.job.to_dict(),
        "artifacts": [asdict(artifact) for artifact in (outcome.result.artifacts if outcome.result else ())],
        "provider_result": outcome.result.metadata if outcome.result else None,
        "error": outcome.error.to_dict() if outcome.error else None,
    }
    return payload, 0 if outcome.success else 2


def _stage3_run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    job_file = args.resume_job.expanduser()
    if job_file.is_symlink():
        raise ValueError("Job contract file cannot be a symlink")
    job = Job.from_json(job_file.read_text(encoding="utf-8"))
    outcome = run_video_stage3_pipeline(job, job_file=job_file)
    payload = {
        "job_file": str(outcome.job_file),
        "job": outcome.job.to_dict(),
        "status": outcome.status,
        "stage3": outcome.result,
        "error": outcome.error.to_dict() if outcome.error else None,
    }
    return payload, 0 if outcome.success else (3 if outcome.status == "paused" else 2)


def _finalize_job(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    job_file = args.resume_job.expanduser()
    if job_file.is_symlink():
        raise ValueError("Job contract file cannot be a symlink")
    job = Job.from_json(job_file.read_text(encoding="utf-8"))
    outcome = run_output_pipeline(job, job_file=job_file)
    payload: dict[str, object] = {
        "job_file": str(outcome.job_file),
        "job": outcome.job.to_dict(),
        "output_result": outcome.result.to_dict() if outcome.result else None,
        "error": outcome.error.to_dict() if outcome.error else None,
    }
    return payload, 0 if outcome.success else 2


def _deliver_videos() -> tuple[dict[str, object], int]:
    policy = load_default_policies(DEFAULT_CONFIG_PATH)
    if policy.delivery_root is None:
        raise ValueError("output.delivery_root is not configured")
    outcomes = deliver_completed_videos(policy.output_root, policy.delivery_root)
    payload: dict[str, object] = {
        "delivery_root": str(policy.delivery_root),
        "jobs": [
            {"job_id": item.job_id, "status": item.status,
             "destination": str(item.destination) if item.destination else None, "reason": item.reason}
            for item in outcomes
        ],
    }
    return payload, 2 if any(item.status == "failed" for item in outcomes) else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        if args.command == "inspect-config":
            payload: dict[str, object] = _policy_payload(args.config)
        elif args.command == "create-job":
            payload = _create_job(args)
        elif args.command == "show-job":
            payload = Job.from_json(args.job_file.read_text(encoding="utf-8")).to_dict()
        elif args.command == "video-run":
            payload, exit_code = _video_run(args)
        elif args.command == "image-run":
            payload, exit_code = _image_run(args)
        elif args.command == "article-run":
            payload, exit_code = _article_run(args)
        elif args.command == "webpage-run":
            payload, exit_code = _webpage_run(args)
        elif args.command == "document-run":
            payload, exit_code = _document_run(args)
        elif args.command == "stage3-run":
            payload, exit_code = _stage3_run(args)
        elif args.command == "finalize-job":
            payload, exit_code = _finalize_job(args)
        elif args.command == "deliver-videos":
            payload, exit_code = _deliver_videos()
        elif args.command == "translation-preflight":
            from .core.video_stage3_pipeline import TRANSLATION_HELPER
            payload = translation_preflight(TRANSLATION_HELPER, source_language=args.source, target_language=args.target)
            exit_code = {"ready": 0, "unsupported": 3, "resource_unavailable": 4}.get(str(payload.get("status")), 2)
        elif args.command == "translation-setup":
            from .core.video_stage3_pipeline import TRANSLATION_HELPER
            payload = setup_translation(TRANSLATION_HELPER, source_language=args.source, target_language=args.target)
            exit_code = {"ready": 0, "unsupported": 3, "resource_unavailable": 4}.get(str(payload.get("status")), 2)
        else:  # pragma: no cover - argparse keeps this unreachable
            raise ValueError(f"unsupported command: {args.command}")
    except ProviderFailure as failure:
        error = map_provider_failure(
            failure.kind.value,
            provider=failure.provider,
            cause=str(failure),
            retry_after_seconds=failure.retry_after_seconds,
        )
        print(json.dumps({"error": error.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, OutputContractError, ValueError) as error:
        print(json.dumps({"error": {"code": "CORE_CONTRACT_ERROR", "message": str(error)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
