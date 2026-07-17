// Package adapters provides cost-tracking adapters for common HTTP clients.
// The HTTP adapter wraps http.Client's Transport with a custom RoundTripper
// that auto-records external cost events for registered domains and for any
// service entry matched by the bundled `pricing.ServiceCatalog`.
package adapters

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
	"github.com/DexwoxBusiness/dexcost-sdk/go/security"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// DomainRate describes the cost incurred for each request to a registered domain.
type DomainRate struct {
	CostUSD decimal.Decimal
	Per     string
}

// maxResponseBodySize caps the size of response bodies we buffer for cost
// extraction. Mirrors the 1 MB cap used by the Python adapter
// (`adapters/http.py:_MAX_BODY_SIZE`).
const maxResponseBodySize = 1_000_000

// defaultNetworkEventThresholdBytes — combined request + response bytes above
// which an un-cataloged call emits a `network` event. Mirrors Python config
// `network_event_threshold_bytes = 102_400` (100 KiB). Overridable at Init via
// SetNetworkEventThreshold (wired from Config.NetworkEventThresholdBytes).
const defaultNetworkEventThresholdBytes = 102_400

// maxRecordedEvents bounds the in-memory event recording buffer. Long-lived
// processes that never call ClearRecordedEvents would otherwise leak. When
// the cap is exceeded the oldest entries are evicted FIFO so tests and
// lightweight integrations still see the latest activity.
const maxRecordedEvents = 10_000

var (
	domainRatesMu sync.RWMutex
	domainRates   = make(map[string]DomainRate)

	recordedEventsMu sync.Mutex
	recordedEvents   []core.Event

	catalogMu sync.RWMutex
	catalog   *pricing.ServiceCatalog

	eventBufferMu sync.RWMutex
	eventBuffer   core.Buffer

	networkEventMu        sync.RWMutex
	networkEventThreshold = defaultNetworkEventThresholdBytes
	// networkEventLatencyMs — emit when call latency exceeds this many
	// milliseconds. 0 disables the latency trigger (the default). Mirrors
	// Python config `network_event_latency_ms`.
	networkEventLatencyMs = 0
)

// SetNetworkEventThreshold overrides the combined-bytes threshold above which
// un-cataloged HTTP calls emit a `network` event. Called once by Init() from
// Config.NetworkEventThresholdBytes; mirrors Python's network-event wiring.
func SetNetworkEventThreshold(thresholdBytes int) {
	networkEventMu.Lock()
	defer networkEventMu.Unlock()
	networkEventThreshold = thresholdBytes
}

// SetNetworkEventLatency overrides the per-call latency (in milliseconds) above
// which un-cataloged HTTP calls emit a `network` event. 0 disables the trigger.
// Called once by Init() from Config.NetworkEventLatencyMs; mirrors Python's
// `network_event_latency_ms` wiring.
func SetNetworkEventLatency(latencyMs int) {
	networkEventMu.Lock()
	defer networkEventMu.Unlock()
	networkEventLatencyMs = latencyMs
}

// networkEventTriggers returns the active byte threshold and latency trigger
// (ms) under a single read lock.
func networkEventTriggers() (thresholdBytes, latencyMs int) {
	networkEventMu.RLock()
	defer networkEventMu.RUnlock()
	return networkEventThreshold, networkEventLatencyMs
}

// SetEventBuffer registers (or clears, with nil) the durable storage buffer the
// HTTP adapter persists external_cost events into. The top-level dexcost
// package wires this to the tracker's buffer during Init so HTTP-captured costs
// reach SQLite and the sync pusher. Without it, events are only held in the
// in-memory recording buffer (GetRecordedEvents) and never sync.
func SetEventBuffer(b core.Buffer) {
	eventBufferMu.Lock()
	defer eventBufferMu.Unlock()
	eventBuffer = b
}

func getEventBuffer() core.Buffer {
	eventBufferMu.RLock()
	defer eventBufferMu.RUnlock()
	return eventBuffer
}

// RegisterDomainRate registers a per-request cost for the given domain.
// Domain should be the host[:port] as returned by url.URL.Hostname() or
// Listener.Addr().String() for test servers. User-registered rates take
// precedence over the bundled service catalog.
func RegisterDomainRate(domain string, costUSD decimal.Decimal, per string) {
	domainRatesMu.Lock()
	defer domainRatesMu.Unlock()
	domainRates[domain] = DomainRate{CostUSD: costUSD, Per: per}
}

// GetDomainRates returns a snapshot of all registered domain rates.
func GetDomainRates() map[string]DomainRate {
	domainRatesMu.RLock()
	defer domainRatesMu.RUnlock()
	snapshot := make(map[string]DomainRate, len(domainRates))
	for k, v := range domainRates {
		snapshot[k] = v
	}
	return snapshot
}

// ClearDomainRates removes all registered domain rates.
func ClearDomainRates() {
	domainRatesMu.Lock()
	defer domainRatesMu.Unlock()
	domainRates = make(map[string]DomainRate)
}

// GetRecordedEvents returns a snapshot of all recorded cost events.
func GetRecordedEvents() []core.Event {
	recordedEventsMu.Lock()
	defer recordedEventsMu.Unlock()
	snapshot := make([]core.Event, len(recordedEvents))
	copy(snapshot, recordedEvents)
	return snapshot
}

// ClearRecordedEvents removes all recorded cost events.
func ClearRecordedEvents() {
	recordedEventsMu.Lock()
	defer recordedEventsMu.Unlock()
	recordedEvents = nil
}

// SetServiceCatalog replaces the active service catalog. Pass nil to disable
// catalog-based auto-detection (test isolation, custom transports).
func SetServiceCatalog(c *pricing.ServiceCatalog) {
	catalogMu.Lock()
	defer catalogMu.Unlock()
	catalog = c
}

// GetServiceCatalog returns the active service catalog, lazily loading the
// bundled catalog on first use. Returns nil only if loading fails.
func GetServiceCatalog() *pricing.ServiceCatalog {
	catalogMu.RLock()
	c := catalog
	catalogMu.RUnlock()
	if c != nil {
		return c
	}
	// Lazy-load bundled catalog.
	loaded, err := pricing.NewServiceCatalog()
	if err != nil {
		return nil
	}
	catalogMu.Lock()
	defer catalogMu.Unlock()
	if catalog == nil {
		catalog = loaded
	}
	return catalog
}

// trackingTransport is an http.RoundTripper that records external cost events
// for requests to registered domains and to any domain matched by the bundled
// service catalog. Requires either a task in context or ambient ContextData
// with a CustomerID for an event to be recorded.
type trackingTransport struct {
	base http.RoundTripper
}

func (t *trackingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	// ── v1 byte measurement — request side (known before RoundTrip) ─────
	host := req.URL.Hostname()
	if host == "" {
		host = req.URL.Host
	}
	protocol := req.URL.Scheme
	if protocol == "" {
		protocol = "https"
	}
	requestBytes := measureRequestBytes(req)
	isInternal := ClassifyDestination(host)
	suppress := core.IsNetworkEventSuppressed(req.Context())

	start := time.Now()
	resp, err := t.base.RoundTrip(req)
	if err != nil {
		return resp, err
	}
	// Latency of the underlying call, measured request-send → response-headers
	// (parity with Python's `latency_ms` around the wrapped call).
	latencyMs := int(time.Since(start).Milliseconds())

	// Resolve task + accountant. resolveTaskID never returns the accountant
	// directly because it's a core helper; we look it up from the registry.
	taskID, autoTask, ok := resolveTaskID(req)
	var accountant *core.NetworkAccountant
	if ok {
		accountant = core.GetAccountant(taskID.String())
	}

	// Response header bytes are always knowable (status line + headers).
	responseHeaderBytes := measureResponseHeaderBytes(resp)

	hostKey := req.URL.Host
	domainRatesMu.RLock()
	rate, hasRate := domainRates[hostKey]
	domainRatesMu.RUnlock()

	cat := GetServiceCatalog()

	// ── Path 1: user-registered domain rate ─────────────────────────────
	if hasRate {
		// Cost event known immediately; bytes are completed on body close.
		byteDetails := map[string]interface{}{
			"protocol":            protocol,
			"request_bytes":       requestBytes,
			"is_internal_traffic": isInternalToValue(isInternal),
		}
		t.recordDomainRate(req, resp, hostKey, rate, byteDetails)
		resp.Body = wrapBodyForRecording(resp.Body, &bodyRecorder{
			accountant:          accountant,
			host:                host,
			requestBytes:        requestBytes,
			responseHeaderBytes: responseHeaderBytes,
			isInternal:          isInternal,
		})
		return resp, nil
	}

	// ── Path 2: service catalog match ──────────────────────────────────
	if cat != nil {
		if entry := cat.Lookup(req.URL.String()); entry != nil {
			headers := flattenHeaders(resp.Header)
			body, replaced, bodyByteCount := readAndReplaceBodyCounted(resp)
			if replaced != nil {
				resp.Body = replaced
			}
			responseBytes := responseHeaderBytes + bodyByteCount
			if accountant != nil {
				accountant.Record(host, responseBytes, requestBytes, isInternal)
			}
			byteDetails := map[string]interface{}{
				"protocol":            protocol,
				"request_bytes":       requestBytes,
				"response_bytes":      responseBytes,
				"is_internal_traffic": isInternalToValue(isInternal),
			}
			t.recordCatalogEntry(req, cat, entry, headers, body, byteDetails)
			return resp, nil
		}
	}

	// ── Path 3: un-cataloged call ──────────────────────────────────────
	// Wrap the body in a recorder that records bytes into the accountant
	// at close AND, if not suppressed and the call is notable, emits a
	// `network` event with cost_pending=true (v2 §6.4 — back-filled at
	// task finalize).
	statusCode := resp.StatusCode
	emit := !suppress && ok
	resp.Body = wrapBodyForRecording(resp.Body, &bodyRecorder{
		accountant:          accountant,
		host:                host,
		requestBytes:        requestBytes,
		responseHeaderBytes: responseHeaderBytes,
		isInternal:          isInternal,
		emitNetworkEvent:    emit,
		taskID:              taskID,
		autoTask:            autoTask,
		protocol:            protocol,
		method:              req.Method,
		statusCode:          statusCode,
		latencyMs:           latencyMs,
		url:                 security.ScrubURL(req.URL.String()),
	})
	return resp, nil
}

// recordDomainRate records an external_cost event from a user-registered rate.
// byteDetails stamps the v1 §4.3 byte fields into details (uniform across
// every event type).
func (t *trackingTransport) recordDomainRate(
	req *http.Request,
	_ *http.Response,
	domain string,
	rate DomainRate,
	byteDetails map[string]interface{},
) {
	taskID, autoTask, ok := resolveTaskID(req)
	if !ok {
		return
	}
	event := core.NewEvent(taskID, core.EventTypeExternalCost)
	event.ServiceName = domain
	event.CostUSD = rate.CostUSD
	event.CostConfidence = core.CostConfidenceComputed
	event.PricingSource = core.PricingSourceManual
	event.Details["url"] = security.ScrubURL(req.URL.String())
	event.Details["attribution_usage_quantity"] = 1
	event.Details["attribution_usage_per"] = rate.Per
	for k, v := range byteDetails {
		event.Details[k] = v
	}

	persistEvent(event, autoTask)
}

// recordCatalogEntry records an external_cost event from a service catalog match.
// byteDetails stamps the v1 §4.3 byte fields (protocol, request_bytes,
// response_bytes, is_internal_traffic) into details.
func (t *trackingTransport) recordCatalogEntry(
	req *http.Request,
	cat *pricing.ServiceCatalog,
	entry *pricing.ServiceEntry,
	headers map[string]string,
	body map[string]interface{},
	byteDetails map[string]interface{},
) {
	taskID, autoTask, ok := resolveTaskID(req)
	if !ok {
		return
	}

	result := cat.ExtractCost(entry, headers, body)

	event := core.NewEvent(taskID, core.EventTypeExternalCost)
	event.Details["url"] = security.ScrubURL(req.URL.String())
	for k, v := range byteDetails {
		event.Details[k] = v
	}
	if result != nil {
		event.ServiceName = result.ServiceName
		event.CostUSD = result.Amount
		event.CostConfidence = core.CostConfidence(result.Confidence)
		event.PricingSource = core.PricingSource(result.PricingSource)
		event.PricingVersion = cat.CatalogVersion()
	} else {
		// Matched a catalog entry but couldn't extract cost — record at zero
		// with unknown confidence so the call still surfaces in reports.
		event.ServiceName = entry.DisplayName
		event.CostUSD = decimal.Zero
		event.CostConfidence = core.CostConfidenceUnknown
		event.PricingSource = core.PricingSource("service_catalog")
		event.PricingVersion = cat.CatalogVersion()
	}

	persistEvent(event, autoTask)
}

// persistEvent records a captured cost event. It always appends to the
// in-memory recording buffer (used by tests and lightweight integrations) and,
// when a durable storage buffer is registered via SetEventBuffer, also writes
// the event — and any per-request auto-task — to storage so the sync pusher
// ships it. autoTask is non-nil only for the per-request auto-task path; it is
// inserted before its event so storage ordering holds, then finalized.
func persistEvent(event core.Event, autoTask *core.Task) {
	storeRecordedEvent(event)

	buf := getEventBuffer()
	if buf == nil {
		// No durable storage wired — finalize the auto-task in memory only.
		finalizeAuto(autoTask, &event, nil)
		return
	}
	if autoTask != nil {
		if err := buf.InsertTask(*autoTask); err != nil {
			log.Printf("[dexcost] failed to persist http auto-task: %v", err)
		}
	}
	if err := buf.InsertEvent(event); err != nil {
		log.Printf("[dexcost] failed to persist http cost event: %v", err)
	}
	finalizeAuto(autoTask, &event, buf)
}

// SessionResolver resolves a session task ID for an anonymous HTTP request,
// grouping consecutive calls into one session task. It returns false to fall
// back to a per-request auto-task.
type SessionResolver func(ctx context.Context, callType string) (uuid.UUID, bool)

var (
	sessionResolverMu sync.RWMutex
	sessionResolver   SessionResolver
)

// SetSessionResolver registers (or clears, with nil) the session resolver used
// by the HTTP adapter. The top-level dexcost package wires this to its
// SessionManager so HTTP calls roll up into session tasks.
func SetSessionResolver(r SessionResolver) {
	sessionResolverMu.Lock()
	defer sessionResolverMu.Unlock()
	sessionResolver = r
}

// resolveTaskID returns the task ID to attribute the event to. Priority:
//
//  1. Explicit task in request context (core.GetCurrentTask).
//  2. Session task from the registered SessionResolver (groups consecutive
//     anonymous calls — Python parity: adapters/http.py session grouping).
//  3. Per-request auto-task from ambient ContextData with non-empty CustomerID.
//
// The boolean return is false when no path produces a task; callers should
// treat that as "skip the event entirely" so anonymous traffic never generates
// orphaned cost rows. The returned *core.Task is non-nil only for path 3 (a
// per-request auto-task that must be finalized); session tasks are owned by the
// SessionManager and outlive the request.
func resolveTaskID(req *http.Request) (uuid.UUID, *core.Task, bool) {
	if task := core.GetCurrentTask(req.Context()); task != nil {
		return task.TaskID, nil, true
	}
	cd := core.GetContextData(req.Context())
	if cd == nil || cd.CustomerID == "" {
		return uuid.Nil, nil, false
	}

	sessionResolverMu.RLock()
	resolver := sessionResolver
	sessionResolverMu.RUnlock()
	if resolver != nil {
		if id, ok := resolver(req.Context(), "http_request"); ok {
			return id, nil, true
		}
	}

	auto := core.CreateAutoTask(req.Context(), "http_request")
	return auto.TaskID, &auto, true
}

// storeRecordedEvent appends an event to the in-memory recording buffer used
// by tests and lightweight integrations. The buffer is capped at
// maxRecordedEvents; once that ceiling is hit the oldest entries are evicted
// FIFO so long-lived processes don't accumulate unbounded memory between
// ClearRecordedEvents calls.
func storeRecordedEvent(event core.Event) {
	recordedEventsMu.Lock()
	defer recordedEventsMu.Unlock()
	recordedEvents = append(recordedEvents, event)
	if len(recordedEvents) > maxRecordedEvents {
		excess := len(recordedEvents) - maxRecordedEvents
		recordedEvents = append(recordedEvents[:0], recordedEvents[excess:]...)
	}
}

// finalizeAuto closes a per-request auto-created task once its cost event is
// recorded, aggregating the event cost into the task totals. When buffer is
// non-nil the finalized task is persisted (core.FinalizeAutoTask issues an
// UpdateTask); pass nil for the in-memory-only path.
func finalizeAuto(autoTask *core.Task, event *core.Event, buffer core.Buffer) {
	if autoTask != nil {
		core.FinalizeAutoTask(autoTask, event, string(core.TaskStatusSuccess), buffer)
	}
}

// readAndReplaceBody reads the response body up to maxResponseBodySize, parses
// it as JSON if the Content-Type indicates JSON, and replaces resp.Body with a
// fresh reader so downstream callers see the body unchanged. Returns the
// parsed body (nil if not JSON, too large, or unparseable) and the replacement
// ReadCloser (nil when the body wasn't consumed).
//
// When the body exceeds the cap (and Content-Length didn't disclose this
// upfront), the buffered prefix is spliced back onto the unread tail via
// io.MultiReader so downstream readers still see the full payload — Python's
// adapter sidesteps this by relying on the requests library's body cache; Go's
// http.Response.Body is single-read so we have to re-stitch.
func readAndReplaceBody(resp *http.Response) (map[string]interface{}, io.ReadCloser) {
	if resp.Body == nil {
		return nil, nil
	}
	if !isJSONContentType(resp.Header.Get("Content-Type")) {
		return nil, nil
	}
	if cl := resp.ContentLength; cl > maxResponseBodySize {
		return nil, nil
	}
	limited := io.LimitReader(resp.Body, maxResponseBodySize+1)
	raw, err := io.ReadAll(limited)
	if err != nil {
		_ = resp.Body.Close()
		return nil, io.NopCloser(bytes.NewReader(raw))
	}
	if len(raw) > maxResponseBodySize {
		// Body exceeded the cap — splice the buffered prefix back onto the
		// unread tail so downstream callers receive the full body. Skip JSON
		// parsing because we don't have all the bytes.
		return nil, &spliceCloser{
			Reader: io.MultiReader(bytes.NewReader(raw), resp.Body),
			closer: resp.Body,
		}
	}
	_ = resp.Body.Close()
	var parsed map[string]interface{}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, io.NopCloser(bytes.NewReader(raw))
	}
	return parsed, io.NopCloser(bytes.NewReader(raw))
}

// spliceCloser fronts an io.Reader (typically an io.MultiReader stitching a
// buffered prefix onto an unread tail) while delegating Close to the original
// tail reader so http.Response cleanup still runs.
type spliceCloser struct {
	io.Reader
	closer io.Closer
}

func (s *spliceCloser) Close() error {
	return s.closer.Close()
}

// isJSONContentType returns true if the Content-Type header indicates a JSON
// response. Matches both `application/json` and `application/<vendor>+json`.
func isJSONContentType(ct string) bool {
	ct = strings.ToLower(ct)
	if ct == "" {
		return false
	}
	if i := strings.Index(ct, ";"); i >= 0 {
		ct = ct[:i]
	}
	ct = strings.TrimSpace(ct)
	return strings.HasSuffix(ct, "/json") || strings.HasSuffix(ct, "+json")
}

// flattenHeaders converts http.Header (multi-valued) to the single-value
// map used by ServiceCatalog.ExtractCost.
func flattenHeaders(h http.Header) map[string]string {
	out := make(map[string]string, len(h))
	for k, v := range h {
		if len(v) == 0 {
			continue
		}
		out[k] = v[0]
	}
	return out
}

// TrackHTTP returns a new *http.Client whose transport is wrapped with a
// trackingTransport. The original client is not mutated.
func TrackHTTP(client *http.Client) *http.Client {
	base := client.Transport
	if base == nil {
		base = http.DefaultTransport
	}
	return &http.Client{
		Transport: &trackingTransport{base: base},
		Timeout:   client.Timeout,
	}
}

var (
	globalTrackingMu      sync.Mutex
	originalDefaultRT     http.RoundTripper
	globalTrackingEnabled bool
)

// EnableGlobalHTTPTracking wraps http.DefaultTransport so that http.Get,
// http.DefaultClient, and any *http.Client with a nil Transport automatically
// record external cost events. Idempotent. This is the Go equivalent of
// Python's init(track_http=True), which patches HTTP libraries process-wide.
func EnableGlobalHTTPTracking() {
	globalTrackingMu.Lock()
	defer globalTrackingMu.Unlock()
	if globalTrackingEnabled {
		return
	}
	base := http.DefaultTransport
	if base == nil {
		base = &http.Transport{}
	}
	originalDefaultRT = base
	http.DefaultTransport = &trackingTransport{base: base}
	globalTrackingEnabled = true
}

// DisableGlobalHTTPTracking restores the original http.DefaultTransport.
// Idempotent. Called on SDK shutdown.
func DisableGlobalHTTPTracking() {
	globalTrackingMu.Lock()
	defer globalTrackingMu.Unlock()
	if !globalTrackingEnabled {
		return
	}
	http.DefaultTransport = originalDefaultRT
	originalDefaultRT = nil
	globalTrackingEnabled = false
}

// ---------------------------------------------------------------------------
// v1 network-capture helpers (byte measurement + body recording)
// ---------------------------------------------------------------------------

// measureRequestBytes approximates the on-the-wire size of an outbound
// request: request line + header block + body length (if known).
// Mirrors python `_netbytes.measure_bytes_from_headers`.
func measureRequestBytes(req *http.Request) int64 {
	headers := flattenHeaders(req.Header)
	bodyLen := 0
	if req.ContentLength > 0 {
		bodyLen = int(req.ContentLength)
	}
	method := req.Method
	if method == "" {
		method = "GET"
	}
	urlStr := req.URL.RequestURI()
	if urlStr == "" {
		urlStr = req.URL.String()
	}
	return int64(MeasureBytesFromHeaders(method, urlStr, headers, bodyLen))
}

// measureResponseHeaderBytes approximates the on-the-wire size of the
// inbound response headers (no body). Body bytes are added separately via
// the buffered-body path (Path 2) or the bodyRecorder.Read counter (Paths
// 1 and 3).
func measureResponseHeaderBytes(resp *http.Response) int64 {
	headers := flattenHeaders(resp.Header)
	// Pass empty method/url so the request-line formula contributes only
	// the constant 12-byte " HTTP/1.1\r\n" overhead (mirrors Python).
	return int64(MeasureBytesFromHeaders("", "", headers, 0))
}

// isInternalToValue returns a JSON-friendly value for the
// is_internal_traffic field (nil → nil, *bool → bool).
func isInternalToValue(p *bool) interface{} {
	if p == nil {
		return nil
	}
	return *p
}

// readAndReplaceBodyCounted is readAndReplaceBody plus the buffered body
// byte count. Returns 0 when the body wasn't read.
func readAndReplaceBodyCounted(resp *http.Response) (map[string]interface{}, io.ReadCloser, int64) {
	if resp.Body == nil {
		return nil, nil, 0
	}
	if !isJSONContentType(resp.Header.Get("Content-Type")) {
		return nil, nil, 0
	}
	if cl := resp.ContentLength; cl > maxResponseBodySize {
		return nil, nil, 0
	}
	limited := io.LimitReader(resp.Body, maxResponseBodySize+1)
	raw, err := io.ReadAll(limited)
	if err != nil {
		_ = resp.Body.Close()
		return nil, io.NopCloser(bytes.NewReader(raw)), int64(len(raw))
	}
	if len(raw) > maxResponseBodySize {
		return nil, &spliceCloser{
			Reader: io.MultiReader(bytes.NewReader(raw), resp.Body),
			closer: resp.Body,
		}, int64(len(raw))
	}
	_ = resp.Body.Close()
	var parsed map[string]interface{}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, io.NopCloser(bytes.NewReader(raw)), int64(len(raw))
	}
	return parsed, io.NopCloser(bytes.NewReader(raw)), int64(len(raw))
}

// bodyRecorder holds the state needed at body close: byte counts, the
// task accountant, and (for un-cataloged calls) the metadata needed to
// emit a `network` event at completion.
type bodyRecorder struct {
	accountant          *core.NetworkAccountant
	host                string
	requestBytes        int64
	responseHeaderBytes int64
	isInternal          *bool

	// Path 3 only — emit a `network` event on close with cost_pending=true.
	emitNetworkEvent bool
	taskID           uuid.UUID
	autoTask         *core.Task
	protocol         string
	method           string
	statusCode       int
	latencyMs        int
	url              string
}

// recordingReadCloser wraps a response body to count read bytes and, on
// close (or EOF), record the totals into the task accountant and
// optionally emit a `network` event. Idempotent — finalize fires once.
type recordingReadCloser struct {
	inner      io.ReadCloser
	state      *bodyRecorder
	bytesRead  int64
	finalized  bool
	finalizeMu sync.Mutex
}

func wrapBodyForRecording(body io.ReadCloser, state *bodyRecorder) io.ReadCloser {
	if body == nil {
		// Nothing to read; finalise immediately so the accountant still
		// sees the request side.
		state.finalize(0)
		return body
	}
	return &recordingReadCloser{inner: body, state: state}
}

func (r *recordingReadCloser) Read(p []byte) (int, error) {
	n, err := r.inner.Read(p)
	r.bytesRead += int64(n)
	if err == io.EOF {
		r.maybeFinalize()
	}
	return n, err
}

func (r *recordingReadCloser) Close() error {
	r.maybeFinalize()
	return r.inner.Close()
}

func (r *recordingReadCloser) maybeFinalize() {
	r.finalizeMu.Lock()
	defer r.finalizeMu.Unlock()
	if r.finalized {
		return
	}
	r.finalized = true
	r.state.finalize(r.bytesRead)
}

// finalize records the call's bytes into the accountant and, for the
// un-cataloged path, emits a `network` event when notable.
func (s *bodyRecorder) finalize(responseBodyBytes int64) {
	responseBytes := s.responseHeaderBytes + responseBodyBytes
	if s.accountant != nil {
		s.accountant.Record(s.host, responseBytes, s.requestBytes, s.isInternal)
	}
	if !s.emitNetworkEvent {
		return
	}
	combined := s.requestBytes + responseBytes
	thresholdBytes, latencyTrigger := networkEventTriggers()
	notable := combined > int64(thresholdBytes) ||
		s.statusCode >= 400 ||
		(latencyTrigger > 0 && s.latencyMs > latencyTrigger)
	if !notable {
		return // not notable — counters-only
	}
	event := core.NewEvent(s.taskID, core.EventTypeNetwork)
	event.ServiceName = s.host
	event.CostUSD = decimal.Zero
	event.CostConfidence = core.CostConfidenceUnknown
	event.PricingSource = ""
	event.Details["url"] = s.url
	event.Details["method"] = s.method
	event.Details["status_code"] = s.statusCode
	event.Details["cost_pending"] = true
	event.Details["protocol"] = s.protocol
	event.Details["request_bytes"] = s.requestBytes
	event.Details["response_bytes"] = responseBytes
	event.Details["is_internal_traffic"] = isInternalToValue(s.isInternal)
	event.Details["latency_ms"] = s.latencyMs
	persistEvent(event, s.autoTask)
}
