#!/usr/bin/env node
/**
 * Runtime smoke test — proves the BUILT package works on the current
 * JavaScript runtime (Node, Bun, Deno). CI runs this under all three, so
 * "works on Bun/Deno" is an enforced claim, not a documented hope.
 *
 * Standalone on purpose: no vitest (Deno can't run it), only `node:assert`
 * and the compiled `dist/` output — which also makes this a packaging
 * check (a missing dist file or broken import chain fails here first).
 *
 * Exercises the capture surface end-to-end in local mode:
 *   1. init() + explicit track() with manual recordLlmCall
 *   2. ambient capture: patched fetch → LLM HTTP fallback (llm_call)
 *   3. dexcostAiMiddleware (wrapGenerate) — the ai>=5 path
 *   4. createHonoMiddleware — the Bun/Deno HTTP-framework path
 *   5. dexcost doctor (offline) reports healthy
 *
 * Usage:  node scripts/runtime-smoke.mjs
 *         bun  scripts/runtime-smoke.mjs
 *         deno run -A scripts/runtime-smoke.mjs
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = (p) => join(here, "..", "dist", p);

const runtime =
  typeof globalThis.Bun !== "undefined"
    ? `bun ${globalThis.Bun.version}`
    : typeof globalThis.Deno !== "undefined"
      ? `deno ${globalThis.Deno.version?.deno}`
      : `node ${process.versions.node}`;

let passed = 0;
const ok = (name) => {
  passed += 1;
  console.log(`  ok ${passed} - ${name}`);
};

const tmp = mkdtempSync(join(tmpdir(), "dexcost-smoke-"));

// ---------------------------------------------------------------------------
// Fake network: install BEFORE init() so dexcost's patch wraps this stub and
// no smoke traffic ever leaves the machine.
// ---------------------------------------------------------------------------
const anthropicBody = JSON.stringify({
  id: "msg_smoke",
  type: "message",
  model: "kimi-k2-0905-preview",
  content: [{ type: "text", text: "hi" }],
  usage: { input_tokens: 1200, output_tokens: 340 },
});
globalThis.fetch = async () =>
  new Response(anthropicBody, {
    status: 200,
    headers: { "content-type": "application/json" },
  });

console.log(`dexcost runtime smoke on ${runtime}`);

try {
  const sdk = await import(dist("index.js"));
  const {
    init,
    track,
    close,
    getTracker,
    runWithContext,
    dexcostAiMiddleware,
    createHonoMiddleware,
  } = sdk;
  ok(`dist/index.js imports (${Object.keys(sdk).length} exports)`);

  init({ dbPath: join(tmp, "smoke.db") }); // no apiKey → local mode, no push
  const tracker = getTracker();
  ok("init() + getTracker()");

  // 1 — explicit track() with a manual LLM call
  await track({ taskType: "smoke_explicit", customerId: "smoke" }, async (task) => {
    task.recordLlmCall("openai", "gpt-4o", 800, 150);
  });
  const explicitTask = tracker.buffer
    .getAllTasks()
    .find((t) => t.taskType === "smoke_explicit");
  assert.ok(explicitTask, "explicit task persisted");
  assert.equal(explicitTask.status, "success");
  assert.equal(explicitTask.totalInputTokens, 800);
  ok("explicit track() + recordLlmCall");

  // 2 — ambient capture through the patched fetch (kodus-style)
  await runWithContext({ customerId: "smoke-ambient" }, async () => {
    const res = await fetch("https://api.kimi.com/anthropic/v1/messages", {
      method: "POST",
      body: "{}",
    });
    await res.text();
  });
  const fallbackEvents = tracker.buffer
    .getAllEvents()
    .filter((e) => e.eventType === "llm_call" && e.provider === "api.kimi.com");
  assert.equal(fallbackEvents.length, 1, "HTTP LLM fallback captured exactly one llm_call");
  assert.equal(fallbackEvents[0].inputTokens, 1200);
  ok("ambient fetch capture (LLM HTTP fallback)");

  // 3 — AI SDK middleware (the ai>=5 path)
  const mw = dexcostAiMiddleware({ tracker });
  await mw.wrapGenerate({
    doGenerate: async () => ({ usage: { inputTokens: 500, outputTokens: 60 } }),
    model: { modelId: "claude-sonnet-4-5", provider: "anthropic.messages" },
  });
  const mwEvents = tracker.buffer
    .getAllEvents()
    .filter((e) => e.details?.source === "ai_sdk_middleware_generate");
  assert.equal(mwEvents.length, 1, "middleware recorded exactly one event");
  assert.equal(mwEvents[0].inputTokens, 500);
  ok("dexcostAiMiddleware wrapGenerate");

  // 4 — Hono middleware (the Bun/Deno framework path)
  const vars = new Map();
  const honoCtx = {
    req: { method: "POST", path: "/smoke", header: () => undefined },
    res: { status: 200 },
    set: (k, v) => vars.set(k, v),
    get: (k) => vars.get(k),
  };
  await createHonoMiddleware({ tracker })(honoCtx, async () => {});
  const honoTask = tracker.buffer.getAllTasks().find((t) => t.taskType === "POST /smoke");
  assert.ok(honoTask, "hono request task persisted");
  assert.equal(honoTask.status, "success");
  ok("createHonoMiddleware request tracking");

  // 5 — doctor (offline: no endpoint probe from CI)
  const { runDoctor } = await import(dist("cli/doctor.js"));
  const report = await runDoctor({ offline: true });
  assert.ok(report.checks.length >= 8, "doctor produced a full report");
  assert.equal(
    report.healthy,
    true,
    `doctor unhealthy: ${JSON.stringify(report.checks.filter((c) => c.status === "fail"))}`,
  );
  ok(`dexcost doctor healthy (${report.checks.length} checks)`);

  close();
  ok("close()");

  // 6 — durable buffering (Node: better-sqlite3; Bun: bun:sqlite compat).
  // Deno has no loadable SQLite driver — memory fallback is the contract.
  if (typeof globalThis.Deno === "undefined") {
    const { EventBuffer } = await import(dist("transport/buffer.js"));
    const reopened = new EventBuffer(join(tmp, "smoke.db"));
    try {
      const persisted = reopened
        .getAllTasks()
        .some((t) => t.taskType === "smoke_explicit");
      assert.ok(persisted, "task survived buffer reopen (durable storage)");
      ok("durable buffer round-trip across reopen");
    } finally {
      reopened.close();
    }
  } else {
    ok("durable buffer skipped on Deno (memory fallback is the documented contract)");
  }

  console.log(`\nruntime smoke PASSED on ${runtime} (${passed} checks)`);
} catch (err) {
  console.error(`\nruntime smoke FAILED on ${runtime} after ${passed} passing checks:`);
  console.error(err);
  process.exit(1);
} finally {
  try {
    rmSync(tmp, { recursive: true, force: true });
  } catch {
    // best-effort cleanup
  }
}
