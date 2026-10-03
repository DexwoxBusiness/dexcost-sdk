# Browser session attribution (Python + TypeScript)

Browserbase and Browserless use the same durable provider-job ledger and server
invoice allocator. Neither SDK computes browser dollars. The old bundled flat
prices have been removed. Go and Rust are deliberately outside this release.

## Native Browserbase capture

```python
from dexcost import instrument_browserbase

# tracker is your initialized DexCost tracker; client is Browserbase/AsyncBrowserbase.
browserbase = instrument_browserbase(client, tracker, billing_account_id="account-id")
# Inside the owning DexCost task:
session = browserbase.sessions.create(project_id="project-id", proxies=True)
# Run the browser, then release it using your normal provider lifecycle.
# Retrieve after provider completion (possibly from a different process/task):
final = browserbase.sessions.retrieve(session.id)
```

```typescript
import { instrumentBrowserbase } from "@dexcost/sdk";
const browserbase = instrumentBrowserbase(client, tracker, { billingAccountId: "account-id" });
// Inside the owning DexCost task:
const session = await browserbase.sessions.create({ projectId: "project-id", proxies: true });
// After normal browser lifecycle completion:
const final = await browserbase.sessions.retrieve(session.id);
```

Use the returned facade. No global patch, background polling, account login,
extra API call, or provider shutdown is performed. Sync and async Python clients
are supported. TypeScript preserves native receivers and APIPromise helpers;
normal await/then and withResponse capture parsed responses. Raw/streaming
response interfaces pass through without capture.

The facade binds `id`, `projectId` and `startedAt` at creation, then records the
first terminal `COMPLETED`, `ERROR` or `TIMED_OUT` response with `endedAt`.
`expiresAt`, `updatedAt`, client disconnect, and `REQUEST_RELEASE` are not end
times. Sessions without a verifiable start/task binding remain unallocated.
Elapsed seconds are provider-observed usage, not rounded billable seconds.
Proxy bytes are captured automatically only when create used `proxies=True` /
`proxies: true`; missing/invalid bytes leave that managed-proxy snapshot pending.
External or mixed proxy configurations are not billed as Browserbase traffic.
Explicit verified configurations can use the normalized binding API below.

## Explicit Browserless usage mapping

Ordinary browser control calls do **not** expose reliable final billed units.
Bind the provider connection/request ID to its task and submit verified
per-connection usage from your provider billing integration. This is explicit
capture, **not an automatic Browserless account/export collector**. If your plan
only exposes account aggregates, keep them as invoice denominators, not task
events. We do not invent a session-export schema that the public docs do not give.

```python
from dexcost import bind_browser_session, record_browser_session

# Inside the owning task; provider_start/provider_end are timezone-aware datetimes.
bind_browser_session(tracker, service_key="browserless", billing_account_id="account-id",
    resource_id="fleet-or-key-id", session_id="connection-id", started_at=provider_start)
# Later, using real per-connection billing facts; decimal strings, not floats:
record_browser_session(tracker, service_key="browserless", billing_account_id="account-id",
    session_id="connection-id", ended_at=provider_end, status="succeeded",
    time_units="3", proxy_units="0.125", captcha_units="10")
```

```typescript
import { bindBrowserSession, recordBrowserSession } from "@dexcost/sdk";
bindBrowserSession(tracker, { serviceKey: "browserless", billingAccountId: "account-id",
  resourceId: "fleet-or-key-id", sessionId: "connection-id", startedAt: providerStart });
recordBrowserSession(tracker, { serviceKey: "browserless", billingAccountId: "account-id",
  sessionId: "connection-id", endedAt: providerEnd, status: "succeeded",
  timeUnits: "3", proxyUnits: "0.125", captchaUnits: "10" });
```

The sample quantities are illustrative, not prices or inferred meter values.
Browserless documents 30-second rounded browser-connection units, separate
proxy units and successful CAPTCHA units. Agent Run turn `units` instead represent
model tokens and MUST NOT be passed here. Never combine `totalUnitsUsed` with its
time/proxy/CAPTCHA components or round local Playwright durations into these fields.
Reconnects with a new billed connection need a new ID; repeated tabs or polls of
one connection reuse its ID. A shared connection must have one owning task; this
release does not infer a split among tasks.

## Corrections, privacy and money

- IDs must be opaque `[A-Za-z0-9._-]` values, up to 100 characters. Never pass
  tokens, WebSocket URLs, cookies, page content, screenshots or arbitrary metadata.
- Bindings persist in SQLite. Task ownership cannot change on a later poll.
- Binding is revision 1; initial usage defaults to revision 2. Corrections must
  explicitly pass revision 3, 4, etc. Each is a full snapshot: omitted meters are
  removed, not added. Old revisions return false; conflicts/gaps raise. Use an
  ordered, stable revision mapping in an external importer. Native Browserbase
  polling never overwrites an existing terminal snapshot; submit verified later
  corrections explicitly with `record_browser_session` / `recordBrowserSession`.
- Failed/cancelled sessions can consume usage. No terminal callback means no
  invented time, zero bill, or background cleanup.
- The explicit APIs validate and raise; the native facade fails open without
  changing provider results/errors. `uninstrument_browserbase` /
  `uninstrumentBrowserbase` stops capture, not the remote browser.
- The server requires closed, reconciled invoice amounts and an explicit
  account/resource match. Raw usage is an allocation weight, never independent
  cash. Keep subscription fees, adjustments, missing usage and cross-period
  cumulative sessions unallocated when no defensible task split exists.
- Do not additionally wrap the same work in legacy `track_browser` / `trackBrowser`
  with a manual monetary rate; those are independent user-supplied estimates.

Official contracts verified 2026-09-27:
[Browserbase session schema](https://github.com/browserbase/sdk-python/blob/main/src/browserbase/types/session.py),
[Browserbase pricing](https://www.browserbase.com/pricing),
[Browserless unit consumption](https://docs.browserless.io/overview/unit-consumption),
[Browserless account export](https://docs.browserless.io/enterprise/private-deployment/graphql-api),
[Browserless Agent Run](https://docs.browserless.io/rest-apis/agent-run).
