//! `NetworkAccountant` — a per-task in-process accumulator of HTTP byte usage.
//!
//! One instance lives on each [`Task`] as an unserialised field. The HTTP
//! adapter calls [`NetworkAccountant::record`] per call;
//! [`NetworkAccountant::finalize`] is called once at task end. After finalize
//! the accountant is frozen — later `record()` calls are no-ops, so
//! late-arriving bytes never mutate already-shipped task aggregates.
//!
//! Mirrors `python/src/dexcost/network_accountant.py`. Locking: the cross-task
//! contention surface is microseconds (Mutex held only inside `record()` and
//! `finalize()`), so `std::sync::Mutex` is appropriate — the spec §5.2
//! `tokio::sync::Mutex` recommendation is overridden here because the
//! accountant is also called from sync contexts inside `reqwest-middleware`.
//! See plan §2 (Rust note).

use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::json;

/// Number of host entries kept in `by_host` after finalize (plus `_other`).
pub const FINALIZE_CAP: usize = 20;
/// Maximum distinct hosts tracked live before overflow folds into `_other`.
pub const LIVE_CAP: usize = 500;

#[derive(Debug, Default)]
struct AccountantInner {
    bytes_in: u64,
    bytes_out: u64,
    external_bytes_out: u64,
    call_count: u64,
    /// host -> [calls, bytes_in, bytes_out, external_bytes_out]
    hosts: HashMap<String, [u64; 4]>,
    /// Overflow bucket once LIVE_CAP distinct hosts are tracked.
    other: [u64; 4],
    frozen: bool,
}

/// Accumulates HTTP byte usage for a single tracked task.
///
/// All public methods are safe to call from multiple threads or from sync /
/// async contexts. The accountant has interior mutability via a single
/// `Mutex`; lock contention is microseconds (writes only; no reads until
/// `finalize`).
///
/// `is_internal` follows the v1 §4.2 three-valued classification:
///   * `Some(true)`  → bytes are intra-VPC / loopback → 0 external bytes.
///   * `Some(false)` → confirmed public IP → all of `bytes_out` are external.
///   * `None`        → unresolved named host → treated as external (conservative —
///     over-attribute rather than undercount).
#[derive(Debug, Default)]
pub struct NetworkAccountant {
    inner: Mutex<AccountantInner>,
}

impl NetworkAccountant {
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds one HTTP call's bytes. No-op once `finalize` has been called.
    pub fn record(
        &self,
        host: &str,
        bytes_in: i64,
        bytes_out: i64,
        is_internal: Option<bool>,
    ) {
        // Clamp negatives — bytes can never be negative.
        let bytes_in = bytes_in.max(0) as u64;
        let bytes_out = bytes_out.max(0) as u64;
        let external_out = if matches!(is_internal, Some(true)) {
            0
        } else {
            bytes_out
        };

        let mut inner = self.inner.lock().expect("NetworkAccountant mutex poisoned");
        if inner.frozen {
            return;
        }
        inner.bytes_in += bytes_in;
        inner.bytes_out += bytes_out;
        inner.external_bytes_out += external_out;
        inner.call_count += 1;

        let key = if host.is_empty() {
            "_unknown".to_string()
        } else {
            host.to_string()
        };

        if let Some(entry) = inner.hosts.get_mut(&key) {
            entry[0] += 1;
            entry[1] += bytes_in;
            entry[2] += bytes_out;
            entry[3] += external_out;
        } else if inner.hosts.len() < LIVE_CAP {
            inner.hosts.insert(key, [1, bytes_in, bytes_out, external_out]);
        } else {
            inner.other[0] += 1;
            inner.other[1] += bytes_in;
            inner.other[2] += bytes_out;
            inner.other[3] += external_out;
        }
    }

    /// Number of distinct hosts currently tracked (excludes `_other`).
    pub fn live_host_count(&self) -> usize {
        self.inner
            .lock()
            .expect("NetworkAccountant mutex poisoned")
            .hosts
            .len()
    }

    /// Freezes the accountant and returns the snapshot for the task fields.
    ///
    /// Returns a [`NetworkSnapshot`] with the canonical scalar
    /// `external_bytes_out` (basis for v2 egress pricing) and a `by_host`
    /// breakdown of the top `FINALIZE_CAP` hosts by total bytes plus an
    /// `_other` bucket summing the rest. Each host entry carries
    /// `external_bytes_out` so v2 per-host egress cost survives the cap.
    pub fn finalize(&self) -> NetworkSnapshot {
        let mut inner = self.inner.lock().expect("NetworkAccountant mutex poisoned");
        inner.frozen = true;

        // Drain hosts into a Vec we can sort by (bytes_in + bytes_out) desc.
        let mut ranked: Vec<(String, [u64; 4])> = inner.hosts.drain().collect();
        ranked.sort_by(|a, b| {
            let total_a = a.1[1] + a.1[2];
            let total_b = b.1[1] + b.1[2];
            total_b.cmp(&total_a)
        });

        let mut other = inner.other;
        let top: Vec<(String, [u64; 4])> = ranked
            .drain(..)
            .enumerate()
            .filter_map(|(idx, (host, vals))| {
                if idx < FINALIZE_CAP {
                    // If a real host is literally named "_other" it would
                    // collide with the synthetic overflow bucket. Fold it
                    // into `other` so the output list never has duplicates.
                    if host == "_other" {
                        for i in 0..4 {
                            other[i] += vals[i];
                        }
                        None
                    } else {
                        Some((host, vals))
                    }
                } else {
                    for i in 0..4 {
                        other[i] += vals[i];
                    }
                    None
                }
            })
            .collect();

        let mut hosts: Vec<serde_json::Value> = top
            .into_iter()
            .map(|(host, vals)| {
                json!({
                    "host": host,
                    "calls": vals[0],
                    "bytes_in": vals[1],
                    "bytes_out": vals[2],
                    "external_bytes_out": vals[3],
                })
            })
            .collect();

        if other[0] > 0 {
            hosts.push(json!({
                "host": "_other",
                "calls": other[0],
                "bytes_in": other[1],
                "bytes_out": other[2],
                "external_bytes_out": other[3],
            }));
        }

        NetworkSnapshot {
            bytes_in: inner.bytes_in,
            bytes_out: inner.bytes_out,
            external_bytes_out: inner.external_bytes_out,
            call_count: inner.call_count,
            by_host: json!({ "hosts": hosts }),
        }
    }
}

/// Snapshot returned by [`NetworkAccountant::finalize`].
#[derive(Debug, Clone)]
pub struct NetworkSnapshot {
    pub bytes_in: u64,
    pub bytes_out: u64,
    /// Canonical scalar — the basis for v2 `network_cost_usd`.
    pub external_bytes_out: u64,
    pub call_count: u64,
    /// `{"hosts": [ {host, calls, bytes_in, bytes_out, external_bytes_out}, ... ]}`
    pub by_host: serde_json::Value,
}

// ---------------------------------------------------------------------------
// Tests — port of python/tests/test_network_accountant.py and
// test_network_accountant_external.py
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_updates_counters() {
        let a = NetworkAccountant::new();
        a.record("a.com", 100, 10, None);
        a.record("a.com", 50, 5, None);
        let snap = a.finalize();
        assert_eq!(snap.bytes_in, 150);
        assert_eq!(snap.bytes_out, 15);
        assert_eq!(snap.call_count, 2);
    }

    #[test]
    fn finalize_groups_by_host() {
        let a = NetworkAccountant::new();
        a.record("a.com", 100, 10, None);
        a.record("b.com", 200, 20, None);
        let snap = a.finalize();
        let hosts = snap.by_host["hosts"].as_array().unwrap();
        let a_host = hosts.iter().find(|h| h["host"] == "a.com").unwrap();
        // is_internal defaults to None → bytes_out attributes as external (v2 §6.1).
        assert_eq!(a_host["calls"], 1);
        assert_eq!(a_host["bytes_in"], 100);
        assert_eq!(a_host["bytes_out"], 10);
        assert_eq!(a_host["external_bytes_out"], 10);
    }

    #[test]
    fn finalize_caps_to_top_20_with_other_bucket() {
        let a = NetworkAccountant::new();
        // 25 hosts; host_i gets i+1 bytes_in so heavy ones are deterministic.
        for i in 0..25 {
            a.record(&format!("h{:02}.com", i), (i as i64) + 1, 0, None);
        }
        let snap = a.finalize();
        let hosts = snap.by_host["hosts"].as_array().unwrap();
        assert_eq!(hosts.len(), FINALIZE_CAP + 1); // 20 + _other
        let names: Vec<&str> = hosts.iter().map(|h| h["host"].as_str().unwrap()).collect();
        assert!(names.contains(&"_other"));
        assert!(names.contains(&"h24.com")); // heaviest survives
        assert!(!names.contains(&"h00.com")); // lightest folded into _other
        let other = hosts.iter().find(|h| h["host"] == "_other").unwrap();
        // 5 lightest folded: (1+2+3+4+5) bytes_in, 5 calls.
        assert_eq!(other["calls"], 5);
        assert_eq!(other["bytes_in"], 1 + 2 + 3 + 4 + 5);
    }

    #[test]
    fn empty_finalize_is_empty_array() {
        let snap = NetworkAccountant::new().finalize();
        assert_eq!(snap.by_host, json!({"hosts": []}));
    }

    #[test]
    fn live_cap_folds_overflow_hosts_into_other() {
        let a = NetworkAccountant::new();
        for i in 0..(LIVE_CAP + 50) {
            a.record(&format!("host{}.com", i), 0, 1, Some(false));
        }
        // Before finalize, only LIVE_CAP distinct hosts in the map.
        assert_eq!(a.live_host_count(), LIVE_CAP);
        let snap = a.finalize();
        let other = snap.by_host["hosts"]
            .as_array()
            .unwrap()
            .iter()
            .find(|h| h["host"] == "_other")
            .unwrap();
        // 50 hosts that overflowed LIVE_CAP + (LIVE_CAP - FINALIZE_CAP) folded
        // from the top-N cap = LIVE_CAP + 50 - FINALIZE_CAP entries.
        assert_eq!(
            other["calls"].as_u64().unwrap(),
            (LIVE_CAP + 50 - FINALIZE_CAP) as u64
        );
    }

    #[test]
    fn frozen_after_finalize_record_is_noop() {
        let a = NetworkAccountant::new();
        a.record("a.com", 100, 10, None);
        let snap1 = a.finalize();
        a.record("b.com", 999, 999, None);
        let snap2 = a.finalize();
        assert_eq!(snap1.bytes_in, snap2.bytes_in);
        assert_eq!(snap1.call_count, snap2.call_count);
    }

    #[test]
    fn empty_host_falls_back_to_unknown() {
        let a = NetworkAccountant::new();
        a.record("", 10, 0, None);
        let hosts = a.finalize().by_host;
        assert_eq!(hosts["hosts"][0]["host"], "_unknown");
    }

    #[test]
    fn negative_bytes_clamped_to_zero() {
        let a = NetworkAccountant::new();
        a.record("a.com", -10, -20, None);
        let snap = a.finalize();
        assert_eq!(snap.bytes_in, 0);
        assert_eq!(snap.bytes_out, 0);
    }

    #[test]
    fn synthetic_other_collides_with_real_host_named_other() {
        let a = NetworkAccountant::new();
        a.record("_other", 100, 50, None);
        a.record("real.com", 1, 1, None);
        let snap = a.finalize();
        let hosts = snap.by_host["hosts"].as_array().unwrap();
        // Only one "_other" entry, no duplicate; "real.com" survives separately.
        let other_count = hosts
            .iter()
            .filter(|h| h["host"] == "_other")
            .count();
        assert_eq!(other_count, 1);
        let other = hosts.iter().find(|h| h["host"] == "_other").unwrap();
        assert_eq!(other["bytes_in"], 100);
    }

    // ── External-byte split (v2) — port of test_network_accountant_external.py

    #[test]
    fn internal_call_does_not_contribute_to_external() {
        let a = NetworkAccountant::new();
        a.record("10.0.0.5", 100, 200, Some(true));
        let snap = a.finalize();
        assert_eq!(snap.external_bytes_out, 0);
        let host = &snap.by_host["hosts"][0];
        assert_eq!(host["external_bytes_out"], 0);
        assert_eq!(host["bytes_out"], 200); // raw still recorded
    }

    #[test]
    fn public_call_contributes_to_external() {
        let a = NetworkAccountant::new();
        a.record("api.example.com", 100, 500, Some(false));
        assert_eq!(a.finalize().external_bytes_out, 500);
    }

    #[test]
    fn null_is_internal_is_treated_as_external() {
        let a = NetworkAccountant::new();
        a.record("api.example.com", 100, 500, None);
        assert_eq!(a.finalize().external_bytes_out, 500);
    }

    #[test]
    fn scalar_equals_sum_of_per_host_external() {
        let a = NetworkAccountant::new();
        a.record("a.com", 0, 100, Some(false));
        a.record("b.com", 0, 200, Some(false));
        a.record("10.0.0.1", 0, 999, Some(true));
        let snap = a.finalize();
        let by_host_sum: u64 = snap.by_host["hosts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h["external_bytes_out"].as_u64().unwrap())
            .sum();
        assert_eq!(by_host_sum, snap.external_bytes_out);
        assert_eq!(snap.external_bytes_out, 300);
    }

    #[test]
    fn other_bucket_carries_external_bytes() {
        // Mirror Python's test: LIVE_CAP+1 hosts → _other gets the overflow
        // 555-byte hit + the (LIVE_CAP - FINALIZE_CAP) live-host folds (each 1
        // byte) = 480 + 555 = 1035.
        let a = NetworkAccountant::new();
        for i in 0..LIVE_CAP {
            a.record(&format!("host{}.com", i), 0, 1, Some(false));
        }
        a.record("overflow.com", 0, 555, Some(false));
        let snap = a.finalize();
        let other = snap.by_host["hosts"]
            .as_array()
            .unwrap()
            .iter()
            .find(|h| h["host"] == "_other")
            .unwrap();
        assert_eq!(
            other["external_bytes_out"].as_u64().unwrap(),
            (LIVE_CAP - FINALIZE_CAP) as u64 + 555
        );
    }

    #[test]
    fn default_is_internal_routes_bytes_as_external() {
        let a = NetworkAccountant::new();
        a.record("api.example.com", 0, 100, None);
        assert_eq!(a.finalize().external_bytes_out, 100);
    }
}
