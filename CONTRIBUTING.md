# Contributing to DexCost SDKs

Thanks for your interest in contributing! DexCost is a set of open-source SDKs
(Python, TypeScript, Rust, Go) for tracking the unit economics of AI agents. This
guide explains how to set up the project, the standards we hold code to, and how to
get a change merged.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).
For help choosing the right contact channel, see [SUPPORT.md](SUPPORT.md).

---

## Ways to contribute

- **Report a bug** — open a [GitHub issue](https://github.com/DexwoxBusiness/dexcost-sdk/issues)
  with a minimal reproduction, the SDK + language version, and what you expected.
- **Request a feature** — open an issue describing the use case before sending a large PR.
- **Improve docs** — typos, clearer examples, and better READMEs are always welcome.
- **Send code** — see the workflow below.

> ⚠️ **Never report a security vulnerability in a public issue.** Follow
> [SECURITY.md](SECURITY.md) instead.

---

## Repository layout

This is a polyglot monorepo — one SDK per top-level directory, sharing one set of
test fixtures so all four implementations stay behavior-compatible:

```
python/      Python SDK
typescript/  TypeScript SDK
rust/        Rust SDK
go/          Go SDK
fixtures/    shared parity fixtures (events, tasks, pricing inputs, expected outputs)
docs/        additional docs
scripts/     repo tooling
```

Most changes touch a single SDK directory. **Behavioral changes (pricing, attribution,
event shapes) usually need to land in all four SDKs** so they remain at parity — see
[Cross-SDK parity](#cross-sdk-parity).

---

## Development setup

Work inside the directory for the SDK you're changing.

### Python (`python/`)
```bash
cd python
pip install -e ".[all]"
pip install ruff black mypy pytest
make lint        # ruff
make format      # black
make typecheck   # mypy (strict)
make test        # pytest
```

### TypeScript (`typescript/`)
```bash
cd typescript
npm install
npm run build    # tsc
npm run lint     # tsc --noEmit
npm test         # vitest
```

### Rust (`rust/`)
```bash
cd rust
cargo build
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

### Go (`go/`)
```bash
cd go
go build ./...
gofmt -l .       # should print nothing
go vet ./...
go test ./...
```

---

## Cross-SDK parity

The SDKs are validated against shared fixtures in [`fixtures/`](fixtures/) so that the
same input produces the same cost/attribution output in every language (see the
`parity` / `cross_sdk_parity` test suites). If your change alters behavior:

1. Update or add a fixture (and its expected output) under `fixtures/`.
2. Implement the change in **each** affected SDK.
3. Make sure the parity tests pass in all four languages.

If you can only implement a change in one language, that's fine — open the PR and note
which SDKs still need it, and we'll help carry it across (or open tracking issues).

---

## Pull request workflow

1. **Fork** the repo and create a branch from `main`
   (e.g. `feat/python-cohere-streaming` or `fix/go-retry-cost`).
2. Make your change with **tests** and keep the diff focused.
3. Run the lint/type/test commands above for the SDK(s) you touched — CI runs them too.
4. Update the relevant `README.md` / `CHANGELOG.md` if you changed public behavior.
5. Open a PR with a clear description: the problem, the approach, and any parity notes.
6. A maintainer will review. Please be responsive to feedback; we aim to be quick and kind.

### Commit & PR style
- Keep commits logical and messages descriptive; conventional-commit prefixes
  (`feat:`, `fix:`, `docs:`, `chore:`) are appreciated but not required.
- One concern per PR. Large refactors are easier to review when split up.
- All contributions are licensed under the project's [MIT License](LICENSE). By
  submitting a PR you agree your contribution may be distributed under those terms.

---

## Questions

For anything that isn't a bug or feature request, reach us at **hello@dexcost.io**.
For support, use **support@dexcost.io**. For security vulnerabilities, use
**security@dexcost.io**. For Code of Conduct concerns, use **conduct@dexcost.io**.
Thanks for helping make DexCost better! 💙
