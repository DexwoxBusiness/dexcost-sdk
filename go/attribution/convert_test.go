package attribution

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/shopspring/decimal"
)

func intPtr(value int) *int { return &value }

func usageQuantities(event *EventV2) map[UsageMetric]string {
	result := map[UsageMetric]string{}
	for _, line := range event.Usage {
		result[line.Metric] = line.Quantity
	}
	return result
}

func TestToEventV2PreservesDisjointAnthropicCacheUsage(t *testing.T) {
	event := core.NewEvent(core.NewTask("test").TaskID, core.EventTypeLLMCall)
	event.Provider, event.Model = "anthropic", "claude-sonnet-4-5"
	event.InputTokens, event.CachedTokens, event.OutputTokens = intPtr(100), intPtr(1000), intPtr(70)
	event.Details["reasoning_output_tokens"] = 20
	event.CostUSD, event.CostConfidence, event.PricingSource, event.PricingVersion = decimal.RequireFromString("0.01"), core.CostConfidenceComputed, core.PricingSourceLiteLLM, "llm:test"
	converted := ToEventV2(event)
	if converted == nil {
		t.Fatal("expected conversion")
	}
	usage := usageQuantities(converted)
	if usage[MetricInputTokens] != "100" || usage[MetricCacheReadTokens] != "1000" || usage[MetricOutputTokens] != "50" || usage[MetricReasoningTokens] != "20" {
		t.Fatalf("wrong disjoint usage: %+v", usage)
	}
	if converted.CostEvidence == nil || converted.CostEvidence.Source != "sdk_catalog" {
		t.Fatalf("missing catalog evidence: %+v", converted.CostEvidence)
	}
}

func TestToEventV2SubtractsIncludedOpenAICacheTokens(t *testing.T) {
	event := core.NewEvent(core.NewTask("test").TaskID, core.EventTypeLLMCall)
	event.Provider = "openai"
	event.InputTokens, event.CachedTokens = intPtr(1000), intPtr(300)
	converted := ToEventV2(event)
	if converted == nil {
		t.Fatal("expected conversion")
	}
	if got := usageQuantities(converted)[MetricInputTokens]; got != "700" {
		t.Fatalf("input tokens = %s", got)
	}
}

func TestToEventV2ClosesTimeBasedComputeAndGPUUsage(t *testing.T) {
	for _, test := range []struct {
		name      string
		eventType core.EventType
		details   map[string]interface{}
		metric    UsageMetric
	}{
		{"compute", core.EventTypeComputeCost, map[string]interface{}{"vcpu_seconds_used": 2.5}, MetricVCPUSeconds},
		{"gpu", core.EventTypeGPUCost, map[string]interface{}{"gpu_seconds_used": 3, "billing_model": "per_gpu_second_active"}, MetricGPUSeconds},
	} {
		t.Run(test.name, func(t *testing.T) {
			event := core.NewEvent(core.NewTask("test").TaskID, test.eventType)
			event.OccurredAt = time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
			event.Details = test.details
			converted := ToEventV2(event)
			if converted == nil {
				t.Fatal("expected conversion")
			}
			if usageQuantities(converted)[test.metric] == "" {
				t.Fatalf("missing %s", test.metric)
			}
			if converted.UsagePeriod == nil || converted.UsagePeriod.EndAt == "" {
				t.Fatal("time usage must have a closed period")
			}
		})
	}
}

func TestToEventV2PreservesRateRegistryQuantity(t *testing.T) {
	event := core.NewEvent(core.NewTask("test").TaskID, core.EventTypeExternalCost)
	event.ServiceName = "search"
	event.CostUSD = decimal.NewFromInt(5)
	event.CostConfidence = core.CostConfidenceComputed
	event.PricingSource = core.PricingSourceRateRegistry
	event.PricingVersion = "rates:test"
	event.Details["attribution_usage_quantity"] = 25
	event.Details["attribution_usage_per"] = "page"
	converted := ToEventV2(event)
	if converted == nil {
		t.Fatal("expected conversion")
	}
	if usageQuantities(converted)[MetricPageCount] != "25" {
		t.Fatalf("usage: %+v", converted.Usage)
	}
	if converted.CostEvidence == nil || converted.CostEvidence.Source != "sdk_rate_registry" {
		t.Fatalf("evidence: %+v", converted.CostEvidence)
	}
}

func TestToEventV2RetainsUserOverrideAsManualEvidence(t *testing.T) {
	event := core.NewEvent(core.NewTask("test").TaskID, core.EventTypeExternalCost)
	event.CostUSD = decimal.RequireFromString("0.05")
	event.CostConfidence = core.CostConfidenceComputed
	event.PricingSource = core.PricingSource("user_override")
	converted := ToEventV2(event)
	if converted == nil || converted.CostEvidence == nil || converted.CostEvidence.Source != "manual" {
		t.Fatalf("user override evidence was lost: %+v", converted)
	}
}

func TestToEventV2NetworkDirectionsAndCatalogEvidence(t *testing.T) {
	event := core.NewEvent(core.NewTask("test").TaskID, core.EventTypeNetwork)
	event.Details["request_bytes"] = 10
	event.Details["response_bytes"] = 20
	event.Details["egress_pricing_source"] = "egress_catalog:aws:us-east-1"
	event.CostUSD, event.CostConfidence, event.PricingVersion = decimal.RequireFromString("0.01"), core.CostConfidenceComputed, "egress:test"
	converted := ToEventV2(event)
	if converted == nil {
		t.Fatal("expected conversion")
	}
	usage := usageQuantities(converted)
	if usage[MetricBytesOut] != "10" || usage[MetricBytesIn] != "20" {
		t.Fatalf("directions: %+v", usage)
	}
	if converted.CostEvidence == nil || converted.CostEvidence.Source != "sdk_catalog" {
		t.Fatalf("evidence: %+v", converted.CostEvidence)
	}
}

func TestToTaskIngestV1ExcludesAggregateCosts(t *testing.T) {
	task := core.NewTask("support")
	task.TotalCostUSD = decimal.NewFromInt(99)
	raw, err := json.Marshal(ToTaskIngestV1(task))
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]interface{}
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"total_cost_usd", "llm_cost_usd", "external_cost_usd", "total_input_tokens"} {
		if _, ok := value[forbidden]; ok {
			t.Fatalf("task payload leaked %s", forbidden)
		}
	}
}

func TestToEventV2DropsDetailsAndObservabilityOnlySignals(t *testing.T) {
	event := core.NewEvent(core.NewTask("test").TaskID, core.EventTypeExternalCost)
	event.Details["secret"] = "nope"
	converted := ToEventV2(event)
	raw, _ := json.Marshal(converted)
	if json.Valid(raw) && string(raw) == "" {
		t.Fatal("unreachable")
	}
	var value map[string]interface{}
	_ = json.Unmarshal(raw, &value)
	if _, ok := value["details"]; ok {
		t.Fatal("details must not cross attribution boundary")
	}
	signal := core.NewEvent(event.TaskID, core.EventTypeGPUUtilizationSignal)
	if ToEventV2(signal) != nil {
		t.Fatal("utilization signal should not be billed")
	}
}
