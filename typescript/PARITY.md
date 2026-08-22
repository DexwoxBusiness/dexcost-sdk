# Python vNext → TypeScript parity contract

The TypeScript SDK follows the frozen DexCost Python vNext contract. This is
not a hand-maintained feature checklist.

## Machine-checkable authority

- `../contracts/python-vnext/v1/public-api.json` freezes every Python root
  export and class member.
- `../contracts/python-vnext/v1/typescript-public-api.json` freezes every
  TypeScript root export (runtime values and type-only exports).
- `../contracts/python-vnext/v1/typescript-api-map.json` classifies all 168
  frozen Python root exports. It currently has zero unresolved exports; the
  only language-specific entries are Python package-version metadata and the
  CrewAI/Griptape framework helpers.
- `scripts/freeze-contract.mjs` regenerates/checks both TypeScript artifacts by
  asking the TypeScript compiler for the actual public module surface.
- `tests/contract-freeze.test.ts` fails on public-surface drift.

Run the check directly:

```bash
node scripts/freeze-contract.mjs
```

## Shared behavior

Python and TypeScript consume the same JSON schemas and golden fixtures for:

- tasks, cost events, attribution v2/v3, business identity, outcomes, exact
  revenue revisions, provider-job revisions, catalog releases, overlays,
  observer rules, ingestion acknowledgements/failures, stream lifecycles,
  and pricing explanations;
- exact decimal wire serialization and deterministic rate-registry versions;
- operation, attempt, retry, idempotency, capability, provider-record, user,
  product, agent, workflow, experiment, and variant identity;
- durable delivery status, failure/quarantine handling, webhook verification,
  and atomic catalog release/LKG behavior.

## Intentional language-shaped equivalents

- Python `AttachedTask` is the non-owning mode of TypeScript `TrackedTask`.
- Python `SyncWorker` maps to TypeScript `EventPusher`.
- Python `Event` maps to TypeScript `CostEvent`.
- Python synchronous/async task context managers map to AsyncLocalStorage
  `runWithTask`.
- Python `DexcostConfig` maps to TypeScript `TrackerOptions` plus
  `ResolvedConfig`.
- CrewAI and Griptape are Python ecosystems. TypeScript exposes the same
  generic task, tool, capability, and provider-job primitives rather than
  pretending those Python packages exist in Node.

## Catalog packaging decision

The control plane is authoritative for one atomic seven-artifact release.
Both SDKs keep a durable active/previous last-known-good release and a full
bundled bootstrap/offline fallback. Bundled catalog slimming is deliberately
blocked until production telemetry proves release availability, rollback,
startup latency, cache recovery, and offline behavior across supported SDK
versions. Both runtimes also share the v1 exact-byte offline bundle and verify
the same domain-separated Ed25519 manifest payload with rotated trust keys.

## Provider coverage

TypeScript ships first-class instruments for OpenAI (including Responses,
usage APIs, and Realtime), Anthropic, Vercel AI, both Google Gemini SDKs,
Bedrock, Cohere, MCP, LiteLLM, Ollama, OpenRouter, Perplexity, and fal. The
provider-specific behavior is tested in addition to the generic HTTP fallback;
OpenRouter is not treated as an alias hidden inside another provider.
