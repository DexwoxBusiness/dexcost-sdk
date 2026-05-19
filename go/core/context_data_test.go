package core

import (
	"context"
	"testing"
)

func TestSetContext(t *testing.T) {
	ctx := context.Background()
	ctx = SetContext(ctx, &ContextData{
		CustomerID: "acme",
		ProjectID:  "chatbot",
	})

	cd := GetContextData(ctx)
	if cd == nil {
		t.Fatal("expected context data, got nil")
	}
	if cd.CustomerID != "acme" {
		t.Errorf("expected acme, got %s", cd.CustomerID)
	}
	if cd.ProjectID != "chatbot" {
		t.Errorf("expected chatbot, got %s", cd.ProjectID)
	}
}

func TestGetContextData_ReturnsNilWhenNotSet(t *testing.T) {
	ctx := context.Background()
	cd := GetContextData(ctx)
	if cd != nil {
		t.Error("expected nil, got context data")
	}
}

func TestSetContext_WithMetadata(t *testing.T) {
	ctx := context.Background()
	ctx = SetContext(ctx, &ContextData{
		CustomerID: "acme",
		Metadata:   map[string]interface{}{"env": "prod"},
	})

	cd := GetContextData(ctx)
	if cd.Metadata["env"] != "prod" {
		t.Error("expected metadata env=prod")
	}
}
