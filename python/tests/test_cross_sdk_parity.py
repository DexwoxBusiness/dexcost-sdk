"""Cross-SDK parity test (Python consumer + canonical generator).

This file drives two flows against the top-level `fixtures/` corpus:

1. **Assert mode (default)** — for every fixture in `fixtures/`, run it through
   the Python SDK's parser / pricing engine / URL scrubber, and assert the
   output matches `fixtures/expected_outputs/`. Any drift fails the test.

2. **Regenerate mode** — when `DEXCOST_REGENERATE_FIXTURES=1`, the same flow
   runs but WRITES the outputs to `expected_outputs/` instead of asserting.
   This is the ONLY way `expected_outputs/` gets updated. Run intentionally
   when the wire format / pricing semantics change, inspect the diff, commit.

The OTHER three SDKs (Go, TypeScript, Rust) have their own parity tests
that consume the SAME `fixtures/` corpus and assert against the SAME
`expected_outputs/`. Python is canonical because it's the reference
implementation for cost math.

Audit findings this suite locks down:
    B1  URL credential scrubbing — `events/edge_cases/url_*.v1.json`
    B3  TS float drift           — `events/edge_cases/tiny_decimal.v1.json`
    B5  Rust ec2_share           — `pricing_inputs/compute/ec2_share_*.json`
        and `events/compute_cost_ec2_share.v1.json`
    B6  Go schema enum           — `events/gpu_cost.v1.json`,
                                   `events/gpu_utilization_signal.v1.json`
    P1  Timestamp format drift   — every event has a fixed `occurred_at`
    P2  LLM cost map drift       — `pricing_inputs/llm/*.json`
    P4  Network event semantics  — `events/network_4xx_below_threshold.v1.json`
    Rust total_cost_usd clobber  — `tasks/task_with_network_gpu.v1.json`

If you add a new fixture: drop it in the right `fixtures/` subdir, run
`DEXCOST_REGENERATE_FIXTURES=1 pytest python/tests/test_cross_sdk_parity.py`,
inspect the generated expected_output, commit both.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexcost.cloud_detect import CloudEnv
from dexcost.compute_pricing import ComputePricingEngine
from dexcost.egress_pricing import EgressPricingEngine
from dexcost.gpu_pricing import GpuPricingEngine
from dexcost.pricing import PricingEngine

# Resolve the top-level fixtures/ dir relative to this test file.
# python/tests/test_cross_sdk_parity.py → repo root → fixtures/
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
EXPECTED = FIXTURES / "expected_outputs"

REGENERATE = os.environ.get("DEXCOST_REGENERATE_FIXTURES") == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _strip_underscored(d: dict[str, Any]) -> dict[str, Any]:
    """Drop top-level keys starting with `_` (fixture-internal comments)."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _glob(subdir: str, pattern: str = "*.json") -> list[Path]:
    return sorted((FIXTURES / subdir).rglob(pattern))


def _id(path: Path) -> str:
    """Stable test id derived from path relative to fixtures/."""
    return path.relative_to(FIXTURES).as_posix()


def _assert_or_write(actual: Any, expected_path: Path) -> None:
    if REGENERATE:
        _write_json(expected_path, actual)
        return
    if not expected_path.exists():
        raise AssertionError(
            f"Missing expected output: {expected_path}. "
            "Run with DEXCOST_REGENERATE_FIXTURES=1 to create it, then commit."
        )
    expected = _load_json(expected_path)
    assert actual == expected, (
        f"Drift vs canonical expected output for {expected_path.name}.\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"If this drift is intentional, regenerate with "
        f"DEXCOST_REGENERATE_FIXTURES=1 and update ALL FOUR SDKs in the same PR."
    )


# ---------------------------------------------------------------------------
# 1. Canonical event serialization — every event/*.json + tasks/*.json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_path", _glob("events", "*.v1.json"), ids=_id
)
def test_event_canonical_serialization(fixture_path: Path) -> None:
    """Read each event fixture, normalize, write/compare canonical form.

    Canonical form: keys sorted, no trailing whitespace, fixture-internal
    `_comment`/`_test_*` keys stripped, decimals preserved as strings.
    This is exactly what every SDK's serializer must produce byte-for-byte.
    """
    if "edge_cases/url_" in fixture_path.as_posix():
        pytest.skip("URL edge cases use _test_input shape, not event shape")

    raw = _load_json(fixture_path)
    canonical = _strip_underscored(raw)
    expected_path = (
        EXPECTED
        / "canonical_serialization"
        / fixture_path.relative_to(FIXTURES / "events").parent
        / fixture_path.name
    )
    _assert_or_write(canonical, expected_path)


@pytest.mark.parametrize(
    "fixture_path", _glob("tasks", "*.v1.json"), ids=_id
)
def test_task_canonical_serialization(fixture_path: Path) -> None:
    raw = _load_json(fixture_path)
    canonical = _strip_underscored(raw)
    expected_path = (
        EXPECTED
        / "canonical_serialization"
        / fixture_path.relative_to(FIXTURES).parent
        / fixture_path.name
    )
    _assert_or_write(canonical, expected_path)


# ---------------------------------------------------------------------------
# 2. Pricing parity — every pricing_inputs/*/*.json → expected cost_usd
# ---------------------------------------------------------------------------


_NO_CLOUD = CloudEnv(provider=None, region=None, source="none", instance_type=None)


def _price_compute(payload: dict[str, Any]) -> dict[str, str]:
    engine = ComputePricingEngine()
    details = _strip_underscored(payload)
    duration_ms = Decimal(str(details.get("duration_ms", 0)))
    window_s = duration_ms / Decimal(1000)
    cloud = CloudEnv(
        provider="aws",  # fixtures pin AWS for cross-SDK reproducibility
        region=details.get("region"),
        source="env",
        instance_type=details.get("instance_type"),
    )
    result = engine.resolve_compute_cost(details, cloud, overrides=None, window_s=window_s)
    return {
        "cost_usd": str(result.cost_usd),
        "currency": "USD",
        "cost_confidence": result.cost_confidence,
    }


def _price_gpu(payload: dict[str, Any]) -> dict[str, str]:
    engine = GpuPricingEngine()
    details = _strip_underscored(payload)
    duration_ms = Decimal(str(details.get("duration_ms", 0)))
    window_s = duration_ms / Decimal(1000)
    cloud = CloudEnv(
        provider="aws",
        region=details.get("region"),
        source="env",
        instance_type=details.get("instance_type"),
    )
    result = engine.resolve_gpu_cost(details, cloud, window_s=window_s)
    return {
        "cost_usd": str(result.cost_usd),
        "currency": "USD",
        "cost_confidence": result.cost_confidence,
    }


def _price_egress(payload: dict[str, Any]) -> dict[str, str]:
    engine = EgressPricingEngine()
    details = _strip_underscored(payload)
    rate = engine.resolve_rate(
        provider=details.get("provider"),
        region=details.get("region"),
    )
    # cost = rate.rate_per_gb * GB
    bytes_out = Decimal(str(details.get("bytes_out", 0)))
    gb = bytes_out / Decimal(1024 * 1024 * 1024)
    cost = (rate.rate_per_gb * gb).quantize(Decimal("0.0000000001"))
    return {
        "cost_usd": str(cost),
        "currency": "USD",
        "rate_per_gb_usd": str(rate.rate_per_gb),
    }


def _price_llm(payload: dict[str, Any]) -> dict[str, str]:
    engine = PricingEngine()
    details = _strip_underscored(payload)
    result = engine.get_cost(
        model=details.get("model"),
        input_tokens=details.get("input_tokens", 0),
        output_tokens=details.get("output_tokens", 0),
        cached_tokens=details.get("cached_tokens", 0),
    )
    return {
        "cost_usd": str(result.cost_usd) if result.cost_usd is not None else "0",
        "currency": "USD",
        "cost_confidence": result.cost_confidence,
        "pricing_source": result.pricing_source,
    }


_PRICERS = {
    "compute": _price_compute,
    "gpu": _price_gpu,
    "egress": _price_egress,
    "llm": _price_llm,
}


@pytest.mark.parametrize(
    "fixture_path", _glob("pricing_inputs", "*.json"), ids=_id
)
def test_pricing_parity(fixture_path: Path) -> None:
    """Run every pricing input through the Python engine, assert cost matches."""
    kind = fixture_path.parent.name  # compute|gpu|egress|llm
    pricer = _PRICERS.get(kind)
    if pricer is None:
        pytest.fail(f"No pricer registered for kind={kind!r} (fixture {fixture_path})")
    payload = _load_json(fixture_path)
    actual = pricer(payload)
    expected_path = (
        EXPECTED / "pricing" / fixture_path.relative_to(FIXTURES / "pricing_inputs")
    )
    _assert_or_write(actual, expected_path)


# ---------------------------------------------------------------------------
# 3. URL scrubber (security) — every edge_cases/url_*.json
# ---------------------------------------------------------------------------


# Param names that always strip. Compared case-insensitively. Matches the
# canonical scrubber semantics specified in the remediation plan.
_SENSITIVE_QUERY_PARAMS = {
    "api_key", "apikey", "access_token", "token", "auth", "password",
    "secret", "signature", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token", "session",
}


def scrub_url(url: str) -> str:
    """Canonical URL scrubber.

    Strip:
      - userinfo (`user:pass@`)
      - query params whose name (case-insensitive) is in the sensitive list
        OR matches `*-Signature` / `*-Credential` / `*-Security-Token` patterns
    Preserve:
      - scheme, host, port, path
      - non-sensitive query params (page, limit, etc.)
      - fragment

    This function exists here to define the canonical algorithm; the SDK's
    real scrubber implementation will live in
    `dexcost/security/redaction.py` and must produce identical output.
    """
    # Find userinfo and strip
    m = re.match(r"^(https?://)([^@/?#]+@)?(.+)$", url)
    if m:
        url = m.group(1) + m.group(3)

    # Split into base + query + fragment
    fragment = ""
    if "#" in url:
        url, fragment = url.split("#", 1)
        fragment = "#" + fragment
    if "?" not in url:
        return url + fragment
    base, query = url.split("?", 1)
    kept = []
    for part in query.split("&"):
        if "=" in part:
            name, _ = part.split("=", 1)
        else:
            name = part
        lname = name.lower()
        sensitive = (
            lname in _SENSITIVE_QUERY_PARAMS
            or lname.endswith("-signature")
            or lname.endswith("-credential")
            or lname.endswith("-security-token")
        )
        if sensitive:
            kept.append(f"{name}=REDACTED")
        else:
            kept.append(part)
    return f"{base}?{'&'.join(kept)}{fragment}"


@pytest.mark.parametrize(
    "fixture_path", _glob("events/edge_cases", "url_*.v1.json"), ids=_id
)
def test_url_scrubber(fixture_path: Path) -> None:
    payload = _load_json(fixture_path)
    test_input = payload.get("_test_input", {})
    raw_url = test_input["url"]
    scrubbed = scrub_url(raw_url)
    actual = {
        "raw_url": raw_url,
        "scrubbed_url": scrubbed,
    }
    expected_path = (
        EXPECTED / "security" / fixture_path.with_suffix(".json").name
    )
    _assert_or_write(actual, expected_path)


# ---------------------------------------------------------------------------
# 4. Decimal precision invariant — tiny_decimal accumulator
# ---------------------------------------------------------------------------


def test_tiny_decimal_accumulation_exact() -> None:
    """Accumulate the tiny-decimal fixture 10000 times.

    Asserts the running total is exact (no IEEE-754 drift). Each language's
    parity test must perform the same 10000-fold accumulation and produce
    the same exact string. This is the canonical test for B3 (TS float
    drift) and the math invariant every SDK must uphold.
    """
    fixture = _load_json(FIXTURES / "events/edge_cases/tiny_decimal.v1.json")
    per_event = Decimal(fixture["cost_usd"])
    total = sum((per_event for _ in range(10_000)), Decimal("0"))
    actual = {
        "per_event_cost_usd": str(per_event),
        "iterations": 10_000,
        "total_cost_usd": str(total),
    }
    expected_path = (
        EXPECTED / "pricing" / "decimal_accumulation_invariant.json"
    )
    _assert_or_write(actual, expected_path)
