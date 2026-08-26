# DexCost Python-first SDK parity audit

## Governing order

1. The control plane owns one atomic, versioned release for all seven catalog
   families.
2. DexCost Python is completed and frozen against the control plane, shared
   schemas, golden fixtures, and competitive SDK audits.
3. TypeScript ports that frozen Python contract completely.
4. Both SDKs pass joint contract, package, storage-migration, provider, and
   control-plane gates.
5. Bundled catalogs are slimmed only after the production evidence gate passes.

This order is encoded in `contracts/python-vnext/v1/` and is not replaced by a
prose claim of “feature parity.”

## Audited sources

The reproducible source pins and hashes live in
`contracts/python-vnext/v1/audit-sources.json`:

- DexCost SDK base: `22e6921335cebe4637725dd5e5707d10b546a094`
- DexCost control-plane base: `adbb9ad9e1739fb8bd69646485b61f1530dd513a`
- Revenium Python 0.6.0 plus its CI-only gitleaks fix:
  `24d979a60c9e761b8d87905694149258dad8bba6`
- Revenium Node 1.1.10: `8988c68057b0241a232a669b120fc493aeb7b0eb`

## Competitive findings and DexCost decisions

- Outcomes are durable, immutable revision streams. Technical task failure is
  never silently converted into a missed business outcome.
- Revenue is a separate exact-decimal ledger with currency, source identity,
  correction history, and outcome linkage. It is not collapsed into a binary
  floating-point outcome value.
- Tool/capability attribution is generic and composable: operation, attempt,
  retry, idempotency, capability source/invocation, provider record, and typed
  usage/dimensions are preserved without creating a second agent hierarchy.
- Asynchronous provider work is a revisioned provider-job lifecycle. Polling
  snapshots replace the current revision and only the latest terminal state
  rolls into task economics.
- OpenRouter is a first-class Python and TypeScript provider with provider
  response cost/upstream cost and generation identity. Neither pinned Revenium
  provider table has equivalent first-class OpenRouter instrumentation; a fal
  route named `openrouter/router` is not an OpenRouter SDK integration.
- Perplexity and CrewAI behavior was implemented from provider/framework
  documentation and captured response shapes, not invented field names.
- Local GPU/network hardware remains visibly unpriced unless the customer
  supplies a positive exact rate. Version-2 `rates.yaml` is deterministic and
  produces the same pricing-version hash in Python and TypeScript.

### Revenium Node cross-check

The pinned Revenium Node 1.1.10 source was re-audited before closing the
TypeScript provider matrix. The useful implementation ideas are covered as
follows:

- Its LiteLLM implementation confirms that the production JavaScript contract
  is the OpenAI-compatible Proxy, not a general LiteLLM inference npm package.
  DexCost now attributes that real `openai`-client route across chat/Responses,
  streams, embeddings, media, batches, video, and fine-tuning while preserving
  upstream-provider and gateway-cost identity.
- Its bounded in-memory store-and-forward, exit flush, stable retry identity,
  and exponential backoff were checked against DexCost's durable SQLite
  delivery ledger, split-leaf acknowledgement, quarantine recovery, exit and
  freeze flushes, and idempotency scopes. The audit found and corrected a
  TypeScript scheduler defect where the reported backoff value did not control
  the next network attempt.
- Its outcome amendment/history API is covered by the stronger DexCost outcome
  and revenue revision ledgers: immutable ordered corrections, exact-decimal
  money, currencies and source identity, outcome linkage, local durability, and
  history queries exist in both finalized SDK contracts. TypeScript now also
  enforces the Python ledger's optimistic amendment, task ownership, immutable
  identity, lifecycle-transition, and currency invariants with dedicated
  persistence and delivery tests.
- Revenium prompt capture was not copied: DexCost's contract is quantities-only
  provider attribution and intentionally does not retain prompt, completion,
  document, query, media, S3, or ARN content by default.
- Revenium client-side cost-control enforcement is a separate product behavior
  that can reject customer inference calls. It is not an attribution-parity
  feature and has no finalized Python/control-plane policy contract in this
  release, so it is deliberately outside this locked SDK migration rather than
  being silently invented in TypeScript alone.

## Frozen evidence

`contracts/python-vnext/v1/` contains:

- public Python and TypeScript API snapshots plus a complete cross-language
  mapping (171 Python exports, 168 equivalent TypeScript mappings, 3 reviewed
  language-specific mappings, and zero unresolved);
- storage migration sources and resulting SQLite schema;
- capability and provider matrices;
- wire-schema inventory and 10 frozen schemas;
- golden ingestion, failure, stream, pricing, provider-job, and catalog
  lifecycles;
- canonical/intentional-exclusion decisions and the catalog-slimming gate;
- a SHA-256 manifest covering every internal artifact and referenced shared
  fixture.

Python verifies this with `tests/test_contract_freeze.py`; TypeScript verifies
it with `tests/contract-freeze.test.ts` and compiler-resolved package-surface
tests.

The final post-freeze matrix passed on Python 3.10.19, 3.11.14, 3.12.12,
3.13.6, and 3.14.2. TypeScript passed 1,298 tests (6 skipped), strict package
types, ESM/CJS build, catalog probes, and release-package inspection. The same
built package passed runtime smoke on Node 20.19.6, Node 22.23.2, Node
24.13.1, Bun 1.4.0, and Deno 2.9.5.

## Catalog release and slimming decision

Moving catalogs server-side is the correct authority model only when startup
does not depend on a synchronous network call. The implemented design uses a
signed/checksummed manifest, monotonic sequence, compatibility bounds, atomic
activation of all artifacts, durable active/previous LKG, rollback, jittered
refresh, bounded exact-byte air-gap bundles, and a complete bootstrap fallback.
Workspace overlays remain explicit and auditable. Production publication now
refuses unsigned releases; both SDKs verify domain-separated Ed25519 signatures
against rotated trust keys before network, cache, or offline activation.

The current decision is `retain_full_bundles`. Production evidence for release
availability, rollback drills, corrupt-cache recovery, cold start, offline
operation, and supported-version adoption has not yet been supplied. Removing
bootstrap catalogs before those gates pass would turn a control-plane incident
into an application pricing outage.
