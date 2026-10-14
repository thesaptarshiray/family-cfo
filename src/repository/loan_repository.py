"""
Persistence for loans: `liability` + `liability_ownership` + `loan_detail`
(migration 008), and each loan's events — `loan_disbursement` (migration
008), `loan_prepayment` and `loan_rate_change`.

Plain storage only. The Financial Engine works out the amortization
schedule from these rows; the columns it keeps up to date from that
schedule (`liability.outstanding_amount`, `loan_detail.interest_rate`) are
written through `DerivedCacheRepository`, never here.
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

_LOAN_COLUMNS = (
    "l.liability_id, l.member_id, l.type, l.lender, l.sanctioned_amount, l.disbursed_amount, "
    "l.outstanding_amount, l.start_date, l.status, d.interest_rate, d.interest_type, "
    "d.original_tenure_months, d.emi_amount, d.emi_start_date, d.initial_interest_rate, "
    "d.down_payment"
)
_LOAN_FROM = "liability l JOIN loan_detail d ON d.liability_id = l.liability_id"


@dataclass(frozen=True)
class Loan:
    liability_id: int
    member_id: int
    loan_type: str
    lender: str
    sanctioned_amount: Decimal
    disbursed_amount: Decimal
    outstanding_amount: Decimal
    start_date: date
    status: str
    interest_rate: Decimal  # cache: the rate in force today
    interest_type: str
    tenure_months: int
    emi_amount: Optional[Decimal]  # as entered; None = the Engine works it out
    emi_start_date: date
    initial_interest_rate: Decimal
    down_payment: Optional[Decimal]


@dataclass(frozen=True)
class LoanDisbursement:
    disbursement_id: int
    liability_id: int
    disbursement_date: date
    amount: Decimal
    note: Optional[str]


@dataclass(frozen=True)
class LoanPrepayment:
    prepayment_id: int
    liability_id: int
    prepayment_date: date
    amount: Decimal


@dataclass(frozen=True)
class LoanRateChange:
    rate_change_id: int
    liability_id: int
    effective_date: date
    old_rate: Decimal
    new_rate: Decimal
    reason: Optional[str]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: object) -> Optional[Decimal]:
    return None if value is None else _decimal(value)


def _loan_from_row(row: sqlite3.Row) -> Loan:
    return Loan(
        liability_id=row["liability_id"], member_id=row["member_id"], loan_type=row["type"],
        lender=row["lender"], sanctioned_amount=_decimal(row["sanctioned_amount"]),
        disbursed_amount=_decimal(row["disbursed_amount"]),
        outstanding_amount=_decimal(row["outstanding_amount"]),
        start_date=date.fromisoformat(row["start_date"]), status=row["status"],
        interest_rate=_decimal(row["interest_rate"]), interest_type=row["interest_type"],
        tenure_months=row["original_tenure_months"],
        emi_amount=_optional_decimal(row["emi_amount"]),
        emi_start_date=date.fromisoformat(row["emi_start_date"]),
        initial_interest_rate=_decimal(row["initial_interest_rate"]),
        down_payment=_optional_decimal(row["down_payment"]),
    )


def _money(value: Optional[Money]) -> Optional[str]:
    return None if value is None else str(_decimal(value))


class LoanRepository(BaseRepository):
    """Create, read, update and delete loans and their events."""

    # -------------------------------------------------- loans

    def create_loan(
        self, member_id: int, loan_type: str, lender: str, sanctioned_amount: Money,
        start_date: date, *, interest_type: str, tenure_months: int,
        emi_amount: Optional[Money], emi_start_date: date, initial_interest_rate: Money,
        down_payment: Optional[Money] = None, uow: Optional[UnitOfWork] = None,
    ) -> Loan:
        """A new loan, wholly owned by `member_id`. `disbursed_amount`
        starts at 0; the Engine sets it from the disbursements."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO liability (member_id, type, lender, sanctioned_amount, "
                    "disbursed_amount, start_date) VALUES (?, ?, ?, ?, 0, ?)",
                    (member_id, loan_type, lender, str(_decimal(sanctioned_amount)),
                     start_date.isoformat()),
                )
                liability_id = cursor.lastrowid
                conn.execute(
                    "INSERT INTO liability_ownership (liability_id, member_id, ownership_pct) "
                    "VALUES (?, ?, 100)",
                    (liability_id, member_id),
                )
                conn.execute(
                    "INSERT INTO loan_detail (liability_id, interest_type, original_tenure_months, "
                    "emi_amount, emi_start_date, initial_interest_rate, down_payment) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (liability_id, interest_type, tenure_months, _money(emi_amount),
                     emi_start_date.isoformat(), str(_decimal(initial_interest_rate)),
                     _money(down_payment)),
                )
                row = conn.execute(
                    f"SELECT {_LOAN_COLUMNS} FROM {_LOAN_FROM} WHERE l.liability_id = ?",
                    (liability_id,),
                ).fetchone()
        return _loan_from_row(row)

    def update_loan(
        self, liability_id: int, member_id: int, loan_type: str, lender: str,
        sanctioned_amount: Money, start_date: date, status: str, *, interest_type: str,
        tenure_months: int, emi_amount: Optional[Money], emi_start_date: date,
        initial_interest_rate: Money, down_payment: Optional[Money] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[Loan]:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE liability SET member_id = ?, type = ?, lender = ?, "
                    "sanctioned_amount = ?, start_date = ?, status = ? WHERE liability_id = ?",
                    (member_id, loan_type, lender, str(_decimal(sanctioned_amount)),
                     start_date.isoformat(), status, liability_id),
                )
                conn.execute(
                    "UPDATE liability_ownership SET member_id = ? WHERE liability_id = ?",
                    (member_id, liability_id),
                )
                conn.execute(
                    "UPDATE loan_detail SET interest_type = ?, original_tenure_months = ?, "
                    "emi_amount = ?, emi_start_date = ?, initial_interest_rate = ?, "
                    "down_payment = ? WHERE liability_id = ?",
                    (interest_type, tenure_months, _money(emi_amount), emi_start_date.isoformat(),
                     str(_decimal(initial_interest_rate)), _money(down_payment), liability_id),
                )
                row = conn.execute(
                    f"SELECT {_LOAN_COLUMNS} FROM {_LOAN_FROM} WHERE l.liability_id = ?",
                    (liability_id,),
                ).fetchone()
        return None if row is None else _loan_from_row(row)

    def set_disbursed_amount(
        self, liability_id: int, amount: Money, *, uow: Optional[UnitOfWork] = None
    ) -> None:
        """`liability.disbursed_amount`: the total of the loan's
        disbursements, as the Engine added them up."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE liability SET disbursed_amount = ? WHERE liability_id = ?",
                    (str(_decimal(amount)), liability_id),
                )

    def delete_loan(self, liability_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        """Remove a loan and all its events (prepayments and rate changes
        first: their foreign keys do not cascade)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                for table in ("loan_prepayment", "loan_rate_change"):
                    conn.execute(f"DELETE FROM {table} WHERE liability_id = ?", (liability_id,))
                cursor = conn.execute(
                    "DELETE FROM liability WHERE liability_id = ?", (liability_id,)
                )
        return cursor.rowcount > 0

    def get_loan(self, liability_id: int, *, uow: Optional[UnitOfWork] = None) -> Optional[Loan]:
        row = self._fetch_one(
            f"SELECT {_LOAN_COLUMNS} FROM {_LOAN_FROM} WHERE l.liability_id = ?",
            (liability_id,), uow,
        )
        return None if row is None else _loan_from_row(row)

    def list_loans(self, member_id: int, *, uow: Optional[UnitOfWork] = None) -> List[Loan]:
        rows = self._fetch_all(
            f"SELECT {_LOAN_COLUMNS} FROM {_LOAN_FROM} WHERE l.member_id = ? "
            "ORDER BY l.start_date, l.liability_id",
            (member_id,), uow,
        )
        return [_loan_from_row(row) for row in rows]

    # -------------------------------------------------- disbursements

    def add_disbursement(
        self, liability_id: int, disbursement_date: date, amount: Money,
        note: Optional[str] = None, *, uow: Optional[UnitOfWork] = None,
    ) -> LoanDisbursement:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO loan_disbursement (liability_id, disbursement_date, amount, note) "
                    "VALUES (?, ?, ?, ?)",
                    (liability_id, disbursement_date.isoformat(), str(_decimal(amount)), note),
                )
        return LoanDisbursement(cursor.lastrowid, liability_id, disbursement_date, _decimal(amount), note)

    def list_disbursements(
        self, liability_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[LoanDisbursement]:
        rows = self._fetch_all(
            "SELECT disbursement_id, liability_id, disbursement_date, amount, note "
            "FROM loan_disbursement WHERE liability_id = ? ORDER BY disbursement_date, disbursement_id",
            (liability_id,), uow,
        )
        return [
            LoanDisbursement(
                r["disbursement_id"], r["liability_id"], date.fromisoformat(r["disbursement_date"]),
                _decimal(r["amount"]), r["note"],
            )
            for r in rows
        ]

    def get_disbursement(
        self, disbursement_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[LoanDisbursement]:
        row = self._fetch_one(
            "SELECT disbursement_id, liability_id, disbursement_date, amount, note "
            "FROM loan_disbursement WHERE disbursement_id = ?",
            (disbursement_id,), uow,
        )
        if row is None:
            return None
        return LoanDisbursement(
            row["disbursement_id"], row["liability_id"], date.fromisoformat(row["disbursement_date"]),
            _decimal(row["amount"]), row["note"],
        )

    def delete_disbursement(self, disbursement_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "DELETE FROM loan_disbursement WHERE disbursement_id = ?", (disbursement_id,)
                )
        return cursor.rowcount > 0

    # -------------------------------------------------- prepayments

    def add_prepayment(
        self, liability_id: int, prepayment_date: date, amount: Money,
        *, uow: Optional[UnitOfWork] = None,
    ) -> LoanPrepayment:
        """Record a prepayment. `principal_reduction` / `revised_outstanding`
        start as the amount and 0; the Engine then sets them from the
        schedule (`set_prepayment_effect`)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO loan_prepayment (liability_id, prepayment_date, amount, "
                    "principal_reduction, revised_outstanding) VALUES (?, ?, ?, ?, 0)",
                    (liability_id, prepayment_date.isoformat(), str(_decimal(amount)),
                     str(_decimal(amount))),
                )
        return LoanPrepayment(cursor.lastrowid, liability_id, prepayment_date, _decimal(amount))

    def set_prepayment_effect(
        self, prepayment_id: int, principal_reduction: Money, revised_outstanding: Money,
        revised_tenure_months: Optional[int], *, uow: Optional[UnitOfWork] = None,
    ) -> None:
        """Store what the Engine's schedule says a prepayment did."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE loan_prepayment SET principal_reduction = ?, revised_outstanding = ?, "
                    "revised_tenure_months = ? WHERE prepayment_id = ?",
                    (str(_decimal(principal_reduction)), str(_decimal(revised_outstanding)),
                     revised_tenure_months, prepayment_id),
                )

    def list_prepayments(
        self, liability_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[LoanPrepayment]:
        rows = self._fetch_all(
            "SELECT prepayment_id, liability_id, prepayment_date, amount FROM loan_prepayment "
            "WHERE liability_id = ? AND reversed_by_txn_id IS NULL "
            "ORDER BY prepayment_date, prepayment_id",
            (liability_id,), uow,
        )
        return [
            LoanPrepayment(
                r["prepayment_id"], r["liability_id"], date.fromisoformat(r["prepayment_date"]),
                _decimal(r["amount"]),
            )
            for r in rows
        ]

    def get_prepayment_loan_id(
        self, prepayment_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[int]:
        row = self._fetch_one(
            "SELECT liability_id FROM loan_prepayment WHERE prepayment_id = ?", (prepayment_id,), uow
        )
        return None if row is None else row["liability_id"]

    def delete_prepayment(self, prepayment_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "DELETE FROM loan_prepayment WHERE prepayment_id = ?", (prepayment_id,)
                )
        return cursor.rowcount > 0

    # -------------------------------------------------- rate changes

    def add_rate_change(
        self, liability_id: int, effective_date: date, old_rate: Money, new_rate: Money,
        reason: Optional[str] = None, *, uow: Optional[UnitOfWork] = None,
    ) -> LoanRateChange:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO loan_rate_change (liability_id, effective_date, old_rate, "
                    "new_rate, reason) VALUES (?, ?, ?, ?, ?)",
                    (liability_id, effective_date.isoformat(), str(_decimal(old_rate)),
                     str(_decimal(new_rate)), reason),
                )
        return LoanRateChange(
            cursor.lastrowid, liability_id, effective_date, _decimal(old_rate),
            _decimal(new_rate), reason,
        )

    def set_rate_change_old_rate(
        self, rate_change_id: int, old_rate: Money, *, uow: Optional[UnitOfWork] = None
    ) -> None:
        """Keep `old_rate` in step when an earlier change is added or removed."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE loan_rate_change SET old_rate = ? WHERE rate_change_id = ?",
                    (str(_decimal(old_rate)), rate_change_id),
                )

    def list_rate_changes(
        self, liability_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[LoanRateChange]:
        rows = self._fetch_all(
            "SELECT rate_change_id, liability_id, effective_date, old_rate, new_rate, reason "
            "FROM loan_rate_change WHERE liability_id = ? ORDER BY effective_date, rate_change_id",
            (liability_id,), uow,
        )
        return [
            LoanRateChange(
                r["rate_change_id"], r["liability_id"], date.fromisoformat(r["effective_date"]),
                _decimal(r["old_rate"]), _decimal(r["new_rate"]), r["reason"],
            )
            for r in rows
        ]

    def get_rate_change_loan_id(
        self, rate_change_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[int]:
        row = self._fetch_one(
            "SELECT liability_id FROM loan_rate_change WHERE rate_change_id = ?",
            (rate_change_id,), uow,
        )
        return None if row is None else row["liability_id"]

    def delete_rate_change(self, rate_change_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "DELETE FROM loan_rate_change WHERE rate_change_id = ?", (rate_change_id,)
                )
        return cursor.rowcount > 0
