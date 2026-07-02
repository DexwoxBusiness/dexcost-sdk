/**
 * `dexcost doctor` — verify the capture pipeline end-to-end and explain
 * every degraded or broken link in it.
 *
 * The SDK's worst failure mode is silence: a provider that cannot be
 * patched, a buffer that fell back to memory, an endpoint that rejects
 * the API key — each leaves the dashboard empty with no error anywhere.
 * Doctor makes the whole chain inspectable in one command:
 *
 *   npx dexcost doctor [--api-key dx_...] [--endpoint https://...]
 *
 * Every check is individually exception-guarded: a crashing check reports
 * itself as failed with the error message; it never takes doctor down.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import { AsyncLocalStorage } from "node:async_hooks";
import { isDeno, runtimeDescription } from "../core/runtime.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ---------------------------------------------------------------------------
// Report types
// ---------------------------------------------------------------------------

export type DoctorStatus = "ok" | "warn" | "fail" | "skip";

export interface DoctorCheck {
  /** Stable machine-readable id, e.g. "provider.ai". */
  id: string;
  /** Human-readable check name. */
  name: string;
  status: DoctorStatus;
  /** One-line outcome, always set. */
  detail: string;
  /** Actionable remedy when status is warn/fail. */
  remedy?: string;
}

export interface DoctorReport {
  checks: DoctorCheck[];
  /** True when no check has status "fail". Warnings do not fail doctor. */
  healthy: boolean;
}

export interface DoctorOptions {
  /** API key to validate (default: DEXCOST_API_KEY env). */
  apiKey?: string;
  /** Endpoint to probe for reachability (default: SDK default endpoint). */
  endpoint?: string;
  /**
   * Skip the network reachability probe (offline environments, CI).
   */
  offline?: boolean;
}

// ---------------------------------------------------------------------------
// Individual checks
// ---------------------------------------------------------------------------

const _require = createRequire(import.meta.url);

/** Providers the SDK can instrument, with their npm package names. */
const PROVIDER_PACKAGES: Array<{ instrument: string; pkg: string }> = [
  { instrument: "openai", pkg: "openai" },
  { instrument: "anthropic", pkg: "@anthropic-ai/sdk" },
  { instrument: "vercel-ai", pkg: "ai" },
  { instrument: "gemini", pkg: "@google/generative-ai" },
  { instrument: "bedrock", pkg: "@aws-sdk/client-bedrock-runtime" },
  { instrument: "cohere", pkg: "cohere-ai" },
];

function checkRuntime(): DoctorCheck {
  const runtime = runtimeDescription();
  const major = Number.parseInt(process.versions.node.split(".")[0] ?? "0", 10);
  if (major < 18) {
    return {
      id: "runtime",
      name: "Runtime",
      status: "fail",
      detail: `${runtime} — Node >= 18 is required`,
      remedy: "Upgrade to Node 18+ (fetch, AsyncLocalStorage, WHATWG streams).",
    };
  }
  return { id: "runtime", name: "Runtime", status: "ok", detail: runtime };
}

function checkAsyncLocalStorage(): DoctorCheck {
  try {
    const als = new AsyncLocalStorage<number>();
    const value = als.run(42, () => als.getStore());
    if (value !== 42) throw new Error("run()/getStore() round-trip returned wrong value");
    return {
      id: "als",
      name: "AsyncLocalStorage (context propagation)",
      status: "ok",
      detail: "run()/getStore() functional",
    };
  } catch (err) {
    return {
      id: "als",
      name: "AsyncLocalStorage (context propagation)",
      status: "fail",
      detail: `not functional: ${err instanceof Error ? err.message : String(err)}`,
      remedy:
        "Task attribution requires AsyncLocalStorage. On Cloudflare Workers " +
        "enable the nodejs_compat flag; on other runtimes upgrade to a version " +
        "that implements node:async_hooks AsyncLocalStorage.",
    };
  }
}

function checkSqlite(): DoctorCheck {
  if (isDeno()) {
    // Deno's dlopen of the V8-ABI addon is a FATAL process error — never
    // probe it. The SDK skips it there too and uses the memory buffer.
    return {
      id: "sqlite",
      name: "Durable buffer (better-sqlite3)",
      status: "warn",
      detail:
        "skipped on Deno — V8-ABI native addons cannot be loaded; the SDK " +
        "uses the in-memory buffer (10k cap, lost on restart)",
      remedy: "For durable buffering run the workload on Node (or Bun once bun#4290 lands).",
    };
  }
  try {
    const Database = _require("better-sqlite3");
    // Requiring can succeed while the native binding still fails at open
    // time (Bun loads the JS wrapper but rejects the .node binding) —
    // actually open a throwaway in-memory database to prove it works.
    const probe = new Database(":memory:");
    probe.close();
    return {
      id: "sqlite",
      name: "Durable buffer (better-sqlite3)",
      status: "ok",
      detail: "native module loads and opens — events persist across restarts",
    };
  } catch (err) {
    const cause = err instanceof Error ? err.message.split("\n")[0] : String(err);
    const bindingsIssue = cause.includes("bindings") || cause.includes(".node");
    return {
      id: "sqlite",
      name: "Durable buffer (better-sqlite3)",
      status: "warn",
      detail: `unavailable — SDK falls back to in-memory buffer (10k cap, lost on restart). Cause: ${cause}`,
      remedy: bindingsIssue
        ? "Rebuild the native binding in the stage that runs your app: `npm rebuild better-sqlite3`."
        : "Install it for durable buffering: `npm install better-sqlite3` (needs python3/make/C++ compiler).",
    };
  }
}

function checkProviderPackages(): DoctorCheck[] {
  const checks: DoctorCheck[] = [];
  for (const { instrument, pkg } of PROVIDER_PACKAGES) {
    try {
      const pkgJson = _require(`${pkg}/package.json`);
      const version: string = pkgJson?.version ?? "unknown";
      if (pkg === "ai") {
        const major = Number.parseInt(version.split(".")[0] ?? "0", 10);
        if (major >= 5) {
          checks.push({
            id: `provider.${instrument}`,
            name: `Provider: ${pkg}`,
            status: "warn",
            detail:
              `v${version} installed — ESM-only since v5; the module-level instrument ` +
              "CANNOT patch it. Calls are captured at the HTTP layer instead.",
            remedy:
              "For exact usage (multi-step tool loops, cached tokens), wrap your " +
              "models: wrapLanguageModel({ model, middleware: dexcostAiMiddleware() }).",
          });
          continue;
        }
      }
      checks.push({
        id: `provider.${instrument}`,
        name: `Provider: ${pkg}`,
        status: "ok",
        detail: `v${version} installed — '${instrument}' instrument applicable`,
      });
    } catch {
      checks.push({
        id: `provider.${instrument}`,
        name: `Provider: ${pkg}`,
        status: "skip",
        detail: "not installed (fine unless you use this provider)",
      });
    }
  }
  return checks;
}

/**
 * Dry-run the instruments against a throwaway buffer and report which
 * actually activated. This catches the silent classes of failure: frozen
 * ESM namespaces, pnpm resolving a different package copy, patched-in-name-
 * only assignments.
 */
async function checkPatchEffectiveness(): Promise<DoctorCheck> {
  let tmp: string | null = null;
  try {
    const [{ EventBuffer }, { PricingEngine }, registry] = await Promise.all([
      import("../transport/buffer.js"),
      import("../pricing/engine.js"),
      import("../instruments/index.js"),
    ]);
    // Load instruments so they self-register (same set the tracker loads).
    await Promise.all([
      import("../instruments/openai.js"),
      import("../instruments/anthropic.js"),
      import("../instruments/vercel-ai.js"),
      import("../instruments/gemini.js"),
      import("../instruments/bedrock.js"),
      import("../instruments/cohere.js"),
      import("../instruments/mcp.js"),
    ]);

    tmp = mkdtempSync(join(tmpdir(), "dexcost-doctor-"));
    const buffer = new EventBuffer(join(tmp, "doctor.db"));
    const pricing = new PricingEngine();

    const active: string[] = [];
    const inactive: string[] = [];
    try {
      for (const name of registry.ALL_SUPPORTED_INSTRUMENTS) {
        const ok = await registry.instrumentProvider(name, pricing, buffer, false);
        (ok ? active : inactive).push(name);
        if (ok) registry.uninstrumentProvider(name);
      }
    } finally {
      buffer.close();
    }

    if (active.length === 0) {
      return {
        id: "patch",
        name: "Instrument dry-run",
        status: "warn",
        detail: `no module instrument activated (inactive: ${inactive.join(", ")})`,
        remedy:
          "Expected when no provider package is installed here, or when packages are " +
          "ESM-only. LLM calls are still captured at the HTTP layer; for the AI SDK " +
          "use dexcostAiMiddleware().",
      };
    }
    return {
      id: "patch",
      name: "Instrument dry-run",
      status: "ok",
      detail: `active: ${active.join(", ")}${inactive.length ? ` | inactive: ${inactive.join(", ")}` : ""}`,
    };
  } catch (err) {
    return {
      id: "patch",
      name: "Instrument dry-run",
      status: "fail",
      detail: `dry-run crashed: ${err instanceof Error ? err.message : String(err)}`,
    };
  } finally {
    if (tmp) {
      try {
        rmSync(tmp, { recursive: true, force: true });
      } catch {
        // best-effort cleanup
      }
    }
  }
}

async function checkFetchPatch(): Promise<DoctorCheck> {
  try {
    const { trackHttp, untrackHttp } = await import("../adapters/http.js");
    trackHttp();
    const marker = Symbol.for("dexcost.patched");
    const patched = (globalThis.fetch as any)?.[marker] === true;
    untrackHttp();
    if (!patched) {
      return {
        id: "fetch",
        name: "globalThis.fetch patch",
        status: "fail",
        detail: "trackHttp() ran but the patch marker is absent — fetch interception is not in effect",
        remedy:
          "Another library may be replacing globalThis.fetch after dexcost. " +
          "Call init() as the LAST fetch-wrapping tool, or check for frozen globals.",
      };
    }
    return {
      id: "fetch",
      name: "globalThis.fetch patch",
      status: "ok",
      detail: "patch installs and uninstalls cleanly",
    };
  } catch (err) {
    return {
      id: "fetch",
      name: "globalThis.fetch patch",
      status: "fail",
      detail: `patching crashed: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}

async function checkBufferRoundTrip(): Promise<DoctorCheck> {
  let tmp: string | null = null;
  try {
    const [{ EventBuffer }, { createTask }] = await Promise.all([
      import("../transport/buffer.js"),
      import("../core/models.js"),
    ]);
    tmp = mkdtempSync(join(tmpdir(), "dexcost-doctor-buf-"));
    const buffer = new EventBuffer(join(tmp, "doctor.db"));
    try {
      const task = createTask({ taskId: randomUUID(), taskType: "doctor_probe" });
      buffer.upsertTask(task);
      const found = buffer.getAllTasks().some((t) => t.taskId === task.taskId);
      if (!found) throw new Error("task written but not readable back");
      return {
        id: "buffer",
        name: "Buffer write/read round-trip",
        status: "ok",
        detail: "task persisted and read back",
      };
    } finally {
      buffer.close();
    }
  } catch (err) {
    return {
      id: "buffer",
      name: "Buffer write/read round-trip",
      status: "fail",
      detail: `round-trip failed: ${err instanceof Error ? err.message : String(err)}`,
      remedy: "Check filesystem permissions for the dbPath directory (default ~/.dexcost).",
    };
  } finally {
    if (tmp) {
      try {
        rmSync(tmp, { recursive: true, force: true });
      } catch {
        // best-effort cleanup
      }
    }
  }
}

async function checkApiKeyAndEndpoint(opts: DoctorOptions): Promise<DoctorCheck[]> {
  const checks: DoctorCheck[] = [];
  const apiKey = opts.apiKey ?? process.env.DEXCOST_API_KEY;

  try {
    const { validateApiKey } = await import("../core/config.js");
    if (!apiKey) {
      checks.push({
        id: "apikey",
        name: "API key",
        status: "warn",
        detail: "absent — SDK runs in LOCAL mode (events buffered, never pushed)",
        remedy: "Set DEXCOST_API_KEY (or init({ apiKey })) to enable cloud sync.",
      });
    } else {
      try {
        validateApiKey(apiKey);
        checks.push({
          id: "apikey",
          name: "API key",
          status: "ok",
          detail: "present, format valid",
        });
      } catch (err) {
        checks.push({
          id: "apikey",
          name: "API key",
          status: "fail",
          detail: `format invalid: ${err instanceof Error ? err.message : String(err)}`,
          remedy: "Copy the key from the dexcost dashboard (dx_live_... / dx_test_...).",
        });
      }
    }
  } catch (err) {
    checks.push({
      id: "apikey",
      name: "API key",
      status: "fail",
      detail: `validation crashed: ${err instanceof Error ? err.message : String(err)}`,
    });
  }

  // Endpoint reachability — only meaningful when pushing is intended.
  if (opts.offline) {
    checks.push({
      id: "endpoint",
      name: "Endpoint reachability",
      status: "skip",
      detail: "skipped (--offline)",
    });
    return checks;
  }
  try {
    const { resolveEndpoint } = await import("../core/endpoint.js");
    const endpoint = resolveEndpoint(opts.endpoint);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5_000);
    try {
      // Any HTTP response (even 401/404) proves the endpoint is reachable;
      // only network-level failures mean unreachable.
      const res = await fetch(endpoint, { method: "HEAD", signal: controller.signal });
      checks.push({
        id: "endpoint",
        name: "Endpoint reachability",
        status: "ok",
        detail: `${endpoint} reachable (HTTP ${res.status})`,
      });
    } finally {
      clearTimeout(timer);
    }
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    checks.push({
      id: "endpoint",
      name: "Endpoint reachability",
      status: apiKey ? "fail" : "warn",
      detail: `cannot reach endpoint: ${detail}`,
      remedy:
        "Check network egress/proxy rules from this environment. In LOCAL mode " +
        "(no API key) this is harmless. Use --offline to skip this probe.",
    });
  }
  return checks;
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

/** Run every doctor check. Never throws. */
export async function runDoctor(opts: DoctorOptions = {}): Promise<DoctorReport> {
  const checks: DoctorCheck[] = [];

  // Sequential on purpose: checks patch/unpatch shared globals (fetch,
  // provider prototypes) and must not interleave.
  const staticChecks: Array<() => DoctorCheck | DoctorCheck[]> = [
    checkRuntime,
    checkAsyncLocalStorage,
    checkSqlite,
    checkProviderPackages,
  ];
  for (const check of staticChecks) {
    try {
      const result = check();
      checks.push(...(Array.isArray(result) ? result : [result]));
    } catch (err) {
      checks.push({
        id: "internal",
        name: check.name || "internal check",
        status: "fail",
        detail: `check crashed: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  }

  const asyncChecks: Array<() => Promise<DoctorCheck | DoctorCheck[]>> = [
    checkPatchEffectiveness,
    checkFetchPatch,
    checkBufferRoundTrip,
    () => checkApiKeyAndEndpoint(opts),
  ];
  for (const check of asyncChecks) {
    try {
      const result = await check();
      checks.push(...(Array.isArray(result) ? result : [result]));
    } catch (err) {
      checks.push({
        id: "internal",
        name: check.name || "internal check",
        status: "fail",
        detail: `check crashed: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  }

  return { checks, healthy: !checks.some((c) => c.status === "fail") };
}

/** Render a report to stdout. Returns the process exit code. */
export function printDoctorReport(report: DoctorReport): number {
  const symbols: Record<DoctorStatus, string> = {
    ok: "✓",
    warn: "⚠",
    fail: "✗",
    skip: "-",
  };
  console.log("dexcost doctor\n");
  for (const check of report.checks) {
    console.log(`  ${symbols[check.status]} ${check.name}: ${check.detail}`);
    if (check.remedy && check.status !== "ok") {
      console.log(`      → ${check.remedy}`);
    }
  }
  const fails = report.checks.filter((c) => c.status === "fail").length;
  const warns = report.checks.filter((c) => c.status === "warn").length;
  console.log(
    `\n${report.healthy ? "Healthy" : "UNHEALTHY"} — ${fails} failure(s), ${warns} warning(s).`,
  );
  return report.healthy ? 0 : 1;
}
