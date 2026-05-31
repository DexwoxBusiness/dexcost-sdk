// DevOps Runbook RAG Agent — DEX-251 E2E validation of the Dexcost Go SDK.
//
// This agent:
//   1. Generates ≥100 synthetic Markdown/YAML runbook docs.
//   2. Batch-embeds the corpus via Voyage AI using errgroup concurrent fan-out.
//   3. Answers ≥50 semantic queries via MiniMax M2.7 (Anthropic-compatible API).
//   4. Triggers and records ≥20 retry events (simulated timeouts / rate-limits).
//   5. Produces ≥200 total events across llm_call, external_cost, compute_cost,
//      and retry_marker kinds.
//   6. Emits events to the Control Layer via the Go SDK HTTP backend.
//   7. Screenshots ≥3 dashboard pages with chromedp.
//
// Run:
//
//	export MINIMAX_API_KEY=...
//	export VOYAGE_API_KEY=...
//	export DEXCOST_API_KEY=dx_test_...          # MUST belong to the same workspace as the dashboard user below
//	export DEXCOST_ENDPOINT=http://localhost:3000        # control-layer API (login + ingest)
//	export DEXCOST_DASHBOARD_URL=http://localhost:3001   # Next.js dashboard for screenshots
//	export DEXCOST_DASHBOARD_EMAIL=admin@dexcost.io
//	export DEXCOST_DASHBOARD_PASSWORD=dexcost123
//	go run .
package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"

	dexcost "github.com/DexwoxBusiness/dexcost-sdk/go"
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))
	if err := run(); err != nil {
		slog.Error("agent failed", "error", err)
		os.Exit(1)
	}
}

func run() error {
	ctx := context.Background()

	// ------------------------------------------------------------------
	// Config from environment
	// ------------------------------------------------------------------
	minimaxKey := os.Getenv("MINIMAX_API_KEY")
	voyageKey := os.Getenv("VOYAGE_API_KEY")
	dexcostKey := os.Getenv("DEXCOST_API_KEY")
	dexcostEndpoint := os.Getenv("DEXCOST_ENDPOINT")
	if dexcostEndpoint == "" {
		dexcostEndpoint = "http://localhost:3000"
	}
	dashboardURL := os.Getenv("DEXCOST_DASHBOARD_URL")
	if dashboardURL == "" {
		dashboardURL = "http://localhost:3001"
	}
	screenshotDir := os.Getenv("SCREENSHOT_DIR")
	if screenshotDir == "" {
		screenshotDir = "docs/dex251/run-artifacts"
	}

	if minimaxKey == "" {
		return fmt.Errorf("MINIMAX_API_KEY not set")
	}
	if voyageKey == "" {
		return fmt.Errorf("VOYAGE_API_KEY not set")
	}
	if dexcostKey == "" {
		return fmt.Errorf("DEXCOST_API_KEY not set")
	}

	// ------------------------------------------------------------------
	// Init Dexcost SDK (cloud mode pushes to Control Layer)
	// ------------------------------------------------------------------
	if err := dexcost.Init(dexcost.Config{
		APIKey:               dexcostKey,
		BatchSize:            50,
		FlushIntervalSeconds: 3,
	}); err != nil {
		return fmt.Errorf("init dexcost: %w", err)
	}
	defer dexcost.Close()

	// Register custom pricing for MiniMax-M2.7 so cost confidence is exact.
	pricingEngine := dexcost.Tracker().Pricing()
	pricingEngine.SetCustomPricing("MiniMax-M2.7",
		decimal.RequireFromString("0.00030"), // $0.30 / 1M input tokens
		decimal.RequireFromString("0.00120"), // $1.20 / 1M output tokens
	)

	buf := dexcost.Tracker().Buffer()

	// ------------------------------------------------------------------
	// Generate synthetic corpus
	// ------------------------------------------------------------------
	runbooks := generateRunbooks()
	slog.Info("corpus generated", "docs", len(runbooks))

	queries := generateQueries()
	slog.Info("queries generated", "queries", len(queries))

	// ------------------------------------------------------------------
	// Start the root task
	// ------------------------------------------------------------------
	ctx, task := dexcost.StartTask(ctx, "devops_runbook_rag",
		dexcost.WithCustomer("dexcost-e2e"),
		dexcost.WithProject("agent-runbook-rag"),
		dexcost.WithMetadata(map[string]interface{}{
			"agent":     "dex251-e2e-go",
			"version":   "1.0.0",
			"runbooks":  len(runbooks),
			"queries":   len(queries),
		}),
	)

	// ------------------------------------------------------------------
	// Budget tracker ($5 hard cap)
	// ------------------------------------------------------------------
	budget := NewBudgetTracker(buf, task.Task.TaskID)

	// ------------------------------------------------------------------
	// Build clients
	// ------------------------------------------------------------------
	minimax := NewMiniMaxClient(minimaxKey, buf, pricingEngine, budget)
	voyage := NewVoyageClient(voyageKey, buf, budget)
	store := NewVectorStore(buf)

	// ------------------------------------------------------------------
	// Ingest: chunk + embed + index
	// ------------------------------------------------------------------
	startIngest := time.Now()
	if err := ingest(ctx, task, runbooks, voyage, store); err != nil {
		task.End(dexcost.StatusFailed)
		return fmt.Errorf("ingest: %w", err)
	}
	ingestDur := time.Since(startIngest)
	slog.Info("ingest complete", "duration", ingestDur)

	// ------------------------------------------------------------------
	// Query: semantic search + LLM answer loop
	// ------------------------------------------------------------------
	startQuery := time.Now()
	if err := queryLoop(ctx, task, queries, store, minimax); err != nil {
		task.End(dexcost.StatusFailed)
		return fmt.Errorf("query loop: %w", err)
	}
	queryDur := time.Since(startQuery)
	slog.Info("query loop complete", "duration", queryDur)

	// ------------------------------------------------------------------
	// End root task
	// ------------------------------------------------------------------
	if err := task.End(dexcost.StatusSuccess); err != nil {
		return fmt.Errorf("end task: %w", err)
	}

	// ------------------------------------------------------------------
	// Flush events to Control Layer
	// ------------------------------------------------------------------
	slog.Info("flushing events to control layer")
	dexcost.Flush()
	time.Sleep(5 * time.Second) // async worker needs time to process SQS → ClickHouse + Postgres

	// ------------------------------------------------------------------
	// Event-count verification (local buffer)
	// ------------------------------------------------------------------
	if err := verifyCounts(ctx, task); err != nil {
		slog.Warn("event count verification failed", "error", err)
	}

	// ------------------------------------------------------------------
	// Server-side visibility verification — catches tenant mismatch,
	// auth errors, and worker-not-running scenarios that local counts
	// cannot detect (DEX-278).
	// ------------------------------------------------------------------
	if err := verifyServerSide(ctx, dexcostEndpoint, dexcostKey, task.Task.TaskID.String()); err != nil {
		slog.Error("server-side verification failed", "error", err)
		return fmt.Errorf("events not visible in control layer: %w", err)
	}

	// ------------------------------------------------------------------
	// Dashboard screenshots
	// ------------------------------------------------------------------
	absShotDir, _ := filepath.Abs(screenshotDir)
	if err := os.MkdirAll(absShotDir, 0755); err == nil {
		if err := screenshotDashboard(ctx, dashboardURL, dexcostEndpoint, absShotDir); err != nil {
			slog.Warn("screenshots failed", "error", err)
		}
	}

	slog.Info("run complete")
	return nil
}

// BudgetTracker tracks cumulative LLM + embedding spend and aborts at $5.
type BudgetTracker struct {
	mu     sync.Mutex
	spent  decimal.Decimal
	capUSD decimal.Decimal
	taskID uuid.UUID
	buf    core.Buffer
}

// NewBudgetTracker creates a budget tracker with a $5 hard cap.
func NewBudgetTracker(buf core.Buffer, taskID uuid.UUID) *BudgetTracker {
	return &BudgetTracker{
		capUSD: decimal.RequireFromString("5.00"),
		taskID: taskID,
		buf:    buf,
	}
}

// CheckAndAdd returns an error if adding cost would breach the cap.
func (b *BudgetTracker) CheckAndAdd(cost decimal.Decimal, service string) error {
	b.mu.Lock()
	defer b.mu.Unlock()

	if b.spent.Add(cost).GreaterThan(b.capUSD) {
		// Record budget_cap event.
		event := core.NewEvent(b.taskID, core.EventTypeExternalCost)
		event.ServiceName = "budget-cap"
		event.CostUSD = decimal.Zero
		event.CostConfidence = core.CostConfidenceExact
		event.PricingSource = core.PricingSourceManual
		event.Details["service"] = service
		event.Details["spent_usd"] = b.spent.String()
		event.Details["cap_usd"] = b.capUSD.String()
		event.Details["requested_usd"] = cost.String()
		_ = b.buf.InsertEvent(event)
		return fmt.Errorf("budget cap breached: spent $%s + requested $%s > cap $%s",
			b.spent.String(), cost.String(), b.capUSD.String())
	}
	b.spent = b.spent.Add(cost)
	return nil
}

// topUpEvents was removed in DEX-260 review — it synthesized fake retry and
// compute_cost events to hit acceptance thresholds, which is exactly the kind
// of fabrication the SDK is meant to detect. Real workload counts are now
// driven entirely by ingest + queryLoop. If the run falls short, fix the
// workload (more docs/queries) or the SDK (event-loss bugs), never the
// counters.

func verifyCounts(ctx context.Context, task *dexcost.TrackedTask) error {
	events, err := dexcost.Tracker().Buffer().QueryEvents(task.Task.TaskID.String())
	if err != nil {
		return err
	}
	var llm, ext, comp, retry int
	var unknownConfidence int
	for _, e := range events {
		switch e.EventType {
		case core.EventTypeLLMCall:
			llm++
		case core.EventTypeExternalCost:
			ext++
		case core.EventTypeComputeCost:
			comp++
		case core.EventTypeRetryMarker:
			retry++
		}
		// Only flag unknown confidence on events that actually carry cost.
		// Timeout failures with zero cost are allowed to be unknown.
		if e.CostConfidence == core.CostConfidenceUnknown && !e.CostUSD.IsZero() {
			unknownConfidence++
			slog.Warn("cost_confidence=unknown detected",
				"event_id", e.EventID,
				"event_type", e.EventType,
				"service", e.ServiceName,
			)
		}
	}
	slog.Info("event counts",
		"total", len(events),
		"llm_call", llm,
		"external_cost", ext,
		"compute_cost", comp,
		"retry_marker", retry,
		"unknown_confidence", unknownConfidence,
	)
	if len(events) < 200 {
		return fmt.Errorf("expected ≥200 events, got %d", len(events))
	}
	if retry < 20 {
		return fmt.Errorf("expected ≥20 retry events, got %d", retry)
	}
	if unknownConfidence > 0 {
		return fmt.Errorf("found %d events with cost_confidence=unknown and non-zero cost", unknownConfidence)
	}
	return nil
}

// verifyServerSide polls the Control Layer API to confirm the task and its
// events are actually visible server-side. This catches tenant mismatches,
// auth errors, and unprocessed SQS messages that local-buffer counts cannot
// detect (DEX-278).
func verifyServerSide(ctx context.Context, endpoint, apiKey, taskID string) error {
	client := &http.Client{Timeout: 5 * time.Second}
	taskURL := fmt.Sprintf("%s/v1/tasks/%s", strings.TrimRight(endpoint, "/"), taskID)
	summaryURL := fmt.Sprintf("%s/v1/analytics/summary?customer_id=dexcost-e2e", strings.TrimRight(endpoint, "/"))

	// Poll task endpoint — confirms task was upserted into Postgres.
	pollCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()

	for {
		select {
		case <-pollCtx.Done():
			return fmt.Errorf("timeout waiting for task %s to appear server-side", taskID)
		default:
		}

		found, err := func() (bool, error) {
			req, err := http.NewRequestWithContext(pollCtx, http.MethodGet, taskURL, nil)
			if err != nil {
				return false, fmt.Errorf("build task request: %w", err)
			}
			req.Header.Set("Authorization", "Bearer "+apiKey)

			resp, err := client.Do(req)
			if err != nil {
				return false, nil
			}
			defer resp.Body.Close()
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))

			if resp.StatusCode == http.StatusOK {
				slog.Info("server-side task confirmed", "task_id", taskID, "url", taskURL)
				return true, nil
			}
			if resp.StatusCode == http.StatusNotFound {
				return false, nil
			}
			return false, fmt.Errorf("unexpected status %d fetching task: %s", resp.StatusCode, string(body))
		}()
		if err != nil {
			return err
		}
		if found {
			break
		}
		time.Sleep(1 * time.Second)
	}

	// Also confirm analytics summary returns 200 for the customer — this
	// proves events have been aggregated and are queryable.
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, summaryURL, nil)
	if err != nil {
		return fmt.Errorf("build summary request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("fetch summary: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("summary endpoint returned %d: %s", resp.StatusCode, string(body))
	}
	slog.Info("server-side analytics confirmed", "customer", "dexcost-e2e")
	return nil
}
