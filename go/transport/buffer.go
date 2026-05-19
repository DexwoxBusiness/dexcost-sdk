package transport

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/core"

	// Pure-Go SQLite driver (no CGO required).
	_ "modernc.org/sqlite"
)

// SQLiteBuffer implements a local event buffer backed by SQLite.
// All costs are stored as TEXT to preserve decimal precision.
// UUIDs are stored as TEXT. Booleans are stored as INTEGER (0/1).
// Timestamps are stored as ISO 8601 TEXT.
type SQLiteBuffer struct {
	db *sql.DB
}

// dsnPragmas are appended to every SQLite DSN so that the pragmas apply on
// every connection in the pool, not just the first one Open() handed back.
//
// busy_timeout is the load-bearing one for concurrent fan-out workloads
// (DEX-260): without it, modernc.org/sqlite returns SQLITE_BUSY (5)
// immediately when a writer holds the lock — which is exactly what dropped
// ~163 external_cost + ~42 retry_marker events in the DEX-251 run. With a
// 5 s busy_timeout each connection waits on the lock instead, so the pool
// serialises writes naturally.
//
// _txlock=immediate makes BEGIN acquire RESERVED upfront (writer priority)
// instead of upgrading mid-transaction, which closes the last common
// SQLITE_BUSY race in the deferred-upgrade path.
const dsnPragmas = "_pragma=journal_mode(WAL)" +
	"&_pragma=busy_timeout(5000)" +
	"&_pragma=synchronous(NORMAL)" +
	"&_pragma=foreign_keys(on)" +
	"&_txlock=immediate"

// buildDSN normalises a caller-supplied path into a file: URI with the
// concurrency pragmas appended. Both bare paths ("/tmp/buf.db") and pre-built
// URIs ("file:/tmp/buf.db?cache=shared") are accepted.
func buildDSN(path string) string {
	if strings.HasPrefix(path, "file:") {
		sep := "?"
		if strings.Contains(path, "?") {
			sep = "&"
		}
		return path + sep + dsnPragmas
	}
	return "file:" + path + "?" + dsnPragmas
}

// NewSQLiteBuffer opens (or creates) a SQLite database at the given path
// and initializes the schema.
func NewSQLiteBuffer(dbPath string) (*SQLiteBuffer, error) {
	db, err := sql.Open("sqlite", buildDSN(dbPath))
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}

	// Limit connection pool to prevent resource exhaustion. With WAL +
	// busy_timeout above, concurrent writers serialise at the file-lock
	// level rather than failing with SQLITE_BUSY.
	db.SetMaxOpenConns(5)
	db.SetMaxIdleConns(2)
	db.SetConnMaxLifetime(5 * time.Minute)

	// Surface DSN / driver mis-configuration immediately rather than on the
	// first write under load.
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}

	buf := &SQLiteBuffer{db: db}
	if err := buf.createTables(); err != nil {
		db.Close()
		return nil, err
	}
	return buf, nil
}

func (b *SQLiteBuffer) createTables() error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS schema_version (
			version INTEGER NOT NULL
		)`,
		`INSERT OR IGNORE INTO schema_version (rowid, version) VALUES (1, 1)`,
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
			schema_version TEXT NOT NULL DEFAULT '1',
			sync_status TEXT DEFAULT 'pending'
		)`,
		`CREATE INDEX IF NOT EXISTS idx_tasks_customer_started
			ON tasks (customer_id, started_at)`,
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
			sync_status TEXT NOT NULL DEFAULT 'pending',
			FOREIGN KEY (task_id) REFERENCES tasks(task_id)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_events_task_id
			ON events (task_id)`,
		`CREATE INDEX IF NOT EXISTS idx_events_sync_status
			ON events (sync_status, occurred_at)`,
		`CREATE INDEX IF NOT EXISTS idx_tasks_sync_status
			ON tasks (sync_status, started_at)`,
	}
	for _, s := range stmts {
		if _, err := b.db.Exec(s); err != nil {
			return fmt.Errorf("create tables: %w", err)
		}
	}

	// Migrate existing databases: add experiment_id, variant, and sync_status columns.
	// ALTER TABLE ADD COLUMN is a no-op if the column already exists in
	// modernc.org/sqlite (returns "duplicate column name" error), so we
	// silently ignore errors here.
	b.db.Exec("ALTER TABLE tasks ADD COLUMN experiment_id TEXT DEFAULT ''")
	b.db.Exec("ALTER TABLE tasks ADD COLUMN variant TEXT DEFAULT ''")
	b.db.Exec("ALTER TABLE tasks ADD COLUMN sync_status TEXT DEFAULT 'pending'")

	return nil
}

// InsertTask inserts a new task into the buffer.
func (b *SQLiteBuffer) InsertTask(task core.Task) error {
	endedAt := sqlNullString(nil)
	if task.EndedAt != nil {
		s := task.EndedAt.Format(time.RFC3339Nano)
		endedAt = sqlNullString(&s)
	}
	parentTaskID := sqlNullString(nil)
	if task.ParentTaskID != nil {
		s := task.ParentTaskID.String()
		parentTaskID = sqlNullString(&s)
	}

	_, err := b.db.Exec(`INSERT INTO tasks (
		task_id, task_type, status, started_at, ended_at, metadata,
		customer_id, project_id, parent_task_id,
		experiment_id, variant,
		llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
		total_input_tokens, total_output_tokens, total_cached_tokens,
		retry_count, retry_cost_usd, failure_count, schema_version
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		task.TaskID.String(),
		task.TaskType,
		string(task.Status),
		task.StartedAt.Format(time.RFC3339Nano),
		endedAt,
		marshalMetadata(task.Metadata),
		task.CustomerID,
		task.ProjectID,
		parentTaskID,
		task.ExperimentID,
		task.Variant,
		task.LLMCostUSD.String(),
		task.ExternalCostUSD.String(),
		task.ComputeCostUSD.String(),
		task.TotalCostUSD.String(),
		task.TotalInputTokens,
		task.TotalOutputTokens,
		task.TotalCachedTokens,
		task.RetryCount,
		task.RetryCostUSD.String(),
		task.FailureCount,
		task.SchemaVersion,
	)
	return err
}

// UpdateTask updates an existing task in the buffer.
func (b *SQLiteBuffer) UpdateTask(task core.Task) error {
	endedAt := sqlNullString(nil)
	if task.EndedAt != nil {
		s := task.EndedAt.Format(time.RFC3339Nano)
		endedAt = sqlNullString(&s)
	}

	_, err := b.db.Exec(`UPDATE tasks SET
		status = ?, ended_at = ?, metadata = ?,
		llm_cost_usd = ?, external_cost_usd = ?, compute_cost_usd = ?,
		total_cost_usd = ?,
		total_input_tokens = ?, total_output_tokens = ?, total_cached_tokens = ?,
		retry_count = ?, retry_cost_usd = ?, failure_count = ?
	WHERE task_id = ?`,
		string(task.Status),
		endedAt,
		marshalMetadata(task.Metadata),
		task.LLMCostUSD.String(),
		task.ExternalCostUSD.String(),
		task.ComputeCostUSD.String(),
		task.TotalCostUSD.String(),
		task.TotalInputTokens,
		task.TotalOutputTokens,
		task.TotalCachedTokens,
		task.RetryCount,
		task.RetryCostUSD.String(),
		task.FailureCount,
		task.TaskID.String(),
	)
	return err
}

// GetTask retrieves a task by its ID.
func (b *SQLiteBuffer) GetTask(taskID string) (*core.Task, error) {
	row := b.db.QueryRow(`SELECT
		task_id, task_type, status, started_at, ended_at, metadata,
		customer_id, project_id, parent_task_id,
		experiment_id, variant,
		llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
		total_input_tokens, total_output_tokens, total_cached_tokens,
		retry_count, retry_cost_usd, failure_count, schema_version
	FROM tasks WHERE task_id = ?`, taskID)

	var (
		id, taskType, status, startedAt             string
		endedAt, metadataStr, customerID, projectID sql.NullString
		parentTaskID                                sql.NullString
		experimentID, variant                       sql.NullString
		llmCost, extCost, compCost                  string
		totalCost                                   string
		inTok, outTok, cacheTok                     int
		retryCnt, failCnt                           int
		retryCost                                   string
		schemaVer                                   string
	)

	err := row.Scan(
		&id, &taskType, &status, &startedAt, &endedAt, &metadataStr,
		&customerID, &projectID, &parentTaskID,
		&experimentID, &variant,
		&llmCost, &extCost, &compCost, &totalCost,
		&inTok, &outTok, &cacheTok,
		&retryCnt, &retryCost, &failCnt, &schemaVer,
	)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	taskUUID, _ := uuid.Parse(id)
	started, _ := time.Parse(time.RFC3339Nano, startedAt)

	task := &core.Task{
		TaskID:            taskUUID,
		TaskType:          taskType,
		Status:            core.TaskStatus(status),
		StartedAt:         started,
		Metadata:          unmarshalMetadata(metadataStr.String),
		LLMCostUSD:        decimal.RequireFromString(llmCost),
		ExternalCostUSD:   decimal.RequireFromString(extCost),
		ComputeCostUSD:    decimal.RequireFromString(compCost),
		TotalCostUSD:      decimal.RequireFromString(totalCost),
		TotalInputTokens:  inTok,
		TotalOutputTokens: outTok,
		TotalCachedTokens: cacheTok,
		RetryCount:        retryCnt,
		RetryCostUSD:      decimal.RequireFromString(retryCost),
		FailureCount:      failCnt,
		SchemaVersion:     schemaVer,
	}

	if endedAt.Valid {
		t, _ := time.Parse(time.RFC3339Nano, endedAt.String)
		task.EndedAt = &t
	}
	if customerID.Valid {
		task.CustomerID = customerID.String
	}
	if projectID.Valid {
		task.ProjectID = projectID.String
	}
	if parentTaskID.Valid && parentTaskID.String != "" {
		pid, _ := uuid.Parse(parentTaskID.String)
		task.ParentTaskID = &pid
	}
	if experimentID.Valid {
		task.ExperimentID = experimentID.String
	}
	if variant.Valid {
		task.Variant = variant.String
	}

	return task, nil
}

// InsertEvent inserts a new event into the buffer with sync_status='pending'.
func (b *SQLiteBuffer) InsertEvent(event core.Event) error {
	retryOf := sqlNullString(nil)
	if event.RetryOf != nil {
		s := event.RetryOf.String()
		retryOf = sqlNullString(&s)
	}

	isRetry := 0
	if event.IsRetry {
		isRetry = 1
	}

	detailsJSON, err := json.Marshal(event.Details)
	if err != nil {
		detailsJSON = []byte("{}")
	}

	_, err = b.db.Exec(`INSERT INTO events (
		event_id, task_id, event_type, occurred_at,
		cost_usd, cost_confidence, pricing_source, pricing_version,
		service_name, provider, model,
		input_tokens, output_tokens, cached_tokens, latency_ms,
		is_retry, retry_reason, retry_of,
		details, schema_version, sync_status
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		event.EventID.String(),
		event.TaskID.String(),
		string(event.EventType),
		event.OccurredAt.Format(time.RFC3339Nano),
		event.CostUSD.String(),
		string(event.CostConfidence),
		string(event.PricingSource),
		event.PricingVersion,
		event.ServiceName,
		event.Provider,
		event.Model,
		nullIntPtr(event.InputTokens),
		nullIntPtr(event.OutputTokens),
		nullIntPtr(event.CachedTokens),
		nullIntPtr(event.LatencyMs),
		isRetry,
		event.RetryReason,
		retryOf,
		string(detailsJSON),
		event.SchemaVersion,
		"pending",
	)
	return err
}

// UpdateEvent updates an existing event in the buffer (matched by event_id).
func (b *SQLiteBuffer) UpdateEvent(event core.Event) error {
	retryOf := sqlNullString(nil)
	if event.RetryOf != nil {
		s := event.RetryOf.String()
		retryOf = sqlNullString(&s)
	}

	isRetry := 0
	if event.IsRetry {
		isRetry = 1
	}

	detailsJSON, err := json.Marshal(event.Details)
	if err != nil {
		detailsJSON = []byte("{}")
	}

	_, err = b.db.Exec(`UPDATE events SET
		task_id = ?, event_type = ?, occurred_at = ?,
		cost_usd = ?, cost_confidence = ?, pricing_source = ?, pricing_version = ?,
		service_name = ?, provider = ?, model = ?,
		input_tokens = ?, output_tokens = ?, cached_tokens = ?, latency_ms = ?,
		is_retry = ?, retry_reason = ?, retry_of = ?,
		details = ?, schema_version = ?
	WHERE event_id = ?`,
		event.TaskID.String(),
		string(event.EventType),
		event.OccurredAt.Format(time.RFC3339Nano),
		event.CostUSD.String(),
		string(event.CostConfidence),
		string(event.PricingSource),
		event.PricingVersion,
		event.ServiceName,
		event.Provider,
		event.Model,
		nullIntPtr(event.InputTokens),
		nullIntPtr(event.OutputTokens),
		nullIntPtr(event.CachedTokens),
		nullIntPtr(event.LatencyMs),
		isRetry,
		event.RetryReason,
		retryOf,
		string(detailsJSON),
		event.SchemaVersion,
		event.EventID.String(),
	)
	return err
}

// QueryEvents retrieves all events for a given task ID.
func (b *SQLiteBuffer) QueryEvents(taskID string) ([]core.Event, error) {
	rows, err := b.db.Query(`SELECT
		event_id, task_id, event_type, occurred_at,
		cost_usd, cost_confidence, pricing_source, pricing_version,
		service_name, provider, model,
		input_tokens, output_tokens, cached_tokens, latency_ms,
		is_retry, retry_reason, retry_of,
		details, schema_version
	FROM events WHERE task_id = ? ORDER BY occurred_at`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEvents(rows)
}

// QueryPendingEvents retrieves up to `limit` events with sync_status='pending'.
func (b *SQLiteBuffer) QueryPendingEvents(limit int) ([]core.Event, error) {
	rows, err := b.db.Query(`SELECT
		event_id, task_id, event_type, occurred_at,
		cost_usd, cost_confidence, pricing_source, pricing_version,
		service_name, provider, model,
		input_tokens, output_tokens, cached_tokens, latency_ms,
		is_retry, retry_reason, retry_of,
		details, schema_version
	FROM events WHERE sync_status = 'pending' ORDER BY occurred_at LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEvents(rows)
}

// MarkSynced updates sync_status to 'synced' for the given event IDs.
func (b *SQLiteBuffer) MarkSynced(eventIDs []string) error {
	if len(eventIDs) == 0 {
		return nil
	}
	placeholders := make([]string, len(eventIDs))
	args := make([]interface{}, len(eventIDs))
	for i, id := range eventIDs {
		placeholders[i] = "?"
		args[i] = id
	}
	query := fmt.Sprintf(
		"UPDATE events SET sync_status = 'synced' WHERE event_id IN (%s)",
		strings.Join(placeholders, ","),
	)
	_, err := b.db.Exec(query, args...)
	return err
}

// InsertTaskWithEvents inserts a task and its events atomically within a
// single database transaction. If any insert fails the entire operation is
// rolled back, leaving the database unchanged. This satisfies the
// core.TransactionalBuffer interface.
func (b *SQLiteBuffer) InsertTaskWithEvents(task core.Task, events []core.Event) error {
	tx, err := b.db.Begin()
	if err != nil {
		return fmt.Errorf("begin transaction: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after commit

	// Insert the task.
	endedAt := sqlNullString(nil)
	if task.EndedAt != nil {
		s := task.EndedAt.Format(time.RFC3339Nano)
		endedAt = sqlNullString(&s)
	}
	parentTaskID := sqlNullString(nil)
	if task.ParentTaskID != nil {
		s := task.ParentTaskID.String()
		parentTaskID = sqlNullString(&s)
	}

	_, err = tx.Exec(`INSERT INTO tasks (
		task_id, task_type, status, started_at, ended_at, metadata,
		customer_id, project_id, parent_task_id,
		experiment_id, variant,
		llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
		total_input_tokens, total_output_tokens, total_cached_tokens,
		retry_count, retry_cost_usd, failure_count, schema_version
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		task.TaskID.String(),
		task.TaskType,
		string(task.Status),
		task.StartedAt.Format(time.RFC3339Nano),
		endedAt,
		marshalMetadata(task.Metadata),
		task.CustomerID,
		task.ProjectID,
		parentTaskID,
		task.ExperimentID,
		task.Variant,
		task.LLMCostUSD.String(),
		task.ExternalCostUSD.String(),
		task.ComputeCostUSD.String(),
		task.TotalCostUSD.String(),
		task.TotalInputTokens,
		task.TotalOutputTokens,
		task.TotalCachedTokens,
		task.RetryCount,
		task.RetryCostUSD.String(),
		task.FailureCount,
		task.SchemaVersion,
	)
	if err != nil {
		return fmt.Errorf("insert task: %w", err)
	}

	// Insert each event.
	for i, event := range events {
		retryOf := sqlNullString(nil)
		if event.RetryOf != nil {
			s := event.RetryOf.String()
			retryOf = sqlNullString(&s)
		}

		isRetry := 0
		if event.IsRetry {
			isRetry = 1
		}

		detailsJSON, jerr := json.Marshal(event.Details)
		if jerr != nil {
			detailsJSON = []byte("{}")
		}

		_, err = tx.Exec(`INSERT INTO events (
			event_id, task_id, event_type, occurred_at,
			cost_usd, cost_confidence, pricing_source, pricing_version,
			service_name, provider, model,
			input_tokens, output_tokens, cached_tokens, latency_ms,
			is_retry, retry_reason, retry_of,
			details, schema_version, sync_status
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			event.EventID.String(),
			event.TaskID.String(),
			string(event.EventType),
			event.OccurredAt.Format(time.RFC3339Nano),
			event.CostUSD.String(),
			string(event.CostConfidence),
			string(event.PricingSource),
			event.PricingVersion,
			event.ServiceName,
			event.Provider,
			event.Model,
			nullIntPtr(event.InputTokens),
			nullIntPtr(event.OutputTokens),
			nullIntPtr(event.CachedTokens),
			nullIntPtr(event.LatencyMs),
			isRetry,
			event.RetryReason,
			retryOf,
			string(detailsJSON),
			event.SchemaVersion,
			"pending",
		)
		if err != nil {
			return fmt.Errorf("insert event %d: %w", i, err)
		}
	}

	return tx.Commit()
}

// Close closes the underlying database connection.
func (b *SQLiteBuffer) Close() error {
	return b.db.Close()
}

// QueryTasksByIDs retrieves tasks matching the given task IDs.
func (b *SQLiteBuffer) QueryTasksByIDs(taskIDs []string) ([]core.Task, error) {
	if len(taskIDs) == 0 {
		return nil, nil
	}
	placeholders := make([]string, len(taskIDs))
	args := make([]interface{}, len(taskIDs))
	for i, id := range taskIDs {
		placeholders[i] = "?"
		args[i] = id
	}
	query := fmt.Sprintf(`SELECT
		task_id, task_type, status, started_at, ended_at, metadata,
		customer_id, project_id, parent_task_id,
		experiment_id, variant,
		llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
		total_input_tokens, total_output_tokens, total_cached_tokens,
		retry_count, retry_cost_usd, failure_count, schema_version
	FROM tasks WHERE task_id IN (%s)`, strings.Join(placeholders, ","))
	rows, err := b.db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanTasks(rows)
}

// MarkTasksSynced updates sync_status to 'synced' for the given task IDs.
func (b *SQLiteBuffer) MarkTasksSynced(taskIDs []string) error {
	if len(taskIDs) == 0 {
		return nil
	}
	placeholders := make([]string, len(taskIDs))
	args := make([]interface{}, len(taskIDs))
	for i, id := range taskIDs {
		placeholders[i] = "?"
		args[i] = id
	}
	query := fmt.Sprintf(
		"UPDATE tasks SET sync_status = 'synced' WHERE task_id IN (%s)",
		strings.Join(placeholders, ","),
	)
	_, err := b.db.Exec(query, args...)
	return err
}

// PurgeSyncedEvents deletes events with sync_status='synced' older than before.
// Returns the number of rows deleted.
func (b *SQLiteBuffer) PurgeSyncedEvents(before time.Time) (int64, error) {
	result, err := b.db.Exec(
		"DELETE FROM events WHERE sync_status = 'synced' AND occurred_at < ?",
		before.Format(time.RFC3339Nano),
	)
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}

// PurgeOldPendingEvents deletes events still in sync_status='pending' that are
// older than before — events that have failed to sync long enough to be
// considered abandoned. Without this, a permanently failing sync (e.g. a
// rejected API key) leaves the buffer growing forever. Returns the number of
// rows deleted. Mirrors Python storage/sqlite.py purge_old_pending.
func (b *SQLiteBuffer) PurgeOldPendingEvents(before time.Time) (int64, error) {
	result, err := b.db.Exec(
		"DELETE FROM events WHERE sync_status = 'pending' AND occurred_at < ?",
		before.Format(time.RFC3339Nano),
	)
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}

// scanTasks reads rows into a slice of core.Task.
func scanTasks(rows *sql.Rows) ([]core.Task, error) {
	var tasks []core.Task
	for rows.Next() {
		var (
			id, taskType, status, startedAt             string
			endedAt, metadataStr, customerID, projectID sql.NullString
			parentTaskID                                sql.NullString
			experimentID, variant                       sql.NullString
			llmCost, extCost, compCost                  string
			totalCost                                   string
			inTok, outTok, cacheTok                     int
			retryCnt, failCnt                           int
			retryCost                                   string
			schemaVer                                   string
		)
		err := rows.Scan(
			&id, &taskType, &status, &startedAt, &endedAt, &metadataStr,
			&customerID, &projectID, &parentTaskID,
			&experimentID, &variant,
			&llmCost, &extCost, &compCost, &totalCost,
			&inTok, &outTok, &cacheTok,
			&retryCnt, &retryCost, &failCnt, &schemaVer,
		)
		if err != nil {
			return nil, err
		}

		taskUUID, _ := uuid.Parse(id)
		started, _ := time.Parse(time.RFC3339Nano, startedAt)

		task := core.Task{
			TaskID:            taskUUID,
			TaskType:          taskType,
			Status:            core.TaskStatus(status),
			StartedAt:         started,
			Metadata:          unmarshalMetadata(metadataStr.String),
			LLMCostUSD:        decimal.RequireFromString(llmCost),
			ExternalCostUSD:   decimal.RequireFromString(extCost),
			ComputeCostUSD:    decimal.RequireFromString(compCost),
			TotalCostUSD:      decimal.RequireFromString(totalCost),
			TotalInputTokens:  inTok,
			TotalOutputTokens: outTok,
			TotalCachedTokens: cacheTok,
			RetryCount:        retryCnt,
			RetryCostUSD:      decimal.RequireFromString(retryCost),
			FailureCount:      failCnt,
			SchemaVersion:     schemaVer,
		}

		if endedAt.Valid {
			t, _ := time.Parse(time.RFC3339Nano, endedAt.String)
			task.EndedAt = &t
		}
		if customerID.Valid {
			task.CustomerID = customerID.String
		}
		if projectID.Valid {
			task.ProjectID = projectID.String
		}
		if parentTaskID.Valid && parentTaskID.String != "" {
			pid, _ := uuid.Parse(parentTaskID.String)
			task.ParentTaskID = &pid
		}
		if experimentID.Valid {
			task.ExperimentID = experimentID.String
		}
		if variant.Valid {
			task.Variant = variant.String
		}

		tasks = append(tasks, task)
	}
	return tasks, rows.Err()
}

// scanEvents reads rows into a slice of core.Event.
func scanEvents(rows *sql.Rows) ([]core.Event, error) {
	var events []core.Event
	for rows.Next() {
		var (
			eid, tid, etype, occurredAt string
			costStr, confStr            string
			pricSrc, pricVer            string
			svcName, provider, model    string
			inTok, outTok, cacheTok     sql.NullInt64
			latMs                       sql.NullInt64
			isRetryInt                  int
			retryReason                 string
			retryOf                     sql.NullString
			detailsStr                  string
			schemaVer                   string
		)
		err := rows.Scan(
			&eid, &tid, &etype, &occurredAt,
			&costStr, &confStr, &pricSrc, &pricVer,
			&svcName, &provider, &model,
			&inTok, &outTok, &cacheTok, &latMs,
			&isRetryInt, &retryReason, &retryOf,
			&detailsStr, &schemaVer,
		)
		if err != nil {
			return nil, err
		}

		eventUUID, _ := uuid.Parse(eid)
		taskUUID, _ := uuid.Parse(tid)
		occurred, _ := time.Parse(time.RFC3339Nano, occurredAt)

		details := make(map[string]interface{})
		if detailsStr != "" {
			if umErr := json.Unmarshal([]byte(detailsStr), &details); umErr != nil {
				// Corrupt details — use empty map
				details = make(map[string]interface{})
			}
		}

		event := core.Event{
			EventID:        eventUUID,
			TaskID:         taskUUID,
			EventType:      core.EventType(etype),
			OccurredAt:     occurred,
			CostUSD:        decimal.RequireFromString(costStr),
			CostConfidence: core.CostConfidence(confStr),
			PricingSource:  core.PricingSource(pricSrc),
			PricingVersion: pricVer,
			ServiceName:    svcName,
			Provider:       provider,
			Model:          model,
			IsRetry:        isRetryInt != 0,
			RetryReason:    retryReason,
			Details:        details,
			SchemaVersion:  schemaVer,
		}

		if inTok.Valid {
			v := int(inTok.Int64)
			event.InputTokens = &v
		}
		if outTok.Valid {
			v := int(outTok.Int64)
			event.OutputTokens = &v
		}
		if cacheTok.Valid {
			v := int(cacheTok.Int64)
			event.CachedTokens = &v
		}
		if latMs.Valid {
			v := int(latMs.Int64)
			event.LatencyMs = &v
		}
		if retryOf.Valid && retryOf.String != "" {
			rid, _ := uuid.Parse(retryOf.String)
			event.RetryOf = &rid
		}

		events = append(events, event)
	}
	return events, rows.Err()
}

// marshalMetadata serialises a task metadata map to a JSON string for the
// `metadata` column, falling back to "{}" on nil/empty input or a marshal
// error. Without this, task metadata (including _trace_links and session
// flags) would never reach SQLite.
func marshalMetadata(m map[string]interface{}) string {
	if len(m) == 0 {
		return "{}"
	}
	b, err := json.Marshal(m)
	if err != nil {
		return "{}"
	}
	return string(b)
}

// unmarshalMetadata parses a `metadata` column value into a map, returning a
// non-nil empty map on empty input or a parse error.
func unmarshalMetadata(s string) map[string]interface{} {
	m := make(map[string]interface{})
	if s == "" || s == "{}" {
		return m
	}
	if err := json.Unmarshal([]byte(s), &m); err != nil {
		return make(map[string]interface{})
	}
	return m
}

// sqlNullString converts a *string to sql.NullString.
func sqlNullString(s *string) sql.NullString {
	if s == nil {
		return sql.NullString{}
	}
	return sql.NullString{String: *s, Valid: true}
}

// nullIntPtr converts an *int to sql.NullInt64.
func nullIntPtr(p *int) sql.NullInt64 {
	if p == nil {
		return sql.NullInt64{}
	}
	return sql.NullInt64{Int64: int64(*p), Valid: true}
}
