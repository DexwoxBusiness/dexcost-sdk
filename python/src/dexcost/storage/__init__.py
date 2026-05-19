"""Storage backends for dexcost.

Provides a protocol-based abstraction for the SQLite storage backend.
"""

from dexcost.storage.migrations import MigrationError
from dexcost.storage.protocol import StorageBackend
from dexcost.storage.sqlite import SQLiteStorage

__all__ = [
    "MigrationError",
    "SQLiteStorage",
    "StorageBackend",
]
