package transport

import (
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// ---------------------------------------------------------------------------
// Pure helper tests
// ---------------------------------------------------------------------------

func TestParseRetryAfterHeader_DeltaSeconds(t *testing.T) {
	cases := []struct {
		name  string
		value string
		want  time.Duration
		ok    bool
	}{
		{"empty", "", 0, false},
		{"whitespace", "   ", 0, false},
		{"zero", "0", 0, true},
		{"positive integer", "30", 30 * time.Second, true},
		{"large", "120", 120 * time.Second, true},
		{"with leading whitespace", "  15", 15 * time.Second, true},
		{"negative", "-1", 0, false},
		{"non-integer garbage", "soon", 0, false},
		{"integer with suffix", "30s", 0, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := parseRetryAfterHeader(c.value, time.Now())
			if ok != c.ok {
				t.Fatalf("ok=%v, want %v (got=%v)", ok, c.ok, got)
			}
			if ok && got != c.want {
				t.Errorf("got=%v, want %v", got, c.want)
			}
		})
	}
}

func TestParseRetryAfterHeader_HTTPDate(t *testing.T) {
	now := time.Date(2026, 5, 6, 12, 0, 0, 0, time.UTC)

	t.Run("future HTTP-date", func(t *testing.T) {
		future := now.Add(45 * time.Second).Format(http.TimeFormat)
		got, ok := parseRetryAfterHeader(future, now)
		if !ok {
			t.Fatalf("expected ok, got false")
		}
		// allow 1s slack for sub-second rounding in http.TimeFormat
		if got < 44*time.Second || got > 46*time.Second {
			t.Errorf("got=%v, want ~45s", got)
		}
	})

	t.Run("past HTTP-date floors to zero", func(t *testing.T) {
		past := now.Add(-30 * time.Second).Format(http.TimeFormat)
		got, ok := parseRetryAfterHeader(past, now)
		if !ok {
			t.Fatalf("expected ok=true for past date, got false")
		}
		if got != 0 {
			t.Errorf("got=%v, want 0 for past date", got)
		}
	})

	t.Run("malformed date", func(t *testing.T) {
		_, ok := parseRetryAfterHeader("not a date", now)
		if ok {
			t.Errorf("expected ok=false for garbage, got true")
		}
	})
}

func TestParseRetryAfterBody(t *testing.T) {
	cases := []struct {
		name string
		body string
		want time.Duration
		ok   bool
	}{
		{"empty", "", 0, false},
		{"not json", "not json", 0, false},
		{"missing field", `{"foo":"bar"}`, 0, false},
		{"null", `{"retry_after_ms":null}`, 0, false},
		{"zero", `{"retry_after_ms":0}`, 0, true},
		{"positive ms", `{"retry_after_ms":1500}`, 1500 * time.Millisecond, true},
		{"negative", `{"retry_after_ms":-1}`, 0, false},
		{"with sibling fields", `{"error":"Too Many Requests","retry_after_ms":12345}`, 12345 * time.Millisecond, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := parseRetryAfterBody([]byte(c.body))
			if ok != c.ok {
				t.Fatalf("ok=%v, want %v (got=%v)", ok, c.ok, got)
			}
			if ok && got != c.want {
				t.Errorf("got=%v, want %v", got, c.want)
			}
		})
	}
}

func TestSetRateLimitBackoff(t *testing.T) {
	cases := []struct {
		name string
		in   time.Duration
		want time.Duration
	}{
		{"negative floors to zero", -5 * time.Second, 0},
		{"zero", 0, 0},
		{"under cap", 10 * time.Second, 10 * time.Second},
		{"at cap", maxBackoff, maxBackoff},
		{"above cap", 2 * maxBackoff, maxBackoff},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p := &EventPusher{}
			p.setRateLimitBackoff(c.in)
			if p.backoff != c.want {
				t.Errorf("got=%v, want %v", p.backoff, c.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// End-to-end tests through pusher + httptest
// ---------------------------------------------------------------------------

func TestPusher_429_RespectsRetryAfterHeader_Seconds(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "30")
		w.WriteHeader(http.StatusTooManyRequests)
	}

	buf, p, _ := setupPusherTest(t, handler)
	task := core.NewTask("rl_test")
	buf.InsertTask(task)
	buf.InsertEvent(core.NewEvent(task.TaskID, core.EventTypeLLMCall))

	if err := p.Flush(); err == nil {
		t.Fatal("expected error from 429, got nil")
	}
	if got := p.Backoff(); got != 30*time.Second {
		t.Errorf("backoff=%v, want 30s (header should override exponential default)", got)
	}
}

func TestPusher_429_RespectsRetryAfterBody(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":"Too Many Requests","retry_after_ms":5000}`))
	}

	buf, p, _ := setupPusherTest(t, handler)
	task := core.NewTask("rl_test")
	buf.InsertTask(task)
	buf.InsertEvent(core.NewEvent(task.TaskID, core.EventTypeLLMCall))

	if err := p.Flush(); err == nil {
		t.Fatal("expected error from 429, got nil")
	}
	if got := p.Backoff(); got != 5*time.Second {
		t.Errorf("backoff=%v, want 5s (body retry_after_ms)", got)
	}
}

func TestPusher_429_HeaderTakesPrecedenceOverBody(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "10")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"retry_after_ms":99000}`))
	}

	buf, p, _ := setupPusherTest(t, handler)
	task := core.NewTask("rl_test")
	buf.InsertTask(task)
	buf.InsertEvent(core.NewEvent(task.TaskID, core.EventTypeLLMCall))

	_ = p.Flush()
	if got := p.Backoff(); got != 10*time.Second {
		t.Errorf("backoff=%v, want 10s (header should win over body)", got)
	}
}

func TestPusher_429_FallsBackToExponentialWhenNoHints(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	}

	buf, p, _ := setupPusherTest(t, handler)
	task := core.NewTask("rl_test")
	buf.InsertTask(task)
	buf.InsertEvent(core.NewEvent(task.TaskID, core.EventTypeLLMCall))

	_ = p.Flush()
	if got := p.Backoff(); got != initialBackoff {
		t.Errorf("first 429 backoff=%v, want %v (initial exponential)", got, initialBackoff)
	}
	_ = p.Flush()
	if got := p.Backoff(); got != 2*initialBackoff {
		t.Errorf("second 429 backoff=%v, want %v (doubled)", got, 2*initialBackoff)
	}
}

func TestPusher_429_BackoffCappedAtMaxBackoff(t *testing.T) {
	// Server says wait an absurd 1 hour — must be capped at maxBackoff (5min).
	handler := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "3600")
		w.WriteHeader(http.StatusTooManyRequests)
	}

	buf, p, _ := setupPusherTest(t, handler)
	task := core.NewTask("rl_test")
	buf.InsertTask(task)
	buf.InsertEvent(core.NewEvent(task.TaskID, core.EventTypeLLMCall))

	_ = p.Flush()
	if got := p.Backoff(); got != maxBackoff {
		t.Errorf("backoff=%v, want %v (cap at maxBackoff)", got, maxBackoff)
	}
}

func TestPusher_429_BackoffResetsOnSuccess(t *testing.T) {
	var calls atomic.Int32
	handler := func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		if n == 1 {
			w.Header().Set("Retry-After", "20")
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.WriteHeader(http.StatusOK)
	}

	buf, p, _ := setupPusherTest(t, handler)
	task := core.NewTask("rl_test")
	buf.InsertTask(task)
	buf.InsertEvent(core.NewEvent(task.TaskID, core.EventTypeLLMCall))

	_ = p.Flush()
	if got := p.Backoff(); got != 20*time.Second {
		t.Fatalf("first flush backoff=%v, want 20s", got)
	}
	if err := p.Flush(); err != nil {
		t.Fatalf("second flush failed: %v", err)
	}
	if got := p.Backoff(); got != 0 {
		t.Errorf("backoff=%v, want 0 after 2xx", got)
	}
}

// Verifies that a 429 with no Retry-After hint does NOT permanently stop
// the pusher (unlike 401/403). It must keep retrying with exponential backoff.
func TestPusher_429_DoesNotStopPusher(t *testing.T) {
	var mu sync.Mutex
	hits := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		hits++
		mu.Unlock()
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	t.Cleanup(srv.Close)

	buf, err := NewSQLiteBuffer(tempDB(t))
	if err != nil {
		t.Fatalf("create buffer: %v", err)
	}
	t.Cleanup(func() { buf.Close() })

	p := NewEventPusher(PusherOptions{
		Buffer:    buf,
		Endpoint:  srv.URL,
		APIKey:    "dx_test_429",
		BatchSize: 100,
		Interval:  1 * time.Hour,
	})
	t.Cleanup(p.Stop)

	task := core.NewTask("rl_test")
	buf.InsertTask(task)
	buf.InsertEvent(core.NewEvent(task.TaskID, core.EventTypeLLMCall))

	_ = p.Flush()
	_ = p.Flush()
	_ = p.Flush()

	mu.Lock()
	defer mu.Unlock()
	if hits != 3 {
		t.Errorf("expected 3 hits across 3 flushes, got %d (pusher may have permanently stopped)", hits)
	}
}
