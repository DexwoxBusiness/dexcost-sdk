package core

import (
	"context"
	"errors"
	"log"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
)

// ErrTaskAlreadyEnded is returned when End is called on an already-ended task.
var ErrTaskAlreadyEnded = errors.New("task already ended")

// TrackerOptions configures the Tracker.
type TrackerOptions struct {
	Buffer                  Buffer // storage backend (e.g. transport.SQLiteBuffer)
	Pricing                 *pricing.Engine
	Rates                   *pricing.RateRegistry
	EnableRetryHeuristics   bool
	RetryHeuristicWindow    float64
	RetryHeuristicThreshold float64

	// Compute v2 (Task 8/9 wiring) — optional. When ComputePricingEngine is
	// nil the tracker auto-creates one at first finalize. Overrides are
	// per-billing-model knobs (e.g. {"cloud_run": "instance"}). K8sNodeAware
	// is wired through for the future opt-in node probe (Task 9 stub).
	ComputePricingEngine    *pricing.ComputePricingEngine
	ComputeBillingOverrides map[string]string
	K8sNodeAware            bool
}

// Tracker manages task lifecycles, cost recording, and aggregation.
type Tracker struct {
	buffer     Buffer
	pricing    *pricing.Engine
	rates      *pricing.RateRegistry
	heuristics *RetryHeuristicEngine

	// Compute v2.
	computePricingEngine    *pricing.ComputePricingEngine
	computeBillingOverrides map[string]string
	k8sNodeAware            bool
}

// NewTracker creates a Tracker using the provided Buffer and pricing engine.
// The caller is responsible for creating and closing the Buffer.
func NewTracker(opts TrackerOptions) (*Tracker, error) {
	var err error
	pricingEngine := opts.Pricing
	if pricingEngine == nil {
		pricingEngine, err = pricing.NewEngine()
		if err != nil {
			return nil, err
		}
	}
	rates := opts.Rates
	if rates == nil {
		rates = pricing.NewRateRegistry()
	}
	tracker := &Tracker{
		buffer:                  opts.Buffer,
		pricing:                 pricingEngine,
		rates:                   rates,
		computePricingEngine:    opts.ComputePricingEngine,
		computeBillingOverrides: opts.ComputeBillingOverrides,
		k8sNodeAware:            opts.K8sNodeAware,
	}
	if opts.EnableRetryHeuristics {
		window := opts.RetryHeuristicWindow
		if window == 0 {
			window = 30
		}
		threshold := opts.RetryHeuristicThreshold
		if threshold == 0 {
			threshold = 0.8
		}
		tracker.heuristics = NewRetryHeuristicEngine(window, threshold)
	}
	return tracker, nil
}

// Buffer returns the underlying storage buffer for direct queries.
func (tr *Tracker) Buffer() Buffer {
	return tr.buffer
}

// Pricing returns the pricing engine.
func (tr *Tracker) Pricing() *pricing.Engine {
	return tr.pricing
}

// Rates returns the rate registry.
func (tr *Tracker) Rates() *pricing.RateRegistry {
	return tr.rates
}

// Close releases all resources held by the Tracker.
func (tr *Tracker) Close() error {
	return tr.buffer.Close()
}

// TaskOption is a functional option for StartTask.
type TaskOption func(*taskConfig)

type taskConfig struct {
	customerID   string
	projectID    string
	experimentID string
	variant      string
	metadata     map[string]interface{}
}

// WithCustomer sets the customer_id on the task.
func WithCustomer(id string) TaskOption {
	return func(c *taskConfig) {
		c.customerID = id
	}
}

// WithProject sets the project_id on the task.
func WithProject(id string) TaskOption {
	return func(c *taskConfig) {
		c.projectID = id
	}
}

// WithMetadata sets additional metadata on the task.
func WithMetadata(m map[string]interface{}) TaskOption {
	return func(c *taskConfig) {
		c.metadata = m
	}
}

// WithExperiment sets the experiment_id on the task.
func WithExperiment(id string) TaskOption {
	return func(c *taskConfig) {
		c.experimentID = id
	}
}

// WithVariant sets the variant label on the task.
func WithVariant(v string) TaskOption {
	return func(c *taskConfig) {
		c.variant = v
	}
}

// StartTask begins tracking a new task and returns a derived context
// with the task attached. The parent task (if any) is linked automatically.
func (tr *Tracker) StartTask(ctx context.Context, taskType string, opts ...TaskOption) (context.Context, *TrackedTask) {
	cfg := &taskConfig{}
	for _, o := range opts {
		o(cfg)
	}

	task := NewTask(taskType)
	task.Status = TaskStatusRunning
	task.CustomerID = cfg.customerID
	task.ProjectID = cfg.projectID
	task.ExperimentID = cfg.experimentID
	task.Variant = cfg.variant
	if cfg.metadata != nil {
		copied := make(map[string]interface{}, len(cfg.metadata))
		for k, v := range cfg.metadata {
			copied[k] = v
		}
		task.Metadata = copied
	}

	// Link parent from context.
	LinkParent(ctx, &task)

	// Persist to buffer.
	if err := tr.buffer.InsertTask(task); err != nil {
		log.Printf("[dexcost] failed to persist task: %v", err)
	}

	// Register a NetworkAccountant for this task so the HTTP adapter
	// (which sees only the task_id via context) can record byte usage
	// via core.GetAccountant(taskID). Unregistered at EndTask.
	RegisterAccountant(task.TaskID.String(), NewNetworkAccountant())

	tt := &TrackedTask{
		Task:    task,
		tracker: tr,
	}

	newCtx := WithTask(ctx, &tt.Task)
	newCtx = WithTrackedTask(newCtx, tt)
	return newCtx, tt
}

// TrackedTask wraps a Task and provides methods to record costs and end the task.
type TrackedTask struct {
	Task    Task
	tracker *Tracker
	mu      sync.Mutex
	ended   bool
}

// LinkTrace attaches an external trace link (e.g. LangSmith, Datadog) to this task.
func (tt *TrackedTask) LinkTrace(provider, traceID string) {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	links, ok := tt.Task.Metadata["_trace_links"].([]interface{})
	if !ok {
		links = []interface{}{}
	}
	links = append(links, map[string]interface{}{
		"provider": provider,
		"trace_id": traceID,
	})
	tt.Task.Metadata["_trace_links"] = links
}

// GetTraceLinks returns all linked traces for this task.
func (tt *TrackedTask) GetTraceLinks() []map[string]string {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	links, ok := tt.Task.Metadata["_trace_links"].([]interface{})
	if !ok {
		return nil
	}
	var result []map[string]string
	for _, l := range links {
		if m, ok := l.(map[string]interface{}); ok {
			r := make(map[string]string, len(m))
			for k, v := range m {
				if s, ok := v.(string); ok {
					r[k] = s
				}
			}
			result = append(result, r)
		}
	}
	return result
}

// RecordCost records a non-LLM cost event (external_cost) on this task.
func (tt *TrackedTask) RecordCost(service string, costUSD decimal.Decimal, opts ...EventOption) error {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	if tt.ended {
		return ErrTaskAlreadyEnded
	}

	ecfg := &eventConfig{}
	for _, o := range opts {
		o.applyEventConfig(ecfg)
	}

	evType := EventTypeExternalCost
	if ecfg.eventType != "" {
		evType = ecfg.eventType
	}
	event := NewEvent(tt.Task.TaskID, evType)
	event.ServiceName = service
	event.CostUSD = costUSD

	if ecfg.costConfidence != "" {
		event.CostConfidence = ecfg.costConfidence
	} else {
		event.CostConfidence = CostConfidenceExact
	}
	if ecfg.pricingSource != "" {
		event.PricingSource = ecfg.pricingSource
	} else {
		event.PricingSource = PricingSourceManual
	}
	if ecfg.pricingVersion != "" {
		event.PricingVersion = ecfg.pricingVersion
	}
	if ecfg.operation != "" {
		event.Details["operation"] = ecfg.operation
	}
	for k, v := range ecfg.details {
		event.Details[k] = v
	}

	return tt.tracker.buffer.InsertEvent(event)
}

// RecordLLMCall records an LLM call event on this task.
// If costUSD is nil or zero, the pricing engine auto-computes the cost.
func (tt *TrackedTask) RecordLLMCall(provider, model string, inputTokens, outputTokens int, opts ...LLMCallOption) error {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	if tt.ended {
		return ErrTaskAlreadyEnded
	}

	lcfg := &llmCallConfig{}
	for _, o := range opts {
		o.applyLLMCallConfig(lcfg)
	}

	event := NewEvent(tt.Task.TaskID, EventTypeLLMCall)
	event.Provider = provider
	event.Model = model
	event.InputTokens = &inputTokens
	event.OutputTokens = &outputTokens

	if lcfg.cachedTokens > 0 {
		event.CachedTokens = &lcfg.cachedTokens
	}
	if lcfg.latencyMs > 0 {
		event.LatencyMs = &lcfg.latencyMs
	}
	if lcfg.errorType != "" {
		event.ErrorType = lcfg.errorType
		// Mirror error_type into Details: the retry heuristic engine
		// (heuristics.go Check) inspects Details["error_type"] on prior
		// events, so without this WithErrorType-based retry detection never
		// fires. Python parity: tracker.py stores error_type in event details.
		event.Details["error_type"] = lcfg.errorType
	}

	if lcfg.costUSD != nil && !lcfg.costUSD.IsZero() {
		event.CostUSD = *lcfg.costUSD
		event.CostConfidence = CostConfidenceExact
		event.PricingSource = PricingSourceManual
	} else {
		// Auto-compute from pricing engine.
		cached := 0
		if lcfg.cachedTokens > 0 {
			cached = lcfg.cachedTokens
		}
		cacheCreation := 0
		if lcfg.cacheCreationTokens > 0 {
			cacheCreation = lcfg.cacheCreationTokens
		}
		result := tt.tracker.pricing.GetCost(model, inputTokens, outputTokens, cached, cacheCreation)
		event.CostUSD = result.CostUSD
		event.CostConfidence = CostConfidence(result.CostConfidence)
		event.PricingSource = PricingSource(result.PricingSource)
		event.PricingVersion = result.PricingVersion
	}

	// Explicit overrides via LLMCallOption — these win over both the explicit-
	// cost branch and the auto-pricing branch so callers can decouple
	// cost_confidence / pricing_source from the registry state (e.g. failure
	// events that must stay Unknown even if the model is later added to the
	// pricing map). Details merge so per-call correlators (query_index, etc.)
	// flow through to the schema's Details field.
	if lcfg.costConfidence != "" {
		event.CostConfidence = lcfg.costConfidence
	}
	if lcfg.pricingSource != "" {
		event.PricingSource = lcfg.pricingSource
	}
	if lcfg.pricingVersion != "" {
		event.PricingVersion = lcfg.pricingVersion
	}
	for k, v := range lcfg.details {
		event.Details[k] = v
	}

	if err := tt.tracker.buffer.InsertEvent(event); err != nil {
		return err
	}

	if tt.tracker.heuristics != nil {
		match := tt.tracker.heuristics.Check(event)
		if match.IsRetry {
			event.IsRetry = true
			event.RetryReason = "heuristic"
			event.RetryOf = match.MatchedEventID
			if updErr := tt.tracker.buffer.UpdateEvent(event); updErr != nil {
				log.Printf("[dexcost] failed to update event with retry heuristic: %v", updErr)
			}
		}
		tt.tracker.heuristics.Record(event)
	}

	return nil
}

// RecordUsage records a non-LLM cost event based on the rate registry.
// It looks up the service in the rate registry and multiplies by units.
func (tt *TrackedTask) RecordUsage(service string, units int) error {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	if tt.ended {
		return ErrTaskAlreadyEnded
	}

	rate := tt.tracker.rates.Get(service)
	if rate == nil {
		// Unknown rate; record with zero cost and unknown confidence.
		event := NewEvent(tt.Task.TaskID, EventTypeExternalCost)
		event.ServiceName = service
		event.CostUSD = decimal.Zero
		event.CostConfidence = CostConfidenceUnknown
		event.PricingSource = PricingSourceUnknown
		event.Details["units"] = units
		return tt.tracker.buffer.InsertEvent(event)
	}

	costUSD := rate.CostUSD.Mul(decimal.NewFromInt(int64(units)))
	event := NewEvent(tt.Task.TaskID, EventTypeExternalCost)
	event.ServiceName = service
	event.CostUSD = costUSD
	event.CostConfidence = CostConfidenceComputed
	event.PricingSource = PricingSourceRateRegistry
	event.PricingVersion = tt.tracker.rates.PricingVersion()
	event.Details["units"] = units
	event.Details["per"] = rate.Per
	return tt.tracker.buffer.InsertEvent(event)
}

// MarkRetry records a retry marker event on this task.
func (tt *TrackedTask) MarkRetry(reason string, opts ...RetryOption) error {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	if tt.ended {
		return ErrTaskAlreadyEnded
	}

	rcfg := &retryConfig{}
	for _, o := range opts {
		o.applyRetryConfig(rcfg)
	}

	event := NewEvent(tt.Task.TaskID, EventTypeRetryMarker)
	event.IsRetry = true
	event.RetryReason = reason

	if rcfg.retryOf != nil {
		event.RetryOf = rcfg.retryOf
	}
	if rcfg.costUSD != nil {
		event.CostUSD = *rcfg.costUSD
	}

	return tt.tracker.buffer.InsertEvent(event)
}

// MarkNotRetry clears the retry flag on a retry-marked event.
// If eventID is uuid.Nil, the first retry event found for this task is cleared.
// If the event is not found or not a retry, this is a no-op.
func (tt *TrackedTask) MarkNotRetry(eventID uuid.UUID) error {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	if tt.ended {
		return ErrTaskAlreadyEnded
	}

	events, err := tt.tracker.buffer.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		return err
	}

	var target *Event
	if eventID != uuid.Nil {
		for i := range events {
			if events[i].EventID == eventID && events[i].IsRetry {
				target = &events[i]
				break
			}
		}
	} else {
		// Find the most recent retry event
		for i := len(events) - 1; i >= 0; i-- {
			if events[i].IsRetry {
				target = &events[i]
				break
			}
		}
	}

	if target == nil {
		return nil
	}
	target.IsRetry = false
	target.RetryReason = ""
	target.RetryOf = nil
	return tt.tracker.buffer.UpdateEvent(*target)
}

// End ends the task with the given status and aggregates all event costs.
func (tt *TrackedTask) End(status TaskStatus) error {
	tt.mu.Lock()
	defer tt.mu.Unlock()
	if tt.ended {
		return ErrTaskAlreadyEnded
	}
	tt.ended = true

	now := time.Now().UTC()
	tt.Task.EndedAt = &now
	tt.Task.Status = status

	// Aggregate costs from all events.
	events, err := tt.tracker.buffer.QueryEvents(tt.Task.TaskID.String())
	if err != nil {
		return err
	}

	tt.aggregateCosts(events)

	return tt.tracker.buffer.UpdateTask(tt.Task)
}

// aggregateCosts sums up costs from all events, matching the Python SDK's
// _aggregate_costs logic.
func (tt *TrackedTask) aggregateCosts(events []Event) {
	llmCost := decimal.Zero
	extCost := decimal.Zero
	compCost := decimal.Zero
	retryCost := decimal.Zero
	var inTok, outTok, cacheTok int
	var retryCnt, failCnt int

	for _, e := range events {
		switch e.EventType {
		case EventTypeLLMCall:
			llmCost = llmCost.Add(e.CostUSD)
			if e.InputTokens != nil {
				inTok += *e.InputTokens
			}
			if e.OutputTokens != nil {
				outTok += *e.OutputTokens
			}
			if e.CachedTokens != nil {
				cacheTok += *e.CachedTokens
			}
		case EventTypeExternalCost:
			extCost = extCost.Add(e.CostUSD)
		case EventTypeComputeCost:
			compCost = compCost.Add(e.CostUSD)
		case EventTypeRetryMarker:
			retryCnt++
			retryCost = retryCost.Add(e.CostUSD)
		}

		if e.IsRetry {
			// Retry events also contribute to retry cost regardless of type.
			if e.EventType != EventTypeRetryMarker {
				retryCost = retryCost.Add(e.CostUSD)
				retryCnt++
			}
		}
	}

	if tt.Task.Status == TaskStatusFailed {
		failCnt = 1
	}

	tt.Task.LLMCostUSD = llmCost
	tt.Task.ExternalCostUSD = extCost
	tt.Task.ComputeCostUSD = compCost
	tt.Task.TotalCostUSD = llmCost.Add(extCost).Add(compCost)
	tt.Task.TotalInputTokens = inTok
	tt.Task.TotalOutputTokens = outTok
	tt.Task.TotalCachedTokens = cacheTok
	tt.Task.RetryCount = retryCnt
	tt.Task.RetryCostUSD = retryCost
	tt.Task.FailureCount = failCnt

	// ── Network finalize — v1 byte aggregates + v2 egress pricing ─────
	// Mirrors python tracker.py:_aggregate_costs + rust TrackedTask::
	// finalize_network. Tier-5 fail-silent: wrap in a closure so a
	// pricing bug never breaks task finalization. On panic or error,
	// task still ships with v1 + LLM/external/compute costs intact.
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[dexcost] WARNING: egress cost computation panicked for task %s: %v", tt.Task.TaskID, r)
			tt.Task.NetworkCostUSD = decimal.Zero
		}
	}()
	tt.finalizeNetwork(events)

	// ── Compute finalize — v2 compute pricing + per-event back-fill ───
	// Mirrors python tracker._finalize_compute. Tier-5 fail-silent in its
	// own recover() so a compute-pricing bug never breaks task finalize.
	func() {
		defer func() {
			if r := recover(); r != nil {
				log.Printf("[dexcost] WARNING: compute cost computation panicked for task %s: %v", tt.Task.TaskID, r)
			}
		}()
		tt.finalizeCompute(events)
	}()
}

// finalizeCompute does three things (mirror python _finalize_compute):
//
//  1. If the task's compute accountant is a long-running runtime
//     (Fargate / EC2 / GCE / Azure VM / K8s pod), call SnapshotEndAndBuild
//     and persist a compute_cost event with CostUSD = 0 and
//     cost_pending: true.
//  2. Walk events for the task; for each compute_cost event with
//     details["cost_pending"] == true, call engine.ResolveComputeCost
//     and update the event (set CostUSD, PricingSource, CostConfidence,
//     PricingVersion = "compute:<catalog_version>", strip cost_pending).
//  3. Adjust task.ComputeCostUSD and task.TotalCostUSD by the DELTA per
//     back-filled event (NOT a full recompute — preserves retry_marker
//     costs already summed by aggregateCosts).
func (tt *TrackedTask) finalizeCompute(events []Event) {
	accountant := UnregisterComputeAccountant(tt.Task.TaskID.String())

	// Step 1 — long-running snapshot: emit the deferred event now.
	if accountant != nil && IsLongRunningRuntime(accountant.Runtime) {
		durationMS := int64(0)
		if tt.Task.EndedAt != nil {
			durationMS = tt.Task.EndedAt.Sub(tt.Task.StartedAt).Milliseconds()
		}
		details := accountant.SnapshotEndAndBuild(durationMS)
		if details != nil {
			ev := NewEvent(tt.Task.TaskID, EventTypeComputeCost)
			ev.CostUSD = decimal.Zero
			ev.CostConfidence = CostConfidenceUnknown
			ev.Details = details
			if err := tt.tracker.buffer.InsertEvent(ev); err != nil {
				log.Printf("[dexcost] WARNING: failed to record compute_cost event: %v", err)
			} else {
				// Include in the local back-fill walk below.
				events = append(events, ev)
			}
		}
	}

	// Lazy-init the engine if the tracker was constructed without one.
	if tt.tracker.computePricingEngine == nil {
		tt.tracker.computePricingEngine = pricing.NewComputePricingEngine()
	}
	engine := tt.tracker.computePricingEngine
	overrides := tt.tracker.computeBillingOverrides
	pricingVersion := "compute:" + engine.CatalogVersion()
	env := cloud.GetCloudEnv()

	// Step 2/3 — back-fill walk over compute_cost events with cost_pending.
	for _, ev := range events {
		if ev.EventType != EventTypeComputeCost {
			continue
		}
		pending, _ := ev.Details["cost_pending"].(bool)
		if !pending {
			continue
		}
		// Decimal window_s := 0 (engine derives from duration_ms when zero).
		cost := engine.ResolveComputeCost(ev.Details, env, overrides, decimal.Zero)

		prev := ev.CostUSD
		ev.CostUSD = cost.CostUSD
		ev.CostConfidence = CostConfidence(cost.CostConfidence)
		ev.PricingSource = PricingSource(cost.PricingSource)
		ev.PricingVersion = pricingVersion
		delete(ev.Details, "cost_pending")

		if err := tt.tracker.buffer.UpdateEvent(ev); err != nil {
			log.Printf("[dexcost] WARNING: failed to back-fill compute event %s: %v", ev.EventID, err)
			continue
		}

		// Delta-based total adjustment — NOT a full recompute. Preserves
		// retry_marker costs already aggregated above; mirrors python.
		delta := cost.CostUSD.Sub(prev)
		tt.Task.ComputeCostUSD = tt.Task.ComputeCostUSD.Add(delta)
		tt.Task.TotalCostUSD = tt.Task.TotalCostUSD.Add(delta)
	}
}

// finalizeNetwork snapshots the NetworkAccountant onto the task's v1
// fields and (if a CloudEnv has been resolved) computes v2 egress dollars
// + back-fills the cost_pending network events for this task.
//
// Caller (aggregateCosts) wraps this in a Tier-5 fail-silent shell so a
// pricing bug never breaks task finalization.
func (tt *TrackedTask) finalizeNetwork(events []Event) {
	// v1 — drain the accountant into task fields. Lookup-then-unregister
	// because the task is ending; further HTTP calls attributed to this
	// task_id will get nil (no orphan rows, matches Python's frozen-then-
	// snapshot rule).
	accountant := UnregisterAccountant(tt.Task.TaskID.String())
	if accountant == nil {
		// No accountant was registered (e.g. ad-hoc task creation outside
		// StartTask). Nothing to do; v1 fields stay at zero.
		return
	}
	snapshot := accountant.Finalize()
	tt.Task.NetworkBytesIn = snapshot.BytesIn
	tt.Task.NetworkBytesOut = snapshot.BytesOut
	tt.Task.NetworkCallCount = snapshot.CallCount
	tt.Task.NetworkByHost = snapshot.ByHost

	// v2 — egress pricing.
	env := cloud.GetCloudEnv()
	engine := pricing.NewEgressPricingEngine()
	rate := engine.ResolveRate(env.Provider, env.Region)
	pricingVersion := "egress:" + engine.CatalogVersion()

	// Convert external_bytes_out scalar to GB (decimal, never float —
	// spec §6.3). 1 GB = 10^9 bytes, NOT 2^30.
	divisor := decimal.NewFromInt(1_000_000_000)
	externalGB := decimal.NewFromInt(snapshot.ExternalBytesOut).Div(divisor)
	tt.Task.NetworkCostUSD = externalGB.Mul(rate.RatePerGB)

	// Stamp per-host egress_cost_usd into network_by_host[].hosts. The
	// per-host external_bytes_out survives the LIVE_CAP overflow + top-N
	// cap; sum(per-host egress_cost_usd) == NetworkCostUSD by construction
	// (v2 §10.3 property invariant 2).
	if hostsRaw, ok := tt.Task.NetworkByHost["hosts"].([]map[string]interface{}); ok {
		for _, host := range hostsRaw {
			var hostExternal int64
			if v, ok := host["external_bytes_out"].(int64); ok {
				hostExternal = v
			}
			hostGB := decimal.NewFromInt(hostExternal).Div(divisor)
			host["egress_cost_usd"] = hostGB.Mul(rate.RatePerGB).String()
		}
	}

	// v2 §6.4 — back-fill each network event for this task. Walk the
	// query result for any cost_pending events, compute their cost,
	// strip the marker, and UpdateEvent to re-sync.
	for _, ev := range events {
		if ev.EventType != EventTypeNetwork {
			continue
		}
		pending, _ := ev.Details["cost_pending"].(bool)
		if !pending {
			continue
		}
		respBytes := int64(0)
		if v, ok := ev.Details["response_bytes"].(int64); ok {
			respBytes = v
		}
		reqBytes := int64(0)
		if v, ok := ev.Details["request_bytes"].(int64); ok {
			reqBytes = v
		}
		isInternal, _ := ev.Details["is_internal_traffic"].(bool)

		var billableBytes int64
		if isInternal {
			billableBytes = 0
		} else {
			billableBytes = respBytes + reqBytes
		}
		evGB := decimal.NewFromInt(billableBytes).Div(divisor)
		evCost := evGB.Mul(rate.RatePerGB)

		ev.CostUSD = evCost
		if isInternal {
			ev.CostConfidence = CostConfidenceExact
		} else {
			ev.CostConfidence = CostConfidence(rate.CostConfidence)
		}
		ev.PricingVersion = pricingVersion
		// Strip the cost_pending marker so the back-filled event is no
		// longer "deferred-cost".
		delete(ev.Details, "cost_pending")
		// Stamp egress_pricing_source string so the wire payload carries
		// the v2 source detail (egress_catalog:aws:us-east-1).
		if isInternal {
			ev.Details["egress_pricing_source"] = "egress_catalog:internal"
		} else {
			ev.Details["egress_pricing_source"] = rate.PricingSource
		}

		if err := tt.tracker.buffer.UpdateEvent(ev); err != nil {
			log.Printf("[dexcost] WARNING: failed to back-fill network event %s: %v", ev.EventID, err)
			continue
		}

		// First-pass total_cost_usd summed this event at $0 (cost_pending);
		// add the back-filled cost.
		tt.Task.TotalCostUSD = tt.Task.TotalCostUSD.Add(evCost)
	}

	// Add network_cost_usd to total — captures every external byte
	// (cataloged + below-threshold un-cataloged calls included via the
	// accountant scalar even when they emitted no per-event row).
	tt.Task.TotalCostUSD = tt.Task.TotalCostUSD.Add(tt.Task.NetworkCostUSD)
}

// EventOption configures a non-LLM cost event. Implementations apply their
// configuration to the eventConfig accumulator.
type EventOption interface {
	applyEventConfig(*eventConfig)
}

// LLMCallOption configures an LLM call event. Implementations apply their
// configuration to the llmCallConfig accumulator.
type LLMCallOption interface {
	applyLLMCallConfig(*llmCallConfig)
}

// RetryOption configures a retry marker event. Implementations apply their
// configuration to the retryConfig accumulator.
type RetryOption interface {
	applyRetryConfig(*retryConfig)
}

type eventConfig struct {
	operation      string
	eventType      EventType
	costConfidence CostConfidence
	pricingSource  PricingSource
	pricingVersion string
	details        map[string]interface{}
}

// eventOptionFunc adapts a plain func to the EventOption interface so existing
// option constructors can be expressed compactly.
type eventOptionFunc func(*eventConfig)

func (f eventOptionFunc) applyEventConfig(c *eventConfig) { f(c) }

// llmCallOptionFunc adapts a plain func to the LLMCallOption interface.
type llmCallOptionFunc func(*llmCallConfig)

func (f llmCallOptionFunc) applyLLMCallConfig(c *llmCallConfig) { f(c) }

// retryOptionFunc adapts a plain func to the RetryOption interface.
type retryOptionFunc func(*retryConfig)

func (f retryOptionFunc) applyRetryConfig(c *retryConfig) { f(c) }

// costConfidenceOption is a dual-target option: it implements both EventOption
// and LLMCallOption so callers can pin cost_confidence on either RecordCost
// or RecordLLMCall (e.g. failure events that must remain Unknown regardless
// of pricing-registry state).
type costConfidenceOption struct{ value CostConfidence }

func (o costConfidenceOption) applyEventConfig(c *eventConfig)     { c.costConfidence = o.value }
func (o costConfidenceOption) applyLLMCallConfig(c *llmCallConfig) { c.costConfidence = o.value }

// pricingSourceOption is a dual-target option for pricing_source.
type pricingSourceOption struct{ value PricingSource }

func (o pricingSourceOption) applyEventConfig(c *eventConfig)     { c.pricingSource = o.value }
func (o pricingSourceOption) applyLLMCallConfig(c *llmCallConfig) { c.pricingSource = o.value }

// pricingVersionOption is a dual-target option for pricing_version.
type pricingVersionOption struct{ value string }

func (o pricingVersionOption) applyEventConfig(c *eventConfig)     { c.pricingVersion = o.value }
func (o pricingVersionOption) applyLLMCallConfig(c *llmCallConfig) { c.pricingVersion = o.value }

// detailsOption is a dual-target option for arbitrary details map merging.
type detailsOption struct{ value map[string]interface{} }

func (o detailsOption) applyEventConfig(c *eventConfig)     { c.details = o.value }
func (o detailsOption) applyLLMCallConfig(c *llmCallConfig) { c.details = o.value }

// WithEventType sets the event type on the cost event.
func WithEventType(t EventType) EventOption {
	return eventOptionFunc(func(c *eventConfig) { c.eventType = t })
}

// WithOperation sets the operation name on the cost event.
func WithOperation(op string) EventOption {
	return eventOptionFunc(func(c *eventConfig) { c.operation = op })
}

// WithDetails attaches arbitrary key-value details. Works on either RecordCost
// (EventOption) or RecordLLMCall (LLMCallOption); keys are merged into
// event.Details so callers can annotate failure events with correlators
// (e.g. query_index) without re-importing the core package.
func WithDetails(d map[string]interface{}) detailsOption {
	return detailsOption{value: d}
}

// WithCostConfidence sets the cost_confidence on the event. Dual-target so
// callers can override the auto-derived value on RecordLLMCall (e.g. pin
// failure events to Unknown regardless of pricing-registry state).
func WithCostConfidence(cc CostConfidence) costConfidenceOption {
	return costConfidenceOption{value: cc}
}

// WithPricingSource sets the pricing_source on the event. Dual-target.
func WithPricingSource(ps PricingSource) pricingSourceOption {
	return pricingSourceOption{value: ps}
}

// WithPricingVersion sets the pricing_version on the event. Dual-target.
func WithPricingVersion(pv string) pricingVersionOption {
	return pricingVersionOption{value: pv}
}

type llmCallConfig struct {
	costUSD             *decimal.Decimal
	cachedTokens        int
	cacheCreationTokens int
	latencyMs           int
	errorType           string
	costConfidence      CostConfidence
	pricingSource       PricingSource
	pricingVersion      string
	details             map[string]interface{}
}

// WithCost sets an explicit cost for the LLM call (skips auto-pricing).
func WithCost(cost decimal.Decimal) LLMCallOption {
	return llmCallOptionFunc(func(c *llmCallConfig) { c.costUSD = &cost })
}

// WithCachedTokens sets the cached (prompt-cache read) token count for the LLM call.
func WithCachedTokens(n int) LLMCallOption {
	return llmCallOptionFunc(func(c *llmCallConfig) { c.cachedTokens = n })
}

// WithCacheCreationTokens sets the prompt-cache *write* token count
// (Anthropic-specific). These are charged at the model's
// cache_creation_input_token_cost rate during auto-pricing.
func WithCacheCreationTokens(n int) LLMCallOption {
	return llmCallOptionFunc(func(c *llmCallConfig) { c.cacheCreationTokens = n })
}

// WithLatency sets the latency in milliseconds for the LLM call.
func WithLatency(ms int) LLMCallOption {
	return llmCallOptionFunc(func(c *llmCallConfig) { c.latencyMs = ms })
}

// WithErrorType sets the error classification for the LLM call.
func WithErrorType(t string) LLMCallOption {
	return llmCallOptionFunc(func(c *llmCallConfig) { c.errorType = t })
}

type retryConfig struct {
	retryOf *uuid.UUID
	costUSD *decimal.Decimal
}

// WithRetryOf sets the event ID this retry is retrying.
func WithRetryOf(id uuid.UUID) RetryOption {
	return retryOptionFunc(func(c *retryConfig) { c.retryOf = &id })
}

// WithRetryCost sets an explicit cost for the retry.
func WithRetryCost(cost decimal.Decimal) RetryOption {
	return retryOptionFunc(func(c *retryConfig) { c.costUSD = &cost })
}

// NewEventWithOptions creates an Event with EventOption overrides applied.
// It is used by the top-level convenience API when a TrackedTask wrapper
// is not available in context.
func NewEventWithOptions(taskID uuid.UUID, defaultType EventType, opts ...EventOption) Event {
	ecfg := &eventConfig{}
	for _, o := range opts {
		o.applyEventConfig(ecfg)
	}

	evType := defaultType
	if ecfg.eventType != "" {
		evType = ecfg.eventType
	}
	event := NewEvent(taskID, evType)

	if ecfg.costConfidence != "" {
		event.CostConfidence = ecfg.costConfidence
	} else {
		event.CostConfidence = CostConfidenceExact
	}
	if ecfg.pricingSource != "" {
		event.PricingSource = ecfg.pricingSource
	} else {
		event.PricingSource = PricingSourceManual
	}
	event.PricingVersion = ecfg.pricingVersion
	if ecfg.operation != "" {
		event.Details["operation"] = ecfg.operation
	}
	for k, v := range ecfg.details {
		event.Details[k] = v
	}

	return event
}
