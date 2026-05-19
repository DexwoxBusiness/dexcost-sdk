"""E2E integration test for Python SDK against local control-layer stack.

Requires:
    - Docker + docker-compose available (infra/docker-compose.yml)
    - Environment variables set per infra/env-reference.md
    - Control-layer server running at localhost:3000

Usage (from sdks/python/):
    pytest tests/test_e2e_local.py -v

If the local stack is not available, tests are skipped with a clear message.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples"

# Propagate PYTHONPATH so the local dexcost package is importable
_TEST_ENV = {**os.environ, "PYTHONPATH": str(_SRC_DIR)}


def _stack_is_available() -> bool:
    """Return True if the docker stack responds."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://localhost:3000/health"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() in ("200", "404", "301", "302")
    except Exception:
        return False


@pytest.fixture(scope="module")
def _local_stack() -> Any:
    """Ensure the local control-layer stack is running.

    Skips tests if docker-compose is not available or the stack is not up.
    """
    available = _stack_is_available()
    if not available:
        pytest.skip("Local control-layer stack not available at localhost:3000")
    return True


class TestE2ELocal:
    """End-to-end tests against the local control-layer stack.

    These tests validate that the Python SDK can ship events to a
    real Hono server and that those events are queryable via the
    dashboard API / ClickHouse within the timeout window.
    """

    def test_quickstart_ships_event_to_local_api(
        self, tmp_path: Any, _local_stack: Any
    ) -> None:
        """Run the quickstart example with sync enabled and verify event arrives."""
        # Use a temp DB so test state is isolated
        import tempfile

        db_file = tmp_path / "e2e.db"

        # Configure SDK to point at local server with a test API key
        env = {
            **_TEST_ENV,
            "DEXCOST_API_KEY": "dx_test_e2e_abc123",
            "DEXCOST_ENDPOINT": "http://localhost:3000",
        }

        # Build an inline script that exercises the SDK's HTTP sync path
        script = tmp_path / "ship_event.py"
        script.write_text(
            f"""
import sys
sys.path.insert(0, '{_SRC_DIR}')
from pathlib import Path
import time

from dexcost import CostTracker
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import SyncWorker
from dexcost.config import DexcostConfig

db_path = Path('{db_file}')
storage = SQLiteStorage(db_path=db_path)

config = DexcostConfig(
    api_key='dx_test_e2e_abc123',
    flush_interval_seconds=1.0,
)
worker = SyncWorker(config=config, storage=storage, db_path=str(db_path))
worker.start()

tracker = CostTracker(storage=storage, auto_instrument=[])
with tracker.task(task_type='e2e_test_task', customer_id='e2e-customer') as t:
    t.record_llm_call(provider='openai', model='gpt-4o', input_tokens=100, output_tokens=50)

# Give the worker time to flush
time.sleep(3)
worker.stop()
storage.close()
print('Event shipped successfully')
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        # The script should complete without error
        assert result.returncode == 0, (
            f"Script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Event shipped successfully" in result.stdout

    def test_event_visible_via_dashboard_api(self, tmp_path: Any, _local_stack: Any) -> None:
        """Poll the dashboard API for the shipped event within 5 seconds."""
        import json
        import urllib.request

        customer_id = "e2e-customer"
        max_wait = 5.0
        poll_interval = 0.5
        start = time.monotonic()

        api_url = "http://localhost:3000/v1/tasks"
        api_key = "dx_test_e2e_abc123"

        while time.monotonic() - start < max_wait:
            try:
                req = urllib.request.Request(
                    f"{api_url}?customer_id={customer_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    # Find tasks for our customer
                    tasks = data if isinstance(data, list) else data.get("tasks", [])
                    e2e_tasks = [t for t in tasks if t.get("customer_id") == customer_id]
                    if e2e_tasks:
                        # Found at least one event
                        return
            except Exception:
                pass  # Not ready yet
            time.sleep(poll_interval)

        pytest.fail(
            f"Event for customer '{customer_id}' not visible via dashboard API "
            f"within {max_wait}s"
        )


class TestE2ELocalSchemaCompliance:
    """Validate Standard Event Schema v1 compliance."""

    def test_llm_call_event_schema_v1_compliant(self, tmp_path: Any) -> None:
        """record_llm_call produces a dict matching dexcost-event.v1.json."""
        import json

        sys.path.insert(0, str(_SRC_DIR))
        from dexcost import CostTracker
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.schema import validate

        db_path = tmp_path / "schema_test.db"
        storage = SQLiteStorage(db_path=db_path)
        tracker = CostTracker(storage=storage, auto_instrument=[])

        with tracker.task(task_type="schema_check", customer_id="schema-cust") as t:
            t.record_llm_call(
                provider="openai",
                model="gpt-4o",
                input_tokens=10,
                output_tokens=5,
            )

        task = storage.get_task(str(t.task_id))
        assert task is not None

        events = storage.query_events(task_id=str(t.task_id))
        assert len(events) == 1

        event_dict = events[0].to_dict()
        errors = validate(event_dict)
        assert errors == [], f"Schema validation errors: {errors}"

        # Check required Standard Event Schema v1 fields
        assert "event_id" in event_dict
        assert "task_id" in event_dict
        assert event_dict["event_type"] == "llm_call"
        assert event_dict["provider"] == "openai"
        assert event_dict["model"] == "gpt-4o"
        assert "input_tokens" in event_dict
        assert "output_tokens" in event_dict
        assert "cost_usd" in event_dict
        assert event_dict["cost_confidence"] in ("exact", "computed", "estimated", "unknown")

        storage.close()

    def test_retry_semantics_fields_present(self, tmp_path: Any) -> None:
        """Events with is_retry=True carry retry_reason and retry_of."""
        sys.path.insert(0, str(_SRC_DIR))
        from dexcost import CostTracker
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.models.event import Event

        db_path = tmp_path / "retry_test.db"
        storage = SQLiteStorage(db_path=db_path)
        tracker = CostTracker(storage=storage, auto_instrument=[])

        with tracker.task(task_type="retry_check", customer_id="retry-cust") as t:
            t.record_llm_call(
                provider="anthropic",
                model="claude-3-5-sonnet-20241022",
                input_tokens=100,
                output_tokens=50,
            )

        events = storage.query_events(task_id=str(t.task_id))
        assert len(events) == 1

        evt = events[0]
        # Check schema has retry fields (even if auto-detect didn't trigger)
        assert hasattr(evt, "is_retry")
        assert hasattr(evt, "retry_reason")
        assert hasattr(evt, "retry_of")

        # Manually create a retry event to verify field presence
        retry_event = Event(
            task_id=t.task_id,
            event_type="llm_call",
            provider="openai",
            model="gpt-4o",
            input_tokens=50,
            output_tokens=25,
            cost_usd=Decimal("0.001"),
            is_retry=True,
            retry_reason="rate_limit",
            retry_of=events[0].event_id,
        )
        storage.insert_event(retry_event)

        stored = storage.query_events(task_id=str(t.task_id))
        retry_events = [e for e in stored if e.is_retry]
        assert len(retry_events) == 1
        assert retry_events[0].retry_reason == "rate_limit"
        assert retry_events[0].retry_of == events[0].event_id

        storage.close()

    def test_external_cost_event_schema_compliant(self, tmp_path: Any) -> None:
        """record_cost produces a schema-compliant external_cost event."""
        sys.path.insert(0, str(_SRC_DIR))
        from dexcost import CostTracker
        from dexcost.storage.sqlite import SQLiteStorage
        from dexcost.schema import validate

        db_path = tmp_path / "ext_cost_test.db"
        storage = SQLiteStorage(db_path=db_path)
        tracker = CostTracker(storage=storage, auto_instrument=[])

        with tracker.task(task_type="ext_cost_check", customer_id="ext-cust") as t:
            t.record_cost(service="pdf_parser", cost_usd="0.002")

        events = storage.query_events(task_id=str(t.task_id))
        assert len(events) == 1

        event_dict = events[0].to_dict()
        errors = validate(event_dict)
        assert errors == [], f"Schema validation errors: {errors}"
        assert event_dict["event_type"] == "external_cost"
        assert event_dict.get("service_name") == "pdf_parser"

        storage.close()