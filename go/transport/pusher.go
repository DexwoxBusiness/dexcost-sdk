package transport

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/attribution"
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/security"
)

const (
	defaultBatchSize     = 100
	defaultFlushInterval = 5 * time.Second
	initialBackoff       = 1 * time.Second
	maxBackoff           = 300 * time.Second
	maxPayloadBytes      = 120_000
	purgeRetention       = 7 * 24 * time.Hour
	purgeInterval        = time.Hour
	conversionScanMax    = 1_000
	conversionScanFactor = 10
	conversionWarnEvery  = time.Hour
)

// taskSyncBuffer extends core.Buffer with task-syncing and purge capabilities.
type taskSyncBuffer interface {
	core.Buffer
	QueryTasksByIDs(taskIDs []string) ([]core.Task, error)
	QueryPendingTasks(limit int) ([]core.Task, error)
	MarkTasksSynced(taskIDs []string) error
	PurgeSyncedEvents(before time.Time) (int64, error)
	PurgeOldPendingEvents(before time.Time) (int64, error)
}

type eventQuarantineBuffer interface {
	MarkQuarantined(eventIDs []string) error
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
	lastPurge time.Time

	lastConversionWarnAt  time.Time
	lastConversionWarnKey string

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
			p.pushBatch(false)
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
					p.pushBatch(false)
					return
				case <-time.After(p.backoff):
				}
			}
			p.pushBatch(false)
		case errCh := <-p.flushCh:
			p.mu.Lock()
			stopped := p.stopped
			p.mu.Unlock()
			if stopped {
				errCh <- nil
				continue
			}
			err := p.pushBatch(true)
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

func (p *EventPusher) pushBatch(surfaceConversionErrors bool) error {
	p.mu.Lock()
	if p.stopped {
		p.mu.Unlock()
		return nil
	}
	p.mu.Unlock()

	batchSize := max(1, p.batchSize)
	eventDicts := make([]map[string]interface{}, 0, batchSize)
	failedEventIDs := make([]string, 0)
	taskIDSet := make(map[string]struct{})
	seenEventIDs := make(map[string]struct{})
	scanLimit := max(batchSize, min(conversionScanMax, batchSize*conversionScanFactor))
	scanned := 0

	// Quarantine malformed pages as they are encountered and continue reading
	// the pending window. This prevents an old invalid prefix from starving
	// newer, valid attribution records forever.
	for len(eventDicts) < batchSize && scanned < scanLimit {
		pageLimit := min(batchSize-len(eventDicts), scanLimit-scanned)
		events, err := p.buffer.QueryPendingEvents(pageLimit)
		if err != nil {
			return err
		}
		if len(events) == 0 {
			break
		}
		// Redact before conversion because selected detail fields become typed
		// provider/resource fields in the v2 payload.
		p.redactEventDetails(events)
		observabilityEventIDs := make([]string, 0)
		pageFailedEventIDs := make([]string, 0)
		newlyScanned := 0
		for _, event := range events {
			eventID := event.EventID.String()
			if _, seen := seenEventIDs[eventID]; seen {
				continue
			}
			seenEventIDs[eventID] = struct{}{}
			newlyScanned++
			scanned++
			if event.EventType == core.EventTypeGPUUtilizationSignal {
				observabilityEventIDs = append(observabilityEventIDs, eventID)
				continue
			}
			converted := attribution.ToEventV2(event)
			if converted == nil {
				pageFailedEventIDs = append(pageFailedEventIDs, eventID)
				continue
			}
			wire, err := toMap(converted)
			if err != nil {
				return fmt.Errorf("serialize attribution event %s: %w", event.EventID, err)
			}
			taskIDSet[event.TaskID.String()] = struct{}{}
			eventDicts = append(eventDicts, wire)
		}
		if err := p.buffer.MarkSynced(observabilityEventIDs); err != nil {
			return err
		}
		if len(pageFailedEventIDs) > 0 {
			quarantine, ok := p.buffer.(eventQuarantineBuffer)
			if !ok {
				return fmt.Errorf("buffer cannot quarantine %d attribution conversion failure(s)", len(pageFailedEventIDs))
			}
			if err := quarantine.MarkQuarantined(pageFailedEventIDs); err != nil {
				return fmt.Errorf("quarantine attribution conversion failures: %w", err)
			}
			failedEventIDs = append(failedEventIDs, pageFailedEventIDs...)
		}
		// A custom backend may fail to advance its pending cursor. Avoid a busy
		// loop; a later flush can try again after the surfaced storage error.
		if newlyScanned == 0 || len(events) < pageLimit {
			break
		}
	}

	taskDicts := make([]map[string]interface{}, 0)
	if tsb, ok := p.buffer.(taskSyncBuffer); ok {
		dependencyIDs := make([]string, 0, len(taskIDSet))
		for tid := range taskIDSet {
			dependencyIDs = append(dependencyIDs, tid)
		}
		pendingTasks, err := tsb.QueryPendingTasks(p.batchSize)
		if err != nil {
			return err
		}
		dependencyTasks, err := tsb.QueryTasksByIDs(dependencyIDs)
		if err != nil {
			return err
		}
		byID := make(map[string]core.Task, len(pendingTasks)+len(dependencyTasks))
		for _, task := range pendingTasks {
			byID[task.TaskID.String()] = task
		}
		for _, task := range dependencyTasks {
			byID[task.TaskID.String()] = task
		}
		tasks := make([]core.Task, 0, len(byID))
		for _, task := range byID {
			tasks = append(tasks, task)
		}
		p.redactTaskMetadata(tasks)
		for _, task := range tasks {
			wire, err := toMap(attribution.ToTaskIngestV1(task))
			if err != nil {
				return fmt.Errorf("serialize task %s: %w", task.TaskID, err)
			}
			taskDicts = append(taskDicts, wire)
		}
	}
	if len(eventDicts) == 0 && len(taskDicts) == 0 {
		return p.handleConversionFailures(failedEventIDs, surfaceConversionErrors)
	}

	if err := p.pushWithSplit(eventDicts, taskDicts, 0); err != nil {
		return err
	}
	// Retention must run only after the selected batch is accepted. Running it
	// before QueryPendingEvents can silently delete deliverable records when a
	// client recovers after an outage longer than the retention window.
	p.maintainBuffer()

	return p.handleConversionFailures(failedEventIDs, surfaceConversionErrors)
}

func conversionFailure(eventIDs []string) error {
	if len(eventIDs) == 0 {
		return nil
	}
	preview := eventIDs
	if len(preview) > 3 {
		preview = preview[:3]
	}
	return fmt.Errorf(
		"%d event(s) were quarantined because they cannot be represented by attribution v2 (event IDs: %s)",
		len(eventIDs),
		strings.Join(preview, ", "),
	)
}

func (p *EventPusher) handleConversionFailures(eventIDs []string, surface bool) error {
	if len(eventIDs) == 0 {
		p.lastConversionWarnKey = ""
		return nil
	}
	err := conversionFailure(eventIDs)
	if surface {
		return err
	}
	now := time.Now()
	ids := append([]string(nil), eventIDs...)
	sort.Strings(ids)
	fingerprint := fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(ids, "\x00"))))
	if fingerprint != p.lastConversionWarnKey || now.Sub(p.lastConversionWarnAt) >= conversionWarnEvery {
		log.Printf("[dexcost] %v", err)
		p.lastConversionWarnKey = fingerprint
		p.lastConversionWarnAt = now
	}
	return nil
}

func (p *EventPusher) maintainBuffer() {
	now := time.Now().UTC()
	if !p.lastPurge.IsZero() && now.Sub(p.lastPurge) < purgeInterval {
		return
	}
	p.lastPurge = now
	tsb, ok := p.buffer.(taskSyncBuffer)
	if !ok {
		return
	}
	before := now.Add(-purgeRetention)
	if n, err := tsb.PurgeSyncedEvents(before); err != nil {
		log.Printf("[dexcost] failed to purge old events: %v", err)
	} else if n > 0 {
		log.Printf("[dexcost] purged %d old synced events", n)
	}
	if n, err := tsb.PurgeOldPendingEvents(before); err != nil {
		log.Printf("[dexcost] failed to purge stale pending/quarantined events: %v", err)
	} else if n > 0 {
		log.Printf("[dexcost] purged %d stale pending/quarantined events", n)
	}
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

// pushWithSplit recursively splits oversized payloads until every POST fits
// under the control-plane queue contract. Task records are sent before events.
func (p *EventPusher) pushWithSplit(events []map[string]interface{}, tasks []map[string]interface{}, depth int) error {
	if len(events) == 0 && len(tasks) == 0 {
		return nil
	}
	if events == nil {
		events = []map[string]interface{}{}
	}
	if tasks == nil {
		tasks = []map[string]interface{}{}
	}
	payload, err := json.Marshal(map[string]interface{}{
		"events": events,
		"tasks":  tasks,
	})
	if err != nil {
		return fmt.Errorf("marshal events: %w", err)
	}

	if len(payload) <= maxPayloadBytes {
		if err := p.postRaw(payload); err != nil {
			return err
		}
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

	// Splitting tasks separately prevents duplication and guarantees that a
	// dependent task is accepted before its event.
	if len(tasks) > 0 && len(events) > 0 {
		if err := p.pushWithSplit(nil, tasks, depth+1); err != nil {
			return err
		}
		return p.pushWithSplit(events, nil, depth+1)
	}
	if len(events) > 1 {
		mid := len(events) / 2
		log.Printf("[dexcost] batch too large (%d bytes, %d events), splitting", len(payload), len(events))
		if err := p.pushWithSplit(events[:mid], nil, depth+1); err != nil {
			return err
		}
		return p.pushWithSplit(events[mid:], nil, depth+1)
	}
	if len(tasks) > 1 {
		mid := len(tasks) / 2
		if err := p.pushWithSplit(nil, tasks[:mid], depth+1); err != nil {
			return err
		}
		return p.pushWithSplit(nil, tasks[mid:], depth+1)
	}

	// A singleton that is too large can never succeed. Acknowledge it locally
	// so it cannot poison every later flush.
	log.Printf("[dexcost] single attribution record exceeds payload limit (%d bytes), dropping", len(payload))
	if len(events) == 1 {
		if id, ok := events[0]["event_id"].(string); ok {
			return p.buffer.MarkSynced([]string{id})
		}
	}
	if len(tasks) == 1 {
		if tsb, ok := p.buffer.(taskSyncBuffer); ok {
			if id, ok := tasks[0]["task_id"].(string); ok {
				return tsb.MarkTasksSynced([]string{id})
			}
		}
	}
	return nil
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
		if summary.Rejected > 0 {
			p.increaseBackoff()
			return fmt.Errorf("ingest rejected %d record(s)", summary.Rejected)
		}
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

func toMap(value interface{}) (map[string]interface{}, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	result := make(map[string]interface{})
	if err := json.Unmarshal(encoded, &result); err != nil {
		return nil, err
	}
	return result, nil
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
