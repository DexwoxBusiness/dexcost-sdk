/**
 * Debug logging for capture decisions.
 *
 * The SDK's cardinal failure mode is SILENCE: a provider package that
 * cannot be patched, an LLM call that degrades to a generic network
 * event, a buffer that silently fell back to memory. Debug mode makes
 * every capture decision loud so an engineer can answer "why wasn't this
 * call captured?" without reading SDK source.
 *
 * Enable with `init({ debug: true })` or `DEXCOST_DEBUG=1` (also accepts
 * "true"). Output goes to stderr, prefixed `[dexcost:<scope>]`, and is a
 * strict no-op when disabled — call sites can be left in hot paths.
 */

let _override: boolean | undefined;

/** Truthy values accepted for the DEXCOST_DEBUG environment variable. */
const _TRUTHY = new Set(["1", "true", "yes", "on"]);

function _envEnabled(): boolean {
  try {
    const v = process.env.DEXCOST_DEBUG;
    return v !== undefined && _TRUTHY.has(v.toLowerCase());
  } catch {
    // `process` may not exist on exotic runtimes — debug off is the
    // safe default.
    return false;
  }
}

/** Programmatic override — wired from `init({ debug })`. */
export function setDebugMode(enabled: boolean): void {
  _override = enabled;
}

/** Reset the override so the environment variable decides again (tests). */
export function _resetDebugModeForTests(): void {
  _override = undefined;
}

/** True when debug logging is active (init option wins over env var). */
export function isDebugMode(): boolean {
  return _override ?? _envEnabled();
}

/**
 * Log one capture decision. No-op unless debug mode is active.
 *
 * @param scope   Subsystem tag, e.g. "instrument", "http", "buffer".
 * @param message Human-readable decision, e.g.
 *                `llm_call captured via http fallback (api.kimi.com, kimi-k2)`.
 */
export function debugLog(scope: string, message: string): void {
  if (!isDebugMode()) return;
  // stderr — never pollutes stdout pipelines (CLIs, JSON output).
  // eslint-disable-next-line no-console
  console.error(`[dexcost:${scope}] ${message}`);
}
