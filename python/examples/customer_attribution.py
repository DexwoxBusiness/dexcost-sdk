"""Customer attribution example -- per-customer cost breakdown.

Shows how to track tasks for multiple customers and query costs grouped
by customer_id.

Usage:
    pip install dexcost
    python examples/customer_attribution.py
"""

from __future__ import annotations

import tempfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from dexcost import CostTracker
from dexcost.storage.sqlite import SQLiteStorage

# Use a temp directory so we never write to ~/.dexcost/
db_path = Path(tempfile.mkdtemp()) / "attribution.db"
storage = SQLiteStorage(db_path=db_path)
tracker = CostTracker(storage=storage, auto_instrument=[])

# Simulate tasks for different customers
customers = {
    "acme-corp": [
        ("resolve_ticket", "gpt-4o", 500, 120),
        ("resolve_ticket", "gpt-4o", 300, 80),
    ],
    "globex-inc": [
        ("generate_report", "gpt-4o-mini", 1200, 400),
    ],
    "initech": [
        ("summarise_doc", "gpt-4o", 900, 200),
        ("summarise_doc", "gpt-4o", 600, 150),
        ("resolve_ticket", "gpt-4o-mini", 400, 100),
    ],
}

for customer_id, tasks in customers.items():
    for task_type, model, inp, out in tasks:
        with tracker.task(task_type=task_type, customer_id=customer_id) as t:
            t.record_llm_call("openai", model, input_tokens=inp, output_tokens=out)

# Query and display per-customer cost breakdown
print(f"{'Customer':<15} {'Tasks':>5} {'Total Cost':>12} {'LLM Cost':>12} {'Tokens':>10}")
print("-" * 60)

cost_by_customer: dict[str, dict[str, Decimal | int]] = defaultdict(
    lambda: {"total": Decimal("0"), "llm": Decimal("0"), "tokens": 0, "count": 0}
)

for customer_id in customers:
    tasks = storage.query_tasks(customer_id=customer_id)
    for task in tasks:
        entry = cost_by_customer[customer_id]
        entry["total"] += task.total_cost_usd
        entry["llm"] += task.llm_cost_usd
        entry["tokens"] += task.total_input_tokens + task.total_output_tokens  # type: ignore[operator]
        entry["count"] += 1  # type: ignore[operator]

for customer_id in sorted(cost_by_customer):
    entry = cost_by_customer[customer_id]
    print(
        f"{customer_id:<15} {entry['count']:>5} "
        f"${str(entry['total']):>11} "
        f"${str(entry['llm']):>11} "
        f"{entry['tokens']:>10}"
    )

print(f"\nDB path: {db_path}")
storage.close()
