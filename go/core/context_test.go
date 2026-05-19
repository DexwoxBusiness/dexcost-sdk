package core

import (
	"context"
	"testing"
)

func TestGetCurrentTask_Empty(t *testing.T) {
	ctx := context.Background()
	task := GetCurrentTask(ctx)
	if task != nil {
		t.Error("expected nil task from empty context")
	}
}

func TestWithTask_SetAndGet(t *testing.T) {
	task := NewTask("resolve_ticket")
	ctx := WithTask(context.Background(), &task)
	got := GetCurrentTask(ctx)
	if got == nil {
		t.Fatal("expected non-nil task")
	}
	if got.TaskID != task.TaskID {
		t.Errorf("expected task_id=%s, got %s", task.TaskID, got.TaskID)
	}
}

func TestWithTask_Nesting(t *testing.T) {
	parent := NewTask("parent_task")
	child := NewTask("child_task")

	ctx := WithTask(context.Background(), &parent)
	ctx2 := WithTask(ctx, &child)

	// Child context sees child task
	got := GetCurrentTask(ctx2)
	if got.TaskID != child.TaskID {
		t.Errorf("expected child task, got %s", got.TaskType)
	}

	// Parent context still sees parent task
	got2 := GetCurrentTask(ctx)
	if got2.TaskID != parent.TaskID {
		t.Errorf("expected parent task, got %s", got2.TaskType)
	}
}

func TestWithTask_AutoParentLinking(t *testing.T) {
	parent := NewTask("parent_task")
	child := NewTask("child_task")

	ctx := WithTask(context.Background(), &parent)

	// LinkParent should set child.ParentTaskID from context
	LinkParent(ctx, &child)
	if child.ParentTaskID == nil {
		t.Fatal("expected parent_task_id to be set")
	}
	if *child.ParentTaskID != parent.TaskID {
		t.Errorf("expected parent_task_id=%s, got %s", parent.TaskID, *child.ParentTaskID)
	}
}

func TestLinkParent_NoParent(t *testing.T) {
	child := NewTask("orphan")
	LinkParent(context.Background(), &child)
	if child.ParentTaskID != nil {
		t.Error("expected nil parent_task_id when no parent in context")
	}
}

func TestLinkParent_AlreadySet(t *testing.T) {
	parent := NewTask("parent")
	child := NewTask("child")
	existingID := parent.TaskID
	child.ParentTaskID = &existingID

	otherParent := NewTask("other_parent")
	ctx := WithTask(context.Background(), &otherParent)

	LinkParent(ctx, &child)
	// Should NOT overwrite existing ParentTaskID
	if *child.ParentTaskID != existingID {
		t.Errorf("should not overwrite existing parent_task_id")
	}
}
