package middleware

import (
	"github.com/labstack/echo/v4"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// EchoMiddleware returns an Echo middleware that automatically starts a
// dexcost task for each request. The task is ended with StatusSuccess
// for 2xx-4xx responses and StatusFailed for 5xx or panics.
//
// Usage:
//
//	tracker := dexcost.Tracker()
//	e := echo.New()
//	e.Use(middleware.EchoMiddleware(tracker, "api_request"))
func EchoMiddleware(tracker *core.Tracker, taskType string, opts ...core.TaskOption) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			ctx, tt := tracker.StartTask(c.Request().Context(), taskType, opts...)
			c.SetRequest(c.Request().WithContext(ctx))

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

			err := next(c)
			panicked = false

			if err != nil {
				tt.End(core.TaskStatusFailed)
				return err
			}

			if c.Response().Status >= 500 {
				tt.End(core.TaskStatusFailed)
			} else {
				tt.End(core.TaskStatusSuccess)
			}
			return nil
		}
	}
}
