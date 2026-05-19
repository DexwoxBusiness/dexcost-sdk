package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

func TestHTTPMiddleware_Success(t *testing.T) {
	tr := newTestTracker(t)
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Verify task is in context.
		task := core.GetCurrentTask(r.Context())
		if task == nil {
			t.Error("expected task in context")
		}
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})

	mw := HTTPMiddleware(tr, "api_request")
	wrapped := mw(handler)

	req := httptest.NewRequest("GET", "/test", nil)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestHTTPMiddleware_500_MarksFailed(t *testing.T) {
	tr := newTestTracker(t)
	var capturedTask *core.Task

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedTask = core.GetCurrentTask(r.Context())
		w.WriteHeader(http.StatusInternalServerError)
	})

	mw := HTTPMiddleware(tr, "api_request")
	wrapped := mw(handler)

	req := httptest.NewRequest("GET", "/fail", nil)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusInternalServerError {
		t.Errorf("expected 500, got %d", w.Code)
	}

	// The task status should be updated in the buffer.
	if capturedTask == nil {
		t.Fatal("expected captured task")
	}
	stored, _ := tr.Buffer().GetTask(capturedTask.TaskID.String())
	if stored == nil {
		t.Fatal("expected stored task")
	}
	if stored.Status != core.TaskStatusFailed {
		t.Errorf("expected failed, got %s", stored.Status)
	}
}

func TestHTTPMiddleware_Panic_MarksFailed(t *testing.T) {
	tr := newTestTracker(t)
	var capturedTask *core.Task

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedTask = core.GetCurrentTask(r.Context())
		panic("test panic")
	})

	mw := HTTPMiddleware(tr, "api_request")
	wrapped := mw(handler)

	req := httptest.NewRequest("GET", "/panic", nil)
	w := httptest.NewRecorder()

	// Should re-panic.
	func() {
		defer func() {
			r := recover()
			if r == nil {
				t.Fatal("expected panic to be re-raised")
			}
			if r != "test panic" {
				t.Errorf("expected 'test panic', got %v", r)
			}
		}()
		wrapped.ServeHTTP(w, req)
	}()

	// Verify task was marked as failed.
	if capturedTask == nil {
		t.Fatal("expected captured task")
	}
	stored, _ := tr.Buffer().GetTask(capturedTask.TaskID.String())
	if stored == nil {
		t.Fatal("expected stored task")
	}
	if stored.Status != core.TaskStatusFailed {
		t.Errorf("expected failed, got %s", stored.Status)
	}
}

func TestHTTPMiddleware_ContextPropagated(t *testing.T) {
	tr := newTestTracker(t)
	var foundTaskType string

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		task := core.GetCurrentTask(r.Context())
		if task != nil {
			foundTaskType = task.TaskType
		}
		w.WriteHeader(http.StatusOK)
	})

	mw := HTTPMiddleware(tr, "web_request")
	wrapped := mw(handler)

	req := httptest.NewRequest("GET", "/ctx", nil)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if foundTaskType != "web_request" {
		t.Errorf("expected web_request, got %s", foundTaskType)
	}
}
