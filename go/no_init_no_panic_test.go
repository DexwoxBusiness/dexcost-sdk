// Sprint 1 Theme B / §2.2.2 1a regression — B7-1a.
//
// The SDK must not panic when customer code uses public API surface
// before (or without) calling dexcost.Init(). The plan ships this as
// the no-op tracker pattern: `mustTracker()` returns nil + log
// warning; downstream methods on *core.Tracker handle nil receivers
// as no-ops; wrap helpers return a wrapper with nil tracker that
// proxies through to the underlying client without recording.

package dexcost

import (
	"context"
	"testing"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

func resetGlobalTracker(t *testing.T) {
	t.Helper()
	// Defensive: clear any leftover from a previous Init() in this test
	// process so we're exercising the "Init never called" path.
	globalTracker = nil
	globalConfig = nil
}

func TestStartTask_DoesNotPanicBeforeInit(t *testing.T) {
	resetGlobalTracker(t)
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("StartTask panicked before Init: %v", r)
		}
	}()
	ctx, tt := StartTask(context.Background(), "test-task")
	_ = ctx
	_ = tt // may be nil; should not crash on nil dereference downstream
}

func TestEndTask_DoesNotPanicBeforeInit(t *testing.T) {
	resetGlobalTracker(t)
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("EndTask panicked before Init: %v", r)
		}
	}()
	// EndTask with empty context should be a silent no-op, not a panic.
	_ = EndTask(context.Background(), core.TaskStatusSuccess)
}

func TestRecordCost_DoesNotPanicBeforeInit(t *testing.T) {
	resetGlobalTracker(t)
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("RecordCost panicked before Init: %v", r)
		}
	}()
	_ = RecordCost(context.Background(), "test-service", "test-op", decimal.NewFromFloat(0.01))
}

func TestTracker_ReturnsNilBeforeInit(t *testing.T) {
	resetGlobalTracker(t)
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("Tracker() panicked before Init: %v", r)
		}
	}()
	tr := Tracker()
	if tr != nil {
		t.Errorf("expected nil tracker before Init, got %v", tr)
	}
}

func TestTrackedTaskMethods_DoNotPanicBeforeInit(t *testing.T) {
	resetGlobalTracker(t)
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("TrackedTask method panicked on no-op stub: %v", r)
		}
	}()
	_, tt := StartTask(context.Background(), "no-init")

	// Every public method on the no-op TrackedTask must be safe.
	if err := tt.RecordCost("svc", decimal.NewFromFloat(0.01)); err != nil {
		t.Errorf("RecordCost returned error on no-op tracker: %v", err)
	}
	if err := tt.RecordLLMCall("openai", "gpt-4o", 100, 50); err != nil {
		t.Errorf("RecordLLMCall returned error on no-op tracker: %v", err)
	}
	if err := tt.RecordUsage("svc", 1); err != nil {
		t.Errorf("RecordUsage returned error on no-op tracker: %v", err)
	}
	if err := tt.MarkRetry("test"); err != nil {
		t.Errorf("MarkRetry returned error on no-op tracker: %v", err)
	}
	tt.LinkTrace("otel", "trace-id")
	_ = tt.GetTraceLinks() // may be nil; acceptable
	if err := tt.End(core.TaskStatusSuccess); err != nil {
		t.Errorf("End returned error on no-op tracker: %v", err)
	}
}

func TestWrapOpenAI_DoesNotPanicBeforeInit(t *testing.T) {
	resetGlobalTracker(t)
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("WrapOpenAI panicked before Init: %v", r)
		}
	}()
	// Customer wraps their client during module init, before any
	// dexcost.Init() call. WrapOpenAI must return a usable wrapper
	// (which silently no-ops on the recording side) rather than panic.
	stub := struct{}{}
	w := WrapOpenAI(stub)
	_ = w
}
