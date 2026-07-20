package attribution

import (
	"encoding/json"
	"fmt"
	"log"
	"regexp"
	"strings"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/shopspring/decimal"
)

var nonCanonicalName = regexp.MustCompile(`[^a-z0-9._-]+`)
var edgePunctuation = regexp.MustCompile(`^[_\-.]+|[_\-.]+$`)
var gib = decimal.NewFromInt(1024).Pow(decimal.NewFromInt(3))

func canonicalTime(value time.Time) string { return value.UTC().Format("2006-01-02T15:04:05.000000Z") }

func decimalDetail(details map[string]interface{}, keys ...string) (decimal.Decimal, bool) {
	for _, key := range keys {
		value, ok := details[key]
		if !ok || value == nil {
			continue
		}
		if b, ok := value.(bool); ok && b {
			continue
		}
		var raw string
		switch v := value.(type) {
		case decimal.Decimal:
			return v, true
		case json.Number:
			raw = v.String()
		case string:
			raw = strings.TrimSpace(v)
		default:
			raw = fmt.Sprint(v)
		}
		parsed, err := decimal.NewFromString(raw)
		if err == nil {
			return parsed, true
		}
	}
	return decimal.Zero, false
}

func stringDetail(details map[string]interface{}, keys ...string) string {
	for _, key := range keys {
		if value, ok := details[key].(string); ok && strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func canonicalName(value, fallback string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	value = strings.TrimPrefix(strings.TrimPrefix(value, "https://"), "http://")
	value = nonCanonicalName.ReplaceAllString(value, "_")
	value = edgePunctuation.ReplaceAllString(value, "")
	if len(value) > 128 {
		value = value[:128]
	}
	if value == "" {
		return fallback
	}
	return value
}

func positiveQuantity(value decimal.Decimal) (string, bool) {
	if !value.IsPositive() {
		return "", false
	}
	if -value.Exponent() > 12 {
		value = value.Round(12)
	}
	if !value.IsPositive() {
		return "", false
	}
	return value.String(), true
}

func usageLine(metric UsageMetric, quantity decimal.Decimal) (UsageLineV2, bool) {
	normalized, ok := positiveQuantity(quantity)
	if !ok {
		return UsageLineV2{}, false
	}
	return UsageLineV2{Metric: metric, Quantity: normalized, Unit: UnitByMetric[metric]}, true
}

func appendUsage(lines []UsageLineV2, metric UsageMetric, quantity decimal.Decimal) []UsageLineV2 {
	if line, ok := usageLine(metric, quantity); ok {
		return append(lines, line)
	}
	return lines
}

func providerFor(event core.Event) ProviderIdentityV2 {
	raw := strings.ToLower(event.Provider)
	provider := ProviderIdentityV2{Name: canonicalName(event.Provider, "unknown"), Service: "api"}
	switch {
	case strings.Contains(raw, "openai"):
		provider.Name, provider.Service = "openai", "responses"
	case strings.Contains(raw, "anthropic"):
		provider.Name, provider.Service = "anthropic", "messages"
	case strings.Contains(raw, "bedrock"):
		provider.Name, provider.Service = "aws", "bedrock"
	case strings.Contains(raw, "gemini") || raw == "google":
		provider.Name, provider.Service = "google", "generate_content"
	case strings.Contains(raw, "cohere"):
		provider.Name, provider.Service = "cohere", "chat"
	case strings.Contains(raw, "vercel"):
		provider.Name, provider.Service = "vercel", "ai_sdk"
	case strings.Contains(raw, "langchain"):
		provider.Name, provider.Service = "langchain", "chat"
	}
	if event.EventType != core.EventTypeLLMCall {
		billing := stringDetail(event.Details, "billing_model")
		switch event.EventType {
		case core.EventTypeComputeCost:
			switch {
			case strings.HasPrefix(billing, "azure"):
				provider.Name = "azure"
			case billing == "gce" || billing == "cloud_functions" || strings.HasPrefix(billing, "cloud_"):
				provider.Name = "google_cloud"
			case billing == "vercel_fluid":
				provider.Name = "vercel"
			case billing == "k8s_pod":
				provider.Name = "kubernetes"
			case billing == "lambda" || billing == "fargate" || billing == "ec2":
				provider.Name = "aws"
			default:
				provider.Name = canonicalName(event.Provider, "runtime")
			}
			provider.Service = canonicalName(firstNonEmpty(billing, event.ServiceName), "compute")
		case core.EventTypeGPUCost:
			provider.Name = canonicalName(firstNonEmpty(stringDetail(event.Details, "cloud_provider"), event.Provider), "runtime")
			provider.Service = canonicalName(billing, "gpu")
		case core.EventTypeNetwork:
			provider.Name = canonicalName(firstNonEmpty(stringDetail(event.Details, "cloud_provider"), event.Provider), "internet")
			provider.Service = "egress"
		case core.EventTypeRetryMarker:
			provider.Name, provider.Service = "dexcost", "retry"
		default:
			service := firstNonEmpty(event.ServiceName, "external")
			if strings.HasPrefix(service, "mcp:") {
				provider.Name, provider.Service = "mcp", canonicalName(strings.TrimPrefix(service, "mcp:"), "tool")
			} else if strings.Contains(service, ".") {
				provider.Name, provider.Service = canonicalName(service, "external"), "http_api"
			} else {
				provider.Name, provider.Service = canonicalName(event.Provider, canonicalName(service, "external")), canonicalName(service, "api")
			}
		}
	}
	if recordID := stringDetail(event.Details, "provider_record_id", "request_id", "call_sid"); len(recordID) > 0 && len(recordID) <= 256 {
		provider.RecordID = recordID
	}
	if region := stringDetail(event.Details, "region", "cloud_region"); region != "" {
		provider.Region = canonicalName(region, "unknown")
	}
	return provider
}

func resourceFor(event core.Event) *ResourceV2 {
	if resourceType := stringDetail(event.Details, "attribution_resource_type"); resourceType != "" {
		if resourceID := stringDetail(event.Details, "attribution_resource_id"); resourceID != "" {
			switch resourceType {
			case "model", "sku", "instance", "endpoint", "session", "other":
				return &ResourceV2{Type: resourceType, ID: truncate(resourceID, 256)}
			}
		}
	}
	// Retry markers may also carry model data copied from the failed call.
	// Keep the retry reason as the marker's identity instead of allowing the
	// generic model branch to hide it.
	if event.EventType == core.EventTypeRetryMarker {
		if reason := strings.TrimSpace(event.RetryReason); reason != "" {
			return &ResourceV2{Type: "other", ID: truncate(reason, 256)}
		}
	}
	if event.Model != "" {
		return &ResourceV2{Type: "model", ID: truncate(event.Model, 256)}
	}
	if event.EventType == core.EventTypeGPUCost {
		if sku := stringDetail(event.Details, "gpu_sku", "instance_type"); sku != "" {
			return &ResourceV2{Type: "sku", ID: truncate(sku, 256)}
		}
	}
	if event.EventType == core.EventTypeComputeCost {
		if instance := stringDetail(event.Details, "instance_type", "architecture"); instance != "" {
			return &ResourceV2{Type: "instance", ID: truncate(instance, 256)}
		}
	}
	return nil
}

func evidenceFor(event core.Event) *CostEvidenceV2 {
	amount, ok := positiveQuantity(event.CostUSD)
	if !ok {
		return nil
	}
	if event.EventType == core.EventTypeRetryMarker {
		return &CostEvidenceV2{Amount: amount, Currency: "USD", Source: "manual", Confidence: "exact"}
	}
	source := string(event.PricingSource)
	if source == "" && event.EventType == core.EventTypeNetwork {
		source = stringDetail(event.Details, "egress_pricing_source")
	}
	if source == "provider_response" {
		confidence := "estimated"
		if event.CostConfidence == core.CostConfidenceExact {
			confidence = "exact"
		}
		return &CostEvidenceV2{Amount: amount, Currency: "USD", Source: "provider_reported", Confidence: confidence}
	}
	if source == "manual" || source == "custom" || source == "user_override" {
		return &CostEvidenceV2{Amount: amount, Currency: "USD", Source: "manual", Confidence: string(event.CostConfidence)}
	}
	mapped := ""
	if source == "rate_registry" {
		mapped = "sdk_rate_registry"
	}
	if source == "service_catalog" || source == "litellm" || source == "tokencost" || strings.HasPrefix(source, "compute_catalog:") || strings.HasPrefix(source, "gpu_catalog:") || strings.HasPrefix(source, "egress_catalog:") {
		mapped = "sdk_catalog"
	}
	if mapped == "" || event.PricingVersion == "" {
		return nil
	}
	confidence := string(event.CostConfidence)
	if confidence == "exact" {
		confidence = "computed"
	}
	return &CostEvidenceV2{Amount: amount, Currency: "USD", Source: mapped, Confidence: confidence, PricingVersion: event.PricingVersion}
}

func componentAndUsage(event core.Event) (Component, []UsageLineV2, decimal.Decimal, bool) {
	details := event.Details
	switch event.EventType {
	case core.EventTypeGPUUtilizationSignal:
		return "", nil, decimal.Zero, false
	case core.EventTypeRetryMarker:
		usage := appendUsage(nil, MetricRequestCount, decimal.NewFromInt(1))
		return ComponentExternal, usage, decimal.Zero, true
	case core.EventTypeLLMCall:
		usage := []UsageLineV2{}
		cached := decimal.NewFromInt(int64(intValue(event.CachedTokens)))
		input := decimal.NewFromInt(int64(intValue(event.InputTokens)))
		provider := strings.ToLower(event.Provider)
		if !(strings.Contains(provider, "anthropic") || strings.Contains(provider, "bedrock") || provider == "aws") {
			input = decimal.Max(decimal.Zero, input.Sub(cached))
		}
		cacheWrite, _ := decimalDetail(details, "cache_creation_input_tokens")
		reasoning, _ := decimalDetail(details, "reasoning_output_tokens", "reasoning_tokens")
		output := decimal.NewFromInt(int64(intValue(event.OutputTokens)))
		if reasoning.IsPositive() {
			output = decimal.Max(decimal.Zero, output.Sub(reasoning))
		}
		usage = appendUsage(usage, MetricInputTokens, input)
		usage = appendUsage(usage, MetricCacheReadTokens, cached)
		usage = appendUsage(usage, MetricCacheWriteTokens, cacheWrite)
		usage = appendUsage(usage, MetricOutputTokens, output)
		usage = appendUsage(usage, MetricReasoningTokens, reasoning)
		if len(usage) == 0 {
			usage = appendUsage(usage, MetricRequestCount, decimal.NewFromInt(1))
		}
		return ComponentLLM, usage, decimal.Zero, true
	case core.EventTypeComputeCost:
		durationMS, _ := decimalDetail(details, "duration_ms")
		duration := durationMS.Div(decimal.NewFromInt(1000))
		if duration.IsZero() {
			duration, _ = decimalDetail(details, "wall_clock_seconds")
		}
		memory, memoryOK := decimalDetail(details, "memory_bytes_limit", "memory_bytes_peak")
		usage := []UsageLineV2{}
		usage = appendUsage(usage, MetricComputeSeconds, duration)
		vcpu, _ := decimalDetail(details, "vcpu_seconds_used")
		usage = appendUsage(usage, MetricVCPUSeconds, vcpu)
		if memoryOK {
			usage = appendUsage(usage, MetricMemoryGiBSeconds, memory.Div(gib).Mul(duration))
		}
		invocations, _ := decimalDetail(details, "invocation_count")
		usage = appendUsage(usage, MetricRequestCount, invocations)
		return ComponentCompute, usage, duration, true
	case core.EventTypeGPUCost:
		durationMS, _ := decimalDetail(details, "duration_ms")
		duration := durationMS.Div(decimal.NewFromInt(1000))
		measured, measuredOK := decimalDetail(details, "gpu_seconds_used")
		count, countOK := decimalDetail(details, "gpu_count")
		if !countOK {
			count = decimal.NewFromInt(1)
		}
		billed := duration.Mul(count)
		if stringDetail(details, "billing_model") == "per_gpu_second_active" && measuredOK {
			billed = measured
		} else if billed.IsZero() && measuredOK {
			billed = measured
		}
		return ComponentGPU, appendUsage(nil, MetricGPUSeconds, billed), duration, true
	case core.EventTypeNetwork:
		out, _ := decimalDetail(details, "request_bytes")
		in, _ := decimalDetail(details, "response_bytes")
		usage := appendUsage(nil, MetricBytesOut, out)
		usage = appendUsage(usage, MetricBytesIn, in)
		return ComponentNetwork, usage, decimal.Zero, true
	case core.EventTypeExternalCost:
		quantity, ok := decimalDetail(details, "attribution_usage_quantity")
		if !ok {
			quantity = decimal.NewFromInt(1)
		}
		metric := MetricRequestCount
		explicit := stringDetail(details, "attribution_usage_metric")
		if _, ok := UnitByMetric[UsageMetric(explicit)]; ok {
			metric = UsageMetric(explicit)
		} else {
			per := canonicalName(stringDetail(details, "attribution_usage_per"), "request")
			switch {
			case strings.Contains(per, "page"):
				metric = MetricPageCount
			case strings.Contains(per, "credit"):
				metric = MetricCreditCount
			case strings.Contains(per, "image"):
				metric = MetricImageCount
			case strings.Contains(per, "call"):
				metric = MetricCallCount
			case strings.Contains(per, "character"):
				metric = MetricCharacters
			}
		}
		component := ComponentExternal
		switch stringDetail(details, "attribution_component") {
		case string(ComponentSpeechToText):
			component = ComponentSpeechToText
		case string(ComponentTextToSpeech):
			component = ComponentTextToSpeech
		}
		duration, _ := decimalDetail(details, "attribution_usage_duration_seconds")
		return component, appendUsage(nil, metric, quantity), duration, true
	default:
		return "", nil, decimal.Zero, false
	}
}

// ToEventV2 converts a durable SDK event into a strict, details-free v2 event.
// It returns nil for observability-only or unrepresentable records.
func ToEventV2(event core.Event) *EventV2 {
	component, usage, duration, ok := componentAndUsage(event)
	if !ok {
		return nil
	}
	if len(usage) == 0 {
		usage = appendUsage(usage, MetricRequestCount, decimal.NewFromInt(1))
	}
	occurred := canonicalTime(event.OccurredAt)
	converted := &EventV2{SchemaVersion: "2", EventID: event.EventID.String(), TaskID: event.TaskID.String(), OccurredAt: occurred, ObservedAt: occurred, Component: component, Provider: providerFor(event), Resource: resourceFor(event), Lifecycle: LifecycleV2{State: "final", Revision: 1}, Usage: usage, CostEvidence: evidenceFor(event)}
	if event.IsRetry && event.RetryOf != nil {
		converted.RetryOf = event.RetryOf.String()
	}
	hasTime := false
	for _, line := range usage {
		if strings.HasSuffix(string(line.Unit), "Seconds") {
			hasTime = true
			break
		}
	}
	if hasTime || duration.IsPositive() {
		offset := time.Duration(0)
		if duration.IsPositive() {
			micros := duration.Mul(decimal.NewFromInt(1_000_000)).Round(0).IntPart()
			offset = time.Duration(micros) * time.Microsecond
		}
		converted.UsagePeriod = &UsagePeriodV2{StartAt: canonicalTime(event.OccurredAt.Add(-offset)), EndAt: occurred}
	}
	validation := ValidateEventV2(converted)
	if !validation.Success {
		paths := make([]string, 0, len(validation.Issues))
		for _, issue := range validation.Issues {
			paths = append(paths, issue.Path)
		}
		log.Printf("[dexcost] event %s cannot be represented by attribution v2: %s", event.EventID, strings.Join(paths, ", "))
		return nil
	}
	return converted
}

func ToTaskIngestV1(task core.Task) TaskIngestV1 {
	result := TaskIngestV1{TaskID: task.TaskID.String(), TaskType: task.TaskType, Status: string(task.Status), StartedAt: canonicalTime(task.StartedAt), Metadata: task.Metadata, CustomerID: optionalString(task.CustomerID), ProjectID: optionalString(task.ProjectID), ExperimentID: optionalString(task.ExperimentID), Variant: optionalString(task.Variant), SchemaVersion: "1"}
	if task.EndedAt != nil {
		value := canonicalTime(*task.EndedAt)
		result.EndedAt = &value
	}
	if task.ParentTaskID != nil {
		value := task.ParentTaskID.String()
		result.ParentTaskID = &value
	}
	if result.Metadata == nil {
		result.Metadata = map[string]interface{}{}
	}
	return result
}

func optionalString(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}
func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
func truncate(value string, max int) string {
	if len(value) > max {
		return value[:max]
	}
	return value
}
func intValue(value *int) int {
	if value == nil {
		return 0
	}
	return *value
}
