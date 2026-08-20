"""Schema validation for Standard Event Schema v1 (US-002)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema

_log = logging.getLogger(__name__)


class SchemaNotFoundError(FileNotFoundError):
    """Raised when a bundled JSON schema is missing from the installed package.

    Silently skipping validation hides an entire class of packaging bugs: a
    wheel built without the schema files would accept every malformed payload.
    Loading now fails loudly instead.
    """


# Wheels ship the schemas inside the package (see ``force-include`` in
# pyproject.toml); source checkouts and editable installs read them from the
# repository directory next to ``src/``.
_PACKAGE_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
_REPO_SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"
_SCHEMA_DIRS = (_PACKAGE_SCHEMA_DIR, _REPO_SCHEMA_DIR)
_schema_cache: dict[str, dict[str, Any]] = {}


def _load_schema(name: str) -> dict[str, Any]:
    """Load and cache a JSON schema file by name.

    Raises:
        SchemaNotFoundError: the schema is absent from every packaged and
            repository location, so validation cannot be performed at all.
        ValueError: the schema file exists but is not readable JSON.
    """
    if name not in _schema_cache:
        searched: list[str] = []
        for directory in _SCHEMA_DIRS:
            schema_path = directory / name
            searched.append(str(schema_path))
            if not schema_path.is_file():
                continue
            try:
                with open(schema_path, encoding="utf-8") as f:
                    _schema_cache[name] = json.load(f)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Schema {schema_path} is not valid JSON: {exc}") from exc
            except OSError as exc:
                raise ValueError(f"Schema {schema_path} could not be read: {exc}") from exc
            break
        else:
            raise SchemaNotFoundError(
                f"Bundled schema {name!r} is missing from the installed dexcost package. "
                f"Searched: {', '.join(searched)}. Reinstall dexcost from a wheel built with "
                "the schemas packaged (pyproject force-include)."
            )
    return _schema_cache[name]


def validate(payload: dict[str, Any]) -> list[str]:
    """Validate a task or event payload against Schema v1.

    Returns an empty list on success, or a list of human-readable error
    messages describing each validation failure.

    Raises:
        SchemaNotFoundError: the bundled schema is missing from the install.
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

    errors: list[str] = []
    validator = jsonschema.Draft7Validator(schema)
    for error in validator.iter_errors(payload):
        errors.append(f"{error.json_path}: {error.message}")
    return errors
