"""Persistence for bank accounts and their ownership shares."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple, Union

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

Money =Union[Decimal, int, str]
OwnershipInput = Tuple[int, Money]


@dataclass(frozen=True)
class BankAccount:
    """One row from ``bank_account``."""

    account_id: int
    member_id: int
    bank_name: str
    account_type: str
    account_number_masked: str
    opening_balance: Decimal
    current_balance: Decimal
    is_active: bool
    # migration 006: the member's default account for the cash-flow tab, and
    # the date `opening_balance` (the balance the user entered) was true.
    is_default: bool = False
    balance_as_of: Optional[date] = None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _account_from_row(row: sqlite3.Row) -> BankAccount:
    return BankAccount(
        account_id=row["account_id"],
        member_id=row["member_id"],
        bank_name=row["bank_name"],
        account_type=row["account_type"],
        account_number_masked=row["account_number_masked"],
        opening_balance=_decimal(row["opening_balance"]),
        current_balance=_decimal(row["current_balance"]),
        is_active=bool(row["is_active"]),
        is_default=bool(row["is_default"]),
        balance_as_of=(
            None if row["balance_as_of"] is None else date.fromisoformat(row["balance_as_of"])
        ),
    )


_COLUMNS = (
    "account_id, member_id, bank_name, account_type, account_number_masked, "
    "opening_balance, current_balance, is_active, is_default, balance_as_of"
)


class BankRepository(BaseRepository):
    """Create and read bank accounts with atomic ownership rows.

    Every method takes an optional keyword `uow`. Without one, the call is its
    own transaction, as before. With one, the call joins that Unit of Work: it
    runs on the unit's connection and leaves committing to the caller.
    """

    def create_account(
        self,
        member_id: int,
        bank_name: str,
        account_type: str,
        account_number_masked: str,
        opening_balance: Money = 0,
        ownership: Sequence[OwnershipInput] = (),
        *,
        balance_as_of: Optional[date] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> BankAccount:
        """Create an account and its complete ownership split atomically."""
        ownership_rows = [
            (owner_id, _decimal(ownership_pct))
            for owner_id, ownership_pct in ownership
        ]
        owner_ids = [owner_id for owner_id, _ in ownership_rows]
        if member_id not in owner_ids:
            raise ValueError("primary holder must have an ownership row")
        if len(owner_ids) != len(set(owner_ids)):
            raise ValueError("ownership members must be unique")
        if sum((share for _, share in ownership_rows), Decimal("0")) != Decimal("100"):
            raise ValueError("ownership percentages must sum to 100")

        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO bank_account "
                    "(member_id, bank_name, account_type, account_number_masked, "
                    "opening_balance, balance_as_of) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        member_id,
                        bank_name,
                        account_type,
                        account_number_masked,
                        str(_decimal(opening_balance)),
                        None if balance_as_of is None else balance_as_of.isoformat(),
                    ),
                )
                account_id = cursor.lastrowid
                conn.executemany(
                    "INSERT INTO account_ownership "
                    "(account_id, member_id, ownership_pct) VALUES (?, ?, ?)",
                    [
                        (account_id, owner_id, str(ownership_pct))
                        for owner_id, ownership_pct in ownership_rows
                    ],
                )
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM bank_account WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
        return _account_from_row(row)

    def get_account(
        self, account_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[BankAccount]:
        row = self._fetch_one(
            f"SELECT {_COLUMNS} FROM bank_account WHERE account_id = ?",
            (account_id,),
            uow,
        )
        return None if row is None else _account_from_row(row)

    def list_accounts(
        self, member_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[BankAccount]:
        rows = self._fetch_all(
            f"SELECT {_COLUMNS} FROM bank_account WHERE member_id = ? ORDER BY account_id",
            (member_id,),
            uow,
        )
        return [_account_from_row(row) for row in rows]

    def list_ownership(
        self, account_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[Tuple[int, Decimal]]:
        rows = self._fetch_all(
            "SELECT member_id, ownership_pct FROM account_ownership "
            "WHERE account_id = ? ORDER BY member_id",
            (account_id,),
            uow,
        )
        return [(row["member_id"], _decimal(row["ownership_pct"])) for row in rows]

    def update_details(
        self, account_id: int, *, bank_name: str, account_number_masked: str,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[BankAccount]:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE bank_account SET bank_name = ?, account_number_masked = ? "
                    "WHERE account_id = ?",
                    (bank_name, account_number_masked, account_id),
                )
        return self.get_account(account_id, uow=uow)

    def set_entered_balance(
        self, account_id: int, balance: Money, as_of: date, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[BankAccount]:
        """Replace the balance the user entered and the date it was true."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE bank_account SET opening_balance = ?, balance_as_of = ? "
                    "WHERE account_id = ?",
                    (str(_decimal(balance)), as_of.isoformat(), account_id),
                )
        return self.get_account(account_id, uow=uow)

    def set_default(self, member_id: int, account_id: int, *, uow: Optional[UnitOfWork] = None) -> None:
        """Make `account_id` the member's only default account."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE bank_account SET is_default = CASE WHEN account_id = ? THEN 1 ELSE 0 END "
                    "WHERE member_id = ?",
                    (account_id, member_id),
                )

    def set_active(self, account_id: int, active: bool, *, uow: Optional[UnitOfWork] = None) -> None:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE bank_account SET is_active = ?, is_default = CASE WHEN ? THEN is_default ELSE 0 END "
                    "WHERE account_id = ?",
                    (int(active), int(active), account_id),
                )

