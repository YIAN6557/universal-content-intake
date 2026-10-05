"""Per-installation files written by the setup wizard.

Both live next to each other in ``~/.config/universal-content-intake/``:

- ``config.yaml``: the user overlay that ``load_config`` merges over
  ``config/defaults.yaml`` (Queue API URL, delivery folder, ...).
- ``setup-state.json``: wizard progress (script and deployment IDs, steps the
  user confirmed, creators waiting for confirmation). Never holds secrets.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.core.policies import load_simple_yaml, merge_config, user_config_path

STATE_FILE_NAME = "setup-state.json"


def config_path() -> Path:
    path = user_config_path()
    if path is None:
        raise RuntimeError("UCI_CONFIG=none disables the user config; unset it to run setup.")
    return path


def state_path() -> Path:
    return config_path().parent / STATE_FILE_NAME


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_user_config() -> dict[str, Any]:
    path = config_path()
    return load_simple_yaml(path) if path.is_file() else {}


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    # The project's YAML reader strips everything after "#" and reads one line per key.
    if "#" in text or "\n" in text or '"' in text:
        raise ValueError(f"config values cannot contain '#', quotes or line breaks: {text!r}")
    return f'"{text}"'


def dump_simple_yaml(values: Mapping[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    for key, value in values.items():
        if isinstance(value, Mapping):
            lines.append(f"{' ' * indent}{key}:")
            lines.append(dump_simple_yaml(value, indent + 2).rstrip("\n"))
        else:
            lines.append(f"{' ' * indent}{key}: {_yaml_scalar(value)}")
    return "\n".join(line for line in lines if line) + "\n"


def update_user_config(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Merge nested ``changes`` into the user config and write it back."""

    merged = merge_config(read_user_config(), dict(changes))
    header = "# Written by bin/uci-setup; overrides config/defaults.yaml for this Mac.\n"
    _atomic_write(config_path(), header + dump_simple_yaml(merged))
    return merged


def read_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def update_state(**changes: Any) -> dict[str, Any]:
    state = read_state()
    state.update(changes)
    _atomic_write(state_path(), json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return state


def confirm(step: str) -> dict[str, Any]:
    state = read_state()
    confirmed = dict(state.get("confirmed") or {})
    confirmed[step] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return update_state(confirmed=confirmed)


def is_confirmed(state: Mapping[str, Any], step: str) -> bool:
    return bool((state.get("confirmed") or {}).get(step))
