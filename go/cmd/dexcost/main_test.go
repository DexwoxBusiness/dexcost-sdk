package main

import (
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	// Pure-Go SQLite driver.
	_ "modernc.org/sqlite"
)

// createTestDB creates a minimal dexcost SQLite database with seed data.
func createTestDB(t *testing.T, dir string) string {
	t.Helper()
	dbPath := filepath.Join(dir, "test.db")

	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	defer db.Close()

	stmts := []string{
		`CREATE TABLE IF NOT EXISTS tasks (
			task_id TEXT PRIMARY KEY,
			task_type TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'pending',
			started_at TEXT NOT NULL,
			ended_at TEXT,
			metadata TEXT DEFAULT '{}',
			customer_id TEXT DEFAULT '',
			project_id TEXT DEFAULT '',
			parent_task_id TEXT,
			experiment_id TEXT DEFAULT '',
			variant TEXT DEFAULT '',
			llm_cost_usd TEXT NOT NULL DEFAULT '0',
			external_cost_usd TEXT NOT NULL DEFAULT '0',
			compute_cost_usd TEXT NOT NULL DEFAULT '0',
			total_cost_usd TEXT NOT NULL DEFAULT '0',
			total_input_tokens INTEGER NOT NULL DEFAULT 0,
			total_output_tokens INTEGER NOT NULL DEFAULT 0,
			total_cached_tokens INTEGER NOT NULL DEFAULT 0,
			retry_count INTEGER NOT NULL DEFAULT 0,
			retry_cost_usd TEXT NOT NULL DEFAULT '0',
			failure_count INTEGER NOT NULL DEFAULT 0,
			schema_version TEXT NOT NULL DEFAULT '1'
		)`,
		`CREATE TABLE IF NOT EXISTS events (
			event_id TEXT PRIMARY KEY,
			task_id TEXT NOT NULL,
			event_type TEXT NOT NULL,
			occurred_at TEXT NOT NULL,
			cost_usd TEXT NOT NULL DEFAULT '0',
			cost_confidence TEXT NOT NULL DEFAULT 'exact',
			pricing_source TEXT DEFAULT '',
			pricing_version TEXT DEFAULT '',
			service_name TEXT DEFAULT '',
			provider TEXT DEFAULT '',
			model TEXT DEFAULT '',
			input_tokens INTEGER,
			output_tokens INTEGER,
			cached_tokens INTEGER,
			latency_ms INTEGER,
			is_retry INTEGER NOT NULL DEFAULT 0,
			retry_reason TEXT DEFAULT '',
			retry_of TEXT,
			details TEXT DEFAULT '{}',
			schema_version TEXT NOT NULL DEFAULT '1',
			sync_status TEXT NOT NULL DEFAULT 'pending'
		)`,
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			t.Fatalf("create table: %v", err)
		}
	}

	// Seed: 2 tasks, 3 events (2 pending, 1 synced).
	taskTime := time.Date(2026, 4, 4, 10, 0, 0, 0, time.UTC).Format(time.RFC3339)
	db.Exec(`INSERT INTO tasks (task_id, task_type, status, started_at, schema_version)
		VALUES ('task-001', 'resolve_ticket', 'completed', ?, '1')`, taskTime)
	db.Exec(`INSERT INTO tasks (task_id, task_type, status, started_at, schema_version)
		VALUES ('task-002', 'generate_report', 'pending', ?, '1')`, taskTime)

	db.Exec(`INSERT INTO events (event_id, task_id, event_type, occurred_at, sync_status, schema_version)
		VALUES ('evt-001', 'task-001', 'llm_call', ?, 'pending', '1')`, taskTime)
	db.Exec(`INSERT INTO events (event_id, task_id, event_type, occurred_at, sync_status, schema_version)
		VALUES ('evt-002', 'task-001', 'llm_call', ?, 'synced', '1')`, taskTime)
	db.Exec(`INSERT INTO events (event_id, task_id, event_type, occurred_at, sync_status, schema_version)
		VALUES ('evt-003', 'task-002', 'external_cost', ?, 'pending', '1')`, taskTime)

	return dbPath
}

// captureStatus runs cmdStatus and returns captured stdout output.
func captureStatus(args []string) (string, int) {
	var sb strings.Builder
	code := runStatus(args, &sb)
	return sb.String(), code
}

// captureRates runs cmdRates and returns captured stdout output.
func captureRates(args []string) (string, int) {
	var sb strings.Builder
	code := runRates(args, &sb)
	return sb.String(), code
}

// TestStatusExistingDB verifies status output for an existing database.
func TestStatusExistingDB(t *testing.T) {
	dir := t.TempDir()
	dbPath := createTestDB(t, dir)

	out, code := captureStatus([]string{"--db", dbPath})
	if code != 0 {
		t.Fatalf("expected exit 0, got %d\noutput: %s", code, out)
	}

	checks := []string{
		"DB location:",
		"Event count:",
		"Task count:",
		"Pending sync:",
		"Synced:",
		"Pricing version:",
	}
	for _, want := range checks {
		if !strings.Contains(out, want) {
			t.Errorf("expected output to contain %q\noutput:\n%s", want, out)
		}
	}

	// Verify counts are correct.
	if !strings.Contains(out, "Event count:       3") {
		t.Errorf("expected event count=3\noutput:\n%s", out)
	}
	if !strings.Contains(out, "Task count:        2") {
		t.Errorf("expected task count=2\noutput:\n%s", out)
	}
	if !strings.Contains(out, "Pending sync:      2") {
		t.Errorf("expected pending sync=2\noutput:\n%s", out)
	}
	if !strings.Contains(out, "Synced:            1") {
		t.Errorf("expected synced=1\noutput:\n%s", out)
	}
}

// TestStatusMissingDB verifies status output when DB doesn't exist.
func TestStatusMissingDB(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "nonexistent.db")

	out, code := captureStatus([]string{"--db", dbPath})
	if code == 0 {
		t.Fatalf("expected non-zero exit, got 0\noutput: %s", out)
	}

	if !strings.Contains(out, "Database not found") {
		t.Errorf("expected 'Database not found'\noutput:\n%s", out)
	}
	if !strings.Contains(out, dbPath) {
		t.Errorf("expected db path in output\noutput:\n%s", out)
	}
}

// TestRatesImportAndList verifies --import then --list shows imported rates.
func TestRatesImportAndList(t *testing.T) {
	dir := t.TempDir()
	yamlPath := filepath.Join(dir, "rates.yaml")

	yamlContent := `rates:
  twilio_sms:
    per: message
    cost_usd: "0.0079"
  sendgrid_email:
    per: email
    cost_usd: "0.000100"
`
	if err := os.WriteFile(yamlPath, []byte(yamlContent), 0644); err != nil {
		t.Fatalf("write yaml: %v", err)
	}

	// Import and list, using an isolated temp store (avoids ~/.dexcost).
	storePath := filepath.Join(dir, "store.yaml")
	out, code := captureRates([]string{"--import", yamlPath, "--list", "--store", storePath})
	if code != 0 {
		t.Fatalf("expected exit 0, got %d\noutput: %s", code, out)
	}

	if !strings.Contains(out, "twilio_sms") {
		t.Errorf("expected twilio_sms in output\noutput:\n%s", out)
	}
	if !strings.Contains(out, "sendgrid_email") {
		t.Errorf("expected sendgrid_email in output\noutput:\n%s", out)
	}
	if !strings.Contains(out, "message") {
		t.Errorf("expected 'message' unit in output\noutput:\n%s", out)
	}
}

// TestRatesExport verifies --export writes a valid YAML file.
func TestRatesExport(t *testing.T) {
	dir := t.TempDir()
	importPath := filepath.Join(dir, "rates_in.yaml")
	exportPath := filepath.Join(dir, "rates_out.yaml")

	yamlContent := `rates:
  stripe_charge:
    per: transaction
    cost_usd: "0.0030"
`
	if err := os.WriteFile(importPath, []byte(yamlContent), 0644); err != nil {
		t.Fatalf("write yaml: %v", err)
	}

	// Import then export, using an isolated temp store (avoids ~/.dexcost).
	storePath := filepath.Join(dir, "store.yaml")
	out, code := captureRates([]string{"--import", importPath, "--export", exportPath, "--store", storePath})
	if code != 0 {
		t.Fatalf("expected exit 0, got %d\noutput: %s", code, out)
	}

	data, err := os.ReadFile(exportPath)
	if err != nil {
		t.Fatalf("read exported file: %v", err)
	}
	if !strings.Contains(string(data), "stripe_charge") {
		t.Errorf("expected stripe_charge in exported YAML\ncontent:\n%s", string(data))
	}
}

// TestRatesPersistAcrossInvocations verifies rates imported in one invocation
// are visible to a separate --list invocation (the persistent-store fix).
func TestRatesPersistAcrossInvocations(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "store.yaml")
	importPath := filepath.Join(dir, "in.yaml")
	yamlContent := `rates:
  twilio_sms:
    per: message
    cost_usd: "0.0079"
`
	if err := os.WriteFile(importPath, []byte(yamlContent), 0644); err != nil {
		t.Fatalf("write yaml: %v", err)
	}

	// Invocation 1: import into the store.
	if _, code := captureRates([]string{"--import", importPath, "--store", storePath}); code != 0 {
		t.Fatalf("import invocation: exit %d", code)
	}

	// Invocation 2: a separate --list must see the imported rates.
	out, code := captureRates([]string{"--list", "--store", storePath})
	if code != 0 {
		t.Fatalf("list invocation: exit %d\noutput: %s", code, out)
	}
	if !strings.Contains(out, "twilio_sms") {
		t.Errorf("rate did not persist across invocations\noutput:\n%s", out)
	}
}
