"""Mechanical release gate for the versioned Python vNext contract freeze."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

import dexcost
from dexcost.catalog_releases import (
    parse_catalog_manifest,
    verify_catalog_manifest_signature,
)
from dexcost.models.event import Event
from dexcost.models.pricing_explanation import PricingProvenance
from dexcost.pricing_explain import explain_event_pricing, register_pricing_provenance
from dexcost.storage.migrations import TARGET_SCHEMA_VERSION

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPOSITORY_ROOT.parent
PYTHON_ROOT = REPOSITORY_ROOT / "python"
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "python-vnext" / "v1"
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
GOLDEN_ROOT = CONTRACT_ROOT / "golden"
FORMAT_CHECKER = FormatChecker()


def _canonical_artifact_bytes(relative: str, data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n") if relative.endswith(".json") else data


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(name: str) -> Draft202012Validator:
    schema = _load(SCHEMA_ROOT / name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


def _assert_valid(validator: Draft202012Validator, value: object) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    assert errors == [], "\n".join(
        f"{'.'.join(map(str, error.path))}: {error.message}" for error in errors
    )


def test_generated_freeze_has_no_drift() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/freeze_contract.py", "--check"],
        cwd=PYTHON_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_manifest_hashes_every_artifact_and_shared_reference() -> None:
    manifest = _load(CONTRACT_ROOT / "manifest.json")
    declared = {entry["path"]: entry for entry in manifest["artifacts"]}
    actual = {
        path.relative_to(CONTRACT_ROOT).as_posix()
        for path in CONTRACT_ROOT.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert set(declared) == actual
    for relative, entry in declared.items():
        payload = _canonical_artifact_bytes(
            relative, (CONTRACT_ROOT / relative).read_bytes()
        )
        assert entry["byte_size"] == len(payload)
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()

    for entry in manifest["referenced_artifacts"]:
        payload = _canonical_artifact_bytes(
            entry["path"], (REPOSITORY_ROOT / entry["path"]).read_bytes()
        )
        assert entry["byte_size"] == len(payload)
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()


def test_catalog_production_trust_is_identical_in_both_sdk_packages() -> None:
    canonical = (CONTRACT_ROOT / "catalog-production-trust.json").read_bytes()
    assert canonical == (
        PYTHON_ROOT / "src" / "dexcost" / "data" / "catalog_production_trust.json"
    ).read_bytes()
    assert canonical == (
        REPOSITORY_ROOT
        / "typescript"
        / "src"
        / "core"
        / "catalog-production-trust.json"
    ).read_bytes()


def test_public_api_and_migration_snapshots_cover_the_reference() -> None:
    public = _load(CONTRACT_ROOT / "public-api.json")
    assert [entry["name"] for entry in public["exports"]] == list(dexcost.__all__)
    assert public["package_version"] == dexcost.__version__

    storage = _load(CONTRACT_ROOT / "storage-migrations.json")
    assert storage["target_schema_version"] == TARGET_SCHEMA_VERSION
    assert [(row["from"], row["to"]) for row in storage["migrations"]] == [
        (version, version + 1) for version in range(1, TARGET_SCHEMA_VERSION)
    ]
    assert {row["name"] for row in storage["schema"] if row["type"] == "table"} >= {
        "tasks", "events", "outcomes", "revenues", "provider_job_revisions",
        "sdk_catalog_artifacts", "sdk_catalog_releases", "sdk_catalog_state",
    }


def test_capability_matrices_are_exhaustive_and_resolved() -> None:
    matrix = _load(CONTRACT_ROOT / "capability-matrix.json")
    implementations = set(matrix["implementations"])
    allowed = {"implemented", "intentionally_excluded", "required"}
    assert len(matrix["rows"]) >= 40
    assert len({row["id"] for row in matrix["rows"]}) == len(matrix["rows"])
    for row in matrix["rows"]:
        assert set(row["cells"]) == implementations
        for implementation, cell in row["cells"].items():
            assert cell["status"] in allowed
            assert cell["status"] != "required", (
                f"unresolved freeze row {row['id']} for {implementation}"
            )
            if cell["status"] == "implemented":
                assert cell["evidence"]
            else:
                assert cell["rationale"].strip()
            for evidence in cell["evidence"]:
                if evidence.startswith("audit:"):
                    continue
                root = WORKSPACE_ROOT if evidence.startswith("control-plane/") else REPOSITORY_ROOT
                assert (root / evidence).exists(), f"missing evidence {evidence}"

    providers = _load(CONTRACT_ROOT / "provider-capabilities.json")
    assert len(providers["providers"]) >= 12
    assert len(set(providers["dimensions"])) == len(providers["dimensions"])
    for provider in providers["providers"]:
        assert set(provider["coverage"]) == set(providers["dimensions"])
        for dimension, languages in provider["coverage"].items():
            assert set(languages) == {"dexcost_python", "dexcost_typescript"}
            for language, cell in languages.items():
                assert cell["status"] in allowed - {"required"}, (
                    f"unresolved {provider['id']}.{dimension}.{language}"
                )
                if cell["status"] == "implemented":
                    assert cell["evidence"]
                    for evidence in cell["evidence"]:
                        assert (REPOSITORY_ROOT / evidence).exists(), evidence
                else:
                    assert cell["rationale"].strip()


def test_all_contract_schemas_are_valid_draft_2020_12() -> None:
    schemas = sorted(SCHEMA_ROOT.glob("*.schema.json"))
    assert len(schemas) >= 10
    for path in schemas:
        Draft202012Validator.check_schema(_load(path))


def test_catalog_production_shadow_template_and_final_validator(tmp_path: Path) -> None:
    template_path = (
        CONTRACT_ROOT / "evidence" / "catalog-production-shadow.template.json"
    )
    template = _load(template_path)
    _assert_valid(
        _validator("catalog-production-shadow.v1.schema.json"),
        template,
    )
    validator = PYTHON_ROOT / "scripts" / "validate_catalog_shadow_evidence.py"
    rejected_template = subprocess.run(
        [sys.executable, str(validator), str(template_path)],
        cwd=PYTHON_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected_template.returncode != 0
    assert "status must be passed" in rejected_template.stderr

    hashes = {
        kind: hashlib.sha256(kind.encode()).hexdigest()
        for kind in (
            "observer_rules",
            "llm_prices",
            "service_prices",
            "compute_prices",
            "gpu_prices",
            "egress_prices",
            "server_pricing_reference",
        )
    }
    evidence = json.loads(json.dumps(template))
    evidence.update(
        evidence_id="catalog-production-shadow-test",
        status="passed",
        recorded_at="2026-08-22T00:00:00Z",
    )
    evidence["environment"].update(
        control_plane_deployment_sha="a" * 40,
        python_sdk_version="0.18.0",
        typescript_sdk_version="0.22.0",
    )
    evidence["trust"].update(
        key_ids=["catalog-key-old", "catalog-key-new"],
        public_key_sha256={
            "catalog-key-old": "1" * 64,
            "catalog-key-new": "2" * 64,
        },
        python_requires_signature=True,
        typescript_requires_signature=True,
        dual_signature_rotation_verified=True,
    )
    release_ids = [f"catalog-release-shadow-{index}" for index in range(1, 5)]
    lifecycle = evidence["release_lifecycle"]
    lifecycle.update(
        canary_release_id=release_ids[0],
        promoted_stable_release_id=release_ids[1],
        changed_stable_release_id=release_ids[2],
        rollback_release_id=release_ids[3],
        sequences_are_strictly_increasing=True,
        artifact_hashes_match_across_server_python_typescript=True,
    )
    for index, release in enumerate(lifecycle["releases"]):
        release.update(
            release_id=release_ids[index],
            release_sequence=101 + index,
            server_artifact_sha256=hashes,
            python_artifact_sha256=hashes,
            typescript_artifact_sha256=hashes,
        )
    evidence["gates"] = {
        key: {"status": "passed", "evidence_refs": [f"test://{key}"]}
        for key in evidence["gates"]
    }
    evidence["counters"].update(
        usage_events_attempted=10,
        usage_events_persisted=10,
        usage_events_reconciled=2,
        usage_events_lost=0,
        queue_messages_attempted=4,
        queue_messages_acknowledged=4,
        queue_messages_retried=1,
        queue_messages_dead_lettered=0,
    )
    evidence["approvals"] = {
        owner: f"approved:{owner}" for owner in evidence["approvals"]
    }
    evidence_path = tmp_path / "production-shadow.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    accepted = subprocess.run(
        [sys.executable, str(validator), str(evidence_path)],
        cwd=PYTHON_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    evidence["counters"]["usage_events_lost"] = 1
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    rejected_loss = subprocess.run(
        [sys.executable, str(validator), str(evidence_path)],
        cwd=PYTHON_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected_loss.returncode != 0
    assert "usage_events_lost must equal zero" in rejected_loss.stderr


def test_golden_ingest_request_and_ack_validate() -> None:
    case = _load(GOLDEN_ROOT / "ingest-accepted.v1.json")
    request = case["request"]
    _assert_valid(_validator("task.v1.schema.json"), request["tasks"][0])
    _assert_valid(
        _validator("business-identity-revision.v1.schema.json"),
        request["business_identities"][0],
    )
    _assert_valid(_validator("outcome-revision.v1.schema.json"), request["outcomes"][0])
    _assert_valid(
        _validator("revenue-revision.v1.schema.json"),
        request["revenue_revisions"][0],
    )

    shared = _load(REPOSITORY_ROOT / "fixtures" / "attribution_v3" / "schemas.json")
    observation_schema = {
        "$schema": shared["$schema"],
        "$ref": "#/components/schemas/AttributionObservation",
        "components": shared["components"],
    }
    _assert_valid(
        Draft202012Validator(observation_schema, format_checker=FORMAT_CHECKER),
        request["events"][0],
    )

    response = _validator("ingestion-response.v1.schema.json")
    _assert_valid(response, case["response"])
    _assert_valid(
        response,
        _load(GOLDEN_ROOT / "ingest-partial-ack.v1.json")["response"],
    )
    for failure in _load(GOLDEN_ROOT / "ingestion-failures.v1.json")["cases"]:
        if "body" in failure:
            _assert_valid(response, failure["body"])


def test_golden_provider_job_and_catalog_release_validate() -> None:
    provider_jobs = _load(GOLDEN_ROOT / "provider-job-lifecycle.v1.json")["revisions"]
    provider_validator = _validator("provider-job-revision.v1.schema.json")
    for revision in provider_jobs:
        _assert_valid(provider_validator, revision)
    assert [revision["revision"] for revision in provider_jobs] == [1, 2]
    assert len({revision["event_id"] for revision in provider_jobs}) == 1

    manifest = _load(GOLDEN_ROOT / "catalog-release.v1.json")
    _assert_valid(_validator("catalog-manifest.v1.schema.json"), manifest)
    assert set(manifest["artifacts"]) == {
        "observer_rules", "llm_prices", "service_prices", "compute_prices",
        "gpu_prices", "egress_prices", "server_pricing_reference",
    }
    assert all(
        descriptor["path"].endswith(descriptor["sha256"])
        for descriptor in manifest["artifacts"].values()
    )
    manifest_raw = (GOLDEN_ROOT / "catalog-release.v1.json").read_bytes()
    parsed_manifest = parse_catalog_manifest(manifest_raw)
    assert verify_catalog_manifest_signature(
        parsed_manifest,
        {
            "dexcost-test-rfc8032-1":
                "11qYAYKxCrfVS_7TyWQHOg7hcvPa"
                "piMlrwIaaPcHURo"
        },
        require_signature=True,
    ) == "dexcost-test-rfc8032-1"


def test_golden_observer_rules_and_overlay_validate() -> None:
    observers = _load(REPOSITORY_ROOT / "fixtures" / "service_usage_observers.json")
    _assert_valid(_validator("catalog-observer-rules.v1.schema.json"), observers)
    assert observers["_meta"]["observer_count"] == len(observers["observers"])
    keys = [observer["service_key"] for observer in observers["observers"]]
    assert len(keys) == len(set(keys))

    overlay = {
        "schema_version": "1",
        "base_release_id": "catalog-release-fixture-184",
        "base_release_sequence": 184,
        "generated_at": "2026-08-21T06:00:00Z",
        "overrides": [{
            "kind": "gpu", "key": "owned/a100", "rate_usd": "1.25",
            "per": "gpu_hour", "notes": None, "updated_at": "2026-08-21T06:00:00Z",
        }],
    }
    _assert_valid(_validator("catalog-overlay.v1.schema.json"), overlay)


def test_golden_pricing_explanation_executes_against_python() -> None:
    case = _load(GOLDEN_ROOT / "pricing-explanation.v1.json")
    raw = case["input_event"]
    provenance = PricingProvenance.from_dict(case["expected"]["provenance"])
    register_pricing_provenance(raw["pricing_version"], provenance)
    event = Event(
        event_id=uuid.UUID(raw["event_id"]),
        task_id=uuid.UUID(raw["task_id"]),
        event_type=raw["event_type"],
        provider=raw["provider"],
        model=raw["model"],
        input_tokens=raw["input_tokens"],
        output_tokens=raw["output_tokens"],
        cached_tokens=raw["cached_tokens"],
        cost_usd=Decimal(raw["cost_usd"]),
        cost_confidence=raw["cost_confidence"],
        pricing_source=raw["pricing_source"],
        pricing_version=raw["pricing_version"],
    )
    assert explain_event_pricing(event).to_dict() == case["expected"]


def test_stream_and_slimming_decision_registers_are_closed() -> None:
    stream = _load(GOLDEN_ROOT / "stream-lifecycles.v1.json")
    assert len(stream["cases"]) >= 8
    assert {case["operation_status"] for case in stream["cases"]} == {
        "succeeded", "failed", "cancelled",
    }
    assert all(case["records"] == 1 for case in stream["cases"])

    slimming = _load(CONTRACT_ROOT / "catalog-slimming-gate.json")
    assert slimming["decision"] == "retain_full_bundles"
    assert all(item["status"] == "unproven" for item in slimming["requirements"])
