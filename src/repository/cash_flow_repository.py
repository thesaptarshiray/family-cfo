"""
Persistence for monthly cash-flow items (`cash_flow_item`, migration 005)
and insurance premium payments (`insurance_premium_payment`).

Plain storage only: the Financial Engine turns quarterly/annual amounts into
monthly equivalents and works out which premiums fall due in a month.
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

_ITEM_COLUMNS = (
    "item_id, member_id, flow_type, name, amount, frequency, category, payment_mode, account_id, spent_on"
)
_PAYMENT_COLUMNS = "payment_id, policy_id, due_date, payment_date, amount, status"


@dataclass(frozen=True)
class CashFlowItem:
    item_id: int
    member_id: int
    flow_type: str
    name: str
    amount: Decimal
    frequency: str
    category: Optional[str] = None
    payment_mode: Optional[str] = None
    account_id: Optional[int] = None
    spent_on: Optional[date] = None  # migration 013: a ONCE (ad hoc) row's date


@dataclass(frozen=True)
class PremiumPayment:
    payment_id: int
    policy_id: int
    due_date: date
    payment_date: Optional[date]
    amount: Decimal
    status: str


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _item_from_row(row: sqlite3.Row) -> CashFlowItem:
    return CashFlowItem(
        item_id=row["item_id"], member_id=row["member_id"], flow_type=row["flow_type"],
        name=row["name"], amount=_decimal(row["amount"]), frequency=row["frequency"],
        category=row["category"], payment_mode=row["payment_mode"], account_id=row["account_id"],
        spent_on=None if row["spent_on"] is None else date.fromisoformat(row["spent_on"]),
    )


def _payment_from_row(row: sqlite3.Row) -> PremiumPayment:
    return PremiumPayment(
        payment_id=row["payment_id"], policy_id=row["policy_id"],
        due_date=date.fromisoformat(row["due_date"]),
        payment_date=None if row["payment_date"] is None else date.fromisoformat(row["payment_date"]),
        amount=_decimal(row["amount"]), status=row["status"],
    )


class CashFlowRepository(BaseRepository):
    """Create, read, update and delete cash-flow items; record premium payments."""

    def create_item(
        self, member_id: int, flow_type: str, name: str, amount: Money, frequency: str,
        *, category: Optional[str] = None, payment_mode: Optional[str] = None,
        account_id: Optional[int] = None, spent_on: Optional[date] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> CashFlowItem:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO cash_flow_item (member_id, flow_type, name, amount, frequency, "
                    "category, payment_mode, account_id, spent_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (member_id, flow_type, name, str(_decimal(amount)), frequency,
                     category, payment_mode, account_id,
                     None if spent_on is None else spent_on.isoformat()),
                )
                row = conn.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM cash_flow_item WHERE item_id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
        return _item_from_row(row)

    def update_item(
        self, item_id: int, member_id: int, flow_type: str, name: str, amount: Money,
        frequency: str, *, category: Optional[str] = None, payment_mode: Optional[str] = None,
        account_id: Optional[int] = None, spent_on: Optional[date] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[CashFlowItem]:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE cash_flow_item SET member_id = ?, flow_type = ?, name = ?, "
                    "amount = ?, frequency = ?, category = ?, payment_mode = ?, account_id = ?, "
                    "spent_on = ? WHERE item_id = ?",
                    (member_id, flow_type, name, str(_decimal(amount)), frequency,
                     category, payment_mode, account_id,
                     None if spent_on is None else spent_on.isoformat(), item_id),
                )
                row = conn.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM cash_flow_item WHERE item_id = ?", (item_id,)
                ).fetchone()
        return None if row is None else _item_from_row(row)

    def delete_item(self, item_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute("DELETE FROM cash_flow_item WHERE item_id = ?", (item_id,))
        return cursor.rowcount > 0

    def list_items(self, member_id: int, *, uow: Optional[UnitOfWork] = None) -> List[CashFlowItem]:
        rows = self._fetch_all(
            f"SELECT {_ITEM_COLUMNS} FROM cash_flow_item WHERE member_id = ? ORDER BY item_id",
            (member_id,), uow,
        )
        return [_item_from_row(row) for row in rows]

    # -------------------------------------------------- premium payments

    def get_payment(
        self, policy_id: int, due_date: date, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[PremiumPayment]:
        row = self._fetch_one(
            f"SELECT {_PAYMENT_COLUMNS} FROM insurance_premium_payment "
            "WHERE policy_id = ? AND due_date = ? ORDER BY payment_id DESC LIMIT 1",
            (policy_id, due_date.isoformat()), uow,
        )
        return None if row is None else _payment_from_row(row)

    def set_payment_status(
        self, policy_id: int, due_date: date, amount: Money, status: str,
        payment_date: Optional[date], *, uow: Optional[UnitOfWork] = None,
    ) -> PremiumPayment:
        """Mark one installment PAID or back to DUE: inserts the row the first
        time, updates it afterwards (one row per policy and due date)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                existing = conn.execute(
                    "SELECT payment_id FROM insurance_premium_payment "
                    "WHERE policy_id = ? AND due_date = ?",
                    (policy_id, due_date.isoformat()),
                ).fetchone()
                values = (
                    None if payment_date is None else payment_date.isoformat(),
                    str(_decimal(amount)), status,
                )
                if existing is None:
                    cursor = conn.execute(
                        "INSERT INTO insurance_premium_payment "
                        "(policy_id, due_date, payment_date, amount, status) VALUES (?, ?, ?, ?, ?)",
                        (policy_id, due_date.isoformat()) + values,
                    )
                    payment_id = cursor.lastrowid
                else:
                    payment_id = existing["payment_id"]
                    conn.execute(
                        "UPDATE insurance_premium_payment SET payment_date = ?, amount = ?, "
                        "status = ? WHERE payment_id = ?",
                        values + (payment_id,),
                    )
                row = conn.execute(
                    f"SELECT {_PAYMENT_COLUMNS} FROM insurance_premium_payment WHERE payment_id = ?",
                    (payment_id,),
                ).fetchone()
        return _payment_from_row(row)
