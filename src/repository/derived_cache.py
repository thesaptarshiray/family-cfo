"""
Persistence for Engine-computed derived cache values.

Some columns of the V6 schema are caches of values the Financial Engine
derives from source rows: a bank account's `current_balance`, for example, is
its opening balance plus its ledger. The DDL marks them `ENGINE-DERIVED
CACHE`. `DerivedCacheRepository` is the one place that writes them, and it
only stores:

  - the Engine calculates the complete value from source rows and passes it
    in; this repository puts it in the column;
  - it never calculates, increments, decrements or derives a value, and never
    checks one against the ledger, so a cache can only ever hold a number the
    Engine decided on;
  - it applies no financial rule (whether a balance may be negative, for
    example, is the Engine's call, made before it calls in here).

Writes always run inside a caller-supplied `UnitOfWork`, on its connection,
and this repository never commits or rolls back. A cache update is only
meaningful together with the source change it was computed from, so the
Engine writes both in the same unit and they commit or roll back together.

`bank_account.current_balance`, `asset.current_value`, `mf_holding.units`/
`avg_nav`, and `asset.invested_amount` are supported so far. The other
derived columns are added, one setter each, together with the Engine
feature that computes them.

`asset.invested_amount` is not marked `ENGINE-DERIVED CACHE` in the DDL —
for a single-lumpsum asset like FD it is a plain fact, set once at creation
by the repository. For a transaction-based category like MF, though, it is
a running total the Engine recomputes after every transaction, so its
setter lives here too; which category needs that treatment is an Engine
decision, not a schema-wide rule.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

from .base import BaseRepository
from .errors import RepositoryError, translate_db_errors
from .unit_of_work import UnitOfWork

Money = Union[Decimal, int, str]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


class DerivedCacheRepository(BaseRepository):
    """Persist complete cache values that the Financial Engine has computed."""

    def set_bank_account_current_balance(
        self, account_id: int, value: Money, *, uow: UnitOfWork
    ) -> None:
        """Replace `bank_account.current_balance` with `value`.

        `value` is stored as given: a full replacement, never an adjustment.
        Raises `RepositoryError` if no account has that id, rather than
        silently leaving a cache unset.
        """
        self._require_unit_of_work(uow, "set_bank_account_current_balance")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE bank_account SET current_balance = ? WHERE account_id = ?",
                    (str(_decimal(value)), account_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryError(f"bank_account {account_id} does not exist")

    def set_asset_current_value(
        self, asset_id: int, value: Money, *, uow: UnitOfWork
    ) -> None:
        """Replace `asset.current_value` with `value`.

        One setter for every asset category (FD, MF, stock, ...): the column
        is generic, and so is this method. `value` is stored as given: a full
        replacement, never an adjustment. Raises `RepositoryError` if no
        asset has that id, rather than silently leaving a cache unset.
        """
        self._require_unit_of_work(uow, "set_asset_current_value")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE asset SET current_value = ? WHERE asset_id = ?",
                    (str(_decimal(value)), asset_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryError(f"asset {asset_id} does not exist")

    def set_asset_invested_amount(
        self, asset_id: int, value: Money, *, uow: UnitOfWork
    ) -> None:
        """Replace `asset.invested_amount` with `value`.

        For transaction-based categories (MF today) — see the module
        docstring for why this differs from FD/Insurance, which set
        `invested_amount` once via the plain repository create call and
        never touch it again.
        """
        self._require_unit_of_work(uow, "set_asset_invested_amount")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE asset SET invested_amount = ? WHERE asset_id = ?",
                    (str(_decimal(value)), asset_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryError(f"asset {asset_id} does not exist")

    def set_mf_units_and_avg_nav(
        self, asset_id: int, units: Money, avg_nav: Money, *, uow: UnitOfWork
    ) -> None:
        """Replace `mf_holding.units` and `mf_holding.avg_nav` together.

        Both are computed by the Engine from `mf_transaction` rows (see
        `financial_engine.engine`). The DDL's long-term intent is for these
        to come from `investment_lot`; MF does not use that table (see the
        DDL's comment on `mf_transaction`), so this is the equivalent path
        for now — one setter, since the two values are always computed and
        replaced together.
        """
        self._require_unit_of_work(uow, "set_mf_units_and_avg_nav")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE mf_holding SET units = ?, avg_nav = ? WHERE asset_id = ?",
                    (str(_decimal(units)), str(_decimal(avg_nav)), asset_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryError(f"mf_holding for asset {asset_id} does not exist")

    def set_stock_quantity_and_avg_price(
        self, asset_id: int, quantity: Money, avg_buy_price: Optional[Money], *, uow: UnitOfWork
    ) -> None:
        """Replace `stock_holding.quantity` and `avg_buy_price` together —
        both computed by the Engine from `stock_transaction` rows, the same
        way `set_mf_units_and_avg_nav` does for MF. `avg_buy_price` is
        `None` once every share has been sold."""
        self._require_unit_of_work(uow, "set_stock_quantity_and_avg_price")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE stock_holding SET quantity = ?, avg_buy_price = ? WHERE asset_id = ?",
                    (
                        str(_decimal(quantity)),
                        None if avg_buy_price is None else str(_decimal(avg_buy_price)),
                        asset_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise RepositoryError(f"stock_holding for asset {asset_id} does not exist")

    def set_liability_outstanding(
        self, liability_id: int, value: Money, *, uow: UnitOfWork
    ) -> None:
        """Replace `liability.outstanding_amount` with `value`: the principal
        owed today by the Engine's amortization schedule."""
        self._require_unit_of_work(uow, "set_liability_outstanding")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE liability SET outstanding_amount = ? WHERE liability_id = ?",
                    (str(_decimal(value)), liability_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryError(f"liability {liability_id} does not exist")

    def set_loan_interest_rate(
        self, liability_id: int, rate: Money, *, uow: UnitOfWork
    ) -> None:
        """Replace `loan_detail.interest_rate` with `rate`: the rate in force
        today, as the Engine found it from the loan's rate history."""
        self._require_unit_of_work(uow, "set_loan_interest_rate")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "UPDATE loan_detail SET interest_rate = ? WHERE liability_id = ?",
                    (str(_decimal(rate)), liability_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryError(f"loan_detail for liability {liability_id} does not exist")
