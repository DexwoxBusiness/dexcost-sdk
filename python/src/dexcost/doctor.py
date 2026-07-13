"""``dexcost doctor`` — diagnostic self-check (parity with TS cli/doctor.ts).

The SDK's cardinal failure mode is silent no-capture: a missing provider
package, an unreachable endpoint, an API key that never got set. ``doctor``
makes each of those loud and actionable without the engineer reading SDK
source. Every check is individually exception-guarded so one failure can
never abort the report.

Checks that are JS-runtime-specific in the TypeScript SDK are intentionally
omitted or replaced by their Python analog:

* AsyncLocalStorage round-trip  → contextvars round-trip.
* better-sqlite3 / bun:sqlite native-binding probe → N/A (Python ships
  ``sqlite3`` in the stdlib); replaced by a buffer write/read round-trip.
* ``globalThis.fetch`` patch install → N/A (Python patches per-library HTTP
  clients, covered by the provider-package check).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Literal

DoctorStatus = Literal["ok", "warn", "fail", "skip"]

# Instrument name -> (import module name, pip package name) for the
# provider-package check. Import name and pip name diverge for some providers
# (gemini imports ``google.genai`` from the ``google-genai`` package;
# bedrock uses ``boto3``).
_PROVIDER_IMPORTS: dict[str, tuple[str, str]] = {
    "openai": ("openai", "openai"),
    "anthropic": ("anthropic", "anthropic"),
    "litellm": ("litellm", "litellm"),
    "gemini": ("google.genai", "google-genai"),
    "bedrock": ("boto3", "boto3"),
    "cohere": ("cohere", "cohere"),
    "mcp": ("mcp", "mcp"),
}

# Minimum supported Python (pyproject target-version = py310).
_MIN_PYTHON = (3, 10)


@dataclass
class DoctorCheck:
    id: str
    name: str
    status: DoctorStatus
    detail: str
    remedy: str | None = None


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_runtime() -> DoctorCheck:
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info[:2] < _MIN_PYTHON:
        return DoctorCheck(
            id="runtime",
            name="Python version",
            status="fail",
            detail=f"Python {version} is below the minimum {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}",
            remedy=f"Upgrade to Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+.",
        )
    return DoctorCheck(
        id="runtime",
        name="Python version",
        status="ok",
        detail=f"Python {version}",
    )


def _check_contextvars() -> DoctorCheck:
    """Task context propagation round-trip (contextvars analog of ALS)."""
    from dexcost.context import get_current_task, set_current_task
    from dexcost.models.task import Task

    previous = get_current_task()
    try:
        probe = Task(task_type="__doctor_probe__")
        set_current_task(probe)
        seen = get_current_task()
        if seen is not None and seen.task_id == probe.task_id:
            return DoctorCheck(
                id="context",
                name="Task context (contextvars)",
                status="ok",
                detail="active task resolves correctly",
            )
        return DoctorCheck(
            id="context",
            name="Task context (contextvars)",
            status="fail",
            detail="set_current_task did not round-trip",
        )
    finally:
        set_current_task(previous)


def _check_sqlite() -> DoctorCheck:
    """Buffer write/read round-trip against a throwaway in-memory DB."""
    try:
        from dexcost.models.event import Event
        from dexcost.models.task import Task
        from dexcost.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=":memory:")
        try:
            task = Task(task_type="__doctor_probe__")
            storage.insert_task(task)
            storage.insert_event(
                Event(task_id=task.task_id, event_type="external_cost")
            )
            read_back = storage.get_task(str(task.task_id))
        finally:
            storage.close()
        if read_back is not None:
            return DoctorCheck(
                id="sqlite",
                name="SQLite durable buffer",
                status="ok",
                detail="write/read round-trip OK (stdlib sqlite3)",
            )
        return DoctorCheck(
            id="sqlite",
            name="SQLite durable buffer",
            status="fail",
            detail="inserted task could not be read back",
        )
    except Exception as exc:  # pragma: no cover - stdlib sqlite3 is always present
        return DoctorCheck(
            id="sqlite",
            name="SQLite durable buffer",
            status="fail",
            detail=f"buffer round-trip crashed: {exc}",
        )


def _check_provider_packages() -> list[DoctorCheck]:
    import importlib
    import importlib.util

    checks: list[DoctorCheck] = []
    for instrument, (module_name, pip_name) in _PROVIDER_IMPORTS.items():
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            checks.append(
                DoctorCheck(
                    id=f"provider:{instrument}",
                    name=f"Provider '{instrument}'",
                    status="warn",
                    detail=f"package '{module_name}' not installed — calls won't be auto-captured",
                    remedy=f"pip install {pip_name} if you use it.",
                )
            )
            continue
        version = "unknown"
        try:
            top = module_name.split(".")[0]
            mod = importlib.import_module(top)
            version = getattr(mod, "__version__", "unknown")
        except Exception:
            pass
        checks.append(
            DoctorCheck(
                id=f"provider:{instrument}",
                name=f"Provider '{instrument}'",
                status="ok",
                detail=f"{module_name}=={version}",
            )
        )
    return checks


def _check_api_key(api_key: str | None) -> DoctorCheck:
    from dexcost.config import InvalidAPIKeyError, validate_api_key

    if not api_key:
        return DoctorCheck(
            id="apikey",
            name="API key",
            status="warn",
            detail="absent — SDK runs in LOCAL mode (events buffered, never pushed)",
            remedy="Set DEXCOST_API_KEY (or init(api_key=...)) to enable cloud sync.",
        )
    try:
        validate_api_key(api_key)
        return DoctorCheck(
            id="apikey", name="API key", status="ok", detail="present, format valid"
        )
    except InvalidAPIKeyError as exc:
        return DoctorCheck(
            id="apikey",
            name="API key",
            status="fail",
            detail=f"format invalid: {exc}",
            remedy="Copy the key from the dexcost dashboard (dx_live_... / dx_test_...).",
        )


def _check_endpoint(
    api_key: str | None, endpoint: str | None, offline: bool
) -> DoctorCheck:
    if offline:
        return DoctorCheck(
            id="endpoint",
            name="Endpoint reachability",
            status="skip",
            detail="skipped (--offline)",
        )
    import urllib.error
    import urllib.request

    from dexcost.config import DexcostConfig

    resolved = DexcostConfig(endpoint_override=endpoint, storage="local").endpoint
    try:
        req = urllib.request.Request(resolved, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:  # HEAD probe
            status = getattr(resp, "status", 200)
        return DoctorCheck(
            id="endpoint",
            name="Endpoint reachability",
            status="ok",
            detail=f"{resolved} reachable (HTTP {status})",
        )
    except urllib.error.HTTPError as exc:
        # Any HTTP response (even 401/404) proves reachability.
        return DoctorCheck(
            id="endpoint",
            name="Endpoint reachability",
            status="ok",
            detail=f"{resolved} reachable (HTTP {exc.code})",
        )
    except Exception as exc:
        return DoctorCheck(
            id="endpoint",
            name="Endpoint reachability",
            status="fail" if api_key else "warn",
            detail=f"cannot reach endpoint: {exc}",
            remedy=(
                "Check network egress/proxy rules from this environment. In LOCAL "
                "mode (no API key) this is harmless. Use --offline to skip."
            ),
        )


# ---------------------------------------------------------------------------
# Runner + renderer
# ---------------------------------------------------------------------------


def run_doctor(
    api_key: str | None = None,
    endpoint: str | None = None,
    offline: bool = False,
) -> DoctorReport:
    """Run every doctor check. Never raises."""
    import os

    resolved_key = api_key if api_key is not None else os.environ.get("DEXCOST_API_KEY")

    report = DoctorReport()
    simple_checks = [_check_runtime, _check_contextvars, _check_sqlite]
    for check in simple_checks:
        try:
            report.checks.append(check())
        except Exception as exc:  # pragma: no cover - defensive
            report.checks.append(
                DoctorCheck(
                    id="internal",
                    name=getattr(check, "__name__", "internal check"),
                    status="fail",
                    detail=f"check crashed: {exc}",
                )
            )

    try:
        report.checks.extend(_check_provider_packages())
    except Exception as exc:  # pragma: no cover - defensive
        report.checks.append(
            DoctorCheck(
                id="providers", name="Provider packages", status="fail",
                detail=f"check crashed: {exc}",
            )
        )

    report.checks.append(_check_api_key(resolved_key))
    report.checks.append(_check_endpoint(resolved_key, endpoint, offline))
    return report


_SYMBOLS: dict[DoctorStatus, str] = {"ok": "✓", "warn": "⚠", "fail": "✗", "skip": "-"}


def format_doctor_report(report: DoctorReport) -> str:
    """Render a report to a string. Does not print."""
    lines = ["dexcost doctor", ""]
    for check in report.checks:
        lines.append(f"  {_SYMBOLS[check.status]} {check.name}: {check.detail}")
        if check.remedy and check.status != "ok":
            lines.append(f"      -> {check.remedy}")
    fails = sum(1 for c in report.checks if c.status == "fail")
    warns = sum(1 for c in report.checks if c.status == "warn")
    verdict = "Healthy" if report.healthy else "UNHEALTHY"
    lines.append("")
    lines.append(f"{verdict} - {fails} failure(s), {warns} warning(s).")
    return "\n".join(lines)
