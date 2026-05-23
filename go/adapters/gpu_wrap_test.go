// Task 7 — Serverless GPU handler wraps tests. Mirrors python commit fc0860a.

package adapters

import (
	"context"
	"os"
	"testing"

	"github.com/google/uuid"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

// stubEventBuffer captures inserted events for assertions.
type gpuStubBuffer struct {
	events []core.Event
}

func (b *gpuStubBuffer) InsertEvent(e core.Event) error {
	b.events = append(b.events, e)
	return nil
}

func setupGPUWrapTest(t *testing.T) (*gpuStubBuffer, context.Context, *core.Task) {
	t.Helper()
	// Reset NVML state + register mock with one device + self-PID samples.
	core.ResetNVMLForTests()
	selfPID := os.Getpid()
	mock := &core.MockNVMLBackend{
		Available:    true,
		DeviceCount:  1,
		ProductNames: map[int]string{0: "NVIDIA H100 80GB HBM3"},
		Memory:       map[int]core.NVMLMemInfo{0: {TotalBytes: 80 * 1024 * 1024 * 1024}},
		PerCallUtilization: map[int][]map[int]core.NVMLUtilSample{
			0: {
				{selfPID: {PID: selfPID, SMUtil: 0, MemUtil: 0, TimeStamp: 0}},
				{selfPID: {PID: selfPID, SMUtil: 80, MemUtil: 30, TimeStamp: 1_000_000}},
			},
		},
	}
	core.SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(core.ResetNVMLForTests)

	core.ResetGpuAccountantRegistryForTests()
	t.Cleanup(core.ResetGpuAccountantRegistryForTests)

	buf := &gpuStubBuffer{}
	SetGPUEventBuffer(buf)
	t.Cleanup(func() { SetGPUEventBuffer(nil) })

	task := &core.Task{TaskID: uuid.New()}
	ctx := core.WithTask(context.Background(), task)
	return buf, ctx, task
}

func TestWrapModalHandlerEmitsCostAndSignalEvents(t *testing.T) {
	buf, ctx, _ := setupGPUWrapTest(t)
	wrapped := WrapModalGPUHandler(func(ctx context.Context, v int) (int, error) {
		return v + 1, nil
	})
	out, err := wrapped(ctx, 41)
	if err != nil {
		t.Fatalf("handler error: %v", err)
	}
	if out != 42 {
		t.Errorf("wrapped result wrong: %d", out)
	}
	// expect 1 gpu_cost + ≥1 gpu_utilization_signal events
	var costCount, sigCount int
	for _, e := range buf.events {
		if e.EventType == core.EventTypeGPUCost {
			costCount++
		}
		if e.EventType == core.EventTypeGPUUtilizationSignal {
			sigCount++
		}
	}
	if costCount != 1 {
		t.Errorf("expected 1 gpu_cost event; got %d", costCount)
	}
	if sigCount < 1 {
		t.Errorf("expected ≥1 gpu_utilization_signal events; got %d", sigCount)
	}
}

func TestWrapModalHandlerNoTaskIsTransparent(t *testing.T) {
	core.ResetNVMLForTests()
	t.Cleanup(core.ResetNVMLForTests)
	buf := &gpuStubBuffer{}
	SetGPUEventBuffer(buf)
	t.Cleanup(func() { SetGPUEventBuffer(nil) })

	wrapped := WrapModalGPUHandler(func(ctx context.Context, v int) (int, error) {
		return v * 2, nil
	})
	out, err := wrapped(context.Background(), 7)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if out != 14 {
		t.Errorf("result: %d", out)
	}
	if len(buf.events) != 0 {
		t.Errorf("expected zero events without active task; got %d", len(buf.events))
	}
}

func TestWrapRunpodHandlerWiresAccountant(t *testing.T) {
	buf, ctx, task := setupGPUWrapTest(t)
	wrapped := WrapRunpodGPUHandler(func(ctx context.Context, v int) (int, error) {
		return v, nil
	})
	if _, err := wrapped(ctx, 0); err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(buf.events) == 0 {
		t.Errorf("expected events to be emitted on RunPod wrap")
	}
	// Ensure billing_model is per_gpu_second_active.
	foundCost := false
	for _, e := range buf.events {
		if e.EventType == core.EventTypeGPUCost {
			foundCost = true
			if bm, _ := e.Details["billing_model"].(string); bm != "per_gpu_second_active" {
				t.Errorf("expected per_gpu_second_active; got %q", bm)
			}
		}
	}
	if !foundCost {
		t.Errorf("no gpu_cost event found for task %s", task.TaskID)
	}
}

func TestWrapReplicateHandlerWiresAccountant(t *testing.T) {
	buf, ctx, _ := setupGPUWrapTest(t)
	wrapped := WrapReplicateGPUHandler(func(ctx context.Context, v int) (int, error) {
		return v, nil
	})
	if _, err := wrapped(ctx, 0); err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(buf.events) == 0 {
		t.Errorf("expected events on Replicate wrap")
	}
}

// Handler exceptions are re-raised AFTER events are persisted.
func TestWrapModalHandlerPersistsEventsOnError(t *testing.T) {
	buf, ctx, _ := setupGPUWrapTest(t)
	wrapped := WrapModalGPUHandler(func(ctx context.Context, v int) (int, error) {
		return 0, context.DeadlineExceeded
	})
	if _, err := wrapped(ctx, 0); err == nil {
		t.Fatalf("expected error to propagate; got nil")
	}
	if len(buf.events) == 0 {
		t.Errorf("error path should still persist GPU events (Modal bills failed invocations)")
	}
}
