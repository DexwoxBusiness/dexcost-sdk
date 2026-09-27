# Agent database observation (Python and TypeScript)

The paired SDKs observe Neon SQL-over-HTTP (`POST /sql` on `*.neon.tech`)
and Supabase Data REST table/RPC requests (`*.supabase.co/rest/v1/...`).
They emit successful request counts and endpoint host identity, **not money**.
Neon batched transactions count as one HTTP request, not a guessed number of SQL statements.

These measurements can participate in an explicitly configured server-side
compute-cost allocation backed by actual billing records. They do not measure
compute-hours, stored GB-months, billed egress, subscription allowances or credits.
Without a linked bill and valid allocation basis, costs remain unknown/unallocated.

Native PostgreSQL and WebSocket traffic, custom domains, Supabase Auth/GraphQL/
Storage/Functions/Realtime, and automatic provider billing-account connections
are not covered by this initial slice. No automatic mapping between project,
branch and pooled/direct endpoint identities is assumed.

Observer rules live in the shared JSON manifest and signed server releases.
The SDKs do not parse SQL/results or retain connection headers. The generic
`redact_query` rule removes query strings/fragments from persisted database URLs,
including query filters on failed or unsupported descendants of observed routes.

Go and Rust are unchanged and remain outside the paired contract.
