# TypeScript SDK — Python Parity

This document records the parity gaps between the **TypeScript SDK** and the
reference **Python SDK** (`dexcost-sdks/python/src/dexcost/`) that have been
closed. Each entry lists the gap, the Python reference, and the change made.

All 16 gaps below are closed. The build (`npm run build`), test suite
(`npm test`, vitest), and lint (`npm run lint`) all pass.

---

## 1. Redaction / hashing / metadata-limit applied on sync

**Python ref:** `sync.py:179-201`
**Files:** `src/transport/pusher.ts`

`EventPusher` POSTed raw event dicts, so `redactFields` / `hashCustomerId`
were accepted but never used — PII could leave the process. Added
`_serializeEvent()`, called from `pushWithSplit`, which applies
`redactDict(details, redactFields)`, SHA-256 hashes `customer_id` /
`project_id` in `details` when `hashCustomerId` is set, and runs
`enforceMetadataLimit` on `details` before serialisation.

## 2. LLM instruments always create an auto-task

**Python ref:** `auto_task.py`, `instruments/openai.py:140-142`
**Files:** new `src/core/auto-task.ts`; `src/instruments/{openai,anthropic,gemini,bedrock,cohere,mcp,vercel-ai}.ts`; `src/clients.ts`

Instruments previously returned without recording when there was no task
**and** no ambient context — LLM costs were silently lost. Added
`createAutoTask()` (mirrors Python `create_auto_task`, reads
customer/project/metadata/agent from `DexcostContext`). Every instrument and
both `Tracked*` clients now create and upsert an auto-task instead of
skipping.

## 3. Auth-failure stops sync permanently

**Python ref:** `sync.py:325-328`
**Files:** `src/transport/pusher.ts`

HTTP 401/403 were treated as ordinary retryable failures. Added an
`_authFailed` flag: on 401/403 `postRaw` logs, sets the flag, and calls
`stop()`. `push()` checks the flag and returns early. Exposed via the
`authFailed` getter.

## 4. `refreshFromServer` parses the correct response shape

**Python ref:** `pricing.py:323-340`
**Files:** `src/pricing/engine.ts`

It read `data.models`; the Control Layer contract nests pricing under
`payload.data.data` with `payload.data.pricing_version` alongside. Fixed
the parsing to read the nested shape, drop `sample_spec`, and capture
`pricing_version` (falling back to a content hash when absent).

## 5. `eventToDict` includes `pricing_version`

**Python ref:** `models/event.py:69`
**Files:** `src/core/models.ts`

Added `pricing_version` to the serialised event dict.

## 6. `recordLlmCall` auto-prices and accepts error/details

**Python ref:** `tracker.py:292-371`
**Files:** `src/core/tracker.ts`

`recordLlmCall` defaulted cost to `0` when omitted. It now takes an options
object (`costConfidence`, `pricingSource`, `pricingVersion`, `details`,
`errorType`); when `cost` is omitted the cost is auto-computed via the
pricing engine. `errorType` is stored in `details.error_type`.

## 7. HTTP capture wired in and strengthened

**Python ref:** `adapters/http.py`, `__init__.py:177-183`
**Files:** `src/adapters/http.ts`, `src/core/tracker.ts`

`trackHttp` only patched `globalThis.fetch` and was never invoked. Now:
(a) `CostTracker` calls `trackHttp` automatically when the `trackHttp` init
option is true (default); (b) Node's `http`/`https` `request`/`get` are
also patched (via `createRequire` to obtain the mutable module objects);
(c) `_maybeRecordCost` creates an auto-task (`http_call`) when no task
exists instead of returning silently.

## 8. LangChain `handleLLMError` records a failure event

**Python ref:** `integrations/langchain.py:147`
**Files:** `src/integrations/langchain.ts`

`handleLLMError` only deleted the pending entry. It now records a failure
`llm_call` event (cost 0, `cost_confidence: "unknown"`) with
`details.error_type` set from the error's name, when a task is active.

## 9. Manual `startTask()` API

**Python ref:** `tracker.py:900`
**Files:** `src/core/tracker.ts`

Added `CostTracker.startTask()` returning a `TrackedTask` the caller ends
explicitly (for Celery / multi-process flows). The previous `track()`
callback API is unchanged.

## 10. `fromDict` deserialisation for Task / Event

**Python ref:** `models/task.py:96`, `models/event.py:85`
**Files:** `src/core/models.ts`

Added `taskFromDict` / `eventFromDict` (inverse of `taskToDict` /
`eventToDict`), exported from the package root.

## 11. Trace links — naming + getter

**Python ref:** `tracker.py:271-290`
**Files:** `src/core/tracker.ts`

`linkTrace` stored links under `metadata.trace_links` with a `traceId`
key. Changed to `metadata._trace_links` with a `trace_id` key (matching
Python so cross-SDK buffers interoperate) and added `getTraceLinks()`.
Python exposes no module-level `link_trace` / `get_trace_links`, so none
were added at module level.

## 12. `recordCost` params + event-type validation

**Python ref:** `tracker.py:145`
**Files:** `src/core/tracker.ts`

Added `pricingSource` / `pricingVersion` parameters. `eventType` is now
validated against `{external_cost, compute_cost}` — any other value throws
an `Error`.

## 13. API-key validation + env resolution

**Python ref:** `config.py:16-31`
**Files:** new `src/core/config.ts`

Added `validateApiKey` (keys must start with `dx_live_` / `dx_test_`),
key-type detection, `InvalidAPIKeyError`, and `resolveConfig` which
resolves `DEXCOST_API_KEY` from the environment and computes the storage
mode. `CostTracker` uses `resolveConfig` during construction.

## 14. Init params: `trackHttp`, `serviceCatalogUrl`, storage mode

**Python ref:** `__init__.py:108-185`
**Files:** `src/core/tracker.ts`

Added `trackHttp` (default `true`), `serviceCatalogUrl`, and an explicit
`storage` (`"local"` / `"cloud"`) init option. `serviceCatalogUrl`
refreshes the HTTP service catalog on init.

## 15. Redaction strategy matches Python

**Python ref:** `redaction.py:18-56`
**Files:** `src/security/redaction.ts`, `tests/redaction.test.ts`

`redactDict` now **deletes** matched keys recursively (was masking with
`"[REDACTED]"`). `enforceMetadataLimit` now returns a deterministic stub
(`{_truncated: true, _original_size_bytes: N}`, or
`{_truncated: true, _error: "unserializable"}`) instead of removing keys
one by one.

## 16. New adapters — browser (Playwright) + AWS Lambda

**Python ref:** `adapters/browser.py`, `adapters/aws_lambda.py`
**Files:** new `src/adapters/browser.ts`, `src/adapters/aws-lambda.ts`,
`src/adapters/index.ts`, `src/adapters/data/aws_lambda_pricing.json`

Added a `trackBrowser` adapter (times a Playwright session and records a
`compute_cost` event; `playwright` added as an optional peer dependency)
and a `lambdaCost` / `getSupportedRegions` AWS Lambda cost calculator
(pure function, bundled pricing JSON ported from Python). Both are
re-exported from the new adapters index and the package root.

---

## Explicitly NOT changed

**Storage-based retry fallback** — Python's `_detect_retry`
(`tracker.py:404`) is unreachable: `CostTracker.__init__` only enables
`_enable_retry_heuristics` together with creating `_heuristic_engine`, so
the storage-fallback branch never runs. The TS SDK already matches the
reachable Python behaviour; no change made.

## Tests

New focused tests: `tests/parity.test.ts` (gaps 1, 3, 5, 6, 8, 9, 10, 11,
12, 13, 14), `tests/aws-lambda.test.ts` and `tests/browser-adapter.test.ts`
(gap 16). Existing tests updated for the changed behaviour of gaps 1, 2, 4,
11, and 15.
