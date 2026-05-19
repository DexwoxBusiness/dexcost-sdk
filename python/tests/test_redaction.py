"""Tests for PII redaction and metadata policy (US-018)."""
from __future__ import annotations

from dexcost.redaction import enforce_metadata_limit, hash_value, redact_dict


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
