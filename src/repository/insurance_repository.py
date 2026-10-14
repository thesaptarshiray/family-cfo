"""
Persistence for insurance policies.

`insurance_policy` is deliberately its own table, outside the `asset`
hierarchy — insurance is a different domain (a premium obligation and a
payout condition, not an investment with a market or formula value), per the
product's own principle that different financial products should not be
forced into one shape. `InsurancePolicyRepository` mirrors the shape of
`AssetRepository`'s simpler methods (no ownership split here: the schema
gives each policy a single `member_id`, the insured person), but is
otherwise unrelated to it.

Like every repository, this one does no calculation: it stores exactly the
fields the schema declares, and validates nothing beyond what the schema's
own constraints already enforce.
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


@dataclass(frozen=True)
class InsurancePolicy:
    """One row from ``insurance_policy``."""

    policy_id: int
    member_id: int
    policy_type: str
    insurer: str
    policy_number: str
    sum_assured: Optional[Decimal]
    premium_amount: Decimal
    premium_frequency: str
    start_date: date
    maturity_date: Optional[date]
    current_surrender_value: Optional[Decimal]
    status: str
    accrued_bonus: Optional[Decimal] = None
    plan_name: Optional[str] = None  # migration 011, e.g. "Jeevan Anand 915"
    nominee: Optional[str] = None  # migration 011
    premium_paying_term_years: Optional[int] = None  # migration 012: limited pay


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: object) -> Optional[Decimal]:
    return None if value is None else _decimal(value)


def _optional_date(value: object) -> Optional[date]:
    return None if value is None else date.fromisoformat(value)


_SELECT = (
    "SELECT policy_id, member_id, policy_type, insurer, policy_number, "
    "sum_assured, premium_amount, premium_frequency, start_date, "
    "maturity_date, current_surrender_value, status, accrued_bonus, plan_name, nominee, premium_paying_term_years "
    "FROM insurance_policy WHERE policy_id = ?"
)


def _policy_from_row(row: sqlite3.Row) -> InsurancePolicy:
    return InsurancePolicy(
        policy_id=row["policy_id"],
        member_id=row["member_id"],
        policy_type=row["policy_type"],
        insurer=row["insurer"],
        policy_number=row["policy_number"],
        sum_assured=_optional_decimal(row["sum_assured"]),
        premium_amount=_decimal(row["premium_amount"]),
        premium_frequency=row["premium_frequency"],
        start_date=date.fromisoformat(row["start_date"]),
        maturity_date=_optional_date(row["maturity_date"]),
        current_surrender_value=_optional_decimal(row["current_surrender_value"]),
        status=row["status"],
        accrued_bonus=_optional_decimal(row["accrued_bonus"]),
        plan_name=row["plan_name"],
        nominee=row["nominee"],
        premium_paying_term_years=row["premium_paying_term_years"],
    )


class InsurancePolicyRepository(BaseRepository):
    """Create and read insurance policies."""

    def create_policy(
        self,
        member_id: int,
        policy_type: str,
        insurer: str,
        policy_number: str,
        premium_amount: Money,
        premium_frequency: str,
        start_date: date,
        *,
        sum_assured: Optional[Money] = None,
        maturity_date: Optional[date] = None,
        current_surrender_value: Optional[Money] = None,
        accrued_bonus: Optional[Money] = None,
        plan_name: Optional[str] = None,
        nominee: Optional[str] = None,
        premium_paying_term_years: Optional[int] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> InsurancePolicy:
        """Insert a policy and return it."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO insurance_policy "
                    "(member_id, policy_type, insurer, policy_number, sum_assured, "
                    "premium_amount, premium_frequency, start_date, maturity_date, "
                    "current_surrender_value, accrued_bonus, plan_name, nominee, premium_paying_term_years) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        member_id, policy_type, insurer, policy_number,
                        None if sum_assured is None else str(_decimal(sum_assured)),
                        str(_decimal(premium_amount)), premium_frequency,
                        start_date.isoformat(),
                        None if maturity_date is None else maturity_date.isoformat(),
                        None if current_surrender_value is None
                        else str(_decimal(current_surrender_value)),
                        None if accrued_bonus is None else str(_decimal(accrued_bonus)),
                        plan_name, nominee, premium_paying_term_years,
                    ),
                )
                row = conn.execute(_SELECT, (cursor.lastrowid,)).fetchone()
        return _policy_from_row(row)

    def update_policy_values(
        self,
        policy_id: int,
        *,
        current_surrender_value: Optional[Money],
        accrued_bonus: Optional[Money],
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[InsurancePolicy]:
        """Replace the two statement-driven values (either may be cleared
        with `None`). Returns the updated policy, or `None` if it does not
        exist."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE insurance_policy SET current_surrender_value = ?, "
                    "accrued_bonus = ? WHERE policy_id = ?",
                    (
                        None if current_surrender_value is None
                        else str(_decimal(current_surrender_value)),
                        None if accrued_bonus is None else str(_decimal(accrued_bonus)),
                        policy_id,
                    ),
                )
                row = conn.execute(_SELECT, (policy_id,)).fetchone()
        return None if row is None else _policy_from_row(row)

    def update_policy(
        self,
        policy_id: int,
        *,
        member_id: int,
        policy_type: str,
        insurer: str,
        policy_number: str,
        premium_amount: Money,
        premium_frequency: str,
        start_date: date,
        sum_assured: Optional[Money],
        maturity_date: Optional[date],
        status: str,
        plan_name: Optional[str] = None,
        nominee: Optional[str] = None,
        premium_paying_term_years: Optional[int] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[InsurancePolicy]:
        """Correct a policy's terms (surrender value and bonus have their own
        update). Returns the updated policy, or `None` if it does not exist."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE insurance_policy SET member_id = ?, policy_type = ?, insurer = ?, "
                    "policy_number = ?, sum_assured = ?, premium_amount = ?, "
                    "premium_frequency = ?, start_date = ?, maturity_date = ?, status = ?, "
                    "plan_name = ?, nominee = ?, premium_paying_term_years = ? WHERE policy_id = ?",
                    (
                        member_id, policy_type, insurer, policy_number,
                        None if sum_assured is None else str(_decimal(sum_assured)),
                        str(_decimal(premium_amount)), premium_frequency,
                        start_date.isoformat(),
                        None if maturity_date is None else maturity_date.isoformat(),
                        status, plan_name, nominee, premium_paying_term_years, policy_id,
                    ),
                )
                row = conn.execute(_SELECT, (policy_id,)).fetchone()
        return None if row is None else _policy_from_row(row)

    def get_policy(
        self, policy_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[InsurancePolicy]:
        row = self._fetch_one(_SELECT, (policy_id,), uow)
        return None if row is None else _policy_from_row(row)

    def list_policies(
        self, member_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[InsurancePolicy]:
        rows = self._fetch_all(
            "SELECT policy_id, member_id, policy_type, insurer, policy_number, "
            "sum_assured, premium_amount, premium_frequency, start_date, "
            "maturity_date, current_surrender_value, status, accrued_bonus, plan_name, nominee, premium_paying_term_years "
            "FROM insurance_policy WHERE member_id = ? ORDER BY policy_id",
            (member_id,),
            uow,
        )
        return [_policy_from_row(row) for row in rows]
