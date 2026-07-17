package transport

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

func TestPusherFlushesTaskWithoutEvents(t *testing.T) {
	var received map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &received)
		_ = json.NewEncoder(w).Encode(map[string]int{"queued": 1, "rejected": 0})
	}))
	defer server.Close()
	buffer, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer buffer.Close()
	task := core.NewTask("task_only")
	if err := buffer.InsertTask(task); err != nil {
		t.Fatal(err)
	}
	pusher := NewEventPusher(PusherOptions{Buffer: buffer, Endpoint: server.URL, APIKey: "test", Interval: time.Hour})
	defer pusher.Stop()
	if err := pusher.Flush(); err != nil {
		t.Fatal(err)
	}
	if len(received["events"].([]interface{})) != 0 || len(received["tasks"].([]interface{})) != 1 {
		t.Fatalf("unexpected payload: %+v", received)
	}
	pending, err := buffer.QueryPendingTasks(10)
	if err != nil || len(pending) != 0 {
		t.Fatalf("task was not acknowledged: %v %+v", err, pending)
	}
}

func TestPusherSendsStrictAttributionV2AndIngestionOnlyTask(t *testing.T) {
	var received map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &received)
		_ = json.NewEncoder(w).Encode(map[string]int{"queued": 2, "rejected": 0})
	}))
	defer server.Close()
	buffer, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer buffer.Close()
	task := core.NewTask("strict")
	if err := buffer.InsertTask(task); err != nil {
		t.Fatal(err)
	}
	event := core.NewEvent(task.TaskID, core.EventTypeLLMCall)
	event.Provider = "openai"
	tokens := 10
	event.InputTokens = &tokens
	event.Details["secret"] = "must-not-leave"
	if err := buffer.InsertEvent(event); err != nil {
		t.Fatal(err)
	}
	pusher := NewEventPusher(PusherOptions{Buffer: buffer, Endpoint: server.URL, APIKey: "test", Interval: time.Hour})
	defer pusher.Stop()
	if err := pusher.Flush(); err != nil {
		t.Fatal(err)
	}
	eventWire := received["events"].([]interface{})[0].(map[string]interface{})
	if eventWire["schema_version"] != "2" {
		t.Fatalf("not v2: %+v", eventWire)
	}
	if _, ok := eventWire["details"]; ok {
		t.Fatal("event details leaked")
	}
	taskWire := received["tasks"].([]interface{})[0].(map[string]interface{})
	if _, ok := taskWire["total_cost_usd"]; ok {
		t.Fatal("aggregate task cost leaked")
	}
}

func TestPusherRedactsDetailsBeforeAttributionConversion(t *testing.T) {
	const (
		secretRequestID = "provider-request-secret"
		secretGPUSKU    = "gpu-sku-secret"
	)
	var received []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received, _ = io.ReadAll(r.Body)
		_ = json.NewEncoder(w).Encode(map[string]int{"queued": 2, "rejected": 0})
	}))
	defer server.Close()

	buffer, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer buffer.Close()
	task := core.NewTask("redaction")
	if err := buffer.InsertTask(task); err != nil {
		t.Fatal(err)
	}
	event := core.NewEvent(task.TaskID, core.EventTypeGPUCost)
	event.Details["request_id"] = secretRequestID
	event.Details["gpu_sku"] = secretGPUSKU
	event.Details["gpu_seconds_used"] = 1
	event.Details["billing_model"] = "per_gpu_second_active"
	if err := buffer.InsertEvent(event); err != nil {
		t.Fatal(err)
	}

	pusher := NewEventPusher(PusherOptions{
		Buffer:       buffer,
		Endpoint:     server.URL,
		APIKey:       "test",
		Interval:     time.Hour,
		RedactFields: []string{"request_id", "gpu_sku"},
	})
	defer pusher.Stop()
	if err := pusher.Flush(); err != nil {
		t.Fatal(err)
	}
	payload := string(received)
	if strings.Contains(payload, secretRequestID) || strings.Contains(payload, secretGPUSKU) {
		t.Fatalf("redacted attribution detail leaked into wire payload: %s", payload)
	}
	if !strings.Contains(payload, "[REDACTED]") {
		t.Fatalf("expected typed attribution fields to contain redaction marker: %s", payload)
	}
}

func TestPusherRequeuesTaskAfterDurableStateChange(t *testing.T) {
	var received []map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode payload: %v", err)
		}
		received = append(received, payload)
		_ = json.NewEncoder(w).Encode(map[string]int{"queued": 1, "rejected": 0})
	}))
	defer server.Close()

	buffer, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer buffer.Close()
	task := core.NewTask("task_lifecycle")
	task.Status = core.TaskStatusRunning
	if err := buffer.InsertTask(task); err != nil {
		t.Fatal(err)
	}
	pusher := NewEventPusher(PusherOptions{Buffer: buffer, Endpoint: server.URL, APIKey: "test", Interval: time.Hour})
	defer pusher.Stop()
	if err := pusher.Flush(); err != nil {
		t.Fatal(err)
	}

	endedAt := time.Now().UTC()
	task.Status = core.TaskStatusSuccess
	task.EndedAt = &endedAt
	if err := buffer.UpdateTask(task); err != nil {
		t.Fatal(err)
	}
	if err := pusher.Flush(); err != nil {
		t.Fatal(err)
	}

	if len(received) != 2 {
		t.Fatalf("expected running and completed task uploads, got %d", len(received))
	}
	firstTask := received[0]["tasks"].([]interface{})[0].(map[string]interface{})
	secondTask := received[1]["tasks"].([]interface{})[0].(map[string]interface{})
	if firstTask["status"] != string(core.TaskStatusRunning) {
		t.Fatalf("expected first task upload to be running, got %+v", firstTask)
	}
	if secondTask["status"] != string(core.TaskStatusSuccess) || secondTask["ended_at"] == nil {
		t.Fatalf("expected completed task to be re-uploaded with ended_at, got %+v", secondTask)
	}
}

func TestPusherDoesNotAcknowledgePartialAcceptance(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]int{"queued": 1, "rejected": 1})
	}))
	defer server.Close()
	buffer, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer buffer.Close()
	task := core.NewTask("partial")
	_ = buffer.InsertTask(task)
	event := core.NewEvent(task.TaskID, core.EventTypeExternalCost)
	_ = buffer.InsertEvent(event)
	pusher := NewEventPusher(PusherOptions{Buffer: buffer, Endpoint: server.URL, APIKey: "test", Interval: time.Hour})
	defer pusher.Stop()
	if err := pusher.Flush(); err == nil {
		t.Fatal("partial rejection must fail the flush")
	}
	pending, err := buffer.QueryPendingEvents(10)
	if err != nil || len(pending) != 1 {
		t.Fatalf("rejected event was acknowledged: %v %+v", err, pending)
	}
}
