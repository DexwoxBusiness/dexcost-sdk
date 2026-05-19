"""Agent cost capture example -- LLM calls, tool costs, and retry waste.

Shows how to wire dexcost around a simulated local AI agent:
  1. Records LLM call costs (provider: "local", model: "local-llm")
  2. Records non-LLM tool costs (web search, maps API)
  3. Demonstrates retry waste tracking (simulated rate-limit retry)
  4. Verifies all events appear in the buffer with correct schema fields.

No API key required -- runs in offline mode.

Usage:
    pip install dexcost
    python examples/agent_cost.py
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from dexcost import CostTracker
from dexcost.storage.sqlite import SQLiteStorage

# Use a temp directory so we never write to ~/.dexcost/
db_path = Path(tempfile.mkdtemp()) / "agent_cost.db"
storage = SQLiteStorage(db_path=db_path)
tracker = CostTracker(storage=storage, auto_instrument=[])


def simulate_llm_call(prompt_tokens: int) -> tuple[int, int, bool]:
    """Simulates a local LLM call. Returns (output_tokens, latency_ms, should_retry)."""
    output_tokens = prompt_tokens * 3  # 3x token amplification
    latency_ms = 180
    should_retry = random.random() > 0.77  # ~23% retry rate
    return output_tokens, latency_ms, should_retry


def simulate_tool_call(tool: str) -> tuple[str, str, dict]:
    """Returns (service_name, cost_usd, details)."""
    if tool == "web_search":
        return "web_search", "0.002", {"query": "weather forecast", "results_count": "5"}
    elif tool == "maps_api":
        return "maps_api", "0.005", {"operation": "route", "waypoints": "3"}
    else:
        return "unknown", "0", {}


print("[dexcost] Initializing SDK (offline mode)...")

# ── Start a task for the agent run ─────────────────────────────────
with tracker.task(
    task_type="local_agent_task",
    customer_id="demo-corp",
    project_id="agent-demo",
    metadata={"agent_framework": "dexcost-demo"},
) as task:
    print(f"[dexcost] Task started: {task.task_id}")

    # ── Step 1: Initial LLM call ─────────────────────────────────────────
    prompt_tokens = 150
    output_tokens, latency_ms, should_retry = simulate_llm_call(prompt_tokens)

    llm_event = task.record_llm_call(
        "local",
        "local-llm",
        input_tokens=prompt_tokens,
        output_tokens=output_tokens,
        cost_usd="0.00075",
        latency_ms=latency_ms,
    )
    print(
        f"[dexcost] LLM call recorded: {prompt_tokens} input + "
        f"{output_tokens} output tokens, cost=${llm_event.cost_usd}, latency={latency_ms}ms"
    )

    # ── Step 2: Non-LLM tool calls ──────────────────────────────────────
    service, cost, details = simulate_tool_call("web_search")
    tool_event = task.record_cost(service=service, cost_usd=cost, details=details)
    print(f"[dexcost] Tool cost recorded: {service} cost=${tool_event.cost_usd}")

    service2, cost2, details2 = simulate_tool_call("maps_api")
    tool_event2 = task.record_cost(service=service2, cost_usd=cost2, details=details2)
    print(f"[dexcost] Tool cost recorded: {service2} cost=${tool_event2.cost_usd}")

    # ── Step 3: Retry waste tracking ───────────────────────────────────
    if should_retry:
        print("[dexcost] Simulated rate-limit -- initiating retry...")
        retry_event = task.mark_retry(reason="rate_limit_hit", cost_usd="0.00075")
        print(
            f"[dexcost] Retry waste recorded: reason={retry_event.retry_reason}, "
            f"cost=${retry_event.cost_usd}"
        )

# Task context manager auto-ends; retrieve the stored task
stored = storage.get_task(str(task.task_id))

# ── Print final summary ───────────────────────────────────────────────
print()
print("=== Dexcost Agent Cost Capture Results ===")
print(f"Task ID:       {stored.task_id}")
print(f"Task Type:     {stored.task_type}")
print(f"Status:        {stored.status}")
print(f"LLM Cost:      ${stored.llm_cost_usd}")
print(f"Tool Costs:    ${stored.external_cost_usd}")
print(f"Total Cost:    ${stored.total_cost_usd}")
print(f"Input Tokens:  {stored.total_input_tokens}")
print(f"Output Tokens: {stored.total_output_tokens}")
print(f"Retry Count:   {stored.retry_count}")
print(f"Retry Waste:   ${stored.retry_cost_usd}")
print("==========================================")

# ── Verify event schema compliance ───────────────────────────────────
events = storage.query_events(task_id=str(task.task_id))
print()
print(f"[dexcost] Events in buffer: {len(events)} events")
for i, ev in enumerate(events, 1):
    print(
        f"  Event {i}: type={ev.event_type} cost=${ev.cost_usd} "
        f"is_retry={ev.is_retry} provider={ev.provider or 'none'} "
        f"model={ev.model or 'none'} service={ev.service_name or 'none'}"
    )
    # Verify Standard Event Schema v1 required fields
    assert ev.event_id, "event_id must be non-empty"
    assert ev.task_id, "task_id must be non-empty"

print()
print("[dexcost] All verifications passed.")
print(f"\nDB path: {db_path}")
storage.close()