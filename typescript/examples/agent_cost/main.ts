/**
 * Dexcost TypeScript SDK — Agent Cost Capture Example
 *
 * Run with: npx tsx examples/agent_cost/main.ts
 *
 * This example demonstrates wiring the dexcost SDK around a simulated local AI agent:
 *   1. Records LLM call costs (provider: "local", model: "local-llm")
 *   2. Records non-LLM tool costs (web search, maps API)
 *   3. Demonstrates retry waste tracking (simulated rate-limit retry)
 *   4. Verifies all events appear in the buffer with correct schema fields.
 *
 * No API key required — runs in offline/dev mode.
 */

import { randomUUID } from "node:crypto";

// Dynamically import to allow running without compile step
const { init, globalClose, getTracker } = await import("../../src/index.js");

interface ToolResult {
  service: string;
  cost: string;
  details: Record<string, unknown>;
}

function simulateLlmCall(promptTokens: number): { outputTokens: number; latencyMs: number; shouldRetry: boolean } {
  const outputTokens = promptTokens * 3; // 3x token amplification
  const latencyMs = 180;
  const shouldRetry = Math.random() > 0.77; // ~23% retry rate for demo
  return { outputTokens, latencyMs, shouldRetry };
}

function simulateToolCall(tool: string): ToolResult {
  if (tool === "web_search") {
    return { service: "web_search", cost: "0.002", details: { query: "weather forecast", results_count: 5 } };
  } else if (tool === "maps_api") {
    return { service: "maps_api", cost: "0.005", details: { operation: "route", waypoints: 3 } };
  }
  return { service: "unknown", cost: "0", details: {} };
}

async function run() {
  console.log("[dexcost] Initializing SDK (offline mode)...");

  const tracker = init({
    environment: "development", // enables dev mode, no cloud push
    autoInstrument: [], // disable auto-instrumentation for this example
  });

  // ── Start a task for the agent run ─────────────────────────────────
  const task = await tracker.track(
    {
      taskType: "local_agent_task",
      customerId: "demo-corp",
      projectId: "agent-demo",
      metadata: { agent_framework: "dexcost-demo" },
    },
    async (task) => {
      console.log(`[dexcost] Task started: ${task.task.taskId}`);

      // ── Step 1: Initial LLM call ─────────────────────────────────────
      const promptTokens = 150;
      const { outputTokens, latencyMs, shouldRetry } = simulateLlmCall(promptTokens);

      const llmCost = 0.00075;
      const llmEvent = task.recordLlmCall(
        "local",
        "local-llm",
        promptTokens,
        outputTokens,
        llmCost,
        undefined, // cachedTokens
        latencyMs,
        "exact"
      );
      console.log(
        `[dexcost] LLM call recorded: ${promptTokens} input + ${outputTokens} output tokens, cost=$${llmEvent.costUsd}, latency=${latencyMs}ms`
      );

      // ── Step 2: Non-LLM tool calls ────────────────────────────────────
      const tool1 = simulateToolCall("web_search");
      const toolEvent1 = task.recordCost(tool1.service, parseFloat(tool1.cost), tool1.details, "external_cost", "exact");
      console.log(`[dexcost] Tool cost recorded: ${tool1.service} cost=$${toolEvent1.costUsd}`);

      const tool2 = simulateToolCall("maps_api");
      const toolEvent2 = task.recordCost(tool2.service, parseFloat(tool2.cost), tool2.details, "external_cost", "exact");
      console.log(`[dexcost] Tool cost recorded: ${tool2.service} cost=$${toolEvent2.costUsd}`);

      // ── Step 3: Retry waste tracking ──────────────────────────────────
      if (shouldRetry) {
        console.log("[dexcost] Simulated rate-limit — initiating retry...");
        const retryEvent = task.markRetry("rate_limit_hit", llmCost);
        console.log(`[dexcost] Retry waste recorded: reason=${retryEvent.retryReason}, cost=$${retryEvent.costUsd}`);
      }

      return task;
    }
  );

  // ── Print final summary ─────────────────────────────────────────────
  const stored = task.task;
  console.log();
  console.log("=== Dexcost Agent Cost Capture Results ===");
  console.log(`Task ID:       ${stored.taskId}`);
  console.log(`Task Type:     ${stored.taskType}`);
  console.log(`Status:        ${stored.status}`);
  console.log(`LLM Cost:      $${stored.llmCostUsd}`);
  console.log(`Tool Costs:    $${stored.externalCostUsd}`);
  console.log(`Total Cost:    $${stored.totalCostUsd}`);
  console.log(`Input Tokens:  ${stored.totalInputTokens}`);
  console.log(`Output Tokens: ${stored.totalOutputTokens}`);
  console.log(`Retry Count:   ${stored.retryCount}`);
  console.log(`Retry Waste:   $${stored.retryCostUsd}`);
  console.log("==========================================");

  // ── Verify event schema compliance ────────────────────────────────
  const events = tracker.buffer.queryEvents(stored.taskId);
  console.log();
  console.log(`[dexcost] Events in buffer: ${events.length} events`);
  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    console.log(
      `  Event ${i + 1}: type=${ev.eventType} cost=$${ev.costUsd} is_retry=${ev.isRetry} provider=${ev.provider ?? "none"} model=${ev.model ?? "none"} service=${ev.serviceName ?? "none"}`
    );
    // Verify Standard Event Schema v1 required fields
    if (!ev.eventId) throw new Error("event_id must be non-empty");
    if (!ev.taskId) throw new Error("task_id must be non-empty");
  }

  console.log();
  console.log("[dexcost] All verifications passed.");

  // Note: globalClose() not called here — the tracker auto-closes when the process exits.
  // In long-running processes, call tracker.close() or globalCloseAsync() instead.
}

run().catch((err) => {
  console.error("[dexcost] Error:", err);
  process.exit(1);
});