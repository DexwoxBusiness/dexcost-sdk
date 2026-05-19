"""Waste detection example -- retry tracking and waste metrics.

Shows how retries are recorded as first-class events and how the task
aggregates retry_count and retry_cost_usd.

Usage:
    pip install dexcost
    python examples/waste_detection.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from dexcost import CostTracker
from dexcost.storage.sqlite import SQLiteStorage

# Use a temp directory so we never write to ~/.dexcost/
db_path = Path(tempfile.mkdtemp()) / "waste.db"
storage = SQLiteStorage(db_path=db_path)
tracker = CostTracker(storage=storage, auto_instrument=[])

# Simulate a task that encounters retries
with tracker.task(task_type="resolve_ticket", customer_id="acme-corp") as t:
    # First LLM call -- rate-limited, wasted
    t.record_llm_call(
        "openai", "gpt-4o",
        input_tokens=500, output_tokens=0,
        cost_usd="0.005",
        error_type="rate_limit",
    )
    # Manually mark the retry
    t.mark_retry(reason="rate_limit", cost_usd="0.005")

    # Second attempt -- timeout, also wasted
    t.record_llm_call(
        "openai", "gpt-4o",
        input_tokens=500, output_tokens=0,
        cost_usd="0.005",
        error_type="timeout",
    )
    t.mark_retry(reason="timeout", cost_usd="0.005")

    # Third attempt -- succeeds
    t.record_llm_call(
        "openai", "gpt-4o",
        input_tokens=500, output_tokens=200,
        cost_usd="0.008",
    )

    # Also record a non-LLM cost
    t.record_cost(service="search_api", cost_usd="0.001")

# Fetch the completed task
task = storage.get_task(str(t.task_id))
assert task is not None

print("=== Waste Detection Report ===")
print(f"Task:          {task.task_type} ({task.status})")
print(f"Total cost:    ${task.total_cost_usd}")
print(f"LLM cost:      ${task.llm_cost_usd}")
print(f"Retry count:   {task.retry_count}")
print(f"Retry waste:   ${task.retry_cost_usd}")
print(f"Useful spend:  ${task.total_cost_usd - task.retry_cost_usd}")

if task.total_cost_usd > 0:
    waste_pct = (task.retry_cost_usd / task.total_cost_usd) * 100
    print(f"Waste ratio:   {waste_pct:.1f}%")

# List all events for the task
events = storage.query_events(task_id=str(t.task_id))
print(f"\n{'Type':<15} {'Cost':>8} {'Retry?':>7} {'Reason':<15}")
print("-" * 50)
for event in reversed(events):  # chronological order
    retry_str = "yes" if event.is_retry else "no"
    reason_str = event.retry_reason or ""
    print(f"{event.event_type:<15} ${event.cost_usd:>7} {retry_str:>7} {reason_str:<15}")

print(f"\nDB path: {db_path}")
storage.close()
