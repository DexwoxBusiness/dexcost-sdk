package adapters_test

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/adapters"
	"github.com/DexwoxBusiness/dexcost-sdk/go/attribution"
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/transport"
	"github.com/shopspring/decimal"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return fn(req) }

func TestTrackHTTP_ObservesEmbeddingUsageWithoutSyntheticCost(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}, "X-Request-Id": {"req-17"}},
			Body:       io.NopCloser(strings.NewReader(`{"model":"text-embedding-3-small","usage":{"prompt_tokens":17,"total_tokens":17}}`)),
			Request:    req,
		}, nil
	})
	client := adapters.TrackHTTP(&http.Client{Transport: base})
	task := core.NewTask("embedding")
	req, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task),
		http.MethodPost,
		"https://api.openai.com/v1/embeddings",
		nil,
	)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	events := adapters.GetRecordedEvents()
	if len(events) != 1 {
		t.Fatalf("expected one usage event, got %d", len(events))
	}
	if !events[0].CostUSD.IsZero() || events[0].CostConfidence != core.CostConfidenceUnknown {
		t.Fatalf("observer asserted money: %+v", events[0])
	}
	wire := attribution.ToEventV2(events[0])
	if wire == nil || len(wire.Usage) != 1 || wire.Usage[0].Metric != attribution.MetricInputTokens || wire.Usage[0].Quantity != "17" {
		t.Fatalf("unexpected attribution event: %+v", wire)
	}
	if wire.CostEvidence != nil || wire.Provider.Name != "openai" || wire.Provider.Service != "embeddings" || wire.Provider.RecordID != "req-17" {
		t.Fatalf("unexpected provider/evidence: %+v", wire)
	}
}

func TestTrackHTTP_DoesNotObserveFailedProviderResponse(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusInternalServerError,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"usage":{"total_tokens":17}}`)),
			Request:    req,
		}, nil
	})
	client := adapters.TrackHTTP(&http.Client{Transport: base})
	task := core.NewTask("embedding")
	req, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task),
		http.MethodPost,
		"https://api.openai.com/v1/embeddings",
		nil,
	)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	for _, event := range adapters.GetRecordedEvents() {
		if event.Details["attribution_observer_service"] == "openai_embeddings" {
			t.Fatalf("failed response produced a usage observation: %+v", event)
		}
	}
}

// Test 1: RegisterDomainRate and GetDomainRates
func TestRegisterAndGetDomainRates(t *testing.T) {
	adapters.ClearDomainRates()

	adapters.RegisterDomainRate("api.example.com", decimal.NewFromFloat(0.005), "request")

	rates := adapters.GetDomainRates()
	if len(rates) != 1 {
		t.Fatalf("expected 1 domain rate, got %d", len(rates))
	}

	rate, ok := rates["api.example.com"]
	if !ok {
		t.Fatal("expected rate for api.example.com")
	}
	if !rate.CostUSD.Equal(decimal.NewFromFloat(0.005)) {
		t.Errorf("expected cost 0.005, got %s", rate.CostUSD)
	}
	if rate.Per != "request" {
		t.Errorf("expected per=request, got %s", rate.Per)
	}
}

// Test 2: ClearDomainRates removes all registrations
func TestClearDomainRates(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.RegisterDomainRate("api.example.com", decimal.NewFromFloat(0.005), "request")
	adapters.RegisterDomainRate("other.example.com", decimal.NewFromFloat(0.01), "call")

	adapters.ClearDomainRates()

	rates := adapters.GetDomainRates()
	if len(rates) != 0 {
		t.Errorf("expected 0 domain rates after clear, got %d", len(rates))
	}
}

// Test 3: Records event when request hits registered domain with active task context
func TestTrackHTTP_RecordsEventWithActiveTask(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	// Extract hostname from server URL (e.g. "127.0.0.1:PORT")
	serverHost := server.Listener.Addr().String()

	adapters.RegisterDomainRate(serverHost, decimal.NewFromFloat(0.002), "request")

	client := adapters.TrackHTTP(&http.Client{})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, err := http.NewRequestWithContext(ctx, "GET", server.URL+"/test", nil)
	if err != nil {
		t.Fatalf("create request: %v", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	events := adapters.GetRecordedEvents()
	if len(events) != 1 {
		t.Fatalf("expected 1 recorded event, got %d", len(events))
	}

	ev := events[0]
	if ev.EventType != core.EventTypeExternalCost {
		t.Errorf("expected event type external_cost, got %s", ev.EventType)
	}
	if ev.ServiceName != serverHost {
		t.Errorf("expected service_name %s, got %s", serverHost, ev.ServiceName)
	}
	if !ev.CostUSD.Equal(decimal.NewFromFloat(0.002)) {
		t.Errorf("expected cost 0.002, got %s", ev.CostUSD)
	}
	if ev.CostConfidence != core.CostConfidenceComputed {
		t.Errorf("expected cost_confidence computed, got %s", ev.CostConfidence)
	}
	if ev.PricingSource != core.PricingSourceManual {
		t.Errorf("expected pricing_source manual, got %s", ev.PricingSource)
	}
	if ev.Details["attribution_usage_per"] != "request" {
		t.Errorf("expected canonical attribution usage per=request, got %v", ev.Details["attribution_usage_per"])
	}
}

// Test 3b: HTTP-captured events are persisted to the durable storage buffer
// (not just the in-memory recording list) when one is registered. This is what
// lets the sync pusher ship HTTP costs to the Control Layer.
func TestTrackHTTP_PersistsEventToBuffer(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	dbPath := filepath.Join(t.TempDir(), "http_persist.db")
	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("NewSQLiteBuffer: %v", err)
	}
	defer buf.Close()
	adapters.SetEventBuffer(buf)
	defer adapters.SetEventBuffer(nil)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	serverHost := server.Listener.Addr().String()
	adapters.RegisterDomainRate(serverHost, decimal.NewFromFloat(0.002), "request")

	// Persist the parent task so its child cost event has a valid parent row.
	task := core.NewTask("http_persist_test")
	if err := buf.InsertTask(task); err != nil {
		t.Fatalf("InsertTask: %v", err)
	}
	ctx := core.WithTask(context.Background(), &task)

	client := adapters.TrackHTTP(&http.Client{})
	req, _ := http.NewRequestWithContext(ctx, "GET", server.URL+"/x", nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	stored, err := buf.QueryEvents(task.TaskID.String())
	if err != nil {
		t.Fatalf("QueryEvents: %v", err)
	}
	if len(stored) != 1 {
		t.Fatalf("expected 1 event persisted to storage, got %d", len(stored))
	}
	if stored[0].EventType != core.EventTypeExternalCost {
		t.Errorf("expected external_cost, got %s", stored[0].EventType)
	}
	if !stored[0].CostUSD.Equal(decimal.NewFromFloat(0.002)) {
		t.Errorf("expected persisted cost 0.002, got %s", stored[0].CostUSD)
	}
}

// Test 4a: Records event via auto-task when no explicit task but ContextData is set
func TestRecordHTTPCost_CreatesAutoTaskWithContext(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	serverHost := server.Listener.Addr().String()
	adapters.RegisterDomainRate(serverHost, decimal.NewFromFloat(0.01), "request")

	client := adapters.TrackHTTP(&http.Client{})

	// Set context with customer_id but NO explicit task
	ctx := context.Background()
	ctx = core.SetContext(ctx, &core.ContextData{CustomerID: "http-auto"})

	req, err := http.NewRequestWithContext(ctx, "GET", server.URL+"/test", nil)
	if err != nil {
		t.Fatalf("create request: %v", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	events := adapters.GetRecordedEvents()
	if len(events) == 0 {
		t.Error("expected auto-task to create event")
	}
	if len(events) > 0 && events[0].CostUSD.String() != "0.01" {
		t.Errorf("expected cost 0.01, got %s", events[0].CostUSD)
	}
}

// Test 4b: Does NOT record when no active task in context AND no ContextData
func TestTrackHTTP_NoRecordWithoutTaskAndWithoutContext(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	serverHost := server.Listener.Addr().String()
	adapters.RegisterDomainRate(serverHost, decimal.NewFromFloat(0.002), "request")

	client := adapters.TrackHTTP(&http.Client{})

	// No task in context — plain background context
	req, err := http.NewRequestWithContext(context.Background(), "GET", server.URL+"/test", nil)
	if err != nil {
		t.Fatalf("create request: %v", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	events := adapters.GetRecordedEvents()
	if len(events) != 0 {
		t.Errorf("expected 0 events when no task in context, got %d", len(events))
	}
}

// Test 5: Does NOT record for unregistered domain
func TestTrackHTTP_NoRecordForUnregisteredDomain(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	// Register a DIFFERENT domain, not the test server's
	adapters.RegisterDomainRate("other.example.com", decimal.NewFromFloat(0.005), "request")

	client := adapters.TrackHTTP(&http.Client{})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, err := http.NewRequestWithContext(ctx, "GET", server.URL+"/test", nil)
	if err != nil {
		t.Fatalf("create request: %v", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	events := adapters.GetRecordedEvents()
	if len(events) != 0 {
		t.Errorf("expected 0 events for unregistered domain, got %d", len(events))
	}
}

// Test 6: GetRecordedEvents returns events and ClearRecordedEvents clears them
func TestGetAndClearRecordedEvents(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	serverHost := server.Listener.Addr().String()
	adapters.RegisterDomainRate(serverHost, decimal.NewFromFloat(0.003), "call")

	client := adapters.TrackHTTP(&http.Client{})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)

	// Make two requests to accumulate two events
	for i := 0; i < 2; i++ {
		req, _ := http.NewRequestWithContext(ctx, "GET", server.URL+"/test", nil)
		resp, err := client.Do(req)
		if err != nil {
			t.Fatalf("do request %d: %v", i, err)
		}
		resp.Body.Close()
	}

	events := adapters.GetRecordedEvents()
	if len(events) != 2 {
		t.Fatalf("expected 2 recorded events, got %d", len(events))
	}

	adapters.ClearRecordedEvents()

	events = adapters.GetRecordedEvents()
	if len(events) != 0 {
		t.Errorf("expected 0 events after clear, got %d", len(events))
	}
}
