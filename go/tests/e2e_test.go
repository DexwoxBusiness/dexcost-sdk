package tests

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/shopspring/decimal"

	dexcost "github.com/DexwoxBusiness/dexcost-sdk/go"
)

// TestE2E_LocalControlLayer exercises the SDK against a local control-layer stack.
// It requires the Docker Compose stack from infra/docker-compose.yml to be running.
// Environment variables used:
//   DEXCOST_ENDPOINT - defaults to http://localhost:3001
//   DEXCOST_API_KEY   - required for cloud mode (use dx_test_* for test key)
func TestE2E_LocalControlLayer(t *testing.T) {
	// Skip if not running against local stack — check for explicit opt-in.
	if os.Getenv("DEXCOST_E2E_LOCAL") != "1" {
		t.Skip("set DEXCOST_E2E_LOCAL=1 to run E2E against local stack")
	}

	endpoint := os.Getenv("DEXCOST_ENDPOINT")
	if endpoint == "" {
		endpoint = "http://localhost:3001"
	}
	apiKey := os.Getenv("DEXCOST_API_KEY")
	if apiKey == "" {
		t.Fatal("DEXCOST_API_KEY must be set for E2E (use dx_test_*)")
	}

	// Initialize SDK in cloud mode with the local endpoint.
	if err := dexcost.Init(dexcost.Config{
		APIKey:    apiKey,
		Storage:   "", // auto-detect: cloud because API key is set
		BatchSize: 10,
	}); err != nil {
		t.Fatalf("init dexcost: %v", err)
	}
	defer dexcost.Close()

	// Create a task and record events.
	ctx := context.Background()
	ctx, task := dexcost.StartTask(ctx, "e2e_test_task",
		dexcost.WithCustomer("e2e-customer"),
		dexcost.WithProject("e2e-project"),
	)

	// Record an LLM call.
	if err := task.RecordLLMCall("openai", "gpt-4o", 1000, 500); err != nil {
		t.Fatalf("record LLM call: %v", err)
	}

	// Record an external cost.
	if err := task.RecordCost("test_service", decimal.RequireFromString("0.05"), dexcost.WithOperation("test_op")); err != nil {
		t.Fatalf("record external cost: %v", err)
	}

	// End the task.
	if err := task.End(dexcost.StatusSuccess); err != nil {
		t.Fatalf("end task: %v", err)
	}

	// Flush to push events to the control layer.
	dexcost.Flush()

	// Poll the dashboard API to verify the task appears.
	taskID := task.Task.TaskID.String()
	pollCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	err := pollForTask(pollCtx, endpoint, apiKey, taskID)
	if err != nil {
		t.Fatalf("event not visible in control layer within 5s: %v", err)
	}

	t.Logf("E2E test passed: task %s visible in control layer", taskID)
}

// pollForTask polls GET /v1/tasks/{task_id} until the task is found or the context times out.
func pollForTask(ctx context.Context, endpoint, apiKey, taskID string) error {
	client := &http.Client{Timeout: 2 * time.Second}
	url := fmt.Sprintf("%s/v1/tasks/%s", endpoint, taskID)

	for {
		select {
		case <-ctx.Done():
			return fmt.Errorf("timeout waiting for task %s: %w", taskID, ctx.Err())
		default:
		}

		req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+apiKey)
		req.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(req)
		if err != nil {
			// Connection refused means server not ready — keep polling.
			time.Sleep(500 * time.Millisecond)
			continue
		}
		defer resp.Body.Close()

		if resp.StatusCode == http.StatusOK {
			return nil // Task found.
		}
		if resp.StatusCode == http.StatusNotFound {
			// Task not created yet — keep polling.
			time.Sleep(500 * time.Millisecond)
			continue
		}

		// Unexpected status — read body and abort.
		var body struct{ Error string }
		json.NewDecoder(resp.Body).Decode(&body)
		return fmt.Errorf("unexpected status %d: %s", resp.StatusCode, body.Error)
	}
}