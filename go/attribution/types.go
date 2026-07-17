// Package attribution defines the public attribution-v2 wire contract and
// converts durable SDK capture records into the strict ingestion shape.
package attribution

const ContractVersion = "2.0.0"

type Component string

const (
	ComponentLLM               Component = "llm"
	ComponentTelephony         Component = "telephony"
	ComponentVoicePlatform     Component = "voice_platform"
	ComponentSpeechToText      Component = "speech_to_text"
	ComponentTextToSpeech      Component = "text_to_speech"
	ComponentRealtimeTransport Component = "realtime_transport"
	ComponentRecording         Component = "recording"
	ComponentPostCallAnalysis  Component = "post_call_analysis"
	ComponentCompute           Component = "compute"
	ComponentGPU               Component = "gpu"
	ComponentNetwork           Component = "network"
	ComponentStorage           Component = "storage"
	ComponentExternal          Component = "external"
)

type UsageMetric string
type UsageUnit string

const (
	MetricInputTokens      UsageMetric = "input_tokens"
	MetricOutputTokens     UsageMetric = "output_tokens"
	MetricCacheReadTokens  UsageMetric = "cache_read_input_tokens"
	MetricCacheWriteTokens UsageMetric = "cache_write_input_tokens"
	MetricReasoningTokens  UsageMetric = "reasoning_output_tokens"
	MetricCharacters       UsageMetric = "characters"
	MetricAudioSeconds     UsageMetric = "audio_seconds"
	MetricConnectedSeconds UsageMetric = "connected_seconds"
	MetricRecordingSeconds UsageMetric = "recording_seconds"
	MetricAgentSeconds     UsageMetric = "agent_seconds"
	MetricComputeSeconds   UsageMetric = "compute_seconds"
	MetricVCPUSeconds      UsageMetric = "vcpu_seconds"
	MetricMemoryGiBSeconds UsageMetric = "memory_gib_seconds"
	MetricGPUSeconds       UsageMetric = "gpu_seconds"
	MetricRequestCount     UsageMetric = "request_count"
	MetricCallCount        UsageMetric = "call_count"
	MetricBytesIn          UsageMetric = "bytes_in"
	MetricBytesOut         UsageMetric = "bytes_out"
	MetricImageCount       UsageMetric = "image_count"
	MetricPageCount        UsageMetric = "page_count"
	MetricCreditCount      UsageMetric = "credit_count"

	UnitTokens      UsageUnit = "Tokens"
	UnitCharacters  UsageUnit = "Characters"
	UnitSeconds     UsageUnit = "Seconds"
	UnitVCPUSeconds UsageUnit = "vCPU-Seconds"
	UnitGiBSeconds  UsageUnit = "GiB-Seconds"
	UnitGPUSeconds  UsageUnit = "GPU-Seconds"
	UnitRequests    UsageUnit = "Requests"
	UnitCalls       UsageUnit = "Calls"
	UnitBytes       UsageUnit = "Bytes"
	UnitImages      UsageUnit = "Images"
	UnitPages       UsageUnit = "Pages"
	UnitCredits     UsageUnit = "Credits"
)

var UnitByMetric = map[UsageMetric]UsageUnit{
	MetricInputTokens: UnitTokens, MetricOutputTokens: UnitTokens,
	MetricCacheReadTokens: UnitTokens, MetricCacheWriteTokens: UnitTokens,
	MetricReasoningTokens: UnitTokens, MetricCharacters: UnitCharacters,
	MetricAudioSeconds: UnitSeconds, MetricConnectedSeconds: UnitSeconds,
	MetricRecordingSeconds: UnitSeconds, MetricAgentSeconds: UnitSeconds,
	MetricComputeSeconds: UnitSeconds, MetricVCPUSeconds: UnitVCPUSeconds,
	MetricMemoryGiBSeconds: UnitGiBSeconds, MetricGPUSeconds: UnitGPUSeconds,
	MetricRequestCount: UnitRequests, MetricCallCount: UnitCalls,
	MetricBytesIn: UnitBytes, MetricBytesOut: UnitBytes,
	MetricImageCount: UnitImages, MetricPageCount: UnitPages,
	MetricCreditCount: UnitCredits,
}

type UsageLineV2 struct {
	Metric   UsageMetric `json:"metric"`
	Quantity string      `json:"quantity"`
	Unit     UsageUnit   `json:"unit"`
}

type ProviderIdentityV2 struct {
	Name     string `json:"name"`
	Service  string `json:"service"`
	RecordID string `json:"record_id,omitempty"`
	Region   string `json:"region,omitempty"`
}

type ResourceV2 struct {
	Type string `json:"type"`
	ID   string `json:"id"`
}

type CostEvidenceV2 struct {
	Amount         string `json:"amount"`
	Currency       string `json:"currency"`
	Source         string `json:"source"`
	Confidence     string `json:"confidence"`
	PricingVersion string `json:"pricing_version,omitempty"`
}

type LifecycleV2 struct {
	State    string `json:"state"`
	Revision int    `json:"revision"`
}

type UsagePeriodV2 struct {
	StartAt string `json:"start_at"`
	EndAt   string `json:"end_at,omitempty"`
}

type EventV2 struct {
	SchemaVersion string             `json:"schema_version"`
	EventID       string             `json:"event_id"`
	TaskID        string             `json:"task_id"`
	OccurredAt    string             `json:"occurred_at"`
	ObservedAt    string             `json:"observed_at"`
	Component     Component          `json:"component"`
	Provider      ProviderIdentityV2 `json:"provider"`
	Resource      *ResourceV2        `json:"resource,omitempty"`
	Lifecycle     LifecycleV2        `json:"lifecycle"`
	UsagePeriod   *UsagePeriodV2     `json:"usage_period,omitempty"`
	Usage         []UsageLineV2      `json:"usage"`
	CostEvidence  *CostEvidenceV2    `json:"cost_evidence,omitempty"`
	RetryOf       string             `json:"retry_of,omitempty"`
}

// TaskIngestV1 intentionally excludes aggregate costs and tokens. Those are
// derived from attribution cost lines by the control plane.
type TaskIngestV1 struct {
	TaskID        string                 `json:"task_id"`
	TaskType      string                 `json:"task_type"`
	Status        string                 `json:"status"`
	StartedAt     string                 `json:"started_at"`
	EndedAt       *string                `json:"ended_at"`
	Metadata      map[string]interface{} `json:"metadata"`
	CustomerID    *string                `json:"customer_id"`
	ProjectID     *string                `json:"project_id"`
	ParentTaskID  *string                `json:"parent_task_id"`
	ExperimentID  *string                `json:"experiment_id"`
	Variant       *string                `json:"variant"`
	SchemaVersion string                 `json:"schema_version"`
}
