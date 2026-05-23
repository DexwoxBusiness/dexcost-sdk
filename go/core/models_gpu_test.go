package core

import (
	"strings"
	"testing"

	"github.com/shopspring/decimal"
)

// Task 0 — EventType GPU values + Task.GpuCostUSD field.
// Mirrors python commit 2785158.

func TestEventTypeGPUValuesMatchPythonByteForByte(t *testing.T) {
	if string(EventTypeGPUCost) != "gpu_cost" {
		t.Fatalf("EventTypeGPUCost = %q; want %q (cross-SDK byte parity)",
			EventTypeGPUCost, "gpu_cost")
	}
	if string(EventTypeGPUUtilizationSignal) != "gpu_utilization_signal" {
		t.Fatalf("EventTypeGPUUtilizationSignal = %q; want %q (cross-SDK byte parity)",
			EventTypeGPUUtilizationSignal, "gpu_utilization_signal")
	}
}

func TestNewTaskHasZeroGpuCostUSD(t *testing.T) {
	tk := NewTask("t")
	if !tk.GpuCostUSD.Equal(decimal.Zero) {
		t.Fatalf("NewTask GpuCostUSD = %s; want 0", tk.GpuCostUSD.String())
	}
}

func TestTaskToDictIncludesGpuCostUSD(t *testing.T) {
	tk := NewTask("t")
	tk.GpuCostUSD = decimal.RequireFromString("1.2345")
	d := tk.ToDict()
	v, ok := d["gpu_cost_usd"]
	if !ok {
		t.Fatalf("ToDict missing gpu_cost_usd key")
	}
	s, ok := v.(string)
	if !ok {
		t.Fatalf("gpu_cost_usd not serialized as string; got %T", v)
	}
	if !strings.HasPrefix(s, "1.2345") {
		t.Fatalf("gpu_cost_usd = %q; want 1.2345", s)
	}
}

func TestTaskFromDictRoundtripsGpuCostUSD(t *testing.T) {
	tk := NewTask("t")
	tk.GpuCostUSD = decimal.RequireFromString("7.89")
	roundtripped, err := TaskFromDict(tk.ToDict())
	if err != nil {
		t.Fatalf("TaskFromDict: %v", err)
	}
	if !roundtripped.GpuCostUSD.Equal(decimal.RequireFromString("7.89")) {
		t.Fatalf("roundtrip GpuCostUSD = %s; want 7.89", roundtripped.GpuCostUSD)
	}
}
