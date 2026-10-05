"""Provider-neutral freshness identity contracts for remote artifacts."""

from __future__ import annotations

from typing import Any, Mapping


REMOTE_IDENTITY_SCHEMA_VERSION = 1

_REVISION_FIELDS = (
    "revision_id",
    "revision",
    "version",
    "generation",
    "remote_checksum",
    "checksum",
    "sha256",
    "sha1",
    "md5",
)
_MODIFIED_FIELDS = ("modified_time", "mod_time", "last_modified", "mtime")
_SIZE_FIELDS = ("size", "content_length", "expected_size")


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _freshness_fields(fields: Mapping[str, Any]) -> list[str]:
    for key in _REVISION_FIELDS:
        value = fields.get(key)
        if not _present(value):
            continue
        if key == "etag" and str(value).strip().lower().startswith("w/"):
            continue
        return [key]

    etag = fields.get("etag")
    if _present(etag) and not str(etag).strip().lower().startswith("w/"):
        return ["etag"]

    modified = next((key for key in _MODIFIED_FIELDS if _present(fields.get(key))), None)
    size = next((key for key in _SIZE_FIELDS if _present(fields.get(key))), None)
    if modified and size:
        return [modified, size]
    return []


def remote_identity(provider: str, fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a serializable identity snapshot without inventing missing fields.

    A provider can pass arbitrary metadata fields. Core selects only documented
    freshness validators when deciding whether a cached artifact can be reused.
    File IDs, URLs, names, and paths are retained as scope data but are not
    freshness proof on their own.
    """

    normalized = {
        str(key): value
        for key, value in sorted((fields or {}).items(), key=lambda item: str(item[0]))
        if _present(value)
    }
    freshness_fields = _freshness_fields(normalized)
    return {
        "schema_version": REMOTE_IDENTITY_SCHEMA_VERSION,
        "provider": provider,
        "fields": normalized,
        "strength": "strong" if freshness_fields else "weak",
        "freshness_fields": freshness_fields,
        "freshness_verified": False,
    }


def remote_identity_is_strong(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != REMOTE_IDENTITY_SCHEMA_VERSION:
        return False
    if not isinstance(value.get("provider"), str) or not value["provider"]:
        return False
    fields = value.get("fields")
    claimed = value.get("freshness_fields")
    if not isinstance(fields, dict) or not isinstance(claimed, list):
        return False
    selected = _freshness_fields(fields)
    return bool(selected) and value.get("strength") == "strong" and claimed == selected


def remote_identity_matches(current: Any, stored: Any) -> bool:
    """Return true only when two strong, current remote snapshots are equal."""

    if not remote_identity_is_strong(current) or not remote_identity_is_strong(stored):
        return False
    return (
        current.get("provider") == stored.get("provider")
        and current.get("freshness_fields") == stored.get("freshness_fields")
        and current.get("fields") == stored.get("fields")
    )


def verified_remote_identity(value: Any, *, verified: bool) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = dict(value)
    result["freshness_verified"] = bool(verified and remote_identity_is_strong(value))
    return result


__all__ = [
    "REMOTE_IDENTITY_SCHEMA_VERSION",
    "remote_identity",
    "remote_identity_is_strong",
    "remote_identity_matches",
    "verified_remote_identity",
]
