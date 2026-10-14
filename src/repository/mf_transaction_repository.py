"""
Persistence for MF unit-acquisition transactions (`mf_transaction`).

An append-only record of SIP/lumpsum purchases for one MF holding — see the
DDL's own comment on this table (Section 8B) for why it exists separately
from `investment_lot`/`"transaction"`: those require a bank account on every
row, which this product does not want for MF. This table is deliberately
narrower: acquisitions only, no redemption/sell (that stays out of scope
until FIFO lot consumption is built, same as everywhere else in this
project).

Like every repository, this one does no calculation: `mf_holding.units`/
`avg_nav` and `asset.invested_amount` are summed from these rows by the
Financial Engine, not here — this module only stores what it is given and
reads it back.
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
    "SELECT mf_txn_id, asset_id, txn_date, txn_type, units, nav, amount, "
    "status, description, is_opening_balance, import_batch_id "
    "FROM mf_transaction WHERE mf_txn_id = ?"
)


@dataclass(frozen=True)
class MfTransaction:
    """One row from ``mf_transaction``."""

    mf_txn_id: int
    asset_id: int
    txn_date: date
    txn_type: str
    units: Decimal
    nav: Decimal
    amount: Decimal
    status: str
    description: Optional[str]
    # Migration 009: the row that stands for everything bought before an
    # imported statement (dates unknown), and the import it came from.
    is_opening_balance: bool = False
    import_batch_id: Optional[int] = None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _mf_transaction_from_row(row: sqlite3.Row) -> MfTransaction:
    return MfTransaction(
        mf_txn_id=row["mf_txn_id"],
        asset_id=row["asset_id"],
        txn_date=date.fromisoformat(row["txn_date"]),
        txn_type=row["txn_type"],
        units=_decimal(row["units"]),
        nav=_decimal(row["nav"]),
        amount=_decimal(row["amount"]),
        status=row["status"],
        description=row["description"],
        is_opening_balance=bool(row["is_opening_balance"]),
        import_batch_id=row["import_batch_id"],
    )


class MfTransactionRepository(BaseRepository):
    """Create and read MF unit-acquisition transactions."""

    def create_transaction(
        self,
        asset_id: int,
        txn_date: date,
        txn_type: str,
        units: Money,
        nav: Money,
        amount: Money,
        *,
        description: Optional[str] = None,
        is_opening_balance: bool = False,
        import_batch_id: Optional[int] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> MfTransaction:
        """Insert one acquisition row and return it as stored."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO mf_transaction "
                    "(asset_id, txn_date, txn_type, units, nav, amount, description, "
                    "is_opening_balance, import_batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        asset_id, txn_date.isoformat(), txn_type,
                        str(_decimal(units)), str(_decimal(nav)), str(_decimal(amount)),
                        description, int(is_opening_balance), import_batch_id,
                    ),
                )
                row = conn.execute(_SELECT, (cursor.lastrowid,)).fetchone()
        return _mf_transaction_from_row(row)

    def update_transaction(
        self,
        mf_txn_id: int,
        *,
        txn_date: date,
        txn_type: str,
        nav: Money,
        amount: Money,
        description: Optional[str],
        is_opening_balance: bool,
        units: Optional[Money] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> MfTransaction:
        """Rewrite a row's date, type, price and amount (and units, when
        given) — used to turn an imported opening balance into dated purchases."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE mf_transaction SET txn_date = ?, txn_type = ?, nav = ?, amount = ?, "
                    "description = ?, is_opening_balance = ?, units = COALESCE(?, units) WHERE mf_txn_id = ?",
                    (
                        txn_date.isoformat(), txn_type, str(_decimal(nav)), str(_decimal(amount)),
                        description, int(is_opening_balance),
                        None if units is None else str(_decimal(units)), mf_txn_id,
                    ),
                )
                row = conn.execute(_SELECT, (mf_txn_id,)).fetchone()
        return _mf_transaction_from_row(row)

    def delete_transaction(self, mf_txn_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute("DELETE FROM mf_transaction WHERE mf_txn_id = ?", (mf_txn_id,))
        return cursor.rowcount > 0

    def delete_for_asset(self, asset_id: int, *, uow: Optional[UnitOfWork] = None) -> int:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute("DELETE FROM mf_transaction WHERE asset_id = ?", (asset_id,))
        return cursor.rowcount

    def get_transaction(
        self, mf_txn_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[MfTransaction]:
        row = self._fetch_one(_SELECT, (mf_txn_id,), uow)
        return None if row is None else _mf_transaction_from_row(row)

    def list_transactions(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[MfTransaction]:
        """Return `asset_id`'s transactions, oldest `txn_date` first."""
        rows = self._fetch_all(
            "SELECT mf_txn_id, asset_id, txn_date, txn_type, units, nav, amount, "
            "status, description, is_opening_balance, import_batch_id FROM mf_transaction "
            "WHERE asset_id = ? ORDER BY txn_date, mf_txn_id",
            (asset_id,),
            uow,
        )
        return [_mf_transaction_from_row(row) for row in rows]
