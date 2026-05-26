use std::collections::HashSet;
use std::sync::OnceLock;

use hex;
use regex::Regex;
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

/// Canonical set of query parameter names (case-insensitive) that
/// [`scrub_url`] strips. Must stay in sync with the same set in Python
/// (dexcost/redaction.py), Go (security/redaction.go), and TypeScript
/// (src/security/redaction.ts).
fn sensitive_query_params() -> &'static HashSet<&'static str> {
    static SET: OnceLock<HashSet<&'static str>> = OnceLock::new();
    SET.get_or_init(|| {
        [
            "api_key",
            "apikey",
            "access_token",
            "token",
            "auth",
            "password",
            "secret",
            "signature",
            "x-amz-signature",
            "x-amz-credential",
            "x-amz-security-token",
            "session",
        ]
        .into_iter()
        .collect()
    })
}

fn userinfo_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(https?://)([^@/?#]+@)?(.+)$").unwrap())
}

/// Strip credentials from a URL before it is captured into an event.
///
/// Removes:
///   - userinfo (`user:pass@`) from the authority
///   - query parameters whose name (case-insensitive) is in the canonical
///     sensitive set OR ends with `-signature`, `-credential`, or
///     `-security-token` (AWS SigV4 surface)
///
/// Preserves scheme, host, port, path, non-sensitive query params, and
/// fragment. The shape of every removed query parameter is preserved as
/// `name=REDACTED` so downstream callers can still see which keys were
/// present without leaking the values.
///
/// Canonical algorithm — Python/Go/TypeScript SDK implementations must
/// produce byte-identical output for the same input (enforced by
/// `/fixtures/expected_outputs/security/`).
pub fn scrub_url(url: &str) -> String {
    if url.is_empty() {
        return String::new();
    }
    let mut url = if let Some(caps) = userinfo_re().captures(url) {
        format!("{}{}", &caps[1], &caps[3])
    } else {
        url.to_string()
    };

    let mut fragment = String::new();
    if let Some(hash_idx) = url.find('#') {
        fragment = url[hash_idx..].to_string();
        url.truncate(hash_idx);
    }
    let q_idx = match url.find('?') {
        Some(i) => i,
        None => return url + &fragment,
    };
    let (base, query_with_q) = url.split_at(q_idx);
    let query = &query_with_q[1..];
    let sensitive_set = sensitive_query_params();

    let scrubbed_parts: Vec<String> = query
        .split('&')
        .map(|part| {
            let name = match part.find('=') {
                Some(eq) => &part[..eq],
                None => part,
            };
            let lname = name.to_ascii_lowercase();
            let sensitive = sensitive_set.contains(lname.as_str())
                || lname.ends_with("-signature")
                || lname.ends_with("-credential")
                || lname.ends_with("-security-token");
            if sensitive {
                format!("{name}=REDACTED")
            } else {
                part.to_string()
            }
        })
        .collect();
    format!("{base}?{}{fragment}", scrubbed_parts.join("&"))
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

    // --- scrub_url ---

    #[test]
    fn scrub_url_empty() {
        assert_eq!(scrub_url(""), "");
    }

    #[test]
    fn scrub_url_no_credentials_unchanged() {
        let u = "https://api.example.com/v1/chat?page=2&limit=50";
        assert_eq!(scrub_url(u), u);
    }

    #[test]
    fn scrub_url_strips_basic_auth() {
        assert_eq!(
            scrub_url("https://alice:s3cr3t@api.example.com/v1/chat"),
            "https://api.example.com/v1/chat"
        );
    }

    #[test]
    fn scrub_url_strips_username_only() {
        assert_eq!(
            scrub_url("https://token123@api.example.com/path"),
            "https://api.example.com/path"
        );
    }

    #[test]
    fn scrub_url_redacts_api_key() {
        assert_eq!(
            scrub_url("https://api.example.com/v1?api_key=sk-secret&page=2"),
            "https://api.example.com/v1?api_key=REDACTED&page=2"
        );
    }

    #[test]
    fn scrub_url_case_insensitive() {
        let out = scrub_url("https://api.example.com/?ApiKey=abc&AUTH=xyz&keep=1");
        assert!(out.contains("ApiKey=REDACTED"), "got {out}");
        assert!(out.contains("AUTH=REDACTED"), "got {out}");
        assert!(out.contains("keep=1"), "got {out}");
    }

    #[test]
    fn scrub_url_aws_sigv4() {
        let u = "https://my-bucket.s3.amazonaws.com/obj.json\
                 ?X-Amz-Algorithm=AWS4-HMAC-SHA256\
                 &X-Amz-Credential=AKIA%2F20260526%2Fus-east-1%2Fs3%2Faws4_request\
                 &X-Amz-Date=20260526T123456Z\
                 &X-Amz-Signature=abcdef1234567890";
        let out = scrub_url(u);
        assert!(out.contains("X-Amz-Credential=REDACTED"), "got {out}");
        assert!(out.contains("X-Amz-Signature=REDACTED"), "got {out}");
        assert!(out.contains("X-Amz-Algorithm=AWS4-HMAC-SHA256"), "got {out}");
        assert!(out.contains("X-Amz-Date=20260526T123456Z"), "got {out}");
    }

    #[test]
    fn scrub_url_security_token_suffix() {
        let out = scrub_url("https://api.aws.amazon.com/?X-Amz-Security-Token=FQoG&page=1");
        assert!(out.contains("X-Amz-Security-Token=REDACTED"), "got {out}");
        assert!(out.contains("page=1"), "got {out}");
    }

    #[test]
    fn scrub_url_preserves_fragment() {
        assert_eq!(
            scrub_url("https://docs.example.com/api?api_key=secret#installation"),
            "https://docs.example.com/api?api_key=REDACTED#installation"
        );
    }

    #[test]
    fn scrub_url_preserves_path_and_port() {
        assert_eq!(
            scrub_url("https://api.example.com:8443/v2/agents/run?token=xyz"),
            "https://api.example.com:8443/v2/agents/run?token=REDACTED"
        );
    }

    #[test]
    fn scrub_url_no_query_unchanged() {
        let u = "https://api.example.com/v1/path/segment";
        assert_eq!(scrub_url(u), u);
    }

    #[test]
    fn scrub_url_value_with_equals() {
        assert_eq!(
            scrub_url("https://api.example.com/?api_key=abc==pad&keep=ok"),
            "https://api.example.com/?api_key=REDACTED&keep=ok"
        );
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
