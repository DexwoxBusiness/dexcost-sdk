//! GPU catalog drift check — Phase 2 Task 10.
//!
//! Rust port of `python/tests/test_gpu_catalog_sync_consistency.py`
//! (commit d7d48b6). Walks up from the test file to find the dexcost-sdk
//! repo root; asserts the Python canonical `gpu_prices.json` is byte-equal
//! to the Rust bundle. Skips gracefully when running from a published
//! crate where `python/` isn't reachable (the canonical sync script
//! `scripts/sync_gpu_catalog.sh` runs at catalog ship time — commit
//! 79c8745 produced today's byte-equal bundles).

use std::path::PathBuf;

const RUST_BUNDLED: &str = include_str!("../src/data/gpu_prices.json");

fn find_repo_root() -> Option<PathBuf> {
    // CARGO_MANIFEST_DIR points to rust/. Walk up until we find python/.
    let manifest = std::env::var("CARGO_MANIFEST_DIR").ok()?;
    let mut p = PathBuf::from(manifest);
    while p.parent().is_some() {
        if p.join("python").is_dir() && p.join("rust").is_dir() {
            return Some(p);
        }
        p = p.parent()?.to_path_buf();
    }
    None
}

#[test]
fn rust_catalog_matches_python_canonical() {
    let root = match find_repo_root() {
        Some(r) => r,
        None => {
            eprintln!(
                "[skip] dexcost-sdk repo root not reachable from test cwd; \
                 skipping cross-SDK drift check"
            );
            return;
        }
    };
    let python_catalog = root.join("python/src/dexcost/data/gpu_prices.json");
    if !python_catalog.exists() {
        eprintln!("[skip] python canonical catalog not found at {:?}", python_catalog);
        return;
    }
    let python_content = std::fs::read_to_string(&python_catalog).unwrap_or_else(|e| {
        panic!(
            "failed to read python canonical catalog at {:?}: {}",
            python_catalog, e
        )
    });
    assert_eq!(
        python_content.trim(),
        RUST_BUNDLED.trim(),
        "Rust gpu_prices.json drifted from Python canonical. \
         Re-run scripts/sync_gpu_catalog.sh."
    );
}
