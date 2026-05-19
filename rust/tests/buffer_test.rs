use dexcost::core::models::{CostEvent, EventType, Task};
use dexcost::transport::buffer::EventBuffer;
use rust_decimal::Decimal;

#[test]
fn test_empty_buffer() {
    let buffer = EventBuffer::new().unwrap();
    assert_eq!(buffer.event_count(), 0);
    assert_eq!(buffer.task_count(), 0);
    assert_eq!(buffer.pending_count(), 0);
}

#[test]
fn test_add_event() {
    let mut buffer = EventBuffer::new().unwrap();
    let event = CostEvent::new("task-1", EventType::LlmCall);
    buffer.add_event(event);

    assert_eq!(buffer.event_count(), 1);
    assert_eq!(buffer.pending_count(), 1);
}

#[test]
fn test_add_multiple_events() {
    let mut buffer = EventBuffer::new().unwrap();

    for i in 0..10 {
        let event = CostEvent::new(&format!("task-{}", i), EventType::LlmCall);
        buffer.add_event(event);
    }

    assert_eq!(buffer.event_count(), 10);
    assert_eq!(buffer.pending_count(), 10);
}

#[test]
fn test_upsert_task_insert() {
    let mut buffer = EventBuffer::new().unwrap();
    let task = Task::new("test_type");
    let task_id = task.task_id.clone();

    buffer.upsert_task(task);
    assert_eq!(buffer.task_count(), 1);

    let retrieved = buffer.get_task(&task_id).unwrap();
    assert_eq!(retrieved.task_type, "test_type");
}

#[test]
fn test_upsert_task_update() {
    let mut buffer = EventBuffer::new().unwrap();
    let mut task = Task::new("test_type");
    let task_id = task.task_id.clone();

    buffer.upsert_task(task.clone());
    assert_eq!(
        buffer.get_task(&task_id).unwrap().llm_cost_usd,
        Decimal::ZERO
    );

    task.llm_cost_usd = Decimal::new(100, 2); // 1.00
    buffer.upsert_task(task);

    assert_eq!(
        buffer.get_task(&task_id).unwrap().llm_cost_usd,
        Decimal::new(100, 2)
    );
}

#[test]
fn test_get_task_not_found() {
    let buffer = EventBuffer::new().unwrap();
    assert!(buffer.get_task("nonexistent").is_none());
}

#[test]
fn test_get_pending_events_limit() {
    let mut buffer = EventBuffer::new().unwrap();

    for _ in 0..5 {
        buffer.add_event(CostEvent::new("task-1", EventType::LlmCall));
    }

    let pending = buffer.get_pending_events(3);
    assert_eq!(pending.len(), 3);

    let pending_all = buffer.get_pending_events(100);
    assert_eq!(pending_all.len(), 5);
}

#[test]
fn test_mark_synced() {
    let mut buffer = EventBuffer::new().unwrap();

    let e1 = CostEvent::new("task-1", EventType::LlmCall);
    let e2 = CostEvent::new("task-1", EventType::ExternalCost);
    let e3 = CostEvent::new("task-1", EventType::ComputeCost);

    let id1 = e1.event_id.clone();
    let id2 = e2.event_id.clone();

    buffer.add_event(e1);
    buffer.add_event(e2);
    buffer.add_event(e3);

    assert_eq!(buffer.pending_count(), 3);

    buffer.mark_synced(&[id1, id2]);

    assert_eq!(buffer.pending_count(), 1);
    assert_eq!(buffer.event_count(), 3); // total doesn't change
}

#[test]
fn test_mark_synced_empty_ids() {
    let mut buffer = EventBuffer::new().unwrap();
    buffer.add_event(CostEvent::new("task-1", EventType::LlmCall));

    buffer.mark_synced(&[]);
    assert_eq!(buffer.pending_count(), 1);
}

#[test]
fn test_all_events() {
    let mut buffer = EventBuffer::new().unwrap();

    buffer.add_event(CostEvent::new("task-1", EventType::LlmCall));
    buffer.add_event(CostEvent::new("task-1", EventType::ExternalCost));

    let all = buffer.all_events();
    assert_eq!(all.len(), 2);
    assert_eq!(all[0].event_type, EventType::LlmCall);
    assert_eq!(all[1].event_type, EventType::ExternalCost);
}

#[test]
fn test_pending_events_excludes_synced() {
    let mut buffer = EventBuffer::new().unwrap();

    let e1 = CostEvent::new("task-1", EventType::LlmCall);
    let id1 = e1.event_id.clone();
    buffer.add_event(e1);
    buffer.add_event(CostEvent::new("task-1", EventType::ExternalCost));

    buffer.mark_synced(std::slice::from_ref(&id1));

    let pending = buffer.get_pending_events(10);
    assert_eq!(pending.len(), 1);
    // The pending event should not be the synced one
    assert_ne!(pending[0].event_id, id1);
}

// ---------------------------------------------------------------------------
// DEX-297 — sync_status on tasks + get_pending_tasks / mark_tasks_synced
// ---------------------------------------------------------------------------

#[test]
fn test_get_pending_tasks_returns_freshly_upserted() {
    let mut buffer = EventBuffer::new().unwrap();
    let task = Task::new("resolve_ticket");
    let task_id = task.task_id.clone();
    buffer.upsert_task(task);

    let pending = buffer.get_pending_tasks(10);
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].task_id, task_id);
    assert_eq!(buffer.pending_task_count(), 1);
}

#[test]
fn test_mark_tasks_synced_clears_pending() {
    let mut buffer = EventBuffer::new().unwrap();
    let task = Task::new("resolve_ticket");
    let task_id = task.task_id.clone();
    buffer.upsert_task(task);
    assert_eq!(buffer.pending_task_count(), 1);

    buffer.mark_tasks_synced(std::slice::from_ref(&task_id));
    assert_eq!(buffer.pending_task_count(), 0);
    assert!(buffer.get_pending_tasks(10).is_empty());
}

#[test]
fn test_upsert_resets_sync_status_to_pending() {
    // This is the core DEX-297 behaviour: end_task / total recompute
    // re-upserts the task, which must mark it pending again so the next
    // flush includes the updated state.
    let mut buffer = EventBuffer::new().unwrap();
    let mut task = Task::new("resolve_ticket");
    let task_id = task.task_id.clone();
    buffer.upsert_task(task.clone());

    // Simulate first flush: task gets synced.
    buffer.mark_tasks_synced(std::slice::from_ref(&task_id));
    assert_eq!(buffer.pending_task_count(), 0);

    // Simulate end_task: status flips, totals updated, re-upsert.
    task.llm_cost_usd = Decimal::new(1234, 4); // 0.1234
    buffer.upsert_task(task);

    // Task must be pending again so the pusher re-sends it.
    assert_eq!(buffer.pending_task_count(), 1);
    let pending = buffer.get_pending_tasks(10);
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].task_id, task_id);
    assert_eq!(pending[0].llm_cost_usd, Decimal::new(1234, 4));
}

#[test]
fn test_get_pending_tasks_respects_limit() {
    let mut buffer = EventBuffer::new().unwrap();
    for _ in 0..5 {
        buffer.upsert_task(Task::new("type_a"));
    }
    let limited = buffer.get_pending_tasks(2);
    assert_eq!(limited.len(), 2);
    assert_eq!(buffer.pending_task_count(), 5);
}

#[test]
fn test_get_tasks_by_ids() {
    let mut buffer = EventBuffer::new().unwrap();
    let t1 = Task::new("type_a");
    let t2 = Task::new("type_b");
    let t3 = Task::new("type_c");
    let id1 = t1.task_id.clone();
    let id3 = t3.task_id.clone();
    buffer.upsert_task(t1);
    buffer.upsert_task(t2);
    buffer.upsert_task(t3);

    let fetched = buffer.get_tasks_by_ids(&[id1.clone(), id3.clone()]);
    assert_eq!(fetched.len(), 2);
    let ids: Vec<_> = fetched.iter().map(|t| t.task_id.clone()).collect();
    assert!(ids.contains(&id1));
    assert!(ids.contains(&id3));

    // Empty input returns empty result
    assert!(buffer.get_tasks_by_ids(&[]).is_empty());
}

#[test]
fn test_sync_status_migration_on_existing_db() {
    // Create a buffer at v1 schema (no sync_status), then reopen with the
    // current code and verify the column was added without losing data.
    let dir = tempfile::tempdir().expect("tempdir");
    let db_path = dir.path().join("legacy.db");
    let db_path_str = db_path.to_str().unwrap();

    {
        // Simulate a pre-DEX-297 database: open via raw rusqlite, create
        // the tasks table with the OLD schema (no sync_status column),
        // then insert a row.
        use rusqlite::Connection;
        let conn = Connection::open(db_path_str).unwrap();
        conn.execute_batch(
            "
            CREATE TABLE tasks (
                task_id             TEXT PRIMARY KEY,
                task_type           TEXT NOT NULL,
                status              TEXT NOT NULL,
                started_at          TEXT NOT NULL,
                ended_at            TEXT,
                metadata            TEXT,
                llm_cost_usd        TEXT,
                external_cost_usd   TEXT,
                compute_cost_usd    TEXT,
                total_cost_usd      TEXT,
                total_input_tokens  INTEGER,
                total_output_tokens INTEGER,
                total_cached_tokens INTEGER,
                retry_count         INTEGER DEFAULT 0,
                retry_cost_usd      TEXT    DEFAULT '0',
                failure_count       INTEGER DEFAULT 0,
                customer_id         TEXT,
                project_id          TEXT,
                parent_task_id      TEXT,
                experiment_id       TEXT,
                variant             TEXT
            );
            INSERT INTO tasks (task_id, task_type, status, started_at,
                               llm_cost_usd, external_cost_usd, compute_cost_usd,
                               total_cost_usd, retry_cost_usd)
            VALUES ('legacy-1', 'old_type', 'pending', '2026-01-01T00:00:00+00:00',
                    '0', '0', '0', '0', '0');
            ",
        )
        .unwrap();
    }

    // Reopen via EventBuffer::open — migration should run.
    let buffer = EventBuffer::open(db_path_str).expect("reopen");
    assert_eq!(buffer.task_count(), 1);
    // Existing row gets the DEFAULT 'pending' value, so it shows up as pending.
    let pending = buffer.get_pending_tasks(10);
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].task_id, "legacy-1");
}
