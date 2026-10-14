"""
Repository-level exceptions.

Deliberately tiny. The Repository reports *what SQLite said*, not what it
means for the family's finances — interpreting a failure (e.g. "this family
doesn't exist, so tell the user to pick another") belongs to the caller.

    RepositoryError            any unexpected database failure
      RepositoryIntegrityError a declared constraint rejected the write
                               (FOREIGN KEY, NOT NULL, CHECK, UNIQUE)

Callers that only need to know "the write was refused" catch
`RepositoryIntegrityError`; callers that want to handle everything catch
`RepositoryError`. The original `sqlite3` exception is always chained as
`__cause__` so no detail is lost.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


class RepositoryError(Exception):
    """An unexpected database failure (I/O error, missing table, locked DB, ...)."""


class RepositoryIntegrityError(RepositoryError):
    """The database rejected a write because it violates a declared constraint."""


@contextmanager
def translate_db_errors() -> Iterator[None]:
    """Re-raise `sqlite3` errors as repository exceptions.

    Wrap this *around* a `Database.transaction()` block, so the transaction
    has already rolled back and closed its connection by the time the
    translated exception reaches the caller.
    """
    try:
        yield
    except sqlite3.IntegrityError as exc:
        raise RepositoryIntegrityError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise RepositoryError(str(exc)) from exc
