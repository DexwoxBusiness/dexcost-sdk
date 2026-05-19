// Package dexcost provides the Go SDK for dexcost — an agent unit economics
// platform for tracking LLM costs, non-LLM service fees, and retry waste.
//
// Usage:
//
//	import "github.com/DexwoxBusiness/dexcost-go"
//
//	func main() {
//	    dexcost.Init(dexcost.Config{Storage: "local"})
//	    defer dexcost.Close()
//
//	    ctx, task := dexcost.StartTask(ctx, "resolve_ticket",
//	        dexcost.WithCustomer("acme"),
//	    )
//	    task.RecordLLMCall("openai", "gpt-4o", 1000, 500)
//	    task.End(dexcost.StatusSuccess)
//	}
package dexcost

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/adapters"
	"github.com/DexwoxBusiness/dexcost-go/clients"
	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/integrations"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/DexwoxBusiness/dexcost-go/schema"
	"github.com/DexwoxBusiness/dexcost-go/security"
	"github.com/DexwoxBusiness/dexcost-go/transport"
)

var (
	globalTracker *core.Tracker
	globalPusher  *transport.EventPusher
	globalConfig  *Config
	initOnce      sync.Once
	initErr       error
)

// errNoActiveTask is returned by context-scoped APIs when no task is active.
var errNoActiveTask = errors.New("dexcost: no active task in context")

// Init initializes the global dexcost SDK. It must be called before
// StartTask, EndTask, or RecordCost. Safe to call multiple times;
// only the first call takes effect.
func Init(cfg Config) error {
	initOnce.Do(func() {
		initErr = doInit(&cfg)
	})
	return initErr
}

func doInit(cfg *Config) error {
	if err := cfg.init(); err != nil {
		return err
	}
	globalConfig = cfg

	// Resolve buffer directory.
	bufDir := cfg.BufferDir
	if bufDir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return fmt.Errorf("resolve home dir: %w", err)
		}
		bufDir = filepath.Join(home, ".dexcost")
		if err := os.MkdirAll(bufDir, 0755); err != nil {
			return fmt.Errorf("create buffer dir: %w", err)
		}
	}
	dbPath := filepath.Join(bufDir, "dexcost.db")

	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		return fmt.Errorf("open buffer: %w", err)
	}

	pricingEngine, err := pricing.NewEngine()
	if err != nil {
		buf.Close()
		return fmt.Errorf("init pricing engine: %w", err)
	}

	tracker, err := core.NewTracker(core.TrackerOptions{
		Buffer:                  buf,
		Pricing:                 pricingEngine,
		Rates:                   pricing.NewRateRegistry(),
		EnableRetryHeuristics:   cfg.EnableRetryHeuristics,
		RetryHeuristicWindow:    cfg.RetryHeuristicWindow,
		RetryHeuristicThreshold: cfg.RetryHeuristicThreshold,
	})
	if err != nil {
		buf.Close()
		return err
	}
	globalTracker = tracker

	// Wire the HTTP adapter to durable storage so auto-captured external_cost
	// events reach SQLite and the sync pusher (not just the in-memory buffer).
	adapters.SetEventBuffer(buf)

	// Start pusher if in cloud mode and not in dev mode.
	if cfg.StorageMode() == "cloud" && !IsDevMode() {
		globalPusher = transport.NewEventPusher(transport.PusherOptions{
			Buffer:         buf,
			Endpoint:       cfg.resolvedEndpoint(),
			APIKey:         cfg.APIKey,
			BatchSize:      cfg.BatchSize,
			Interval:       time.Duration(cfg.FlushIntervalSeconds * float64(time.Second)),
			RedactFields:   cfg.RedactFields,
			HashCustomerID: cfg.HashCustomerID,
		})
	}

	wireHTTPAdapters(cfg)
	setupServiceCatalog(cfg)

	return nil
}

// wireHTTPAdapters registers the HTTP session-grouping resolver and, when
// cfg.TrackHTTP is set, enables process-wide HTTP cost tracking
// (Python parity: init(track_http=True) + SessionManager grouping).
func wireHTTPAdapters(cfg *Config) {
	// Anonymous HTTP calls roll up into one session task per attribution
	// identity. Python groups by thread; Go has no goroutine identity, so it
	// groups by customer/project/agent instead.
	adapters.SetSessionResolver(func(ctx context.Context, callType string) (uuid.UUID, bool) {
		tr := globalTracker
		if tr == nil {
			return uuid.Nil, false
		}
		task := SessionMgr().GetOrCreateSessionForIdentity(ctx, callType, tr.Buffer())
		if task == nil {
			return uuid.Nil, false
		}
		return task.TaskID, true
	})

	if cfg.TrackHTTP {
		adapters.EnableGlobalHTTPTracking()
	}
}

// setupServiceCatalog refreshes the service catalog from cfg.ServiceCatalogURL
// (when configured) and registers it with the HTTP adapter so auto-detected
// external costs use the remote entries (Python parity: __init__.py:181-183).
func setupServiceCatalog(cfg *Config) {
	if cfg.ServiceCatalogURL == "" {
		return
	}
	catalog, err := pricing.NewServiceCatalog()
	if err != nil {
		log.Printf("[dexcost] failed to load service catalog: %v", err)
		return
	}
	if refreshErr := catalog.RefreshFromURL(cfg.ServiceCatalogURL); refreshErr != nil {
		log.Printf("[dexcost] failed to refresh service catalog: %v", refreshErr)
	}
	adapters.SetServiceCatalog(catalog)
}

// mustTracker panics if Init has not been called.
func mustTracker() *core.Tracker {
	if globalTracker == nil {
		panic("dexcost: Init() must be called before using the SDK")
	}
	return globalTracker
}

// TrackedTask wraps core.TrackedTask for the public API.
type TrackedTask = core.TrackedTask

// StartTask begins tracking a new task and returns a derived context
// with the task attached. The parent task (if any) is linked automatically
// from the context.
func StartTask(ctx context.Context, taskType string, opts ...TaskOption) (context.Context, *TrackedTask) {
	tr := mustTracker()
	coreOpts := toTrackerOpts(opts)
	return tr.StartTask(ctx, taskType, coreOpts...)
}

// EndTask ends the task found in the given context.
// It is a convenience for getting the task from context and calling End.
func EndTask(ctx context.Context, status TaskStatus) error {
	tt := core.GetCurrentTrackedTask(ctx)
	if tt != nil {
		return tt.End(status)
	}
	// Fallback: if only a raw *Task is in context, update directly
	// (aggregation is skipped — this path is deprecated).
	task := core.GetCurrentTask(ctx)
	if task == nil {
		return errNoActiveTask
	}
	now := time.Now().UTC()
	task.EndedAt = &now
	task.Status = status
	return mustTracker().Buffer().UpdateTask(*task)
}

// RecordCost records a non-LLM cost on the current task in the context.
// Optional EventOption overrides (e.g. WithEventType, WithCostConfidence,
// WithPricingSource, WithPricingVersion) are passed through to the event.
func RecordCost(ctx context.Context, service string, operation string, costUSD decimal.Decimal, opts ...EventOption) error {
	task := core.GetCurrentTask(ctx)
	if task == nil {
		return errNoActiveTask
	}
	if operation != "" {
		opts = append([]EventOption{WithOperation(operation)}, opts...)
	}
	event := core.NewEventWithOptions(task.TaskID, core.EventTypeExternalCost, opts...)
	event.ServiceName = service
	event.CostUSD = costUSD
	return mustTracker().Buffer().InsertEvent(event)
}

// Flush forces all buffered events to be pushed immediately (blocking).
// No-op in local-only mode. Logs push errors so silent transport failures
// (e.g. auth errors or tenant mismatches) are observable.
func Flush() {
	if globalPusher != nil {
		if err := globalPusher.Flush(); err != nil {
			log.Printf("[dexcost] flush failed: %v", err)
		}
	}
}

// Close stops the background pusher, finalizes idle sessions, and releases
// resources. Should be called on application shutdown (e.g. via defer).
func Close() {
	// Finalize any remaining sessions before shutting down.
	if globalSessionManager != nil && globalTracker != nil {
		globalSessionManager.FinalizeIdleSessions(globalTracker.Buffer())
	}
	resetSessionManager()

	// Restore http.DefaultTransport if global HTTP tracking was enabled and
	// clear the adapter hooks (session resolver + storage buffer).
	adapters.DisableGlobalHTTPTracking()
	adapters.SetSessionResolver(nil)
	adapters.SetEventBuffer(nil)

	if globalPusher != nil {
		globalPusher.Flush() // Flush pending events before stopping
		globalPusher.Stop()
		globalPusher = nil
	}
	if globalTracker != nil {
		globalTracker.Close()
		globalTracker = nil
	}
	// Reset initOnce so Init can be called again (useful for tests).
	initOnce = sync.Once{}
	initErr = nil
	globalConfig = nil
	disableDevMode()
}

// Tracker returns the global tracker for advanced usage.
// Panics if Init has not been called.
func Tracker() *core.Tracker {
	return mustTracker()
}

// SetContext attaches customer and project attribution to the context without
// starting an explicit task. Adapters (e.g. TrackHTTP) will automatically
// create a task from this attribution when no explicit task is present.
//
// Example:
//
//	ctx = dexcost.SetContext(ctx, "acme", "chatbot")
//	resp, err := trackedClient.Do(req.WithContext(ctx))
func SetContext(ctx context.Context, customerID, projectID string) context.Context {
	return core.SetContext(ctx, &core.ContextData{
		CustomerID: customerID,
		ProjectID:  projectID,
	})
}

// WrapOpenAI creates a TrackedOpenAI wrapper around an OpenAI-compatible client.
// The wrapper automatically records LLM cost events for each chat completion.
// Panics if Init has not been called.
func WrapOpenAI(inner interface{}) *clients.TrackedOpenAI {
	tr := mustTracker()
	return clients.NewTrackedOpenAI(inner, tr, tr.Pricing())
}

// WrapAnthropic creates a TrackedAnthropic wrapper around an Anthropic-compatible
// client. The wrapper automatically records LLM cost events for each message.
// Panics if Init has not been called.
func WrapAnthropic(inner interface{}) *clients.TrackedAnthropic {
	tr := mustTracker()
	return clients.NewTrackedAnthropic(inner, tr, tr.Pricing())
}

// WrapGemini creates a TrackedGemini wrapper around a Google Gemini-compatible
// client. The wrapper automatically records LLM cost events for each generation.
// Panics if Init has not been called.
func WrapGemini(inner interface{}) *clients.TrackedGemini {
	tr := mustTracker()
	return clients.NewTrackedGemini(inner, tr, tr.Pricing())
}

// WrapBedrock creates a TrackedBedrock wrapper around an AWS Bedrock client.
// The wrapper automatically records LLM cost events for each invocation.
// Panics if Init has not been called.
func WrapBedrock(inner interface{}) *clients.TrackedBedrock {
	tr := mustTracker()
	return clients.NewTrackedBedrock(inner, tr, tr.Pricing())
}

// WrapCohere creates a TrackedCohere wrapper around a Cohere-compatible client.
// The wrapper automatically records LLM cost events for each generation.
// Panics if Init has not been called.
func WrapCohere(inner interface{}) *clients.TrackedCohere {
	tr := mustTracker()
	return clients.NewTrackedCohere(inner, tr, tr.Pricing())
}

// WrapGroq creates a TrackedGroq wrapper around a Groq-compatible client.
// The wrapper automatically records LLM cost events for each completion.
// Panics if Init has not been called.
func WrapGroq(inner interface{}) *clients.TrackedGroq {
	tr := mustTracker()
	return clients.NewTrackedGroq(inner, tr, tr.Pricing())
}

func init() {
	// Wire up the dev console log callback so tracked clients can log events
	// without importing the top-level dexcost package (which would create a
	// circular dependency).
	clients.SetDevLogFunc(LogEvent)
}

// Version is the current SDK version.
const Version = "0.1.0"

// ALL_SUPPORTED_INSTRUMENTS lists the providers/integrations the Go SDK can
// instrument. Each provider has a top-level Wrap* wrapper client; litellm is
// covered by RecordLiteLLM and langchain by DexcostCallbackHandler. (Differs
// from Python's list: Go adds "groq" and omits "mcp".)
var ALL_SUPPORTED_INSTRUMENTS = []string{
	"openai", "anthropic", "gemini", "bedrock", "cohere", "groq", "litellm", "langchain",
}

// --- Type aliases for parity with Python SDK ---

type Task = core.Task
type Event = core.Event
type DexcostContext = core.ContextData
type CostTracker = core.Tracker
type PricingEngine = pricing.Engine
type RateRegistry = pricing.RateRegistry
type RateEntry = pricing.RateEntry
type ServiceCatalog = pricing.ServiceCatalog
type CostResult = pricing.CostResult
type SyncWorker = transport.EventPusher
type DexcostCallbackHandler = integrations.DexcostCallbackHandler

// InvalidAPIKeyError is an alias for ErrInvalidAPIKey.
var InvalidAPIKeyError = ErrInvalidAPIKey

// --- Functional option re-exports for convenience ---

// WithCost sets an explicit cost for an LLM call (skips auto-pricing).
func WithCost(cost decimal.Decimal) core.LLMCallOption { return core.WithCost(cost) }

// WithLatency sets the latency in milliseconds for an LLM call.
func WithLatency(ms int) core.LLMCallOption { return core.WithLatency(ms) }

// WithErrorType sets the error classification for the LLM call.
func WithErrorType(t string) core.LLMCallOption { return core.WithErrorType(t) }

// WithOperation sets the operation name on a cost event.
func WithOperation(op string) core.EventOption { return core.WithOperation(op) }

// WithRetryCost sets an explicit cost for a retry marker.
func WithRetryCost(cost decimal.Decimal) core.RetryOption { return core.WithRetryCost(cost) }

// GetCurrentTask returns the active task from the context, or nil.
func GetCurrentTask(ctx context.Context) *core.Task {
	return core.GetCurrentTask(ctx)
}

// SetCurrentTask attaches a task to the context.
func SetCurrentTask(ctx context.Context, task *core.Task) context.Context {
	return core.WithTask(ctx, task)
}

// GetContext returns the DexcostContext from the context, or nil.
func GetContext(ctx context.Context) *core.ContextData {
	return core.GetContextData(ctx)
}

// ClearContext removes the DexcostContext from the context.
func ClearContext(ctx context.Context) context.Context {
	return core.ClearContext(ctx)
}

// SetContextWithMetadata attaches customer attribution and optional metadata
// and agent name to the context.
func SetContextWithMetadata(ctx context.Context, customerID, projectID, agent string, metadata map[string]interface{}) context.Context {
	return core.SetContext(ctx, &core.ContextData{
		CustomerID: customerID,
		ProjectID:  projectID,
		Agent:      agent,
		Metadata:   metadata,
	})
}

// LinkTrace attaches an external trace link to the current task in context.
func LinkTrace(ctx context.Context, provider, traceID string) error {
	task := core.GetCurrentTask(ctx)
	if task == nil {
		return errNoActiveTask
	}
	links, ok := task.Metadata["_trace_links"].([]interface{})
	if !ok {
		links = []interface{}{}
	}
	links = append(links, map[string]interface{}{
		"provider": provider,
		"trace_id": traceID,
	})
	task.Metadata["_trace_links"] = links
	return nil
}

// Validate checks a task or event payload against Schema v1.
func Validate(payload map[string]interface{}) []string {
	return schema.Validate(payload)
}

// EnforceMetadataLimit caps metadata size, trimming from the end if needed.
func EnforceMetadataLimit(details map[string]interface{}, maxBytes int) map[string]interface{} {
	return security.EnforceMetadataLimit(details, maxBytes)
}

// HashValue returns the SHA-256 hex digest of value.
func HashValue(value string) string {
	return security.HashValue(value)
}

// RedactDict returns a shallow copy of data with matching keys replaced by "[REDACTED]".
func RedactDict(data map[string]interface{}, fields []string) map[string]interface{} {
	return security.RedactMap(data, fields)
}

// TaskFromDict deserializes a Task from a Standard Event Schema v1 map
// (the inverse of Task.ToDict). Re-exported from core for top-level parity
// with Python's Task.from_dict.
func TaskFromDict(d map[string]interface{}) (Task, error) {
	return core.TaskFromDict(d)
}

// EventFromDict deserializes an Event from a Standard Event Schema v1 map
// (the inverse of Event.ToDict). Re-exported from core for top-level parity
// with Python's Event.from_dict.
func EventFromDict(d map[string]interface{}) (Event, error) {
	return core.EventFromDict(d)
}

// RecordLiteLLM records an LLM cost event from a LiteLLM-style response map
// against the active task in ctx. LiteLLM is a Python gateway library with no
// Go equivalent to wrap, so LiteLLM-routed costs are recorded via this helper
// (Python parity: instrument_litellm patches litellm.completion).
//
// The response map must contain "model" and a "usage" sub-map; an optional
// "_hidden_params.custom_llm_provider" overrides the provider prefix.
func RecordLiteLLM(ctx context.Context, response map[string]interface{}) (Event, error) {
	task := core.GetCurrentTask(ctx)
	if task == nil {
		return Event{}, errNoActiveTask
	}
	tr := mustTracker()
	return clients.RecordLiteLLMResponse(tr.Buffer(), tr.Pricing(), task.TaskID, response)
}
