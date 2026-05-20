/**
 * Helpers for the HTTP network adapter: destination classification and
 * byte measurement. Pure functions — no SDK state, no I/O beyond parsing.
 *
 * Mirrors python/src/dexcost/adapters/_netbytes.py.
 */

// ---------------------------------------------------------------------------
// IP literal parsing (open-coded, no `ipaddr.js` dependency — recommended in
// the network-capture plan to avoid adding a dep just for classification).
// ---------------------------------------------------------------------------

const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

function parseIPv4(host: string): [number, number, number, number] | null {
  const m = IPV4_RE.exec(host);
  if (!m) return null;
  const octets: number[] = [];
  for (let i = 1; i <= 4; i++) {
    const n = Number(m[i]);
    if (!Number.isInteger(n) || n < 0 || n > 255) return null;
    // Reject leading zeros beyond a single "0" (e.g. "010" is ambiguous on the
    // wire). ipaddress.ip_address in Python is similarly strict in 3.10+.
    if (m[i]!.length > 1 && m[i]!.startsWith("0")) return null;
    octets.push(n);
  }
  return octets as [number, number, number, number];
}

/**
 * Loose-grammar IPv6 detector: covers the common literal shapes including
 * `::`-compressed forms. We do not need full numeric reconstruction because
 * the classifier only inspects the textual prefix (`fc00::/7`, `fe80::/10`,
 * `::1`).
 */
function isIPv6Literal(host: string): boolean {
  if (host.length === 0) return false;
  // Strip optional zone-id (`fe80::1%eth0`) before validation.
  const bare = host.split("%", 1)[0]!;
  if (bare.indexOf(":") === -1) return false;
  // Disallow obvious non-hex characters.
  if (!/^[0-9a-fA-F:.]+$/.test(bare)) return false;
  // At most one "::".
  const dblIdx = bare.indexOf("::");
  if (dblIdx !== -1 && bare.indexOf("::", dblIdx + 1) !== -1) return false;
  // Split and sanity-check each group.
  const parts = bare.split(":");
  if (parts.length < 3 || parts.length > 8) return false;
  for (const p of parts) {
    if (p.length === 0) continue; // produced by "::"
    if (p.length > 4) {
      // Could be an embedded IPv4 ("::ffff:1.2.3.4") — accept if last segment.
      if (p === parts[parts.length - 1] && parseIPv4(p) !== null) continue;
      return false;
    }
  }
  return true;
}

function isPrivateIPv4(octets: [number, number, number, number]): boolean {
  const [a, b] = octets;
  // 10.0.0.0/8
  if (a === 10) return true;
  // 172.16.0.0/12
  if (a === 172 && b >= 16 && b <= 31) return true;
  // 192.168.0.0/16
  if (a === 192 && b === 168) return true;
  return false;
}

function isLoopbackIPv4(octets: [number, number, number, number]): boolean {
  return octets[0] === 127;
}

function isLinkLocalIPv4(octets: [number, number, number, number]): boolean {
  return octets[0] === 169 && octets[1] === 254;
}

/**
 * Return whether *host* is internal traffic.
 *
 * - `true`  — host is an RFC1918 / loopback / link-local IPv4 literal, an
 *             IPv6 ULA (`fc00::/7`), IPv6 link-local (`fe80::/10`), or
 *             IPv6 loopback (`::1`).
 * - `false` — host parses as an IP literal but is not in the above ranges.
 * - `null`  — host is a name (not an IP literal); the SDK does not perform
 *             an extra DNS lookup to resolve it.
 *
 * Note: CGNAT shared address space (100.64.0.0/10, RFC 6598) is classified
 * `false` — Python's `ipaddress.is_private` does not include it, and we
 * mirror that behaviour for cross-SDK parity.
 */
export function classifyDestination(host: string): boolean | null {
  if (!host) return null;

  const v4 = parseIPv4(host);
  if (v4 !== null) {
    return (
      isPrivateIPv4(v4) || isLoopbackIPv4(v4) || isLinkLocalIPv4(v4)
    );
  }

  if (isIPv6Literal(host)) {
    const lower = host.toLowerCase().split("%", 1)[0]!;
    // ::1 loopback (also covers "0:0:0:0:0:0:0:1").
    if (lower === "::1") return true;
    if (/^0:0:0:0:0:0:0:0*1$/.test(lower)) return true;
    // fc00::/7 — ULA. First byte is 0xfc or 0xfd; in text form that means
    // the address starts with "fc" or "fd" followed by a hex digit (or "::").
    if (/^f[cd][0-9a-f]{0,2}:/.test(lower) || /^f[cd]:/.test(lower)) {
      return true;
    }
    // fe80::/10 — link-local. First 10 bits are 1111111010, so the leading
    // 16 bits are between 0xfe80 and 0xfebf.
    const firstGroup = lower.split(":")[0]!;
    if (firstGroup.length > 0 && firstGroup.length <= 4) {
      const v = parseInt(firstGroup, 16);
      if (!Number.isNaN(v) && v >= 0xfe80 && v <= 0xfebf) return true;
    }
    return false;
  }

  return null;
}

// ---------------------------------------------------------------------------
// Byte-size measurement
// ---------------------------------------------------------------------------

function _headersByteLen(headers: Record<string, string>): number {
  let total = 0;
  for (const [key, value] of Object.entries(headers)) {
    // ": " + CRLF == 4 extra bytes per header line.
    total += String(key).length + String(value).length + 4;
  }
  return total + 2; // trailing CRLF that ends the header block
}

/**
 * Approximate the on-the-wire byte size of one HTTP message.
 *
 * `request line + header block + body`. Used for both directions: pass the
 * request method/url/headers for bytes-out, or `"" / "" / response headers`
 * for bytes-in. `bodyLen` is the known body length in bytes; negative values
 * are clamped to zero.
 */
export function measureBytesFromHeaders(
  method: string,
  url: string,
  headers: Record<string, string>,
  bodyLen: number,
): number {
  // method + url + " HTTP/1.1\r\n" → +12 trailing bytes
  const requestLine = String(method).length + String(url).length + 12;
  const body = Math.max(0, Math.trunc(Number(bodyLen) || 0));
  return requestLine + _headersByteLen(headers) + body;
}
