"""
Persistence for stock buy/sell transactions (`stock_transaction`).

An append-only record of BUY and SELL entries for one stock holding — see
the DDL's comment on this table (Section 8C, migration 002) for why it
exists separately from `investment_lot`/`"transaction"`.

Like every repository, this one does no calculation: quantity, average buy
price, cost basis and realised gain are derived from these rows by the
Financial Engine (`financial_engine.stock_position`), not here.
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

_COLUMNS = (
    "stock_txn_id, asset_id, txn_date, txn_type, quantity, price, charges, "
    "status, description, is_opening_balance, import_batch_id"
)


@dataclass(frozen=True)
class StockTransaction:
    """One row from ``stock_transaction``."""

    stock_txn_id: int
    asset_id: int
    txn_date: date
    txn_type: str
    quantity: Decimal
    price: Decimal
    charges: Decimal
    status: str
    description: Optional[str]
    # Migration 009 — see MfTransaction.
    is_opening_balance: bool = False
    import_batch_id: Optional[int] = None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _from_row(row: sqlite3.Row) -> StockTransaction:
    return StockTransaction(
        stock_txn_id=row["stock_txn_id"],
        asset_id=row["asset_id"],
        txn_date=date.fromisoformat(row["txn_date"]),
        txn_type=row["txn_type"],
        quantity=_decimal(row["quantity"]),
        price=_decimal(row["price"]),
        charges=_decimal(row["charges"]),
        status=row["status"],
        description=row["description"],
        is_opening_balance=bool(row["is_opening_balance"]),
        import_batch_id=row["import_batch_id"],
    )


class StockTransactionRepository(BaseRepository):
    """Create and read stock buy/sell transactions."""

    def create_transaction(
        self,
        asset_id: int,
        txn_date: date,
        txn_type: str,
        quantity: Money,
        price: Money,
        charges: Money = 0,
        *,
        description: Optional[str] = None,
        is_opening_balance: bool = False,
        import_batch_id: Optional[int] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> StockTransaction:
        """Insert one row and return it as stored."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO stock_transaction "
                    "(asset_id, txn_date, txn_type, quantity, price, charges, description, "
                    "is_opening_balance, import_batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        asset_id, txn_date.isoformat(), txn_type,
                        str(_decimal(quantity)), str(_decimal(price)), str(_decimal(charges)),
                        description, int(is_opening_balance), import_batch_id,
                    ),
                )
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM stock_transaction WHERE stock_txn_id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
        return _from_row(row)

    def list_transactions(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[StockTransaction]:
        """Return `asset_id`'s transactions, oldest `txn_date` first (ties in
        insertion order)."""
        rows = self._fetch_all(
            f"SELECT {_COLUMNS} FROM stock_transaction "
            "WHERE asset_id = ? ORDER BY txn_date, stock_txn_id",
            (asset_id,),
            uow,
        )
        return [_from_row(row) for row in rows]

    def get_transaction(
        self, stock_txn_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[StockTransaction]:
        row = self._fetch_one(
            f"SELECT {_COLUMNS} FROM stock_transaction WHERE stock_txn_id = ?", (stock_txn_id,), uow
        )
        return None if row is None else _from_row(row)

    def update_transaction(
        self, stock_txn_id: int, *, txn_date: date, txn_type: str, quantity: Money, price: Money,
        charges: Money, description: Optional[str], uow: Optional[UnitOfWork] = None,
    ) -> StockTransaction:
        """Correct a row in place. A corrected row is a real, dated trade,
        so it is no longer an imported opening balance."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE stock_transaction SET txn_date = ?, txn_type = ?, quantity = ?, price = ?, "
                    "charges = ?, description = ?, is_opening_balance = 0 WHERE stock_txn_id = ?",
                    (
                        txn_date.isoformat(), txn_type, str(_decimal(quantity)), str(_decimal(price)),
                        str(_decimal(charges)), description, stock_txn_id,
                    ),
                )
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM stock_transaction WHERE stock_txn_id = ?", (stock_txn_id,)
                ).fetchone()
        return _from_row(row)

    def delete_transaction(self, stock_txn_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "DELETE FROM stock_transaction WHERE stock_txn_id = ?", (stock_txn_id,)
                )
        return cursor.rowcount > 0

    def delete_for_asset(self, asset_id: int, *, uow: Optional[UnitOfWork] = None) -> int:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute("DELETE FROM stock_transaction WHERE asset_id = ?", (asset_id,))
        return cursor.rowcount

    def reassign_asset(self, from_asset_id: int, to_asset_id: int, *, uow: Optional[UnitOfWork] = None) -> int:
        """Move every transaction of one holding onto another — merging two
        holdings of the same stock. Returns how many rows moved."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE stock_transaction SET asset_id = ? WHERE asset_id = ?",
                    (to_asset_id, from_asset_id),
                )
        return cursor.rowcount

