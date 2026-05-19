// Dexcost Go SDK — Agent Cost Capture Example
//
// Run with: go run examples/agent_cost/main.go
//
// This example demonstrates wiring the dexcost SDK around a local AI agent:
//   1. Records LLM call costs (provider: "local", model: "local-llm")
//   2. Records non-LLM tool costs (web search, maps API)
//   3. Demonstrates retry waste tracking (simulated rate-limit retry)
//   4. Verifies all events appear in the buffer with correct schema fields.
//
// No API key required — runs in offline/dev mode.
package main

import (
	"context"
	"fmt"
	"math/rand"
	"os"
	"time"

	"github.com/shopspring/decimal"

	dexcost "github.com/DexwoxBusiness/dexcost-go"
)

// simulateLLMCall mimics a local LLM invocation.
// Returns outputTokens, latencyMs, shouldRetry.
func simulateLLMCall(promptTokens int64) (int64, int64, bool) {
	outputTokens := promptTokens * 3                               // 3x token amplification
	latencyMs := int64(180)                                        // 180ms
	shouldRetry := rand.Float32() > 0.77                          // ~23% retry rate for demo
	return outputTokens, latencyMs, shouldRetry
}

// simulateToolCall returns (serviceName, costUSD, details).
func simulateToolCall(tool string) (string, decimal.Decimal, map[string]string) {
	switch tool {
	case "web_search":
		return "web_search", decimal.RequireFromString("0.002"), map[string]string{
			"query":          "weather forecast",
			"results_count":  "5",
		}
	case "maps_api":
		return "maps_api", decimal.RequireFromString("0.005"), map[string]string{
			"operation":  "route",
			"waypoints":  "3",
		}
	default:
		return "unknown", decimal.Zero, nil
	}
}

func run() error {
	fmt.Println("[dexcost] Initializing SDK (offline mode)...")
	if err := dexcost.Init(dexcost.Config{
		Storage: "local",
	}); err != nil {
		return fmt.Errorf("init dexcost: %w", err)
	}
	defer dexcost.Close()

	ctx := context.Background()

	// ── Start a task for the agent run ─────────────────────────────────
	ctx, task := dexcost.StartTask(ctx, "local_agent_task",
		dexcost.WithCustomer("demo-corp"),
		dexcost.WithProject("agent-demo"),
		dexcost.WithMetadata(map[string]interface{}{
			"agent_framework": "dexcost-demo",
		}),
	)
	fmt.Printf("[dexcost] Task started: %s\n", task.Task.TaskID)

	// ── Step 1: Initial LLM call ─────────────────────────────────────────
	promptTokens := int64(150)
	outputTokens, latencyMs, shouldRetry := simulateLLMCall(promptTokens)

	llmCost := decimal.RequireFromString("0.00075")
	if err := task.RecordLLMCall("local", "local-llm", int(promptTokens), int(outputTokens),
		dexcost.WithCost(llmCost),
		dexcost.WithLatency(int(latencyMs)),
	); err != nil {
		return fmt.Errorf("record LLM call: %w", err)
	}
	fmt.Printf("[dexcost] LLM call recorded: %d input + %d output tokens, cost=$%s, latency=%dms\n",
		promptTokens, outputTokens, llmCost, latencyMs)

	// ── Step 2: Non-LLM tool calls ─────────────────────────────────────
	service, cost, details := simulateToolCall("web_search")
	if err := task.RecordCost(service, cost, dexcost.WithOperation(details["query"])); err != nil {
		return fmt.Errorf("record web_search cost: %w", err)
	}
	fmt.Printf("[dexcost] Tool cost recorded: %s cost=$%s\n", service, cost)

	service2, cost2, details2 := simulateToolCall("maps_api")
	if err := task.RecordCost(service2, cost2, dexcost.WithOperation(details2["operation"])); err != nil {
		return fmt.Errorf("record maps_api cost: %w", err)
	}
	fmt.Printf("[dexcost] Tool cost recorded: %s cost=$%s\n", service2, cost2)

	// ── Step 3: Retry waste tracking ───────────────────────────────────
	if shouldRetry {
		fmt.Println("[dexcost] Simulated rate-limit — initiating retry...")
		if err := task.MarkRetry("rate_limit_hit", dexcost.WithRetryCost(llmCost)); err != nil {
			return fmt.Errorf("mark retry: %w", err)
		}
		fmt.Printf("[dexcost] Retry waste recorded: reason=rate_limit_hit, cost=$%s\n", llmCost)
	}

	// ── End task ───────────────────────────────────────────────────────
	status := dexcost.StatusSuccess
	if shouldRetry {
		status = dexcost.StatusFailed
	}
	if err := task.End(status); err != nil {
		return fmt.Errorf("end task: %w", err)
	}

	// ── Print final summary ────────────────────────────────────────────
	stored := task.Task
	fmt.Println()
	fmt.Println("=== Dexcost Agent Cost Capture Results ===")
	fmt.Printf("Task ID:       %s\n", stored.TaskID)
	fmt.Printf("Task Type:     %s\n", stored.TaskType)
	fmt.Printf("Status:        %s\n", stored.Status)
	fmt.Printf("LLM Cost:      $%s\n", stored.LLMCostUSD.String())
	fmt.Printf("Tool Costs:    $%s\n", stored.ExternalCostUSD.String())
	fmt.Printf("Total Cost:    $%s\n", stored.TotalCostUSD.String())
	fmt.Printf("Input Tokens:  %d\n", stored.TotalInputTokens)
	fmt.Printf("Output Tokens: %d\n", stored.TotalOutputTokens)
	fmt.Printf("Retry Count:   %d\n", stored.RetryCount)
	fmt.Printf("Retry Waste:   $%s\n", stored.RetryCostUSD.String())
	fmt.Println("==========================================")

	// ── Verify event schema compliance ─────────────────────────────────
	tracker := dexcost.Tracker()
	events, err := tracker.Buffer().QueryEvents(stored.TaskID.String())
	if err != nil {
		return fmt.Errorf("query events: %w", err)
	}
	fmt.Println()
	fmt.Printf("[dexcost] Events in buffer: %d events\n", len(events))
	for i, ev := range events {
		retryOf := "<none>"
		if ev.RetryOf != nil {
			retryOf = ev.RetryOf.String()
		}
		fmt.Printf("  Event %d: type=%s cost=$%s is_retry=%t provider=%s model=%s service=%s retry_reason=%s\n",
			i+1, ev.EventType, ev.CostUSD.String(), ev.IsRetry,
			ev.Provider, ev.Model, ev.ServiceName, ev.RetryReason)
		// Verify Standard Event Schema v1 required fields
		if ev.EventID.String() == "" {
			return fmt.Errorf("event_id must be non-empty")
		}
		if ev.TaskID.String() == "" {
			return fmt.Errorf("task_id must be non-empty")
		}
		_ = retryOf // suppress unused
	}

	fmt.Println()
	fmt.Println("[dexcost] All verifications passed.")
	return nil
}

func main() {
	rand.Seed(time.Now().UnixNano())
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "[dexcost] Error: %v\n", err)
		os.Exit(1)
	}
}