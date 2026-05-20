package adapters

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// roundTripperFor returns a *trackingTransport wrapping the test server's
// transport so we can drive the adapter without the global patch.
func roundTripperFor() *trackingTransport {
	return &trackingTransport{base: http.DefaultTransport}
}

func newClientWithTracking() *http.Client {
	return &http.Client{Transport: roundTripperFor()}
}

// Drive a request inside a task context so resolveTaskID returns ok and the
// accountant lookup works.
func newTaskContext(taskID uuid.UUID) context.Context {
	task := &core.Task{TaskID: taskID, TaskType: "test"}
	return core.WithTask(context.Background(), task)
}

func TestHTTPAdapter_RecordsBytesIntoRegisteredAccountant(t *testing.T) {
	ClearDomainRates()
	ClearRecordedEvents()
	resetAccountantRegistryForTests()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("hello world"))
	}))
	defer server.Close()

	taskID := uuid.New()
	accountant := NewNetworkAccountant()
	RegisterAccountant(taskID.String(), accountant)

	req, _ := http.NewRequestWithContext(newTaskContext(taskID), "GET", server.URL+"/x", nil)
	resp, err := newClientWithTracking().Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	body, _ := io.ReadAll(resp.Body) // drain → triggers EOF finalize
	_ = resp.Body.Close()
	if !strings.Contains(string(body), "hello world") {
		t.Fatalf("body payload not delivered to caller: %q", body)
	}

	snap := accountant.Finalize()
	if snap.CallCount != 1 {
		t.Fatalf("CallCount = %d, want 1", snap.CallCount)
	}
	if snap.BytesIn <= 0 {
		t.Fatalf("BytesIn = %d, want > 0", snap.BytesIn)
	}
	if snap.BytesOut <= 0 {
		t.Fatalf("BytesOut = %d, want > 0", snap.BytesOut)
	}
}

func TestHTTPAdapter_UncatalogedAboveThresholdEmitsNetworkEvent(t *testing.T) {
	ClearDomainRates()
	ClearRecordedEvents()
	resetAccountantRegistryForTests()

	// 200 KB body — pushes combined bytes well past the 100 KiB threshold.
	bigBody := strings.Repeat("x", 200_000)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte(bigBody))
	}))
	defer server.Close()

	taskID := uuid.New()
	RegisterAccountant(taskID.String(), NewNetworkAccountant())
	req, _ := http.NewRequestWithContext(newTaskContext(taskID), "GET", server.URL+"/big", nil)
	resp, err := newClientWithTracking().Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()

	netEvents := 0
	for _, ev := range GetRecordedEvents() {
		if ev.EventType == core.EventTypeNetwork {
			netEvents++
			// v2 §6.4 cost_pending marker so finalize back-fills.
			if v, _ := ev.Details["cost_pending"].(bool); !v {
				t.Fatalf("network event missing cost_pending=true: %v", ev.Details)
			}
			// v1 §4.3 uniform byte placement.
			if _, ok := ev.Details["request_bytes"]; !ok {
				t.Fatal("network event missing request_bytes")
			}
			if _, ok := ev.Details["response_bytes"]; !ok {
				t.Fatal("network event missing response_bytes")
			}
			if _, ok := ev.Details["is_internal_traffic"]; !ok {
				t.Fatal("network event missing is_internal_traffic")
			}
		}
	}
	if netEvents != 1 {
		t.Fatalf("expected exactly 1 network event, got %d", netEvents)
	}
}

func TestHTTPAdapter_UncatalogedBelowThresholdNoNetworkEvent(t *testing.T) {
	ClearDomainRates()
	ClearRecordedEvents()
	resetAccountantRegistryForTests()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("tiny"))
	}))
	defer server.Close()

	taskID := uuid.New()
	RegisterAccountant(taskID.String(), NewNetworkAccountant())
	req, _ := http.NewRequestWithContext(newTaskContext(taskID), "GET", server.URL+"/small", nil)
	resp, _ := newClientWithTracking().Do(req)
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()

	for _, ev := range GetRecordedEvents() {
		if ev.EventType == core.EventTypeNetwork {
			t.Fatalf("below-threshold call must not emit network event; got %v", ev.Details)
		}
	}
}

func TestHTTPAdapter_UncatalogedStatus500EmitsNetworkEvent(t *testing.T) {
	ClearDomainRates()
	ClearRecordedEvents()
	resetAccountantRegistryForTests()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(503)
		_, _ = w.Write([]byte("oops"))
	}))
	defer server.Close()

	taskID := uuid.New()
	RegisterAccountant(taskID.String(), NewNetworkAccountant())
	req, _ := http.NewRequestWithContext(newTaskContext(taskID), "GET", server.URL+"/err", nil)
	resp, _ := newClientWithTracking().Do(req)
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()

	got := false
	for _, ev := range GetRecordedEvents() {
		if ev.EventType == core.EventTypeNetwork {
			got = true
			if sc, _ := ev.Details["status_code"].(int); sc != 503 {
				t.Fatalf("status_code = %v, want 503", ev.Details["status_code"])
			}
		}
	}
	if !got {
		t.Fatal("5xx error must emit network event even below byte threshold")
	}
}

func TestHTTPAdapter_SuppressionScopeNoNetworkEvent(t *testing.T) {
	ClearDomainRates()
	ClearRecordedEvents()
	resetAccountantRegistryForTests()

	bigBody := strings.Repeat("x", 200_000)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte(bigBody))
	}))
	defer server.Close()

	taskID := uuid.New()
	accountant := NewNetworkAccountant()
	RegisterAccountant(taskID.String(), accountant)

	// LLM-instrument-style: wrap the request context with the suppression flag.
	ctx := core.WithSuppressNetworkEvent(newTaskContext(taskID))
	req, _ := http.NewRequestWithContext(ctx, "GET", server.URL+"/big", nil)
	resp, _ := newClientWithTracking().Do(req)
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()

	for _, ev := range GetRecordedEvents() {
		if ev.EventType == core.EventTypeNetwork {
			t.Fatalf("suppressed scope must withhold network event; got %v", ev.Details)
		}
	}
	// Bytes still recorded into the accountant.
	snap := accountant.Finalize()
	if snap.CallCount != 1 {
		t.Fatalf("CallCount = %d, want 1 (bytes still recorded under suppression)", snap.CallCount)
	}
	if snap.BytesIn <= 0 {
		t.Fatal("BytesIn must be > 0 even when emission is suppressed")
	}
}

func TestHTTPAdapter_CatalogPathStampsByteDetails(t *testing.T) {
	ClearDomainRates()
	ClearRecordedEvents()
	resetAccountantRegistryForTests()

	// Register a domain rate so we hit Path 1 (deterministic vs catalog).
	RegisterDomainRate("api.example.com", decimal.RequireFromString("0.01"), "request")
	defer ClearDomainRates()

	// Use httptest but rewrite the host to api.example.com.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("ok"))
	}))
	defer server.Close()

	taskID := uuid.New()
	RegisterAccountant(taskID.String(), NewNetworkAccountant())
	req, _ := http.NewRequestWithContext(newTaskContext(taskID), "GET", server.URL+"/x", nil)
	req.Host = "api.example.com"
	req.URL.Host = "api.example.com" // domain-rate lookup uses URL.Host
	// Reroute to the actual httptest server by setting RequestURI? Actually
	// to make this go through the test server, we need to re-target the
	// transport, but with URL.Host overridden the test server won't receive
	// it. Instead, drive the recordDomainRate path directly through the
	// adapter helpers — simulating a call without an actual network hop.
	// The test value here is the byte_details stamp on the emitted event.
	tracker := roundTripperFor()
	byteDetails := map[string]interface{}{
		"protocol":            "https",
		"request_bytes":       int64(100),
		"is_internal_traffic": false,
	}
	tracker.recordDomainRate(req, nil, "api.example.com", DomainRate{
		CostUSD: decimal.RequireFromString("0.01"),
		Per:     "request",
	}, byteDetails)

	got := GetRecordedEvents()
	if len(got) == 0 {
		t.Fatal("expected an external_cost event")
	}
	ev := got[len(got)-1]
	if ev.EventType != core.EventTypeExternalCost {
		t.Fatalf("event type = %v, want external_cost", ev.EventType)
	}
	if v, _ := ev.Details["protocol"].(string); v != "https" {
		t.Fatalf("protocol stamp missing: %v", ev.Details)
	}
	if v, _ := ev.Details["request_bytes"].(int64); v != 100 {
		t.Fatalf("request_bytes stamp wrong: %v", ev.Details["request_bytes"])
	}
}
