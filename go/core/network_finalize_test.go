// Phase D Task 10 — task-finalize egress pricing tests.
//
// Mirrors python/tests/test_network_cost_finalize.py +
// test_network_cost_dual_invoice.py + the parametrized property
// invariants from test_network_cost_invariants.py.

package core

import (
	"context"
	"fmt"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
	"github.com/shopspring/decimal"
)

// makeTrackedTask constructs an in-memory tracker + a started task, then
// pins CloudEnv to the requested (provider, region). Returns the TT and
// the buffer so tests can inspect events.
func makeTrackedTask(t *testing.T, provider, region string) (*TrackedTask, Buffer) {
	t.Helper()
	cloud.ResetForTests()
	if provider != "" || region != "" {
		cloud.SetResultForTests(cloud.CloudEnv{
			Provider: provider, Region: region, Source: "env",
		})
	}
	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}
	_, tt := tr.StartTask(context.Background(), "test")
	return tt, buf
}

func TestFinalize_NetworkCostFromCanonicalScalar(t *testing.T) {
	tt, _ := makeTrackedTask(t, "aws", "us-east-1")
	// 1 GB external = $0.09 at aws/us-east-1.
	acct := GetAccountant(tt.Task.TaskID.String())
	if acct == nil {
		t.Fatal("StartTask must register an accountant")
	}
	acct.Record("api.example.com", 0, 1_000_000_000, boolPtr(false))

	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}

	want := decimal.RequireFromString("0.09")
	if !tt.Task.NetworkCostUSD.Equal(want) {
		t.Fatalf("NetworkCostUSD = %s, want %s", tt.Task.NetworkCostUSD, want)
	}
	if tt.Task.NetworkBytesOut != 1_000_000_000 {
		t.Fatalf("NetworkBytesOut = %d, want 1_000_000_000", tt.Task.NetworkBytesOut)
	}
	if tt.Task.NetworkCallCount != 1 {
		t.Fatalf("NetworkCallCount = %d, want 1", tt.Task.NetworkCallCount)
	}
}

func TestFinalize_PerHostEgressCostInByHost(t *testing.T) {
	tt, _ := makeTrackedTask(t, "aws", "us-east-1")
	acct := GetAccountant(tt.Task.TaskID.String())
	acct.Record("api.example.com", 0, 500_000_000, boolPtr(false))

	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}

	hosts := tt.Task.NetworkByHost["hosts"].([]map[string]interface{})
	var host map[string]interface{}
	for _, h := range hosts {
		if h["host"].(string) == "api.example.com" {
			host = h
			break
		}
	}
	if host == nil {
		t.Fatal("api.example.com host entry missing")
	}
	cost, ok := host["egress_cost_usd"].(string)
	if !ok {
		t.Fatalf("egress_cost_usd missing/wrong type: %#v", host)
	}
	got := decimal.RequireFromString(cost)
	// 0.5 GB * 0.09 = 0.045
	want := decimal.RequireFromString("0.045")
	if !got.Equal(want) {
		t.Fatalf("per-host egress_cost_usd = %s, want %s", got, want)
	}
}

func TestFinalize_InternalHostZeroEgressCost(t *testing.T) {
	tt, _ := makeTrackedTask(t, "aws", "us-east-1")
	acct := GetAccountant(tt.Task.TaskID.String())
	// 999 MB to private IP → 0 external bytes → $0 cost.
	acct.Record("10.0.0.5", 0, 999_999_999, boolPtr(true))

	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}

	if !tt.Task.NetworkCostUSD.Equal(decimal.Zero) {
		t.Fatalf("NetworkCostUSD = %s, want 0", tt.Task.NetworkCostUSD)
	}
}

func TestFinalize_BackfillsNetworkEventCost(t *testing.T) {
	tt, buf := makeTrackedTask(t, "aws", "us-east-1")

	// Pre-insert a cost_pending network event (mirrors what the HTTP
	// adapter would have emitted at body-completion time).
	ev := NewEvent(tt.Task.TaskID, EventTypeNetwork)
	ev.CostUSD = decimal.Zero
	ev.CostConfidence = CostConfidenceUnknown
	ev.ServiceName = "api.example.com"
	ev.Details["url"] = "https://api.example.com/x"
	ev.Details["request_bytes"] = int64(0)
	ev.Details["response_bytes"] = int64(1_000_000_000)
	ev.Details["is_internal_traffic"] = false
	ev.Details["cost_pending"] = true
	if err := buf.InsertEvent(ev); err != nil {
		t.Fatalf("InsertEvent: %v", err)
	}

	// Drive the accountant so the per-task scalar matches.
	acct := GetAccountant(tt.Task.TaskID.String())
	acct.Record("api.example.com", 0, 1_000_000_000, boolPtr(false))

	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}

	stored, err := buf.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	var net *Event
	for i := range stored {
		if stored[i].EventType == EventTypeNetwork {
			net = &stored[i]
			break
		}
	}
	if net == nil {
		t.Fatal("network event not stored")
	}
	want := decimal.RequireFromString("0.09")
	if !net.CostUSD.Equal(want) {
		t.Fatalf("back-filled cost_usd = %s, want %s", net.CostUSD, want)
	}
	if _, hasPending := net.Details["cost_pending"]; hasPending {
		t.Fatal("cost_pending should be stripped after back-fill")
	}
	if src, _ := net.Details["egress_pricing_source"].(string); src != "egress_catalog:aws:us-east-1" {
		t.Fatalf("egress_pricing_source = %q, want egress_catalog:aws:us-east-1", src)
	}
	if net.PricingVersion != "egress:1.0.0" {
		t.Fatalf("pricing_version = %q, want egress:1.0.0", net.PricingVersion)
	}
}

func TestFinalize_NoCloudFallsToMetaDefaultRate(t *testing.T) {
	// Tier 3 ladder — no provider detected → universal $0.09/GB.
	tt, _ := makeTrackedTask(t, "", "")
	acct := GetAccountant(tt.Task.TaskID.String())
	acct.Record("api.example.com", 0, 1_000_000_000, boolPtr(false))

	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}

	want := decimal.RequireFromString("0.09")
	if !tt.Task.NetworkCostUSD.Equal(want) {
		t.Fatalf("Tier-3 fallback cost = %s, want %s", tt.Task.NetworkCostUSD, want)
	}
}

func TestFinalize_ZeroBytesYieldsZeroCost(t *testing.T) {
	tt, _ := makeTrackedTask(t, "aws", "us-east-1")
	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}
	if !tt.Task.NetworkCostUSD.Equal(decimal.Zero) {
		t.Fatalf("NetworkCostUSD = %s, want 0", tt.Task.NetworkCostUSD)
	}
	if tt.Task.NetworkCallCount != 0 {
		t.Fatalf("NetworkCallCount = %d, want 0", tt.Task.NetworkCallCount)
	}
}

// Decision #7 dual-invoice attribution (mandatory per Decisions Log).
//
// A cataloged-vendor call must produce exactly ONE event (external_cost
// with the vendor charge) AND populate both task.ExternalCostUSD (vendor
// invoice) AND task.NetworkCostUSD (cloud egress on the SAME bytes).
// The external_cost event's own CostUSD stays unchanged at the vendor
// charge — no egress dollars stamped on it. v2 §3.3 + §10.2.
func TestFinalize_Decision7_DualInvoiceAttribution(t *testing.T) {
	tt, buf := makeTrackedTask(t, "aws", "us-east-1")

	// Pre-record the vendor invoice (HTTP adapter emits this at RoundTrip
	// return time for cataloged calls).
	ev := NewEvent(tt.Task.TaskID, EventTypeExternalCost)
	ev.CostUSD = decimal.RequireFromString("0.01") // vendor charge
	ev.CostConfidence = CostConfidenceExact
	ev.ServiceName = "api.vendor.com"
	ev.Details["url"] = "https://api.vendor.com/x"
	ev.Details["request_bytes"] = int64(0)
	ev.Details["response_bytes"] = int64(500_000_000)
	ev.Details["is_internal_traffic"] = false
	if err := buf.InsertEvent(ev); err != nil {
		t.Fatalf("InsertEvent: %v", err)
	}

	// Same bytes → accountant → external_bytes_out.
	acct := GetAccountant(tt.Task.TaskID.String())
	acct.Record("api.vendor.com", 0, 500_000_000, boolPtr(false))

	if err := tt.End(TaskStatusSuccess); err != nil {
		t.Fatalf("End: %v", err)
	}

	// (1) Exactly ONE event for this call — the vendor's external_cost.
	stored, _ := buf.QueryEvents(tt.Task.TaskID.String())
	if len(stored) != 1 {
		t.Fatalf("expected exactly one event, got %d", len(stored))
	}
	if stored[0].EventType != EventTypeExternalCost {
		t.Fatalf("event type = %q, want external_cost", stored[0].EventType)
	}

	// (2) Vendor's per-request invoice is intact.
	if !tt.Task.ExternalCostUSD.Equal(decimal.RequireFromString("0.01")) {
		t.Fatalf("ExternalCostUSD = %s, want 0.01", tt.Task.ExternalCostUSD)
	}

	// (3) Cloud's egress invoice on those same bytes is captured IN ADDITION.
	//     0.5 GB * 0.09 = 0.045
	if !tt.Task.NetworkCostUSD.Equal(decimal.RequireFromString("0.045")) {
		t.Fatalf("NetworkCostUSD = %s, want 0.045", tt.Task.NetworkCostUSD)
	}

	// (4) Total = vendor + egress, no double-count, no silent drop.
	wantTotal := decimal.RequireFromString("0.055")
	if !tt.Task.TotalCostUSD.Equal(wantTotal) {
		t.Fatalf("TotalCostUSD = %s, want %s", tt.Task.TotalCostUSD, wantTotal)
	}

	// (5) The external_cost event's own CostUSD is UNCHANGED — no egress
	//     dollars stamped onto it. Events carry measurement; task carries
	//     derived attribution (v2 §3.3).
	if !stored[0].CostUSD.Equal(decimal.RequireFromString("0.01")) {
		t.Fatalf("external_cost event cost_usd = %s, want 0.01 (unchanged)", stored[0].CostUSD)
	}
}

// Property invariant (v2 §10.3 #2): sum(per-host egress_cost_usd) ==
// task.NetworkCostUSD. Parametrized across host counts × is_internal modes.
func TestFinalize_PropertyInvariants(t *testing.T) {
	cases := []struct {
		nHosts int
		mode   string // "internal", "external", "nil"
	}{
		{1, "internal"}, {1, "external"}, {1, "nil"},
		{5, "internal"}, {5, "external"}, {5, "nil"},
		{20, "internal"}, {20, "external"}, {20, "nil"},
		{100, "internal"}, {100, "external"}, {100, "nil"},
		{1000, "internal"}, {1000, "external"}, {1000, "nil"},
	}
	for _, c := range cases {
		t.Run(fmt.Sprintf("n=%d/%s", c.nHosts, c.mode), func(t *testing.T) {
			tt, _ := makeTrackedTask(t, "aws", "us-east-1")
			acct := GetAccountant(tt.Task.TaskID.String())
			var isInternal *bool
			switch c.mode {
			case "internal":
				isInternal = boolPtr(true)
			case "external":
				isInternal = boolPtr(false)
			case "nil":
				isInternal = nil
			}
			for i := 0; i < c.nHosts; i++ {
				bytesIn := int64(i*37) % 5000
				bytesOut := int64(i*53) % 5000
				acct.Record(fmt.Sprintf("h%d.com", i), bytesIn, bytesOut, isInternal)
			}

			if err := tt.End(TaskStatusSuccess); err != nil {
				t.Fatalf("End: %v", err)
			}

			// Sum per-host egress_cost_usd from the stored network_by_host.
			hosts := tt.Task.NetworkByHost["hosts"].([]map[string]interface{})
			sum := decimal.Zero
			for _, h := range hosts {
				s, ok := h["egress_cost_usd"].(string)
				if !ok {
					continue
				}
				sum = sum.Add(decimal.RequireFromString(s))
			}
			if !sum.Equal(tt.Task.NetworkCostUSD) {
				t.Fatalf("invariant 2 failed: sum(per-host)=%s task=%s",
					sum, tt.Task.NetworkCostUSD)
			}
		})
	}
}
