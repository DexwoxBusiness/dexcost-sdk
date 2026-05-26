package core

import (
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

// canonicalTimeFormat is the wire-format pattern for occurred_at /
// started_at / ended_at fields. Sprint 3 Theme F / §4.1.1 (P1):
// RFC3339 with microsecond precision (6 fractional digits, zero-padded)
// and "Z" suffix, matching the Python canonical.
const canonicalTimeFormat = "2006-01-02T15:04:05.000000Z"

// formatCanonicalTime serialises a time.Time to the canonical wire
// format. Always UTC; nanoseconds are truncated to microseconds.
func formatCanonicalTime(t time.Time) string {
	return t.UTC().Format(canonicalTimeFormat)
}

// TaskStatus represents the lifecycle status of a tracked task.
type TaskStatus string

const (
	TaskStatusPending TaskStatus = "pending"
	TaskStatusRunning TaskStatus = "running"
	TaskStatusSuccess TaskStatus = "success"
	TaskStatusFailed  TaskStatus = "failed"
)

// EventType discriminates cost-generating events.
type EventType string

const (
	EventTypeLLMCall               EventType = "llm_call"
	EventTypeExternalCost          EventType = "external_cost"
	EventTypeComputeCost           EventType = "compute_cost"
	EventTypeRetryMarker           EventType = "retry_marker"
	EventTypeNetwork               EventType = "network"
	EventTypeGPUCost               EventType = "gpu_cost"
	EventTypeGPUUtilizationSignal  EventType = "gpu_utilization_signal"
)

// CostConfidence indicates how trustworthy the reported cost is.
type CostConfidence string

const (
	CostConfidenceExact     CostConfidence = "exact"
	CostConfidenceComputed  CostConfidence = "computed"
	CostConfidenceEstimated CostConfidence = "estimated"
	CostConfidenceUnknown   CostConfidence = "unknown"
)

// PricingSource indicates where the cost figure was derived from.
type PricingSource string

const (
	PricingSourceLiteLLM          PricingSource = "litellm"
	PricingSourceProviderResponse PricingSource = "provider_response"
	PricingSourceManual           PricingSource = "manual"
	PricingSourceCustom           PricingSource = "custom"
	PricingSourceRateRegistry     PricingSource = "rate_registry"
	PricingSourceUnknown          PricingSource = "unknown"
)

// Task represents a tracked business task (e.g. "resolve support ticket").
// All downstream events roll up into the aggregated cost and token fields.
type Task struct {
	TaskID       uuid.UUID              `json:"task_id"`
	TaskType     string                 `json:"task_type"`
	Status       TaskStatus             `json:"status"`
	StartedAt    time.Time              `json:"started_at"`
	EndedAt      *time.Time             `json:"ended_at,omitempty"`
	Metadata     map[string]interface{} `json:"metadata"`
	CustomerID   string                 `json:"customer_id,omitempty"`
	ProjectID    string                 `json:"project_id,omitempty"`
	ParentTaskID *uuid.UUID             `json:"parent_task_id,omitempty"`
	ExperimentID string                 `json:"experiment_id,omitempty"`
	Variant      string                 `json:"variant,omitempty"`

	// Aggregated costs (rolled up from child events)
	LLMCostUSD      decimal.Decimal `json:"llm_cost_usd"`
	ExternalCostUSD decimal.Decimal `json:"external_cost_usd"`
	ComputeCostUSD  decimal.Decimal `json:"compute_cost_usd"`
	// v2 cloud-egress cost, computed at task finalize from the
	// accountant's canonical external_bytes_out scalar. Distinct from
	// ExternalCostUSD (vendor API charges) — see Decision #7.
	NetworkCostUSD decimal.Decimal `json:"network_cost_usd"`
	// v2 GPU cost (Phase 2 — Decision #1/#3 capture). Computed at task
	// finalize from the per-task GpuAccountant's gpu_cost event back-fill.
	// Distinct from ComputeCostUSD (CPU/RAM) and ExternalCostUSD (vendor APIs).
	GpuCostUSD   decimal.Decimal `json:"gpu_cost_usd"`
	TotalCostUSD decimal.Decimal `json:"total_cost_usd"`

	// Token totals
	TotalInputTokens  int `json:"total_input_tokens"`
	TotalOutputTokens int `json:"total_output_tokens"`
	TotalCachedTokens int `json:"total_cached_tokens"`

	// Waste metrics
	RetryCount   int             `json:"retry_count"`
	RetryCostUSD decimal.Decimal `json:"retry_cost_usd"`
	FailureCount int             `json:"failure_count"`

	// Network capture (v1)
	NetworkBytesIn   int64                  `json:"network_bytes_in"`
	NetworkBytesOut  int64                  `json:"network_bytes_out"`
	NetworkCallCount int64                  `json:"network_call_count"`
	NetworkByHost    map[string]interface{} `json:"network_by_host"`

	// Schema contract
	SchemaVersion string `json:"schema_version"`
}

// NewTask creates a Task with sensible defaults and a new UUID.
func NewTask(taskType string) Task {
	return Task{
		TaskID:          uuid.New(),
		TaskType:        taskType,
		Status:          TaskStatusPending,
		StartedAt:       time.Now().UTC(),
		Metadata:        make(map[string]interface{}),
		LLMCostUSD:      decimal.Zero,
		ExternalCostUSD: decimal.Zero,
		ComputeCostUSD:  decimal.Zero,
		NetworkCostUSD:  decimal.Zero,
		GpuCostUSD:      decimal.Zero,
		TotalCostUSD:    decimal.Zero,
		RetryCostUSD:    decimal.Zero,
		NetworkByHost:   map[string]interface{}{"hosts": []interface{}{}},
		SchemaVersion:   "1",
	}
}

// ToDict serializes the Task to a map matching the Standard Event Schema v1 wire format.
// Costs are serialized as strings to preserve precision.
func (t Task) ToDict() map[string]interface{} {
	d := map[string]interface{}{
		"task_id":             t.TaskID.String(),
		"task_type":           t.TaskType,
		"status":              string(t.Status),
		"started_at":          formatCanonicalTime(t.StartedAt),
		"ended_at":            nil,
		"metadata":            t.Metadata,
		"customer_id":         nilIfEmpty(t.CustomerID),
		"project_id":          nilIfEmpty(t.ProjectID),
		"parent_task_id":      nil,
		"experiment_id":       nilIfEmpty(t.ExperimentID),
		"variant":             nilIfEmpty(t.Variant),
		"llm_cost_usd":        t.LLMCostUSD.String(),
		"external_cost_usd":   t.ExternalCostUSD.String(),
		"compute_cost_usd":    t.ComputeCostUSD.String(),
		"network_cost_usd":    t.NetworkCostUSD.String(),
		"gpu_cost_usd":        t.GpuCostUSD.String(),
		"total_cost_usd":      t.TotalCostUSD.String(),
		"total_input_tokens":  t.TotalInputTokens,
		"total_output_tokens": t.TotalOutputTokens,
		"total_cached_tokens": t.TotalCachedTokens,
		"retry_count":         t.RetryCount,
		"retry_cost_usd":      t.RetryCostUSD.String(),
		"failure_count":       t.FailureCount,
		"network_bytes_in":    t.NetworkBytesIn,
		"network_bytes_out":   t.NetworkBytesOut,
		"network_call_count":  t.NetworkCallCount,
		"network_by_host":     t.networkByHostForDict(),
		"schema_version":      t.SchemaVersion,
	}
	if t.EndedAt != nil {
		d["ended_at"] = formatCanonicalTime(*t.EndedAt)
	}
	if t.ParentTaskID != nil {
		d["parent_task_id"] = t.ParentTaskID.String()
	}
	return d
}

// Event represents a single cost event (LLM call, external API, compute, retry).
// Matches the Dexcost Standard Event Schema v1.
type Event struct {
	EventID    uuid.UUID     `json:"event_id"`
	TaskID     uuid.UUID     `json:"task_id"`
	EventType  EventType     `json:"event_type"`
	OccurredAt time.Time     `json:"occurred_at"`

	CostUSD        decimal.Decimal `json:"cost_usd"`
	CostConfidence CostConfidence  `json:"cost_confidence"`
	PricingSource  PricingSource   `json:"pricing_source,omitempty"`
	PricingVersion string          `json:"pricing_version,omitempty"`

	ServiceName string `json:"service_name,omitempty"`
	Provider    string `json:"provider,omitempty"`
	Model       string `json:"model,omitempty"`
	ErrorType   string `json:"error_type,omitempty"`

	InputTokens  *int `json:"input_tokens,omitempty"`
	OutputTokens *int `json:"output_tokens,omitempty"`
	CachedTokens *int `json:"cached_tokens,omitempty"`
	LatencyMs    *int `json:"latency_ms,omitempty"`

	IsRetry     bool       `json:"is_retry"`
	RetryReason string     `json:"retry_reason,omitempty"`
	RetryOf     *uuid.UUID `json:"retry_of,omitempty"`

	Details map[string]interface{} `json:"details"`

	SchemaVersion string `json:"schema_version"`
}

// NewEvent creates an Event with sensible defaults and a new UUID.
func NewEvent(taskID uuid.UUID, eventType EventType) Event {
	return Event{
		EventID:        uuid.New(),
		TaskID:         taskID,
		EventType:      eventType,
		OccurredAt:     time.Now().UTC(),
		CostUSD:        decimal.Zero,
		CostConfidence: CostConfidenceExact,
		Details:        make(map[string]interface{}),
		SchemaVersion:  "1",
	}
}

// ToDict serializes the Event to a map matching the Standard Event Schema v1 wire format.
func (e Event) ToDict() map[string]interface{} {
	d := map[string]interface{}{
		"event_id":        e.EventID.String(),
		"task_id":         e.TaskID.String(),
		"event_type":      string(e.EventType),
		"occurred_at":     formatCanonicalTime(e.OccurredAt),
		"cost_usd":        e.CostUSD.String(),
		"cost_confidence": string(e.CostConfidence),
		"pricing_source":  nilIfEmpty(string(e.PricingSource)),
		"pricing_version": nilIfEmpty(e.PricingVersion),
		"service_name":    nilIfEmpty(e.ServiceName),
		"provider":        nilIfEmpty(e.Provider),
		"model":           nilIfEmpty(e.Model),
		"error_type":      nilIfEmpty(e.ErrorType),
		"input_tokens":    e.InputTokens,
		"output_tokens":   e.OutputTokens,
		"cached_tokens":   e.CachedTokens,
		"latency_ms":      e.LatencyMs,
		"is_retry":        e.IsRetry,
		"retry_reason":    nilIfEmpty(e.RetryReason),
		"retry_of":        nil,
		"details":         e.Details,
		"schema_version":  e.SchemaVersion,
	}
	if e.RetryOf != nil {
		d["retry_of"] = e.RetryOf.String()
	}
	return d
}

// TaskToDictJSON returns the JSON bytes of TaskToDict.
func TaskToDictJSON(t Task) ([]byte, error) {
	return json.Marshal(t.ToDict())
}

// EventToDictJSON returns the JSON bytes of EventToDict.
func EventToDictJSON(e Event) ([]byte, error) {
	return json.Marshal(e.ToDict())
}

// parseDecimalOrZero parses a decimal string from a wire payload. On parse
// failure (corrupt input, forwards-incompat schema, partial write) logs a
// warning and returns Decimal.Zero rather than panicking via
// decimal.RequireFromString — Sprint 1 Theme B / §2.2.2 1c.
func parseDecimalOrZero(s, field string) decimal.Decimal {
	d, err := decimal.NewFromString(s)
	if err != nil {
		log.Printf("dexcost: failed to parse %s=%q as decimal, defaulting to 0: %v", field, s, err)
		return decimal.Zero
	}
	return d
}

// TaskFromDict deserializes a Task from a map matching the Standard Event Schema v1 wire format.
func TaskFromDict(d map[string]interface{}) (Task, error) {
	t := NewTask("")

	if v, ok := d["task_id"].(string); ok {
		id, err := uuid.Parse(v)
		if err != nil {
			return t, fmt.Errorf("task_id: %w", err)
		}
		t.TaskID = id
	}
	if v, ok := d["task_type"].(string); ok {
		t.TaskType = v
	}
	if v, ok := d["status"].(string); ok {
		t.Status = TaskStatus(v)
	}
	if v, ok := d["started_at"].(string); ok {
		parsed, err := time.Parse(time.RFC3339Nano, v)
		if err == nil {
			t.StartedAt = parsed
		}
	}
	if v, ok := d["ended_at"].(string); ok && v != "" {
		parsed, err := time.Parse(time.RFC3339Nano, v)
		if err == nil {
			t.EndedAt = &parsed
		}
	}
	if v, ok := d["metadata"].(map[string]interface{}); ok {
		t.Metadata = v
	}
	if v, ok := d["customer_id"].(string); ok {
		t.CustomerID = v
	}
	if v, ok := d["project_id"].(string); ok {
		t.ProjectID = v
	}
	if v, ok := d["parent_task_id"].(string); ok && v != "" {
		id, err := uuid.Parse(v)
		if err == nil {
			t.ParentTaskID = &id
		}
	}
	if v, ok := d["experiment_id"].(string); ok {
		t.ExperimentID = v
	}
	if v, ok := d["variant"].(string); ok {
		t.Variant = v
	}
	if v, ok := d["llm_cost_usd"].(string); ok {
		t.LLMCostUSD = parseDecimalOrZero(v, "llm_cost_usd")
	}
	if v, ok := d["external_cost_usd"].(string); ok {
		t.ExternalCostUSD = parseDecimalOrZero(v, "external_cost_usd")
	}
	if v, ok := d["compute_cost_usd"].(string); ok {
		t.ComputeCostUSD = parseDecimalOrZero(v, "compute_cost_usd")
	}
	if v, ok := d["network_cost_usd"].(string); ok {
		t.NetworkCostUSD = parseDecimalOrZero(v, "network_cost_usd")
	}
	if v, ok := d["gpu_cost_usd"].(string); ok {
		t.GpuCostUSD = parseDecimalOrZero(v, "gpu_cost_usd")
	}
	if v, ok := d["total_cost_usd"].(string); ok {
		t.TotalCostUSD = parseDecimalOrZero(v, "total_cost_usd")
	}
	if v := dictInt(d, "total_input_tokens"); v != nil {
		t.TotalInputTokens = *v
	}
	if v := dictInt(d, "total_output_tokens"); v != nil {
		t.TotalOutputTokens = *v
	}
	if v := dictInt(d, "total_cached_tokens"); v != nil {
		t.TotalCachedTokens = *v
	}
	if v := dictInt(d, "retry_count"); v != nil {
		t.RetryCount = *v
	}
	if v, ok := d["retry_cost_usd"].(string); ok {
		t.RetryCostUSD = parseDecimalOrZero(v, "retry_cost_usd")
	}
	if v := dictInt(d, "failure_count"); v != nil {
		t.FailureCount = *v
	}
	if v := dictInt64(d, "network_bytes_in"); v != nil {
		t.NetworkBytesIn = *v
	}
	if v := dictInt64(d, "network_bytes_out"); v != nil {
		t.NetworkBytesOut = *v
	}
	if v := dictInt64(d, "network_call_count"); v != nil {
		t.NetworkCallCount = *v
	}
	if v, ok := d["network_by_host"].(map[string]interface{}); ok && v != nil {
		t.NetworkByHost = v
	}
	if t.NetworkByHost == nil {
		t.NetworkByHost = map[string]interface{}{"hosts": []interface{}{}}
	}
	if v, ok := d["schema_version"].(string); ok {
		t.SchemaVersion = v
	}

	return t, nil
}

// networkByHostForDict returns the NetworkByHost map, defaulting to
// {"hosts": []} if the field is nil. Mirrors Python's
// `network_by_host=field(default_factory=lambda: {"hosts": []})`.
func (t Task) networkByHostForDict() map[string]interface{} {
	if t.NetworkByHost == nil {
		return map[string]interface{}{"hosts": []interface{}{}}
	}
	return t.NetworkByHost
}

// EventFromDict deserializes an Event from a map matching the Standard Event Schema v1 wire format.
func EventFromDict(d map[string]interface{}) (Event, error) {
	e := NewEvent(uuid.Nil, EventTypeLLMCall)

	if v, ok := d["event_id"].(string); ok {
		id, err := uuid.Parse(v)
		if err != nil {
			return e, fmt.Errorf("event_id: %w", err)
		}
		e.EventID = id
	}
	if v, ok := d["task_id"].(string); ok {
		id, err := uuid.Parse(v)
		if err != nil {
			return e, fmt.Errorf("task_id: %w", err)
		}
		e.TaskID = id
	}
	if v, ok := d["event_type"].(string); ok {
		e.EventType = EventType(v)
	}
	if v, ok := d["occurred_at"].(string); ok {
		parsed, err := time.Parse(time.RFC3339Nano, v)
		if err == nil {
			e.OccurredAt = parsed
		}
	}
	if v, ok := d["cost_usd"].(string); ok {
		e.CostUSD = parseDecimalOrZero(v, "cost_usd")
	}
	if v, ok := d["cost_confidence"].(string); ok {
		e.CostConfidence = CostConfidence(v)
	}
	if v, ok := d["pricing_source"].(string); ok {
		e.PricingSource = PricingSource(v)
	}
	if v, ok := d["pricing_version"].(string); ok {
		e.PricingVersion = v
	}
	if v, ok := d["service_name"].(string); ok {
		e.ServiceName = v
	}
	if v, ok := d["provider"].(string); ok {
		e.Provider = v
	}
	if v, ok := d["model"].(string); ok {
		e.Model = v
	}
	if v, ok := d["error_type"].(string); ok {
		e.ErrorType = v
	}
	if v := dictInt(d, "input_tokens"); v != nil {
		e.InputTokens = v
	}
	if v := dictInt(d, "output_tokens"); v != nil {
		e.OutputTokens = v
	}
	if v := dictInt(d, "cached_tokens"); v != nil {
		e.CachedTokens = v
	}
	if v := dictInt(d, "latency_ms"); v != nil {
		e.LatencyMs = v
	}
	if v, ok := d["is_retry"].(bool); ok {
		e.IsRetry = v
	}
	if v, ok := d["retry_reason"].(string); ok {
		e.RetryReason = v
	}
	if v, ok := d["retry_of"].(string); ok && v != "" {
		id, err := uuid.Parse(v)
		if err == nil {
			e.RetryOf = &id
		}
	}
	if v, ok := d["details"].(map[string]interface{}); ok {
		e.Details = v
	}
	if v, ok := d["schema_version"].(string); ok {
		e.SchemaVersion = v
	}

	return e, nil
}

func nilIfEmpty(s string) interface{} {
	if s == "" {
		return nil
	}
	return s
}

// dictInt extracts an int (or float64) value from a map and returns a pointer
// to the int. It handles both native Go int values (from ToDict) and float64
// values (from json.Unmarshal).
func dictInt(d map[string]interface{}, key string) *int {
	v, ok := d[key]
	if !ok {
		return nil
	}
	switch n := v.(type) {
	case int:
		return &n
	case int64:
		v := int(n)
		return &v
	case float64:
		v := int(n)
		return &v
	case *int:
		return n
	default:
		return nil
	}
}

// dictInt64 extracts an int64 (or other numeric) value from a map and returns
// a pointer to the int64. Handles native int/int64 values (from ToDict) and
// float64 values (from json.Unmarshal).
func dictInt64(d map[string]interface{}, key string) *int64 {
	v, ok := d[key]
	if !ok {
		return nil
	}
	switch n := v.(type) {
	case int64:
		return &n
	case int:
		v := int64(n)
		return &v
	case float64:
		v := int64(n)
		return &v
	case *int64:
		return n
	default:
		return nil
	}
}
