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


def test_measure_bytes_from_content_length():
    headers = {"Content-Length": "2048", "Content-Type": "application/json"}
    # request line + header bytes + body length
    n = measure_bytes_from_headers("POST", "https://x.com/v1/y", headers, body_len=2048)
    assert n >= 2048
    # headers contribute too
    assert n > 2048


def test_measure_bytes_zero_body():
    n = measure_bytes_from_headers("GET", "https://x.com/", {}, body_len=0)
    assert n > 0  # request line + minimal headers still cost bytes
