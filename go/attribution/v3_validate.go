package attribution

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/santhosh-tekuri/jsonschema/v5"
	"github.com/shopspring/decimal"
)

//go:embed attribution-v3-schema.json
var attributionV3SchemaJSON string

var attributionV3Schema *jsonschema.Schema

var (
	v3TimestampPattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$`)
	v3TimeMetrics      = stringSet("audio_seconds", "connected_seconds", "recording_seconds", "agent_seconds", "compute_seconds", "vcpu_seconds", "memory_gib_seconds", "gpu_seconds")
)

func init() {
	compiler := jsonschema.NewCompiler()
	compiler.Draft = jsonschema.Draft2020
	const schemaID = "https://schemas.dexcost.io/attribution/conformance-v3.json"
	// Go's regexp engine has no lookahead. Remove only the positivity lookahead
	// from the in-memory compiler copy; semantic validation below enforces > 0.
	schemaForCompiler := strings.ReplaceAll(attributionV3SchemaJSON, "(?=.*[1-9])", "")
	if err := compiler.AddResource(schemaID, strings.NewReader(schemaForCompiler)); err != nil {
		panic(fmt.Errorf("dexcost: load attribution v3 schema: %w", err))
	}
	var err error
	attributionV3Schema, err = compiler.Compile(schemaID + "#/components/schemas/AttributionObservation")
	if err != nil {
		panic(fmt.Errorf("dexcost: compile attribution v3 observation schema: %w", err))
	}
}

// ValidateObservationV3 validates the complete published schema and the
// control-plane cross-field invariants that JSON Schema cannot express.
func ValidateObservationV3(value interface{}) ValidationResult {
	issues := make([]ValidationIssue, 0)
	add := func(path, message string) {
		for _, issue := range issues {
			if issue.Path == path && issue.Message == message {
				return
			}
		}
		issues = append(issues, ValidationIssue{Path: path, Message: message})
	}

	var normalized interface{}
	encoded, err := json.Marshal(value)
	if err == nil {
		decoder := json.NewDecoder(bytes.NewReader(encoded))
		decoder.UseNumber()
		err = decoder.Decode(&normalized)
	}
	if err != nil {
		add("", "Observation must be JSON serializable")
		return ValidationResult{Success: false, Issues: issues}
	}

	if err := attributionV3Schema.Validate(normalized); err != nil {
		if validation, ok := err.(*jsonschema.ValidationError); ok {
			collectV3SchemaIssues(validation, add)
		} else {
			add("", err.Error())
		}
	}
	if event, ok := normalized.(map[string]interface{}); ok {
		validateV3Semantics(event, add)
	}
	return ValidationResult{Success: len(issues) == 0, Issues: issues}
}

func collectV3SchemaIssues(err *jsonschema.ValidationError, add func(string, string)) {
	if len(err.Causes) == 0 {
		path := jsonPointerPath(err.InstanceLocation)
		if path == "" && strings.HasPrefix(err.Message, "additionalProperties '") {
			parts := strings.Split(err.Message, "'")
			if len(parts) >= 2 {
				path = parts[1]
			}
		}
		add(path, err.Message)
		return
	}
	for _, cause := range err.Causes {
		collectV3SchemaIssues(cause, add)
	}
}

func jsonPointerPath(pointer string) string {
	pointer = strings.TrimPrefix(pointer, "/")
	if pointer == "" {
		return ""
	}
	parts := strings.Split(pointer, "/")
	for i := range parts {
		parts[i] = strings.ReplaceAll(strings.ReplaceAll(parts[i], "~1", "/"), "~0", "~")
	}
	return strings.Join(parts, ".")
}

func validateV3Semantics(event map[string]interface{}, add func(string, string)) {
	validV3Timestamp(event["occurred_at"], "occurred_at", add)
	validV3Timestamp(event["observed_at"], "observed_at", add)

	period, _ := event["usage_period"].(map[string]interface{})
	if period != nil {
		start, startOK := validV3Timestamp(period["start_at"], "usage_period.start_at", add)
		if rawEnd, exists := period["end_at"]; exists {
			end, endOK := validV3Timestamp(rawEnd, "usage_period.end_at", add)
			if startOK && endOK && end.Before(start) {
				add("usage_period.end_at", "Cannot precede start_at")
			}
		}
	}

	usage, _ := event["usage"].([]interface{})
	lineIDs := make(map[string]struct{}, len(usage))
	hasTimeMetric := false
	for index, raw := range usage {
		line, ok := raw.(map[string]interface{})
		if !ok {
			continue
		}
		quantity, quantityOK := line["quantity"].(string)
		parsedQuantity, quantityErr := decimal.NewFromString(quantity)
		if !quantityOK || quantityErr != nil || !parsedQuantity.IsPositive() {
			add(fmt.Sprintf("usage.%d.quantity", index), "Must be a positive plain decimal")
		}
		if lineID, ok := line["line_id"].(string); ok {
			if _, exists := lineIDs[lineID]; exists {
				add(fmt.Sprintf("usage.%d.line_id", index), "Must be unique in a full snapshot")
			}
			lineIDs[lineID] = struct{}{}
		}
		metric, _ := line["metric"].(string)
		if unit, known := UnitByMetric[UsageMetric(metric)]; known {
			if line["unit"] != string(unit) {
				add(fmt.Sprintf("usage.%d.unit", index), "Must use the canonical unit")
			}
			if v3TimeMetrics[metric] {
				hasTimeMetric = true
			}
		}
		dimensions, _ := line["dimensions"].([]interface{})
		keys := make(map[string]struct{}, len(dimensions))
		for dimensionIndex, rawDimension := range dimensions {
			dimension, ok := rawDimension.(map[string]interface{})
			if !ok {
				continue
			}
			if key, ok := dimension["key"].(string); ok {
				if _, exists := keys[key]; exists {
					add(fmt.Sprintf("usage.%d.dimensions.%d.key", index, dimensionIndex), "Must be unique within the usage line")
				}
				keys[key] = struct{}{}
			}
		}
	}

	operation, _ := event["operation"].(map[string]interface{})
	attempt, _ := operation["attempt"].(map[string]interface{})
	attemptNumber, attemptNumberOK := jsonInteger(attempt["number"])
	_, hasRetryOf := attempt["retry_of"]
	if attemptNumberOK && attemptNumber == 1 && hasRetryOf {
		add("operation.attempt.retry_of", "Attempt 1 cannot retry another attempt")
	}
	if attemptNumberOK && attemptNumber > 1 && !hasRetryOf {
		add("operation.attempt.retry_of", "Later attempts require retry_of")
	}
	attemptID, attemptIDOK := attempt["id"].(string)
	retryOf, retryOfOK := attempt["retry_of"].(string)
	if attemptIDOK && retryOfOK && attemptID == retryOf {
		add("operation.attempt.retry_of", "Attempt cannot retry itself")
	}
	if operation["status"] == "succeeded" && operation["error"] != nil {
		add("operation.error", "A succeeded operation cannot carry an error")
	}

	capability, _ := event["capability"].(map[string]interface{})
	if capability != nil && capability["source_id"] != nil && capability["source"] == nil {
		add("capability.source_id", "source_id requires source")
	}

	lifecycle, _ := event["lifecycle"].(map[string]interface{})
	state, _ := lifecycle["state"].(string)
	costEvidence, _ := event["cost_evidence"].(map[string]interface{})
	_, hasCostEvidence := event["cost_evidence"]
	hasClosedPeriod := period != nil && period["end_at"] != nil
	switch state {
	case "pending":
		if len(usage) != 0 {
			add("usage", "Pending cannot assert usage")
		}
		if hasCostEvidence {
			add("cost_evidence", "Pending cannot assert cost evidence")
		}
		if hasClosedPeriod {
			add("usage_period.end_at", "Pending cannot close usage")
		}
	case "provisional":
		if len(usage) == 0 {
			add("usage", "Provisional requires usage")
		}
		if costEvidence["confidence"] == "exact" {
			add("cost_evidence.confidence", "Provisional cost cannot be exact")
		}
	case "final":
		if operation["status"] == "in_progress" {
			add("operation.status", "Final operation cannot be in progress")
		}
		if operation["status"] == "succeeded" && len(usage) == 0 {
			add("usage", "Successful final operation requires usage")
		}
	case "voided":
		revision, ok := jsonInteger(lifecycle["revision"])
		if !ok || revision <= 1 {
			add("lifecycle.revision", "Voided revision must exceed 1")
		}
		if len(usage) != 0 {
			add("usage", "Voided cannot assert usage")
		}
		if hasCostEvidence {
			add("cost_evidence", "Voided cannot assert cost evidence")
		}
	}
	if (state == "provisional" || state == "final") && hasTimeMetric && !hasClosedPeriod {
		add("usage_period.end_at", "Time-based usage requires a closed period")
	}

	if hasCostEvidence {
		amount, amountOK := costEvidence["amount"].(string)
		parsedAmount, amountErr := decimal.NewFromString(amount)
		if !amountOK || amountErr != nil || !parsedAmount.IsPositive() {
			add("cost_evidence.amount", "Must be a positive plain decimal")
		}
	}

	source, _ := costEvidence["source"].(string)
	confidence, _ := costEvidence["confidence"].(string)
	if source == "provider_reported" && confidence != "exact" && confidence != "estimated" {
		add("cost_evidence.confidence", "Provider-reported evidence must be exact or estimated")
	}
	if source == "sdk_catalog" || source == "sdk_rate_registry" {
		if confidence == "exact" {
			add("cost_evidence.confidence", "SDK evidence cannot be exact")
		}
		version, _ := costEvidence["pricing_version"].(string)
		if version == "" {
			add("cost_evidence.pricing_version", "SDK evidence requires a pricing version")
		}
	}
}

func validV3Timestamp(value interface{}, path string, add func(string, string)) (time.Time, bool) {
	raw, ok := value.(string)
	if !ok || !v3TimestampPattern.MatchString(raw) {
		add(path, "Must be a valid offset-aware ISO 8601 instant")
		return time.Time{}, false
	}
	parsed, err := time.Parse(time.RFC3339Nano, raw)
	if err != nil {
		add(path, "Must be a valid offset-aware ISO 8601 instant")
		return time.Time{}, false
	}
	return parsed, true
}

func jsonInteger(value interface{}) (int64, bool) {
	switch number := value.(type) {
	case json.Number:
		parsed, err := strconv.ParseInt(number.String(), 10, 64)
		return parsed, err == nil
	case float64:
		if number != float64(int64(number)) {
			return 0, false
		}
		return int64(number), true
	case int:
		return int64(number), true
	case int64:
		return number, true
	default:
		return 0, false
	}
}
