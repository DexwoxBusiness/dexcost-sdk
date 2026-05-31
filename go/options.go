package dexcost

import (
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// TaskStatus re-exports core.TaskStatus for the public API.
type TaskStatus = core.TaskStatus

// TaskStatus constants re-exported for convenience.
const (
	StatusPending = core.TaskStatusPending
	StatusRunning = core.TaskStatusRunning
	StatusSuccess = core.TaskStatusSuccess
	StatusFailed  = core.TaskStatusFailed
)

// EventType re-exports core.EventType for the public API.
type EventType = core.EventType

// EventType constants re-exported so callers using top-level WithEventType
// don't need to import core to express the value. Closes DEX-251c.1 Gap 5.
const (
	EventTypeLLMCall      = core.EventTypeLLMCall
	EventTypeExternalCost = core.EventTypeExternalCost
	EventTypeComputeCost  = core.EventTypeComputeCost
	EventTypeRetryMarker  = core.EventTypeRetryMarker
)

// CostConfidence re-exports core.CostConfidence so callers can express
// confidence values without importing the core package.
type CostConfidence = core.CostConfidence

// CostConfidence constants re-exported for convenience. Closes DEX-287
// Gap (top-level parity with the Standard Event Schema enums).
const (
	CostConfidenceExact     = core.CostConfidenceExact
	CostConfidenceComputed  = core.CostConfidenceComputed
	CostConfidenceEstimated = core.CostConfidenceEstimated
	CostConfidenceUnknown   = core.CostConfidenceUnknown
)

// PricingSource re-exports core.PricingSource for the public API.
type PricingSource = core.PricingSource

// PricingSource constants re-exported for convenience.
const (
	PricingSourceLiteLLM          = core.PricingSourceLiteLLM
	PricingSourceProviderResponse = core.PricingSourceProviderResponse
	PricingSourceManual           = core.PricingSourceManual
	PricingSourceCustom           = core.PricingSourceCustom
	PricingSourceRateRegistry     = core.PricingSourceRateRegistry
	PricingSourceUnknown          = core.PricingSourceUnknown
)

// Option types re-exported from core for use with TrackedTask methods.
type (
	EventOption   = core.EventOption
	LLMCallOption = core.LLMCallOption
	RetryOption   = core.RetryOption
)

// Re-exported option constructors not already exposed at the top level by
// dexcost.go. WithCost / WithLatency / WithOperation / WithRetryCost live in
// dexcost.go as wrapper functions; the rest are simple var aliases here.
var (
	WithEventType           = core.WithEventType
	WithCachedTokens        = core.WithCachedTokens
	WithCacheCreationTokens = core.WithCacheCreationTokens
	WithRetryOf             = core.WithRetryOf
	WithDetails             = core.WithDetails
	WithCostConfidence      = core.WithCostConfidence
	WithPricingSource       = core.WithPricingSource
	WithPricingVersion      = core.WithPricingVersion
)

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

// toTrackerOpts converts public TaskOptions to core.TaskOptions.
func toTrackerOpts(opts []TaskOption) []core.TaskOption {
	cfg := &taskConfig{}
	for _, o := range opts {
		o(cfg)
	}
	var coreOpts []core.TaskOption
	if cfg.customerID != "" {
		coreOpts = append(coreOpts, core.WithCustomer(cfg.customerID))
	}
	if cfg.projectID != "" {
		coreOpts = append(coreOpts, core.WithProject(cfg.projectID))
	}
	if cfg.metadata != nil {
		coreOpts = append(coreOpts, core.WithMetadata(cfg.metadata))
	}
	if cfg.experimentID != "" {
		coreOpts = append(coreOpts, core.WithExperiment(cfg.experimentID))
	}
	if cfg.variant != "" {
		coreOpts = append(coreOpts, core.WithVariant(cfg.variant))
	}
	return coreOpts
}
