package transport

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/security"
)

const (
	defaultBatchSize     = 100
	defaultFlushInterval = 5 * time.Second
	initialBackoff       = 1 * time.Second
	maxBackoff           = 300 * time.Second
	maxPayloadBytes      = 200_000 // 200KB — well under SQS 256KB limit
	maxSplitDepth        = 5
	purgeRetention       = 7 * 24 * time.Hour

	// sdkVersion mirrors dexcost.Version. Duplicated here to avoid
	// importing the top-level package from a sub-package (would
	// create a cycle). Bump both in lockstep.
	sdkVersion = "0.1.0"
	userAgent  = "dexcost-go/" + sdkVersion
)

// taskSyncBuffer extends core.Buffer with task-syncing and purge capabilities.
type taskSyncBuffer interface {
	core.Buffer
	QueryTasksByIDs(taskIDs []string) ([]core.Task, error)
	MarkTasksSynced(taskIDs []string) error
	PurgeSyncedEvents(before time.Time) (int64, error)
	PurgeOldPendingEvents(before time.Time) (int64, error)
}

// EventPusher asynchronously pushes buffered events to the Control Layer API.
// It runs a background goroutine that periodically flushes pending events,
// with exponential backoff on failures.
type EventPusher struct {
	buffer    core.Buffer
	endpoint  string
	apiKey    string
	batchSize int
	interval  time.Duration
	backoff   time.Duration

	redactFields   []string
	hashCustomerID bool

	stopCh  chan struct{}
	flushCh chan chan error
	wg      sync.WaitGroup
	client  *http.Client

	mu      sync.Mutex
	stopped bool // permanently stopped on auth errors (401/403)
	running atomic.Bool
}

// PusherOptions configures the EventPusher.
type PusherOptions struct {
	Buffer         core.Buffer
	Endpoint       string
	APIKey         string
	BatchSize      int
	Interval       time.Duration
	Client         *http.Client
	RedactFields   []string
	HashCustomerID bool
}

// NewEventPusher creates and starts a new EventPusher background worker.
func NewEventPusher(opts PusherOptions) *EventPusher {
	batchSize := opts.BatchSize
	if batchSize <= 0 {
		batchSize = defaultBatchSize
	}
	interval := opts.Interval
	if interval <= 0 {
		interval = defaultFlushInterval
	}
	client := opts.Client
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}

	p := &EventPusher{
		buffer:         opts.Buffer,
		endpoint:       opts.Endpoint,
		apiKey:         opts.APIKey,
		batchSize:      batchSize,
		interval:       interval,
		backoff:        0,
		redactFields:   opts.RedactFields,
		hashCustomerID: opts.HashCustomerID,
		stopCh:         make(chan struct{}),
		flushCh:        make(chan chan error, 1),
		client:         client,
	}

	// Mark running BEFORE spawning the goroutine so Stop() called immediately
	// after construction observes running==true and waits, instead of
	// short-circuiting and leaking the goroutine.
	p.running.Store(true)
	p.wg.Add(1)
	go p.run()
	return p
}

func (p *EventPusher) run() {
	defer func() {
		p.running.Store(false)
		p.wg.Done()
	}()
	ticker := time.NewTicker(p.interval)
	defer ticker.Stop()

	for {
		select {
		case <-p.stopCh:
			// Final flush before exiting.
			p.pushBatch()
			return
		case <-ticker.C:
			p.mu.Lock()
			stopped := p.stopped
			p.mu.Unlock()
			if stopped {
				continue
			}
			if p.backoff > 0 {
				// Use select so a stop signal during backoff is not missed.
				select {
				case <-p.stopCh:
					p.pushBatch()
					return
				case <-time.After(p.backoff):
				}
			}
			p.pushBatch()
		case errCh := <-p.flushCh:
			p.mu.Lock()
			stopped := p.stopped
			p.mu.Unlock()
			if stopped {
				errCh <- nil
				continue
			}
			err := p.pushBatch()
			errCh <- err
		}
	}
}

// Start begins (or restarts) the background push loop.
// It is a no-op if the pusher is already running.
// SetAPIKey updates the pusher's API key and clears the stopped flag.
// Sprint 2 Theme D / §3.2.3 (B14). When the Control Layer returns
// 401/403 the pusher sets stopped=true (pusher.go:355) and silently
// skips every subsequent tick. This method is the only public path
// back to a working push loop without restarting the process.
func (p *EventPusher) SetAPIKey(newKey string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.apiKey = newKey
	p.stopped = false
}

func (p *EventPusher) Start() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.running.Load() {
		return
	}
	p.stopped = false
	p.stopCh = make(chan struct{})
	p.flushCh = make(chan chan error, 1)
	// Mark running BEFORE spawning the goroutine so a concurrent Stop()
	// observes running==true and waits on stopCh + wg, instead of
	// returning early and leaking the new run() goroutine.
	p.running.Store(true)
	p.wg.Add(1)
	go p.run()
}

func (p *EventPusher) pushBatch() error {
	p.mu.Lock()
	if p.stopped {
		p.mu.Unlock()
		return nil
	}
	p.mu.Unlock()

	events, err := p.buffer.QueryPendingEvents(p.batchSize)
	if err != nil {
		return err
	}
	if len(events) == 0 {
		return nil
	}

	// Apply PII redaction before serialization.
	p.redactEventDetails(events)

	// Serialize events to JSON dicts and collect IDs for sync marking.
	eventDicts := make([]map[string]interface{}, len(events))
	eventIDs := make([]string, len(events))
	taskIDSet := make(map[string]struct{})
	for i, e := range events {
		eventDicts[i] = e.ToDict()
		eventIDs[i] = e.EventID.String()
		taskIDSet[e.TaskID.String()] = struct{}{}
	}

	// Gather tasks for sync if the buffer supports it.
	var taskDicts []map[string]interface{}
	var taskIDs []string
	if tsb, ok := p.buffer.(taskSyncBuffer); ok {
		for tid := range taskIDSet {
			taskIDs = append(taskIDs, tid)
		}
		tasks, err := tsb.QueryTasksByIDs(taskIDs)
		if err == nil {
			p.redactTaskMetadata(tasks)
			taskDicts = make([]map[string]interface{}, len(tasks))
			for i := range tasks {
				taskDicts[i] = tasks[i].ToDict()
			}
		}
	}

	// Push with adaptive splitting for oversized payloads. Sprint 2
	// Theme D / §3.2.1 (B12): pushWithSplit now marks events / tasks
	// synced INSIDE each leaf POST that succeeds, so a partial failure
	// (first half OK, second half 5xx) doesn't cause the first half to
	// be re-sent next tick → no duplicates at the control plane.
	if err := p.pushWithSplit(eventDicts, taskDicts, 0); err != nil {
		return err
	}

	// Outer MarkSynced retained as a no-op safety net: if pushWithSplit
	// reached the leaf and marked everything synced, this is a no-op;
	// if a future code path returns nil without splitting, this still
	// ensures the marker call fires. Idempotent.
	if err := p.buffer.MarkSynced(eventIDs); err != nil {
		return err
	}

	// Mark tasks synced and purge old events if supported.
	if tsb, ok := p.buffer.(taskSyncBuffer); ok {
		if err := tsb.MarkTasksSynced(taskIDs); err != nil {
			log.Printf("[dexcost] failed to mark tasks synced: %v", err)
		}
		if n, err := tsb.PurgeSyncedEvents(time.Now().UTC().Add(-purgeRetention)); err != nil {
			log.Printf("[dexcost] failed to purge old events: %v", err)
		} else if n > 0 {
			log.Printf("[dexcost] purged %d old synced events", n)
		}
		// Also purge events stuck pending past the retention window so a
		// permanently failing sync doesn't grow the buffer unbounded.
		if n, err := tsb.PurgeOldPendingEvents(time.Now().UTC().Add(-purgeRetention)); err != nil {
			log.Printf("[dexcost] failed to purge stale pending events: %v", err)
		} else if n > 0 {
			log.Printf("[dexcost] purged %d stale pending events", n)
		}
	}

	return nil
}

// redactEventDetails applies PII redaction, customer-ID hashing, and the
// metadata-size limit to each event's Details before the event leaves the
// process.
func (p *EventPusher) redactEventDetails(events []core.Event) {
	for i := range events {
		if len(p.redactFields) > 0 && events[i].Details != nil {
			events[i].Details = security.RedactMap(events[i].Details, p.redactFields)
		}
		if p.hashCustomerID {
			if cid, ok := events[i].Details["customer_id"]; ok {
				if s, ok := cid.(string); ok {
					events[i].Details["customer_id"] = security.HashValue(s)
				}
			}
		}
		if events[i].Details != nil {
			events[i].Details = security.EnforceMetadataLimit(events[i].Details, 0)
		}
	}
}

// redactTaskMetadata applies PII redaction/hashing to task metadata and
// attribution fields before the task leaves the process. Without this,
// redact_fields and hash_customer_id would protect only event.details,
// leaving task.metadata (including _trace_links) and customer_id raw.
func (p *EventPusher) redactTaskMetadata(tasks []core.Task) {
	for i := range tasks {
		if len(p.redactFields) > 0 && tasks[i].Metadata != nil {
			tasks[i].Metadata = security.RedactMap(tasks[i].Metadata, p.redactFields)
		}
		if tasks[i].Metadata != nil {
			tasks[i].Metadata = security.EnforceMetadataLimit(tasks[i].Metadata, 0)
		}
		if p.hashCustomerID {
			if tasks[i].CustomerID != "" {
				tasks[i].CustomerID = security.HashValue(tasks[i].CustomerID)
			}
			if tasks[i].ProjectID != "" {
				tasks[i].ProjectID = security.HashValue(tasks[i].ProjectID)
			}
		}
	}
}

// pushWithSplit recursively splits oversized payloads until they fit within
// maxPayloadBytes. Tasks are only sent with the first half to avoid duplication.
func (p *EventPusher) pushWithSplit(events []map[string]interface{}, tasks []map[string]interface{}, depth int) error {
	payload, err := json.Marshal(map[string]interface{}{
		"events": events,
		"tasks":  tasks,
	})
	if err != nil {
		return fmt.Errorf("marshal events: %w", err)
	}

	if len(payload) <= maxPayloadBytes || depth >= maxSplitDepth {
		if err := p.postRaw(payload); err != nil {
			return err
		}
		// Sprint 2 Theme D / §3.2.1 (B12) — mark synced at the leaf so
		// a sibling-half failure does not unwind work that succeeded.
		ids := make([]string, 0, len(events))
		for _, e := range events {
			if id, ok := e["event_id"].(string); ok {
				ids = append(ids, id)
			}
		}
		if err := p.buffer.MarkSynced(ids); err != nil {
			return err
		}
		if tsb, ok := p.buffer.(taskSyncBuffer); ok && len(tasks) > 0 {
			tids := make([]string, 0, len(tasks))
			for _, t := range tasks {
				if id, ok := t["task_id"].(string); ok {
					tids = append(tids, id)
				}
			}
			if err := tsb.MarkTasksSynced(tids); err != nil {
				log.Printf("[dexcost] failed to mark tasks synced at leaf: %v", err)
			}
		}
		return nil
	}

	if len(events) <= 1 {
		log.Printf("[dexcost] Single event exceeds payload limit (%d bytes), skipping", len(payload))
		return nil
	}

	mid := len(events) / 2
	log.Printf("[dexcost] Batch too large (%d bytes, %d events), splitting", len(payload), len(events))

	if err := p.pushWithSplit(events[:mid], tasks, depth+1); err != nil {
		return err
	}
	return p.pushWithSplit(events[mid:], nil, depth+1)
}

// postRaw sends a pre-serialized JSON payload to the ingestion endpoint.
func (p *EventPusher) postRaw(body []byte) error {
	url := p.endpoint + "/v1/ingest"
	req, err := http.NewRequest("POST", url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+p.apiKey)
	req.Header.Set("User-Agent", userAgent)

	resp, err := p.client.Do(req)
	if err != nil {
		p.increaseBackoff()
		return fmt.Errorf("push events: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 413 {
		// Permanent error — don't retry, batch is too large.
		log.Printf("[dexcost] Server returned 413 — batch too large")
		return fmt.Errorf("payload too large (413)")
	}

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		// Auth error — stop permanently.
		log.Printf("[dexcost] Server returned %d — stopping sync (invalid API key)", resp.StatusCode)
		p.mu.Lock()
		p.stopped = true
		p.mu.Unlock()
		return fmt.Errorf("auth error (%d)", resp.StatusCode)
	}

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		// Success: read body to log queued / rejected counts so silent
		// tenant-mismatch or validation-reject scenarios are observable.
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		var summary struct {
			Queued   int `json:"queued"`
			Rejected int `json:"rejected"`
		}
		if err := json.Unmarshal(body, &summary); err != nil {
			log.Printf("[dexcost] ingest response unreadable: %v", err)
		}
		log.Printf("[dexcost] ingest accepted=%d rejected=%d status=%d", summary.Queued, summary.Rejected, resp.StatusCode)
		p.resetBackoff()
		return nil
	}

	if resp.StatusCode == 429 {
		// Server-directed backoff. Per RFC 7231 §7.1.3 the canonical signal is
		// the Retry-After header (delta-seconds or HTTP-date). The Dexcost
		// ingestion middleware also returns retry_after_ms in the JSON body
		// when no header is set. Prefer header, then body; fall back to
		// exponential backoff if the server returns 429 without a wait hint.
		if d, ok := parseRetryAfterHeader(resp.Header.Get("Retry-After"), time.Now()); ok {
			p.setRateLimitBackoff(d)
			log.Printf("[dexcost] Server returned 429 with Retry-After=%q — backing off %v", resp.Header.Get("Retry-After"), p.backoff)
			return fmt.Errorf("rate limited (429), retry after %v", p.backoff)
		}
		bodyBytes, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		if d, ok := parseRetryAfterBody(bodyBytes); ok {
			p.setRateLimitBackoff(d)
			log.Printf("[dexcost] Server returned 429 with retry_after_ms=%dms — backing off %v", int64(d/time.Millisecond), p.backoff)
			return fmt.Errorf("rate limited (429), retry after %v", p.backoff)
		}
		p.increaseBackoff()
		log.Printf("[dexcost] Server returned 429 with no Retry-After hint — using exponential backoff %v", p.backoff)
		return fmt.Errorf("rate limited (429)")
	}

	// Server error: increase backoff.
	p.increaseBackoff()
	return fmt.Errorf("push failed with status %d", resp.StatusCode)
}

func (p *EventPusher) increaseBackoff() {
	if p.backoff == 0 {
		p.backoff = initialBackoff
	} else {
		p.backoff *= 2
		if p.backoff > maxBackoff {
			p.backoff = maxBackoff
		}
	}
}

func (p *EventPusher) resetBackoff() {
	p.backoff = 0
}

// Backoff returns the current backoff duration (for testing).
func (p *EventPusher) Backoff() time.Duration {
	return p.backoff
}

// setRateLimitBackoff applies a server-directed wait, capped at maxBackoff to
// bound waste from a misbehaving or buggy server. Negative values are floored
// at zero so the next tick proceeds immediately.
func (p *EventPusher) setRateLimitBackoff(d time.Duration) {
	if d < 0 {
		d = 0
	}
	if d > maxBackoff {
		d = maxBackoff
	}
	p.backoff = d
}

// parseRetryAfterHeader parses an HTTP Retry-After header per RFC 7231 §7.1.3.
// The value is either delta-seconds (a non-negative integer) or an HTTP-date.
// Returns the wait duration and true on success; on parse failure returns 0
// and false so callers can fall back to other signals.
func parseRetryAfterHeader(value string, now time.Time) (time.Duration, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, false
	}
	if seconds, err := strconv.Atoi(value); err == nil {
		if seconds < 0 {
			return 0, false
		}
		return time.Duration(seconds) * time.Second, true
	}
	if t, err := http.ParseTime(value); err == nil {
		d := t.Sub(now)
		if d < 0 {
			return 0, true
		}
		return d, true
	}
	return 0, false
}

// parseRetryAfterBody extracts retry_after_ms from a 429 JSON response body.
// Dexcost's ingestion rate limiter returns this field when it has no
// Retry-After header to set. Returns the wait duration and true on success;
// returns 0 and false on any parse failure or non-positive value.
func parseRetryAfterBody(body []byte) (time.Duration, bool) {
	if len(body) == 0 {
		return 0, false
	}
	var v struct {
		RetryAfterMs *int64 `json:"retry_after_ms"`
	}
	if err := json.Unmarshal(body, &v); err != nil || v.RetryAfterMs == nil || *v.RetryAfterMs < 0 {
		return 0, false
	}
	return time.Duration(*v.RetryAfterMs) * time.Millisecond, true
}

// Flush forces an immediate push of pending events and blocks until complete.
func (p *EventPusher) Flush() error {
	errCh := make(chan error, 1)
	p.flushCh <- errCh
	return <-errCh
}

// Stop signals the background goroutine to exit and waits for it to finish.
// It is safe to call multiple times.
func (p *EventPusher) Stop() {
	p.mu.Lock()
	if !p.running.Load() {
		p.mu.Unlock()
		return
	}
	p.mu.Unlock()
	close(p.stopCh)
	p.wg.Wait()
	p.mu.Lock()
	p.stopped = true
	p.mu.Unlock()
}
