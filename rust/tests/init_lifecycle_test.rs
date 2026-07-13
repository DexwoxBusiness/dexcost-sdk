use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use dexcost::{close, init, Config, DexcostError};
use tempfile::tempdir;
use tokio::net::TcpListener;

#[tokio::test]
async fn rejected_second_init_does_not_start_pricing_refresh() {
    let first_dir = tempdir().unwrap();
    let first = Config {
        api_key: Some("dx_test_first".to_string()),
        endpoint: Some("http://127.0.0.1:1".to_string()),
        buffer_path: Some(first_dir.path().join("buffer.db")),
        track_http: false,
        ..Config::default()
    };
    init(first).expect("first initialization should succeed");

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let accepted = Arc::new(AtomicUsize::new(0));
    let accepted_by_server = Arc::clone(&accepted);
    let server = tokio::spawn(async move {
        if matches!(
            tokio::time::timeout(Duration::from_millis(250), listener.accept()).await,
            Ok(Ok(_))
        ) {
            accepted_by_server.fetch_add(1, Ordering::SeqCst);
        }
    });

    let second_dir = tempdir().unwrap();
    let second = Config {
        api_key: Some("dx_test_rejected".to_string()),
        endpoint: Some(endpoint),
        buffer_path: Some(second_dir.path().join("buffer.db")),
        track_http: false,
        ..Config::default()
    };
    assert!(matches!(
        init(second),
        Err(DexcostError::AlreadyInitialized)
    ));

    server.await.unwrap();
    assert_eq!(
        accepted.load(Ordering::SeqCst),
        0,
        "a rejected init must not spawn an orphan pricing refresh worker"
    );
    close();
}
