// Example quickstart for dexcost-go.
//
// Run with:
//
//	go run examples/quickstart/main.go
//
// This demonstrates creating a task, recording an LLM call and an external cost,
// then printing the aggregated cost to stdout.
package main

import (
	"context"
	"fmt"
	"log"

	"github.com/shopspring/decimal"

	dexcost "github.com/DexwoxBusiness/dexcost-sdk/go"
)

func main() {
	if err := run(); err != nil {
		log.Fatalf("dexcost quickstart failed: %v", err)
	}
}

func run() error {
	// Initialize dexcost in local mode (no cloud push, stores in ~/.dexcost).
	if err := dexcost.Init(dexcost.Config{
		Storage: "local",
	}); err != nil {
		return fmt.Errorf("init dexcost: %w", err)
	}
	defer dexcost.Close()

	ctx := context.Background()

	// Start a task with customer and project attribution.
	ctx, task := dexcost.StartTask(ctx, "resolve_ticket",
		dexcost.WithCustomer("acme-corp"),
		dexcost.WithProject("support-q1"),
	)

	// Record an LLM call (openai, gpt-4o, 1000 input, 500 output tokens).
	// The cost is auto-computed from the bundled pricing catalog.
	if err := task.RecordLLMCall("openai", "gpt-4o", 1000, 500); err != nil {
		return fmt.Errorf("record LLM call: %w", err)
	}

	// Record a non-LLM cost via the task's RecordCost method.
	if err := task.RecordCost("stripe_api", decimal.RequireFromString("0.025"), dexcost.WithOperation("payment_lookup")); err != nil {
		return fmt.Errorf("record external cost: %w", err)
	}

	// End the task successfully.
	if err := task.End(dexcost.StatusSuccess); err != nil {
		return fmt.Errorf("end task: %w", err)
	}

	// Retrieve the final task to show aggregated cost.
	tracker := dexcost.Tracker()
	stored, err := tracker.Buffer().GetTask(task.Task.TaskID.String())
	if err != nil {
		return fmt.Errorf("get task: %w", err)
	}

	fmt.Println("=== Dexcost Quickstart ===")
	fmt.Printf("Task ID:       %s\n", stored.TaskID)
	fmt.Printf("Task Type:     %s\n", stored.TaskType)
	fmt.Printf("Customer:      %s\n", stored.CustomerID)
	fmt.Printf("Project:       %s\n", stored.ProjectID)
	fmt.Printf("Status:        %s\n", stored.Status)
	fmt.Printf("LLM Cost USD:  %s\n", stored.LLMCostUSD.String())
	fmt.Printf("External Cost: %s\n", stored.ExternalCostUSD.String())
	fmt.Printf("Total Cost:    %s\n", stored.TotalCostUSD.String())
	fmt.Printf("Input Tokens:  %d\n", stored.TotalInputTokens)
	fmt.Printf("Output Tokens: %d\n", stored.TotalOutputTokens)
	fmt.Printf("Retry Count:   %d\n", stored.RetryCount)

	// Sanity check: total cost should be non-zero after an LLM call.
	if stored.TotalCostUSD.IsZero() {
		return fmt.Errorf("expected non-zero total cost")
	}

	fmt.Println("\nQuickstart completed successfully.")
	_ = ctx // suppress unused warning
	return nil
}