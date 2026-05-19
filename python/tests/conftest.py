"""Pytest configuration.

Ensures the local src/dexcost package is found before any namespace-package
stubs that might exist in site-packages (e.g., from a stale editable install
pointing to a deleted worktree).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Insert the local src/ directory at the front of sys.path so that the
# real dexcost package is always preferred over any site-packages stub.
_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
