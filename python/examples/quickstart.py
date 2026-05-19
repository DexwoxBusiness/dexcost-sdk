"""Quickstart example -- from import to first tracked task in 15 lines.

Usage:
    pip install dexcost
    python examples/quickstart.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from dexcost import CostTracker
from dexcost.storage.sqlite import SQLiteStorage

# Use a temp directory so we never write to ~/.dexcost/
db_path = Path(tempfile.mkdtemp()) / "quickstart.db"
storage = SQLiteStorage(db_path=db_path)
tracker = CostTracker(storage=storage, auto_instrument=[])

# Track a task using the context manager
with tracker.task(task_type="summarise_doc", customer_id="acme-corp") as t:
    # Record an LLM call (cost auto-computed from bundled pricing)
    t.record_llm_call("openai", "gpt-4o", input_tokens=800, output_tokens=150)
    # Record a non-LLM service fee
    t.record_cost(service="pdf_parser", cost_usd="0.002")

# Fetch the completed task and inspect the aggregated costs
task = storage.get_task(str(t.task_id))
assert task is not None

print(f"Task:       {task.task_type} ({task.status})")
print(f"Customer:   {task.customer_id}")
print(f"Total cost: ${task.total_cost_usd}")
print(f"LLM cost:   ${task.llm_cost_usd}")
print(f"Ext. cost:  ${task.external_cost_usd}")
print(f"Tokens:     {task.total_input_tokens} in / {task.total_output_tokens} out")
print(f"DB path:    {db_path}")

storage.close()
