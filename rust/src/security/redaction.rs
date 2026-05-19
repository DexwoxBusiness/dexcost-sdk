use std::collections::HashSet;

use hex;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// Returns a copy of `data` with matching keys removed entirely.
/// Recursively redacts nested objects.
///
/// Matches Python `redaction.py` `redact_dict`, which **deletes** matched keys
/// (it does not mask them with a placeholder).
pub fn redact_map(data: &Map<String, Value>, fields: &[&str]) -> Map<String, Value> {
    let field_set: HashSet<&str> = fields.iter().copied().collect();
    let mut out = Map::new();
    for (key, val) in data {
        if field_set.contains(key.as_str()) {
            // Drop matched keys entirely (Python parity).
            continue;
        } else if let Value::Object(nested) = val {
            out.insert(key.clone(), Value::Object(redact_map(nested, fields)));
        } else {
            out.insert(key.clone(), val.clone());
        }
    }
    out
}

/// Returns the SHA-256 hex digest of `value`.
pub fn hash_value(value: &str) -> String {
    let digest = Sha256::digest(value.as_bytes());
    hex::encode(digest)
}

/// Returns `details` unchanged if its serialised JSON size is within
/// `max_bytes`. Otherwise returns a deterministic stub:
/// `{"_truncated": true, "_original_size_bytes": N}`.
///
/// Matches Python `redaction.py` `enforce_metadata_limit`, which returns a
/// fixed stub rather than dropping individual trailing entries.
pub fn enforce_metadata_limit(
    details: &Map<String, Value>,
    max_bytes: usize,
) -> Map<String, Value> {
    let serialised = serde_json::to_string(details).unwrap_or_default();
    let byte_size = serialised.len();
    if byte_size <= max_bytes {
        return details.clone();
    }

    let mut stub = Map::new();
    stub.insert("_truncated".to_string(), Value::Bool(true));
    stub.insert(
        "_original_size_bytes".to_string(),
        Value::Number(byte_size.into()),
    );
    stub
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map_from_value(v: serde_json::Value) -> Map<String, Value> {
        match v {
            Value::Object(m) => m,
            _ => panic!("expected object"),
        }
    }

    // 1. Deletes matching keys entirely (Python parity)
    #[test]
    fn test_redacts_matching_keys() {
        let data = map_from_value(json!({
            "email": "alice@example.com",
            "name": "Alice",
            "score": 42
        }));
        let result = redact_map(&data, &["email", "name"]);
        assert!(!result.contains_key("email"));
        assert!(!result.contains_key("name"));
        assert_eq!(result["score"], json!(42));
    }

    // 2. Recursively deletes nested matching keys
    #[test]
    fn test_recursive_redaction() {
        let data = map_from_value(json!({
            "user": {
                "email": "bob@example.com",
                "age": 30
            },
            "request_id": "req-1"
        }));
        let result = redact_map(&data, &["email"]);
        let user = result["user"].as_object().expect("user should be object");
        assert!(!user.contains_key("email"));
        assert_eq!(user["age"], json!(30));
        assert_eq!(result["request_id"], json!("req-1"));
    }

    // 3. Original is not modified (the Rust borrow checker enforces immutability)
    #[test]
    fn test_original_unchanged() {
        let data = map_from_value(json!({"secret": "s3cr3t", "keep": "ok"}));
        let _result = redact_map(&data, &["secret"]);
        // data is still accessible and unchanged — guaranteed by the &Map signature
        assert_eq!(data["secret"], json!("s3cr3t"));
    }

    // 4. hash_value returns 64-char hex string
    #[test]
    fn test_hash_value_length() {
        let h = hash_value("test");
        assert_eq!(h.len(), 64);
        assert!(h.chars().all(|c| c.is_ascii_hexdigit()));
    }

    // 5. hash_value is deterministic
    #[test]
    fn test_hash_value_deterministic() {
        let input = "user@example.com";
        assert_eq!(hash_value(input), hash_value(input));
    }

    // 6. enforce_metadata_limit — unchanged when under the limit
    #[test]
    fn test_enforce_metadata_limit_unchanged() {
        let data = map_from_value(json!({"a": "b"}));
        let result = enforce_metadata_limit(&data, 10240);
        assert_eq!(result, data);
    }

    // 7. enforce_metadata_limit — returns a deterministic stub when over limit
    #[test]
    fn test_enforce_metadata_limit_truncates() {
        // Build a map large enough to exceed a small limit
        let long_val: String = "x".repeat(200);
        let data = map_from_value(json!({
            "key1": long_val,
            "key2": "short"
        }));
        let original_size = serde_json::to_string(&data).unwrap().len();

        let result = enforce_metadata_limit(&data, 50);
        // The stub replaces the data entirely (Python parity).
        assert_eq!(result.len(), 2);
        assert_eq!(result["_truncated"], json!(true));
        assert_eq!(result["_original_size_bytes"], json!(original_size));
        assert!(!result.contains_key("key1"));
    }
}
