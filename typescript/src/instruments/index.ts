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

const registry = new Map<string, { instrument: InstrumentFn; uninstrument: UninstrumentFn }>();

/**
 * Register an instrument by name.
 *
 * Called at module load time by each provider instrument (e.g., `openai.ts`).
 */
export function registerInstrument(
  name: string,
  instrument: InstrumentFn,
  uninstrument: UninstrumentFn,
): void {
  registry.set(name, { instrument, uninstrument });
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
