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
 * Activate the named instrument, monkey-patching the provider library.
 *
 * Returns `true` if the instrument was found and activated successfully,
 * `false` if the name is unknown or activation threw.
 */
export async function instrumentProvider(
  name: string,
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<boolean> {
  const entry = registry.get(name);
  if (!entry) return false;
  try {
    await entry.instrument(pricing, buffer);
    return true;
  } catch (err) {
    // Log the actual error so users can debug instrumentation failures
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("not found") || msg.includes("Could not resolve") || msg.includes("Cannot find module") || msg.includes("Failed to load url") || msg.includes("not installed")) {
      // Module not installed — expected, debug level
    } else {
      console.warn(`[dexcost] Failed to instrument ${name}: ${msg}`);
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
