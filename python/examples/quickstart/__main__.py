"""Dexcost Python SDK — minimal quickstart.

Usage (from sdks/python/):
    pip install -e .
    python -m examples.quickstart

Or, once published to PyPI:
    pip install dexcost
    python -m examples.quickstart
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from dexcost import CostTracker
from dexcost.storage.sqlite import SQLiteStorage

# ── 1. Storage (temp file — never ~/.dexcost/) ──────────────────────────────
db_path = Path(tempfile.mkdtemp()) / "quickstart.db"
storage = SQLiteStorage(db_path=db_path)

# ── 2. Tracker ──────────────────────────────────────────────────────────────
tracker = CostTracker(storage=storage, auto_instrument=[])

# ── 3. Track a task ──────────────────────────────────────────────────────────
with tracker.task(task_type="summarise_doc", customer_id="acme-corp") as t:
    # Record an LLM call (cost auto-computed from bundled model pricing)
    t.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=800,
        output_tokens=150,
    )
    # Record a non-LLM external service cost
    t.record_cost(service="pdf_parser", cost_usd="0.002")

# ── 4. Inspect aggregated results ───────────────────────────────────────────
task = storage.get_task(str(t.task_id))
assert task is not None

print("=== Dexcost Quickstart ===")
print(f"Task:       {task.task_type}  ({task.status})")
print(f"Customer:   {task.customer_id}")
print(f"Total cost: ${task.total_cost_usd}")
print(f"  LLM cost: ${task.llm_cost_usd}")
print(f"  Ext cost: ${task.external_cost_usd}")
print(f"Tokens:     {task.total_input_tokens} in / {task.total_output_tokens} out")
print(f"DB:         {db_path}")

storage.close()