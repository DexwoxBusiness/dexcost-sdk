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

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
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
)

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
	resp, err := t.base.RoundTrip(req)
	if err != nil {
		return resp, err
	}

	// First check: user-registered domain rates take precedence.
	hostKey := req.URL.Host
	domainRatesMu.RLock()
	rate, hasRate := domainRates[hostKey]
	domainRatesMu.RUnlock()

	if hasRate {
		t.recordDomainRate(req, resp, hostKey, rate)
		return resp, nil
	}

	// Second check: service catalog match.
	cat := GetServiceCatalog()
	if cat == nil {
		return resp, nil
	}
	entry := cat.Lookup(req.URL.String())
	if entry == nil {
		return resp, nil
	}

	headers := flattenHeaders(resp.Header)
	body, replaced := readAndReplaceBody(resp)
	if replaced != nil {
		resp.Body = replaced
	}

	t.recordCatalogEntry(req, cat, entry, headers, body)
	return resp, nil
}

// recordDomainRate records an external_cost event from a user-registered rate.
func (t *trackingTransport) recordDomainRate(
	req *http.Request,
	_ *http.Response,
	domain string,
	rate DomainRate,
) {
	taskID, autoTask, ok := resolveTaskID(req)
	if !ok {
		return
	}
	event := core.NewEvent(taskID, core.EventTypeExternalCost)
	event.ServiceName = domain
	event.CostUSD = rate.CostUSD
	event.CostConfidence = core.CostConfidenceExact
	event.PricingSource = core.PricingSourceRateRegistry
	event.Details["url"] = req.URL.String()
	event.Details["per"] = rate.Per

	persistEvent(event, autoTask)
}

// recordCatalogEntry records an external_cost event from a service catalog match.
func (t *trackingTransport) recordCatalogEntry(
	req *http.Request,
	cat *pricing.ServiceCatalog,
	entry *pricing.ServiceEntry,
	headers map[string]string,
	body map[string]interface{},
) {
	taskID, autoTask, ok := resolveTaskID(req)
	if !ok {
		return
	}

	result := cat.ExtractCost(entry, headers, body)

	event := core.NewEvent(taskID, core.EventTypeExternalCost)
	event.Details["url"] = req.URL.String()
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
