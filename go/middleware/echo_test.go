package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

func TestEchoMiddleware_Success(t *testing.T) {
	tr := newTestTracker(t)

	e := echo.New()
	e.Use(EchoMiddleware(tr, "echo_request"))
	e.GET("/test", func(c echo.Context) error {
		task := core.GetCurrentTask(c.Request().Context())
		if task == nil {
			t.Error("expected task in context")
		}
		return c.String(http.StatusOK, "ok")
	})

	req := httptest.NewRequest("GET", "/test", nil)
	w := httptest.NewRecorder()
	e.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestEchoMiddleware_500_MarksFailed(t *testing.T) {
	tr := newTestTracker(t)
	var capturedTaskID string

	e := echo.New()
	e.Use(EchoMiddleware(tr, "echo_request"))
	e.GET("/fail", func(c echo.Context) error {
		task := core.GetCurrentTask(c.Request().Context())
		if task != nil {
			capturedTaskID = task.TaskID.String()
		}
		return c.String(http.StatusInternalServerError, "error")
	})

	req := httptest.NewRequest("GET", "/fail", nil)
	w := httptest.NewRecorder()
	e.ServeHTTP(w, req)

	if capturedTaskID == "" {
		t.Fatal("expected task ID")
	}
	stored, _ := tr.Buffer().GetTask(capturedTaskID)
	if stored == nil {
		t.Fatal("expected stored task")
	}
	if stored.Status != core.TaskStatusFailed {
		t.Errorf("expected failed, got %s", stored.Status)
	}
}

func TestEchoMiddleware_ContextPropagated(t *testing.T) {
	tr := newTestTracker(t)
	var foundTaskType string

	e := echo.New()
	e.Use(EchoMiddleware(tr, "echo_api"))
	e.GET("/ctx", func(c echo.Context) error {
		task := core.GetCurrentTask(c.Request().Context())
		if task != nil {
			foundTaskType = task.TaskType
		}
		return c.String(http.StatusOK, "ok")
	})

	req := httptest.NewRequest("GET", "/ctx", nil)
	w := httptest.NewRecorder()
	e.ServeHTTP(w, req)

	if foundTaskType != "echo_api" {
		t.Errorf("expected echo_api, got %s", foundTaskType)
	}
}

func TestEchoMiddleware_Error_MarksFailed(t *testing.T) {
	tr := newTestTracker(t)
	var capturedTaskID string

	e := echo.New()
	e.Use(EchoMiddleware(tr, "echo_request"))
	e.GET("/err", func(c echo.Context) error {
		task := core.GetCurrentTask(c.Request().Context())
		if task != nil {
			capturedTaskID = task.TaskID.String()
		}
		return echo.NewHTTPError(http.StatusBadGateway, "gateway error")
	})

	req := httptest.NewRequest("GET", "/err", nil)
	w := httptest.NewRecorder()
	e.ServeHTTP(w, req)

	if capturedTaskID == "" {
		t.Fatal("expected task ID")
	}
	stored, _ := tr.Buffer().GetTask(capturedTaskID)
	if stored == nil {
		t.Fatal("expected stored task")
	}
	if stored.Status != core.TaskStatusFailed {
		t.Errorf("expected failed, got %s", stored.Status)
	}
}
