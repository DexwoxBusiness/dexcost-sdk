"""Tests for PII redaction and metadata policy (US-018)."""
from __future__ import annotations

from dexcost.redaction import enforce_metadata_limit, hash_value, redact_dict, scrub_url


class TestRedactDict:
    def test_strips_specified_keys(self) -> None:
        data = {"email": "user@example.com", "cost": 5, "name": "Alice"}
        result = redact_dict(data, ["email"])
        assert result == {"cost": 5, "name": "Alice"}

    def test_strips_multiple_keys(self) -> None:
        data = {"email": "x", "prompt": "hello", "keep": True}
        result = redact_dict(data, ["email", "prompt"])
        assert result == {"keep": True}

    def test_strips_nested_keys(self) -> None:
        data = {"user": {"email": "x", "id": 1}, "cost": 5}
        result = redact_dict(data, ["email"])
        assert result == {"user": {"id": 1}, "cost": 5}

    def test_strips_deeply_nested(self) -> None:
        data = {"a": {"b": {"email": "x", "ok": True}}}
        result = redact_dict(data, ["email"])
        assert result == {"a": {"b": {"ok": True}}}

    def test_empty_fields_no_change(self) -> None:
        data = {"email": "x", "name": "y"}
        result = redact_dict(data, [])
        assert result == data

    def test_empty_dict(self) -> None:
        assert redact_dict({}, ["email"]) == {}

    def test_no_matching_keys(self) -> None:
        data = {"name": "Alice", "age": 30}
        result = redact_dict(data, ["email"])
        assert result == data


class TestHashValue:
    def test_returns_sha256_hex(self) -> None:
        result = hash_value("acme-corp")
        assert len(result) == 64  # SHA-256 hex is 64 chars
        assert result.isalnum()

    def test_deterministic(self) -> None:
        assert hash_value("acme-corp") == hash_value("acme-corp")

    def test_different_inputs_different_hashes(self) -> None:
        assert hash_value("acme-corp") != hash_value("globex-inc")

    def test_empty_string(self) -> None:
        result = hash_value("")
        assert len(result) == 64


class TestEnforceMetadataLimit:
    def test_under_limit_unchanged(self) -> None:
        data = {"key": "small value"}
        assert enforce_metadata_limit(data) == data

    def test_exactly_at_limit(self) -> None:
        # Create a dict that serializes to exactly 10KB or less
        data = {"x": "a" * 9000}
        result = enforce_metadata_limit(data)
        assert result == data  # Should not be truncated

    def test_over_limit_truncated(self) -> None:
        # Create a dict that exceeds 10KB
        data = {"x": "a" * 20000}
        result = enforce_metadata_limit(data)
        assert result["_truncated"] is True
        assert "_original_size_bytes" in result
        assert result["_original_size_bytes"] > 10 * 1024

    def test_empty_dict(self) -> None:
        assert enforce_metadata_limit({}) == {}

    def test_nested_large_dict(self) -> None:
        data = {"nested": {"big": "x" * 20000}}
        result = enforce_metadata_limit(data)
        assert result["_truncated"] is True


class TestScrubUrl:
    def test_empty_returns_empty(self) -> None:
        assert scrub_url("") == ""

    def test_no_credentials_unchanged(self) -> None:
        url = "https://api.example.com/v1/chat?page=2&limit=50"
        assert scrub_url(url) == url

    def test_strips_basic_auth_userinfo(self) -> None:
        assert (
            scrub_url("https://alice:s3cr3t@api.example.com/v1/chat")
            == "https://api.example.com/v1/chat"
        )

    def test_strips_username_only(self) -> None:
        # userinfo without password still gets dropped
        assert (
            scrub_url("https://token123@api.example.com/path")
            == "https://api.example.com/path"
        )

    def test_strips_api_key_query(self) -> None:
        url = "https://api.example.com/v1?api_key=sk-proj-secret&page=2"
        assert scrub_url(url) == "https://api.example.com/v1?api_key=REDACTED&page=2"

    def test_strips_case_insensitive(self) -> None:
        # ApiKey, API_KEY, AUTHORIZATION-type variations
        url = "https://api.example.com/?ApiKey=abc&AUTH=xyz&keep=1"
        out = scrub_url(url)
        assert "ApiKey=REDACTED" in out
        assert "AUTH=REDACTED" in out
        assert "keep=1" in out

    def test_strips_aws_sigv4_signature_and_credential(self) -> None:
        url = (
            "https://my-bucket.s3.amazonaws.com/obj.json"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIA%2F20260526%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260526T123456Z"
            "&X-Amz-Signature=abcdef1234567890"
        )
        out = scrub_url(url)
        assert "X-Amz-Credential=REDACTED" in out
        assert "X-Amz-Signature=REDACTED" in out
        # algorithm + date are not secrets, preserve them
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in out
        assert "X-Amz-Date=20260526T123456Z" in out

    def test_strips_security_token_suffix(self) -> None:
        url = "https://api.aws.amazon.com/?X-Amz-Security-Token=FQoG&page=1"
        out = scrub_url(url)
        assert "X-Amz-Security-Token=REDACTED" in out
        assert "page=1" in out

    def test_preserves_fragment(self) -> None:
        url = "https://docs.example.com/api?api_key=secret#installation"
        out = scrub_url(url)
        assert out == "https://docs.example.com/api?api_key=REDACTED#installation"

    def test_preserves_path_and_port(self) -> None:
        url = "https://api.example.com:8443/v2/agents/run?token=xyz"
        out = scrub_url(url)
        assert out == "https://api.example.com:8443/v2/agents/run?token=REDACTED"

    def test_no_query_returns_unchanged(self) -> None:
        url = "https://api.example.com/v1/path/segment"
        assert scrub_url(url) == url

    def test_value_with_equals_sign_in_value(self) -> None:
        # api_key value containing '=' should not split-and-leak
        url = "https://api.example.com/?api_key=abc==pad&keep=ok"
        out = scrub_url(url)
        assert out == "https://api.example.com/?api_key=REDACTED&keep=ok"

    def test_param_without_value(self) -> None:
        # bare query param (no `=`) should be preserved if not sensitive
        url = "https://api.example.com/?debug&token=secret"
        out = scrub_url(url)
        assert "debug" in out
        assert "token=REDACTED" in out

    def test_deepgram_free_form_values_are_redacted(self) -> None:
        url = (
            "https://api.deepgram.com/v1/listen?model=nova-3&language=multi"
            "&keyterm=Acme%20Secret&custom_topic=Roadmap"
        )
        assert scrub_url(url) == (
            "https://api.deepgram.com/v1/listen?model=nova-3&language=multi"
            "&keyterm=REDACTED&custom_topic=REDACTED"
        )
