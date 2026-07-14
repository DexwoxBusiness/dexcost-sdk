//! Cross-SDK drift check for the safety-filtered service catalog.

use std::path::PathBuf;

const RUST_BUNDLED: &str = include_str!("../src/data/service_prices.json");

fn find_repo_root() -> Option<PathBuf> {
    let manifest = std::env::var("CARGO_MANIFEST_DIR").ok()?;
    let mut path = PathBuf::from(manifest);
    while path.parent().is_some() {
        if path.join("python").is_dir() && path.join("rust").is_dir() {
            return Some(path);
        }
        path = path.parent()?.to_path_buf();
    }
    None
}

#[test]
fn rust_service_catalog_matches_safe_python_canonical() {
    let Some(root) = find_repo_root() else {
        eprintln!("[skip] dexcost-sdk repository root is not reachable");
        return;
    };
    let canonical = root.join("python/src/dexcost/data/service_prices.json");
    if !canonical.exists() {
        eprintln!("[skip] Python service catalog canonical is not reachable");
        return;
    }
    let python_content = std::fs::read_to_string(&canonical)
        .unwrap_or_else(|error| panic!("failed to read {:?}: {}", canonical, error));
    assert_eq!(
        python_content.as_bytes(),
        RUST_BUNDLED.as_bytes(),
        "Rust service catalog drifted; run scripts/sync_service_catalog.sh"
    );
}
