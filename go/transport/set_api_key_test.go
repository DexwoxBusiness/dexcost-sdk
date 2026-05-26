// Sprint 2 Theme D / §3.2.3 (B14) — pusher-level set-api-key contract.
//
// This test lives in the transport package (not dexcost) so it can
// inspect the EventPusher's private state directly.

package transport

import (
	"testing"
	"time"
)

func TestEventPusher_SetAPIKey_UpdatesKeyAndClearsStopped(t *testing.T) {
	buf, err := NewSQLiteBuffer(t.TempDir() + "/buf.db")
	if err != nil {
		t.Fatalf("buf: %v", err)
	}
	defer buf.Close()

	p := NewEventPusher(PusherOptions{
		Buffer:    buf,
		Endpoint:  "https://example.invalid",
		APIKey:    "dx_test_old",
		BatchSize: 100,
		Interval:  time.Hour,
	})

	// Simulate auth failure: pusher silently halts on 401/403.
	p.mu.Lock()
	p.stopped = true
	p.mu.Unlock()

	p.SetAPIKey("dx_live_new")

	p.mu.Lock()
	stopped := p.stopped
	gotKey := p.apiKey
	p.mu.Unlock()
	if stopped {
		t.Error("SetAPIKey did not clear stopped flag")
	}
	if gotKey != "dx_live_new" {
		t.Errorf("expected apiKey=dx_live_new, got %q", gotKey)
	}
}
