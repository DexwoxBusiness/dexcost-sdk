package dexcost

import (
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/middleware"
)

// TestPublicTaskOptions_AcceptedByMiddleware is a compile-time regression guard:
// the public dexcost.With* constructors must produce values usable both with
// dexcost.StartTask and with the middleware package (which accepts
// ...core.TaskOption). Before TaskOption was aliased to core.TaskOption this
// file would not compile.
func TestPublicTaskOptions_AcceptedByMiddleware(t *testing.T) {
	opts := []TaskOption{
		WithCustomer("acme"),
		WithProject("chatbot"),
		WithExperiment("exp-1"),
		WithVariant("a"),
		WithMetadata(map[string]interface{}{"k": "v"}),
	}

	// Passes to middleware that declares ...core.TaskOption.
	_ = middleware.GinMiddleware(&core.Tracker{}, "http_request", opts...)
	_ = middleware.HTTPMiddleware(&core.Tracker{}, "http_request", WithCustomer("acme"))
}
