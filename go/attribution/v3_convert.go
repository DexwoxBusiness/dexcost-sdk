package attribution

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/shopspring/decimal"
)

var (
	v3UUIDPattern = regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	v3UnitPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._{}/*^+\-]{0,63}$`)
	v3TraceID     = regexp.MustCompile(`^[0-9a-f]{32}$`)
	v3SpanID      = regexp.MustCompile(`^[0-9a-f]{16}$`)
	v3Integer     = regexp.MustCompile(`^-?(?:0|[1-9]\d{0,25})$`)
	v3Decimal     = regexp.MustCompile(`^-?(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$`)
)

var v3Components = map[string]Component{
	string(ComponentLLM):               ComponentLLM,
	string(ComponentTelephony):         ComponentTelephony,
	string(ComponentVoicePlatform):     ComponentVoicePlatform,
	string(ComponentSpeechToText):      ComponentSpeechToText,
	string(ComponentTextToSpeech):      ComponentTextToSpeech,
	string(ComponentRealtimeTransport): ComponentRealtimeTransport,
	string(ComponentRecording):         ComponentRecording,
	string(ComponentPostCallAnalysis):  ComponentPostCallAnalysis,
	string(ComponentCompute):           ComponentCompute,
	string(ComponentGPU):               ComponentGPU,
	string(ComponentNetwork):           ComponentNetwork,
	string(ComponentStorage):           ComponentStorage,
	string(ComponentExternal):          ComponentExternal,
}

type v3MappedUsage struct {
	component       Component
	usage           []UsageLineV2
	durationSeconds decimal.Decimal
	dimensions      []BillingDimensionV3
}

func canonicalV3DimensionsJSON(dimensions []BillingDimensionV3) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(dimensions); err != nil {
		return nil, err
	}
	encoded := bytes.TrimSuffix(buffer.Bytes(), []byte{'\n'})
	encoded = bytes.ReplaceAll(encoded, []byte(`\u2028`), []byte("\u2028"))
	encoded = bytes.ReplaceAll(encoded, []byte(`\u2029`), []byte("\u2029"))
	return encoded, nil
}

func deterministicV3UUID(namespace string, parts ...string) string {
	hash := sha256.New()
	values := append([]string{namespace}, parts...)
	for index, value := range values {
		if index > 0 {
			hash.Write([]byte{0})
		}
		hash.Write([]byte(value))
	}
	bytes := hash.Sum(nil)[:16]
	bytes[6] = (bytes[6] & 0x0f) | 0x50
	bytes[8] = (bytes[8] & 0x3f) | 0x80
	hexValue := hex.EncodeToString(bytes)
	return fmt.Sprintf("%s-%s-%s-%s-%s", hexValue[:8], hexValue[8:12], hexValue[12:16], hexValue[16:20], hexValue[20:])
}

func v3PositiveQuantity(value interface{}) (string, bool) {
	details := map[string]interface{}{"value": value}
	parsed, ok := decimalDetail(details, "value")
	if !ok {
		return "", false
	}
	return positiveQuantity(parsed)
}

func parseV3DimensionValue(value interface{}) (BillingDimensionValueV3, bool) {
	candidate, ok := value.(map[string]interface{})
	if !ok {
		return BillingDimensionValueV3{}, false
	}
	kind, _ := candidate["type"].(string)
	switch kind {
	case "string":
		text, ok := candidate["value"].(string)
		return BillingDimensionValueV3{Type: kind, Value: text}, ok && len(text) > 0 && len(text) <= 256
	case "boolean":
		boolean, ok := candidate["value"].(bool)
		return BillingDimensionValueV3{Type: kind, Value: boolean}, ok
	case "integer", "decimal":
		text, ok := candidate["value"].(string)
		valid := v3Integer.MatchString(text)
		if kind == "decimal" {
			valid = v3Decimal.MatchString(text)
		}
		return BillingDimensionValueV3{Type: kind, Value: text}, ok && valid
	default:
		return BillingDimensionValueV3{}, false
	}
}

func explicitV3Dimensions(details map[string]interface{}) ([]BillingDimensionV3, bool) {
	raw, exists := details["attribution_dimensions"]
	if !exists {
		return []BillingDimensionV3{}, true
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, false
	}
	var candidates []map[string]interface{}
	decoder := json.NewDecoder(strings.NewReader(string(encoded)))
	decoder.UseNumber()
	if err := decoder.Decode(&candidates); err != nil || len(candidates) > 24 {
		return nil, false
	}
	dimensions := make([]BillingDimensionV3, 0, len(candidates))
	for _, candidate := range candidates {
		key, ok := candidate["key"].(string)
		if !ok || !canonicalPattern.MatchString(key) {
			return nil, false
		}
		value, ok := parseV3DimensionValue(candidate["value"])
		if !ok {
			return nil, false
		}
		dimensions = append(dimensions, BillingDimensionV3{Key: key, Value: value})
	}
	sort.Slice(dimensions, func(i, j int) bool { return dimensions[i].Key < dimensions[j].Key })
	return dimensions, true
}

func gpuSignalV3Usage(event core.Event) v3MappedUsage {
	type candidate struct{ metric, key, unit string }
	candidates := []candidate{
		{"gpu.sm_utilization_percent", "sm_util_pct", "Percent"},
		{"gpu.memory_utilization_percent", "mem_util_pct", "Percent"},
		{"gpu.vram_peak_bytes", "vram_used_peak_bytes", "Bytes"},
		{"gpu.vram_capacity_bytes", "vram_total_bytes", "Bytes"},
		{"gpu.process_count", "process_count", "Processes"},
		{"gpu.sample_count", "sample_count", "Samples"},
	}
	usage := make([]UsageLineV2, 0, len(candidates))
	for _, item := range candidates {
		quantity, ok := v3PositiveQuantity(event.Details[item.key])
		if ok {
			usage = append(usage, UsageLineV2{Metric: UsageMetric(item.metric), Quantity: quantity, Unit: UsageUnit(item.unit)})
		}
	}
	dimensions := make([]BillingDimensionV3, 0, 2)
	if gpuIndex, ok := decimalDetail(event.Details, "gpu_index"); ok && !gpuIndex.IsNegative() && gpuIndex.Equal(gpuIndex.Truncate(0)) {
		dimensions = append(dimensions, BillingDimensionV3{Key: "gpu_index", Value: BillingDimensionValueV3{Type: "integer", Value: gpuIndex.String()}})
	}
	if sku := strings.TrimSpace(stringDetail(event.Details, "gpu_sku")); sku != "" {
		dimensions = append(dimensions, BillingDimensionV3{Key: "gpu_sku", Value: BillingDimensionValueV3{Type: "string", Value: truncate(sku, 256)}})
	}
	duration := decimal.Zero
	if durationMS, ok := decimalDetail(event.Details, "task_duration_ms"); ok && durationMS.IsPositive() {
		duration = durationMS.Div(decimal.NewFromInt(1_000))
	}
	return v3MappedUsage{component: ComponentGPU, usage: usage, durationSeconds: duration, dimensions: dimensions}
}

func unknownExplicitV3Usage(event core.Event) (v3MappedUsage, bool) {
	if event.EventType != core.EventTypeExternalCost {
		return v3MappedUsage{}, false
	}
	metric := strings.TrimSpace(stringDetail(event.Details, "attribution_usage_metric"))
	if metric == "" || !canonicalPattern.MatchString(metric) {
		return v3MappedUsage{}, false
	}
	if _, known := UnitByMetric[UsageMetric(metric)]; known {
		return v3MappedUsage{}, false
	}
	unit := strings.TrimSpace(stringDetail(event.Details, "attribution_usage_unit"))
	quantity, ok := v3PositiveQuantity(event.Details["attribution_usage_quantity"])
	if !ok || !v3UnitPattern.MatchString(unit) {
		return v3MappedUsage{}, false
	}
	duration, _ := decimalDetail(event.Details, "attribution_usage_duration_seconds")
	return v3MappedUsage{
		component:       ComponentExternal,
		usage:           []UsageLineV2{{Metric: UsageMetric(metric), Quantity: quantity, Unit: UsageUnit(unit)}},
		durationSeconds: duration,
	}, true
}

func selectedV3Component(event core.Event, fallback Component) (Component, bool) {
	explicit := strings.TrimSpace(stringDetail(event.Details, "attribution_component"))
	if explicit == "" {
		return fallback, true
	}
	component, ok := v3Components[explicit]
	return component, ok
}

func v3OperationName(event core.Event) string {
	explicit := strings.TrimSpace(stringDetail(event.Details, "attribution_operation_name"))
	if canonicalPattern.MatchString(explicit) {
		return explicit
	}
	switch event.EventType {
	case core.EventTypeLLMCall:
		return "llm.call"
	case core.EventTypeExternalCost:
		return "external.call"
	case core.EventTypeComputeCost:
		return "compute.consume"
	case core.EventTypeGPUCost:
		return "gpu.consume"
	case core.EventTypeGPUUtilizationSignal:
		return "gpu.observe"
	case core.EventTypeNetwork:
		return "network.transfer"
	case core.EventTypeRetryMarker:
		return "retry.attempt"
	default:
		return "external.call"
	}
}

func v3OperationStatus(event core.Event) string {
	explicit := strings.TrimSpace(stringDetail(event.Details, "attribution_operation_status"))
	switch explicit {
	case "in_progress", "succeeded", "failed", "cancelled", "unknown":
		return explicit
	}
	if event.EventType == core.EventTypeGPUUtilizationSignal {
		return "unknown"
	}
	if event.EventType == core.EventTypeRetryMarker || event.ErrorType != "" || stringDetail(event.Details, "error_type") != "" {
		return "failed"
	}
	return "succeeded"
}

func v3OperationFor(event core.Event) (OperationIdentityV3, bool) {
	eventID := strings.ToLower(event.EventID.String())
	retryOf := ""
	if event.RetryOf != nil {
		retryOf = strings.ToLower(event.RetryOf.String())
		if !v3UUIDPattern.MatchString(retryOf) {
			return OperationIdentityV3{}, false
		}
	}
	explicitOperationID := strings.TrimSpace(stringDetail(event.Details, "attribution_operation_id"))
	hasOperationID := v3UUIDPattern.MatchString(explicitOperationID)
	operationID := eventID
	if hasOperationID {
		operationID = strings.ToLower(explicitOperationID)
	}
	attemptID := eventID
	if explicit := strings.TrimSpace(stringDetail(event.Details, "attribution_attempt_id")); v3UUIDPattern.MatchString(explicit) {
		attemptID = strings.ToLower(explicit)
	}
	attemptNumber := int64(1)
	hasAttemptNumber := false
	if raw, ok := decimalDetail(event.Details, "attribution_attempt_number"); ok && raw.IsPositive() && raw.Equal(raw.Truncate(0)) && raw.LessThanOrEqual(decimal.NewFromInt(math.MaxInt32)) {
		attemptNumber = raw.IntPart()
		hasAttemptNumber = true
	}
	if retryOf != "" && (!hasOperationID || !hasAttemptNumber || attemptNumber <= 1) {
		return OperationIdentityV3{}, false
	}
	operation := OperationIdentityV3{
		ID:      operationID,
		Name:    v3OperationName(event),
		Status:  v3OperationStatus(event),
		Attempt: AttemptIdentityV3{ID: attemptID, Number: int(attemptNumber), RetryOf: retryOf},
	}
	traceID := strings.ToLower(strings.TrimSpace(stringDetail(event.Details, "trace_id")))
	spanID := strings.ToLower(strings.TrimSpace(stringDetail(event.Details, "span_id")))
	if v3TraceID.MatchString(traceID) && v3SpanID.MatchString(spanID) {
		operation.Trace = &TraceIdentityV3{TraceID: traceID, SpanID: spanID}
	}
	return operation, true
}

// ToObservationV3 converts durable v1 capture into strict attribution v3.
// Invalid or incomplete records return nil and are quarantined by transport.
func ToObservationV3(event core.Event) *ObservationV3 {
	mapped, explicit := unknownExplicitV3Usage(event)
	if event.EventType == core.EventTypeGPUUtilizationSignal {
		mapped = gpuSignalV3Usage(event)
		explicit = true
	}
	if !explicit {
		component, usage, duration, ok := componentAndUsage(event)
		if !ok {
			return nil
		}
		mapped = v3MappedUsage{component: component, usage: usage, durationSeconds: duration}
	}

	dimensions, ok := explicitV3Dimensions(event.Details)
	if !ok {
		log.Printf("[dexcost] event %s has invalid attribution_dimensions", event.EventID)
		return nil
	}
	dimensions = append(dimensions, mapped.dimensions...)
	sort.Slice(dimensions, func(i, j int) bool { return dimensions[i].Key < dimensions[j].Key })
	stableDimensions, err := canonicalV3DimensionsJSON(dimensions)
	if err != nil {
		return nil
	}
	usage := make([]UsageLineV3, 0, len(mapped.usage))
	for _, line := range mapped.usage {
		usage = append(usage, UsageLineV3{
			LineID: deterministicV3UUID("dexcost:attribution-usage-line:v3", strings.ToLower(event.EventID.String()), string(line.Metric), string(line.Unit), string(stableDimensions)),
			Metric: string(line.Metric), Quantity: line.Quantity, Unit: string(line.Unit), Dimensions: dimensions,
		})
	}
	component, ok := selectedV3Component(event, mapped.component)
	if !ok {
		log.Printf("[dexcost] event %s has invalid attribution_component", event.EventID)
		return nil
	}
	operation, ok := v3OperationFor(event)
	if !ok {
		log.Printf("[dexcost] event %s has invalid or incomplete retry lineage", event.EventID)
		return nil
	}
	occurred := canonicalTime(event.OccurredAt)
	converted := &ObservationV3{
		SchemaVersion: "3", EventID: strings.ToLower(event.EventID.String()), TaskID: strings.ToLower(event.TaskID.String()),
		OccurredAt: occurred, ObservedAt: occurred, Component: component, Provider: providerFor(event), Resource: resourceFor(event),
		Operation: operation, Lifecycle: LifecycleV3{State: "final", Revision: 1}, UsageSnapshot: "full", Usage: usage,
	}
	if event.EventType != core.EventTypeGPUUtilizationSignal {
		converted.CostEvidence = evidenceFor(event)
	}
	hasTime := false
	for _, line := range usage {
		canonical, known := UnitByMetric[UsageMetric(line.Metric)]
		if known && string(canonical) == line.Unit && strings.HasSuffix(line.Unit, "Seconds") {
			hasTime = true
			break
		}
	}
	if hasTime || mapped.durationSeconds.IsPositive() {
		offset := time.Duration(0)
		if mapped.durationSeconds.IsPositive() {
			micros := mapped.durationSeconds.Mul(decimal.NewFromInt(1_000_000)).Round(0).IntPart()
			offset = time.Duration(micros) * time.Microsecond
		}
		converted.UsagePeriod = &UsagePeriodV3{StartAt: canonicalTime(event.OccurredAt.Add(-offset)), EndAt: occurred}
	}
	validation := ValidateObservationV3(converted)
	if !validation.Success {
		paths := make([]string, 0, len(validation.Issues))
		for _, issue := range validation.Issues {
			paths = append(paths, issue.Path)
		}
		log.Printf("[dexcost] event %s cannot be represented by attribution v3: %s", event.EventID, strings.Join(paths, ", "))
		return nil
	}
	return converted
}

func ToEventV3(event core.Event) *EventV3 { return ToObservationV3(event) }
