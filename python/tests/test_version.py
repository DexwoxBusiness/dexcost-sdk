"""Smoke test — verify the package imports and exposes a version string."""

import dexcost


def test_version_exists() -> None:
    """dexcost.__version__ must be a non-empty semver string."""
    assert hasattr(dexcost, "__version__")
    assert isinstance(dexcost.__version__, str)
    assert dexcost.__version__ == "0.1.0"


def test_package_docstring() -> None:
    """Package-level docstring should be present."""
    assert dexcost.__doc__ is not None
    assert "Agent Unit Economics" in dexcost.__doc__
