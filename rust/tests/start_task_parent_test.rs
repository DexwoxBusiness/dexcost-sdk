//! Fix 2 — a task started *inside* another task's scope must have its
//! `parent_task_id` linked automatically.
//!
//! `dexcost::init` uses a process-wide `OnceLock`, so this lives in its own
//! test binary and calls `init` exactly once. The buffer is pointed at a
//! temp file to keep the test hermetic.

use dexcost::{init, start_task, with_task, Config, TaskOptions, TaskStatus};

#[tokio::test]
async fn nested_start_task_auto_links_parent() {
    // Hermetic buffer — do not touch ~/.dexcost/buffer.db.
    let dir = tempfile::tempdir().expect("tempdir");
    let buffer_path = dir.path().join("buffer.db");
    init(Config {
        buffer_path: Some(buffer_path),
        ..Config::default()
    })
    .expect("init should succeed");

    // Start a parent task.
    let mut parent = start_task("parent_task", TaskOptions::default())
        .await
        .expect("start parent");
    let parent_id = parent.task().task_id.clone();

    // A task started inside `parent.scope(...)` discovers `parent` as its
    // parent via the task-local context — no manual `parent_task_id` needed.
    let child_id = parent
        .scope(async {
            let mut child = start_task("child_task", TaskOptions::default())
                .await
                .expect("start child");

            assert_eq!(
                child.task().parent_task_id.as_deref(),
                Some(parent_id.as_str()),
                "child started inside the parent scope must auto-link parent_task_id"
            );

            // A grandchild started inside the child's scope links to the child.
            let grandchild_parent = child
                .scope(async {
                    let mut grandchild = start_task("grandchild_task", TaskOptions::default())
                        .await
                        .expect("start grandchild");
                    let gp = grandchild.task().parent_task_id.clone();
                    grandchild.end(TaskStatus::Success).await.expect("end gc");
                    gp
                })
                .await;

            let child_id = child.task().task_id.clone();
            assert_eq!(
                grandchild_parent.as_deref(),
                Some(child_id.as_str()),
                "grandchild must link to the child, not the parent"
            );

            child.end(TaskStatus::Success).await.expect("end child");
            child_id
        })
        .await;

    assert_ne!(child_id, parent_id);

    // The same auto-linking works through the free `with_task` helper, which
    // is now re-exported from the crate root.
    let scoped_child_parent = with_task(parent.task().clone(), async {
        let mut child = start_task("with_task_child", TaskOptions::default())
            .await
            .expect("start with_task child");
        let p = child.task().parent_task_id.clone();
        child.end(TaskStatus::Success).await.expect("end");
        p
    })
    .await;
    assert_eq!(
        scoped_child_parent.as_deref(),
        Some(parent_id.as_str()),
        "with_task scoping must also auto-link parent_task_id"
    );

    // A task started OUTSIDE any scope has no parent.
    let mut orphan = start_task("orphan_task", TaskOptions::default())
        .await
        .expect("start orphan");
    assert!(
        orphan.task().parent_task_id.is_none(),
        "a task started outside any scope must not have a parent"
    );

    // An explicit parent_task_id is still honoured over the ambient context.
    let explicit_parent = parent
        .scope(async {
            let mut child = start_task(
                "explicit_parent_child",
                TaskOptions {
                    parent_task_id: Some("explicit-parent-id".to_string()),
                    ..Default::default()
                },
            )
            .await
            .expect("start explicit child");
            let p = child.task().parent_task_id.clone();
            child.end(TaskStatus::Success).await.expect("end");
            p
        })
        .await;
    assert_eq!(
        explicit_parent.as_deref(),
        Some("explicit-parent-id"),
        "an explicit parent_task_id must win over the task-local context"
    );

    orphan.end(TaskStatus::Success).await.expect("end orphan");
    parent.end(TaskStatus::Success).await.expect("end parent");
}
