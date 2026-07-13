"""Tests for ``dexcost doctor`` diagnostics (parity with TS cli/doctor.ts)."""

from __future__ import annotations

from typing import Any

from click.testing import CliRunner

from dexcost.cli import main
from dexcost.doctor import format_doctor_report, run_doctor


class TestRunDoctor:
    def test_core_checks_pass_offline(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DEXCOST_API_KEY", raising=False)
        report = run_doctor(offline=True)
        ids = {c.id: c for c in report.checks}
        assert ids["runtime"].status == "ok"
        assert ids["context"].status == "ok"
        assert ids["sqlite"].status == "ok"
        assert ids["endpoint"].status == "skip"
        # No API key -> warn (LOCAL mode), never a hard failure.
        assert ids["apikey"].status == "warn"
        assert report.healthy  # warnings don't make it unhealthy

    def test_invalid_api_key_fails(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DEXCOST_API_KEY", raising=False)
        report = run_doctor(api_key="not-a-valid-key", offline=True)
        apikey = next(c for c in report.checks if c.id == "apikey")
        assert apikey.status == "fail"
        assert not report.healthy

    def test_valid_api_key_ok(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DEXCOST_API_KEY", raising=False)
        report = run_doctor(api_key="dx_test_abc123", offline=True)
        apikey = next(c for c in report.checks if c.id == "apikey")
        assert apikey.status == "ok"

    def test_api_key_read_from_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DEXCOST_API_KEY", "dx_live_fromenv")
        report = run_doctor(offline=True)
        apikey = next(c for c in report.checks if c.id == "apikey")
        assert apikey.status == "ok"

    def test_provider_checks_present_for_all_instruments(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DEXCOST_API_KEY", raising=False)
        report = run_doctor(offline=True)
        provider_ids = {c.id for c in report.checks if c.id.startswith("provider:")}
        assert provider_ids == {
            f"provider:{name}"
            for name in ("openai", "anthropic", "litellm", "gemini", "bedrock", "cohere", "mcp")
        }


class TestFormatReport:
    def test_renders_header_and_verdict(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DEXCOST_API_KEY", raising=False)
        text = format_doctor_report(run_doctor(offline=True))
        assert "dexcost doctor" in text
        assert "Healthy" in text or "UNHEALTHY" in text


class TestDoctorCLI:
    def test_cli_offline_exit_zero_when_healthy(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DEXCOST_API_KEY", raising=False)
        result = CliRunner().invoke(main, ["doctor", "--offline"])
        assert result.exit_code == 0
        assert "dexcost doctor" in result.output

    def test_cli_bad_key_exit_one(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("DEXCOST_API_KEY", raising=False)
        result = CliRunner().invoke(
            main, ["doctor", "--offline", "--api-key", "bogus"]
        )
        assert result.exit_code == 1
        assert "UNHEALTHY" in result.output
