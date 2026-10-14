"""
Low-level SQLite connection and transaction management.

This module owns the single point of contact with SQLite for the whole
application. Nothing outside the `repository` package should import
`sqlite3` directly or hold a connection of its own — every other layer
(Financial Engine, and eventually the UI/AI layer) goes through `Database`.

This module has NO knowledge of the schema's tables and NO financial
business rules. It only knows how to: open a connection with foreign keys
enforced, and provide a transaction boundary. That separation is
deliberate — see the repository/__init__.py docstring and the design
notes in the accompanying chat response (sections B-D) for why.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from .errors import RepositoryError, translate_db_errors
from .unit_of_work import UnitOfWork


class Database:
    """Manages SQLite connections to a single database file.

    Each call to `connect()` or `transaction()` opens a fresh connection.
    SQLite connections are not safe to share across threads, and holding
    one open long-term invites exactly the kind of implicit, un-scoped
    state this foundation is trying to avoid — so no connection is cached
    on the instance.
    """

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        """Open a new connection with foreign-key enforcement turned on.

        Returns a connection with `row_factory = sqlite3.Row` so callers
        can access columns by name. Foreign-key enforcement is OFF by
        default in SQLite for every new connection — it is turned on here,
        explicitly, every time, rather than relying on it being set once
        somewhere else.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Provide one connection scoped to a single atomic transaction.

        Commits on clean exit, rolls back on any exception, and always
        closes the connection afterward. This is the only transaction
        boundary this foundation provides — every multi-statement write in
        later blocks (Repository operations, and eventually the Financial
        Engine's atomic functions) is expected to happen inside one
        `with database.transaction() as conn:` block, so a failure partway
        through never leaves a partial write behind.

        Usage:
            with database.transaction() as conn:
                conn.execute("INSERT INTO ...", (...,))
                conn.execute("INSERT INTO ...", (...,))
            # committed together on success; rolled back together on error
        """
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        """Open one transaction that several operations can share.

        Everything done with the yielded `UnitOfWork` commits together when
        the `with` block exits cleanly, and is rolled back together if
        anything raises. The write lock is taken up front (`BEGIN
        IMMEDIATE`), so a check made inside the block ("is the balance
        sufficient?") cannot be invalidated by another writer before the
        block commits. `sqlite3` errors leave as repository errors.

        Usage:
            with database.unit_of_work() as uow:
                bank_repository.create_account(..., uow=uow)
                family_repository.create_family_member(..., uow=uow)
            # committed together on success; rolled back together on error
        """
        with translate_db_errors():
            conn = self.connect()
            uow = UnitOfWork(conn, self.db_path.resolve())
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield uow
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                uow._open = False
                conn.close()

    @contextmanager
    def scope(self, uow: Optional[UnitOfWork] = None) -> Iterator[sqlite3.Connection]:
        """Connection for one repository operation (repository-internal).

        With no `uow` this is exactly `transaction()`: the operation commits
        on its own. With a `uow` it runs on that unit's connection inside a
        SAVEPOINT and never commits: if the operation fails part-way, only
        that operation is undone, so a caller that catches the error cannot
        go on to commit half of a repository call. Committing or rolling back
        the whole unit is left to the `unit_of_work()` block.
        """
        if uow is None:
            with self.transaction() as conn:
                yield conn
            return

        if not uow._open:
            raise RepositoryError("UnitOfWork is closed")
        if uow._db_path != self.db_path.resolve():
            raise ValueError("UnitOfWork belongs to a different database")

        conn = uow._conn
        name = uow._next_savepoint_name()
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {name}")

    def foreign_keys_enabled(self) -> bool:
        """Report whether foreign-key enforcement is active on a fresh connection.

        Exists mainly so tests (and, later, a startup health check) can
        verify the PRAGMA actually took effect, rather than assuming it did
        because the code that sets it looks right.
        """
        conn = self.connect()
        try:
            cursor = conn.execute("PRAGMA foreign_keys;")
            row = cursor.fetchone()
            return bool(row[0])
        finally:
            conn.close()
