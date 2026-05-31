package adapters_test

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/adapters"
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/shopspring/decimal"
)

// stubTransport returns a fixed response for every request — used to drive
// the tracking RoundTripper against any URL the test wants to assert against
// (notably catalog domains like api.tavily.com that we can't physically reach
// from a unit test).
type stubTransport struct {
	statusCode  int
	body        string
	contentType string
}

func (s *stubTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	body := io.NopCloser(bytes.NewReader([]byte(s.body)))
	resp := &http.Response{
		StatusCode:    s.statusCode,
		Status:        http.StatusText(s.statusCode),
		Body:          body,
		ContentLength: int64(len(s.body)),
		Header:        http.Header{},
		Request:       req,
		Proto:         "HTTP/1.1",
		ProtoMajor:    1,
		ProtoMinor:    1,
	}
	if s.contentType != "" {
		resp.Header.Set("Content-Type", s.contentType)
	}
	return resp, nil
}

// TestTrackHTTP_RecordsCatalogEntryFromBody — Tavily Search uses
// response_body extraction (`usage.credits` * 0.008/credit). With a stub
// returning {"usage":{"credits":5}}, the recorded event should carry
// cost=5*0.008=0.04 with confidence=computed and pricing_source=service_catalog.
func TestTrackHTTP_RecordsCatalogEntryFromBody(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	stub := &stubTransport{
		statusCode:  200,
		body:        `{"usage":{"credits":5},"results":[]}`,
		contentType: "application/json",
	}
	client := adapters.TrackHTTP(&http.Client{Transport: stub})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, err := http.NewRequestWithContext(ctx, "POST", "https://api.tavily.com/search", nil)
	if err != nil {
		t.Fatalf("create request: %v", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()

	// Consumer must still be able to read the body normally.
	buf, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(buf), `"credits":5`) {
		t.Errorf("body was consumed by tracker — got %q", buf)
	}

	events := adapters.GetRecordedEvents()
	if len(events) != 1 {
		t.Fatalf("expected 1 recorded event, got %d", len(events))
	}
	ev := events[0]
	if ev.EventType != core.EventTypeExternalCost {
		t.Errorf("event type: expected external_cost, got %s", ev.EventType)
	}
	expected := decimal.RequireFromString("0.040")
	if !ev.CostUSD.Equal(expected) {
		t.Errorf("cost: expected %s, got %s", expected, ev.CostUSD)
	}
	if ev.CostConfidence != core.CostConfidenceComputed {
		t.Errorf("confidence: expected computed, got %s", ev.CostConfidence)
	}
	if string(ev.PricingSource) != "service_catalog" {
		t.Errorf("pricing_source: expected service_catalog, got %s", ev.PricingSource)
	}
}

// TestTrackHTTP_RecordsCatalogEntryFixed — Exa Search uses a `fixed` extraction.
// The recorded event should carry the per-request rate verbatim.
func TestTrackHTTP_RecordsCatalogEntryFixed(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	stub := &stubTransport{statusCode: 200, body: "", contentType: ""}
	client := adapters.TrackHTTP(&http.Client{Transport: stub})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, err := http.NewRequestWithContext(ctx, "POST", "https://api.exa.ai/search", nil)
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
	expected := decimal.RequireFromString("0.007")
	if !events[0].CostUSD.Equal(expected) {
		t.Errorf("cost: expected %s, got %s", expected, events[0].CostUSD)
	}
	if events[0].CostConfidence != core.CostConfidenceExact {
		t.Errorf("confidence: expected exact, got %s", events[0].CostConfidence)
	}
}

// TestTrackHTTP_DomainRatePrecedence — user rate must override catalog match.
func TestTrackHTTP_DomainRatePrecedence(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	// User registers their own rate for a known catalog domain.
	adapters.RegisterDomainRate("api.tavily.com", decimal.RequireFromString("0.99"), "request")

	stub := &stubTransport{
		statusCode:  200,
		body:        `{"usage":{"credits":50}}`,
		contentType: "application/json",
	}
	client := adapters.TrackHTTP(&http.Client{Transport: stub})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, _ := http.NewRequestWithContext(ctx, "POST", "https://api.tavily.com/search", nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	events := adapters.GetRecordedEvents()
	if len(events) != 1 {
		t.Fatalf("expected 1 recorded event, got %d", len(events))
	}
	if !events[0].CostUSD.Equal(decimal.RequireFromString("0.99")) {
		t.Errorf("user rate should win: expected 0.99, got %s", events[0].CostUSD)
	}
	if events[0].PricingSource != core.PricingSourceRateRegistry {
		t.Errorf("pricing_source: expected rate_registry, got %s", events[0].PricingSource)
	}
}

// TestTrackHTTP_NonJSONResponseSkipsBodyParse — content-type other than JSON
// should not invoke body parsing. For Exa (fixed extraction) the cost is still
// recorded; the test asserts the body is left untouched.
func TestTrackHTTP_NonJSONResponseSkipsBodyParse(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	stub := &stubTransport{
		statusCode:  200,
		body:        "<html>not json</html>",
		contentType: "text/html",
	}
	client := adapters.TrackHTTP(&http.Client{Transport: stub})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, _ := http.NewRequestWithContext(ctx, "GET", "https://api.exa.ai/search", nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if string(body) != "<html>not json</html>" {
		t.Errorf("body was modified: got %q", body)
	}
	if len(adapters.GetRecordedEvents()) != 1 {
		t.Errorf("expected fixed-cost event recorded for exa.ai")
	}
}

// TestTrackHTTP_NoCatalogMatchSilent — unknown domain with no user rate should
// not produce any event.
func TestTrackHTTP_NoCatalogMatchSilent(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := adapters.TrackHTTP(&http.Client{})
	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, _ := http.NewRequestWithContext(ctx, "GET", server.URL, nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	if len(adapters.GetRecordedEvents()) != 0 {
		t.Errorf("expected no events for unknown domain, got %d", len(adapters.GetRecordedEvents()))
	}
}

// TestTrackHTTP_BodyReplaced — verify the response body is fully readable
// downstream after the tracker has parsed it. Regression guard for the
// io.NopCloser wrapping in readAndReplaceBody.
func TestTrackHTTP_BodyReplaced(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()

	bodyText := `{"usage":{"credits":3},"answer":"hello"}`
	stub := &stubTransport{
		statusCode:  200,
		body:        bodyText,
		contentType: "application/json",
	}
	client := adapters.TrackHTTP(&http.Client{Transport: stub})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, _ := http.NewRequestWithContext(ctx, "POST", "https://api.tavily.com/search", nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()

	read, _ := io.ReadAll(resp.Body)
	if string(read) != bodyText {
		t.Errorf("body roundtrip mismatch: got %q want %q", read, bodyText)
	}
}

// TestTrackHTTP_LazyCatalogLoad — clearing the in-memory catalog should
// trigger a lazy reload on next call rather than panic.
func TestTrackHTTP_LazyCatalogLoad(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	adapters.SetServiceCatalog(nil)

	stub := &stubTransport{statusCode: 200, body: "", contentType: ""}
	client := adapters.TrackHTTP(&http.Client{Transport: stub})

	task := core.NewTask("test")
	ctx := core.WithTask(context.Background(), &task)
	req, _ := http.NewRequestWithContext(ctx, "POST", "https://api.exa.ai/search", nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	if len(adapters.GetRecordedEvents()) != 1 {
		t.Errorf("expected 1 recorded event after lazy catalog load, got %d", len(adapters.GetRecordedEvents()))
	}
}
