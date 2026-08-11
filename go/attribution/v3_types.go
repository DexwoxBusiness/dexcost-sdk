package attribution

const ContractVersionV3 = "3.0.0"

// BillingDimensionValueV3 is a tagged scalar used by server pricing
// selectors. Value is string for string/integer/decimal dimensions and bool
// for boolean dimensions.
type BillingDimensionValueV3 struct {
	Type  string      `json:"type"`
	Value interface{} `json:"value"`
}

type BillingDimensionV3 struct {
	Key   string                  `json:"key"`
	Value BillingDimensionValueV3 `json:"value"`
}

type UsageLineV3 struct {
	LineID     string               `json:"line_id"`
	Metric     string               `json:"metric"`
	Quantity   string               `json:"quantity"`
	Unit       string               `json:"unit"`
	Dimensions []BillingDimensionV3 `json:"dimensions"`
}

type ProviderIdentityV3 = ProviderIdentityV2
type ResourceV3 = ResourceV2
type CostEvidenceV3 = CostEvidenceV2

type TraceIdentityV3 struct {
	TraceID string `json:"trace_id"`
	SpanID  string `json:"span_id"`
}

type AttemptIdentityV3 struct {
	ID      string `json:"id"`
	Number  int    `json:"number"`
	RetryOf string `json:"retry_of,omitempty"`
}

type OperationIdentityV3 struct {
	ID      string            `json:"id"`
	Name    string            `json:"name"`
	Status  string            `json:"status"`
	Attempt AttemptIdentityV3 `json:"attempt"`
	Trace   *TraceIdentityV3  `json:"trace,omitempty"`
}

type LifecycleV3 struct {
	State    string `json:"state"`
	Revision int    `json:"revision"`
}

type UsagePeriodV3 struct {
	StartAt string `json:"start_at"`
	EndAt   string `json:"end_at,omitempty"`
}

// ObservationV3 is the complete, details-free observation revision accepted
// by /v1/ingest. Usage is a full snapshot and local cost is evidence only.
type ObservationV3 struct {
	SchemaVersion string              `json:"schema_version"`
	EventID       string              `json:"event_id"`
	TaskID        string              `json:"task_id"`
	OccurredAt    string              `json:"occurred_at"`
	ObservedAt    string              `json:"observed_at"`
	Component     Component           `json:"component"`
	Provider      ProviderIdentityV3  `json:"provider"`
	Resource      *ResourceV3         `json:"resource,omitempty"`
	Operation     OperationIdentityV3 `json:"operation"`
	Lifecycle     LifecycleV3         `json:"lifecycle"`
	UsageSnapshot string              `json:"usage_snapshot"`
	UsagePeriod   *UsagePeriodV3      `json:"usage_period,omitempty"`
	Usage         []UsageLineV3       `json:"usage"`
	CostEvidence  *CostEvidenceV3     `json:"cost_evidence,omitempty"`
}

type EventV3 = ObservationV3
