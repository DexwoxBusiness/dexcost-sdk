"""Bundled attribution-v3 schema: in-place extension and packaging guarantees.

The v3 contract was extended in place (the wire ``schema_version`` stays ``"3"``)
with an optional ``environment``, ``operation.latency_ms``, ``operation.error``,
a ``"tool"`` resource type, and wide ``user_id``/``product_id`` business
identities. These tests pin the bundled JSON schema against those rules so the
emitters never need a strip-and-retry fallback again.
"""

from __future__ import annotations

import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import dexcost.schema as schema_module
from dexcost.attribution.v3_validate import (
    ATTRIBUTION_V3_SCHEMA_FILENAME,
    validate_attribution_observation_v3,
)
from dexcost.schema import SchemaNotFoundError, validate

_MAX_LATENCY_MS = 86_400_000

_BASE_OBSERVATION: dict[str, Any] = {
    "schema_version": "3",
    "event_id": "11111111-1111-4111-8111-111111111111",
    "task_id": "22222222-2222-4222-8222-222222222222",
    "occurred_at": "2026-08-11T10:00:00.123456Z",
    "observed_at": "2026-08-11T10:00:01.123456Z",
    "component": "llm",
    "provider": {"name": "anthropic", "service": "messages"},
    "resource": {"type": "model", "id": "claude-sonnet-4-5"},
    "operation": {
        "id": "33333333-3333-4333-8333-333333333333",
        "name": "agent.answer",
        "status": "succeeded",
        "attempt": {"id": "44444444-4444-4444-8444-444444444444", "number": 1},
    },
    "lifecycle": {"state": "final", "revision": 1},
    "usage_snapshot": "full",
    "usage": [
        {
            "line_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            "metric": "input_tokens",
            "quantity": "100",
            "unit": "Tokens",
            "dimensions": [],
        }
    ],
}


def _observation(**overrides: Any) -> dict[str, Any]:
    event = copy.deepcopy(_BASE_OBSERVATION)
    event.update(copy.deepcopy(overrides))
    return event


def _failed_observation(**operation_overrides: Any) -> dict[str, Any]:
    event = _observation()
    event["operation"]["status"] = "failed"
    event["operation"].update(copy.deepcopy(operation_overrides))
    event["usage"] = []
    return event


def _paths(event: dict[str, Any]) -> set[str]:
    return {issue.path for issue in validate_attribution_observation_v3(event).issues}


def _bundled_schema() -> dict[str, Any]:
    packaged = files("dexcost.attribution").joinpath(ATTRIBUTION_V3_SCHEMA_FILENAME)
    return json.loads(packaged.read_text(encoding="utf-8"))


# ------------------------------------------------------------------
# Baseline — pre-extension payloads keep validating unchanged
# ------------------------------------------------------------------


def test_observation_without_any_extension_field_still_validates() -> None:
    assert validate_attribution_observation_v3(_observation()).success


# ------------------------------------------------------------------
# environment
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "environment",
    ["production", "staging", "eu-west-1.prod", "a", "0", "a" * 64],
)
def test_environment_accepts_canonical_names(environment: str) -> None:
    result = validate_attribution_observation_v3(_observation(environment=environment))
    assert result.success, result.issues


@pytest.mark.parametrize(
    "environment",
    ["Production", "-prod", ".prod", "prod space", "prod/eu", "", "a" * 65],
)
def test_environment_rejects_non_canonical_names(environment: str) -> None:
    assert "environment" in _paths(_observation(environment=environment))


# ------------------------------------------------------------------
# operation.latency_ms
# ------------------------------------------------------------------


@pytest.mark.parametrize("latency_ms", [0, 1, 1250, _MAX_LATENCY_MS])
def test_latency_ms_accepts_whole_millisecond_bounds(latency_ms: int) -> None:
    event = _observation()
    event["operation"]["latency_ms"] = latency_ms
    result = validate_attribution_observation_v3(event)
    assert result.success, result.issues


@pytest.mark.parametrize("latency_ms", [-1, _MAX_LATENCY_MS + 1, 1.5, "1250", True])
def test_latency_ms_rejects_out_of_range_or_non_integer(latency_ms: object) -> None:
    event = _observation()
    event["operation"]["latency_ms"] = latency_ms
    assert "operation.latency_ms" in _paths(event)


# ------------------------------------------------------------------
# operation.error
# ------------------------------------------------------------------


def test_operation_error_accepted_on_a_failed_operation() -> None:
    result = validate_attribution_observation_v3(
        _failed_observation(error={"type": "timeout", "code": "ETIMEDOUT"})
    )
    assert result.success, result.issues


def test_operation_error_code_is_optional() -> None:
    result = validate_attribution_observation_v3(_failed_observation(error={"type": "rate_limit"}))
    assert result.success, result.issues


def test_operation_error_rejected_on_a_succeeded_operation() -> None:
    event = _observation()
    event["operation"]["error"] = {"type": "timeout"}
    assert "operation.error" in _paths(event)


def test_operation_error_requires_a_canonical_type() -> None:
    assert "operation.error.type" in _paths(_failed_observation(error={"type": "Timeout"}))


def test_operation_error_rejects_unknown_members() -> None:
    event = _failed_observation(error={"type": "timeout", "message": "boom"})
    assert "operation.error.message" in _paths(event)


@pytest.mark.parametrize("code", ["", "x" * 65])
def test_operation_error_code_length_is_bounded(code: str) -> None:
    event = _failed_observation(error={"type": "timeout", "code": code})
    assert "operation.error.code" in _paths(event)


# ------------------------------------------------------------------
# resource.type: "tool"
# ------------------------------------------------------------------


def test_tool_resource_type_is_accepted() -> None:
    event = _failed_observation()
    event["resource"] = {"type": "tool", "id": "web_browser"}
    result = validate_attribution_observation_v3(event)
    assert result.success, result.issues


def test_unknown_resource_type_is_still_rejected() -> None:
    event = _observation(resource={"type": "widget", "id": "web_browser"})
    assert "resource.type" in _paths(event)


# ------------------------------------------------------------------
# Business identity assignment — user_id / product_id
# ------------------------------------------------------------------


@pytest.mark.parametrize("field", ["user_id", "product_id"])
def test_business_assignment_accepts_wide_opaque_identities(field: str) -> None:
    document = _bundled_schema()
    validator = Draft202012Validator(
        {
            "$schema": document["$schema"],
            "$ref": "#/components/schemas/AttributionBusinessAssignment",
            "components": document["components"],
        },
        format_checker=FormatChecker(),
    )
    assert list(validator.iter_errors({field: "x" * 512})) == []
    assert list(validator.iter_errors({field: "x" * 513})) != []
    assert list(validator.iter_errors({field: ""})) != []


# ------------------------------------------------------------------
# Packaging — the schema must ship, and its absence must be loud
# ------------------------------------------------------------------


def test_attribution_v3_schema_is_packaged_inside_the_installed_package() -> None:
    packaged = files("dexcost.attribution").joinpath(ATTRIBUTION_V3_SCHEMA_FILENAME)
    assert packaged.is_file()
    assert json.loads(packaged.read_text(encoding="utf-8"))["components"]["schemas"]


def test_event_schema_v1_is_reachable_from_the_installed_package() -> None:
    assert validate({"schema_version": "1", "event_id": "x"}) != []


def test_validate_raises_when_the_schema_file_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(schema_module, "_SCHEMA_DIRS", (tmp_path,))
    monkeypatch.setattr(schema_module, "_schema_cache", {})

    with pytest.raises(SchemaNotFoundError) as excinfo:
        validate({"schema_version": "1", "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"})

    message = str(excinfo.value)
    assert "dexcost-task.v1.json" in message
    assert str(tmp_path) in message


def test_validate_reports_unreadable_schema_instead_of_passing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "dexcost-task.v1.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(schema_module, "_SCHEMA_DIRS", (tmp_path,))
    monkeypatch.setattr(schema_module, "_schema_cache", {})

    with pytest.raises(ValueError, match="not valid JSON"):
        validate({"schema_version": "1", "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"})
