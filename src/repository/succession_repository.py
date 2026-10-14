"""
Persistence for each person's hand-over checklist (`succession_check`,
migration 011): which of the items the Financial Health tab lists are done.
Plain storage only.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Optional

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork


class SuccessionRepository(BaseRepository):
    def list_checks(self, member_id: int, *, uow: Optional[UnitOfWork] = None) -> Dict[str, bool]:
        rows = self._fetch_all(
            "SELECT item, done FROM succession_check WHERE member_id = ?", (member_id,), uow
        )
        return {row["item"]: bool(row["done"]) for row in rows}

    def set_check(
        self, member_id: int, item: str, done: bool, on: date, *, uow: Optional[UnitOfWork] = None
    ) -> None:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "INSERT INTO succession_check (member_id, item, done, updated_on) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (member_id, item) DO UPDATE SET done = excluded.done, "
                    "updated_on = excluded.updated_on",
                    (member_id, item, int(done), on.isoformat()),
                )
