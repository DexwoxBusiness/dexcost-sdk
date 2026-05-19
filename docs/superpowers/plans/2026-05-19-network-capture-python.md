# Network / Egress Capture (Python SDK) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Python SDK's HTTP adapter measure request/response bytes for every instrumented call, attribute them to the active task, and emit a `network` event for notable un-cataloged calls — implementing the approved spec `docs/superpowers/specs/2026-05-19-network-capture-design.md` (v1: bytes only, no dollar cost).

**Architecture:** Extend the existing HTTP adapter (`adapters/http.py`). A `NetworkAccountant` accumulator attaches to each `Task` as a non-serialized field; the adapter records bytes into it per call; `CostTracker._aggregate_costs` finalizes it onto the four new `Task` fields at task end. Un-cataloged calls — which today emit a noise `external_cost $0` event — are re-typed to `network` events (above threshold) or counters-only (below). A context-scoped suppression flag, set by the LLM instruments, enforces "≤1 event per HTTP call".

**Tech Stack:** Python 3.13, `dataclasses`, stdlib `ipaddress` / `threading` / `contextvars`, `sqlite3`, `pytest`. No new dependencies.

**Run tests with:** `cd python && python -m pytest <path> -v`

---

### Task 1: `network` event type

**Files:**
- Modify: `python/src/dexcost/models/enums.py:14-20`
- Modify: `python/schemas/dexcost-event.v1.json` (the `event_type` `enum` array)
- Test: `python/tests/test_enums_network.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_enums_network.py`:

```python
"""Network event type is registered."""

from dexcost.models.enums import EventType


def test_network_event_type_exists():
    assert EventType.NETWORK.value == "network"


def test_network_in_event_type_members():
    assert "network" in {e.value for e in EventType}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_enums_network.py -v`
Expected: FAIL — `AttributeError: NETWORK`

- [ ] **Step 3: Add the enum member**

In `python/src/dexcost/models/enums.py`, change the `EventType` class:

```python
class EventType(str, Enum):
    """Discriminator for cost-generating events."""

    LLM_CALL = "llm_call"
    EXTERNAL_COST = "external_cost"
    COMPUTE_COST = "compute_cost"
    RETRY_MARKER = "retry_marker"
    NETWORK = "network"
```

- [ ] **Step 4: Add `network` to the event schema**

Open `python/schemas/dexcost-event.v1.json`. Find the `event_type` property — it has an `"enum"` array listing `"llm_call"`, `"external_cost"`, `"compute_cost"`, `"retry_marker"`. Add `"network"` to that array.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_enums_network.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/src/dexcost/models/enums.py python/schemas/dexcost-event.v1.json python/tests/test_enums_network.py
git commit -m "feat(network): add network event type"
```

---

### Task 2: Four network fields on the `Task` model

**Files:**
- Modify: `python/src/dexcost/models/task.py`
- Modify: `python/schemas/dexcost-task.v1.json`
- Test: `python/tests/test_task_network_fields.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_task_network_fields.py`:

```python
"""Task model carries the four network-capture fields."""

from dexcost.models.task import Task


def test_network_field_defaults():
    t = Task(task_type="x")
    assert t.network_bytes_in == 0
    assert t.network_bytes_out == 0
    assert t.network_call_count == 0
    assert t.network_by_host == {"hosts": []}


def test_network_fields_round_trip():
    t = Task(task_type="x")
    t.network_bytes_in = 4096
    t.network_bytes_out = 512
    t.network_call_count = 3
    t.network_by_host = {"hosts": [{"host": "a.com", "calls": 3, "bytes_in": 4096, "bytes_out": 512}]}
    restored = Task.from_dict(t.to_dict())
    assert restored.network_bytes_in == 4096
    assert restored.network_bytes_out == 512
    assert restored.network_call_count == 3
    assert restored.network_by_host == {"hosts": [{"host": "a.com", "calls": 3, "bytes_in": 4096, "bytes_out": 512}]}


def test_network_by_host_absent_in_dict_defaults_empty():
    # A task dict produced before this feature has no network_* keys.
    legacy = Task(task_type="x").to_dict()
    del legacy["network_by_host"]
    del legacy["network_bytes_in"]
    restored = Task.from_dict(legacy)
    assert restored.network_by_host == {"hosts": []}
    assert restored.network_bytes_in == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_task_network_fields.py -v`
Expected: FAIL — `TypeError` / `AssertionError` (fields do not exist)

- [ ] **Step 3: Add the fields to the dataclass**

In `python/src/dexcost/models/task.py`, after the `failure_count: int = 0` line (inside the "Waste metrics" block) and before `# Schema contract`, add:

```python
    # Network capture (rolled up from instrumented HTTP calls)
    network_bytes_in: int = 0
    network_bytes_out: int = 0
    network_call_count: int = 0
    network_by_host: dict[str, Any] = field(default_factory=lambda: {"hosts": []})
```

- [ ] **Step 4: Add the fields to `to_dict`**

In `to_dict`, before `"schema_version": self.schema_version,`, add:

```python
            "network_bytes_in": self.network_bytes_in,
            "network_bytes_out": self.network_bytes_out,
            "network_call_count": self.network_call_count,
            "network_by_host": self.network_by_host,
```

- [ ] **Step 5: Add the fields to `from_dict`**

In `from_dict`, before `schema_version=data.get("schema_version", "1"),`, add:

```python
                network_bytes_in=data.get("network_bytes_in", 0),
                network_bytes_out=data.get("network_bytes_out", 0),
                network_call_count=data.get("network_call_count", 0),
                network_by_host=data.get("network_by_host") or {"hosts": []},
```

- [ ] **Step 6: Add the fields to the task schema**

In `python/schemas/dexcost-task.v1.json`, add four properties to the `properties` object: `network_bytes_in`, `network_bytes_out`, `network_call_count` as `{"type": "integer", "minimum": 0}`, and `network_by_host` as `{"type": "object"}`. Do **not** add them to any `required` array — old payloads omit them.

- [ ] **Step 7: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_task_network_fields.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add python/src/dexcost/models/task.py python/schemas/dexcost-task.v1.json python/tests/test_task_network_fields.py
git commit -m "feat(network): add network fields to Task model"
```

---

### Task 3: SQLite schema columns + migration v3→v4

**Files:**
- Modify: `python/src/dexcost/storage/migrations.py`
- Modify: `python/src/dexcost/storage/sqlite.py`
- Test: `python/tests/test_network_migration.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_migration.py`:

```python
"""v3→v4 migration adds the four network columns; round-trip works."""

import sqlite3
import uuid
from datetime import datetime, timezone

from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage


def test_fresh_db_has_network_columns(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert {"network_bytes_in", "network_bytes_out",
            "network_call_count", "network_by_host"} <= cols
    st.close()


def test_task_network_fields_round_trip_through_storage(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    t = Task(task_id=uuid.uuid4(), task_type="scrape",
             started_at=datetime.now(timezone.utc))
    t.network_bytes_in = 9000
    t.network_bytes_out = 1200
    t.network_call_count = 5
    t.network_by_host = {"hosts": [{"host": "x.com", "calls": 5,
                                    "bytes_in": 9000, "bytes_out": 1200}]}
    st.insert_task(t)
    got = st.get_task(str(t.task_id))
    assert got.network_bytes_in == 9000
    assert got.network_bytes_out == 1200
    assert got.network_call_count == 5
    assert got.network_by_host == {"hosts": [{"host": "x.com", "calls": 5,
                                              "bytes_in": 9000, "bytes_out": 1200}]}
    st.close()


def test_v3_db_migrates_to_v4(tmp_path):
    # Build a v3-shaped tasks table (no network columns), record version 3.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, sync_status TEXT "
        "NOT NULL DEFAULT 'pending')"
    )
    conn.execute(
        "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version_number INTEGER NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), migration_name TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version_number, migration_name) VALUES (3, 'seed')"
    )
    conn.commit()
    conn.close()

    st = SQLiteStorage(db_path=str(db))  # opening runs migrations
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "network_by_host" in cols
    assert st.get_schema_version() == 4
    st.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_migration.py -v`
Expected: FAIL — columns missing / schema version is 3

- [ ] **Step 3: Bump the target version and register the migration**

In `python/src/dexcost/storage/migrations.py`, change line 20:

```python
TARGET_SCHEMA_VERSION = 4
```

At the end of the file (after `_sqlite_v2_to_v3`), add:

```python
@register_sqlite_migration(3, 4)
def _sqlite_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Add the four network-capture columns to the tasks table (idempotent)."""
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "network_bytes_in" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN network_bytes_in INTEGER NOT NULL DEFAULT 0")
    if "network_bytes_out" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN network_bytes_out INTEGER NOT NULL DEFAULT 0")
    if "network_call_count" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN network_call_count INTEGER NOT NULL DEFAULT 0")
    if "network_by_host" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_by_host TEXT NOT NULL "
            "DEFAULT '{\"hosts\": []}'"
        )
```

- [ ] **Step 4: Add the columns to the fresh-create DDL**

In `python/src/dexcost/storage/sqlite.py`, in `_CREATE_TASKS`, add four columns before the closing `);` (after `sync_status ... DEFAULT 'pending'` — add a comma to that line):

```python
    sync_status         TEXT NOT NULL DEFAULT 'pending',
    network_bytes_in    INTEGER NOT NULL DEFAULT 0,
    network_bytes_out   INTEGER NOT NULL DEFAULT 0,
    network_call_count  INTEGER NOT NULL DEFAULT 0,
    network_by_host     TEXT NOT NULL DEFAULT '{"hosts": []}'
);
```

- [ ] **Step 5: Write the columns in `insert_task`**

In `insert_task`, extend the column list, the `VALUES` placeholders, and the parameter tuple. Replace the SQL string and tuple so the statement reads:

```python
                """INSERT INTO tasks (
                    task_id, task_type, status, started_at, ended_at, metadata,
                    llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
                    total_input_tokens, total_output_tokens, total_cached_tokens,
                    retry_count, retry_cost_usd, failure_count,
                    customer_id, project_id, parent_task_id,
                    experiment_id, variant,
                    network_bytes_in, network_bytes_out, network_call_count, network_by_host
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
```

and append to the parameter tuple, after `task.variant,`:

```python
                    task.network_bytes_in,
                    task.network_bytes_out,
                    task.network_call_count,
                    _json_dumps(task.network_by_host),
```

- [ ] **Step 6: Write the columns in `update_task`**

In `update_task`, extend the `SET` clause — change the `experiment_id=?, variant=?, sync_status='pending'` line to:

```python
                    experiment_id=?, variant=?,
                    network_bytes_in=?, network_bytes_out=?,
                    network_call_count=?, network_by_host=?,
                    sync_status='pending'
```

and add to the parameter tuple, after `task.variant,` and before `str(task.task_id),`:

```python
                    task.network_bytes_in,
                    task.network_bytes_out,
                    task.network_call_count,
                    _json_dumps(task.network_by_host),
```

- [ ] **Step 7: Read the columns in `_row_to_task`**

In `_row_to_task`, add to the `Task(...)` constructor call, after `variant=row["variant"],`:

```python
            network_bytes_in=row["network_bytes_in"] if "network_bytes_in" in row.keys() else 0,
            network_bytes_out=row["network_bytes_out"] if "network_bytes_out" in row.keys() else 0,
            network_call_count=row["network_call_count"] if "network_call_count" in row.keys() else 0,
            network_by_host=(
                _json_loads(row["network_by_host"])
                if "network_by_host" in row.keys() and row["network_by_host"]
                else {"hosts": []}
            ),
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_migration.py -v`
Expected: PASS

- [ ] **Step 9: Run the full storage + migration suites for regression**

Run: `cd python && python -m pytest tests/test_sqlite_storage.py tests/test_migrations.py -v`
Expected: PASS (all existing tests still green)

- [ ] **Step 10: Commit**

```bash
git add python/src/dexcost/storage/migrations.py python/src/dexcost/storage/sqlite.py python/tests/test_network_migration.py
git commit -m "feat(network): persist network fields + v3->v4 migration"
```

---

### Task 4: `_netbytes` helpers — destination classifier + byte measurement

**Files:**
- Create: `python/src/dexcost/adapters/_netbytes.py`
- Test: `python/tests/test_netbytes.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_netbytes.py`:

```python
"""Destination classification and byte measurement helpers."""

from dexcost.adapters._netbytes import classify_destination, measure_bytes_from_headers


def test_private_ipv4_is_internal():
    assert classify_destination("10.1.2.3") is True
    assert classify_destination("192.168.0.5") is True
    assert classify_destination("172.16.9.9") is True


def test_localhost_and_link_local_are_internal():
    assert classify_destination("127.0.0.1") is True
    assert classify_destination("::1") is True
    assert classify_destination("169.254.10.1") is True


def test_public_ip_is_not_internal():
    assert classify_destination("8.8.8.8") is False
    assert classify_destination("1.1.1.1") is False


def test_named_host_is_unknown():
    # A hostname (not an IP literal): we do not do an extra DNS lookup.
    assert classify_destination("api.openai.com") is None
    assert classify_destination("") is None


def test_measure_bytes_from_content_length():
    headers = {"Content-Length": "2048", "Content-Type": "application/json"}
    # request line + header bytes + body length
    n = measure_bytes_from_headers("POST", "https://x.com/v1/y", headers, body_len=2048)
    assert n >= 2048
    # headers contribute too
    assert n > 2048


def test_measure_bytes_zero_body():
    n = measure_bytes_from_headers("GET", "https://x.com/", {}, body_len=0)
    assert n > 0  # request line + minimal headers still cost bytes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_netbytes.py -v`
Expected: FAIL — `ModuleNotFoundError: dexcost.adapters._netbytes`

- [ ] **Step 3: Write the module**

Create `python/src/dexcost/adapters/_netbytes.py`:

```python
"""Helpers for the HTTP network adapter: destination classification and
byte measurement. Pure functions — no SDK state, no I/O beyond parsing.
"""

from __future__ import annotations

import ipaddress
from typing import Any


def classify_destination(host: str) -> bool | None:
    """Return whether *host* is internal traffic.

    ``True``  — host is an RFC1918 / loopback / link-local IP literal.
    ``False`` — host is a public IP literal.
    ``None``  — host is a name (not an IP literal); the SDK does not perform
                an extra DNS lookup to resolve it.
    """
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _headers_byte_len(headers: dict[str, Any]) -> int:
    """Approximate on-the-wire size of a header block: ``Key: Value\\r\\n`` each."""
    total = 0
    for key, value in headers.items():
        total += len(str(key)) + len(str(value)) + 4  # ": " + CRLF
    return total + 2  # trailing CRLF that ends the header block


def measure_bytes_from_headers(
    method: str, url: str, headers: dict[str, Any], body_len: int
) -> int:
    """Approximate the on-the-wire byte size of one HTTP message.

    ``request line + header block + body``. Used for both directions: pass
    the request method/url/headers for bytes-out, or ``"" / "" / response
    headers`` for bytes-in. *body_len* is the known body length in bytes.
    """
    request_line = len(str(method)) + len(str(url)) + 12  # method + url + " HTTP/1.1\r\n"
    return request_line + _headers_byte_len(headers) + max(0, int(body_len))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_netbytes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/adapters/_netbytes.py python/tests/test_netbytes.py
git commit -m "feat(network): add destination classifier + byte measurement helpers"
```

---

### Task 5: `NetworkAccountant`

**Files:**
- Create: `python/src/dexcost/network_accountant.py`
- Test: `python/tests/test_network_accountant.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_accountant.py`:

```python
"""NetworkAccountant — per-task in-process byte accumulator."""

from dexcost.network_accountant import NetworkAccountant, FINALIZE_CAP, LIVE_CAP


def test_record_accumulates_scalars():
    acc = NetworkAccountant()
    acc.record("a.com", bytes_in=100, bytes_out=10)
    acc.record("a.com", bytes_in=50, bytes_out=5)
    snap = acc.finalize()
    assert snap["bytes_in"] == 150
    assert snap["bytes_out"] == 15
    assert snap["call_count"] == 2


def test_finalize_groups_by_host():
    acc = NetworkAccountant()
    acc.record("a.com", 100, 10)
    acc.record("b.com", 200, 20)
    hosts = {h["host"]: h for h in acc.finalize()["by_host"]["hosts"]}
    assert hosts["a.com"] == {"host": "a.com", "calls": 1, "bytes_in": 100, "bytes_out": 10}
    assert hosts["b.com"]["bytes_in"] == 200


def test_finalize_caps_to_top_20_with_other_bucket():
    acc = NetworkAccountant()
    # 25 hosts; host_i gets i bytes_in so the heavy ones are deterministic.
    for i in range(25):
        acc.record(f"h{i:02d}.com", bytes_in=i + 1, bytes_out=0)
    hosts = acc.finalize()["by_host"]["hosts"]
    assert len(hosts) == FINALIZE_CAP + 1  # 20 + _other
    names = {h["host"] for h in hosts}
    assert "_other" in names
    assert "h24.com" in names  # heaviest survives
    assert "h00.com" not in names  # lightest folded into _other
    other = next(h for h in hosts if h["host"] == "_other")
    # _other holds the 5 lightest (1+2+3+4+5 bytes_in) and their 5 calls.
    assert other["calls"] == 5
    assert other["bytes_in"] == 1 + 2 + 3 + 4 + 5


def test_empty_finalize_is_empty_array():
    assert NetworkAccountant().finalize()["by_host"] == {"hosts": []}


def test_live_cap_folds_overflow_hosts_into_other():
    acc = NetworkAccountant()
    for i in range(LIVE_CAP + 50):
        acc.record(f"h{i}.com", bytes_in=1, bytes_out=0)
    # Live map never exceeds LIVE_CAP tracked hosts (+ the _other bucket).
    assert acc.live_host_count() <= LIVE_CAP
    snap = acc.finalize()
    assert snap["call_count"] == LIVE_CAP + 50  # every call still counted


def test_record_after_finalize_is_noop():
    acc = NetworkAccountant()
    acc.record("a.com", 100, 10)
    acc.finalize()
    acc.record("a.com", 999, 999)  # frozen — must be ignored
    snap = acc.finalize()
    assert snap["bytes_in"] == 100
    assert snap["call_count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_accountant.py -v`
Expected: FAIL — `ModuleNotFoundError: dexcost.network_accountant`

- [ ] **Step 3: Write the module**

Create `python/src/dexcost/network_accountant.py`:

```python
"""NetworkAccountant — a per-task, in-process accumulator of HTTP byte usage.

One instance lives (un-serialised) on each Task. The HTTP adapter calls
``record()`` per call; ``finalize()`` is called once at task end. After
finalize the accountant is frozen — later ``record()`` calls are no-ops, so
late-arriving bytes never mutate already-shipped task aggregates.
"""

from __future__ import annotations

import threading
from typing import Any

# Hosts kept in the per-task `by_host` array after finalize (plus `_other`).
FINALIZE_CAP = 20
# Distinct hosts tracked live during the task before overflow folds into
# `_other` — bounds mid-task memory for pathological many-host workloads.
LIVE_CAP = 500


class NetworkAccountant:
    """Accumulates bytes in/out, call count, and a per-host breakdown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes_in = 0
        self._bytes_out = 0
        self._call_count = 0
        # host -> [calls, bytes_in, bytes_out]
        self._hosts: dict[str, list[int]] = {}
        # Overflow bucket once LIVE_CAP distinct hosts are tracked.
        self._other = [0, 0, 0]
        self._frozen = False

    def record(self, host: str, bytes_in: int, bytes_out: int) -> None:
        """Add one HTTP call's bytes. No-op once finalized."""
        with self._lock:
            if self._frozen:
                return
            self._bytes_in += bytes_in
            self._bytes_out += bytes_out
            self._call_count += 1
            key = host or "_unknown"
            entry = self._hosts.get(key)
            if entry is not None:
                entry[0] += 1
                entry[1] += bytes_in
                entry[2] += bytes_out
            elif len(self._hosts) < LIVE_CAP:
                self._hosts[key] = [1, bytes_in, bytes_out]
            else:
                self._other[0] += 1
                self._other[1] += bytes_in
                self._other[2] += bytes_out

    def live_host_count(self) -> int:
        """Number of distinct hosts currently tracked (excludes `_other`)."""
        with self._lock:
            return len(self._hosts)

    def finalize(self) -> dict[str, Any]:
        """Freeze the accountant and return the snapshot for the task fields.

        Returns ``{"bytes_in", "bytes_out", "call_count", "by_host"}`` where
        ``by_host`` is ``{"hosts": [...]}`` — the top FINALIZE_CAP hosts by
        total bytes, plus an `_other` bucket summing the rest.
        """
        with self._lock:
            self._frozen = True
            ranked = sorted(
                self._hosts.items(),
                key=lambda kv: kv[1][1] + kv[1][2],
                reverse=True,
            )
            top = ranked[:FINALIZE_CAP]
            overflow = ranked[FINALIZE_CAP:]

            other = list(self._other)  # copy: [calls, bytes_in, bytes_out]
            for _host, (calls, b_in, b_out) in overflow:
                other[0] += calls
                other[1] += b_in
                other[2] += b_out

            hosts: list[dict[str, Any]] = [
                {"host": host, "calls": c, "bytes_in": bi, "bytes_out": bo}
                for host, (c, bi, bo) in top
            ]
            if other[0] > 0:
                hosts.append(
                    {"host": "_other", "calls": other[0],
                     "bytes_in": other[1], "bytes_out": other[2]}
                )
            return {
                "bytes_in": self._bytes_in,
                "bytes_out": self._bytes_out,
                "call_count": self._call_count,
                "by_host": {"hosts": hosts},
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_accountant.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/network_accountant.py python/tests/test_network_accountant.py
git commit -m "feat(network): add NetworkAccountant accumulator"
```

---

### Task 6: Attach the accountant to `Task`; finalize it in `_aggregate_costs`

**Files:**
- Modify: `python/src/dexcost/models/task.py`
- Modify: `python/src/dexcost/tracker.py:1072-1101` (`_aggregate_costs`)
- Test: `python/tests/test_network_finalize.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_finalize.py`:

```python
"""Task end finalizes the NetworkAccountant onto the four task fields."""

import dexcost


def test_recorded_bytes_land_on_task_at_end(tmp_path):
    dexcost.close()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "b.db"))
    tracker = dexcost.CostTracker()
    task = tracker.start_task(task_type="scrape")
    # Simulate the adapter recording two HTTP calls.
    task.task._network.record("api.a.com", bytes_in=8000, bytes_out=400)
    task.task._network.record("api.b.com", bytes_in=200, bytes_out=50)
    task.end()

    stored = tracker._storage.get_task(str(task.task_id))
    assert stored.network_bytes_in == 8200
    assert stored.network_bytes_out == 450
    assert stored.network_call_count == 2
    hosts = {h["host"] for h in stored.network_by_host["hosts"]}
    assert hosts == {"api.a.com", "api.b.com"}
    dexcost.close()


def test_zero_call_task_ships_present_zero_fields(tmp_path):
    dexcost.close()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "b.db"))
    tracker = dexcost.CostTracker()
    task = tracker.start_task(task_type="noop")
    task.end()
    stored = tracker._storage.get_task(str(task.task_id))
    assert stored.network_bytes_in == 0
    assert stored.network_bytes_out == 0
    assert stored.network_call_count == 0
    assert stored.network_by_host == {"hosts": []}
    dexcost.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_finalize.py -v`
Expected: FAIL — `AttributeError: 'Task' object has no attribute '_network'`

- [ ] **Step 3: Add the non-serialized accountant field to `Task`**

In `python/src/dexcost/models/task.py`, add the import near the top (after the existing imports):

```python
from dexcost.network_accountant import NetworkAccountant
```

In the `Task` dataclass, after the `network_by_host` field and before `# Schema contract`, add:

```python
    # In-memory only — the per-task byte accumulator. Never serialised:
    # to_dict()/from_dict() do not touch it; a fresh task gets a fresh one.
    _network: NetworkAccountant = field(
        default_factory=NetworkAccountant, compare=False, repr=False
    )
```

(`to_dict` and `from_dict` are explicit field lists — they already ignore `_network`. No change needed there.)

- [ ] **Step 4: Finalize the accountant in `_aggregate_costs`**

In `python/src/dexcost/tracker.py`, at the end of `_aggregate_costs` (after the `for event in events:` loop, after the final `task.total_cost_usd += event.cost_usd` line), add:

```python
        # Network capture — finalize the in-process accountant onto the task.
        net = task._network.finalize()
        task.network_bytes_in = net["bytes_in"]
        task.network_bytes_out = net["bytes_out"]
        task.network_call_count = net["call_count"]
        task.network_by_host = net["by_host"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_finalize.py -v`
Expected: PASS

- [ ] **Step 6: Run the tracker suite for regression**

Run: `cd python && python -m pytest tests/test_tracker.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add python/src/dexcost/models/task.py python/src/dexcost/tracker.py python/tests/test_network_finalize.py
git commit -m "feat(network): attach accountant to Task, finalize at task end"
```

---

### Task 7: Config fields for network capture

**Files:**
- Modify: `python/src/dexcost/config.py:42-50`
- Test: `python/tests/test_network_config.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_config.py`:

```python
"""Network-capture config fields and their defaults."""

from dexcost.config import DexcostConfig


def test_network_config_defaults():
    c = DexcostConfig(storage="local")
    assert c.track_network is True
    assert c.network_event_threshold_bytes == 102_400
    assert c.network_event_on_error is True
    assert c.network_event_latency_ms == 0


def test_network_config_overrides():
    c = DexcostConfig(storage="local", track_network=False,
                      network_event_threshold_bytes=4096,
                      network_event_on_error=False,
                      network_event_latency_ms=5000)
    assert c.track_network is False
    assert c.network_event_threshold_bytes == 4096
    assert c.network_event_on_error is False
    assert c.network_event_latency_ms == 5000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_config.py -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'track_network'`

- [ ] **Step 3: Add the fields**

In `python/src/dexcost/config.py`, in the `DexcostConfig` dataclass, after `environment: str | None = None` and before `_key_type: ...`, add:

```python
    # Network capture (spec: 2026-05-19-network-capture-design)
    track_network: bool = True
    network_event_threshold_bytes: int = 102_400  # 100 KiB; combined req+resp
    network_event_on_error: bool = True
    network_event_latency_ms: int = 0  # 0 = latency trigger disabled
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/config.py python/tests/test_network_config.py
git commit -m "feat(network): add network-capture config fields"
```

---

### Task 8: Context-scoped event-suppression flag

**Files:**
- Modify: `python/src/dexcost/context.py`
- Test: `python/tests/test_network_suppression.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_suppression.py`:

```python
"""Context-scoped flag that suppresses the per-call network event."""

from dexcost.context import is_network_event_suppressed, suppress_network_event


def test_default_not_suppressed():
    assert is_network_event_suppressed() is False


def test_suppress_context_manager_sets_and_clears():
    assert is_network_event_suppressed() is False
    with suppress_network_event():
        assert is_network_event_suppressed() is True
    assert is_network_event_suppressed() is False


def test_nested_suppression_restores_outer_state():
    with suppress_network_event():
        with suppress_network_event():
            assert is_network_event_suppressed() is True
        assert is_network_event_suppressed() is True
    assert is_network_event_suppressed() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_suppression.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_network_event_suppressed'`

- [ ] **Step 3: Add the flag + helpers**

In `python/src/dexcost/context.py`, after the `_current_task` ContextVar block (after `set_current_task`), add:

```python
# ---------------------------------------------------------------------------
# Per-call network-event suppression flag
# ---------------------------------------------------------------------------
# When set, the HTTP adapter records bytes for the call but does NOT emit a
# standalone `network` event — used by the LLM instruments so an LLM API call
# does not produce both an `llm_call` event and a `network` event.

_suppress_network: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_suppress_network", default=False
)


def is_network_event_suppressed() -> bool:
    """Return True when the current call must not emit a `network` event."""
    return _suppress_network.get()


@contextmanager
def suppress_network_event() -> Generator[None, None, None]:
    """Within this block, the HTTP adapter suppresses standalone network events.

    Bytes are still recorded into the task counters; only the per-call
    `network` event is withheld. Used by LLM instruments around their HTTP
    call so it does not double-emit (`llm_call` + `network`).
    """
    token = _suppress_network.set(True)
    try:
        yield
    finally:
        _suppress_network.reset(token)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_suppression.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/context.py python/tests/test_network_suppression.py
git commit -m "feat(network): add context-scoped network-event suppression flag"
```

---

### Task 9: HTTP adapter — byte accounting, re-typed un-cataloged calls, error counter

**Files:**
- Modify: `python/src/dexcost/adapters/http.py`
- Test: `python/tests/test_network_capture.py` (create)

This task does the integration. The adapter's five wrappers time each call and
pass the request object + latency to a single handler `_handle_http_call`,
which replaces the old `_maybe_record_cost`. The handler: (1) records bytes
into the active task's accountant, (2) for catalog/domain matches stamps bytes
into the `external_cost` event, (3) for un-cataloged calls emits a `network`
event when above threshold / on error / on latency, else nothing.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_capture.py`:

```python
"""End-to-end network capture through the HTTP adapter."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from dexcost.adapters import http as http_adapter
from dexcost.adapters.http import (
    _handle_http_call,
    clear_domain_rates,
    clear_recorded_events,
    get_network_error_count,
    get_recorded_events,
    register_domain_rate,
    reset_network_error_count,
)
from dexcost.context import clear_context, set_current_task, suppress_network_event
from dexcost.models.task import Task
from dexcost.session import reset_session_manager


@pytest.fixture(autouse=True)
def _clean():
    clear_domain_rates()
    clear_recorded_events()
    reset_network_error_count()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    http_adapter.set_catalog(None)
    yield
    clear_domain_rates()
    clear_recorded_events()
    reset_network_error_count()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    http_adapter.set_catalog(None)


class _Resp:
    """Minimal response stand-in: headers dict + status_code."""

    def __init__(self, status_code: int = 200, body_len: int = 0,
                 content_type: str = "application/json"):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type,
                        "Content-Length": str(body_len)}
        self._body_len = body_len

    def json(self):  # noqa: D401 - test stub
        return {}


def _task() -> Task:
    return Task(task_id=uuid.uuid4(), task_type="t",
                started_at=datetime.now(timezone.utc))


def test_bytes_land_on_task_counters():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/v1/x", method="POST",
                      request_headers={"Content-Type": "application/json"},
                      request_body_len=120, response=_Resp(200, body_len=500),
                      latency_ms=12)
    snap = task._network.finalize()
    assert snap["call_count"] == 1
    assert snap["bytes_in"] > 500   # response body + headers
    assert snap["bytes_out"] > 120  # request body + headers


def test_uncataloged_above_threshold_emits_network_event():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/big", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(200, body_len=200_000), latency_ms=40)
    events = get_recorded_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "network"
    assert ev.service_name == "api.uncataloged.com"
    assert ev.cost_usd == 0
    assert ev.cost_confidence == "unknown"
    assert ev.details["protocol"] == "https"
    assert ev.details["status_code"] == 200
    assert ev.details["response_bytes"] >= 200_000
    assert ev.details["is_internal_traffic"] is None  # named host


def test_uncataloged_below_threshold_emits_no_event():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/small", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(200, body_len=300), latency_ms=5)
    assert get_recorded_events() == []          # no event
    assert task._network.finalize()["call_count"] == 1  # counters still updated


def test_uncataloged_error_emits_event_even_when_small():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/fail", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(503, body_len=80), latency_ms=5)
    events = get_recorded_events()
    assert len(events) == 1
    assert events[0].event_type == "network"
    assert events[0].details["status_code"] == 503


def test_cataloged_domain_rate_stamps_bytes_no_network_event():
    task = _task()
    set_current_task(task)
    register_domain_rate("api.vendor.com", cost_usd="0.01")
    _handle_http_call("https://api.vendor.com/charge", method="POST",
                      request_headers={}, request_body_len=40,
                      response=_Resp(200, body_len=900), latency_ms=8)
    events = get_recorded_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "external_cost"      # not re-typed
    assert ev.details["request_bytes"] >= 40     # bytes stamped in
    assert ev.details["response_bytes"] >= 900
    assert ev.details["protocol"] == "https"


def test_suppressed_call_records_bytes_but_no_network_event():
    task = _task()
    set_current_task(task)
    with suppress_network_event():
        _handle_http_call("https://api.openai.com/v1/chat", method="POST",
                          request_headers={}, request_body_len=100,
                          response=_Resp(200, body_len=300_000), latency_ms=900)
    assert get_recorded_events() == []                  # no network event
    assert task._network.finalize()["bytes_in"] > 300_000  # bytes still counted


def test_no_active_task_is_noop():
    set_current_task(None)
    # No catalog, no domain rate, no session — must not raise, must not record.
    _handle_http_call("https://api.uncataloged.com/x", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(200, body_len=500_000), latency_ms=10)
    assert get_recorded_events() == []


def test_handler_failure_is_swallowed_and_counted():
    task = _task()
    set_current_task(task)
    # response.headers raising → measurement throws → swallowed + counted.
    class _Bad:
        status_code = 200
        @property
        def headers(self):
            raise RuntimeError("boom")
    _handle_http_call("https://api.uncataloged.com/x", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Bad(), latency_ms=1)
    assert get_network_error_count() >= 1  # observable, not hidden
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_capture.py -v`
Expected: FAIL — `ImportError: cannot import name '_handle_http_call'`

- [ ] **Step 3: Add network state + helpers to the adapter**

In `python/src/dexcost/adapters/http.py`, add to the imports block:

```python
import time
from urllib.parse import urlparse

from dexcost.adapters._netbytes import classify_destination, measure_bytes_from_headers
from dexcost.config import DexcostConfig
from dexcost.context import get_current_task, is_network_event_suppressed
```

(Replace the existing `from dexcost.context import get_current_task` line with the combined import above; `urlparse` is already imported — keep one copy.)

After the `_in_patched_call` definition, add:

```python
# Active config — wired by set_network_config(); falls back to defaults.
_network_config: DexcostConfig | None = None

# Count of exceptions swallowed by network accounting — surfaced by
# get_network_error_count() so silent capture failure is observable.
_network_error_count = 0


def set_network_config(config: DexcostConfig | None) -> None:
    """Wire the adapter to the SDK config (thresholds, on/off toggles)."""
    global _network_config
    _network_config = config


def _cfg() -> DexcostConfig:
    """Return the active config, or a defaults instance if none wired."""
    global _network_config
    if _network_config is None:
        _network_config = DexcostConfig(storage="local")
    return _network_config


def get_network_error_count() -> int:
    """Number of exceptions swallowed by network accounting since reset."""
    return _network_error_count


def reset_network_error_count() -> None:
    """Reset the swallowed-exception counter (tests / `dexcost status`)."""
    global _network_error_count
    _network_error_count = 0
```

- [ ] **Step 4: Add the response-size helper**

In `http.py`, after `_get_response_body`, add:

```python
def _response_body_len(response: Any) -> int:
    """Best-effort response body length in bytes.

    Uses the ``Content-Length`` header when present; otherwise falls back to
    the length of an already-materialised body. Never forces a stream read.
    """
    headers = _get_response_headers(response)
    for key, value in headers.items():
        if key.lower() == "content-length":
            try:
                return max(0, int(value))
            except (ValueError, TypeError):
                break
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return len(content)
    return 0
```

- [ ] **Step 5: Add `_handle_http_call` (the unified handler)**

In `http.py`, replace the entire `_maybe_record_cost` function with `_handle_http_call`:

```python
def _handle_http_call(
    url: str,
    *,
    method: str = "GET",
    request_headers: dict[str, Any] | None = None,
    request_body_len: int = 0,
    response: Any = None,
    latency_ms: int = 0,
) -> None:
    """Record cost + network bytes for one instrumented HTTP call.

    Fail-silent: any exception is swallowed and counted (see
    get_network_error_count) so a measurement bug never breaks the call.
    """
    try:
        _handle_http_call_inner(
            url, method, request_headers or {}, request_body_len, response, latency_ms
        )
    except Exception:  # noqa: BLE001 - byte capture must never break the call
        global _network_error_count
        _network_error_count += 1
        _log.warning("network capture failed for %s", url, exc_info=True)


def _resolve_task() -> Any | None:
    """Return the active task, or an auto-session task, or None."""
    task = get_current_task()
    if task is not None:
        return task
    session_mgr = get_session_manager()
    return session_mgr.get_or_create_session("http_call", _storage)


def _handle_http_call_inner(
    url: str,
    method: str,
    request_headers: dict[str, Any],
    request_body_len: int,
    response: Any,
    latency_ms: int,
) -> None:
    parsed = urlparse(str(url))
    domain = parsed.hostname or ""
    protocol = parsed.scheme or "https"

    task = _resolve_task()
    if task is None:
        return  # anonymous traffic — never create orphan rows

    # ── byte measurement ──────────────────────────────────────────────
    bytes_out = measure_bytes_from_headers(method, url, request_headers, request_body_len)
    response_headers = _get_response_headers(response) if response is not None else {}
    response_body_len = _response_body_len(response) if response is not None else 0
    bytes_in = measure_bytes_from_headers("", "", response_headers, response_body_len)
    status_code = int(getattr(response, "status_code", 0) or 0)

    # ── always: feed the task's byte counters (lossless) ──────────────
    task._network.record(domain, bytes_in=bytes_in, bytes_out=bytes_out)

    byte_details = {
        "protocol": protocol,
        "request_bytes": bytes_out,
        "response_bytes": bytes_in,
        "is_internal_traffic": classify_destination(domain),
    }

    # ── 1. user-registered domain rate (cataloged) ────────────────────
    rate = _domain_rates.get(domain)
    if rate is not None:
        event = Event(
            task_id=task.task_id, event_type="external_cost",
            cost_usd=rate["cost_usd"], cost_confidence="exact",
            pricing_source="rate_registry", service_name=domain,
            details={"url": url, "per": rate["per"], **byte_details},
        )
        _persist_event(event)
        return

    # ── 2. service-catalog match (cataloged) ──────────────────────────
    catalog = get_catalog()
    entry = catalog.lookup(url)
    if entry is not None:
        result = catalog.extract_cost(
            entry, response_headers, _get_response_body(response) if response else None
        )
        if result is not None:
            event = Event(
                task_id=task.task_id, event_type="external_cost",
                cost_usd=result.amount, cost_confidence=result.confidence,
                pricing_source=result.pricing_source,
                pricing_version=catalog.catalog_version,
                service_name=result.service_name,
                details={"url": url, **byte_details},
            )
        else:
            event = Event(
                task_id=task.task_id, event_type="external_cost",
                cost_usd=Decimal("0"), cost_confidence="unknown",
                pricing_source="service_catalog", service_name=entry.display_name,
                details={"url": url, **byte_details},
            )
        _persist_event(event)
        return

    # ── 3. un-cataloged — emit a `network` event when notable ─────────
    if is_network_event_suppressed():
        return  # the `llm_call` event already represents this call

    cfg = _cfg()
    notable = (
        (bytes_in + bytes_out) > cfg.network_event_threshold_bytes
        or (cfg.network_event_on_error and status_code >= 400)
        or (cfg.network_event_latency_ms > 0 and latency_ms > cfg.network_event_latency_ms)
    )
    if not notable:
        return  # counters already updated; below threshold → no event

    event = Event(
        task_id=task.task_id, event_type="network",
        cost_usd=Decimal("0"), cost_confidence="unknown",
        pricing_source=None, service_name=domain,
        details={"url": url, "method": method, "status_code": status_code, **byte_details},
    )
    _persist_event(event)
```

- [ ] **Step 6: Update the five wrappers to time the call and pass request data**

In `http.py`, replace `_requests_wrapper`:

```python
def _requests_wrapper(wrapped, instance, args, kwargs):
    """wrapt wrapper for ``requests.Session.send``."""
    _in_patched_call.active = True
    t0 = time.monotonic()
    try:
        response = wrapped(*args, **kwargs)
    finally:
        _in_patched_call.active = False
    latency_ms = int((time.monotonic() - t0) * 1000)
    if args:
        req = args[0]
        url = str(getattr(req, "url", "") or "")
        body = getattr(req, "body", None)
        body_len = len(body) if isinstance(body, (bytes, bytearray, str)) else 0
        headers = {str(k): str(v) for k, v in getattr(req, "headers", {}).items()}
        _handle_http_call(url, method=str(getattr(req, "method", "GET")),
                          request_headers=headers, request_body_len=body_len,
                          response=response, latency_ms=latency_ms)
    return response
```

Replace `_httpx_wrapper`:

```python
def _httpx_wrapper(wrapped, instance, args, kwargs):
    """wrapt wrapper for ``httpx.Client.send``."""
    _in_patched_call.active = True
    t0 = time.monotonic()
    try:
        response = wrapped(*args, **kwargs)
    finally:
        _in_patched_call.active = False
    latency_ms = int((time.monotonic() - t0) * 1000)
    if args:
        req = args[0]
        url = str(getattr(req, "url", "") or "")
        content = getattr(req, "content", None)
        body_len = len(content) if isinstance(content, (bytes, bytearray)) else 0
        headers = {str(k): str(v) for k, v in getattr(req, "headers", {}).items()}
        _handle_http_call(url, method=str(getattr(req, "method", "GET")),
                          request_headers=headers, request_body_len=body_len,
                          response=response, latency_ms=latency_ms)
    return response
```

Replace `_aiohttp_wrapper`:

```python
async def _aiohttp_wrapper(wrapped, instance, args, kwargs):
    """wrapt wrapper for ``aiohttp.ClientSession._request``."""
    t0 = time.monotonic()
    response = await wrapped(*args, **kwargs)
    latency_ms = int((time.monotonic() - t0) * 1000)
    method = str(args[0]) if args else str(kwargs.get("method", "GET"))
    url = str(args[1]) if len(args) > 1 else str(kwargs.get("str_or_url", ""))
    _handle_http_call(url, method=method, request_headers={},
                      request_body_len=0, response=response, latency_ms=latency_ms)
    return response
```

Replace `_botocore_wrapper`:

```python
def _botocore_wrapper(wrapped, instance, args, kwargs):
    """wrapt wrapper for ``botocore.httpsession.URLLib3Session.send``."""
    _in_patched_call.active = True
    t0 = time.monotonic()
    try:
        response = wrapped(*args, **kwargs)
    finally:
        _in_patched_call.active = False
    latency_ms = int((time.monotonic() - t0) * 1000)
    if args:
        req = args[0]
        url = str(getattr(req, "url", "") or "")
        body = getattr(req, "body", None)
        body_len = len(body) if isinstance(body, (bytes, bytearray, str)) else 0
        _handle_http_call(url, method=str(getattr(req, "method", "GET")),
                          request_headers={}, request_body_len=body_len,
                          response=response, latency_ms=latency_ms)
    return response
```

Replace `_urllib3_wrapper`:

```python
def _urllib3_wrapper(wrapped, instance, args, kwargs):
    """wrapt wrapper for ``urllib3.HTTPConnectionPool.urlopen``."""
    if getattr(_in_patched_call, "active", False):
        return wrapped(*args, **kwargs)  # nested in requests/botocore — skip

    t0 = time.monotonic()
    response = wrapped(*args, **kwargs)
    latency_ms = int((time.monotonic() - t0) * 1000)

    method = str(args[0]) if args else str(kwargs.get("method", "GET"))
    url_path = args[1] if len(args) > 1 else kwargs.get("url", "")
    scheme = getattr(instance, "scheme", "https")
    host = getattr(instance, "host", "")
    port = getattr(instance, "port", None)
    if port and port not in (80, 443):
        full_url = f"{scheme}://{host}:{port}{url_path}"
    else:
        full_url = f"{scheme}://{host}{url_path}"
    _handle_http_call(full_url, method=method, request_headers={},
                      request_body_len=0, response=response, latency_ms=latency_ms)
    return response
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_capture.py -v`
Expected: PASS (all 8 tests)

- [ ] **Step 8: Update the existing HTTP-adapter tests for the behaviour change**

Run the existing suite: `cd python && python -m pytest tests/test_http_adapter.py tests/test_http_adapter_v2.py -v`

Any test that calls the (now-removed) `_maybe_record_cost` must call `_handle_http_call(url, response=...)` instead. Any test asserting an **un-cataloged** call produces an `external_cost` event must be updated to expect either a `network` event (if the stubbed response body is ≥ 100 KiB or status ≥ 400) or **no** event (small successful response) — per the deliberate behaviour change in spec §4.4. Cataloged-host assertions are unchanged. Fix each failing test accordingly, then re-run until green.

- [ ] **Step 9: Commit**

```bash
git add python/src/dexcost/adapters/http.py python/tests/test_network_capture.py python/tests/test_http_adapter.py python/tests/test_http_adapter_v2.py
git commit -m "feat(network): HTTP adapter byte accounting + re-typed un-cataloged calls"
```

---

### Task 10: LLM instruments set the suppression flag

**Files:**
- Modify: `python/src/dexcost/instruments/openai.py`, `anthropic.py`, `bedrock.py`, `gemini.py`, `cohere.py`, `litellm.py`, `mcp.py`
- Test: `python/tests/test_network_llm_suppression.py` (create)

Each instrument wraps a provider call that internally makes an HTTP request the
adapter also sees. Wrapping the provider call in `suppress_network_event()`
means the inner HTTP call records bytes but emits no standalone `network`
event — the `llm_call` event already represents it.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_llm_suppression.py`:

```python
"""An LLM call must not also produce a standalone network event."""

import uuid
from datetime import datetime, timezone

import pytest

from dexcost.adapters.http import (
    _handle_http_call, clear_recorded_events, get_recorded_events,
)
from dexcost.context import set_current_task
from dexcost.models.task import Task


@pytest.fixture(autouse=True)
def _clean():
    clear_recorded_events()
    set_current_task(None)
    yield
    clear_recorded_events()
    set_current_task(None)


class _Resp:
    status_code = 200
    headers = {"Content-Type": "application/json", "Content-Length": "400000"}

    def json(self):
        return {}


def test_llm_instrument_wraps_call_in_suppression(monkeypatch):
    """Smoke test: importing the instruments exposes suppress usage.

    The behavioural guarantee is exercised by test_network_capture.py
    (test_suppressed_call_records_bytes_but_no_network_event). Here we
    assert each instrument module references suppress_network_event so a
    future edit that drops it fails CI.
    """
    import inspect

    from dexcost.instruments import (
        anthropic, bedrock, cohere, gemini, litellm, mcp, openai,
    )

    for module in (openai, anthropic, bedrock, gemini, cohere, litellm, mcp):
        src = inspect.getsource(module)
        assert "suppress_network_event" in src, (
            f"{module.__name__} must wrap its HTTP call in suppress_network_event()"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_llm_suppression.py -v`
Expected: FAIL — `AssertionError: openai must wrap ...`

- [ ] **Step 3: Wrap each instrument's provider call**

For **each** of the seven instrument files, locate the wrapper function that
calls the real provider method (the `wrapt` wrapper that invokes
`wrapped(*args, **kwargs)` — the actual network call). Add the import at the
top of the file:

```python
from dexcost.context import suppress_network_event
```

Then wrap the line that invokes the provider. For a synchronous wrapper, change:

```python
    response = wrapped(*args, **kwargs)
```

to:

```python
    with suppress_network_event():
        response = wrapped(*args, **kwargs)
```

For an `async` wrapper, change:

```python
    response = await wrapped(*args, **kwargs)
```

to:

```python
    with suppress_network_event():
        response = await wrapped(*args, **kwargs)
```

Apply this to every wrapper in each file (sync and async variants both — e.g.
`openai.py` has Completions and AsyncCompletions; wrap both). For streaming
wrappers, wrap only the call that *initiates* the stream (the `wrapped(...)`
that returns the stream object), not the iteration.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_llm_suppression.py -v`
Expected: PASS

- [ ] **Step 5: Run all instrument tests for regression**

Run: `cd python && python -m pytest tests/test_openai_instrument.py tests/test_anthropic_instrument.py tests/test_bedrock_instrument.py tests/test_gemini_instrument.py tests/test_cohere_instrument.py tests/test_litellm_instrument.py tests/test_mcp_instrument.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add python/src/dexcost/instruments/ python/tests/test_network_llm_suppression.py
git commit -m "feat(network): LLM instruments suppress duplicate network events"
```

---

### Task 11: Wire `track_network` config into `init()`

**Files:**
- Modify: `python/src/dexcost/__init__.py` (the `init()` function)
- Test: `python/tests/test_network_init_wiring.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_network_init_wiring.py`:

```python
"""init() wires the SDK config into the HTTP network adapter."""

import dexcost
from dexcost.adapters import http as http_adapter


def test_init_wires_network_config(tmp_path):
    dexcost.close()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "b.db"),
                 network_event_threshold_bytes=4096)
    cfg = http_adapter._cfg()
    assert cfg.network_event_threshold_bytes == 4096
    assert cfg.track_network is True
    dexcost.close()


def test_init_track_network_false_disables_threshold_path(tmp_path):
    dexcost.close()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "b.db"),
                 track_network=False)
    assert http_adapter._cfg().track_network is False
    dexcost.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_network_init_wiring.py -v`
Expected: FAIL — `init()` does not accept `network_event_threshold_bytes`

- [ ] **Step 3: Thread the params through `init()`**

In `python/src/dexcost/__init__.py`, add four parameters to the `init()`
signature (alongside the existing ones such as `track_http`):

```python
    track_network: bool = True,
    network_event_threshold_bytes: int = 102_400,
    network_event_on_error: bool = True,
    network_event_latency_ms: int = 0,
```

Pass them into the `DexcostConfig(...)` constructor call inside `init()`:

```python
        track_network=track_network,
        network_event_threshold_bytes=network_event_threshold_bytes,
        network_event_on_error=network_event_on_error,
        network_event_latency_ms=network_event_latency_ms,
```

In the block where `init()` already calls `track_http()` and `set_storage(...)`
on the HTTP adapter, add — right after `set_storage(...)`:

```python
        from dexcost.adapters.http import set_network_config
        set_network_config(_global_config)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && python -m pytest tests/test_network_init_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd python && python -m pytest tests/ --ignore=tests/test_e2e_local.py -q`
Expected: PASS — all tests green (the `test_e2e_local.py` Docker suite is excluded; it is unrelated and infra-gated).

- [ ] **Step 6: Commit**

```bash
git add python/src/dexcost/__init__.py python/tests/test_network_init_wiring.py
git commit -m "feat(network): wire network-capture config through init()"
```

---

## Self-Review

**Spec coverage** — every spec section maps to a task:

| Spec section | Task(s) |
|---|---|
| §4.1 Task fields | 2, 3 |
| §4.2 `network` event type + `details` (protocol, is_internal_traffic) | 1, 9 |
| §4.3 uniform byte placement on both event types | 9 (`byte_details` spread into both `external_cost` and `network`) |
| §4.4 emission rule (incl. re-typed un-cataloged calls) | 9 |
| §4.5 schema changes | 1, 2, 3 |
| §5.1 `NetworkAccountant`, byte measurement, config | 4, 5, 7 |
| §5.2 thread-safety (Python `threading.Lock`) | 5 (`NetworkAccountant._lock`) |
| §5.3 ≤1-event invariant + context flag | 8, 9, 10 |
| §5.4 per-call flow | 9 |
| §5.5 streaming / Content-Length fallback | 4 (`measure_bytes_from_headers`), 9 (`_response_body_len`) |
| §6.1 fail-silent + error counter | 9 (`_handle_http_call` try/except + `_network_error_count`) |
| §6.2 no-task no-op | 9 (`_resolve_task` / `test_no_active_task_is_noop`) |
| §6.3 double-count guard reuse | 9 (`_in_patched_call` preserved in all wrappers) |
| §6.5 live cap (~500) | 5 (`LIVE_CAP`) |
| §6.7 snapshot-and-freeze | 5 (`_frozen`) |
| §7 tests incl. zero-call task | 6 (`test_zero_call_task...`), all test files |

**Placeholder scan** — no `TBD`/`TODO`; the two schema-JSON edits (Task 1 step 4, Task 2 step 6) and the instrument edits (Task 10 step 3) are described as exact, mechanical changes because those files were not read verbatim — each step names the precise edit. Task 9 step 8 and Task 10 step 3 require reading the target file before editing; this is inherent to a behaviour change across many small files and is called out explicitly.

**Type consistency** — `NetworkAccountant.finalize()` returns `{"bytes_in", "bytes_out", "call_count", "by_host"}`; Task 6 step 4 reads exactly those keys. `network_by_host` is `{"hosts": [...]}` everywhere (Task 2, 5, 6). `_handle_http_call` keyword signature in Task 9 step 5 matches every call site in the wrappers (step 6) and the tests (step 1).

**Known follow-ups (out of scope, not gaps):** the Go/Rust/TS ports (separate plans, per the spec's Python-first rollout); exposing `get_network_error_count()` in a `dexcost status` CLI command (the counter exists and is tested; surfacing it in CLI output is a one-line add deferred to the status-command owner).
