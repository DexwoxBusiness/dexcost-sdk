"""Cross-SDK gpu_prices.json drift check.

Asserts the canonical Python catalog is byte-equal to the synced bundles
in go/pricing/data/, rust/src/data/, and typescript/src/data/. Skips
gracefully when running from a published wheel where the other SDK
directories aren't reachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _find_repo_root() -> Path | None:
    """Locate the dexcost-sdk repo root by walking up from this test file."""
    cur = Path(__file__).resolve()
    for parent in cur.parents:
        if (parent / "go" / "pricing" / "data").exists() and \
           (parent / "rust" / "src" / "data").exists() and \
           (parent / "typescript" / "src" / "data").exists():
            return parent
    return None


def test_gpu_catalog_byte_equal_across_sdks():
    """All four SDKs MUST bundle byte-identical gpu_prices.json.

    scripts/sync_gpu_catalog.sh is the canonical sync tool. If this test
    fails, run: bash scripts/sync_gpu_catalog.sh
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        pytest.skip("non-monorepo install — other SDK directories not reachable")

    canonical = repo_root / "python" / "src" / "dexcost" / "data" / "gpu_prices.json"
    targets = {
        "go": repo_root / "go" / "pricing" / "data" / "gpu_prices.json",
        "rust": repo_root / "rust" / "src" / "data" / "gpu_prices.json",
        "typescript": repo_root / "typescript" / "src" / "data" / "gpu_prices.json",
    }

    canonical_bytes = canonical.read_bytes()
    drifted = []
    for sdk, path in targets.items():
        if not path.exists():
            pytest.skip(f"{sdk} bundle missing — non-monorepo layout")
        if path.read_bytes() != canonical_bytes:
            drifted.append(sdk)

    assert not drifted, (
        f"gpu_prices.json drift detected in: {drifted}. "
        f"Run: bash scripts/sync_gpu_catalog.sh"
    )
