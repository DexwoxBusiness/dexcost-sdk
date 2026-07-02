/**
 * Cross-layer LLM capture dedup.
 *
 * The SDK captures LLM calls at several layers (module instruments, AI SDK
 * middleware, the patched-fetch fallback, and the OTel span bridge). The
 * first three coordinate through the AsyncLocalStorage suppression scope —
 * an outer capture suppresses the inner ones. The OTel bridge CANNOT join
 * that scope (span processors observe from outside the call chain), so a
 * call captured by the fetch fallback would be captured a second time when
 * its `ai.generateText.doGenerate` span ends.
 *
 * This registry closes that hole: the primary layers register a
 * fingerprint of every llm_call they record; the bridge checks it before
 * emitting. Fingerprints are `taskId|inputTokens|outputTokens` — the model
 * string is intentionally EXCLUDED because layers see different names for
 * the same call (response `model` vs configured `modelId`). Entries expire
 * after a short TTL, so two genuinely identical calls in the same task
 * only collide when they finish within the window (rare; the cost of a
 * collision is one uncounted duplicate-looking event, the cost of no dedup
 * is systematic double counting).
 */

const TTL_MS = 5_000;

/** fingerprint → expiry epoch ms. */
const _recent = new Map<string, number>();

/** Bounded sweep so the map never grows past a few hundred entries. */
const _MAX_ENTRIES = 512;

function _fingerprint(taskId: string, inputTokens: number, outputTokens: number): string {
  return `${taskId}|${inputTokens}|${outputTokens}`;
}

function _sweep(now: number): void {
  if (_recent.size < _MAX_ENTRIES) return;
  for (const [key, expiry] of _recent) {
    if (expiry <= now) _recent.delete(key);
  }
  // Still over the cap (burst of live entries): drop oldest-inserted.
  if (_recent.size >= _MAX_ENTRIES) {
    for (const key of _recent.keys()) {
      _recent.delete(key);
      if (_recent.size < _MAX_ENTRIES / 2) break;
    }
  }
}

/** Record that an llm_call with this usage was captured for this task. */
export function registerLlmCapture(
  taskId: string,
  inputTokens: number,
  outputTokens: number,
): void {
  // Zero-usage events carry no dedup signal — never register them.
  if (inputTokens <= 0 && outputTokens <= 0) return;
  const now = Date.now();
  _sweep(now);
  _recent.set(_fingerprint(taskId, inputTokens, outputTokens), now + TTL_MS);
}

/** True when an llm_call with this usage was captured recently for this task. */
export function wasLlmRecentlyCaptured(
  taskId: string,
  inputTokens: number,
  outputTokens: number,
): boolean {
  if (inputTokens <= 0 && outputTokens <= 0) return false;
  const key = _fingerprint(taskId, inputTokens, outputTokens);
  const expiry = _recent.get(key);
  if (expiry === undefined) return false;
  if (expiry <= Date.now()) {
    _recent.delete(key);
    return false;
  }
  return true;
}

/** Test-only: clear the registry. */
export function _resetLlmDedupForTests(): void {
  _recent.clear();
}
