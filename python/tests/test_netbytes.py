"""Destination classification and byte measurement helpers."""

from dexcost.adapters._netbytes import classify_destination, measure_bytes_from_headers


def test_private_ipv4_is_internal():
    assert classify_destination("10.1.2.3") is True
    assert classify_destination("192.168.0.5") is True
    assert classify_destination("172.16.9.9") is True


def test_localhost_and_link_local_are_internal():
    assert classify_destination("127.0.0.1") is True
    assert classify_destination("::1") is True
    assert classify_destination("169.254.10.1") is True


def test_public_ip_is_not_internal():
    assert classify_destination("8.8.8.8") is False
    assert classify_destination("1.1.1.1") is False


def test_named_host_is_unknown():
    # A hostname (not an IP literal): we do not do an extra DNS lookup.
    assert classify_destination("api.openai.com") is None
    assert classify_destination("") is None


def test_measure_bytes_includes_headers_and_body():
    headers = {"Content-Length": "2048", "Content-Type": "application/json"}
    # request line + header bytes + body length
    n = measure_bytes_from_headers("POST", "https://x.com/v1/y", headers, body_len=2048)
    assert n >= 2048
    # headers contribute too
    assert n > 2048


def test_measure_bytes_exact_total():
    # Pin the +4/+2/+12 constants against silent regression.
    # Input: method="GET", url="https://a.io/", headers={"X-H": "v"}, body_len=0
    # request_line = len("GET") + len("https://a.io/") + 12 = 3 + 13 + 12 = 28
    # headers: (len("X-H") + len("v") + 4) + 2 = (3 + 1 + 4) + 2 = 10
    # body = 0
    # total = 28 + 10 + 0 = 38
    n = measure_bytes_from_headers("GET", "https://a.io/", {"X-H": "v"}, body_len=0)
    assert n == 38


def test_ipv6_ula_is_internal():
    # fd00::/8 is IPv6 unique-local (RFC 4193) — must be classified internal.
    assert classify_destination("fd00::1") is True


def test_measure_bytes_zero_body():
    n = measure_bytes_from_headers("GET", "https://x.com/", {}, body_len=0)
    assert n > 0  # request line + minimal headers still cost bytes
