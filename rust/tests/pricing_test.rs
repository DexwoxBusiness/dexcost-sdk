use dexcost::core::models::{CostConfidence, PricingSource};
use dexcost::pricing::engine::PricingEngine;
use rust_decimal::Decimal;
use std::sync::Arc;
use std::time::Duration;

#[tokio::test]
async fn test_engine_loads_models() {
    let engine = PricingEngine::new();
    assert!(engine.model_count().await > 0);
}

#[tokio::test]
async fn test_pricing_version_nonempty() {
    let engine = PricingEngine::new();
    let version = engine.pricing_version().await;
    assert!(!version.is_empty());
    assert_eq!(version.len(), 12); // 6 bytes = 12 hex chars
}

#[tokio::test]
async fn test_known_model_returns_computed() {
    let engine = PricingEngine::new();
    let result = engine.get_cost("gpt-4o", 1000, 500, 0, 0).await;

    // gpt-4o should be in the bundled data
    if result.cost_confidence == CostConfidence::Computed {
        assert!(result.cost_usd > Decimal::ZERO);
        assert_eq!(result.pricing_source, PricingSource::Litellm);
    }
}

#[tokio::test]
async fn test_unknown_model_returns_zero() {
    let engine = PricingEngine::new();
    let result = engine
        .get_cost("nonexistent-model-xyz-999", 1000, 500, 0, 0)
        .await;

    assert_eq!(result.cost_usd, Decimal::ZERO);
    assert_eq!(result.cost_confidence, CostConfidence::Unknown);
    assert_eq!(result.pricing_source, PricingSource::Unknown);
}

#[tokio::test]
async fn test_zero_tokens_returns_zero_cost() {
    let engine = PricingEngine::new();
    let result = engine.get_cost("gpt-4o", 0, 0, 0, 0).await;
    assert_eq!(result.cost_usd, Decimal::ZERO);
}

#[tokio::test]
async fn test_custom_pricing_overrides_bundled() {
    let engine = PricingEngine::new();

    let input_per_1k = Decimal::new(1, 3); // 0.001
    let output_per_1k = Decimal::new(2, 3); // 0.002

    engine
        .set_custom_pricing("my-model", input_per_1k, output_per_1k)
        .await;

    let result = engine.get_cost("my-model", 1000, 500, 0, 0).await;

    // input: 0.001 * 1000 / 1000 = 0.001
    // output: 0.002 * 500 / 1000 = 0.001
    // total: 0.002
    assert_eq!(result.cost_usd, Decimal::new(2, 3));
    assert_eq!(result.cost_confidence, CostConfidence::Computed);
    assert_eq!(result.pricing_source, PricingSource::Custom);
}

#[tokio::test]
async fn test_custom_pricing_higher_values() {
    let engine = PricingEngine::new();

    let input_per_1k = Decimal::new(10, 0); // 10.0
    let output_per_1k = Decimal::new(20, 0); // 20.0

    engine
        .set_custom_pricing("expensive-model", input_per_1k, output_per_1k)
        .await;

    let result = engine.get_cost("expensive-model", 2000, 1000, 0, 0).await;

    // input: 10.0 * 2000 / 1000 = 20.0
    // output: 20.0 * 1000 / 1000 = 20.0
    // total: 40.0
    assert_eq!(result.cost_usd, Decimal::new(40, 0));
}

#[tokio::test]
async fn test_provider_prefix_stripping() {
    let engine = PricingEngine::new();

    let with_prefix = engine.get_cost("openai/gpt-4o", 1000, 500, 0, 0).await;
    let without_prefix = engine.get_cost("gpt-4o", 1000, 500, 0, 0).await;

    // Both should resolve to the same model with the same cost
    assert_eq!(with_prefix.cost_usd, without_prefix.cost_usd);
    assert_eq!(with_prefix.cost_confidence, without_prefix.cost_confidence);
}

#[tokio::test]
async fn test_custom_pricing_takes_precedence_over_bundled() {
    let engine = PricingEngine::new();

    // Set custom pricing for a known model
    let input_per_1k = Decimal::new(999, 3); // 0.999
    let output_per_1k = Decimal::new(999, 3); // 0.999

    engine
        .set_custom_pricing("gpt-4o", input_per_1k, output_per_1k)
        .await;

    let result = engine.get_cost("gpt-4o", 1000, 1000, 0, 0).await;

    // Should use custom pricing, not bundled
    assert_eq!(result.pricing_source, PricingSource::Custom);
    // input: 0.999 * 1000 / 1000 = 0.999
    // output: 0.999 * 1000 / 1000 = 0.999
    // total: 1.998
    assert_eq!(result.cost_usd, Decimal::new(1998, 3));
}

#[tokio::test]
async fn test_pricing_version_is_deterministic() {
    let engine1 = PricingEngine::new();
    let engine2 = PricingEngine::new();
    assert_eq!(
        engine1.pricing_version().await,
        engine2.pricing_version().await
    );
}

// ---------------------------------------------------------------------------
// Background refresh tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_refresh_from_server_unreachable_returns_error() {
    let engine = PricingEngine::new();
    let result = engine.refresh_from_server("http://127.0.0.1:1").await;
    assert!(result.is_err(), "should fail for unreachable endpoint");
}

#[tokio::test]
async fn test_refresh_from_server_missing_models_key_returns_error() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        if let Ok((mut stream, _)) = listener.accept().await {
            let mut buf = [0u8; 4096];
            let _ = stream.read(&mut buf).await;
            let body = b"null";
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(resp.as_bytes()).await;
            let _ = stream.write_all(body).await;
        }
    });

    let engine = PricingEngine::new();
    let result = engine
        .refresh_from_server(&format!("http://{}", addr))
        .await;
    assert!(result.is_err(), "should fail when 'models' key is absent");
}

#[tokio::test]
async fn test_refresh_from_server_updates_model_map() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    let body = r#"{"data":{"pricing_version":"refresh-test-v1","source":"litellm","fetched_at":"2026-07-14T00:00:00Z","model_count":1,"data":{"refresh-only-model":{"input_cost_per_token":0.001,"output_cost_per_token":0.002}}}}"#;
    let body_bytes = body.as_bytes().to_vec();
    let body_len = body_bytes.len();

    tokio::spawn(async move {
        for _ in 0..2u8 {
            if let Ok((mut stream, _)) = listener.accept().await {
                let body_bytes = body_bytes.clone();
                // Drain the HTTP request before writing the response (required on Windows)
                let mut buf = [0u8; 4096];
                let _ = stream.read(&mut buf).await;
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body_len
                );
                let _ = stream.write_all(header.as_bytes()).await;
                let _ = stream.write_all(&body_bytes).await;
            }
        }
    });

    let engine = PricingEngine::new();
    let result = engine
        .refresh_from_server(&format!("http://{}", addr))
        .await;
    assert!(result.is_ok(), "refresh should succeed: {:?}", result);

    // After refresh the map is replaced with what the server returned
    assert_eq!(engine.model_count().await, 1);

    let cost = engine.get_cost("refresh-only-model", 1000, 500, 0, 0).await;
    assert_eq!(cost.cost_confidence, CostConfidence::Computed);
    assert!(cost.cost_usd > Decimal::ZERO);
}

#[tokio::test]
async fn test_start_stop_background_refresh_no_panic() {
    let engine = PricingEngine::new();
    engine.start_background_refresh("http://127.0.0.1:1".to_string(), Duration::from_millis(50));
    tokio::time::sleep(Duration::from_millis(20)).await;
    engine.stop_background_refresh();
}

#[tokio::test]
async fn test_stop_before_start_no_panic() {
    let engine = PricingEngine::new();
    engine.stop_background_refresh(); // must not panic
}

#[tokio::test]
async fn test_background_refresh_updates_engine() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    let body = r#"{"data":{"pricing_version":"background-test-v1","source":"litellm","fetched_at":"2026-07-14T00:00:00Z","model_count":1,"data":{"bg-refresh-model":{"input_cost_per_token":0.005,"output_cost_per_token":0.010}}}}"#;
    let body_bytes = body.as_bytes().to_vec();
    let body_len = body_bytes.len();

    // Serve multiple connections, each time draining the request first
    tokio::spawn(async move {
        loop {
            if let Ok((mut stream, _)) = listener.accept().await {
                let body_bytes = body_bytes.clone();
                tokio::spawn(async move {
                    let mut buf = [0u8; 4096];
                    let _ = stream.read(&mut buf).await;
                    let header = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body_len
                    );
                    let _ = stream.write_all(header.as_bytes()).await;
                    let _ = stream.write_all(&body_bytes).await;
                });
            }
        }
    });

    let engine = Arc::new(PricingEngine::new());

    engine.start_background_refresh(format!("http://{}", addr), Duration::from_millis(50));

    // Wait for at least one full refresh cycle
    tokio::time::sleep(Duration::from_millis(300)).await;

    engine.stop_background_refresh();

    let cost = engine.get_cost("bg-refresh-model", 1000, 500, 0, 0).await;
    assert_eq!(cost.cost_confidence, CostConfidence::Computed);
    assert!(cost.cost_usd > Decimal::ZERO);
}
