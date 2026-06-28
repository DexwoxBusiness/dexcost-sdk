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
//
// It is a type alias for core.TaskOption (not a distinct named type) so that
// options produced by the constructors below interoperate directly with the
// middleware package, whose handlers accept ...core.TaskOption (e.g.
// GinMiddleware, EchoMiddleware, HTTPMiddleware). Defining a separate
// dexcost.TaskOption would make dexcost.WithCustomer("acme") a compile error
// when passed to those middlewares.
type TaskOption = core.TaskOption

// Task attribution option constructors, re-exported from core so callers
// don't need to import core directly. Because TaskOption aliases
// core.TaskOption, the values returned here are accepted everywhere a
// core.TaskOption is expected.
var (
	WithCustomer   = core.WithCustomer
	WithProject    = core.WithProject
	WithMetadata   = core.WithMetadata
	WithExperiment = core.WithExperiment
	WithVariant    = core.WithVariant
)
