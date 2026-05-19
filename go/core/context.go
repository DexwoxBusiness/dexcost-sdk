package core

import "context"

type contextKey struct{}

// ContextData holds customer/project attribution independent of tasks.
// It enables dynamic attribution without requiring an explicit task to be started.
type ContextData struct {
	CustomerID string
	ProjectID  string
	Metadata   map[string]interface{}
	Agent      string // used as task_type for auto-created session tasks
}

type trackedTaskKeyType struct{}

var trackedTaskKey = trackedTaskKeyType{}

type contextDataKeyType struct{}

var contextDataKey = contextDataKeyType{}

// SetContext attaches customer attribution data to the context.
// This allows the HTTP adapter and other components to create auto-tasks
// with the correct attribution without an explicit task being started.
func SetContext(ctx context.Context, cd *ContextData) context.Context {
	return context.WithValue(ctx, contextDataKey, cd)
}

// GetContextData returns the ContextData from the context, or nil if not set.
func GetContextData(ctx context.Context) *ContextData {
	v := ctx.Value(contextDataKey)
	if v == nil {
		return nil
	}
	return v.(*ContextData)
}

// ClearContext removes the ContextData from the context.
func ClearContext(ctx context.Context) context.Context {
	return context.WithValue(ctx, contextDataKey, nil)
}

// WithTask returns a new context with the given Task attached.
func WithTask(ctx context.Context, task *Task) context.Context {
	return context.WithValue(ctx, contextKey{}, task)
}

// GetCurrentTask returns the Task from the context, or nil.
func GetCurrentTask(ctx context.Context) *Task {
	v := ctx.Value(contextKey{})
	if v == nil {
		return nil
	}
	return v.(*Task)
}

// WithTrackedTask returns a new context with the given TrackedTask attached.
func WithTrackedTask(ctx context.Context, tt *TrackedTask) context.Context {
	return context.WithValue(ctx, trackedTaskKey, tt)
}

// GetCurrentTrackedTask returns the TrackedTask from the context, or nil.
func GetCurrentTrackedTask(ctx context.Context) *TrackedTask {
	v := ctx.Value(trackedTaskKey)
	if v == nil {
		return nil
	}
	return v.(*TrackedTask)
}

// LinkParent sets child.ParentTaskID from the context's current task,
// but only if ParentTaskID is not already set.
func LinkParent(ctx context.Context, child *Task) {
	if child.ParentTaskID != nil {
		return
	}
	parent := GetCurrentTask(ctx)
	if parent != nil {
		id := parent.TaskID
		child.ParentTaskID = &id
	}
}
