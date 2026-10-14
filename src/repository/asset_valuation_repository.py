"""
Persistence for dated market-value history (`asset_valuation`).

The `source` column (`AMFI`/`NSE`/`MANUAL`/`OTHER`) is what lets a valuation
say where it came from — a manual entry today, an AMFI NAV feed later. For
MF, `market_value` holds the NAV (price per unit), not a total holding
value — the Financial Engine multiplies by units to get a current value.
That is a per-category interpretation the Engine owns; this repository just
stores whatever number and source it is given.

The schema allows at most one non-correction valuation per (asset, date) —
`uq_asset_valuation_noncorrected` — so recording a second one for a date
that already has one raises `RepositoryIntegrityError`, the same as any
other constraint violation; this module does not upsert or special-case it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import List, Optional, Union

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

Money = Union[Decimal, int, str]

_SELECT = (
    "SELECT valuation_id, asset_id, valuation_date, market_value, source, "
    "data_confidence, is_correction FROM asset_valuation WHERE valuation_id = ?"
)


@dataclass(frozen=True)
class AssetValuation:
    """One row from ``asset_valuation``."""

    valuation_id: int
    asset_id: int
    valuation_date: date
    market_value: Decimal
    source: str
    data_confidence: str
    is_correction: bool


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _valuation_from_row(row: sqlite3.Row) -> AssetValuation:
    return AssetValuation(
        valuation_id=row["valuation_id"],
        asset_id=row["asset_id"],
        valuation_date=date.fromisoformat(row["valuation_date"]),
        market_value=_decimal(row["market_value"]),
        source=row["source"],
        data_confidence=row["data_confidence"],
        is_correction=bool(row["is_correction"]),
    )


class AssetValuationRepository(BaseRepository):
    """Record and read dated market-value history, for any asset category."""

    def record_valuation(
        self,
        asset_id: int,
        valuation_date: date,
        market_value: Money,
        source: str,
        *,
        is_correction: bool = False,
        uow: Optional[UnitOfWork] = None,
    ) -> AssetValuation:
        """Insert one valuation row and return it as stored."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO asset_valuation "
                    "(asset_id, valuation_date, market_value, source, is_correction) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        asset_id, valuation_date.isoformat(),
                        str(_decimal(market_value)), source, int(is_correction),
                    ),
                )
                row = conn.execute(_SELECT, (cursor.lastrowid,)).fetchone()
        return _valuation_from_row(row)

    def get_latest_valuation(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[AssetValuation]:
        """The most recent valuation by date (ties broken by insertion order,
        so a same-day correction entered after the original wins)."""
        row = self._fetch_one(
            "SELECT valuation_id, asset_id, valuation_date, market_value, source, "
            "data_confidence, is_correction FROM asset_valuation "
            "WHERE asset_id = ? ORDER BY valuation_date DESC, valuation_id DESC LIMIT 1",
            (asset_id,),
            uow,
        )
        return None if row is None else _valuation_from_row(row)

    def list_valuations(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[AssetValuation]:
        """Return `asset_id`'s valuation history, oldest date first."""
        rows = self._fetch_all(
            "SELECT valuation_id, asset_id, valuation_date, market_value, source, "
            "data_confidence, is_correction FROM asset_valuation "
            "WHERE asset_id = ? ORDER BY valuation_date, valuation_id",
            (asset_id,),
            uow,
        )
        return [_valuation_from_row(row) for row in rows]
