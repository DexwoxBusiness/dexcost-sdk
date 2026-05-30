# Security Policy

DexCost SDKs run inside customer applications and may observe metadata about AI
tasks, LLM usage, HTTP calls, cloud runtime, compute usage, GPU usage, and
customer/project attribution. Please report suspected vulnerabilities privately
so we can coordinate a fix before public disclosure.

## Reporting a Vulnerability

Email security@dexcost.io with the subject line `Security disclosure: <short
summary>`.

Please include as much of the following as you can:

- Affected SDK or component: Python, TypeScript, Rust, Go, fixtures, schema,
  transport, pricing data, redaction, or documentation.
- Affected version, commit, or branch.
- Reproduction steps or proof of concept.
- Impact: data exposure, credential leakage, remote code execution, denial of
  service, incorrect cost attribution, unsafe endpoint handling, or another risk.
- Any logs, stack traces, payload examples, or sanitized screenshots.
- Whether the issue is already public.

Do not open a public GitHub issue for vulnerabilities. If you are unsure whether
something is security-sensitive, email security@dexcost.io first.

## What to Expect

We aim to acknowledge reports within 2 business days. After triage, we will share
the expected remediation path and coordinate disclosure timing with you when
appropriate.

During remediation we may:

- Ask for extra reproduction details.
- Prepare patches across all affected SDKs.
- Add or update shared fixtures to protect cross-SDK parity.
- Publish a security advisory or release notes after a fix is available.

## Supported Versions

The SDKs are currently in the 0.x release line. Security fixes are targeted at
the latest released version and the active development branch. If a fix affects
wire format, event schema, or control-plane compatibility, we will call that out
in the changelog and migration notes.

| Package | Supported |
| --- | --- |
| Python `dexcost` | Latest 0.x |
| TypeScript `dexcost` | Latest 0.x |
| Rust `dexcost` | Latest 0.x |
| Go `github.com/DexwoxBusiness/dexcost-go` | Latest 0.x |

## Security-Sensitive Areas

We are especially interested in reports involving:

- API key handling, endpoint allow-listing, or cloud push authentication.
- URL, header, request, response, or customer identifier redaction.
- Local SQLite buffer permissions or event persistence behavior.
- Cross-task context leakage between customers, projects, async tasks, or
  goroutines.
- Incorrect serialization or validation of event/task schemas.
- Unbounded retries, batching, memory growth, or denial-of-service behavior.
- Dependency vulnerabilities that are reachable through normal SDK usage.

## Safe Harbor

We will not pursue legal action against good-faith security research that:

- Avoids privacy violations, data destruction, service disruption, and account
  compromise.
- Uses only accounts, workspaces, or systems you are authorized to test.
- Reports findings promptly to security@dexcost.io.
- Gives us a reasonable opportunity to remediate before public disclosure.

If your research could affect other users or production systems, stop and contact
us before continuing.
