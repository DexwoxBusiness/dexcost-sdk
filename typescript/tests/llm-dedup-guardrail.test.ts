/**
 * Guardrail for the cross-layer LLM dedup invariant.
 *
 * EVERY layer that records an llm_call event must register a fingerprint
 * via registerLlmCapture(), or the OTel bridge (DexcostSpanProcessor)
 * will emit a DUPLICATE event when the same call's telemetry span ends —
 * silent double billing. This was found in review once (the openai
 * instrument's non-streaming path); this test makes the whole class of
 * miss impossible to reintroduce quietly.
 *
 * Static check: an explicit per-file expectation of registerLlmCapture
 * CALL sites (imports excluded). If you add or remove an llm_call
 * recording site, update BOTH the code (register after addEvent) and
 * this table — the failure message walks you through it.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import ts from "typescript";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = join(here, "..", "src");

/** registerLlmCapture call sites expected per file (imports excluded). */
const EXPECTED_REGISTRATIONS: Record<string, number> = {
  "adapters/http.ts": 2, // JSON fallback + SSE fallback
  "clients.ts": 1, // TrackedOpenAI/TrackedAnthropic shared helper
  "core/tracker.ts": 1, // TrackedTask.recordLlmCall (manual layer)
  "instruments/anthropic.ts": 3, // non-stream + stream usage + stream no-usage
  "instruments/bedrock.ts": 1,
  "instruments/cohere.ts": 3,
  "instruments/gemini.ts": 3,
  "instruments/openai.ts": 3,
  "instruments/vercel-ai.ts": 1, // single shared recordEvent
  "integrations/ai-sdk.ts": 1, // single shared _recordEvent
  "integrations/langchain.ts": 2, // llm end + llm error
  "integrations/otel.ts": 1, // the bridge registers its own captures too
};

/** Files that mention llm_call but never record events (types, printers). */
const NO_CAPTURE_ALLOWLIST = new Set([
  "core/models.ts",
  "core/heuristics.ts",
  "core/llm-dedup.ts",
  "dev-console.ts",
  "cli/scanner.ts",
]);

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (full.endsWith(".ts")) out.push(full);
  }
  return out;
}

/**
 * Count actual `registerLlmCapture(...)` CALL EXPRESSIONS via the
 * TypeScript AST — immune to mentions in comments and string literals
 * (which a substring scan would count, letting a future comment silently
 * satisfy — or spuriously break — the invariant), and to formatting
 * (inline `if (x) registerLlmCapture(...)`, multiline argument lists).
 */
function countRegistrations(source: string, fileName: string): number {
  const sourceFile = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true);
  let count = 0;
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      // Accept both bare calls (named import) and qualified calls
      // (namespace import `dedup.registerLlmCapture(...)`, class method
      // `this.registerLlmCapture(...)`) so an import-style refactor can't
      // make real registrations count as zero and fail the guardrail
      // spuriously.
      const isBare = ts.isIdentifier(callee) && callee.text === "registerLlmCapture";
      const isQualified =
        ts.isPropertyAccessExpression(callee) && callee.name.text === "registerLlmCapture";
      if (isBare || isQualified) {
        count += 1;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return count;
}

describe("LLM dedup fingerprint guardrail", () => {
  it("every file recording llm_call events registers the expected fingerprints", () => {
    const problems: string[] = [];
    const seen = new Set<string>();

    for (const file of walk(srcRoot)) {
      const rel = relative(srcRoot, file).replace(/\\/g, "/");
      const source = readFileSync(file, "utf-8");
      if (!source.includes('eventType: "llm_call"')) continue;
      seen.add(rel);

      if (NO_CAPTURE_ALLOWLIST.has(rel)) {
        if (source.includes("addEvent(")) {
          problems.push(
            `${rel} is allowlisted as non-capturing but now calls addEvent() — ` +
              "move it into EXPECTED_REGISTRATIONS and register fingerprints.",
          );
        }
        continue;
      }

      const expected = EXPECTED_REGISTRATIONS[rel];
      if (expected === undefined) {
        problems.push(
          `${rel} records llm_call events but is not in EXPECTED_REGISTRATIONS — ` +
            "add registerLlmCapture(taskId, in, out) after each addEvent(event) " +
            "and list the file here, or the OTel bridge will double-count its calls.",
        );
        continue;
      }
      const actual = countRegistrations(source, rel);
      if (actual !== expected) {
        problems.push(
          `${rel}: expected ${expected} registerLlmCapture call site(s), found ${actual} — ` +
            "every llm_call addEvent needs a matching registration; update code and table together.",
        );
      }
    }

    for (const rel of Object.keys(EXPECTED_REGISTRATIONS)) {
      if (!seen.has(rel)) {
        problems.push(`${rel} listed in EXPECTED_REGISTRATIONS but no longer records llm_call events.`);
      }
    }

    expect(problems, problems.join("\n")).toEqual([]);
  });
});

describe("behavioral: instrument captures register fingerprints (review finding)", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "dexcost-guardrail-"));
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("a non-streaming anthropic instrument capture is visible to wasLlmRecentlyCaptured", async () => {
    const [{ EventBuffer }, { PricingEngine }, { createTask }, ctx, dedup, anthropic] =
      await Promise.all([
        import("../src/transport/buffer.js"),
        import("../src/pricing/engine.js"),
        import("../src/core/models.js"),
        import("../src/core/context.js"),
        import("../src/core/llm-dedup.js"),
        import("../src/instruments/anthropic.js"),
      ]);
    dedup._resetLlmDedupForTests();
    const buffer = new EventBuffer(join(tmpDir, "t.db"));

    class Messages {
      async create(): Promise<unknown> {
        return {
          model: "claude-sonnet-4-5",
          usage: { input_tokens: 1200, output_tokens: 340 },
        };
      }
    }
    anthropic._setMessagesClass(Messages);
    try {
      await anthropic.instrumentAnthropic(new PricingEngine(), buffer);
      const task = createTask({ taskId: randomUUID(), taskType: "t" });
      await ctx.runWithTask(task, () => Messages.prototype.create.call({}, {}));

      // The OTel bridge's dedup check must see this capture — this is the
      // exact double-count path flagged in review.
      expect(dedup.wasLlmRecentlyCaptured(task.taskId, 1200, 340)).toBe(true);
    } finally {
      anthropic.uninstrumentAnthropic();
      anthropic._resetMessagesClass();
      dedup._resetLlmDedupForTests();
      buffer.close();
    }
  });
});
