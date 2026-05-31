// Serverless handler wraps for compute capture.
//
// Each wrap is a thin generic shim that:
//  1. Reads runtime-specific env vars (memory limit, init type, region).
//  2. Creates a core.ComputeAccountant and registers it for the active task.
//  3. Times the handler with time.Now() deltas.
//  4. Reads cgroup memory.peak at exit.
//  5. Builds the per-invocation compute_cost event with cost_pending:true
//     and persists it via the package-level event buffer hook.
//  6. tracker.aggregateCosts back-fills the dollar at task finalize.
//
// When no dexcost task is in context the wrap is a transparent pass-through
// — anonymous compute never creates orphan events (capture spec §6 case 2).
//
// Mirrors python/src/dexcost/compute_wrap.py.

package adapters

import (
	"context"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/shopspring/decimal"
	"github.com/google/uuid"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// EventBuffer is the minimal subset of core.Buffer that the compute wraps
// need — InsertEvent is enough. Package-level so the SDK can wire it once
// at init() and every wrap can find it.
type EventBuffer interface {
	InsertEvent(e core.Event) error
}

var (
	computeEventBuffer EventBuffer
)

// SetComputeEventBuffer wires the compute wraps to an event sink. Called
// from dexcost.Init(); test helpers swap it directly.
func SetComputeEventBuffer(b EventBuffer) {
	computeEventBuffer = b
}

// WrapLambdaHandler wraps an AWS Lambda handler — emits one compute_cost
// event per invocation with the env-declared memory limit, the wall-clock
// duration, the runtime architecture, and the initialization type.
//
// Go-idiomatic equivalent of Python's @wrap_lambda_handler decorator —
// generic in both event type T and result R.
func WrapLambdaHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return func(ctx context.Context, evt T) (R, error) {
		task := core.GetCurrentTask(ctx)
		if task == nil {
			return fn(ctx, evt)
		}
		mem := lambdaMemoryMB()
		initType := os.Getenv("AWS_LAMBDA_INITIALIZATION_TYPE")
		if initType == "" {
			initType = "on-demand"
		}
		region := os.Getenv("AWS_REGION")
		if region == "" {
			region = os.Getenv("AWS_DEFAULT_REGION")
		}
		accountant := core.NewComputeAccountant(
			core.RuntimeLambda,
			core.WithLambdaMemoryMB(mem),
			core.WithInitializationType(initType),
			core.WithRegion(region),
		)
		core.RegisterComputeAccountant(task.TaskID.String(), accountant)
		return timeAndCapture(ctx, accountant, task.TaskID, fn, evt)
	}
}

// WrapCloudRunHandler wraps a Cloud Run HTTP handler. Default billing
// model is request-based (estimated confidence); override via Config.
// ComputeBillingOverrides{"cloud_run":"instance"} for instance customers.
func WrapCloudRunHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return func(ctx context.Context, evt T) (R, error) {
		task := core.GetCurrentTask(ctx)
		if task == nil {
			return fn(ctx, evt)
		}
		accountant := core.NewComputeAccountant(
			core.RuntimeCloudRun,
			core.WithRegion(gcpRegionFromEnv()),
		)
		core.RegisterComputeAccountant(task.TaskID.String(), accountant)
		return timeAndCapture(ctx, accountant, task.TaskID, fn, evt)
	}
}

// WrapCloudFunctionsHandler wraps a Cloud Functions Gen2 handler.
func WrapCloudFunctionsHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return func(ctx context.Context, evt T) (R, error) {
		task := core.GetCurrentTask(ctx)
		if task == nil {
			return fn(ctx, evt)
		}
		accountant := core.NewComputeAccountant(
			core.RuntimeCloudFunctions,
			core.WithRegion(gcpRegionFromEnv()),
		)
		core.RegisterComputeAccountant(task.TaskID.String(), accountant)
		return timeAndCapture(ctx, accountant, task.TaskID, fn, evt)
	}
}

// WrapAzureFunctionsHandler wraps an Azure Functions handler.
func WrapAzureFunctionsHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return func(ctx context.Context, evt T) (R, error) {
		task := core.GetCurrentTask(ctx)
		if task == nil {
			return fn(ctx, evt)
		}
		accountant := core.NewComputeAccountant(
			core.RuntimeAzureFunctions,
			core.WithRegion(os.Getenv("REGION_NAME")),
		)
		core.RegisterComputeAccountant(task.TaskID.String(), accountant)
		return timeAndCapture(ctx, accountant, task.TaskID, fn, evt)
	}
}

// WrapVercelHandler wraps a Vercel Fluid handler.
func WrapVercelHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return func(ctx context.Context, evt T) (R, error) {
		task := core.GetCurrentTask(ctx)
		if task == nil {
			return fn(ctx, evt)
		}
		accountant := core.NewComputeAccountant(
			core.RuntimeVercel,
			core.WithRegion(os.Getenv("VERCEL_REGION")),
		)
		core.RegisterComputeAccountant(task.TaskID.String(), accountant)
		return timeAndCapture(ctx, accountant, task.TaskID, fn, evt)
	}
}

// timeAndCapture runs `fn(ctx, evt)` while measuring wall duration. On exit
// (including handler error) it builds the per-invocation event and persists
// it. Handler errors are re-raised AFTER the event is persisted (capture
// spec §6 case 7 — the cost is still incurred).
func timeAndCapture[T any, R any](
	ctx context.Context,
	accountant *core.ComputeAccountant,
	taskID uuid.UUID,
	fn func(context.Context, T) (R, error),
	evt T,
) (result R, retErr error) {
	t0 := time.Now()
	defer func() {
		durationMS := time.Since(t0).Milliseconds()
		peak, _ := core.ReadMemoryPeak()
		// Fail-silent — convention §9. A bug in event-build/persist must
		// NOT mask the handler's return value.
		func() {
			defer func() {
				if r := recover(); r != nil {
					log.Printf("[dexcost] compute_wrap panic during event build: %v", r)
				}
			}()
			details := accountant.BuildServerlessEvent(durationMS, peak)
			if details == nil {
				return
			}
			persistComputeEvent(taskID, details)
		}()
	}()
	return fn(ctx, evt)
}

// persistComputeEvent inserts a compute_cost event with cost_pending=true
// via the package-level buffer hook. Tracker.aggregateCosts back-fills
// cost_usd at task finalize.
func persistComputeEvent(taskID uuid.UUID, details map[string]any) {
	buf := computeEventBuffer
	if buf == nil {
		return
	}
	ev := core.NewEvent(taskID, core.EventTypeComputeCost)
	ev.CostUSD = decimal.Zero
	ev.CostConfidence = core.CostConfidenceUnknown
	ev.Details = details
	if err := buf.InsertEvent(ev); err != nil {
		log.Printf("[dexcost] compute_wrap failed to persist event: %v", err)
	}
}

// lambdaMemoryMB reads AWS_LAMBDA_FUNCTION_MEMORY_SIZE; defaults to 128 on
// missing or malformed values.
func lambdaMemoryMB() int {
	s := os.Getenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
	if s == "" {
		return 128
	}
	v, err := strconv.Atoi(s)
	if err != nil || v <= 0 {
		return 128
	}
	return v
}

// gcpRegionFromEnv — GCP region is not exposed via env vars on Cloud Run /
// Cloud Functions Gen2; it comes from cloud_detect's Phase 2 IMDS probe.
// Returns "" here; the pricing engine falls through to provider defaults.
func gcpRegionFromEnv() string {
	return ""
}
