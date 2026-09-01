package adapters_test

import (
	"context"
	"fmt"
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
	repeated, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task),
		http.MethodPost,
		"https://api.openai.com/v1/embeddings",
		nil,
	)
	repeatedResp, err := client.Do(repeated)
	if err != nil {
		t.Fatal(err)
	}
	repeatedResp.Body.Close()
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

func TestTrackHTTP_BraveObserverSupersedesLegacyDomainCatalog(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"type":"search","web":{"results":[]}}`)),
			Request:    req,
		}, nil
	})
	client := adapters.TrackHTTP(&http.Client{Transport: base})
	task := core.NewTask("search")
	req, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task),
		http.MethodGet,
		"https://api.search.brave.com/res/v1/web/search?q=dexcost",
		nil,
	)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	events := adapters.GetRecordedEvents()
	if len(events) != 1 {
		t.Fatalf("expected one Brave observer event, got %d", len(events))
	}
	event := events[0]
	if !event.CostUSD.IsZero() || event.CostConfidence != core.CostConfidenceUnknown ||
		event.Details["attribution_observer_service"] != "brave_search" {
		t.Fatalf("legacy catalog intercepted Brave observer route: %+v", event)
	}
	wire := attribution.ToEventV2(event)
	if wire == nil || wire.Provider.Name != "brave" || wire.Provider.Service != "web_search" ||
		len(wire.Usage) != 1 || wire.Usage[0].Metric != attribution.MetricRequestCount ||
		wire.Usage[0].Quantity != "1" || wire.Resource == nil ||
		wire.Resource.Type != "sku" || wire.Resource.ID != "search" || wire.CostEvidence != nil {
		t.Fatalf("unexpected Brave attribution event: %+v", wire)
	}
}

func TestTrackHTTP_FailedBraveRequestDoesNotUseLegacyPrice(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusServiceUnavailable,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"error":"unavailable"}`)),
			Request:    req,
		}, nil
	})
	client := adapters.TrackHTTP(&http.Client{Transport: base})
	task := core.NewTask("search")
	req, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task),
		http.MethodGet,
		"https://api.search.brave.com/res/v1/web/search?q=dexcost",
		nil,
	)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	events := adapters.GetRecordedEvents()
	if len(events) != 1 || events[0].EventType != core.EventTypeNetwork {
		t.Fatalf("failed Brave request used a billable fallback: %+v", events)
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

func TestTrackHTTP_ObserverEndpointsBypassUnrelatedRerankCatalogEntries(t *testing.T) {
	testCases := []struct {
		name, rawURL, body, requestBody, observerService, provider string
	}{
		{
			name: "cohere", rawURL: "https://api.cohere.com/v2/embed",
			body:            `{"id":"cohere-1","meta":{"billed_units":{"input_tokens":29}}}`,
			requestBody:     `{"model":"embed-v4.0","texts":["hello"]}`,
			observerService: "cohere_embed", provider: "cohere",
		},
		{
			name: "jina", rawURL: "https://api.jina.ai/v1/embeddings",
			body:            `{"model":"jina-embeddings-v3","usage":{"total_tokens":53}}`,
			observerService: "jina_embeddings", provider: "jina",
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			adapters.ClearDomainRates()
			adapters.ClearRecordedEvents()
			base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": {"application/json"}},
					Body:       io.NopCloser(strings.NewReader(testCase.body)),
					Request:    req,
				}, nil
			})
			client := adapters.TrackHTTP(&http.Client{Transport: base})
			task := core.NewTask("embedding")
			req, _ := http.NewRequestWithContext(
				core.WithTask(context.Background(), &task),
				http.MethodPost,
				testCase.rawURL,
				strings.NewReader(testCase.requestBody),
			)
			resp, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			resp.Body.Close()
			events := adapters.GetRecordedEvents()
			if len(events) != 1 {
				t.Fatalf("expected one observer event, got %d", len(events))
			}
			event := events[0]
			if !event.CostUSD.IsZero() || event.Provider != testCase.provider ||
				event.Details["attribution_observer_service"] != testCase.observerService {
				t.Fatalf("observer endpoint was misclassified: %+v", event)
			}
			if wire := attribution.ToEventV2(event); wire == nil || wire.CostEvidence != nil {
				t.Fatalf("unexpected attribution evidence: %+v", wire)
			} else if testCase.name == "cohere" && (wire.Resource == nil || wire.Resource.Type != "model" || wire.Resource.ID != "embed-v4.0") {
				t.Fatalf("cohere request model was not preserved: %+v", wire.Resource)
			}
		})
	}
}

func TestTrackHTTP_MissingObserverUsageStillAttributesNotableNetwork(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	adapters.SetNetworkEventThreshold(0)
	defer adapters.SetNetworkEventThreshold(102_400)
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"model":"text-embedding-3-small"}`)),
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
	events := adapters.GetRecordedEvents()
	if len(events) != 1 || events[0].EventType != core.EventTypeNetwork {
		t.Fatalf("missing provider usage should preserve network attribution: %+v", events)
	}
	if events[0].Details["cost_pending"] != true {
		t.Fatalf("network attribution must remain pending: %+v", events[0])
	}
}

func TestTrackHTTP_DeepgramEmitsSeparateBillableAddonLines(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(
				`{"metadata":{"request_id":"dg-addon","duration":10,"channels":2}}`,
			)),
			Request: req,
		}, nil
	})
	client := adapters.TrackHTTP(&http.Client{Transport: base})
	task := core.NewTask("transcription")
	rawURL := "https://api.deepgram.com/v1/listen?model=nova-3&language=multi" +
		"&multichannel=true&diarize_model=v2&redact=pci&keyterm=Acme&detect_entities=true"
	req, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task), http.MethodPost, rawURL, nil,
	)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	events := adapters.GetRecordedEvents()
	if len(events) != 5 {
		t.Fatalf("expected base plus four add-on events, got %d", len(events))
	}
	wantResources := []string{
		"nova-3:multilingual", "speaker_diarization", "redaction", "keyterm_prompting",
		"entity_detection",
	}
	for i, event := range events {
		wire := attribution.ToEventV2(event)
		if wire == nil || wire.Resource == nil || wire.Resource.ID != wantResources[i] ||
			len(wire.Usage) != 1 || wire.Usage[0].Quantity != "20" || wire.CostEvidence != nil {
			t.Fatalf("unexpected Deepgram line %d: %+v", i, wire)
		}
	}
}

func TestTrackHTTP_OpenAITTSObservesCharactersWithoutConsumingAudio(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header: http.Header{
				"Content-Type": {"audio/mpeg"},
				"X-Request-Id": {"req-tts-4"},
			},
			Body:    io.NopCloser(strings.NewReader("audio")),
			Request: req,
		}, nil
	})
	client := adapters.TrackHTTP(&http.Client{Transport: base})
	task := core.NewTask("speech")
	req, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task),
		http.MethodPost,
		"https://api.openai.com/v1/audio/speech",
		strings.NewReader(`{"model":"tts-1-hd","input":"Hi 🌍"}`),
	)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if string(body) != "audio" {
		t.Fatalf("provider audio body was changed: %q", body)
	}
	events := adapters.GetRecordedEvents()
	if len(events) != 1 {
		t.Fatalf("expected one TTS usage event, got %d", len(events))
	}
	wire := attribution.ToEventV2(events[0])
	if wire == nil || wire.Component != attribution.ComponentTextToSpeech ||
		wire.Provider.Name != "openai" || wire.Provider.Service != "text_to_speech" ||
		wire.Provider.RecordID != "req-tts-4" || wire.Resource == nil ||
		wire.Resource.ID != "tts-1-hd" || len(wire.Usage) != 1 ||
		wire.Usage[0].Metric != attribution.MetricCharacters ||
		wire.Usage[0].Quantity != "4" || wire.CostEvidence != nil {
		t.Fatalf("unexpected TTS attribution: %+v", wire)
	}
}

func TestTrackHTTP_ElevenLabsTTSUsesProviderBilledCharactersWithoutConsumingAudio(t *testing.T) {
	adapters.ClearDomainRates()
	adapters.ClearRecordedEvents()
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header: http.Header{
				"Content-Type":   {"audio/mpeg"},
				"Character-Cost": {"11"},
				"X-Trace-Id":     {"el-trace-11"},
			},
			Body:    io.NopCloser(strings.NewReader("audio")),
			Request: req,
		}, nil
	})
	client := adapters.TrackHTTP(&http.Client{Transport: base})
	task := core.NewTask("speech")
	req, _ := http.NewRequestWithContext(
		core.WithTask(context.Background(), &task),
		http.MethodPost,
		"https://api.elevenlabs.io/v1/text-to-speech/voice_123/stream",
		strings.NewReader(`{"model_id":"eleven_flash_v2_5","text":"Not eleven chars"}`),
	)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if string(body) != "audio" {
		t.Fatalf("provider audio body was changed: %q", body)
	}
	events := adapters.GetRecordedEvents()
	if len(events) != 1 {
		t.Fatalf("expected one ElevenLabs usage event, got %d", len(events))
	}
	if strings.Contains(strings.TrimSpace(fmt.Sprint(events[0].Details)), "Not eleven chars") {
		t.Fatal("raw narration text leaked into event details")
	}
	wire := attribution.ToEventV2(events[0])
	if wire == nil || wire.Component != attribution.ComponentTextToSpeech ||
		wire.Provider.Name != "elevenlabs" || wire.Provider.Service != "text_to_speech" ||
		wire.Provider.RecordID != "el-trace-11" || wire.Resource == nil ||
		wire.Resource.ID != "eleven_flash_v2_5" || len(wire.Usage) != 1 ||
		wire.Usage[0].Metric != attribution.MetricCharacters ||
		wire.Usage[0].Quantity != "11" || wire.CostEvidence != nil {
		t.Fatalf("unexpected ElevenLabs attribution: %+v", wire)
	}
	v3 := attribution.ToObservationV3(events[0])
	if v3 == nil || v3.SchemaVersion != "3" || v3.Component != "text_to_speech" ||
		v3.Provider.Name != "elevenlabs" || v3.Provider.Service != "text_to_speech" ||
		v3.Provider.RecordID != "el-trace-11" || v3.Resource == nil ||
		v3.Resource.ID != "eleven_flash_v2_5" || len(v3.Usage) != 1 ||
		v3.Usage[0].Metric != "characters" || v3.Usage[0].Quantity != "11" ||
		v3.Usage[0].Unit != "Characters" || len(v3.Usage[0].Dimensions) != 1 ||
		v3.Usage[0].Dimensions[0].Key != "voice_id" ||
		v3.Usage[0].Dimensions[0].Value.Type != "string" ||
		v3.Usage[0].Dimensions[0].Value.Value != "voice_123" ||
		v3.CostEvidence != nil {
		t.Fatalf("unexpected ElevenLabs attribution v3: %+v", v3)
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
