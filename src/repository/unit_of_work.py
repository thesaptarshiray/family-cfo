"""
Unit of Work: one atomic database transaction shared by several operations.

A `UnitOfWork` is the token for a single open transaction. It is created only
by `Database.unit_of_work()`, which commits when the `with` block exits
cleanly and rolls everything back if anything raises. Repository methods that
are handed the token do their work on its connection and never commit,
roll back or close it — so several repository calls (and, later, the
Financial Engine's multi-step operations) succeed or fail together.

The token is deliberately opaque. It holds the SQLite connection privately;
only code inside the `repository` package may use it. Callers above the
repository layer just pass it along, so they never see SQLite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class UnitOfWork:
    """Handle to one open transaction. Create it with `Database.unit_of_work()`."""

    __slots__ = ("_conn", "_db_path", "_open", "_savepoints")

    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self._db_path = db_path
        self._open = True
        self._savepoints = 0

    def _next_savepoint_name(self) -> str:
        self._savepoints += 1
        return f"repo_op_{self._savepoints}"
