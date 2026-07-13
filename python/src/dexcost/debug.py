"""Debug logging for capture decisions (parity with TypeScript core/debug.ts).

The SDK's cardinal failure mode is SILENCE: a provider package that cannot
be patched, an LLM call that degrades to a generic network event, a buffer
that silently fell back. Debug mode makes every capture decision loud so an
engineer can answer "why wasn't this call captured?" without reading SDK
source.

Enable with ``init(debug=True)`` or ``DEXCOST_DEBUG=1`` (also accepts
``true``/``yes``/``on``). Output goes to **stderr**, prefixed
``[dexcost:<scope>]``, and is a strict no-op when disabled — call sites can
be left in hot paths.

This is deliberately independent of the stdlib ``logging`` module: it is a
zero-configuration switch (one env var / one init flag) that a support
engineer can flip in the field without the customer having to wire up a
logging handler, and it writes to stderr so it never pollutes a stdout JSON
pipeline.
"""

from __future__ import annotations

import os
import sys

# Truthy values accepted for the DEXCOST_DEBUG environment variable.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Programmatic override — wired from init(debug=...). None means "defer to env".
_override: bool | None = None


def _env_enabled() -> bool:
    """True when DEXCOST_DEBUG is set to a recognized truthy value."""
    try:
        value = os.environ.get("DEXCOST_DEBUG")
    except Exception:  # pragma: no cover - os.environ is always present
        return False
    return value is not None and value.lower() in _TRUTHY


def set_debug_mode(enabled: bool) -> None:
    """Programmatic override — wired from ``init(debug=...)``.

    The override wins over the ``DEXCOST_DEBUG`` environment variable.
    """
    global _override
    _override = bool(enabled)


def _reset_debug_mode_for_tests() -> None:
    """Test-only: clear the override so the env var decides again."""
    global _override
    _override = None


def is_debug_mode() -> bool:
    """Return ``True`` when debug logging is active.

    The ``init(debug=...)`` override takes precedence over the environment
    variable; when no override is set the env var decides.
    """
    if _override is not None:
        return _override
    return _env_enabled()


def debug_log(scope: str, message: str) -> None:
    """Log one capture decision. No-op unless debug mode is active.

    Args:
        scope: Subsystem tag, e.g. ``"instrument"``, ``"http"``, ``"buffer"``.
        message: Human-readable decision, e.g.
            ``"llm_call captured via http fallback (api.kimi.com, kimi-k2)"``.
    """
    if not is_debug_mode():
        return
    # stderr — never pollutes stdout pipelines (CLIs, JSON output).
    print(f"[dexcost:{scope}] {message}", file=sys.stderr)
