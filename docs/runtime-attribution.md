# Modal and E2B: task evidence, invoice money

Opt-in Python/TypeScript capture emits `runtime.task_seconds` on an explicit
`billing_account_id/resource_id`. These are **client-observed task wall-time
weights**, not billed sandbox lifetime, CPU utilization, or a dollar estimate.
Only the control-plane billing-period allocator creates attributed money from
reconciled provider invoices. No provider credentials or extra requests are used.

## Modal / generic runtime work

```python
from dexcost import wrap_runtime_handler

# remote_fn is a sync/async callable; use the app/object ID from Modal's report.
tracked = wrap_runtime_handler(
    remote_fn, tracker, service_key="modal_compute",
    billing_account_id="team1", resource_id="ap-example",
)
# Invoke tracked(...) inside your existing DexCost task context.
```

```typescript
import { wrapRuntimeHandler } from "@dexcost/sdk";
const tracked = wrapRuntimeHandler(remoteFn, tracker, {
  serviceKey: "modal_compute", billingAccountId: "team1", resourceId: "ap-example",
});
// Invoke tracked(...) inside tracker.track(...).
```

Wrap completed work, not submission/polling or a background-job handle. A remote
call includes client/network/queue time; a wrapper inside the runtime measures
that handler's local wall time instead. Use **one** placement consistently for a
resource and denominator; do not mix these allocation policies. Use instead of
the legacy monetary GPU wrapper, never both for the same bill. Modal tags are
not inferred as task ownership.

## E2B

```python
from dexcost import instrument_e2b_sandbox
tracked = instrument_e2b_sandbox(sandbox, tracker, billing_account_id="team1")
tracked.commands.run("your command")  # inside an active task; await for AsyncSandbox
tracked.run_code("your code")        # code-interpreter sandbox, if present
tracked.close()                      # stops capture only; does NOT kill sandbox
```

```typescript
import { instrumentE2bSandbox } from "@dexcost/sdk";
const capture = instrumentE2bSandbox(sandbox, tracker, { billingAccountId: "team1" });
await capture.sandbox.commands.run("your command"); // inside an active task
await capture.sandbox.runCode("your code");        // if code-interpreter SDK is used
capture.close();                                  // no provider resource shutdown
```

Use the returned facade. Raw client calls are not captured. `create`, `connect`,
`pause`, `kill`, timeout extensions, filesystem calls and metrics are not priced
or timed as work. Lifecycle methods delegate unchanged, including failures.
**Wrap a newly returned sandbox after reconnect/resume.** SDK `endAt`/`end_at`
is a scheduled timeout, not evidence of actual billed duration. Background
commands are skipped. In-flight work can still finish recording after `close()`.

For the SDK's symmetric helper API, `uninstrument_e2b_sandbox(tracked)` (Python)
or `uninstrumentE2bSandbox(capture)` (TypeScript) also stops capture idempotently.
It never calls a provider shutdown method.

Configuration may optionally declare integer `vcpu_count`/`vcpuCount` and
`memory_mib`/`memoryMiB`. They are matching dimensions, never inferred defaults
or pricing rates. Re-instrument after changing resource configuration.

## Guarantees and limits

- Same-resource/task nested wrappers are suppressed. Concurrent calls remain
  separate weights. No cross-process deduplication of separately wrapped work.
- Sync results, async results and raised exceptions are preserved. A returned
  execution object is not inspected for provider-specific embedded errors;
  operation status describes completion/throw of the SDK call.
- Missing task context, uncompleted/disconnected calls, nonpositive durations
  and durations over 24 hours produce no usable allocation evidence. A local
  cancellation closes only observed work; it does not imply remote termination.
- No commands, source code, outputs, environment names, credentials or exception
  messages enter telemetry. Identifiers must be opaque account/resource IDs.
- Server clips closed intervals at bill boundaries. It does not assign idle,
  disconnected, storage, transfer, plan fees, or unsupported usage to tasks.
- A full-period denominator must use the same wall-time weighting policy,
  including known unobserved work. If unavailable, omit it and keep money
  unallocated. This is a shared-cost allocation policy, not exact per-task CPU
  billing. Do not substitute billed CPU-seconds or sandbox TTL for that basis.

Verified references (2026-09-27): [Modal billing](https://modal.com/docs/guide/billing),
[Modal report CLI](https://modal.com/docs/cli/latest/billing),
[E2B persistence](https://docs.e2b.dev/sandbox/persistence),
[E2B sandbox SDK](https://docs.e2b.dev/sdk-reference/js-sdk/v2.3.0/sandbox),
[E2B prices](https://e2b.dev/pricing). Public rates are not copied into the SDK.
