"""Versioned User-Agent shared by DexCost control-plane requests."""

from __future__ import annotations


def sdk_user_agent() -> str:
    """Return the public Python SDK identity after package initialization."""
    from dexcost import __version__

    return f"dexcost-python/{__version__}"
