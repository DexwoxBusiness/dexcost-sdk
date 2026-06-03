"""Smoke test — verify the package imports and exposes a version string."""

import re

import dexcost


def test_version_exists() -> None:
    """dexcost.__version__ must be a non-empty semver string."""
    assert hasattr(dexcost, "__version__")
    assert isinstance(dexcost.__version__, str)
    # Assert the FORMAT (MAJOR.MINOR.PATCH with an optional pre-release/build
    # suffix), not a hardcoded value — otherwise this breaks on every release
    # bump (e.g. release-please moving 0.1.0 -> 0.1.1).
    assert re.match(r"^\d+\.\d+\.\d+", dexcost.__version__) is not None


def test_package_docstring() -> None:
    """Package-level docstring should be present."""
    assert dexcost.__doc__ is not None
    assert "Agent Unit Economics" in dexcost.__doc__
