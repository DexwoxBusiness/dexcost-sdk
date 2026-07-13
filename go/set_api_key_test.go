// Sprint 2 Theme D / §3.2.3 (B14) — Go set_api_key recovery.
//
// After a 401/403 the EventPusher permanently stops (pusher.go:355).
// Without a public API to update the key + clear the stopped flag,
// the only recovery is restarting the customer's process. This test
// pins the public API contract:
//
//   dexcost.SetAPIKey(new_key) — returns true if a global tracker
//   exists (clears stopped + updates key), false otherwise.

package dexcost

import (
	"testing"
)

func TestSetAPIKey_BeforeInit_NoOp(t *testing.T) {
	Close()
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("SetAPIKey panicked before init: %v", r)
		}
	}()
	if ok := SetAPIKey("dx_test_x"); ok {
		t.Fatal("expected false from SetAPIKey before init")
	}
}

func TestSetAPIKey_UpdatesGlobalConfig(t *testing.T) {
	Close()
	defer Close()

	if err := Init(Config{
		APIKey:    "dx_test_old",
		Storage:   "local",
		BufferDir: t.TempDir(),
	}); err != nil {
		t.Fatalf("Init: %v", err)
	}
	if globalConfig.APIKey != "dx_test_old" {
		t.Fatalf("init didn't set APIKey: got %q", globalConfig.APIKey)
	}

	if !SetAPIKey("dx_live_new") {
		t.Fatal("expected SetAPIKey to return true after init")
	}
	if globalConfig.APIKey != "dx_live_new" {
		t.Errorf("expected APIKey=dx_live_new, got %q", globalConfig.APIKey)
	}
}

// Pusher-level state assertion lives in the transport package
// (see go/transport/set_api_key_test.go) where the private fields
// are visible. The dexcost-package test asserts only the global API
// contract.
