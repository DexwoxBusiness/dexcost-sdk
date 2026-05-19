package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

func init() {
	gin.SetMode(gin.TestMode)
}

func TestGinMiddleware_Success(t *testing.T) {
	tr := newTestTracker(t)

	r := gin.New()
	r.Use(GinMiddleware(tr, "gin_request"))
	r.GET("/test", func(c *gin.Context) {
		task := core.GetCurrentTask(c.Request.Context())
		if task == nil {
			t.Error("expected task in context")
		}
		c.String(http.StatusOK, "ok")
	})

	req := httptest.NewRequest("GET", "/test", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestGinMiddleware_500_MarksFailed(t *testing.T) {
	tr := newTestTracker(t)
	var capturedTaskID string

	r := gin.New()
	r.Use(GinMiddleware(tr, "gin_request"))
	r.GET("/fail", func(c *gin.Context) {
		task := core.GetCurrentTask(c.Request.Context())
		if task != nil {
			capturedTaskID = task.TaskID.String()
		}
		c.String(http.StatusInternalServerError, "error")
	})

	req := httptest.NewRequest("GET", "/fail", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

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

func TestGinMiddleware_ContextPropagated(t *testing.T) {
	tr := newTestTracker(t)
	var foundTaskType string

	r := gin.New()
	r.Use(GinMiddleware(tr, "gin_api"))
	r.GET("/ctx", func(c *gin.Context) {
		task := core.GetCurrentTask(c.Request.Context())
		if task != nil {
			foundTaskType = task.TaskType
		}
		c.String(http.StatusOK, "ok")
	})

	req := httptest.NewRequest("GET", "/ctx", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if foundTaskType != "gin_api" {
		t.Errorf("expected gin_api, got %s", foundTaskType)
	}
}
