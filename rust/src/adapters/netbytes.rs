//! Helpers for the HTTP network adapter: destination classification and
//! byte measurement. Pure functions — no SDK state, no I/O beyond parsing.
//!
//! Mirrors `python/src/dexcost/adapters/_netbytes.py`.

use std::collections::HashMap;
use std::net::IpAddr;

/// Return whether `host` is internal traffic.
///
/// * `Some(true)`  — host is an RFC1918 / loopback / link-local IP literal.
/// * `Some(false)` — host is a public IP literal.
/// * `None`        — host is a name (not an IP literal) or empty; the SDK
///   does not perform an extra DNS lookup to resolve it.
///
/// Note: CGNAT shared address space (100.64.0.0/10, RFC 6598) is classified
/// as `Some(false)` because `Ipv4Addr::is_private` does not include it
/// (matching Python's `ipaddress.is_private` behaviour).
pub fn classify_destination(host: &str) -> Option<bool> {
    if host.is_empty() {
        return None;
    }
    let ip: IpAddr = host.parse().ok()?;
    let is_internal = match ip {
        IpAddr::V4(v4) => v4.is_private() || v4.is_loopback() || v4.is_link_local(),
        IpAddr::V6(v6) => v6.is_loopback() || v6.is_unique_local() || v6.is_unicast_link_local(),
    };
    Some(is_internal)
}

/// Approximate on-the-wire size of a header block: `Key: Value\r\n` each
/// plus the terminating `\r\n`.
fn headers_byte_len(headers: &HashMap<String, String>) -> usize {
    let mut total: usize = 0;
    for (key, value) in headers {
        total += key.len() + value.len() + 4; // ": " + CRLF
    }
    total + 2 // trailing CRLF that ends the header block
}

/// Approximate the on-the-wire byte size of one HTTP message.
///
/// `request line + header block + body`. Used for both directions: pass
/// the request method/url/headers for bytes-out, or `"" / "" / response
/// headers` for bytes-in. `body_len` is the known body length in bytes.
pub fn measure_bytes_from_headers(
    method: &str,
    url: &str,
    headers: &HashMap<String, String>,
    body_len: usize,
) -> usize {
    let request_line = method.len() + url.len() + 12; // method + url + " HTTP/1.1\r\n"
    request_line + headers_byte_len(headers) + body_len
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_ipv4_is_internal() {
        assert_eq!(classify_destination("10.1.2.3"), Some(true));
        assert_eq!(classify_destination("192.168.0.5"), Some(true));
        assert_eq!(classify_destination("172.16.9.9"), Some(true));
    }

    #[test]
    fn localhost_and_link_local_are_internal() {
        assert_eq!(classify_destination("127.0.0.1"), Some(true));
        assert_eq!(classify_destination("::1"), Some(true));
        assert_eq!(classify_destination("169.254.10.1"), Some(true));
    }

    #[test]
    fn public_ip_is_not_internal() {
        assert_eq!(classify_destination("8.8.8.8"), Some(false));
        assert_eq!(classify_destination("1.1.1.1"), Some(false));
    }

    #[test]
    fn cgnat_is_not_internal() {
        // 100.64.0.0/10 (RFC 6598 shared address space) — Python's
        // ipaddress.is_private does NOT include it; we must mirror that.
        assert_eq!(classify_destination("100.64.0.1"), Some(false));
    }

    #[test]
    fn named_host_is_unknown() {
        assert_eq!(classify_destination("api.openai.com"), None);
        assert_eq!(classify_destination(""), None);
        assert_eq!(classify_destination("not-an-ip"), None);
    }

    #[test]
    fn ipv6_ula_is_internal() {
        // fd00::/8 is IPv6 unique-local (RFC 4193) — must be classified internal.
        assert_eq!(classify_destination("fd00::1"), Some(true));
        assert_eq!(classify_destination("fc00::1"), Some(true));
    }

    #[test]
    fn ipv6_link_local_is_internal() {
        // fe80::/10 — IPv6 link-local.
        assert_eq!(classify_destination("fe80::1"), Some(true));
    }

    #[test]
    fn ipv6_public_is_not_internal() {
        // 2001:db8::/32 is documentation-only but parses as a public unicast.
        assert_eq!(classify_destination("2001:4860:4860::8888"), Some(false));
    }

    #[test]
    fn measure_bytes_includes_headers_and_body() {
        let mut headers = HashMap::new();
        headers.insert("Content-Length".to_string(), "2048".to_string());
        headers.insert("Content-Type".to_string(), "application/json".to_string());
        let n = measure_bytes_from_headers("POST", "https://x.com/v1/y", &headers, 2048);
        assert!(n >= 2048);
        assert!(n > 2048);
    }

    #[test]
    fn measure_bytes_exact_total() {
        // Pin the +4/+2/+12 constants against silent regression.
        // Input: method="GET", url="https://a.io/", headers={"X-H": "v"}, body_len=0
        // request_line = 3 + 13 + 12 = 28
        // headers: (3 + 1 + 4) + 2 = 10
        // body = 0
        // total = 38
        let mut headers = HashMap::new();
        headers.insert("X-H".to_string(), "v".to_string());
        let n = measure_bytes_from_headers("GET", "https://a.io/", &headers, 0);
        assert_eq!(n, 38);
    }

    #[test]
    fn measure_bytes_zero_body() {
        let headers = HashMap::new();
        let n = measure_bytes_from_headers("GET", "https://x.com/", &headers, 0);
        assert!(n > 0); // request line + trailing CRLF still cost bytes
    }

    #[test]
    fn measure_bytes_empty_headers_exact() {
        // method="GET" (3), url="" (0), 12 = 15
        // headers: no entries -> 0 + 2 trailing CRLF = 2
        // body = 0
        // total = 17
        let headers = HashMap::new();
        let n = measure_bytes_from_headers("GET", "", &headers, 0);
        assert_eq!(n, 17);
    }
}
