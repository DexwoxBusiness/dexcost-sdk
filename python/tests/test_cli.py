"""Tests for CLI commands (US-021).

Validates:
- dexcost status shows DB location, event count, last task, pricing version, SDKs
- dexcost rates --list / --import / --export manages cost rates
- Entry point is wired via click
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from dexcost.cli import main
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_instrument=[])


@pytest.fixture()
def seeded_db(tmp_path: Any) -> Path:
    """Create a seeded SQLite DB and return its path."""
    db_path = tmp_path / "seeded.db"
    s = SQLiteStorage(db_path=db_path)
    t = CostTracker(storage=s, auto_instrument=[])

    with t.task(task_type="resolve_ticket", customer_id="acme") as task:
        task.record_llm_call("openai", "gpt-4", 200, 100, "0.10")
        task.record_cost(service="google_maps", cost_usd="0.005")

    with t.task(task_type="generate_report", customer_id="beta") as task:
        task.record_llm_call("anthropic", "claude-3", 150, 75, "0.08")

    t.pricing.close()
    s.close()
    return db_path


# ---------------------------------------------------------------------------
# AC7: Built with click library, entry point
# ---------------------------------------------------------------------------


class TestClickEntryPoint:
    """Built with click library, entry point in pyproject.toml."""

    def test_main_is_click_group(self) -> None:
        """main is a click Group."""
        import click

        assert isinstance(main, click.Group)

    def test_main_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "dexcost" in result.output

    def test_subcommands_registered(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert "status" in result.output
        assert "rates" in result.output


# ---------------------------------------------------------------------------
# AC7: dexcost status
# ---------------------------------------------------------------------------


class TestStatusCommand:
    """dexcost status shows DB location, event count, last task, etc."""

    def test_status_with_seeded_db(self, runner: CliRunner, seeded_db: Path) -> None:
        result = runner.invoke(main, ["status", "--db", str(seeded_db)])
        assert result.exit_code == 0
        assert "DB location:" in result.output
        assert str(seeded_db) in result.output
        assert "Event count:" in result.output
        assert "Task count:" in result.output
        assert "Last task:" in result.output
        assert "Pricing version:" in result.output
        assert "SDKs detected:" in result.output

    def test_status_event_count(self, runner: CliRunner, seeded_db: Path) -> None:
        result = runner.invoke(main, ["status", "--db", str(seeded_db)])
        assert result.exit_code == 0
        # 3 events: 1 LLM + 1 external (acme) + 1 LLM (beta)
        assert "Event count:       3" in result.output

    def test_status_task_count(self, runner: CliRunner, seeded_db: Path) -> None:
        result = runner.invoke(main, ["status", "--db", str(seeded_db)])
        assert result.exit_code == 0
        assert "Task count:        2" in result.output

    def test_status_missing_db(self, runner: CliRunner, tmp_path: Any) -> None:
        missing = tmp_path / "nonexistent.db"
        result = runner.invoke(main, ["status", "--db", str(missing)])
        assert result.exit_code == 0
        assert "Database not found" in result.output

    def test_status_last_task_present(self, runner: CliRunner, seeded_db: Path) -> None:
        result = runner.invoke(main, ["status", "--db", str(seeded_db)])
        assert result.exit_code == 0
        # Should show an ISO timestamp, not "(none)"
        assert "(none)" not in result.output.split("Last task:")[1].split("\n")[0]

    def test_status_empty_db(self, runner: CliRunner, tmp_path: Any) -> None:
        db_path = tmp_path / "empty.db"
        s = SQLiteStorage(db_path=db_path)
        s.close()
        result = runner.invoke(main, ["status", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Event count:       0" in result.output
        assert "Task count:        0" in result.output
        assert "(none)" in result.output


# ---------------------------------------------------------------------------
# AC4/5/6: dexcost rates
# ---------------------------------------------------------------------------


class TestRatesCommand:
    """dexcost rates --list / --import / --export manages cost rates."""

    def test_rates_list_empty(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["rates", "--list"])
        assert result.exit_code == 0
        assert "No rates registered" in result.output

    def test_rates_import_and_list(self, runner: CliRunner, tmp_path: Any) -> None:
        yaml_content = (
            "rates:\n"
            "  maps.googleapis.com:\n"
            "    per: request\n"
            '    cost_usd: "0.005"\n'
            "  ocr-api.com:\n"
            "    per: page\n"
            '    cost_usd: "0.01"\n'
        )
        rates_file = tmp_path / "rates.yaml"
        rates_file.write_text(yaml_content, encoding="utf-8")

        result = runner.invoke(main, ["rates", "--import", str(rates_file), "--list"])
        assert result.exit_code == 0
        assert "Loaded 2 rate(s)" in result.output
        assert "maps.googleapis.com" in result.output
        assert "ocr-api.com" in result.output
        assert "request" in result.output
        assert "page" in result.output

    def test_rates_export(self, runner: CliRunner, tmp_path: Any) -> None:
        yaml_content = "rates:\n" "  test-service:\n" "    per: unit\n" '    cost_usd: "0.10"\n'
        rates_file = tmp_path / "input.yaml"
        rates_file.write_text(yaml_content, encoding="utf-8")

        export_path = tmp_path / "exported.yaml"
        result = runner.invoke(
            main,
            ["rates", "--import", str(rates_file), "--export", str(export_path)],
        )
        assert result.exit_code == 0
        assert "Exported 1 rate(s)" in result.output
        assert export_path.exists()
        content = export_path.read_text(encoding="utf-8")
        assert "test-service" in content

    def test_rates_no_action(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["rates"])
        assert result.exit_code == 1
        assert "No action specified" in result.output

    def test_rates_list_table_format(self, runner: CliRunner, tmp_path: Any) -> None:
        yaml_content = "rates:\n" "  api-service:\n" "    per: call\n" '    cost_usd: "0.025"\n'
        rates_file = tmp_path / "rates.yaml"
        rates_file.write_text(yaml_content, encoding="utf-8")

        result = runner.invoke(main, ["rates", "--import", str(rates_file), "--list"])
        assert result.exit_code == 0
        assert "Service" in result.output
        assert "Per" in result.output
        assert "Cost (USD)" in result.output

    def test_rates_import_shows_count(self, runner: CliRunner, tmp_path: Any) -> None:
        yaml_content = (
            "rates:\n"
            "  svc-a:\n"
            "    per: unit\n"
            '    cost_usd: "0.01"\n'
            "  svc-b:\n"
            "    per: unit\n"
            '    cost_usd: "0.02"\n'
            "  svc-c:\n"
            "    per: unit\n"
            '    cost_usd: "0.03"\n'
        )
        rates_file = tmp_path / "rates.yaml"
        rates_file.write_text(yaml_content, encoding="utf-8")

        result = runner.invoke(main, ["rates", "--import", str(rates_file)])
        assert result.exit_code == 0
        assert "Loaded 3 rate(s)" in result.output
