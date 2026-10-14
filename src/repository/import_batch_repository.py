"""
Persistence for import records (`bulk_import_batch`): one row per imported
statement, so every row an import added can say where it came from.
Plain storage only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

_COLUMNS = "batch_id, source_description, status, started_at, completed_at, row_count"


@dataclass(frozen=True)
class ImportBatch:
    batch_id: int
    source_description: str
    status: str
    started_at: str
    completed_at: Optional[str]
    row_count: int


def _from_row(row: sqlite3.Row) -> ImportBatch:
    return ImportBatch(
        batch_id=row["batch_id"], source_description=row["source_description"],
        status=row["status"], started_at=row["started_at"],
        completed_at=row["completed_at"], row_count=row["row_count"],
    )


class ImportBatchRepository(BaseRepository):
    """Create and read import records."""

    def create_batch(
        self, source_description: str, *, uow: Optional[UnitOfWork] = None
    ) -> ImportBatch:
        """Start a record for an import in progress (status STAGED)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO bulk_import_batch (source_description, status) VALUES (?, 'STAGED')",
                    (source_description,),
                )
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM bulk_import_batch WHERE batch_id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
        return _from_row(row)

    def complete_batch(
        self, batch_id: int, row_count: int, *, uow: Optional[UnitOfWork] = None
    ) -> None:
        """Mark an import COMMITTED with how many rows it wrote."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE bulk_import_batch SET status = 'COMMITTED', completed_at = ?, "
                    "row_count = ? WHERE batch_id = ?",
                    (datetime.now().isoformat(timespec="seconds"), row_count, batch_id),
                )

    def list_batches(self, *, uow: Optional[UnitOfWork] = None) -> List[ImportBatch]:
        rows = self._fetch_all(
            f"SELECT {_COLUMNS} FROM bulk_import_batch ORDER BY batch_id DESC", (), uow
        )
        return [_from_row(row) for row in rows]
