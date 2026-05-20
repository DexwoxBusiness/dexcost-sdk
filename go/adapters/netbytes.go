// Package adapters — helpers for the HTTP network adapter: destination
// classification and byte measurement. Pure functions — no SDK state, no I/O
// beyond parsing.
package adapters

import "net"

// ClassifyDestination returns whether host is internal traffic.
//
//   - *bool(true)  — host is an RFC1918 / loopback / link-local IP literal.
//   - *bool(false) — host is a public IP literal.
//   - nil          — host is a name (not an IP literal); the SDK does not
//     perform an extra DNS lookup to resolve it.
//
// Note: CGNAT shared address space (100.64.0.0/10, RFC 6598) is classified
// false because net.IP.IsPrivate (added in Go 1.17) does not include it,
// mirroring Python's ipaddress.is_private behaviour.
func ClassifyDestination(host string) *bool {
	if host == "" {
		return nil
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return nil
	}
	internal := ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast()
	return &internal
}

// headersByteLen approximates the on-the-wire size of a header block:
// "Key: Value\r\n" per entry, plus a trailing CRLF that ends the block.
func headersByteLen(headers map[string]string) int {
	total := 0
	for key, value := range headers {
		total += len(key) + len(value) + 4 // ": " + CRLF
	}
	return total + 2 // trailing CRLF
}

// MeasureBytesFromHeaders approximates the on-the-wire byte size of one HTTP
// message: request line + header block + body. Used for both directions —
// pass the request method/url/headers for bytes-out, or "" / "" / response
// headers for bytes-in. bodyLen is the known body length in bytes; negative
// values are clamped to zero.
func MeasureBytesFromHeaders(method, url string, headers map[string]string, bodyLen int) int {
	requestLine := len(method) + len(url) + 12 // method + url + " HTTP/1.1\r\n"
	body := bodyLen
	if body < 0 {
		body = 0
	}
	return requestLine + headersByteLen(headers) + body
}
