"""Default-first policy loading without a Stage 1 third-party YAML dependency."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PolicyLoadError(ValueError):
    pass


# Shipped defaults live in the repository; each installation's own settings
# (Queue API URL, delivery folder, timezone, ...) live in a user config file
# written by first-run setup and merged over the defaults. UCI_CONFIG points
# elsewhere, or "none" disables the overlay (tests use this).
PROJECT_DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "config" / "defaults.yaml"
USER_CONFIG_ENV = "UCI_CONFIG"
DEFAULT_USER_CONFIG_PATH = Path("~/.config/universal-content-intake/config.yaml")


def user_config_path() -> Path | None:
    raw = os.environ.get(USER_CONFIG_ENV, "").strip()
    if raw.lower() == "none":
        return None
    return Path(raw or DEFAULT_USER_CONFIG_PATH).expanduser()


def merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


# The shipped defaults name macOS folders; on Windows the same places are spelled differently.
WINDOWS_DEFAULTS = {
    "~/Library/Application Support/Universal Content Intake": "%LOCALAPPDATA%/Universal Content Intake",
    "~/Movies/": "~/Videos/",
}


def _native_defaults(values: dict[str, Any]) -> dict[str, Any]:
    if os.name != "nt":
        return values

    def native(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: native(item) for key, item in value.items()}
        if isinstance(value, str):
            for mac, windows in WINDOWS_DEFAULTS.items():
                if value.startswith(mac):
                    local = os.environ.get("LOCALAPPDATA") or "~/AppData/Local"
                    return windows.replace("%LOCALAPPDATA%", local.replace("\\", "/")) + value[len(mac):]
        return value

    return native(values)


def load_config(path: Path | str) -> dict[str, Any]:
    """Load a config file; the project defaults also get the user overlay."""

    path = Path(path).expanduser()
    values = load_simple_yaml(path)
    try:
        is_project_defaults = path.resolve() == PROJECT_DEFAULTS_PATH.resolve()
    except OSError:
        is_project_defaults = False
    if is_project_defaults:
        values = _native_defaults(values)
    overlay = user_config_path() if is_project_defaults else None
    if overlay is not None and overlay.is_file():
        values = merge_config(values, load_simple_yaml(overlay))
    return values


def _scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def load_simple_yaml(path: Path) -> dict[str, Any]:
    """Load the restricted mapping-only YAML used by Stage 1 configuration files.

    JSON is accepted as valid YAML input. The implementation intentionally rejects
    YAML features not needed by Canonical defaults instead of silently guessing.
    """

    raw = path.read_text(encoding="utf-8")
    if raw.lstrip().startswith("{"):
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise PolicyLoadError("configuration root must be a mapping")
        return value
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        without_comment = raw_line.split("#", 1)[0].rstrip()
        if not without_comment.strip():
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        if "\t" in without_comment[:indent] or indent % 2:
            raise PolicyLoadError(f"line {line_number}: use two-space mapping indentation")
        stripped = without_comment.strip()
        if stripped.startswith("-") or ":" not in stripped:
            raise PolicyLoadError(f"line {line_number}: only mapping YAML is supported")
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise PolicyLoadError(f"line {line_number}: empty key")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise PolicyLoadError(f"line {line_number}: invalid indentation")
        parent = stack[-1][1]
        if key in parent:
            raise PolicyLoadError(f"line {line_number}: duplicate key {key}")
        if raw_value:
            parent[key] = _scalar(raw_value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


@dataclass(frozen=True)
class DefaultPolicies:
    schema_version: int
    target_translation_language: str
    video_target_resolution: str
    video_container: str
    video_preferred_video_codec: str
    video_preferred_audio_codec: str
    video_download_retries: int
    video_fragment_retries: int
    video_extractor_retries: int
    audio_output: str
    image_quality: str
    preserve_source_aspect_ratio: bool
    output_root: Path
    file_conflict: str
    archive_by_default: bool
    unnecessary_user_interruption: bool
    delivery_root: Path | None = None
    # Opt-in: retry a sign-in-gated YouTube video once with the local Chrome session.
    video_allow_browser_cookies: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "DefaultPolicies":
        try:
            schema_version = int(value["schema_version"])
            policies = cls(
                schema_version=schema_version,
                target_translation_language=str(value["translation"]["target_language"]),
                video_target_resolution=str(value["video"]["target_resolution"]),
                video_container=str(value["video"]["container"]),
                video_preferred_video_codec=str(value["video"]["preferred_video_codec"]),
                video_preferred_audio_codec=str(value["video"]["preferred_audio_codec"]),
                video_download_retries=int(value["video"]["download_retries"]),
                video_fragment_retries=int(value["video"]["fragment_retries"]),
                video_extractor_retries=int(value["video"]["extractor_retries"]),
                audio_output=str(value["audio"]["output_format"]),
                image_quality=str(value["image"]["quality"]),
                preserve_source_aspect_ratio=bool(value["media"]["preserve_source_aspect_ratio"]),
                output_root=Path(str(value["output"]["root"])).expanduser(),
                file_conflict=str(value["output"]["file_conflict"]),
                archive_by_default=bool(value["output"]["archive_by_default"]),
                unnecessary_user_interruption=bool(value["execution"]["unnecessary_user_interruption"]),
                delivery_root=Path(str(value["output"]["delivery_root"])).expanduser() if value["output"].get("delivery_root") else None,
                video_allow_browser_cookies=value["video"].get("allow_browser_cookies") is True,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PolicyLoadError("defaults.yaml is missing a required Core policy") from error
        if policies.schema_version != 1:
            raise PolicyLoadError(f"unsupported defaults schema version: {policies.schema_version}")
        if policies.file_conflict != "deterministic_rename":
            raise PolicyLoadError("Core requires deterministic non-overwrite conflict handling")
        if policies.archive_by_default:
            raise PolicyLoadError("Core policy forbids archive-by-default")
        if min(policies.video_download_retries, policies.video_fragment_retries, policies.video_extractor_retries) < 0:
            raise PolicyLoadError("video retries must be zero or greater")
        if policies.video_container != "mp4":
            raise PolicyLoadError("V1 VIDEO source container policy is MP4")
        return policies


def load_default_policies(path: Path) -> DefaultPolicies:
    return DefaultPolicies.from_mapping(load_config(path))
