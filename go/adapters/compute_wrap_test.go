// Serverless handler wraps for compute capture.
// Mirrors python/tests/test_compute_wrap.py.

package adapters

import (
	"context"
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

// computeEventCollector is a minimal Buffer implementation that captures
// inserted events in memory — used by the wrap tests to verify emission
// without spinning up the full SQLite tracker.
type computeEventCollector struct {
	events []core.Event
}

func (b *computeEventCollector) InsertTask(t core.Task) error    { return nil }
func (b *computeEventCollector) UpdateTask(t core.Task) error    { return nil }
func (b *computeEventCollector) GetTask(id string) (*core.Task, error) {
	return nil, nil
}
func (b *computeEventCollector) InsertEvent(e core.Event) error {
	b.events = append(b.events, e)
	return nil
}
func (b *computeEventCollector) UpdateEvent(e core.Event) error  { return nil }
func (b *computeEventCollector) QueryEvents(taskID string) ([]core.Event, error) {
	return b.events, nil
}
func (b *computeEventCollector) Close() error { return nil }

func newComputeWrapTestEnv(t *testing.T) (*computeEventCollector, *core.Task) {
	t.Helper()
	core.ResetComputeRegistryForTests()
	t.Cleanup(core.ResetComputeRegistryForTests)
	buf := &computeEventCollector{}
	SetComputeEventBuffer(buf)
	t.Cleanup(func() { SetComputeEventBuffer(nil) })
	task := &core.Task{TaskID: uuid.New()}
	return buf, task
}

func TestLambdaWrapEmitsEventWithCostPending(t *testing.T) {
	buf, task := newComputeWrapTestEnv(t)
	t.Setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "512")
	t.Setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand")
	t.Setenv("AWS_REGION", "us-east-1")

	ctx := core.WithTask(context.Background(), task)

	captured := false
	wrapped := WrapLambdaHandler(func(ctx context.Context, evt map[string]any) (string, error) {
		captured = true
		return "ok", nil
	})
	if _, err := wrapped(ctx, map[string]any{"hello": "world"}); err != nil {
		t.Fatalf("wrapped handler err: %v", err)
	}
	if !captured {
		t.Fatal("inner handler never called")
	}
	if len(buf.events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(buf.events))
	}
	ev := buf.events[0]
	if ev.EventType != core.EventTypeComputeCost {
		t.Fatalf("event type = %s, want compute_cost", ev.EventType)
	}
	if ev.Details["billing_model"] != "lambda" {
		t.Fatalf("billing_model = %v", ev.Details["billing_model"])
	}
	if ev.Details["region"] != "us-east-1" {
		t.Fatalf("region = %v", ev.Details["region"])
	}
	if pending, _ := ev.Details["cost_pending"].(bool); !pending {
		t.Fatal("cost_pending should be true at emit time")
	}
	if ev.Details["initialization_type"] != "on-demand" {
		t.Fatalf("initialization_type = %v", ev.Details["initialization_type"])
	}
}

func TestLambdaWrapPassThroughWhenNoTask(t *testing.T) {
	buf, _ := newComputeWrapTestEnv(t)

	called := false
	wrapped := WrapLambdaHandler(func(ctx context.Context, evt int) (int, error) {
		called = true
		return evt * 2, nil
	})
	out, err := wrapped(context.Background(), 21)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if out != 42 {
		t.Fatalf("out = %d, want 42", out)
	}
	if !called {
		t.Fatal("inner not called")
	}
	if len(buf.events) != 0 {
		t.Fatalf("expected 0 events (no task in ctx), got %d", len(buf.events))
	}
}

func TestLambdaWrapHandlerErrorStillEmits(t *testing.T) {
	buf, task := newComputeWrapTestEnv(t)
	t.Setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "128")
	t.Setenv("AWS_REGION", "us-east-1")

	ctx := core.WithTask(context.Background(), task)
	boom := errors.New("handler explosion")
	wrapped := WrapLambdaHandler(func(ctx context.Context, evt int) (int, error) {
		return 0, boom
	})
	_, err := wrapped(ctx, 1)
	if !errors.Is(err, boom) {
		t.Fatalf("err = %v, want %v (re-raised after capture)", err, boom)
	}
	if len(buf.events) != 1 {
		t.Fatalf("expected 1 event even on handler error, got %d", len(buf.events))
	}
}

func TestAllFiveWrapsExportedAndCallable(t *testing.T) {
	t.Setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "128")
	t.Setenv("AWS_REGION", "us-east-1")

	// Compile-time check that all five wraps exist with the expected
	// signature.
	_ = WrapLambdaHandler(func(ctx context.Context, e int) (int, error) { return 0, nil })
	_ = WrapCloudRunHandler(func(ctx context.Context, e int) (int, error) { return 0, nil })
	_ = WrapCloudFunctionsHandler(func(ctx context.Context, e int) (int, error) { return 0, nil })
	_ = WrapAzureFunctionsHandler(func(ctx context.Context, e int) (int, error) { return 0, nil })
	_ = WrapVercelHandler(func(ctx context.Context, e int) (int, error) { return 0, nil })
}
