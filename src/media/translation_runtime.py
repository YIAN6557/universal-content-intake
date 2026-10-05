"""No-UI Apple Translation helper invocation; Setup remains an explicit command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping


class TranslationRuntimeError(RuntimeError):
    pass


def _invoke(helper: Path, arguments: list[str], *, timeout_seconds: int = 3600) -> tuple[int, dict[str, Any]]:
    if not helper.is_file():
        raise TranslationRuntimeError("Apple Translation helper is not installed")
    try:
        result = subprocess.run([str(helper), *arguments], check=False, capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TranslationRuntimeError("Apple Translation helper could not execute") from error
    try:
        decoded = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise TranslationRuntimeError("Apple Translation helper returned invalid structured output") from error
    if not isinstance(decoded, dict):
        raise TranslationRuntimeError("Apple Translation helper output must be a JSON object")
    return result.returncode, decoded


def translation_preflight(helper: Path, *, source_language: str, target_language: str = "zh-Hans") -> dict[str, Any]:
    code, outcome = _invoke(helper, ["preflight", "--source", source_language, "--target", target_language], timeout_seconds=120)
    if code not in {0, 3, 4}:
        raise TranslationRuntimeError("Apple Translation preflight failed unexpectedly")
    return outcome


def translate_cues(
    helper: Path,
    *,
    request_path: Path,
    response_path: Path,
    source_language: str,
    target_language: str = "zh-Hans",
) -> dict[str, Any]:
    if not helper.is_file():
        raise TranslationRuntimeError("Apple Translation helper is not installed")
    try:
        process = subprocess.run([
            str(helper), "translate", "--input", str(request_path), "--output", str(response_path),
        ], check=False, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TranslationRuntimeError("Apple Translation cue translation could not execute") from error
    code = process.returncode
    try:
        outcome = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TranslationRuntimeError("Apple Translation helper response file is missing or invalid") from error
    if not isinstance(outcome, dict):
        raise TranslationRuntimeError("Apple Translation response must be a JSON object")
    if code == 3:
        return outcome
    if code == 4:
        return outcome
    if code != 0:
        raise TranslationRuntimeError("Apple Translation cue translation failed")
    try:
        response = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TranslationRuntimeError("Apple Translation response file is missing or invalid") from error
    if not isinstance(response, Mapping):
        raise TranslationRuntimeError("Apple Translation response must be a JSON object")
    if response.get("source_language") != source_language or response.get("target_language") != target_language:
        raise TranslationRuntimeError("Apple Translation response language pair does not match the request")
    return dict(response)


def setup_translation(helper: Path, *, source_language: str, target_language: str = "zh-Hans") -> dict[str, Any]:
    """Human-attended language-pack preparation. Never called from Job execution."""
    code, outcome = _invoke(helper, ["setup", "--source", source_language, "--target", target_language], timeout_seconds=3600)
    if code not in {0, 3, 4}:
        raise TranslationRuntimeError("Apple Translation attended Setup failed")
    return outcome
