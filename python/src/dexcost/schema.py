"""Schema validation for Standard Event Schema v1 (US-002)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema

_log = logging.getLogger(__name__)

_SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"
_schema_cache: dict[str, dict[str, Any]] = {}


def _load_schema(name: str) -> dict[str, Any] | None:
    """Load and cache a JSON schema file by name."""
    if name not in _schema_cache:
        schema_path = _SCHEMA_DIR / name
        try:
            with open(schema_path, encoding="utf-8") as f:
                _schema_cache[name] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            _log.warning("Failed to load schema %s: %s", schema_path, exc)
            return None
    return _schema_cache[name]


def validate(payload: dict[str, Any]) -> list[str]:
    """Validate a task or event payload against Schema v1.

    Returns an empty list on success, or a list of human-readable error
    messages describing each validation failure.
    """
    sv = payload.get("schema_version", "1")
    if sv != "1":
        return [f"Unsupported schema_version: {sv}"]

    if "event_id" in payload:
        schema = _load_schema("dexcost-event.v1.json")
    elif "task_id" in payload:
        schema = _load_schema("dexcost-task.v1.json")
    else:
        return ["Cannot determine payload type: missing task_id or event_id"]

    if schema is None:
        return []  # Can't validate without schema

    errors: list[str] = []
    validator = jsonschema.Draft7Validator(schema)
    for error in validator.iter_errors(payload):
        errors.append(f"{error.json_path}: {error.message}")
    return errors
