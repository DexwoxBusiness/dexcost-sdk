/**
 * dexcost bootstrap for a kodus-ai app (api / worker / webhooks).
 *
 * MUST be imported before anything else in the app entrypoint so the
 * fetch patch is installed before any HTTP/LLM client captures a
 * reference to the unpatched `globalThis.fetch`:
 *
 *   // apps/worker/src/main.ts — FIRST import in the file
 *   import "./dexcost-bootstrap";
 *
 * Notes for the kodus runtime specifically:
 * - kodus's v5 agent engine (Vercel AI SDK v6) resolves `globalThis.fetch`
 *   at call time, so the fetch-level capture works regardless of ordering —
 *   but the legacy v2 engine's `@anthropic-ai/sdk` client captures fetch at
 *   CONSTRUCTION time, so bootstrap must still run first.
 * - The `ai` package (v5+) is ESM-only and cannot be monkey-patched; you
 *   will see a one-line warning from the vercel-ai instrument. That is
 *   expected — those calls are captured at the HTTP layer instead.
 * - kodus uses pnpm with strict node_modules: add @dexcost/sdk as a REAL
 *   dependency of the orchestrator (`pnpm add @dexcost/sdk`), not a
 *   transitive one, so it resolves the same provider packages as the app.
 */

import { init } from "@dexcost/sdk";

init({
  apiKey: process.env.DEXCOST_API_KEY,
  // The worker is the process that makes the LLM calls — every app that
  // talks to a provider needs its own init (api, worker, webhooks are
  // separate containers in docker-compose).
  environment: process.env.NODE_ENV === "production" ? undefined : "development",
});
