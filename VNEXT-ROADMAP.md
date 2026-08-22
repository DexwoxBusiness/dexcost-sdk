# DexCost vNext Governing Roadmap

Status: active
Sequence owner: Python reference implementation
Last revised: 2026-08-21

This document is the governing delivery sequence for the next DexCost SDK generation. Work may be split into smaller pull requests, but the ordering and completion gates below do not change unless the product owner explicitly changes this document.

## Non-negotiable sequence

1. Inventory the complete Python, TypeScript, control-plane, and shared-contract surfaces.
2. Establish the server-side Catalog Release Service for every SDK catalog family.
3. Complete and harden DexCost Python vNext, including the catalog migration and competitive improvements.
4. Make the complete Python release gate green.
5. Freeze the Python vNext API, wire contracts, catalog contract and DSL, storage migrations, golden fixtures, and capability matrix.
6. Port the frozen Python reference completely to TypeScript.
7. Pass joint Python/TypeScript conformance and release gates.
8. Only then reduce or remove the full JSON catalogs bundled in SDK packages.

The permanent feature rule is:

> Shared contract, then Python implementation, then TypeScript implementation, then joint completion.

During the TypeScript parity phase, Python-only feature drift is not allowed. A critical Python correction must update the frozen contract and be included in TypeScript in the same delivery window.

## Product boundary

DexCost is not a basic token meter. The reference product contract covers auditable unit economics:

- normalized billable usage and exact provider-reported cost when available;
- provisional SDK valuation with explicit evidence, confidence, and catalog provenance;
- immutable server valuation and reconciliation evidence;
- task, root-task, parent-task, agent, workflow, customer, project, user, product, session, environment, and error attribution;
- business outcomes whose lifecycle is independent of technical task success;
- recognized revenue only when explicitly recorded, with immutable revisions;
- delivery health, durable local buffering, idempotency, quarantine, and explainability.

## Phase 0: inventory and shared contracts

Deliverables:

- one current capability matrix covering Python, TypeScript, control plane, Revenium Python, and Revenium Node;
- an explicit public API inventory for Python;
- wire-schema inventory for tasks, events, business identities, outcomes, revenue revisions, ingestion acknowledgements, and catalogs;
- provider/integration matrix including sync, async, stream completion, stream failure, cancellation, retries, tool calls, media, embeddings, and provider request IDs;
- storage/migration inventory and golden cross-language fixtures;
- replacement of stale parity documents with generated or test-backed status.

Exit gate: every surface is classified as implemented, intentionally excluded with rationale, or required for Python vNext. There are no unclassified rows.

## Phase 1: authoritative Catalog Release Service

The server becomes the authority; SDKs retain local evaluation and offline resilience.

Each immutable release atomically groups these artifact kinds:

- `observer_rules`
- `llm_prices`
- `service_prices`
- `compute_prices`
- `gpu_prices`
- `egress_prices`
- `server_pricing_reference`

The release contract includes a monotonic release sequence, content hashes, byte sizes, item counts, artifact schema versions, SDK contract bounds, safety-policy version, publication and expiry times, and server pricing-catalog reference.

Required delivery behavior:

- public manifest endpoint with `ETag`, `If-None-Match`, `304`, bounded cache lifetime, and `stale-if-error`;
- immutable content-addressed artifact endpoints with long-lived immutable caching;
- private workspace-overlay endpoint with separate authentication and cache policy;
- validate all artifacts before activation and activate a release atomically;
- append-only activation history, audited withdrawal, canary, and rollback;
- rollback publishes a new, higher release sequence that can reuse earlier artifact hashes;
- strict payload, decompression, schema, semantic, compatibility, origin, and timeout limits;
- declarative observer rules only; the server never ships executable SDK code;
- compatibility endpoints remain available during migration.

SDK runtime policy:

- provider calls never block on catalog network access;
- startup order is active durable cache, previous durable cache, minimal bootstrap, then usage-only/unpriced operation;
- background refresh uses conditional requests and jitter;
- download, validate, and persist before one-transaction activation;
- a failed or invalid refresh never replaces the active release;
- stale observer rules may continue measuring quantities;
- stale prices are always marked provisional and stale;
- every estimate exposes release, artifact, rule, safety-policy, source, confidence, and staleness provenance.

Exit gate: the control plane can publish, canary, activate, withdraw, and roll back a full validated release; the manifest and artifact APIs pass cache, integrity, monotonicity, failure, and compatibility tests.

## Phase 2: complete Python vNext

The Python reference includes, tests, and documents all of the following:

- initialization, configuration, endpoint safety, lifecycle, shutdown, and reset;
- context propagation and task/root/parent hierarchy;
- customer, project, user, product, agent/version, workflow/session, environment, and error identity;
- outcomes, outcome history, outcome-definition validation, and immutable amendments;
- revenue recording, immutable revenue revisions, and history;
- usage, exact/provisional cost, retries, failures, cancellation, abandoned streams, and early stream close;
- supported provider clients and integrations across sync, async, streaming, embeddings, media, tools, and responses APIs;
- general `track_tool`, cross-process `attach_task`, capability/skill identity, caller idempotency, and provider request correlation;
- HTTP/service observation, compute, GPU, egress/network, rate overrides, and all catalog engines;
- durable storage, migrations, sync, acknowledgements, splitting, quarantine, purge, retry, and delivery health;
- local `explain_pricing` with complete provenance;
- redaction, privacy classifications, allowlists, and safe prompt-capture defaults;
- CLI, scanner, exports, packaging, examples, documentation, and upgrade guidance.

Competitive additions selected for the Python reference:

- delivery-health status and callbacks;
- provider/framework breadth based on customer value, including CrewAI and Griptape paths where support is reliable;
- general tool tracking and cross-process task attachment;
- cross-provider capability identity rather than provider-specific skill metadata;
- stable idempotency and correlation APIs;
- outcome and revenue history ergonomics;
- webhook verification utilities where SDK-side verification is required;
- signed, cross-provider budget policy as a later contract after catalog signatures are established.

Explicit exclusions:

- technical failure never automatically means a missed business outcome;
- money and outcome numbers never use binary floating-point contracts;
- outcome recording never requires a synchronous successful network call;
- durable delivery is never an in-memory-only queue;
- prompt content is never captured by default;
- no parallel agentic-job identity model competes with canonical task and business identity;
- no provider-specific skill model becomes the cross-provider public contract.

Python release gate:

- full test suite green without order-dependent global instrumentation failures;
- Ruff green;
- strict mypy green;
- build and wheel-content verification green for every supported Python version;
- catalog offline, corruption, expiry, downgrade, oversize, hash, schema, retry, and atomic-activation tests green;
- every capability-matrix row resolved;
- docs and examples execute against the frozen API.

## Phase 3: freeze the Python reference

The freeze is a versioned, reviewable artifact containing:

- public import and signature snapshot;
- task/event/outcome/revenue/catalog JSON schemas;
- catalog manifest and observer-rule DSL schemas;
- SQLite schema and migration sequence;
- canonical serialization and decimal rules;
- golden requests, acknowledgements, failures, stream lifecycles, catalog releases, and pricing explanations;
- provider capability matrix and intentional-exclusion register.

Exit gate: a language implementation can determine parity mechanically from the frozen artifacts without interpreting Python internals.

## Phase 4: exact TypeScript parity

TypeScript ports the frozen reference, including behavior that is not visible in top-level method names:

- canonical identity and inheritance;
- outcomes and revenue revision ledgers;
- durable catalog cache and atomic release activation;
- failed-call, retry, cancellation, and abandoned-stream events;
- provider response variants and parse/stream APIs;
- delivery health, quarantine, acknowledgements, purge, and idempotency;
- provenance, explainability, redaction, privacy, CLI, scanner, exports, packaging, and documentation.

Exit gate: shared golden fixtures, schema validation, capability matrix, and language-specific suites are green with no unexplained Python/TypeScript differences.

## Phase 5: bundle slimming

Full bundled catalogs are removed or reduced only after production evidence proves:

- first-run bootstrap works;
- offline startup works;
- durable last-known-good and previous-release fallback work;
- corrupt and expired caches fail safely;
- air-gapped users have an explicit supported artifact-import path;
- rollback works without an SDK release;
- usage is never lost because pricing is unavailable.

The SDK retains a minimal bootstrap sufficient to observe usage safely. Server authority does not mean runtime dependence on a live catalog request.

## Completion definition

The program is complete only when all phases above meet their exit gates. Shipping a server endpoint, a Python feature, or a TypeScript method by itself is progress, not parity or completion.
