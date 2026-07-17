package adapters

import (
	"context"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/security"
)

// DefaultBrowserRatePerMinute is the fallback cost per minute of browser usage
// (USD) used when StartBrowserSession is given a zero rate.
var DefaultBrowserRatePerMinute = decimal.NewFromFloat(0.01)

// BrowserSession tracks an in-progress browser-automation session. Create one
// with StartBrowserSession and close it with End (defer-friendly) to record a
// compute_cost event proportional to the session's wall-clock duration.
//
// The adapter is dependency-free and duck-typed: the page is identified by a
// URL string rather than a Playwright object, so the SDK needs no browser
// library. This mirrors Python's adapters/browser.py track_browser and the
// TypeScript trackBrowser adapter.
type BrowserSession struct {
	ctx           context.Context
	pageURL       string
	ratePerMinute decimal.Decimal
	start         time.Time
	ended         bool
}

// StartBrowserSession begins timing a browser-automation session. Call End
// (typically via defer) to record the cost event.
//
// ratePerMinute is the cost per minute of browser usage; pass decimal.Zero to
// use DefaultBrowserRatePerMinute.
func StartBrowserSession(ctx context.Context, pageURL string, ratePerMinute decimal.Decimal) *BrowserSession {
	if ratePerMinute.IsZero() {
		ratePerMinute = DefaultBrowserRatePerMinute
	}
	return &BrowserSession{
		ctx:           ctx,
		pageURL:       pageURL,
		ratePerMinute: ratePerMinute,
		start:         time.Now(),
	}
}

// End records a compute_cost event of elapsed_minutes * ratePerMinute against
// the active task in the session's context. It is a no-op when there is no
// active task (Python parity: adapters/browser.py records only with a task) or
// when called more than once. The event is persisted to durable storage when
// a buffer is registered via SetEventBuffer, so the sync pusher ships it.
func (s *BrowserSession) End() {
	if s.ended {
		return
	}
	s.ended = true

	task := core.GetCurrentTask(s.ctx)
	if task == nil {
		return
	}

	elapsed := time.Since(s.start)
	elapsedMinutes := decimal.NewFromFloat(elapsed.Seconds()).Div(decimal.NewFromInt(60))
	costUSD := elapsedMinutes.Mul(s.ratePerMinute)

	event := core.NewEvent(task.TaskID, core.EventTypeComputeCost)
	event.ServiceName = "playwright_browser"
	event.CostUSD = costUSD
	event.CostConfidence = core.CostConfidenceComputed
	// The rate is supplied by the caller (or the SDK default), not selected
	// from the versioned rate registry. Attribute it as manual evidence.
	event.PricingSource = core.PricingSourceManual
	event.Details["wall_clock_seconds"] = elapsed.Seconds()
	event.Details["rate_per_minute"] = s.ratePerMinute.String()
	event.Details["page_url"] = security.ScrubURL(s.pageURL)

	persistEvent(event, nil)
}

// TrackBrowser runs fn while timing it as a browser-automation session and
// records the compute_cost event when fn returns — even if fn panics or
// returns an error. It is a callback-style convenience wrapper over
// StartBrowserSession / End.
func TrackBrowser(ctx context.Context, pageURL string, ratePerMinute decimal.Decimal, fn func() error) error {
	session := StartBrowserSession(ctx, pageURL, ratePerMinute)
	defer session.End()
	return fn()
}
