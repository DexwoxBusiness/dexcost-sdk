# dexcost-go

Go SDK for [dexcost](https://github.com/DexwoxBusiness/dexcost-sdk) -- Agent Unit Economics platform. Track LLM costs, non-LLM service fees, and retry waste attributed to customers, projects, and workflows.

## Installation

```bash
go get github.com/DexwoxBusiness/dexcost-sdk/go
```

Requires Go 1.21+.

## Quickstart

```go
package main

import (
    "context"
    "fmt"

    "github.com/shopspring/decimal"

    dexcost "github.com/DexwoxBusiness/dexcost-sdk/go"
)

func main() {
    // Initialize in local-only mode (no API key needed).
    err := dexcost.Init(dexcost.Config{Storage: "local"})
    if err != nil {
        panic(err)
    }
    defer dexcost.Close()

    // Start a task.
    ctx, task := dexcost.StartTask(context.Background(), "resolve_ticket",
        dexcost.WithCustomer("acme-corp"),
        dexcost.WithProject("support"),
    )

    // Record an LLM call (auto-priced from bundled model data).
    task.RecordLLMCall("openai", "gpt-4o", 1000, 500)

    // Record a non-LLM cost.
    task.RecordCost("google_maps", decimal.NewFromFloat(0.005))

    // Mark a retry.
    task.MarkRetry("rate_limit")

    // End the task.
    task.End(dexcost.StatusSuccess)

    fmt.Printf("Total cost: %s USD\n", task.Task.TotalCostUSD)
    _ = ctx // ctx carries the task for nested operations
}
```

## Cloud Mode

To push events to the dexcost Control Layer:

```go
dexcost.Init(dexcost.Config{
    APIKey: "dx_live_your_key_here",  // or set DEXCOST_API_KEY env var
})
defer dexcost.Close()
```

Events are buffered locally in SQLite and pushed in batches every 5 seconds.

### Endpoint

The Control Layer endpoint defaults to `https://api.dexcost.io`. To override it
(e.g. for local end-to-end testing), set `Config.Endpoint` explicitly:

```go
dexcost.Init(dexcost.Config{
    APIKey:   "dx_test_your_key_here",
    Endpoint: "http://localhost:3001", // explicit, trusted; http:// is allowed
})
```

The endpoint comes ONLY from this in-code field. The `DEXCOST_ENDPOINT`
environment variable is no longer read — this prevents a hostile process
environment (e.g. a compromised CI runner or container) from redirecting
telemetry and the Bearer API key to an attacker-controlled host. A non-empty
`Endpoint` must start with `http://` or `https://`; otherwise it is ignored and
the production default is used.

## HTTP Middleware

### net/http

```go
import "github.com/DexwoxBusiness/dexcost-sdk/go/middleware"

tracker := dexcost.Tracker()
mux := http.NewServeMux()
mux.Handle("/api/", middleware.HTTPMiddleware(tracker, "api_request")(yourHandler))
```

### Gin

```go
import "github.com/DexwoxBusiness/dexcost-sdk/go/middleware"

r := gin.Default()
r.Use(middleware.GinMiddleware(dexcost.Tracker(), "api_request"))
```

### Echo

```go
import "github.com/DexwoxBusiness/dexcost-sdk/go/middleware"

e := echo.New()
e.Use(middleware.EchoMiddleware(dexcost.Tracker(), "api_request"))
```

## Nested Tasks

Tasks are linked via `context.Context`. Child tasks automatically get `parent_task_id` set:

```go
ctx, parent := dexcost.StartTask(ctx, "workflow")
ctx, child := dexcost.StartTask(ctx, "sub_step")
// child.Task.ParentTaskID == parent.Task.TaskID
child.End(dexcost.StatusSuccess)
parent.End(dexcost.StatusSuccess)
```

## Custom Pricing

Override bundled LLM pricing:

```go
tracker := dexcost.Tracker()
tracker.Pricing().SetCustomPricing("my-model",
    decimal.RequireFromString("0.001"),  // per 1k input tokens
    decimal.RequireFromString("0.002"),  // per 1k output tokens
)
```

Register non-LLM service rates:

```go
tracker.Rates().Register("google_maps", "request", decimal.RequireFromString("0.005"))
// Then use task.RecordUsage("google_maps", 10) to auto-compute cost
```

## Key Design Decisions

- All costs use `shopspring/decimal` -- never float64 for money
- SQLite buffer uses `modernc.org/sqlite` -- pure Go, no CGO required
- UUIDs and decimals stored as TEXT in SQLite for precision
- Raw parameterized SQL, no ORM
- Retry waste is a first-class metric (`is_retry`, `retry_reason`, `retry_of`)
- Schema v1 compatible with the Python and TypeScript SDKs

## Testing

```bash
go test ./... -v -count=1
```

## Privacy

When you connect to the Dexcost Control Layer, the SDK transmits usage data
subject to our [Privacy Policy](https://dexcost.io/privacy).

## License

MIT — see [LICENSE](LICENSE).
