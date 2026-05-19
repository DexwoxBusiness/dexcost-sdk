"""Regression tests for the public ``dexcost`` package surface (DEX-330).

These tests guard the invariant that every ``instrument_*`` / ``uninstrument_*``
adapter helper imported at the top of ``dexcost/__init__.py`` is also exported
in ``dexcost.__all__``. They catch the silent-export-gap class of bugs (e.g.
``instrument_mcp`` was imported but not re-exported, hiding it from
``from dexcost import *``, IDE auto-imports, and Sphinx autodoc).
"""

from __future__ import annotations

import ast
from pathlib import Path

import dexcost

_INIT_PATH = Path(dexcost.__file__).resolve()


def _imported_adapter_names() -> set[str]:
    """Return every ``instrument_*`` / ``uninstrument_*`` symbol imported
    from ``dexcost.instruments`` at the top of ``dexcost/__init__.py``.

    Walks the AST instead of grepping so the test stays robust against
    formatting changes (line wraps, trailing commas, comments).
    """
    tree = ast.parse(_INIT_PATH.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dexcost.instruments":
            for alias in node.names:
                name = alias.asname or alias.name
                if name.startswith("instrument_") or name.startswith("uninstrument_"):
                    found.add(name)
    return found


def test_every_imported_adapter_is_in_dunder_all() -> None:
    """Every adapter helper imported into ``dexcost`` must also be in ``__all__``.

    Regression for DEX-330: ``instrument_mcp`` / ``uninstrument_mcp`` were
    imported but missing from ``__all__``.
    """
    imported = _imported_adapter_names()
    assert imported, "expected at least one instrument_* import in dexcost/__init__.py"
    exported = set(dexcost.__all__)
    missing = sorted(imported - exported)
    assert not missing, (
        f"adapter helpers imported in dexcost/__init__.py but missing from "
        f"dexcost.__all__: {missing}. Either add them to __all__ or drop the "
        f"import."
    )


def test_instrument_uninstrument_pairs_are_symmetric() -> None:
    """Every ``instrument_X`` in ``__all__`` must have a matching
    ``uninstrument_X`` (and vice versa). Half-paired adapters are an API smell.
    """
    exported = set(dexcost.__all__)
    instrumenters = {
        name[len("instrument_") :]
        for name in exported
        if name.startswith("instrument_") and name != "instrument_"
    }
    uninstrumenters = {
        name[len("uninstrument_") :]
        for name in exported
        if name.startswith("uninstrument_") and name != "uninstrument_"
    }
    only_instrument = sorted(instrumenters - uninstrumenters)
    only_uninstrument = sorted(uninstrumenters - instrumenters)
    assert (
        not only_instrument
    ), f"instrument_X exported without matching uninstrument_X: {only_instrument}"
    assert (
        not only_uninstrument
    ), f"uninstrument_X exported without matching instrument_X: {only_uninstrument}"


def test_mcp_helpers_specifically_exported() -> None:
    """Belt-and-suspenders for DEX-330: the two MCP names must be present and
    callable, in case the broader parity test ever gets generalised away.
    """
    assert "instrument_mcp" in dexcost.__all__
    assert "uninstrument_mcp" in dexcost.__all__
    assert callable(dexcost.instrument_mcp)
    assert callable(dexcost.uninstrument_mcp)


def test_dunder_all_is_sorted() -> None:
    """``dexcost.__all__`` is intentionally kept in lexicographic order.

    A sorted list makes diffs minimal and stops two adapter additions from
    landing the same name in two places. This is a soft convention check —
    bump the list if the convention ever changes.
    """
    actual = list(dexcost.__all__)
    assert actual == sorted(actual), (
        "dexcost.__all__ is not sorted alphabetically; out-of-order entries: "
        f"{[(a, b) for a, b in zip(actual, sorted(actual), strict=True) if a != b][:5]}"
    )
