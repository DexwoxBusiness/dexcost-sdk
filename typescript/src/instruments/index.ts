/**
 * Instrument registry for dexcost TypeScript SDK.
 *
 * Provides a central registry where provider-specific instrumentation modules
 * (e.g., OpenAI, Anthropic) can self-register. The main SDK entry point uses
 * `instrumentProvider` / `uninstrumentProvider` to activate and deactivate
 * monkey-patches on demand.
 */

import type { PricingEngine } from "../pricing/engine.js";
import type { EventBuffer } from "../transport/buffer.js";
import { debugLog } from "../core/debug.js";

/** All provider instruments the SDK ships with. */
export const ALL_SUPPORTED_INSTRUMENTS = ["openai", "anthropic", "vercel-ai", "gemini", "bedrock", "cohere", "mcp"] as const;

/** Union type of supported instrument names. */
export type InstrumentName = (typeof ALL_SUPPORTED_INSTRUMENTS)[number];

type InstrumentFn = (pricing: PricingEngine, buffer: EventBuffer) => Promise<void>;
type UninstrumentFn = () => void;
type ProvideModuleFn = (ref: unknown) => void;

const registry = new Map<
  string,
  { instrument: InstrumentFn; uninstrument: UninstrumentFn; provideModule?: ProvideModuleFn }
>();

/**
 * Register an instrument by name.
 *
 * Called at module load time by each provider instrument (e.g., `openai.ts`).
 * `provideModule` accepts an explicit module/class reference for bundled
 * apps where runtime resolution fails (see `instrumentModules`).
 */
export function registerInstrument(
  name: string,
  instrument: InstrumentFn,
  uninstrument: UninstrumentFn,
  provideModule?: ProvideModuleFn,
): void {
  registry.set(name, { instrument, uninstrument, provideModule });
}

/** User-facing aliases for instrument names ("ai" is the npm package name). */
const INSTRUMENT_ALIASES: Record<string, string> = { ai: "vercel-ai" };

/**
 * Hand an instrument an explicit module/class reference — the escape hatch
 * for bundlers (Next.js/webpack/esbuild) that inline provider packages so
 * runtime `import()` resolution finds a DIFFERENT copy than the one the
 * app actually calls (Traceloop's `instrumentModules` pattern). Returns
 * false for unknown names or instruments without module injection.
 */
export function provideInstrumentModule(name: string, ref: unknown): boolean {
  const canonical = INSTRUMENT_ALIASES[name] ?? name;
  const entry = registry.get(canonical);
  if (!entry?.provideModule) {
    console.warn(
      `[dexcost] instrumentModules: unknown provider '${name}'. ` +
        `Supported: ${[...registry.keys()].join(", ")} (alias: ai).`,
    );
    return false;
  }
  try {
    entry.provideModule(ref);
    return true;
  } catch (err) {
    console.warn(
      `[dexcost] instrumentModules: failed to accept module for '${name}': ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
    return false;
  }
}

/** Resolve an alias to the canonical instrument name. */
export function canonicalInstrumentName(name: string): string {
  return INSTRUMENT_ALIASES[name] ?? name;
}

/**
 * True when `err` indicates the provider package is simply not installed
 * (the common, expected case — the SDK tries every supported provider by
 * default and most apps only use one or two).
 *
 * Node/bundlers phrase this several ways depending on loader and module
 * system: ESM throws `ERR_MODULE_NOT_FOUND` ("Cannot find package 'openai'
 * imported from ..."), CJS throws "Cannot find module 'openai'", and esbuild
 * /vitest add their own phrasings. We match all of them so a missing
 * optional provider never produces a scary warning.
 */
function isModuleNotInstalled(msg: string): boolean {
  return (
    msg.includes("Cannot find package") ||
    msg.includes("Cannot find module") ||
    msg.includes("Could not resolve") ||
    msg.includes("Failed to load url") ||
    msg.includes("not found") ||
    msg.includes("not installed") ||
    msg.includes("ERR_MODULE_NOT_FOUND") ||
    // Deno phrases a missing bare specifier as a relative-import error:
    // "Relative import path \"cohere-ai\" not prefixed with / or ./ or ../"
    msg.includes("not prefixed with /")
  );
}

/**
 * Activate the named instrument, monkey-patching the provider library.
 *
 * Returns `true` if the instrument was found and activated successfully,
 * `false` if the name is unknown or activation threw.
 *
 * `explicit` controls log noise (issue: noisy warnings for uninstalled
 * providers). When the SDK auto-instruments the full default provider set,
 * a missing package is expected and stays silent. When the user explicitly
 * listed the provider via `autoInstrument`, a failure is surfaced as a
 * warning — they asked for it, so they should know it didn't load.
 */
export async function instrumentProvider(
  name: string,
  pricing: PricingEngine,
  buffer: EventBuffer,
  explicit: boolean = false,
): Promise<boolean> {
  const entry = registry.get(name);
  if (!entry) {
    if (explicit) {
      console.warn(
        `[dexcost] Cannot instrument unknown provider '${name}'. ` +
          `Supported providers: ${ALL_SUPPORTED_INSTRUMENTS.join(", ")}.`,
      );
    }
    return false;
  }
  try {
    await entry.instrument(pricing, buffer);
    debugLog("instrument", `${name}: activated (module patch in effect)`);
    return true;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (isModuleNotInstalled(msg)) {
      debugLog("instrument", `${name}: package not installed — instrument inactive`);
      // Provider package not installed. Expected during default
      // auto-instrumentation — only surface it when the user explicitly
      // asked for this provider, and then with an actionable hint.
      if (explicit) {
        console.warn(
          `[dexcost] Provider '${name}' was requested via autoInstrument but its ` +
            `package is not installed. Install it to enable auto-instrumentation.`,
        );
      }
    } else {
      // The package IS present but patching threw — a real problem worth
      // surfacing regardless of explicit/default.
      console.warn(`[dexcost] Failed to instrument ${name}: ${msg}`);
      debugLog("instrument", `${name}: activation FAILED — ${msg}`);
    }
    return false;
  }
}

/**
 * Deactivate the named instrument, restoring the original library methods.
 */
export function uninstrumentProvider(name: string): void {
  const entry = registry.get(name);
  if (entry) entry.uninstrument();
}
