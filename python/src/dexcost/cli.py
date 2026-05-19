"""CLI commands for dexcost (US-021).

Provides terminal access to common dexcost operations:

- ``dexcost status`` — show DB location, event count, last task, etc.
- ``dexcost rates`` — list, import, or export cost rates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click


@click.group()
def main() -> None:
    """dexcost — Agent Unit Economics SDK CLI."""


# ---------------------------------------------------------------------------
# dexcost status
# ---------------------------------------------------------------------------


@main.command()
@click.option("--db", default=None, help="Path to SQLite database file.")
def status(db: str | None) -> None:
    """Show DB location, event count, last task timestamp, pricing data age, SDK versions."""
    from dexcost.storage.sqlite import _DEFAULT_DB_PATH, SQLiteStorage

    db_path = Path(db) if db else _DEFAULT_DB_PATH
    if not db_path.exists():
        click.echo(f"DB location:       {db_path}")
        click.echo("Status:            Database not found")
        return

    storage = SQLiteStorage(db_path=db_path)
    try:
        _print_status(storage, db_path)
    finally:
        storage.close()


def _print_status(storage: Any, db_path: Path) -> None:
    """Render status output."""
    import sqlite3

    click.echo(f"DB location:       {db_path}")

    try:
        # Event count
        conn = storage._conn
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        event_count: int = row[0] if row else 0
        click.echo(f"Event count:       {event_count}")

        # Task count
        row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        task_count: int = row[0] if row else 0
        click.echo(f"Task count:        {task_count}")

        # Last task timestamp
        row = conn.execute("SELECT started_at FROM tasks ORDER BY started_at DESC LIMIT 1").fetchone()
        if row:
            click.echo(f"Last task:         {row[0]}")
        else:
            click.echo("Last task:         (none)")

        # Sync status
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE sync_status = 'pending'"
        ).fetchone()
        pending: int = row[0] if row else 0
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE sync_status = 'synced'"
        ).fetchone()
        synced: int = row[0] if row else 0
        click.echo(f"Pending sync:      {pending}")
        click.echo(f"Synced:            {synced}")
    except (sqlite3.OperationalError, AttributeError) as exc:
        click.echo(f"Error reading database: {exc}")

    # Pricing data age
    from dexcost.pricing import PricingEngine

    engine = PricingEngine(auto_update=False)
    click.echo(f"Pricing version:   {engine.pricing_version}")
    engine.close()

    # Config
    from dexcost.config import DexcostConfig

    cfg = DexcostConfig()
    click.echo(f"Mode:              {cfg.storage_mode}")
    if cfg.key_type:
        click.echo(f"Key type:          {cfg.key_type}")

    # SDK versions detected
    _print_sdk_versions()


def _print_sdk_versions() -> None:
    """Detect and print installed SDK versions."""
    sdks = {"openai": "openai", "anthropic": "anthropic", "litellm": "litellm"}
    detected: list[str] = []
    for label, module_name in sdks.items():
        try:
            mod = __import__(module_name)
            version = getattr(mod, "__version__", "unknown")
            detected.append(f"{label}=={version}")
        except ImportError:
            pass

    if detected:
        click.echo(f"SDKs detected:     {', '.join(detected)}")
    else:
        click.echo("SDKs detected:     (none)")


# ---------------------------------------------------------------------------
# dexcost rates
# ---------------------------------------------------------------------------


@main.command()
@click.option("--list", "list_rates", is_flag=True, help="Show all registered rates.")
@click.option(
    "--import",
    "import_path",
    default=None,
    type=click.Path(exists=True),
    help="Import rates from YAML file.",
)
@click.option("--export", "export_path", default=None, help="Export rates to YAML file.")
@click.option("--db", default=None, help="Path to SQLite database file.")
def rates(
    list_rates: bool,
    import_path: str | None,
    export_path: str | None,
    db: str | None,
) -> None:
    """List, import, or export cost rates."""
    from dexcost.rates import RateRegistry

    registry = RateRegistry()

    # If importing, load the file first
    if import_path:
        registry.load(import_path)
        click.echo(f"Loaded {len(registry.rates)} rate(s) from {import_path}")

    if list_rates:
        all_rates = registry.rates
        if not all_rates:
            click.echo("No rates registered.")
        else:
            click.echo(f"{'Service':<40} {'Per':<15} {'Cost (USD)':<15}")
            click.echo("-" * 70)
            for service in sorted(all_rates):
                entry = all_rates[service]
                click.echo(f"{service:<40} {entry.per:<15} {entry.cost_usd:<15}")

    if export_path:
        registry.export(export_path)
        click.echo(f"Exported {len(registry.rates)} rate(s) to {export_path}")

    if not list_rates and not import_path and not export_path:
        click.echo("No action specified. Use --list, --import, or --export.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# dexcost scan
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", default=".", type=click.Path())
@click.option(
    "--generate-stubs", "gen_stubs", is_flag=True, help="Generate record_cost() snippets."
)
def scan(path: str, gen_stubs: bool) -> None:
    """Scan codebase for cost points (US-019). Pure static analysis, no API key needed."""
    from dexcost.scanner import generate_stubs as _gen_stubs
    from dexcost.scanner import scan_directory

    target = Path(path)
    if not target.exists():
        click.echo(f"Path not found: {path}")
        sys.exit(1)

    result = scan_directory(target)

    if not result.cost_points:
        click.echo(f"Scanned {result.files_scanned} file(s). No cost points found.")
        return

    auto = [cp for cp in result.cost_points if cp.auto_instrumented]
    manual = [cp for cp in result.cost_points if not cp.auto_instrumented]

    click.echo(f"\nScanned {result.files_scanned} file(s)\n")

    if auto:
        click.echo("LLM CALLS (auto-instrumented)")
        for cp in auto:
            click.echo(f"  [auto] {cp.file}:{cp.line}  {cp.description}")

    if manual:
        click.echo("\nNEED record_cost()")
        for cp in manual:
            click.echo(f"  [manual] {cp.file}:{cp.line}  {cp.description}")

    click.echo("\nSUMMARY")
    click.echo(f"  {len(auto)} LLM call(s) auto-instrumented")
    click.echo(f"  {len(manual)} cost point(s) need record_cost()")

    if gen_stubs and manual:
        click.echo("\nGENERATED STUBS:\n")
        click.echo(_gen_stubs(result))
