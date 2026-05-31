// Serverless GPU handler wraps — Modal / RunPod / Replicate.
// Mirrors python/src/dexcost/gpu_wrap.py.
//
// Each wrap:
//  1. Looks up the active dexcost task in ctx; if absent, becomes a
//     transparent pass-through (capture spec §6 case 2).
//  2. Creates a core.GpuAccountant for the runtime + ambient CloudEnv.
//  3. Registers it in the global GpuAccountant registry so the tracker
//     finds it at task finalize.
//  4. Times the handler. On exit (including error), snapshots the end NVML
//     state and persists 1 gpu_cost event (cost_pending=true) + N
//     gpu_utilization_signal events. Handler errors are re-raised AFTER
//     events are persisted because the GPU-seconds were consumed regardless
//     (Modal et al. bill failed invocations the same as successes).
//
// Phase 1 generic-handler signature pattern carried forward verbatim.

package adapters

import (
	"context"
	"log"
	"time"

	"github.com/shopspring/decimal"
	"github.com/google/uuid"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// GPUEventBuffer is the minimal subset of core.Buffer that GPU wraps need.
type GPUEventBuffer interface {
	InsertEvent(e core.Event) error
}

var gpuEventBuffer GPUEventBuffer

// SetGPUEventBuffer wires the GPU wraps to an event sink. Called from
// dexcost.Init(); tests swap it directly.
func SetGPUEventBuffer(b GPUEventBuffer) { gpuEventBuffer = b }

// WrapModalGPUHandler instruments a Modal handler — emits 1 gpu_cost +
// N gpu_utilization_signal events around each invocation.
func WrapModalGPUHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return wrapGPU(core.GpuRuntimeModal, fn)
}

// WrapRunpodGPUHandler instruments a RunPod handler.
func WrapRunpodGPUHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return wrapGPU(core.GpuRuntimeRunpod, fn)
}

// WrapReplicateGPUHandler instruments a Replicate handler.
func WrapReplicateGPUHandler[T any, R any](fn func(context.Context, T) (R, error)) func(context.Context, T) (R, error) {
	return wrapGPU(core.GpuRuntimeReplicate, fn)
}

// wrapGPU is the generic per-runtime body shared by Modal / RunPod / Replicate.
func wrapGPU[T any, R any](
	runtime core.GpuRuntimeKind,
	fn func(context.Context, T) (R, error),
) func(context.Context, T) (R, error) {
	return func(ctx context.Context, evt T) (R, error) {
		task := core.GetCurrentTask(ctx)
		if task == nil {
			return fn(ctx, evt)
		}
		accountant := core.NewGpuAccountant(runtime, cloud.GetCloudEnv())
		core.RegisterGpuAccountant(task.TaskID.String(), accountant)
		return timeAndCaptureGPU(ctx, accountant, task.TaskID, fn, evt)
	}
}

// timeAndCaptureGPU runs fn while sampling NVML. On exit (including handler
// error) it persists the dual events. Handler errors propagate AFTER events
// are persisted — the GPU-seconds were consumed (capture spec §6 case 7).
func timeAndCaptureGPU[T any, R any](
	ctx context.Context,
	accountant *core.GpuAccountant,
	taskID uuid.UUID,
	fn func(context.Context, T) (R, error),
	evt T,
) (result R, retErr error) {
	accountant.SnapshotStart()
	t0 := time.Now()
	defer func() {
		duration := time.Since(t0).Milliseconds()
		// Fail-silent shell around event build + persist.
		func() {
			defer func() {
				if r := recover(); r != nil {
					log.Printf("[dexcost] gpu_wrap panic during event build: %v", r)
				}
			}()
			cost, signals := accountant.SnapshotEndAndBuild(duration)
			persistGPUEvents(taskID, cost, signals)
		}()
	}()
	return fn(ctx, evt)
}

// persistGPUEvents inserts the gpu_cost event (cost_pending=true) and the
// per-device gpu_utilization_signal events through the configured buffer.
// The pricing engine back-fills cost_usd at task finalize.
func persistGPUEvents(taskID uuid.UUID, cost map[string]any, signals []map[string]any) {
	buf := gpuEventBuffer
	if buf == nil {
		return
	}
	if cost != nil {
		ev := core.NewEvent(taskID, core.EventTypeGPUCost)
		ev.CostUSD = decimal.Zero
		ev.CostConfidence = core.CostConfidenceUnknown
		ev.Details = cost
		if err := buf.InsertEvent(ev); err != nil {
			log.Printf("[dexcost] gpu_wrap failed to persist gpu_cost: %v", err)
		}
	}
	for _, sig := range signals {
		ev := core.NewEvent(taskID, core.EventTypeGPUUtilizationSignal)
		ev.CostUSD = decimal.Zero
		ev.CostConfidence = core.CostConfidenceExact
		ev.Details = sig
		if err := buf.InsertEvent(ev); err != nil {
			log.Printf("[dexcost] gpu_wrap failed to persist gpu signal: %v", err)
		}
	}
}
