// GPU finalize — Phase 2 v2 GPU pricing + per-event back-fill.
//
// Mirrors python tracker._finalize_gpu. Called from aggregateCosts via a
// Tier-5 fail-silent shell. Algorithm:
//
//  1. If a GpuAccountant is registered AND no gpu_cost event exists yet
//     for this task (long-running path with no in-flight wrap), call
//     SnapshotEndAndBuild now and persist the dual events.
//  2. Walk events; for each gpu_cost with details["cost_pending"]=true,
//     call gpu_pricing_engine.ResolveGPUCost and update the event
//     (set cost_usd, pricing_source, cost_confidence, pricing_version,
//     strip cost_pending).
//  3. Adjust Task.GpuCostUSD + Task.TotalCostUSD by the DELTA per back-
//     filled event.
//
// gpu_utilization_signal events are NEVER aggregated — convention §1
// carve-out for observability-only events.

package core

import (
	"log"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
)

// finalizeGPU back-fills gpu_cost events at task finalize.
func (tt *TrackedTask) finalizeGPU(events []Event) {
	accountant := UnregisterGpuAccountant(tt.Task.TaskID.String())

	// Step 1 — long-running snapshot: if an accountant is registered AND
	// no gpu_cost event has landed yet for this task, build one now.
	hasGpuCostEvent := false
	for _, ev := range events {
		if ev.EventType == EventTypeGPUCost {
			hasGpuCostEvent = true
			break
		}
	}
	if accountant != nil && !hasGpuCostEvent {
		durationMS := int64(0)
		if tt.Task.EndedAt != nil {
			durationMS = tt.Task.EndedAt.Sub(tt.Task.StartedAt).Milliseconds()
		} else {
			durationMS = time.Since(tt.Task.StartedAt).Milliseconds()
		}
		cost, signals := accountant.SnapshotEndAndBuild(durationMS)
		if cost != nil {
			ev := NewEvent(tt.Task.TaskID, EventTypeGPUCost)
			ev.CostUSD = decimal.Zero
			ev.CostConfidence = CostConfidenceUnknown
			ev.Details = cost
			if err := tt.tracker.buffer.InsertEvent(ev); err != nil {
				log.Printf("[dexcost] WARNING: failed to record gpu_cost event: %v", err)
			} else {
				events = append(events, ev)
			}
		}
		for _, sig := range signals {
			evSig := NewEvent(tt.Task.TaskID, EventTypeGPUUtilizationSignal)
			evSig.CostUSD = decimal.Zero
			evSig.CostConfidence = CostConfidenceExact
			evSig.Details = sig
			if err := tt.tracker.buffer.InsertEvent(evSig); err != nil {
				log.Printf("[dexcost] WARNING: failed to record gpu signal event: %v", err)
			}
		}
	}

	// Lazy-init the engine.
	if tt.tracker.gpuPricingEngine == nil {
		tt.tracker.gpuPricingEngine = pricing.NewGpuPricingEngine()
	}
	engine := tt.tracker.gpuPricingEngine
	pricingVersion := "gpu:" + engine.CatalogVersion()
	env := cloud.GetCloudEnv()

	// Step 2/3 — back-fill walk over gpu_cost events with cost_pending.
	// gpu_utilization_signal events are NEVER aggregated — convention §1.
	for _, ev := range events {
		if ev.EventType != EventTypeGPUCost {
			continue
		}
		pending, _ := ev.Details["cost_pending"].(bool)
		if !pending {
			continue
		}
		cost := engine.ResolveGPUCost(ev.Details, env, decimal.Zero)
		prev := ev.CostUSD
		ev.CostUSD = cost.CostUSD
		ev.CostConfidence = CostConfidence(cost.CostConfidence)
		ev.PricingSource = PricingSource(cost.PricingSource)
		ev.PricingVersion = pricingVersion
		delete(ev.Details, "cost_pending")
		if err := tt.tracker.buffer.UpdateEvent(ev); err != nil {
			log.Printf("[dexcost] WARNING: failed to back-fill gpu event %s: %v", ev.EventID, err)
			continue
		}
		delta := cost.CostUSD.Sub(prev)
		tt.Task.GpuCostUSD = tt.Task.GpuCostUSD.Add(delta)
		tt.Task.TotalCostUSD = tt.Task.TotalCostUSD.Add(delta)
	}
}
