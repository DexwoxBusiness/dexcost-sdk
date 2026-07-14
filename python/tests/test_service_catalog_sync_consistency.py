"""Cross-SDK service catalog safety and drift checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _find_repo_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (
            (parent / "go" / "pricing" / "data").exists()
            and (parent / "rust" / "src" / "data").exists()
            and (parent / "typescript" / "src" / "data").exists()
        ):
            return parent
    return None


def test_service_catalog_is_safe_and_byte_equal_across_sdks() -> None:
    repo_root = _find_repo_root()
    if repo_root is None:
        pytest.skip("non-monorepo install - other SDK directories not reachable")

    canonical = repo_root / "python" / "src" / "dexcost" / "data" / "service_prices.json"
    targets = {
        "go": repo_root / "go" / "pricing" / "data" / "service_prices.json",
        "rust": repo_root / "rust" / "src" / "data" / "service_prices.json",
        "typescript": repo_root / "typescript" / "src" / "data" / "service_prices.json",
    }

    canonical_bytes = canonical.read_bytes()
    drifted = [sdk for sdk, path in targets.items() if path.read_bytes() != canonical_bytes]
    assert not drifted, (
        f"service_prices.json drift detected in: {drifted}. "
        "Run: bash scripts/sync_service_catalog.sh"
    )

    catalog = json.loads(canonical_bytes)
    entries = {key: value for key, value in catalog.items() if key != "_meta"}
    metadata = catalog["_meta"]

    assert metadata["safety_policy_version"] == "2026-07-14.2"
    assert metadata["disabled_service_count"] == 94
    assert metadata["service_count"] == len(entries) == 73

    zero_rate_entries = [
        key
        for key, entry in entries.items()
        if any(
            field.startswith("cost_per_") and float(value) == 0
            for field, value in entry.items()
        )
    ]
    assert zero_rate_entries == []
    missing_rate_entries = [
        key
        for key, entry in entries.items()
        if not any(field.startswith("cost_per_") for field in entry)
    ]
    assert missing_rate_entries == []
