package dexcost

import (
	"fmt"
	"os"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

// devMode controls whether development mode console output is enabled.
// When true, every recorded event is printed to stderr with a formatted summary.
var devMode bool

// IsDevMode returns true when development mode is active.
func IsDevMode() bool {
	return devMode
}

// EnableDevMode enables development mode output. All events and task completions
// are printed to stderr. Cloud sync is disabled.
func EnableDevMode() {
	devMode = true
	devPrint("dev mode \u2014 cloud sync disabled")
}

// disableDevMode resets development mode. Exported only for testing.
func disableDevMode() {
	devMode = false
}

// LogEvent prints a single event to stderr when dev mode is active.
func LogEvent(event *core.Event, taskType string) {
	if !devMode {
		return
	}

	cost := event.CostUSD
	confidence := event.CostConfidence

	switch event.EventType {
	case core.EventTypeLLMCall:
		provider := event.Provider
		if provider == "" {
			provider = "?"
		}
		model := event.Model
		if model == "" {
			model = "?"
		}
		inTok := 0
		if event.InputTokens != nil {
			inTok = *event.InputTokens
		}
		outTok := 0
		if event.OutputTokens != nil {
			outTok = *event.OutputTokens
		}
		cached := 0
		if event.CachedTokens != nil {
			cached = *event.CachedTokens
		}
		retryTag := ""
		if event.IsRetry {
			retryTag = "  \033[33m(retry)\033[0m"
		}
		cacheTag := ""
		if cached > 0 {
			cacheTag = fmt.Sprintf("  cached: %d", cached)
		}
		devPrint(fmt.Sprintf(
			"\033[32m+\033[0m llm_call  %s/%s  %d in / %d out%s  $%s%s%s",
			provider, model, inTok, outTok, cacheTag, cost.String(), retryTag, taskTag(taskType),
		))

	case core.EventTypeExternalCost, core.EventTypeComputeCost:
		service := event.ServiceName
		if service == "" {
			service = "unknown"
		}
		if confidence == core.CostConfidenceUnknown || cost.Equal(decimal.Zero) {
			devPrint(fmt.Sprintf(
				"\033[33m!\033[0m %s  %s  $0.00 \033[33m(no rate configured)\033[0m%s",
				string(event.EventType), service, taskTag(taskType),
			))
		} else {
			devPrint(fmt.Sprintf(
				"\033[32m+\033[0m %s  %s  $%s%s",
				string(event.EventType), service, cost.String(), taskTag(taskType),
			))
		}

	case core.EventTypeRetryMarker:
		reason := event.RetryReason
		if reason == "" {
			reason = "unknown"
		}
		devPrint(fmt.Sprintf(
			"\033[33m~\033[0m retry_marker  reason: %s  $%s%s",
			reason, cost.String(), taskTag(taskType),
		))
	}
}

// LogTaskComplete prints a task completion summary to stderr when dev mode is active.
func LogTaskComplete(task *core.Task) {
	if !devMode {
		return
	}

	retryInfo := ""
	if task.RetryCount > 0 {
		retryInfo = fmt.Sprintf("  retries: %d  retry cost: $%s",
			task.RetryCount, task.RetryCostUSD.String())
	}

	devPrint(fmt.Sprintf(
		"\033[36m+\033[0m task %s  %s  total: $%s%s",
		string(task.Status), task.TaskType, task.TotalCostUSD.String(), retryInfo,
	))
}

// taskTag returns a formatted task type tag for console output.
func taskTag(taskType string) string {
	if taskType != "" {
		return fmt.Sprintf("  \033[90m(task: %s)\033[0m", taskType)
	}
	return ""
}

// devPrint writes a formatted message to stderr with the dexcost prefix.
func devPrint(msg string) {
	fmt.Fprintf(os.Stderr, "\033[36m[dexcost]\033[0m %s\n", msg)
}
