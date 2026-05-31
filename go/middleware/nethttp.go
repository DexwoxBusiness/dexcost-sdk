// Package middleware provides HTTP middleware for automatic task tracking
// with dexcost. Supports net/http, Gin, and Echo.
package middleware

import (
	"net/http"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// statusCapture wraps http.ResponseWriter to capture the status code.
type statusCapture struct {
	http.ResponseWriter
	statusCode int
	written    bool
}

func (w *statusCapture) WriteHeader(code int) {
	if !w.written {
		w.statusCode = code
		w.written = true
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *statusCapture) Write(b []byte) (int, error) {
	if !w.written {
		w.statusCode = http.StatusOK
		w.written = true
	}
	return w.ResponseWriter.Write(b)
}

// TrackerProvider is the interface needed to start tasks.
// This allows middleware to work with the core.Tracker directly
// without depending on the top-level dexcost package (avoiding cycles).
type TrackerProvider interface {
	StartTask(ctx interface{}, taskType string, opts ...core.TaskOption) (interface{}, *core.TrackedTask)
}

// HTTPMiddleware returns a net/http middleware that automatically starts
// a dexcost task for each request. The task is ended with StatusSuccess
// for 2xx-4xx responses and StatusFailed for 5xx or panics.
//
// Usage:
//
//	tracker := dexcost.Tracker()
//	mux.Handle("/", middleware.HTTPMiddleware(tracker, "api_request")(handler))
func HTTPMiddleware(tracker *core.Tracker, taskType string, opts ...core.TaskOption) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx, tt := tracker.StartTask(r.Context(), taskType, opts...)
			r = r.WithContext(ctx)

			sc := &statusCapture{ResponseWriter: w, statusCode: http.StatusOK}

			// Handle panics.
			panicked := true
			defer func() {
				if panicked {
					// Safely try to end the task — don't re-panic
					func() {
						defer func() { recover() }() // swallow any End() panic
						tt.End(core.TaskStatusFailed)
					}()
				}
			}()

			next.ServeHTTP(sc, r)
			panicked = false

			if sc.statusCode >= 500 {
				tt.End(core.TaskStatusFailed)
			} else {
				tt.End(core.TaskStatusSuccess)
			}
		})
	}
}

// HTTPMiddlewareFunc is a convenience that wraps an http.HandlerFunc.
func HTTPMiddlewareFunc(tracker *core.Tracker, taskType string, opts ...core.TaskOption) func(http.HandlerFunc) http.Handler {
	mw := HTTPMiddleware(tracker, taskType, opts...)
	return func(hf http.HandlerFunc) http.Handler {
		return mw(hf)
	}
}
