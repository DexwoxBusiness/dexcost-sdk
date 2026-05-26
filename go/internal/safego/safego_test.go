// Sprint 1 Theme B / §2.2.5 regression tests.

package safego

import (
	"sync"
	"testing"
	"time"
)

// A panicking goroutine launched via Go must not bring down the test
// process; the panic should be caught by the internal recover.
func TestGo_RecoversPanic(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("safego.Go leaked a panic to the caller: %v", r)
		}
	}()

	var wg sync.WaitGroup
	wg.Add(1)
	Go("test-panicker", func() {
		defer wg.Done()
		panic("intentional — must not crash the process")
	})

	// Wait for the goroutine to finish (panic + recover); 1s is enough.
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
		// Test thread is alive — main goal of the helper.
	case <-time.After(time.Second):
		t.Fatal("safego.Go did not finish within 1s")
	}
}

// A successful function runs to completion exactly as `go fn()` would.
func TestGo_RunsFunctionOnSuccess(t *testing.T) {
	ran := make(chan struct{})
	Go("test-runner", func() { close(ran) })

	select {
	case <-ran:
	case <-time.After(time.Second):
		t.Fatal("safego.Go did not invoke fn within 1s")
	}
}
