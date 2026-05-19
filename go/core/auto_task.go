package core

import (
	"context"
	"log"
	"time"
)

// NeedsAutoTask returns true if no explicit task is present in the context.
// This is used by adapters to decide whether to create an auto-task from
// ambient ContextData.
func NeedsAutoTask(ctx context.Context) bool {
	return GetCurrentTask(ctx) == nil
}

// CreateAutoTask creates a Task populated from ambient ContextData in the context.
// If no ContextData is set, the task is created with empty attribution fields.
// The taskType should describe the operation being tracked (e.g. "openai.chat",
// "http_request").
func CreateAutoTask(ctx context.Context, taskType string) Task {
	task := NewTask(taskType)
	cd := GetContextData(ctx)
	if cd != nil {
		task.CustomerID = cd.CustomerID
		task.ProjectID = cd.ProjectID
		if cd.Metadata != nil {
			task.Metadata = cd.Metadata
		}
	}
	return task
}

// FinalizeAutoTask completes an auto-created task, aggregates the event
// cost into the task totals, and persists the result to buffer. If task
// is nil the call is a no-op.
func FinalizeAutoTask(task *Task, event *Event, status string, buffer Buffer) {
	if task == nil {
		return
	}

	task.Status = TaskStatus(status)
	now := time.Now().UTC()
	task.EndedAt = &now

	if event != nil {
		switch event.EventType {
		case EventTypeLLMCall:
			task.LLMCostUSD = task.LLMCostUSD.Add(event.CostUSD)
		case EventTypeExternalCost:
			task.ExternalCostUSD = task.ExternalCostUSD.Add(event.CostUSD)
		case EventTypeComputeCost:
			task.ComputeCostUSD = task.ComputeCostUSD.Add(event.CostUSD)
		}
		task.TotalCostUSD = task.LLMCostUSD.Add(task.ExternalCostUSD).Add(task.ComputeCostUSD)

		if event.InputTokens != nil {
			task.TotalInputTokens += *event.InputTokens
		}
		if event.OutputTokens != nil {
			task.TotalOutputTokens += *event.OutputTokens
		}
		if event.CachedTokens != nil {
			task.TotalCachedTokens += *event.CachedTokens
		}
		if event.IsRetry {
			task.RetryCount++
			task.RetryCostUSD = task.RetryCostUSD.Add(event.CostUSD)
		}
	}

	if task.Status == TaskStatusFailed {
		task.FailureCount = 1
	}

	if buffer != nil {
		if err := buffer.UpdateTask(*task); err != nil {
			log.Printf("[dexcost] failed to update auto-task: %v", err)
		}
	}
}
