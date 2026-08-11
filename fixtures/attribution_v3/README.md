# Attribution v3 convergence corpus

`manifest.json` and `conformance.json` lock the shared SDK contract before any
language migrates. The control plane's implemented observation and business
schemas are the authority; this corpus is their language-neutral executable
input.

The corpus is usage-first. SDK prices are diagnostic evidence, never the
authoritative customer total. Coverage includes stable usage-line identity,
full revision snapshots, lifecycle and retry semantics, typed dimensions,
business identity/outcome/revenue records, and redaction before detail fields
can be promoted into typed wire fields.

Invalid fixtures use a small mutation format (`mutate_from`, `set`, `delete`,
`append_usage`, and `append_dimension`). Every SDK must apply the same single
fault to the referenced valid record and assert the documented error path.

The repository-level integrity workflow protects the corpus itself. TypeScript,
Python, Go, and Rust adopt it in separate release-scoped PRs so a language is
never advertised as v3-conformant before its runtime consumer passes.
