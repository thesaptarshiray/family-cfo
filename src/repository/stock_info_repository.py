"""
Persistence for stock reference data (`stock_info`, migration 007): each
symbol's company name, sector and SEBI market-cap category.

Rows come from NSE's index lists (source 'NSE', refreshed automatically) or
from the user (source 'MANUAL'). The refresh never overwrites a MANUAL row.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, Optional

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

_COLUMNS = "symbol, company_name, sector, cap_category, source, updated_on"


@dataclass(frozen=True)
class StockInfo:
    symbol: str
    company_name: Optional[str]
    sector: Optional[str]
    cap_category: Optional[str]
    source: str
    updated_on: date


def _from_row(row: sqlite3.Row) -> StockInfo:
    return StockInfo(
        symbol=row["symbol"], company_name=row["company_name"], sector=row["sector"],
        cap_category=row["cap_category"], source=row["source"],
        updated_on=date.fromisoformat(row["updated_on"]),
    )


class StockInfoRepository(BaseRepository):
    def all_info(self, *, uow: Optional[UnitOfWork] = None) -> Dict[str, StockInfo]:
        rows = self._fetch_all(f"SELECT {_COLUMNS} FROM stock_info", (), uow)
        return {row["symbol"]: _from_row(row) for row in rows}

    def last_nse_refresh(self, *, uow: Optional[UnitOfWork] = None) -> Optional[date]:
        row = self._fetch_one(
            "SELECT MAX(updated_on) AS latest FROM stock_info WHERE source = 'NSE'", (), uow
        )
        return None if row is None or row["latest"] is None else date.fromisoformat(row["latest"])

    def upsert_nse(
        self, rows: Iterable[tuple], today: date, *, uow: Optional[UnitOfWork] = None
    ) -> int:
        """Insert or refresh NSE-sourced rows `(symbol, company, sector, cap)`;
        MANUAL rows are left untouched. Returns rows written."""
        count = 0
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                for symbol, company, sector, cap in rows:
                    cursor = conn.execute(
                        "INSERT INTO stock_info (symbol, company_name, sector, cap_category, "
                        "source, updated_on) VALUES (?, ?, ?, ?, 'NSE', ?) "
                        "ON CONFLICT(symbol) DO UPDATE SET company_name = excluded.company_name, "
                        "sector = excluded.sector, cap_category = excluded.cap_category, "
                        "updated_on = excluded.updated_on WHERE stock_info.source = 'NSE'",
                        (symbol, company, sector, cap, today.isoformat()),
                    )
                    count += cursor.rowcount
        return count

    def set_manual(
        self, symbol: str, sector: Optional[str], cap_category: Optional[str], today: date,
        *, uow: Optional[UnitOfWork] = None,
    ) -> StockInfo:
        """The user's own sector / cap for a symbol; replaces any NSE row."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "INSERT INTO stock_info (symbol, company_name, sector, cap_category, source, "
                    "updated_on) VALUES (?, NULL, ?, ?, 'MANUAL', ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET sector = excluded.sector, "
                    "cap_category = excluded.cap_category, source = 'MANUAL', "
                    "updated_on = excluded.updated_on",
                    (symbol, sector, cap_category, today.isoformat()),
                )
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM stock_info WHERE symbol = ?", (symbol,)
                ).fetchone()
        return _from_row(row)
