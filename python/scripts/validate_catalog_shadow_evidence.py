"""Reject incomplete or internally inconsistent catalog production-shadow evidence."""

from __future__ import annotations

import argparse
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "contracts"
    / "python-vnext"
    / "v1"
    / "schemas"
    / "catalog-production-shadow.v1.schema.json"
)
CATALOG_KINDS = {
    "observer_rules",
    "llm_prices",
    "service_prices",
    "compute_prices",
    "gpu_prices",
    "egress_prices",
    "server_pricing_reference",
}
STAGES = ("canary", "promoted_stable", "changed_stable", "rollback")


def _schema_errors(value: object) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    ]


def validate_production_shadow_evidence(value: object) -> list[str]:
    """Return every reason this record cannot authorize a production decision."""
    errors = _schema_errors(value)
    if errors or not isinstance(value, dict):
        return errors

    evidence: dict[str, Any] = value
    if evidence["status"] != "passed":
        errors.append("status must be passed")
    if not evidence["evidence_id"]:
        errors.append("evidence_id must be recorded")
    if not evidence["recorded_at"]:
        errors.append("recorded_at must be recorded")

    environment = evidence["environment"]
    for field in (
        "control_plane_deployment_sha",
        "python_sdk_version",
        "typescript_sdk_version",
    ):
        if not environment[field]:
            errors.append(f"environment.{field} must be recorded")

    trust = evidence["trust"]
    key_ids = trust["key_ids"]
    fingerprints = trust["public_key_sha256"]
    if len(key_ids) < 2:
        errors.append("trust.key_ids must contain at least two rotation keys")
    if set(key_ids) != set(fingerprints):
        errors.append("trust.public_key_sha256 must exactly cover trust.key_ids")
    if trust["private_key_present_in_repository_or_evidence"] is not False:
        errors.append("private key material must not be present in repository or evidence")
    for field in (
        "python_requires_signature",
        "typescript_requires_signature",
        "dual_signature_rotation_verified",
    ):
        if trust[field] is not True:
            errors.append(f"trust.{field} must be true")

    lifecycle = evidence["release_lifecycle"]
    releases = lifecycle["releases"]
    if [item["stage"] for item in releases] != list(STAGES):
        errors.append("release_lifecycle.releases must use the required stage order")
    release_ids = [item["release_id"] for item in releases]
    sequences = [item["release_sequence"] for item in releases]
    if any(item is None for item in release_ids) or len(set(release_ids)) != 4:
        errors.append("release IDs must be recorded and unique")
    if any(item is None for item in sequences) or not all(
        left < right for left, right in pairwise(sequences)
    ):
        errors.append("release sequences must be recorded and strictly increasing")
    lifecycle_id_fields = (
        "canary_release_id",
        "promoted_stable_release_id",
        "changed_stable_release_id",
        "rollback_release_id",
    )
    for field, release_id in zip(lifecycle_id_fields, release_ids, strict=True):
        if lifecycle[field] != release_id:
            errors.append(f"release_lifecycle.{field} does not match release evidence")
    if lifecycle["sequences_are_strictly_increasing"] is not True:
        errors.append("release_lifecycle.sequences_are_strictly_increasing must be true")
    if lifecycle["artifact_hashes_match_across_server_python_typescript"] is not True:
        errors.append(
            "release_lifecycle.artifact_hashes_match_across_server_python_typescript "
            "must be true"
        )
    for index, release in enumerate(releases):
        server_hashes = release["server_artifact_sha256"]
        python_hashes = release["python_artifact_sha256"]
        typescript_hashes = release["typescript_artifact_sha256"]
        if (
            not isinstance(server_hashes, dict)
            or set(server_hashes) != CATALOG_KINDS
            or server_hashes != python_hashes
            or server_hashes != typescript_hashes
        ):
            errors.append(
                f"release_lifecycle.releases.{index} must contain identical seven-family "
                "server/Python/TypeScript hashes"
            )

    for gate, result in evidence["gates"].items():
        if result["status"] != "passed":
            errors.append(f"gates.{gate} must be passed")
        if not result["evidence_refs"]:
            errors.append(f"gates.{gate}.evidence_refs must not be empty")

    counters = evidence["counters"]
    attempted = counters["usage_events_attempted"]
    persisted = counters["usage_events_persisted"]
    reconciled = counters["usage_events_reconciled"]
    lost = counters["usage_events_lost"]
    if attempted is None or attempted <= 0:
        errors.append("counters.usage_events_attempted must be greater than zero")
    if persisted != attempted:
        errors.append("usage_events_persisted must equal usage_events_attempted")
    if lost != 0:
        errors.append("usage_events_lost must equal zero")
    if reconciled is None or reconciled <= 0:
        errors.append("usage_events_reconciled must be greater than zero")
    elif persisted is not None and reconciled > persisted:
        errors.append("usage_events_reconciled cannot exceed usage_events_persisted")
    queue_attempted = counters["queue_messages_attempted"]
    queue_acknowledged = counters["queue_messages_acknowledged"]
    queue_retried = counters["queue_messages_retried"]
    queue_dead_lettered = counters["queue_messages_dead_lettered"]
    if queue_attempted is None or queue_attempted <= 0:
        errors.append("counters.queue_messages_attempted must be greater than zero")
    if queue_acknowledged != queue_attempted:
        errors.append("queue_messages_acknowledged must equal queue_messages_attempted")
    if queue_retried is None or queue_retried <= 0:
        errors.append("queue_messages_retried must be greater than zero")
    if queue_dead_lettered != 0:
        errors.append("queue_messages_dead_lettered must equal zero")

    for owner, approval in evidence["approvals"].items():
        if not approval:
            errors.append(f"approvals.{owner} must be recorded")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    value = json.loads(args.evidence.read_text(encoding="utf-8"))
    errors = validate_production_shadow_evidence(value)
    if errors:
        raise SystemExit("catalog production-shadow evidence rejected:\n- " + "\n- ".join(errors))
    print(f"catalog production-shadow evidence accepted: {args.evidence}")


if __name__ == "__main__":
    main()
