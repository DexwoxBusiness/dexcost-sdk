package middleware

import (
	"github.com/gin-gonic/gin"

	"github.com/DexwoxBusiness/dexcost-go/core"
)

// GinMiddleware returns a Gin middleware that automatically starts a
// dexcost task for each request. The task is ended with StatusSuccess
// for 2xx-4xx responses and StatusFailed for 5xx or panics.
//
// Usage:
//
//	tracker := dexcost.Tracker()
//	r := gin.Default()
//	r.Use(middleware.GinMiddleware(tracker, "api_request"))
func GinMiddleware(tracker *core.Tracker, taskType string, opts ...core.TaskOption) gin.HandlerFunc {
	return func(c *gin.Context) {
		ctx, tt := tracker.StartTask(c.Request.Context(), taskType, opts...)
		c.Request = c.Request.WithContext(ctx)

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

		c.Next()
		panicked = false

		if c.Writer.Status() >= 500 {
			tt.End(core.TaskStatusFailed)
		} else {
			tt.End(core.TaskStatusSuccess)
		}
	}
}
