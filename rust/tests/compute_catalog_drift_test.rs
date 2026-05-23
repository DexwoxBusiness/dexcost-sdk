//! Drift check vs Python canonical catalog.
//!
//! The Rust SDK's `rust/src/data/compute_prices.json` is synced from Python's
//! canonical `python/src/dexcost/data/compute_prices.json` via
//! `scripts/sync_compute_catalog.sh`. This test asserts byte-equality so a
//! drift is caught in CI rather than at runtime.
//!
//! Skip gracefully in published-crate environment where the Python file isn't
//! reachable (e.g. when building from a sdist / crates.io tarball).

use std::path::PathBuf;

#[test]
fn rust_catalog_matches_python_canonical_byte_equal() {
    // Both files live at well-known paths in the monorepo. Resolve from
    // CARGO_MANIFEST_DIR (rust/) up to the repo root.
    let rust_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = rust_dir
        .parent()
        .expect("rust/ has a parent (repo root)")
        .to_path_buf();
    let rust_catalog = rust_dir.join("src").join("data").join("compute_prices.json");
    let python_catalog = repo_root
        .join("python")
        .join("src")
        .join("dexcost")
        .join("data")
        .join("compute_prices.json");

    if !python_catalog.exists() {
        eprintln!(
            "[soft-skip] Python canonical catalog not present at {:?} — \
             likely a published-crate build. Skipping drift check.",
            python_catalog
        );
        return;
    }

    let rust_bytes = std::fs::read(&rust_catalog).expect("read rust catalog");
    let python_bytes = std::fs::read(&python_catalog).expect("read python catalog");

    assert_eq!(
        rust_bytes,
        python_bytes,
        "compute_prices.json drift between rust and python — re-run \
         scripts/sync_compute_catalog.sh to re-sync (rust={} bytes, \
         python={} bytes)",
        rust_bytes.len(),
        python_bytes.len(),
    );
}
