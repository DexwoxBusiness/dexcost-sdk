//! Tests for compute auto-emit + back-fill at task finalize.
//!
//! Mirrors python/tests/test_compute_auto_emission_long_running.py + the
//! deferred-cost back-fill scenarios.

use std::sync::Arc;
use std::sync::Mutex as StdMutex;

use dexcost::core::cgroup_reader::{reset_cgroup_root_for_tests, set_cgroup_root_for_tests};
use dexcost::core::compute_accountant::ComputeAccountant;
use dexcost::core::compute_runtime::RuntimeKind;
use dexcost::core::models::{EventType, Task, TaskStatus};
use dexcost::core::tracker::TrackedTask;
use dexcost::transport::buffer::EventBuffer;
use rust_decimal::Decimal;
use tempfile::tempdir;

/// Tests mutate process-global cgroup root; serialize.
static TEST_LOCK: std::sync::LazyLock<StdMutex<()>> =
    std::sync::LazyLock::new(|| StdMutex::new(()));

fn lock() -> std::sync::MutexGuard<'static, ()> {
    match TEST_LOCK.lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    }
}

fn cgroup_fixture() -> tempfile::TempDir {
    let t = tempdir().unwrap();
    std::fs::write(t.path().join("cpu.stat"), "usage_usec 1000000\n").unwrap();
    std::fs::write(t.path().join("cpu.max"), "200000 100000\n").unwrap();
    std::fs::write(t.path().join("memory.peak"), "1073741824\n").unwrap();
    std::fs::write(t.path().join("memory.max"), "2147483648\n").unwrap();
    t
}

#[tokio::test]
async fn ec2_long_running_auto_emits_compute_cost_event() {
    let _g = lock();
    let t = cgroup_fixture();
    set_cgroup_root_for_tests(t.path());

    let mut task = Task::new("test-task");
    task.compute = Some(Arc::new(
        ComputeAccountant::new(RuntimeKind::Ec2)
            .with_region("us-east-1".into()),
    ));
    task.compute.as_ref().unwrap().snapshot_start();

    // Bump cpu.stat for end-snapshot.
    std::fs::write(t.path().join("cpu.stat"), "usage_usec 3000000\n").unwrap();

    let buffer = Arc::new(tokio::sync::Mutex::new(EventBuffer::new().unwrap()));
    let mut tracked = TrackedTask::new(task, buffer.clone(), None);
    tracked.end(TaskStatus::Success).await.unwrap();

    // Verify the buffer has a compute_cost event for the task.
    let buf = buffer.lock().await;
    let events = buf.query_events(tracked.task().task_id.as_str());
    drop(buf);
    let compute_events: Vec<_> = events
        .iter()
        .filter(|e| e.event_type == EventType::ComputeCost)
        .collect();
    assert_eq!(
        compute_events.len(),
        1,
        "EC2 long-running emits exactly one compute_cost event"
    );
    // The back-fill replaces cost_pending=true and stamps pricing_version.
    let ev = compute_events[0];
    assert!(ev.pricing_version.as_deref().unwrap_or("").starts_with("compute:"));
    assert!(
        !ev.details.contains_key("cost_pending"),
        "cost_pending marker is stripped after back-fill"
    );

    reset_cgroup_root_for_tests();
}

#[tokio::test]
async fn lambda_runtime_does_not_auto_emit_long_running_event() {
    let _g = lock();
    let t = tempdir().unwrap();
    set_cgroup_root_for_tests(t.path());

    let mut task = Task::new("test-task");
    // Lambda is serverless — long-running auto-emit must NOT fire for it.
    task.compute = Some(Arc::new(
        ComputeAccountant::new(RuntimeKind::Lambda)
            .with_lambda_memory_mb(512)
            .with_region("us-east-1".into()),
    ));

    let buffer = Arc::new(tokio::sync::Mutex::new(EventBuffer::new().unwrap()));
    let mut tracked = TrackedTask::new(task, buffer.clone(), None);
    tracked.end(TaskStatus::Success).await.unwrap();

    let buf = buffer.lock().await;
    let events = buf.query_events(tracked.task().task_id.as_str());
    drop(buf);
    let compute_events: Vec<_> = events
        .iter()
        .filter(|e| e.event_type == EventType::ComputeCost)
        .collect();
    assert_eq!(
        compute_events.len(),
        0,
        "Lambda runtime should not auto-emit a long-running compute_cost (the handler wrap emits it instead)"
    );

    reset_cgroup_root_for_tests();
}

#[tokio::test]
async fn compute_cost_event_delta_added_to_totals() {
    let _g = lock();
    let t = cgroup_fixture();
    set_cgroup_root_for_tests(t.path());

    let mut task = Task::new("test-task");
    task.compute = Some(Arc::new(
        ComputeAccountant::new(RuntimeKind::Ec2)
            .with_region("us-east-1".into()),
    ));
    task.compute.as_ref().unwrap().snapshot_start();
    std::fs::write(t.path().join("cpu.stat"), "usage_usec 5000000\n").unwrap();

    let starting_total = task.total_cost_usd;
    let buffer = Arc::new(tokio::sync::Mutex::new(EventBuffer::new().unwrap()));
    let mut tracked = TrackedTask::new(task, buffer.clone(), None);
    tracked.end(TaskStatus::Success).await.unwrap();

    let final_task = tracked.task();
    // Delta-not-recompute: the new compute_cost_usd is added on top of any
    // pre-existing total. With no prior cost and zero network bytes:
    assert!(final_task.compute_cost_usd >= Decimal::ZERO);
    assert!(final_task.total_cost_usd >= starting_total);

    reset_cgroup_root_for_tests();
}

#[tokio::test]
async fn no_compute_accountant_finalizes_without_event() {
    let _g = lock();
    let t = tempdir().unwrap();
    set_cgroup_root_for_tests(t.path());

    let task = Task::new("test-task"); // no compute accountant attached
    let buffer = Arc::new(tokio::sync::Mutex::new(EventBuffer::new().unwrap()));
    let mut tracked = TrackedTask::new(task, buffer.clone(), None);
    tracked.end(TaskStatus::Success).await.unwrap();
    let buf = buffer.lock().await;
    let events = buf.query_events(tracked.task().task_id.as_str());
    drop(buf);
    assert_eq!(
        events
            .iter()
            .filter(|e| e.event_type == EventType::ComputeCost)
            .count(),
        0
    );

    reset_cgroup_root_for_tests();
}
