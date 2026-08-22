# DexCost Python vNext contract freeze v1

This directory is the mechanical source of truth for the Python-first vNext
contract. A TypeScript implementation can determine parity from these files
without reading Python internals or treating Revenium as the product boundary.

The freeze contains:

- generated Python public imports, signatures, class members, SQLite schema,
  and the complete migration chain;
- canonical wire schemas for operational tasks/events, attribution
  observations, business identity, outcomes, revenue, provider jobs,
  acknowledgements, and every catalog-delivery document;
- exact serialization and decimal rules;
- golden accepted requests, partial acknowledgements, permanent and retryable
  failures, stream lifecycles, catalog releases, and pricing explanations;
- an exhaustive product capability matrix across DexCost Python, DexCost
  TypeScript, the control plane, Revenium Python, and Revenium Node;
- provider sub-capability evidence and the intentional-exclusion register.

Every row uses exactly one classification: `implemented`,
`intentionally_excluded`, or `required`. A `required` row makes the release
gate fail. Comparison-only Revenium cells use the same vocabulary but do not
change the DexCost product boundary.

Generated artifacts are refreshed only after review:

```text
cd python
python scripts/freeze_contract.py --write
python scripts/freeze_contract.py --check
```

`manifest.json` hashes every other file in this directory. Python and
TypeScript tests verify the manifest, schemas, fixtures, classification
closure, public exports, and migration continuity.
