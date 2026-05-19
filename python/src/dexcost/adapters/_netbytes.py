"""Helpers for the HTTP network adapter: destination classification and
byte measurement. Pure functions — no SDK state, no I/O beyond parsing.
"""

from __future__ import annotations

import ipaddress
from typing import Any


def classify_destination(host: str) -> bool | None:
    """Return whether *host* is internal traffic.

    ``True``  — host is an RFC1918 / loopback / link-local IP literal.
    ``False`` — host is a public IP literal.
    ``None``  — host is a name (not an IP literal); the SDK does not perform
                an extra DNS lookup to resolve it.
    """
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _headers_byte_len(headers: dict[str, Any]) -> int:
    """Approximate on-the-wire size of a header block: ``Key: Value\\r\\n`` each."""
    total = 0
    for key, value in headers.items():
        total += len(str(key)) + len(str(value)) + 4  # ": " + CRLF
    return total + 2  # trailing CRLF that ends the header block


def measure_bytes_from_headers(
    method: str, url: str, headers: dict[str, Any], body_len: int
) -> int:
    """Approximate the on-the-wire byte size of one HTTP message.

    ``request line + header block + body``. Used for both directions: pass
    the request method/url/headers for bytes-out, or ``"" / "" / response
    headers`` for bytes-in. *body_len* is the known body length in bytes.
    """
    request_line = len(str(method)) + len(str(url)) + 12  # method + url + " HTTP/1.1\r\n"
    return request_line + _headers_byte_len(headers) + max(0, int(body_len))
