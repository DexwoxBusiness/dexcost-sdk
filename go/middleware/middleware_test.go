package middleware

import (
	"path/filepath"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
	"github.com/DexwoxBusiness/dexcost-sdk/go/transport"
)

func newTestTracker(t *testing.T) *core.Tracker {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "test.db")
	buf, err := transport.NewSQLiteBuffer(dbPath)
	if err != nil {
		t.Fatalf("create buffer: %v", err)
	}
	tr, err := core.NewTracker(core.TrackerOptions{Buffer: buf})
	if err != nil {
		buf.Close()
		t.Fatalf("create tracker: %v", err)
	}
	t.Cleanup(func() { tr.Close() })
	return tr
}
