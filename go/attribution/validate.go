package attribution

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"
)

type ValidationIssue struct {
	Path    string `json:"path"`
	Message string `json:"message"`
}

type ValidationResult struct {
	Success bool              `json:"success"`
	Issues  []ValidationIssue `json:"issues"`
}

var (
	uuidPattern      = regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	canonicalPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,127}$`)
	positiveDecimal  = regexp.MustCompile(`^(?:0|[1-9][0-9]{0,25})(?:\.[0-9]{1,12})?$`)
	currencyPattern  = regexp.MustCompile(`^[A-Z]{3}$`)
	timestampPattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$`)
)

var components = stringSet("llm", "telephony", "voice_platform", "speech_to_text", "text_to_speech", "realtime_transport", "recording", "post_call_analysis", "compute", "gpu", "network", "storage", "external")
var resourceTypes = stringSet("model", "sku", "instance", "endpoint", "session", "other")
var lifecycleStates = stringSet("pending", "provisional", "final", "voided")
var evidenceSources = stringSet("provider_reported", "sdk_catalog", "sdk_rate_registry", "manual")
var confidences = stringSet("exact", "computed", "estimated", "unknown")

func stringSet(values ...string) map[string]bool {
	set := make(map[string]bool, len(values))
	for _, value := range values {
		set[value] = true
	}
	return set
}

func ValidateEventV2(value interface{}) ValidationResult {
	var event map[string]interface{}
	b, err := json.Marshal(value)
	if err == nil {
		decoder := json.NewDecoder(bytes.NewReader(b))
		decoder.UseNumber()
		err = decoder.Decode(&event)
	}
	issues := make([]ValidationIssue, 0)
	add := func(path, message string) { issues = append(issues, ValidationIssue{Path: path, Message: message}) }
	if err != nil || event == nil {
		add("", "Event must be an object")
		return ValidationResult{Success: false, Issues: issues}
	}
	unknownKeys(event, stringSet("schema_version", "event_id", "task_id", "occurred_at", "observed_at", "component", "provider", "resource", "lifecycle", "usage_period", "usage", "cost_evidence", "retry_of"), "", add)
	if event["schema_version"] != "2" {
		add("schema_version", "Must equal 2")
	}
	validString(event["event_id"], "event_id", uuidPattern, add)
	validString(event["task_id"], "task_id", uuidPattern, add)
	parseTimestamp(event["occurred_at"], "occurred_at", add)
	parseTimestamp(event["observed_at"], "observed_at", add)
	component, ok := event["component"].(string)
	if !ok || !components[component] {
		add("component", "Unknown attribution component")
	}
	if retry, exists := event["retry_of"]; exists {
		validString(retry, "retry_of", uuidPattern, add)
	}

	provider, ok := event["provider"].(map[string]interface{})
	if !ok {
		add("provider", "Provider must be an object")
	} else {
		unknownKeys(provider, stringSet("name", "service", "record_id", "region"), "provider", add)
		validString(provider["name"], "provider.name", canonicalPattern, add)
		validString(provider["service"], "provider.service", canonicalPattern, add)
		if record, exists := provider["record_id"]; exists {
			s, ok := record.(string)
			if !ok || len(s) < 1 || len(s) > 256 {
				add("provider.record_id", "Invalid provider record ID")
			}
		}
		if region, exists := provider["region"]; exists {
			validString(region, "provider.region", canonicalPattern, add)
		}
	}

	if raw, exists := event["resource"]; exists {
		resource, ok := raw.(map[string]interface{})
		if !ok {
			add("resource", "Resource must be an object")
		} else {
			unknownKeys(resource, stringSet("type", "id"), "resource", add)
			t, ok := resource["type"].(string)
			if !ok || !resourceTypes[t] {
				add("resource.type", "Invalid resource type")
			}
			id, ok := resource["id"].(string)
			if !ok || len(id) < 1 || len(id) > 256 {
				add("resource.id", "Invalid resource ID")
			}
		}
	}

	state := ""
	revision := int64(0)
	lifecycle, ok := event["lifecycle"].(map[string]interface{})
	if !ok {
		add("lifecycle", "Lifecycle must be an object")
	} else {
		unknownKeys(lifecycle, stringSet("state", "revision"), "lifecycle", add)
		state, ok = lifecycle["state"].(string)
		if !ok || !lifecycleStates[state] {
			add("lifecycle.state", "Invalid lifecycle state")
			state = ""
		}
		n, ok := lifecycle["revision"].(json.Number)
		if !ok {
			add("lifecycle.revision", "Revision must be a positive integer")
		} else if revision, err = n.Int64(); err != nil || revision < 1 || revision > 2147483647 {
			add("lifecycle.revision", "Revision must be a positive integer")
			revision = 0
		}
	}

	var period map[string]interface{}
	var start, end time.Time
	var startOK, endOK bool
	if raw, exists := event["usage_period"]; exists {
		period, ok = raw.(map[string]interface{})
		if !ok {
			add("usage_period", "Usage period must be an object")
		} else {
			unknownKeys(period, stringSet("start_at", "end_at"), "usage_period", add)
			start, startOK = parseTimestamp(period["start_at"], "usage_period.start_at", add)
			if rawEnd, exists := period["end_at"]; exists {
				end, endOK = parseTimestamp(rawEnd, "usage_period.end_at", add)
			}
			if startOK && endOK && end.Before(start) {
				add("usage_period.end_at", "End cannot precede start")
			}
		}
	}

	usage, usageOK := event["usage"].([]interface{})
	hasTime := false
	seen := map[string]bool{}
	if !usageOK {
		add("usage", "Usage must be an array")
	} else {
		if len(usage) > 32 {
			add("usage", "At most 32 usage lines are allowed")
		}
		for i, raw := range usage {
			prefix := fmt.Sprintf("usage.%d", i)
			line, ok := raw.(map[string]interface{})
			if !ok {
				add(prefix, "Usage line must be an object")
				continue
			}
			unknownKeys(line, stringSet("metric", "quantity", "unit"), prefix, add)
			metric, metricOK := line["metric"].(string)
			canonicalUnit, knownMetric := UnitByMetric[UsageMetric(metric)]
			if !metricOK || !knownMetric {
				add(prefix+".metric", "Invalid usage metric")
			} else if seen[metric] {
				add(prefix+".metric", "Duplicate usage metric")
			} else {
				seen[metric] = true
			}
			quantity, quantityOK := line["quantity"].(string)
			if !quantityOK || !positiveDecimal.MatchString(quantity) || !strings.ContainsAny(quantity, "123456789") {
				add(prefix+".quantity", "Must be a positive plain decimal string")
			}
			unit, unitOK := line["unit"].(string)
			if !unitOK || !unitKnown(unit) {
				add(prefix+".unit", "Invalid usage unit")
			} else {
				hasTime = hasTime || strings.HasSuffix(unit, "Seconds")
				if knownMetric && UsageUnit(unit) != canonicalUnit {
					add(prefix+".unit", "Metric must use its canonical unit")
				}
			}
		}
	}

	var cost map[string]interface{}
	if raw, exists := event["cost_evidence"]; exists {
		cost, ok = raw.(map[string]interface{})
		if !ok {
			add("cost_evidence", "Cost evidence must be an object")
		} else {
			unknownKeys(cost, stringSet("amount", "currency", "source", "confidence", "pricing_version"), "cost_evidence", add)
			amount, amountOK := cost["amount"].(string)
			if !amountOK || !positiveDecimal.MatchString(amount) || !strings.ContainsAny(amount, "123456789") {
				add("cost_evidence.amount", "Must be a positive plain decimal string")
			}
			currency, currencyOK := cost["currency"].(string)
			if !currencyOK || !currencyPattern.MatchString(currency) {
				add("cost_evidence.currency", "Invalid currency")
			}
			source, sourceOK := cost["source"].(string)
			if !sourceOK || !evidenceSources[source] {
				add("cost_evidence.source", "Invalid evidence source")
				source = ""
			}
			confidence, confidenceOK := cost["confidence"].(string)
			if !confidenceOK || !confidences[confidence] {
				add("cost_evidence.confidence", "Invalid confidence")
				confidence = ""
			}
			if pv, exists := cost["pricing_version"]; exists {
				s, ok := pv.(string)
				if !ok || len(s) < 1 || len(s) > 128 {
					add("cost_evidence.pricing_version", "Invalid pricing version")
				}
			}
			if source == "provider_reported" && confidence != "" && confidence != "exact" && confidence != "estimated" {
				add("cost_evidence.confidence", "Provider-reported cost must be exact or estimated")
			}
			if (source == "sdk_catalog" || source == "sdk_rate_registry") && confidence == "exact" {
				add("cost_evidence.confidence", "SDK-derived cost cannot be exact")
			}
			if source == "sdk_catalog" || source == "sdk_rate_registry" {
				if _, exists := cost["pricing_version"]; !exists {
					add("cost_evidence.pricing_version", "SDK-derived cost requires pricing_version")
				}
			}
		}
	}

	if hasTime && (state == "provisional" || state == "final") && !endOK {
		add("usage_period.end_at", "Finalized time-based usage requires a closed usage period")
	}
	usageLen := 0
	if usageOK {
		usageLen = len(usage)
	}
	switch state {
	case "pending":
		if usageLen != 0 {
			add("usage", "Pending events cannot assert usage")
		}
		if _, exists := event["cost_evidence"]; exists {
			add("cost_evidence", "Pending events cannot assert cost")
		}
		if period != nil {
			if _, exists := period["end_at"]; exists {
				add("usage_period.end_at", "Pending events cannot close usage")
			}
		}
	case "provisional":
		if usageLen == 0 {
			add("usage", "Provisional events require usage")
		}
		if cost != nil && cost["confidence"] == "exact" {
			add("cost_evidence.confidence", "Provisional cost cannot be exact")
		}
	case "final":
		if usageLen == 0 {
			add("usage", "Final events require usage")
		}
	case "voided":
		if revision == 1 {
			add("lifecycle.revision", "Voided events must supersede an earlier revision")
		}
		if usageLen != 0 {
			add("usage", "Voided events must be tombstones")
		} else if _, exists := event["cost_evidence"]; exists {
			add("usage", "Voided events must be tombstones")
		}
	}
	return ValidationResult{Success: len(issues) == 0, Issues: issues}
}

func unknownKeys(value map[string]interface{}, allowed map[string]bool, prefix string, add func(string, string)) {
	for key := range value {
		if !allowed[key] {
			path := key
			if prefix != "" {
				path = prefix + "." + key
			}
			add(path, "Unknown field")
		}
	}
}

func validString(value interface{}, path string, pattern *regexp.Regexp, add func(string, string)) bool {
	s, ok := value.(string)
	if !ok || s == "" || (pattern != nil && !pattern.MatchString(s)) {
		add(path, "Invalid string value")
		return false
	}
	return true
}

func parseTimestamp(value interface{}, path string, add func(string, string)) (time.Time, bool) {
	s, ok := value.(string)
	if !ok || !timestampPattern.MatchString(s) {
		add(path, "Invalid string value")
		return time.Time{}, false
	}
	parsed, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		add(path, "Timestamp must be a valid ISO 8601 calendar instant")
		return time.Time{}, false
	}
	return parsed.UTC(), true
}

func unitKnown(unit string) bool {
	for _, candidate := range UnitByMetric {
		if string(candidate) == unit {
			return true
		}
	}
	return false
}
