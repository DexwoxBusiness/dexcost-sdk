# Network Cost (v2) — Egress Pricing (Python SDK) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the external egress bytes captured by v1 into dollars per task — a `Task.network_cost_usd` field, populated at task finalize from a bundled per-region egress catalog and an automatic cloud-environment detection probe. Implements the approved spec `docs/superpowers/specs/2026-05-20-network-cost-v2-design.md`.

**Architecture:** Three new modules — a bundled `data/egress_prices.json` catalog, an `egress_pricing.py` resolver that mirrors `pricing.py`, and a non-blocking `cloud_detect.py` daemon-thread probe. The `NetworkAccountant` gains an `external_bytes_out` split (scalar + per-host) keyed off the `is_internal_traffic` value the HTTP adapter already computes in `byte_details`. At task finalize, `_aggregate_costs` resolves the rate once, sets `task.network_cost_usd` from the canonical scalar, stamps per-host `egress_cost_usd` into `network_by_host`, and back-fills the deferred `cost_usd` on every `network` event via `storage.update_event` (which now correctly re-marks `sync_status='pending'` — landed in the pre-requisite commit `ff96e94`).

**Tech Stack:** Python 3.10+, stdlib `decimal` / `threading` / `urllib.request` / `pathlib`, `sqlite3`, `pytest`. No new runtime dependencies.

**Run tests with:** `cd python && uv run pytest <path> -v`

**Pre-requisites already landed on this branch:**
- `ff96e94 fix(storage): re-mark sync_status='pending' on update_event` — `_aggregate_costs` depends on this for the per-event cost stamp to re-sync.
- `a326af3 docs(network-cost-v2): make dual-invoice attribution explicit` — Decision #7 + §3.3 invariants + §10.2 test.
- v1 already wires `classify_destination` into `_measure_bytes` (`http.py:597`) and stamps `is_internal_traffic` into every event's `details`. No prep work for that needed.

---

### Task 1: `Task.network_cost_usd` field

**Files:**
- Modify: `python/src/dexcost/models/task.py`
- Modify: `python/schemas/dexcost-task.v1.json`
- Test: `python/tests/test_task_network_cost_field.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_task_network_cost_field.py`:

```python
"""Task model carries network_cost_usd, parallel to the other *_cost_usd fields."""

from decimal import Decimal

from dexcost.models.task import Task


def test_network_cost_usd_defaults_to_zero():
    t = Task(task_type="x")
    assert t.network_cost_usd == Decimal("0")
    assert isinstance(t.network_cost_usd, Decimal)


def test_network_cost_usd_round_trip_through_dict():
    t = Task(task_type="x")
    t.network_cost_usd = Decimal("0.0042")
    d = t.to_dict()
    assert d["network_cost_usd"] == "0.0042"

    t2 = Task.from_dict(d)
    assert t2.network_cost_usd == Decimal("0.0042")


def test_from_dict_defaults_network_cost_usd_for_old_payloads():
    # Old payload without the v2 field — must default to Decimal("0").
    d = Task(task_type="x").to_dict()
    d.pop("network_cost_usd")
    t = Task.from_dict(d)
    assert t.network_cost_usd == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_task_network_cost_field.py -v`
Expected: FAIL — `network_cost_usd` does not exist.

- [ ] **Step 3: Add the field to the dataclass**

In `python/src/dexcost/models/task.py`, add after `compute_cost_usd: Decimal = Decimal("0")` and before `total_cost_usd`:

```python
    network_cost_usd: Decimal = Decimal("0")
```

- [ ] **Step 4: Wire `to_dict()` / `from_dict()`**

In `to_dict()`, add the line under `"compute_cost_usd": str(self.compute_cost_usd),`:

```python
            "network_cost_usd": str(self.network_cost_usd),
```

In `from_dict()`, add under `compute_cost_usd=Decimal(data["compute_cost_usd"]),`:

```python
                network_cost_usd=Decimal(data.get("network_cost_usd", "0")),
```

> `data.get(..., "0")` not `data["..."]` — old v4-era payloads (and old DB rows once they round-trip through `_row_to_task`) lack the key and must read back as `Decimal("0")`.

- [ ] **Step 5: Add to the JSON schema**

In `python/schemas/dexcost-task.v1.json`, add an entry to `properties` (do NOT add it to `required` — old payloads predate v2 and would fail validation):

```json
    "network_cost_usd": {
      "type": "string",
      "pattern": "^-?\\d+(\\.\\d+)?$",
      "description": "Aggregated cloud-egress cost in USD (Decimal as string). Distinct from external_cost_usd, which captures vendor API fees — see spec Decision #7."
    },
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_task_network_cost_field.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/src/dexcost/models/task.py python/schemas/dexcost-task.v1.json python/tests/test_task_network_cost_field.py
git commit -m "feat(network-cost-v2): add Task.network_cost_usd field"
```

---

### Task 2: SQLite migration v4 → v5

**Files:**
- Modify: `python/src/dexcost/storage/migrations.py`
- Modify: `python/src/dexcost/storage/sqlite.py`
- Test: `python/tests/test_network_cost_migration.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_cost_migration.py`:

```python
"""v4→v5 migration adds the network_cost_usd column; Decimal round-trip."""

import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage


def test_fresh_db_has_network_cost_usd_column(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "network_cost_usd" in cols
    assert st.get_schema_version() == 5
    st.close()


def test_network_cost_usd_round_trip_through_storage(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    t.network_cost_usd = Decimal("0.0042")
    st.insert_task(t)
    got = st.get_task(str(t.task_id))
    assert got.network_cost_usd == Decimal("0.0042")
    st.close()


def test_v4_db_migrates_to_v5(tmp_path):
    # Build a v4-shaped tasks table (no network_cost_usd), record version 4.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, "
        "sync_status TEXT NOT NULL DEFAULT 'pending', "
        "network_bytes_in INTEGER NOT NULL DEFAULT 0, "
        "network_bytes_out INTEGER NOT NULL DEFAULT 0, "
        "network_call_count INTEGER NOT NULL DEFAULT 0, "
        "network_by_host TEXT NOT NULL DEFAULT '{\"hosts\": []}')"
    )
    conn.execute(
        "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version_number INTEGER NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), migration_name TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version_number, migration_name) VALUES (4, 'seed')"
    )
    conn.commit()
    conn.close()

    st = SQLiteStorage(db_path=str(db))  # opening runs migrations
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "network_cost_usd" in cols
    # Re-applying the migration is a no-op (idempotent).
    st.close()
    st2 = SQLiteStorage(db_path=str(db))
    assert st2.get_schema_version() == 5
    st2.close()


def test_v4_task_reads_back_with_zero_network_cost(tmp_path):
    # Old v4 task (pre-migration) reads back as Decimal("0") — not None, not 0.0.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    # Minimal v4 schema with a single task row, then migrate.
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, "
        "metadata TEXT, llm_cost_usd TEXT NOT NULL DEFAULT '0', "
        "external_cost_usd TEXT NOT NULL DEFAULT '0', "
        "compute_cost_usd TEXT NOT NULL DEFAULT '0', "
        "total_cost_usd TEXT NOT NULL DEFAULT '0', "
        "total_input_tokens INTEGER NOT NULL DEFAULT 0, "
        "total_output_tokens INTEGER NOT NULL DEFAULT 0, "
        "total_cached_tokens INTEGER NOT NULL DEFAULT 0, "
        "retry_count INTEGER NOT NULL DEFAULT 0, "
        "retry_cost_usd TEXT NOT NULL DEFAULT '0', "
        "failure_count INTEGER NOT NULL DEFAULT 0, "
        "customer_id TEXT, project_id TEXT, parent_task_id TEXT, "
        "experiment_id TEXT, variant TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending', "
        "network_bytes_in INTEGER NOT NULL DEFAULT 0, "
        "network_bytes_out INTEGER NOT NULL DEFAULT 0, "
        "network_call_count INTEGER NOT NULL DEFAULT 0, "
        "network_by_host TEXT NOT NULL DEFAULT '{\"hosts\": []}')"
    )
    tid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, started_at) VALUES (?, ?, ?, ?)",
        (tid, "old", "success", datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version_number INTEGER NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), migration_name TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version_number, migration_name) VALUES (4, 'seed')"
    )
    # Also need the events table for SQLiteStorage to open cleanly.
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "event_type TEXT NOT NULL, timestamp TEXT NOT NULL, "
        "cost_usd TEXT NOT NULL DEFAULT '0', cost_confidence TEXT, "
        "pricing_source TEXT, pricing_version TEXT, service_name TEXT, "
        "provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
        "cached_tokens INTEGER, latency_ms INTEGER, is_retry INTEGER, "
        "retry_reason TEXT, retry_of TEXT, details TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending')"
    )
    conn.commit()
    conn.close()

    st = SQLiteStorage(db_path=str(db))
    got = st.get_task(tid)
    assert got is not None
    assert got.network_cost_usd == Decimal("0")
    assert isinstance(got.network_cost_usd, Decimal)
    st.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_network_cost_migration.py -v`
Expected: FAIL — column missing / schema version 4.

- [ ] **Step 3: Bump the target and register the migration**

In `python/src/dexcost/storage/migrations.py`, change line 20:

```python
TARGET_SCHEMA_VERSION = 5
```

At the end of the file, after `_sqlite_v3_to_v4`, add:

```python
@register_sqlite_migration(4, 5)
def _sqlite_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Add network_cost_usd column to tasks table (idempotent).

    Stores the per-task cloud-egress cost as Decimal-in-TEXT, consistent
    with the other *_cost_usd columns. Existing rows default to '0' so
    no data backfill is required.
    """
    existing = {
        row[1]
        for row in conn.execute(_TASKS_TABLE_INFO).fetchall()
    }
    if "network_cost_usd" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_cost_usd TEXT NOT NULL DEFAULT '0'"
        )
```

- [ ] **Step 4: Add the column to the fresh-create DDL**

In `python/src/dexcost/storage/sqlite.py`, in `_CREATE_TASKS`, add `network_cost_usd TEXT NOT NULL DEFAULT '0',` next to the other `*_cost_usd` columns (immediately after `compute_cost_usd TEXT,` or matching the existing order).

- [ ] **Step 5: Extend `insert_task`**

In `insert_task`, append `network_cost_usd` to the column list, add one `?` to `VALUES`, and add `str(task.network_cost_usd),` to the parameter tuple.

- [ ] **Step 6: Extend `update_task`**

In `update_task`'s `SET` clause, add `network_cost_usd=?` next to the other `*_cost_usd=?` columns, and append `str(task.network_cost_usd),` to the parameter tuple (before `str(task.task_id),`).

- [ ] **Step 7: Read the column in `_row_to_task`**

In `_row_to_task`, add to the `Task(...)` constructor, near the other `*_cost_usd` reads:

```python
            network_cost_usd=(
                Decimal(row["network_cost_usd"])
                if "network_cost_usd" in row.keys() and row["network_cost_usd"] is not None
                else Decimal("0")
            ),
```

The `if "network_cost_usd" in row.keys()` guard mirrors the existing v3→v4 fields and tolerates downgraded/legacy rows.

- [ ] **Step 8: Add to `_prepare_task_dict`**

`sync._prepare_task_dict` calls `task.to_dict()` which already includes the new field after Task 1. No change needed here — confirm by re-reading `sync.py:274-296`.

- [ ] **Step 9: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_network_cost_migration.py tests/test_sqlite_storage.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add python/src/dexcost/storage/migrations.py python/src/dexcost/storage/sqlite.py python/tests/test_network_cost_migration.py
git commit -m "feat(network-cost-v2): persist network_cost_usd + v4->v5 migration"
```

---

### Task 3: Bundled egress catalog `data/egress_prices.json`

**Files:**
- Create: `python/src/dexcost/data/egress_prices.json`
- Test: `python/tests/test_egress_catalog_integrity.py` (create)

This task is **the launch-prerequisite data-entry job** called out in spec §4.5. Every commercial AWS / GCP / Azure region must appear at launch. Catalog values are transcribed from each provider's public pricing page — first (highest) volume tier only, decimal-string encoded.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_egress_catalog_integrity.py`:

```python
"""Catalog integrity — structure, Decimal parsing, freshness."""

import datetime as _dt
import importlib.resources as ir
import json
import warnings
from decimal import Decimal


def _load():
    raw = ir.files("dexcost").joinpath("data").joinpath("egress_prices.json").read_text()
    return json.loads(raw), raw


def test_catalog_parses_as_json():
    data, _ = _load()
    assert "_meta" in data


def test_meta_has_required_keys():
    data, _ = _load()
    meta = data["_meta"]
    for k in ("version", "last_updated", "currency",
              "default_rate_usd_per_gb", "description", "notes"):
        assert k in meta, f"_meta missing {k}"
    assert meta["currency"] == "USD"
    # Universal default parses as Decimal cleanly.
    Decimal(meta["default_rate_usd_per_gb"])


def test_every_rate_is_decimal_parseable():
    data, _ = _load()
    for provider, block in data.items():
        if provider == "_meta":
            continue
        Decimal(block["default_usd_per_gb"])
        for region, rate in block["regions"].items():
            try:
                Decimal(rate)
            except Exception as e:  # noqa: BLE001
                raise AssertionError(f"{provider}.{region} not Decimal: {rate}") from e


def test_every_provider_has_last_verified():
    data, _ = _load()
    today = _dt.date.today()
    soft_limit = _dt.timedelta(days=180)
    for provider, block in data.items():
        if provider == "_meta":
            continue
        verified = _dt.date.fromisoformat(block["_last_verified"])
        if today - verified > soft_limit:
            warnings.warn(
                f"egress_prices.json: {provider} _last_verified is "
                f"{(today - verified).days} days old (soft limit 180)",
                stacklevel=2,
            )


def test_aws_gcp_azure_present():
    data, _ = _load()
    for p in ("aws", "gcp", "azure"):
        assert p in data, f"missing provider block: {p}"


def test_known_anchor_regions_have_rates():
    # Spot-check the anchor regions named in the spec.  If a regional
    # rate disappears the human transcribing the next refresh will see
    # this test fail loudly.
    data, _ = _load()
    assert data["aws"]["regions"].get("us-east-1") is not None
    assert data["gcp"]["regions"].get("us-central1") is not None
    assert data["azure"]["regions"].get("eastus") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_egress_catalog_integrity.py -v`
Expected: FAIL — file does not exist.

- [ ] **Step 3: Author `egress_prices.json`**

Create `python/src/dexcost/data/egress_prices.json`. Structure exactly as in spec §4.1. Skeleton:

```json
{
  "_meta": {
    "version": "1.0.0",
    "last_updated": "2026-05-20",
    "currency": "USD",
    "default_rate_usd_per_gb": "0.09",
    "description": "Dexcost egress catalog — per-GB internet data-transfer-out rates by cloud provider/region. Community-maintained; submit PRs to add or refresh rates.",
    "notes": "Rates are standard internet data-transfer-out, FIRST pricing tier only. Cloud egress is billed in descending monthly-volume tiers; the SDK has no monthly cumulative view, so it uses the first (highest) tier. Effect: customers exceeding ~10 TB/month of egress on a single cloud may see attributed cost up to ~45% above their actual invoice for their highest-volume tier; customers under the first tier (the majority) see no over-attribution. The universal default_rate_usd_per_gb is AWS us-east-1 first-tier ($0.09/GB) — the modal egress rate across hyperscalers; a deliberate conservative choice so undetected environments over-attribute slightly rather than undercount. Intra-region/internal traffic is free and never priced from this file."
  },
  "aws":   { "_last_verified": "2026-05-20", "default_usd_per_gb": "0.09",  "regions": { "us-east-1": "0.09",  "...": "..." } },
  "gcp":   { "_last_verified": "2026-05-20", "default_usd_per_gb": "0.12",  "regions": { "us-central1": "0.12", "...": "..." } },
  "azure": { "_last_verified": "2026-05-20", "default_usd_per_gb": "0.087", "regions": { "eastus": "0.087",     "...": "..." } }
}
```

> **Data entry instructions:** Open each provider's public egress-pricing page, transcribe **every commercial region** into the `regions` map, decimal-string-encoded. Human-review each provider block end-to-end before saving. Sources:
> - AWS: https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer_within_the_same_AWS_Region (Data Transfer OUT From Amazon EC2 To Internet).
> - GCP: https://cloud.google.com/vpc/network-pricing#general (Internet egress, standard tier).
> - Azure: https://azure.microsoft.com/en-us/pricing/details/bandwidth/ (Internet egress, Zone 1 pricing).

- [ ] **Step 4: Confirm bundling**

The `data/` directory is already shipped (see `data/model_cost_map.json` and `data/service_prices.json`). Verify no `MANIFEST.in` or `pyproject.toml` change is required by running:

```bash
cd python && uv run python -c "import importlib.resources as ir; print(ir.files('dexcost').joinpath('data').joinpath('egress_prices.json').is_file())"
```

Expected: `True`. If `False`, add a `tool.hatch.build.targets.wheel.force-include` entry for `data/egress_prices.json` in `pyproject.toml`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_egress_catalog_integrity.py -v`
Expected: PASS (the 180-day freshness check is a `warnings.warn`, never a failure).

- [ ] **Step 6: Commit**

```bash
git add python/src/dexcost/data/egress_prices.json python/tests/test_egress_catalog_integrity.py
git commit -m "feat(network-cost-v2): bundle egress price catalog (AWS/GCP/Azure)"
```

---

### Task 4: `egress_pricing.py` — rate resolver + degradation ladder

**Files:**
- Create: `python/src/dexcost/egress_pricing.py`
- Test: `python/tests/test_egress_pricing.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_egress_pricing.py`:

```python
"""Egress pricing resolver — every tier of the §7.1 ladder."""

import json
from decimal import Decimal

import pytest

from dexcost.egress_pricing import EgressPricingEngine, EgressRate


@pytest.fixture
def engine():
    return EgressPricingEngine()


def test_tier1_region_match_is_computed(engine):
    r = engine.resolve_rate("aws", "us-east-1")
    assert r.rate_per_gb == Decimal("0.09")
    assert r.pricing_source == "egress_catalog:aws:us-east-1"
    assert r.cost_confidence == "computed"


def test_tier2_provider_known_region_missing_is_estimated(engine):
    r = engine.resolve_rate("aws", "moon-base-1")
    assert r.rate_per_gb == Decimal(engine._catalog["aws"]["default_usd_per_gb"])
    assert r.pricing_source == "egress_catalog:aws:default"
    assert r.cost_confidence == "estimated"


def test_tier3_unknown_provider_falls_to_meta_default(engine):
    r = engine.resolve_rate(None, None)
    assert r.rate_per_gb == Decimal(engine._catalog["_meta"]["default_rate_usd_per_gb"])
    assert r.pricing_source == "egress_catalog:default"
    assert r.cost_confidence == "estimated"


def test_internal_traffic_is_free_and_exact(engine):
    r = engine.rate_for_internal()
    assert r.rate_per_gb == Decimal("0")
    assert r.pricing_source == "egress_catalog:internal"
    assert r.cost_confidence == "exact"


def test_tier4_missing_catalog_falls_to_hardcoded(tmp_path):
    bogus = tmp_path / "no.json"
    eng = EgressPricingEngine(catalog_path=bogus)
    r = eng.resolve_rate("aws", "us-east-1")
    assert r.rate_per_gb == Decimal("0.09")
    assert r.cost_confidence == "estimated"


def test_tier4_malformed_catalog_falls_to_hardcoded(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    eng = EgressPricingEngine(catalog_path=bad)
    r = eng.resolve_rate("aws", "us-east-1")
    assert r.rate_per_gb == Decimal("0.09")
    assert r.cost_confidence == "estimated"


def test_tier4_meta_default_missing_falls_to_hardcoded(tmp_path):
    bad = tmp_path / "no_meta_default.json"
    bad.write_text(json.dumps({"_meta": {"version": "x", "currency": "USD"}}))
    eng = EgressPricingEngine(catalog_path=bad)
    r = eng.resolve_rate(None, None)
    assert r.rate_per_gb == Decimal("0.09")
    assert r.cost_confidence == "estimated"


def test_warn_once_per_failure_mode(tmp_path, caplog):
    from dexcost.egress_pricing import _reset_warning_state
    _reset_warning_state()

    bogus = tmp_path / "missing.json"
    EgressPricingEngine(catalog_path=bogus)
    EgressPricingEngine(catalog_path=bogus)  # second open — should NOT re-warn
    catalog_missing_logs = [
        rec for rec in caplog.records
        if "egress catalog" in rec.getMessage().lower()
    ]
    assert len(catalog_missing_logs) == 1


def test_warn_distinct_modes_independently(tmp_path, caplog):
    from dexcost.egress_pricing import _reset_warning_state
    _reset_warning_state()

    missing = tmp_path / "missing.json"
    malformed = tmp_path / "bad.json"
    malformed.write_text("{")
    EgressPricingEngine(catalog_path=missing)
    EgressPricingEngine(catalog_path=malformed)
    msgs = [rec.getMessage().lower() for rec in caplog.records]
    assert any("missing" in m or "not found" in m for m in msgs)
    assert any("malformed" in m or "parse" in m for m in msgs)


def test_decimal_no_float_drift():
    # Pin the spec §6.3 invariant: never coerce through float.
    assert Decimal("0.1093") * Decimal("1000000000") == Decimal("109300000.0000")
    # Multiplication step against a hand-computed expected value.
    assert Decimal("0.087") * Decimal("12345678") == Decimal("1074073.986")


def test_pricing_version_from_meta(engine):
    assert engine.catalog_version.startswith("1.")


def test_egress_rate_is_immutable(engine):
    r = engine.resolve_rate("aws", "us-east-1")
    with pytest.raises(Exception):  # frozen dataclass — any attempt errors
        r.rate_per_gb = Decimal("99")  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_egress_pricing.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `egress_pricing.py`**

Create `python/src/dexcost/egress_pricing.py`:

```python
"""Egress pricing engine — resolves a per-GB egress rate from
``(provider, region)`` using the bundled ``data/egress_prices.json`` catalog.
Mirrors :mod:`dexcost.pricing` in shape: bundled JSON + a resolver returning
``(rate, pricing_source, cost_confidence)``.

Fail-silent contract: every failure mode degrades through the spec §7.1
ladder; the engine always returns a usable :class:`EgressRate`.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Tier-4 ultimate fallback — used only when the catalog cannot be read at all
# AND _meta.default_rate_usd_per_gb cannot be resolved.  Matches the spec §7.1
# hardcoded constant.
_HARDCODED_DEFAULT = Decimal("0.09")

# Module-level set of warning-mode tokens already logged in this process.
# Reset via _reset_warning_state() in tests.
_warned_modes: set[str] = set()
_warn_lock = threading.Lock()


def _reset_warning_state() -> None:
    """Test-only: clear the warn-once tracking set."""
    with _warn_lock:
        _warned_modes.clear()


def _warn_once(mode: str, message: str) -> None:
    with _warn_lock:
        if mode in _warned_modes:
            return
        _warned_modes.add(mode)
    _log.warning(message)


@dataclass(frozen=True)
class EgressRate:
    """The result of an egress-rate lookup."""

    rate_per_gb: Decimal
    pricing_source: str
    cost_confidence: str  # exact | computed | estimated


class EgressPricingEngine:
    """Resolve egress rates from the bundled catalog.

    Args:
        catalog_path: Optional override path. ``None`` uses the bundled
            ``data/egress_prices.json``.
    """

    def __init__(self, catalog_path: str | Path | None = None) -> None:
        self._catalog: dict[str, Any] = {}
        self._catalog_path = catalog_path
        self._catalog_version: str = "unknown"
        self._load()

    def _load(self) -> None:
        try:
            if self._catalog_path is not None:
                raw = Path(self._catalog_path).read_text(encoding="utf-8")
            else:
                raw = (
                    resources.files("dexcost")
                    .joinpath("data")
                    .joinpath("egress_prices.json")
                    .read_text(encoding="utf-8")
                )
        except FileNotFoundError:
            _warn_once(
                "catalog_missing",
                "egress catalog file not found; falling back to hardcoded default",
            )
            return
        except OSError as exc:
            _warn_once(
                "catalog_unreadable",
                f"egress catalog unreadable ({exc}); falling back to hardcoded default",
            )
            return

        try:
            self._catalog = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn_once(
                "catalog_malformed",
                f"egress catalog malformed JSON ({exc}); falling back to hardcoded default",
            )
            self._catalog = {}
            return

        meta = self._catalog.get("_meta", {})
        self._catalog_version = str(meta.get("version", "unknown"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    def rate_for_internal(self) -> EgressRate:
        """Rate for a call classified as internal traffic — always free."""
        return EgressRate(
            rate_per_gb=Decimal("0"),
            pricing_source="egress_catalog:internal",
            cost_confidence="exact",
        )

    def resolve_rate(self, provider: str | None, region: str | None) -> EgressRate:
        """Resolve an egress rate via the §7.1 degradation ladder.

        Tier 1: ``(provider, region)`` exact match → region rate, ``computed``.
        Tier 2: provider known, region absent/unknown → provider default,
            ``estimated``.
        Tier 3: provider not detected / not in catalog → ``_meta`` default,
            ``estimated``.
        Tier 4: catalog unreadable or ``_meta`` default absent → hardcoded
            ``Decimal("0.09")``, ``estimated``.
        """
        # Tier 1 / Tier 2
        if provider:
            block = self._catalog.get(provider)
            if isinstance(block, dict):
                regions = block.get("regions", {})
                if region and region in regions:
                    try:
                        rate = Decimal(str(regions[region]))
                    except InvalidOperation:
                        _warn_once(
                            f"region_rate_malformed:{provider}:{region}",
                            f"egress region rate malformed for {provider}/{region}",
                        )
                    else:
                        return EgressRate(
                            rate_per_gb=rate,
                            pricing_source=f"egress_catalog:{provider}:{region}",
                            cost_confidence="computed",
                        )
                # Provider known, no region or region unknown → provider default.
                try:
                    rate = Decimal(str(block.get("default_usd_per_gb", "")))
                except (InvalidOperation, TypeError):
                    rate = None  # type: ignore[assignment]
                if rate is not None:
                    return EgressRate(
                        rate_per_gb=rate,
                        pricing_source=f"egress_catalog:{provider}:default",
                        cost_confidence="estimated",
                    )

        # Tier 3 — universal _meta default.
        meta = self._catalog.get("_meta") if self._catalog else None
        if isinstance(meta, dict):
            try:
                rate = Decimal(str(meta.get("default_rate_usd_per_gb", "")))
            except (InvalidOperation, TypeError):
                rate = None  # type: ignore[assignment]
            if rate is not None:
                return EgressRate(
                    rate_per_gb=rate,
                    pricing_source="egress_catalog:default",
                    cost_confidence="estimated",
                )
            _warn_once(
                "meta_default_missing",
                "egress _meta.default_rate_usd_per_gb missing/malformed; "
                "using hardcoded default",
            )

        # Tier 4 — hardcoded last resort.
        return EgressRate(
            rate_per_gb=_HARDCODED_DEFAULT,
            pricing_source="egress_catalog:default",
            cost_confidence="estimated",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_egress_pricing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/egress_pricing.py python/tests/test_egress_pricing.py
git commit -m "feat(network-cost-v2): egress rate resolver + degradation ladder"
```

---

### Task 5: `cloud_detect.py` — non-blocking provider/region detection

**Files:**
- Create: `python/src/dexcost/cloud_detect.py`
- Test: `python/tests/test_cloud_detect.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_cloud_detect.py`:

```python
"""cloud_detect — env / DMI / IMDS phases; init never blocks."""

import time
from unittest.mock import patch

import pytest

from dexcost.cloud_detect import CloudEnv, detect_now, start_background_detection


def test_aws_lambda_env_resolves_fully(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    env = detect_now()
    assert env.provider == "aws"
    assert env.region == "us-east-1"
    assert env.source == "env"


def test_azure_app_service_provider_no_region(monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", "x")
    monkeypatch.delenv("REGION_NAME", raising=False)
    env = detect_now()
    assert env.provider == "azure"
    assert env.region is None
    assert env.source == "env"


def test_gcp_cloud_run_provider_no_region(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "my-svc")
    env = detect_now()
    assert env.provider == "gcp"
    assert env.region is None
    assert env.source == "env"


def test_no_env_no_dmi_returns_undetected(monkeypatch, tmp_path):
    # Strip the typical env vars; point DMI path at a non-existent file.
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV", "AWS_REGION",
              "AWS_DEFAULT_REGION", "WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME",
              "CONTAINER_APP_NAME", "REGION_NAME", "K_SERVICE", "GAE_ENV",
              "FUNCTION_TARGET"):
        monkeypatch.delenv(v, raising=False)
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(tmp_path / "nope"),)):
        env = detect_now()
    assert env.provider is None
    assert env.region is None
    assert env.source == "none"


def test_dmi_amazon_resolves_provider(tmp_path, monkeypatch):
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV",
              "WEBSITE_SITE_NAME", "K_SERVICE"):
        monkeypatch.delenv(v, raising=False)
    dmi = tmp_path / "board_vendor"
    dmi.write_text("Amazon EC2\n")
    with patch("dexcost.cloud_detect._DMI_PATHS", (str(dmi),)):
        env = detect_now()
    assert env.provider == "aws"
    assert env.source == "dmi"


def test_gcp_zone_to_region_strips_trailing_letter():
    from dexcost.cloud_detect import _gcp_zone_to_region
    assert _gcp_zone_to_region("projects/123/zones/us-central1-a") == "us-central1"
    assert _gcp_zone_to_region("us-central1-a") == "us-central1"
    assert _gcp_zone_to_region("") is None


def test_init_never_blocks_when_metadata_unreachable(monkeypatch):
    # No env/DMI signals → start_background_detection schedules a probe.
    # Patch the per-request HTTP probe to a tight 50 ms ceiling.
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV",
              "WEBSITE_SITE_NAME", "K_SERVICE"):
        monkeypatch.delenv(v, raising=False)
    with patch("dexcost.cloud_detect._DMI_PATHS", ()):
        t0 = time.perf_counter()
        start_background_detection()  # returns immediately
        elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"init took {elapsed:.3f}s, expected < 50 ms"


def test_track_network_false_skips_probe(monkeypatch):
    from dexcost.cloud_detect import start_background_detection, get_cloud_env
    # Reset module state.
    import dexcost.cloud_detect as cd
    cd._result = CloudEnv(None, None, "none")
    start_background_detection(track_network=False)
    # No probe means the result must still be the seed value.
    assert get_cloud_env().source == "none"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cloud_detect.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `cloud_detect.py`**

Create `python/src/dexcost/cloud_detect.py`:

```python
"""Cloud-environment detection for egress pricing.

Phase 1a — env-var detection (sub-millisecond, synchronous).
Phase 1b — DMI vendor check (~1 ms, Linux-only).
Phase 2  — background metadata probe (daemon thread, ~250 ms budget,
           never blocks ``dexcost.init()``).

See spec §5 for the resolution rules per provider.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

_log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 0.25  # seconds — bounds Phase 2 wall time
_DMI_PATHS: tuple[str, ...] = (
    "/sys/class/dmi/id/board_vendor",
    "/sys/class/dmi/id/sys_vendor",
)


@dataclass(frozen=True)
class CloudEnv:
    """Detected cloud environment.

    ``source`` is the audit trail: ``"env" | "dmi" | "imds" | "none"``.
    """

    provider: str | None
    region: str | None
    source: str


# Module-global result, lock-guarded. Written once at detection completion.
_lock = threading.Lock()
_result: CloudEnv = CloudEnv(None, None, "none")
_thread: threading.Thread | None = None


def get_cloud_env() -> CloudEnv:
    """Return the most recently resolved CloudEnv (may be ``source='none'``)."""
    with _lock:
        return _result


def _set_result(env: CloudEnv) -> None:
    global _result
    with _lock:
        _result = env


# ---------------------------------------------------------------------------
# Phase 1a — environment variable detection
# ---------------------------------------------------------------------------


def _detect_env() -> CloudEnv | None:
    # AWS
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("AWS_EXECUTION_ENV"):
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        return CloudEnv("aws", region, "env")
    # Azure App Service / Functions / Container Apps
    if any(os.environ.get(k) for k in
           ("WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME", "CONTAINER_APP_NAME")):
        region = os.environ.get("REGION_NAME") or None
        return CloudEnv("azure", region, "env")
    # GCP
    if any(os.environ.get(k) for k in ("K_SERVICE", "GAE_ENV", "FUNCTION_TARGET")):
        return CloudEnv("gcp", None, "env")
    # Bare AWS_REGION on its own (EC2 with explicit setting) — provider unknown,
    # do not assume aws; fall through to DMI/IMDS.
    return None


# ---------------------------------------------------------------------------
# Phase 1b — DMI check
# ---------------------------------------------------------------------------


def _detect_dmi() -> CloudEnv | None:
    for path in _DMI_PATHS:
        try:
            with open(path, encoding="utf-8") as f:
                value = f.read().strip().lower()
        except OSError:
            continue
        if "amazon" in value:
            return CloudEnv("aws", None, "dmi")
        if "google" in value:
            return CloudEnv("gcp", None, "dmi")
        if "microsoft" in value:
            return CloudEnv("azure", None, "dmi")
    return None


# ---------------------------------------------------------------------------
# Phase 2 — metadata probes
# ---------------------------------------------------------------------------


def _gcp_zone_to_region(zone: str) -> str | None:
    if not zone:
        return None
    last = zone.rsplit("/", 1)[-1]  # e.g. "us-central1-a"
    if "-" not in last:
        return None
    return last.rsplit("-", 1)[0]


def _probe_aws() -> CloudEnv | None:
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            token = resp.read().decode("ascii")
        req2 = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/placement/region",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(req2, timeout=_PROBE_TIMEOUT) as resp:
            region = resp.read().decode("ascii").strip()
        return CloudEnv("aws", region or None, "imds")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _probe_gcp() -> CloudEnv | None:
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/zone",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            zone = resp.read().decode("ascii").strip()
        return CloudEnv("gcp", _gcp_zone_to_region(zone), "imds")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _probe_azure() -> CloudEnv | None:
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
            headers={"Metadata": "true"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        region = payload.get("compute", {}).get("location") or None
        return CloudEnv("azure", region, "imds")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


_PROBES = {"aws": _probe_aws, "gcp": _probe_gcp, "azure": _probe_azure}


def _run_probe(provider_hint: str | None) -> CloudEnv:
    """Run Phase 2 probes; return the first success, or "none"."""
    if provider_hint and provider_hint in _PROBES:
        env = _PROBES[provider_hint]()
        return env or CloudEnv(provider_hint, None, "imds")

    # Unknown provider — run all three in parallel threads, first wins.
    results: list[CloudEnv] = []
    done = threading.Event()
    lock = threading.Lock()

    def _runner(fn):  # type: ignore[no-untyped-def]
        env = fn()
        if env is not None:
            with lock:
                if not results:
                    results.append(env)
                    done.set()

    threads = [
        threading.Thread(target=_runner, args=(fn,), daemon=True)
        for fn in _PROBES.values()
    ]
    for t in threads:
        t.start()
    done.wait(timeout=_PROBE_TIMEOUT + 0.05)
    if results:
        return results[0]
    return CloudEnv(None, None, "none")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def detect_now() -> CloudEnv:
    """Run Phase 1a + 1b synchronously. Used by tests; never calls IMDS."""
    env = _detect_env()
    if env is not None and env.provider is not None and env.region is not None:
        return env
    env_dmi = _detect_dmi()
    if env is None:
        env = env_dmi
    elif env_dmi is not None and env.region is None:
        # Phase 1a got provider only → DMI confirms; keep env source for audit.
        pass
    return env if env is not None else CloudEnv(None, None, "none")


def start_background_detection(track_network: bool = True) -> None:
    """Resolve provider/region without blocking. Idempotent.

    When ``track_network`` is False, no probe is launched — the SDK will not
    compute egress cost, so detection is unnecessary.
    """
    global _thread
    if not track_network:
        _set_result(CloudEnv(None, None, "none"))
        return

    initial = detect_now()
    _set_result(initial)
    # If env+DMI already fully resolved → no Phase 2 needed.
    if initial.provider is not None and initial.region is not None:
        return

    def _background() -> None:
        try:
            env = _run_probe(initial.provider)
            if env.provider is not None:
                # Preserve the more reliable env-source region if Phase 2 only got provider.
                if initial.region and not env.region:
                    env = CloudEnv(env.provider, initial.region, env.source)
                _set_result(env)
        except Exception:  # noqa: BLE001 — fail-silent
            _log.warning("cloud_detect background probe failed", exc_info=True)

    if _thread is not None and _thread.is_alive():
        return  # already running
    _thread = threading.Thread(
        target=_background, daemon=True, name="dexcost-cloud-detect"
    )
    _thread.start()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cloud_detect.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/cloud_detect.py python/tests/test_cloud_detect.py
git commit -m "feat(network-cost-v2): non-blocking cloud provider/region detection"
```

---

### Task 6: `NetworkAccountant` — external-byte split

**Files:**
- Modify: `python/src/dexcost/network_accountant.py`
- Test: `python/tests/test_network_accountant_external.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_accountant_external.py`:

```python
"""NetworkAccountant — external_bytes_out scalar + per-host split."""

from dexcost.network_accountant import NetworkAccountant


def test_internal_call_does_not_contribute_to_external():
    a = NetworkAccountant()
    a.record("10.0.0.5", bytes_in=100, bytes_out=200, is_internal=True)
    snap = a.finalize()
    assert snap["external_bytes_out"] == 0
    host = snap["by_host"]["hosts"][0]
    assert host["external_bytes_out"] == 0
    assert host["bytes_out"] == 200  # raw measurement still recorded


def test_public_call_contributes_to_external():
    a = NetworkAccountant()
    a.record("api.example.com", bytes_in=100, bytes_out=500, is_internal=False)
    snap = a.finalize()
    assert snap["external_bytes_out"] == 500


def test_null_is_internal_is_treated_as_external():
    """is_internal=None (unresolved named host) is conservatively external."""
    a = NetworkAccountant()
    a.record("api.example.com", bytes_in=100, bytes_out=500, is_internal=None)
    snap = a.finalize()
    assert snap["external_bytes_out"] == 500


def test_scalar_equals_sum_of_per_host_external():
    a = NetworkAccountant()
    a.record("a.com", 0, 100, is_internal=False)
    a.record("b.com", 0, 200, is_internal=False)
    a.record("10.0.0.1", 0, 999, is_internal=True)
    snap = a.finalize()
    by_host_sum = sum(h["external_bytes_out"] for h in snap["by_host"]["hosts"])
    assert by_host_sum == snap["external_bytes_out"] == 300


def test_other_bucket_carries_external_bytes():
    a = NetworkAccountant()
    # Force the LIVE_CAP overflow into _other.
    from dexcost.network_accountant import LIVE_CAP
    for i in range(LIVE_CAP):
        a.record(f"host{i}.com", 0, 1, is_internal=False)
    a.record("overflow.com", 0, 555, is_internal=False)
    snap = a.finalize()
    other = next(h for h in snap["by_host"]["hosts"] if h["host"] == "_other")
    assert other["external_bytes_out"] == 555


def test_default_is_internal_is_none():
    """record() with no is_internal kwarg behaves as is_internal=None."""
    a = NetworkAccountant()
    a.record("api.example.com", bytes_in=0, bytes_out=100)
    snap = a.finalize()
    assert snap["external_bytes_out"] == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_network_accountant_external.py -v`
Expected: FAIL — `record()` does not accept `is_internal`.

- [ ] **Step 3: Extend `record()` and tracked state**

In `python/src/dexcost/network_accountant.py`:

(a) In `__init__`, add a scalar:

```python
        self._external_bytes_out = 0
        # host entries become [calls, bytes_in, bytes_out, external_bytes_out]
        self._other = [0, 0, 0, 0]
```

(b) Change `record()` signature and accounting:

```python
    def record(
        self, host: str, bytes_in: int, bytes_out: int,
        is_internal: bool | None = None,
    ) -> None:
        """Add one HTTP call's bytes. No-op once finalized.

        ``is_internal`` follows spec §4.2's three-valued classification:
        - ``True``  → bytes are intra-VPC / loopback → 0 external bytes.
        - ``False`` → confirmed public IP → all of ``bytes_out`` are external.
        - ``None``  → unresolved named host → treated as external
                      (conservative — over-attribute rather than undercount).
        """
        bytes_in = max(0, bytes_in)
        bytes_out = max(0, bytes_out)
        external_out = 0 if is_internal is True else bytes_out
        with self._lock:
            if self._frozen:
                return
            self._bytes_in += bytes_in
            self._bytes_out += bytes_out
            self._external_bytes_out += external_out
            self._call_count += 1
            key = host or "_unknown"
            entry = self._hosts.get(key)
            if entry is not None:
                entry[0] += 1
                entry[1] += bytes_in
                entry[2] += bytes_out
                entry[3] += external_out
            elif len(self._hosts) < LIVE_CAP:
                self._hosts[key] = [1, bytes_in, bytes_out, external_out]
            else:
                self._other[0] += 1
                self._other[1] += bytes_in
                self._other[2] += bytes_out
                self._other[3] += external_out
```

(c) Update `finalize()` so the entry / overflow / `_other` produce
`external_bytes_out` on every dict and `"external_bytes_out"` is exposed at
the top level. Replace the body with:

```python
        with self._lock:
            self._frozen = True
            ranked = sorted(
                self._hosts.items(),
                key=lambda kv: kv[1][1] + kv[1][2],
                reverse=True,
            )
            top = ranked[:FINALIZE_CAP]
            overflow = ranked[FINALIZE_CAP:]

            other = list(self._other)  # [calls, bytes_in, bytes_out, external_out]
            for _host, vals in overflow:
                for i in range(4):
                    other[i] += vals[i]

            top_clean = []
            for item in top:
                if item[0] == "_other":
                    for i in range(4):
                        other[i] += item[1][i]
                else:
                    top_clean.append(item)

            hosts: list[dict[str, Any]] = [
                {
                    "host": host,
                    "calls": vals[0],
                    "bytes_in": vals[1],
                    "bytes_out": vals[2],
                    "external_bytes_out": vals[3],
                }
                for host, vals in top_clean
            ]
            if other[0] > 0:
                hosts.append(
                    {
                        "host": "_other",
                        "calls": other[0],
                        "bytes_in": other[1],
                        "bytes_out": other[2],
                        "external_bytes_out": other[3],
                    }
                )
            return {
                "bytes_in": self._bytes_in,
                "bytes_out": self._bytes_out,
                "external_bytes_out": self._external_bytes_out,
                "call_count": self._call_count,
                "by_host": {"hosts": hosts},
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_network_accountant_external.py tests/test_network_accountant.py -v`
Expected: PASS.

> The existing `test_network_accountant.py` still asserts the v1 keys — those keys remain present, plus `external_bytes_out` is added. If any v1 test failed because it asserted exact dict equality for a host entry, update that assertion to include `"external_bytes_out": 0` (when no `is_internal=False` was passed) — old tests called `record(host, bytes_in, bytes_out)` without the kwarg, so under the new "default is None → external" rule their external bytes equal `bytes_out`.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/network_accountant.py python/tests/test_network_accountant.py python/tests/test_network_accountant_external.py
git commit -m "feat(network-cost-v2): NetworkAccountant external-byte split"
```

---

### Task 7: HTTP adapter — thread `is_internal_traffic` into all three `record()` call sites

**Files:**
- Modify: `python/src/dexcost/adapters/http.py`
- Test: `python/tests/test_http_external_byte_attribution.py` (create)

`is_internal_traffic` is already computed inside `_measure_bytes` and put into `byte_details` (`http.py:597`). v2's only adapter-side change is to **pass it into `task._network.record(...)` from all three call sites** so the accountant can do the split.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_http_external_byte_attribution.py`:

```python
"""All three adapter paths must forward is_internal into the accountant."""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

from dexcost.adapters import http as adapter
from dexcost.adapters.http import (
    _handle_catalog_entry, _handle_domain_rate, _handle_uncataloged,
    register_domain_rate, clear_domain_rates,
)
from dexcost.config import DexcostConfig
from dexcost.context import set_current_task
from dexcost.models.task import Task


def _fake_byte_details(is_internal):
    return {
        "protocol": "https",
        "request_bytes": 10,
        "response_bytes": 100,
        "is_internal_traffic": is_internal,
    }


def test_domain_rate_path_records_is_internal_false():
    clear_domain_rates()
    register_domain_rate("api.example.com", cost_usd="0.01")
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    try:
        ok = _handle_domain_rate(
            "https://api.example.com/x", "api.example.com",
            track_network=True, bytes_in=100, bytes_out=200,
            byte_details=_fake_byte_details(False),
        )
        assert ok is True
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 200
    finally:
        from dexcost.context import _current_task
        _current_task.reset(token)
        clear_domain_rates()


def test_catalog_path_records_is_internal_false(monkeypatch):
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)

    fake_catalog = MagicMock()
    fake_entry = MagicMock()
    fake_catalog.lookup.return_value = fake_entry
    fake_catalog.extract_cost.return_value = None
    fake_catalog.catalog_version = "v"
    fake_entry.display_name = "openai"
    monkeypatch.setattr(adapter, "get_catalog", lambda: fake_catalog)

    try:
        ok = _handle_catalog_entry(
            "https://api.openai.com/x", "api.openai.com",
            track_network=True, bytes_in=100, bytes_out=200,
            response_headers={}, response=None,
            byte_details=_fake_byte_details(False),
        )
        assert ok is True
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 200
    finally:
        from dexcost.context import _current_task
        _current_task.reset(token)


def test_uncataloged_internal_call_records_zero_external():
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    cfg = DexcostConfig(storage="local")
    try:
        _handle_uncataloged(
            "http://10.0.0.5/x", "GET", "10.0.0.5",
            bytes_in=100, bytes_out=200, status_code=200, latency_ms=10,
            byte_details=_fake_byte_details(True), cfg=cfg,
        )
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 0
        assert snap["bytes_out"] == 200
    finally:
        from dexcost.context import _current_task
        _current_task.reset(token)


def test_uncataloged_external_call_records_full_external():
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    cfg = DexcostConfig(storage="local")
    try:
        _handle_uncataloged(
            "https://api.example.com/x", "GET", "api.example.com",
            bytes_in=100, bytes_out=200, status_code=200, latency_ms=10,
            byte_details=_fake_byte_details(None), cfg=cfg,
        )
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 200
    finally:
        from dexcost.context import _current_task
        _current_task.reset(token)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_http_external_byte_attribution.py -v`
Expected: FAIL — `record()` is called without `is_internal`.

- [ ] **Step 3: Forward `is_internal` from `byte_details` into `record()` (three sites)**

In `python/src/dexcost/adapters/http.py`:

(a) In `_handle_domain_rate`, replace:

```python
    if track_network:
        task._network.record(domain, bytes_in=bytes_in, bytes_out=bytes_out)
```

with:

```python
    if track_network:
        task._network.record(
            domain, bytes_in=bytes_in, bytes_out=bytes_out,
            is_internal=byte_details.get("is_internal_traffic"),
        )
```

(b) Same edit in `_handle_catalog_entry`.

(c) In `_handle_uncataloged`, replace the existing `task._network.record(...)` call the same way — using `byte_details.get("is_internal_traffic")`.

> All three handlers already receive `byte_details` as a parameter; no signature changes are needed.

- [ ] **Step 4: Emit `network` events with the deferred-cost marker**

Spec §6.4: at emission, `network` events ship `cost_usd=0` with `details["cost_pending"] = True` so the finalize step (Task 8) can identify and update them. In `_handle_uncataloged`, change the event construction:

```python
    event = Event(
        task_id=task.task_id, event_type="network",
        cost_usd=Decimal("0"), cost_confidence="unknown",
        pricing_source=None, service_name=domain,
        details={
            "url": url, "method": method, "status_code": status_code,
            "cost_pending": True,
            **byte_details,
        },
    )
```

> `cost_pending` lives inside `details` which already accepts arbitrary keys (`dexcost-event.v1.json` constrains the event's top level but leaves `details` open). No schema change required.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_http_external_byte_attribution.py tests/test_http_adapter.py tests/test_network_capture.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/src/dexcost/adapters/http.py python/tests/test_http_external_byte_attribution.py
git commit -m "feat(network-cost-v2): forward is_internal_traffic into accountant + deferred-cost marker"
```

---

### Task 8: Task finalize — resolve rate, compute `network_cost_usd`, stamp events

**Files:**
- Modify: `python/src/dexcost/tracker.py`
- Test: `python/tests/test_network_cost_finalize.py` (create)

This is the core of v2 — `_aggregate_costs` reads the resolved `CloudEnv`, resolves a rate once per task, sets `task.network_cost_usd` from the **scalar** `external_bytes_out` (not by summing events), populates each `network_by_host[].egress_cost_usd`, and back-fills every `network` event with the resolved cost via `storage.update_event` (which now correctly re-marks `sync_status='pending'`).

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_cost_finalize.py`:

```python
"""_aggregate_costs computes network_cost_usd from the canonical scalar."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dexcost import cloud_detect
from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv("aws", "us-east-1", "env")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage)


def _make_task(storage, external_bytes_out):
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    storage.insert_task(t)
    t._network.record(
        "api.example.com", bytes_in=0, bytes_out=external_bytes_out,
        is_internal=False,
    )
    return t


def test_network_cost_usd_from_canonical_scalar(tracker):
    # 1 GB external out * $0.09/GB = $0.09
    t = _make_task(tracker._storage, 1_000_000_000)
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0.09")


def test_total_cost_usd_includes_network(tracker):
    t = _make_task(tracker._storage, 1_000_000_000)
    tracker._storage.insert_event(Event(
        task_id=t.task_id, event_type="llm_call",
        cost_usd=Decimal("0.10"), cost_confidence="computed",
    ))
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0.09")
    assert t.llm_cost_usd == Decimal("0.10")
    assert t.total_cost_usd == Decimal("0.19")


def test_per_host_egress_cost_in_by_host(tracker):
    t = _make_task(tracker._storage, 500_000_000)
    tracker._aggregate_costs(t)
    host = t.network_by_host["hosts"][0]
    assert host["host"] == "api.example.com"
    assert "egress_cost_usd" in host
    assert Decimal(host["egress_cost_usd"]) == Decimal("0.045")


def test_internal_host_has_zero_egress_cost(tracker):
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    t._network.record("10.0.0.5", bytes_in=0, bytes_out=999_999_999,
                       is_internal=True)
    tracker._aggregate_costs(t)
    host = t.network_by_host["hosts"][0]
    assert Decimal(host["egress_cost_usd"]) == Decimal("0")
    assert t.network_cost_usd == Decimal("0")


def test_network_event_cost_stamped_at_finalize(tracker):
    t = _make_task(tracker._storage, 1_000_000_000)
    ev = Event(
        task_id=t.task_id, event_type="network",
        cost_usd=Decimal("0"), cost_confidence="unknown",
        service_name="api.example.com",
        details={"cost_pending": True, "url": "x",
                 "request_bytes": 0, "response_bytes": 1_000_000_000,
                 "is_internal_traffic": False},
    )
    tracker._storage.insert_event(ev)
    tracker._aggregate_costs(t)

    refreshed = tracker._storage.query_events(task_id=str(t.task_id))[0]
    assert refreshed.cost_usd == Decimal("0.09")
    assert refreshed.cost_confidence == "computed"
    assert refreshed.pricing_source == "egress_catalog:aws:us-east-1"
    assert refreshed.pricing_version is not None
    assert refreshed.pricing_version.startswith("egress:")
    assert "cost_pending" not in refreshed.details


def test_below_threshold_uncataloged_bytes_still_priced(tracker):
    # A small call (no network event emitted) still contributes to network_cost_usd.
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    t._network.record("api.example.com", bytes_in=0, bytes_out=100_000_000,
                       is_internal=False)  # 100 MB, no event
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0.009")  # 0.1 GB * $0.09


def test_track_network_false_leaves_network_cost_zero(tmp_path, monkeypatch):
    # detection skipped → resolver returns Tier 3 estimated, but external_bytes_out
    # remained 0 because no calls were recorded → product is 0 either way.
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv(None, None, "none")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage)
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    storage.insert_task(t)
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_network_cost_finalize.py -v`
Expected: FAIL — `network_cost_usd` is not computed.

- [ ] **Step 3: Wire pricing into `_aggregate_costs`**

In `python/src/dexcost/tracker.py`:

(a) Add imports at the top of the file:

```python
from dexcost import cloud_detect
from dexcost.egress_pricing import EgressPricingEngine
```

(b) In `CostTracker.__init__`, create a single engine instance:

```python
        self._egress_pricing = EgressPricingEngine()
```

(c) Add a helper:

```python
    @staticmethod
    def _bytes_to_gb(b: int) -> Decimal:
        # Cloud egress is billed per GB = 10^9 bytes (decimal), NOT GiB = 2^30.
        # Pinned by test_decimal_no_float_drift — never use the float literal 1e9.
        return Decimal(b) / Decimal("1000000000")
```

(d) Replace the network-aggregation block at the bottom of `_aggregate_costs` (currently lines ~1103-1108). New body, replacing only the network-finalize section:

```python
        # Network capture — finalize the in-process accountant onto the task.
        net = task._network.finalize()
        task.network_bytes_in = net["bytes_in"]
        task.network_bytes_out = net["bytes_out"]
        task.network_call_count = net["call_count"]

        # v2 — egress pricing.  Resolve rate once per task; compute the
        # canonical scalar; stamp per-host egress_cost_usd and back-fill
        # network events (deferred per spec §6.4).
        try:
            env = cloud_detect.get_cloud_env()
            rate = self._egress_pricing.resolve_rate(env.provider, env.region)
            external_gb = self._bytes_to_gb(net["external_bytes_out"])
            task.network_cost_usd = external_gb * rate.rate_per_gb
            pricing_version = f"egress:{self._egress_pricing.catalog_version}"

            # Stamp per-host egress_cost_usd into the by_host blob.
            for host in net["by_host"]["hosts"]:
                host_external = host.get("external_bytes_out", 0)
                host_cost = self._bytes_to_gb(host_external) * rate.rate_per_gb
                host["egress_cost_usd"] = str(host_cost)
            task.network_by_host = net["by_host"]

            # Back-fill each network event for this task.
            net_events = [e for e in events if e.event_type == "network"]
            for ev in net_events:
                resp_bytes = int(ev.details.get("response_bytes", 0) or 0)
                req_bytes = int(ev.details.get("request_bytes", 0) or 0)
                is_internal = ev.details.get("is_internal_traffic")
                billable = 0 if is_internal is True else (resp_bytes + req_bytes)
                ev_cost = self._bytes_to_gb(billable) * rate.rate_per_gb
                ev.cost_usd = ev_cost
                ev.cost_confidence = (
                    "exact" if is_internal is True else rate.cost_confidence
                )
                ev.pricing_source = (
                    "egress_catalog:internal" if is_internal is True
                    else rate.pricing_source
                )
                ev.pricing_version = pricing_version
                ev.details = {
                    k: v for k, v in ev.details.items() if k != "cost_pending"
                }
                self._storage.update_event(ev)
                task.total_cost_usd += ev_cost  # network events were $0 in the first pass

            # network_cost_usd is part of total — added once, from the scalar.
            task.total_cost_usd += task.network_cost_usd
        except Exception:  # noqa: BLE001 — Tier 5 fail-silent
            _log.warning(
                "egress cost computation failed for task %s",
                task.task_id, exc_info=True,
            )
            task.network_cost_usd = Decimal("0")
            task.network_by_host = net["by_host"]
```

> Two subtleties pinned by the tests above:
> - `total_cost_usd` already summed events in the per-event loop; `network` events had `cost_usd=0` at that time. The per-event stamp here both updates the row in storage *and* adds the freshly-resolved per-event cost to `total_cost_usd`.
> - `task.network_cost_usd` is the **scalar** truth (computed from `external_bytes_out`); the event sum can be less (cataloged + below-threshold calls contribute bytes but no event). The §6.5 inequality `sum(network event cost) ≤ network_cost_usd` is asserted by the property test in Task 10.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_network_cost_finalize.py tests/test_tracker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/tracker.py python/tests/test_network_cost_finalize.py
git commit -m "feat(network-cost-v2): finalize-time egress pricing on tasks + events"
```

---

### Task 9: Wire cloud detection into `dexcost.init()`

**Files:**
- Modify: `python/src/dexcost/__init__.py`
- Test: `python/tests/test_init_cloud_detection_wiring.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_init_cloud_detection_wiring.py`:

```python
"""dexcost.init() launches the cloud-detection probe unless track_network=False."""

import time

import dexcost
from dexcost import cloud_detect


def _reset():
    cloud_detect._result = cloud_detect.CloudEnv(None, None, "none")
    cloud_detect._thread = None


def test_init_launches_detection_under_default(monkeypatch):
    _reset()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    t0 = time.perf_counter()
    dexcost.init(storage="local")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05  # never blocks init
    env = cloud_detect.get_cloud_env()
    assert env.provider == "aws"
    assert env.region == "us-east-1"


def test_init_skips_detection_when_track_network_false(monkeypatch):
    _reset()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    dexcost.init(storage="local", track_network=False)
    env = cloud_detect.get_cloud_env()
    assert env.source == "none"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_init_cloud_detection_wiring.py -v`
Expected: FAIL — detection is not launched from `init()`.

- [ ] **Step 3: Launch detection from `init()`**

In `python/src/dexcost/__init__.py`, immediately after `_global_config = DexcostConfig(...)` (around line 161, but before any HTTP-adapter wiring), add:

```python
    # v2 network-cost — kick off non-blocking cloud detection.  No-op when
    # track_network is off.
    from dexcost.cloud_detect import start_background_detection as _start_detect
    _start_detect(track_network=_global_config.track_network)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_init_cloud_detection_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/__init__.py python/tests/test_init_cloud_detection_wiring.py
git commit -m "feat(network-cost-v2): launch cloud detection from init()"
```

---

### Task 10: Property invariants + Decision #7 dual-invoice test

**Files:**
- Test: `python/tests/test_network_cost_invariants.py` (create)
- Test: `python/tests/test_network_cost_dual_invoice.py` (create)

This task adds no production code — it exists to **pin the three structural
invariants (§10.3) plus the spec §10.2 Decision #7 contract** so a future
refactor cannot silently regress the design.

- [ ] **Step 1: Write the property-invariant suite**

Create `python/tests/test_network_cost_invariants.py`:

```python
"""§10.3 property invariants — must hold across arbitrary task shapes."""

import itertools
import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dexcost import cloud_detect
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _params():
    host_counts = (1, 5, 20, 100, 1000)
    classifications = (True, False, None)
    return list(itertools.product(host_counts, classifications))


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv("aws", "us-east-1", "env")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage)


@pytest.mark.parametrize("n_hosts,internal", _params())
def test_invariants(tracker, n_hosts, internal):
    rng = random.Random(42 + n_hosts)
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    for i in range(n_hosts):
        t._network.record(
            f"h{i}.com", bytes_in=rng.randint(0, 5000),
            bytes_out=rng.randint(0, 5000), is_internal=internal,
        )
    tracker._aggregate_costs(t)

    # Invariant 1: per-host external == scalar external.
    per_host = sum(h.get("external_bytes_out", 0)
                   for h in t.network_by_host["hosts"])
    # Read the scalar back from the live snapshot we already finalized once;
    # post-finalize, t.network_cost_usd is computed from the same scalar.
    assert per_host == t._network._external_bytes_out

    # Invariant 2: per-host cost == network_cost_usd.
    per_host_cost = sum(
        Decimal(h.get("egress_cost_usd", "0"))
        for h in t.network_by_host["hosts"]
    )
    assert per_host_cost == t.network_cost_usd

    # Invariant 3: sum(network event cost) ≤ network_cost_usd.
    events = tracker._storage.query_events(task_id=str(t.task_id))
    event_sum = sum(e.cost_usd for e in events if e.event_type == "network")
    assert event_sum <= t.network_cost_usd
```

- [ ] **Step 2: Write the dual-invoice (Decision #7) test**

Create `python/tests/test_network_cost_dual_invoice.py`:

```python
"""Spec §10.2 — cataloged vendor calls produce ONE event but contribute to BOTH
external_cost_usd and network_cost_usd.  Pins Decision #7 (§2) so a future
refactor cannot silently strip the cloud-egress half of vendor-call cost.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dexcost import cloud_detect
from dexcost.adapters.http import (
    _handle_domain_rate, register_domain_rate, clear_domain_rates,
)
from dexcost.context import set_current_task, _current_task
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv("aws", "us-east-1", "env")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage)


def test_dual_invoice_cataloged_vendor_call(tracker, monkeypatch):
    clear_domain_rates()
    register_domain_rate("api.vendor.com", cost_usd="0.01")

    # Wire the HTTP adapter to the test storage so _persist_event lands rows.
    from dexcost.adapters import http as adapter_mod
    monkeypatch.setattr(adapter_mod, "_storage", tracker._storage)

    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    token = set_current_task(t)
    try:
        _handle_domain_rate(
            "https://api.vendor.com/x", "api.vendor.com",
            track_network=True, bytes_in=0, bytes_out=500_000_000,  # 0.5 GB
            byte_details={
                "protocol": "https", "request_bytes": 0,
                "response_bytes": 500_000_000,
                "is_internal_traffic": False,
            },
        )
    finally:
        _current_task.reset(token)
        clear_domain_rates()

    t.ended_at = datetime.now(timezone.utc)
    tracker._aggregate_costs(t)

    # (1) Exactly one event for this call, type external_cost — the "one event
    #     per call" invariant from §3.3 holds.
    events = tracker._storage.query_events(task_id=str(t.task_id))
    assert len(events) == 1
    assert events[0].event_type == "external_cost"

    # (2) Vendor's per-request invoice is intact.
    assert t.external_cost_usd == Decimal("0.01")

    # (3) The cloud's egress invoice on those same bytes is captured IN ADDITION.
    #     0.5 GB * $0.09/GB = $0.045
    assert t.network_cost_usd == Decimal("0.045")

    # (4) Total = vendor + egress (no double-count, no silent drop).
    assert t.total_cost_usd == Decimal("0.055")

    # (5) The external_cost event's own cost_usd is unchanged from v1 — no
    #     egress dollars were stamped onto it (events carry measurement,
    #     task carries derived attribution — §3.3).
    assert events[0].cost_usd == Decimal("0.01")
```

- [ ] **Step 3: Run both suites**

Run: `cd python && uv run pytest tests/test_network_cost_invariants.py tests/test_network_cost_dual_invoice.py -v`
Expected: PASS.

- [ ] **Step 4: Run the full suite for regression**

Run: `cd python && uv run pytest -q`
Expected: every test green; total count increased by the new tests.

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_network_cost_invariants.py python/tests/test_network_cost_dual_invoice.py
git commit -m "test(network-cost-v2): property invariants + Decision #7 dual-invoice contract"
```

---

## Self-Review

**Spec coverage** — every spec section maps to a task:

| Spec section | Task(s) |
|---|---|
| §2 Decision #1 (egress only) | 6 (accountant external split keys off `is_internal`; `bytes_in` never enters the cost arithmetic) |
| §2 Decision #2 (zero user config) | 9 (`init()` adds no new knobs) |
| §2 Decision #3 (no per-event egress on llm_call/external_cost) | 8 (event stamping touches only `network` events), 10 (Decision-#7 test asserts external_cost cost_usd unchanged) |
| §2 Decision #4 (deferred per-event cost) | 7 (`cost_pending=true` at emission), 8 (back-fill at finalize) |
| §2 Decision #5 (4-valued confidence) | 4 (`resolve_rate` returns `computed`/`estimated`; `rate_for_internal` returns `exact`) |
| §2 Decision #6 (first-tier rates) | 3 (catalog `_meta.notes` documents the trade-off) |
| §2 Decision #7 (dual-invoice attribution) | 10 (explicit test) |
| §3.1 New modules | 3, 4, 5 |
| §3.2 Data model changes | 1 (`network_cost_usd`), 2 (migration), 6 (`external_bytes_out` per-host), 8 (`network_by_host` egress_cost_usd) |
| §3.3 Measurement/pricing separation invariant | 8, 10 |
| §3.4 LLM instruments unchanged | (no task — verified by absence of edits to `instruments/`) |
| §4 Catalog structure / freshness / launch coverage | 3 |
| §4.6 Rate resolution ladder | 4 |
| §5.1 Phase 1a env detection | 5 |
| §5.2 Phase 1b DMI | 5 |
| §5.3 Phase 2 metadata probe | 5 |
| §5.4 Result lifecycle | 5 |
| §5.5 Probe self-classification (internal) | inherited from v1 — no new code |
| §6.1 Accountant external-byte split | 6 |
| §6.2 Rate resolution table | 4 |
| §6.3 GB = 10^9, never float | 4 (test_decimal_no_float_drift), 8 (`_bytes_to_gb` uses `Decimal("1000000000")`) |
| §6.4 Deferred cost & finalize flow | 7 (emission marker), 8 (finalize stamp) |
| §6.5 Canonical scalar | 8, 10 (invariant 1+2) |
| §7 Degradation ladder | 4 (Tiers 1–4), 8 (Tier 5 try/except) |
| §7.3 Warn-once per failure mode | 4 |
| §8.1 SQLite v4→v5 migration | 2 |
| §8.2 `update_event` re-mark-pending fix | **pre-requisite commit `ff96e94`** (already landed) |
| §8.3 Backward compatibility | 1 (`from_dict` default), 2 (migration + `_row_to_task` guard) |
| §9 Configuration interaction | 9 (track_network bypass), 7+8 (threshold gates emission, not accounting) |
| §10.1 Unit tests | 3, 4, 5, 6 |
| §10.2 Integration tests | 7, 8, 10 (Decision #7) |
| §10.3 Property invariants | 10 |
| §10.4 Cross-language matrix | out of scope (Go/Rust/TS plans land separately, per §11) |
| §10.5 Not tested | n/a |

**Placeholder scan** — no `TBD`/`TODO`. Task 3 step 3 contains the only literal data-entry handoff: every commercial AWS/GCP/Azure region must be transcribed by hand from each provider's public pricing page. The skeleton is provided; the integrity tests in step 1 fail loudly if a provider block is missing or malformed, so the data-entry step has a green/red signal.

**Type consistency** — `NetworkAccountant.finalize()` returns `{"bytes_in", "bytes_out", "external_bytes_out", "call_count", "by_host"}` after Task 6; Task 8 step 3 reads exactly those keys. `network_by_host[].egress_cost_usd` is stored as a Decimal-string (matching `*_cost_usd` columns) — readers parse with `Decimal(...)`. `EgressRate` is a frozen dataclass.

**Pre-requisite chain (already merged on this branch):**
1. `ff96e94 fix(storage): re-mark sync_status='pending' on update_event` — Task 8 step 3 depends on this; without it, event-cost stamps would never re-sync.
2. `a326af3 docs(network-cost-v2): make dual-invoice attribution explicit` — Task 10's Decision #7 test is the executable spec of the now-explicit decision.

**Known follow-ups (out of scope, not gaps):**
- **Go / Rust / TypeScript ports** — each its own spec → plan → implementation cycle (spec §11). The shared `egress_prices.json` from Task 3 is the cross-SDK catalog contract.
- **Catalog refresh automation** — spec §4.5 calls launch coverage a one-time data-entry job; an automated scrape/refresh tool is explicitly deferred.
- **`dexcost status` CLI surfacing of cloud_detect result** — the `CloudEnv` is exposed via `get_cloud_env()`; surfacing it under the existing `dexcost status` command is a one-line add deferred to the status-command owner.
- **Monthly-tier pricing** — explicitly deferred (spec §4.4); requires a workspace-scoped cumulative view the SDK does not have.
