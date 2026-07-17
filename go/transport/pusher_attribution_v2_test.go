package transport

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
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
