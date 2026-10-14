"""
Shared plumbing for the concrete repositories.

`BaseRepository` holds the `Database` and the two read helpers every
repository uses. It knows nothing about any table: the SQL always comes from
the subclass, and the helpers only decide which connection runs it.

  - With a `uow`, the read runs on that Unit of Work's connection, so it sees
    the unit's own uncommitted writes.
  - Without one, it opens a short-lived connection of its own and closes it.

Writes are not routed through here: each write method opens its own scope
with `self._db.scope(uow)`, because what it does inside that scope is
specific to the repository.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from .database import Database
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork


class BaseRepository:
    """Base class for repositories: owns the database handle and reads."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def _require_unit_of_work(self, uow: Optional[UnitOfWork], method: str) -> None:
        """Refuse a write made without a Unit of Work.

        For repositories whose writes only make sense as part of a larger
        operation. The `uow` parameter has no default, so leaving it out is a
        TypeError; this catches an explicit `uow=None`, which
        `Database.scope` would otherwise quietly turn into a private,
        self-committing transaction.
        """
        if uow is None:
            raise ValueError(f"{type(self).__name__}.{method} requires a UnitOfWork")

    def _fetch_one(
        self, sql: str, params: tuple, uow: Optional[UnitOfWork] = None
    ) -> Optional[sqlite3.Row]:
        with translate_db_errors():
            if uow is not None:
                with self._db.scope(uow) as conn:
                    return conn.execute(sql, params).fetchone()
            conn = self._db.connect()
            try:
                return conn.execute(sql, params).fetchone()
            finally:
                conn.close()

    def _fetch_all(
        self, sql: str, params: tuple, uow: Optional[UnitOfWork] = None
    ) -> List[sqlite3.Row]:
        with translate_db_errors():
            if uow is not None:
                with self._db.scope(uow) as conn:
                    return conn.execute(sql, params).fetchall()
            conn = self._db.connect()
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
