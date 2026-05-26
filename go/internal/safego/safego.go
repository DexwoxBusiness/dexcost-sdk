// Package safego provides a single helper, Go, that launches a goroutine
// guarded by a top-level `defer recover()` so a panic in a detached SDK
// worker (background probes, ticker workers, etc.) never crashes the
// customer's process.
//
// Remediation plan §2.2.5: replaces the bare `go func() { ... }()` pattern
// at cloud/cloud_detect.go:589 and pricing/engine.go:326. Any new detached
// goroutine added to the SDK must go through this helper.
package safego

import "log"

// Go launches fn in a new goroutine. Any panic in fn is recovered and
// logged with the supplied name as context. The caller is unblocked
// immediately, as with the `go` keyword.
func Go(name string, fn func()) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				log.Printf("dexcost: panic in goroutine %q recovered: %v", name, r)
			}
		}()
		fn()
	}()
}
