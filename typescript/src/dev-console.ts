/**
 * Development mode console output for dexcost.
 *
 * When DEXCOST_ENV=development or environment: "development" is passed,
 * every recorded event is printed to stderr with a formatted summary.
 */

import type { CostEvent, Task } from "./core/models.js";

let _devMode = false;

export function isDevMode(): boolean {
  return _devMode;
}

export function enableDevMode(): void {
  _devMode = true;
  // `print()` already prefixes "[dexcost]" — don't repeat it here.
  // Wording is explicit that local printing is ON and cloud sync is OFF,
  // and *why*, so it's never mistaken for an unexpected/error state.
  print(
    "development mode active (environment=\"development\") — cost events are " +
      "printed below; cloud sync is intentionally disabled in this mode",
  );
}

export function logEvent(event: CostEvent, taskType: string = ""): void {
  if (!_devMode) return;

  const cost = event.costUsd;
  const taskTag = taskType ? `  \x1b[90m(task: ${taskType})\x1b[0m` : "";

  if (event.eventType === "llm_call") {
    const provider = event.provider ?? "?";
    const model = event.model ?? "?";
    const inTok = event.inputTokens ?? 0;
    const outTok = event.outputTokens ?? 0;
    const cached = event.cachedTokens ?? 0;
    const retryTag = event.isRetry ? "  \x1b[33m(retry)\x1b[0m" : "";
    const cacheTag = cached > 0 ? `  cached: ${cached.toLocaleString()}` : "";
    print(
      `\x1b[32m✓\x1b[0m llm_call  ${provider}/${model}  ` +
      `${inTok.toLocaleString()} in / ${outTok.toLocaleString()} out${cacheTag}  ` +
      `$${cost}${retryTag}${taskTag}`
    );
  } else if (event.eventType === "external_cost" || event.eventType === "compute_cost") {
    const service = event.serviceName ?? "unknown";
    if (event.costConfidence === "unknown" || cost.isZero()) {
      print(
        `\x1b[33m⚠\x1b[0m ${event.eventType}  ${service}  ` +
        `$0.00 \x1b[33m(no rate configured)\x1b[0m${taskTag}`
      );
    } else {
      print(`\x1b[32m✓\x1b[0m ${event.eventType}  ${service}  $${cost}${taskTag}`);
    }
  } else if (event.eventType === "retry_marker") {
    const reason = event.retryReason ?? "unknown";
    print(`\x1b[33m↻\x1b[0m retry_marker  reason: ${reason}  $${cost}${taskTag}`);
  }
}

export function logTaskComplete(task: Task): void {
  if (!_devMode) return;

  let retryInfo = "";
  if (task.retryCount > 0) {
    retryInfo = `  retries: ${task.retryCount}  retry cost: $${task.retryCostUsd}`;
  }

  print(
    `\x1b[36m✓\x1b[0m task ${task.status}  ${task.taskType}  ` +
    `total: $${task.totalCostUsd}${retryInfo}`
  );
}

function print(msg: string): void {
  process.stderr.write(`\x1b[36m[dexcost]\x1b[0m ${msg}\n`);
}
