"""
Persistence for ledger transactions (the `"transaction"` table).

`TransactionRepository` turns method calls into parameterized SQL and rows
into `Transaction` objects. That is all it does. It makes no financial
decision: it does not check that an amount is positive, that the two legs of
a transfer match, that an account can afford a debit, or that a status change
is a legal reversal. Those are the Financial Engine's rules, and the Engine
decides before it calls in here. The only rules enforced are the ones the
schema itself declares (NOT NULL, CHECK, FOREIGN KEY), reported as
`RepositoryIntegrityError`.

Writes always run inside a caller-supplied `UnitOfWork`. A ledger row written
on its own, outside the operation that gives it meaning (its balance update,
the other leg of its transfer), would be a partial change, so a write cannot
be made without one, and this repository never commits or rolls back: the
caller owns the unit's lifecycle. Reads take an optional `uow` like the other
repositories; given one they see its uncommitted rows.

Only the columns the Financial Engine uses today are mapped. The rest of the
table (category_id, asset_id, liability_id, data_confidence, import_batch_id,
reference_no) is added when the feature that needs it arrives.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import List, Optional, Sequence, Union

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

Money = Union[Decimal, int, str]

_SELECT_BY_ID = (
    "SELECT txn_id, account_id, txn_date, amount, txn_type, description, "
    "transfer_group_id, status, reversal_of_txn_id "
    'FROM "transaction" WHERE txn_id = ?'
)


@dataclass(frozen=True)
class Transaction:
    """One row of the `"transaction"` table.

    The first six fields are the Financial Engine's original transaction
    record, unchanged in name, order and meaning. `transfer_group_id`,
    `status` and `reversal_of_txn_id` are further columns of the same table
    that the Engine already wrote and read directly; they are mapped here so
    the Engine no longer needs SQL to reach them. `amount` is signed:
    negative is an outflow.
    """

    txn_id: int
    account_id: int
    txn_date: date
    amount: Decimal
    txn_type: str
    description: Optional[str] = None
    transfer_group_id: Optional[str] = None
    status: str = "POSTED"  # mirrors the schema default; rows read back carry the real value
    reversal_of_txn_id: Optional[int] = None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _transaction_from_row(row: sqlite3.Row) -> Transaction:
    return Transaction(
        txn_id=row["txn_id"],
        account_id=row["account_id"],
        txn_date=date.fromisoformat(row["txn_date"]),
        amount=_decimal(row["amount"]),
        txn_type=row["txn_type"],
        description=row["description"],
        transfer_group_id=row["transfer_group_id"],
        status=row["status"],
        reversal_of_txn_id=row["reversal_of_txn_id"],
    )


class TransactionRepository(BaseRepository):
    """Create, read and re-status ledger transactions.

    Write methods require a keyword `uow` and join that Unit of Work: they run
    on its connection inside a savepoint and never commit. Read methods take
    an optional `uow`; without one they use a connection of their own.
    """

    def create_transaction(
        self,
        *,
        account_id: int,
        txn_date: date,
        amount: Money,
        txn_type: str,
        transfer_group_id: Optional[str] = None,
        status: Optional[str] = None,
        reversal_of_txn_id: Optional[int] = None,
        description: Optional[str] = None,
        uow: UnitOfWork,
    ) -> Transaction:
        """Insert one ledger row and return it as stored.

        `amount` is stored as given (signed); the repository does not judge
        it. If `status` is omitted the column is left out of the INSERT, so
        the schema's own DEFAULT applies.
        """
        self._require_unit_of_work(uow, "create_transaction")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                if status is None:
                    cursor = conn.execute(
                        'INSERT INTO "transaction" (account_id, txn_date, amount, '
                        "txn_type, transfer_group_id, reversal_of_txn_id, description) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            account_id,
                            txn_date.isoformat(),
                            str(_decimal(amount)),
                            txn_type,
                            transfer_group_id,
                            reversal_of_txn_id,
                            description,
                        ),
                    )
                else:
                    cursor = conn.execute(
                        'INSERT INTO "transaction" (account_id, txn_date, amount, '
                        "txn_type, transfer_group_id, status, reversal_of_txn_id, "
                        "description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            account_id,
                            txn_date.isoformat(),
                            str(_decimal(amount)),
                            txn_type,
                            transfer_group_id,
                            status,
                            reversal_of_txn_id,
                            description,
                        ),
                    )
                row = conn.execute(_SELECT_BY_ID, (cursor.lastrowid,)).fetchone()
        return _transaction_from_row(row)

    def get_transaction(
        self, txn_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[Transaction]:
        """Return the transaction, or `None` if no row has that id."""
        row = self._fetch_one(_SELECT_BY_ID, (txn_id,), uow)
        return None if row is None else _transaction_from_row(row)

    def update_transaction_status(
        self, txn_id: int, status: str, *, uow: UnitOfWork
    ) -> Optional[Transaction]:
        """Set the row's status and return it, or `None` if no row has that id.

        Any status the schema accepts is stored. Whether the change is a legal
        step (POSTED -> REVERSED and so on) is the Engine's decision.
        """
        self._require_unit_of_work(uow, "update_transaction_status")
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    'UPDATE "transaction" SET status = ? WHERE txn_id = ?',
                    (status, txn_id),
                )
                if cursor.rowcount == 0:
                    return None
                row = conn.execute(_SELECT_BY_ID, (txn_id,)).fetchone()
        return _transaction_from_row(row)

    def list_amounts(
        self,
        account_id: int,
        statuses: Sequence[str],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> List[Decimal]:
        """Return the signed amounts of `account_id`'s rows whose status is one
        of `statuses`, oldest `txn_id` first.

        The rows are returned, not summed: adding them up is a financial
        calculation and belongs to the Engine. Which statuses count is the
        Engine's decision too, which is why the caller passes them in.
        """
        if isinstance(statuses, str):
            raise TypeError("statuses must be a sequence of status strings, not one string")
        statuses = tuple(statuses)
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        rows = self._fetch_all(
            'SELECT amount FROM "transaction" WHERE account_id = ? AND status IN ('
            + placeholders
            + ") ORDER BY txn_id",
            (account_id, *statuses),
            uow,
        )
        return [_decimal(row["amount"]) for row in rows]
