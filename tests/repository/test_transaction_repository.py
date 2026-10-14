"""
Tests for `TransactionRepository`: persistence of ledger rows, on a caller's
Unit of Work, with no financial decisions of its own.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from repository.bank_repository import BankRepository
from repository.database import Database
from repository.errors import RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema
from repository.transaction_repository import Transaction, TransactionRepository

DAY = date(2026, 1, 15)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "ledger.db")
    initialize_schema(db)
    return db


@pytest.fixture
def txn_repo(database):
    return TransactionRepository(database)


@pytest.fixture
def accounts(database):
    """Two committed bank accounts (ids) belonging to one member."""
    family_repo = FamilyRepository(database)
    bank_repo = BankRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    return [
        bank_repo.create_account(
            member.member_id, "Example Bank", "SAVINGS", last4,
            ownership=[(member.member_id, 100)],
        ).account_id
        for last4 in ("1111", "2222")
    ], member.member_id


def _count(database, table='"transaction"'):
    conn = database.connect()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _leg(txn_repo, uow, account_id, amount, **overrides):
    fields = dict(
        account_id=account_id, txn_date=DAY, amount=amount, txn_type="TRANSFER",
        transfer_group_id="group-1", description="test leg", uow=uow,
    )
    fields.update(overrides)
    return txn_repo.create_transaction(**fields)


# ----------------------------------------------------------------------
# Creating and reading
# ----------------------------------------------------------------------


def test_create_transaction_returns_the_stored_row(database, txn_repo, accounts):
    (a, _), _ = accounts

    with database.unit_of_work() as uow:
        txn = txn_repo.create_transaction(
            account_id=a, txn_date=DAY, amount=Decimal("-250.00"), txn_type="TRANSFER",
            transfer_group_id="group-1", description="Family transfer", uow=uow,
        )

    assert txn == Transaction(
        txn_id=1, account_id=a, txn_date=DAY, amount=Decimal("-250.00"),
        txn_type="TRANSFER", description="Family transfer",
        transfer_group_id="group-1", status="POSTED", reversal_of_txn_id=None,
    )
    assert txn_repo.get_transaction(txn.txn_id) == txn


def test_status_defaults_from_the_schema_and_can_be_set_explicitly(database, txn_repo, accounts):
    (a, _), _ = accounts

    with database.unit_of_work() as uow:
        default = txn_repo.create_transaction(
            account_id=a, txn_date=DAY, amount=100, txn_type="INCOME", uow=uow
        )
        explicit = txn_repo.create_transaction(
            account_id=a, txn_date=DAY, amount="12.50", txn_type="INCOME",
            status="VOID", uow=uow,
        )

    assert default.status == "POSTED"
    assert explicit.status == "VOID"
    assert explicit.amount == Decimal("12.5")  # any Money input is stored as an exact decimal


def test_get_transaction_missing_returns_none(txn_repo):
    assert txn_repo.get_transaction(999) is None


def test_reversal_link_is_stored(database, txn_repo, accounts):
    (a, _), _ = accounts

    with database.unit_of_work() as uow:
        original = _leg(txn_repo, uow, a, -100)
        reversal = _leg(
            txn_repo, uow, a, 100, transfer_group_id="group-2", status="REVERSED",
            reversal_of_txn_id=original.txn_id,
        )

    assert txn_repo.get_transaction(reversal.txn_id).reversal_of_txn_id == original.txn_id


# ----------------------------------------------------------------------
# Unit of Work behaviour
# ----------------------------------------------------------------------


def test_write_methods_require_a_unit_of_work(database, txn_repo, accounts):
    (a, _), _ = accounts

    with pytest.raises(TypeError):  # `uow` has no default: leaving it out is an error
        txn_repo.create_transaction(account_id=a, txn_date=DAY, amount=1, txn_type="INCOME")
    with pytest.raises(TypeError):
        txn_repo.update_transaction_status(1, "VOID")

    # Passing None explicitly must not quietly become a private transaction.
    with pytest.raises(ValueError, match="requires a UnitOfWork"):
        txn_repo.create_transaction(
            account_id=a, txn_date=DAY, amount=1, txn_type="INCOME", uow=None
        )
    with pytest.raises(ValueError, match="requires a UnitOfWork"):
        txn_repo.update_transaction_status(1, "VOID", uow=None)

    assert _count(database) == 0


def test_create_is_rolled_back_with_the_unit(database, txn_repo, accounts):
    (a, _), _ = accounts

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            txn = _leg(txn_repo, uow, a, -250)
            assert txn_repo.get_transaction(txn.txn_id, uow=uow) == txn  # visible inside
            assert txn_repo.get_transaction(txn.txn_id) is None  # a separate connection: not yet
            raise RuntimeError("fail after the ledger row was written")

    assert _count(database) == 0


def test_create_is_committed_with_the_unit_and_is_durable(database, txn_repo, accounts):
    (a, _), _ = accounts

    with database.unit_of_work() as uow:
        txn = _leg(txn_repo, uow, a, -250)
        assert _count(database) == 0  # the repository did not commit on its own

    assert _count(database) == 1
    reopened = TransactionRepository(Database(database.db_path))
    assert reopened.get_transaction(txn.txn_id) == txn


def test_ledger_rows_and_other_repositories_share_one_unit(database, txn_repo, accounts):
    (a, _), member_id = accounts
    bank_repo = BankRepository(database)

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            account = bank_repo.create_account(
                member_id, "Example Bank", "SAVINGS", "3333",
                ownership=[(member_id, 100)], uow=uow,
            )
            _leg(txn_repo, uow, account.account_id, 500, txn_type="INCOME", transfer_group_id=None)
            raise RuntimeError("fail after both repositories wrote")
    assert _count(database, "bank_account") == 2  # only the two fixture accounts
    assert _count(database) == 0

    with database.unit_of_work() as uow:
        account = bank_repo.create_account(
            member_id, "Example Bank", "SAVINGS", "3333",
            ownership=[(member_id, 100)], uow=uow,
        )
        _leg(txn_repo, uow, account.account_id, 500, txn_type="INCOME", transfer_group_id=None)
    assert _count(database, "bank_account") == 3
    assert _count(database) == 1


def test_a_failed_create_is_undone_on_its_own_and_the_unit_continues(
    database, txn_repo, accounts
):
    (a, _), _ = accounts

    with database.unit_of_work() as uow:
        _leg(txn_repo, uow, a, -100)
        with pytest.raises(RepositoryIntegrityError):
            _leg(txn_repo, uow, 999, 100)  # no such account
        assert _count(database) == 0  # nothing committed yet either way
        _leg(txn_repo, uow, a, -50, transfer_group_id="group-2")

    assert [amount for amount in txn_repo.list_amounts(a, ("POSTED",))] == [
        Decimal("-100"), Decimal("-50"),
    ]


# ----------------------------------------------------------------------
# Updating status
# ----------------------------------------------------------------------


def test_update_status_is_committed_with_the_unit(database, txn_repo, accounts):
    (a, _), _ = accounts
    with database.unit_of_work() as uow:
        txn = _leg(txn_repo, uow, a, -100)

    with database.unit_of_work() as uow:
        updated = txn_repo.update_transaction_status(txn.txn_id, "REVERSED", uow=uow)
        assert updated.status == "REVERSED"
        assert txn_repo.get_transaction(txn.txn_id).status == "POSTED"  # not committed yet

    assert txn_repo.get_transaction(txn.txn_id).status == "REVERSED"
    assert updated == txn_repo.get_transaction(txn.txn_id)


def test_update_status_is_rolled_back_with_the_unit(database, txn_repo, accounts):
    (a, _), _ = accounts
    with database.unit_of_work() as uow:
        txn = _leg(txn_repo, uow, a, -100)

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            txn_repo.update_transaction_status(txn.txn_id, "REVERSED", uow=uow)
            assert txn_repo.get_transaction(txn.txn_id, uow=uow).status == "REVERSED"
            raise RuntimeError("fail after the status change")

    assert txn_repo.get_transaction(txn.txn_id) == txn


def test_update_status_of_a_missing_row_returns_none(database, txn_repo):
    with database.unit_of_work() as uow:
        assert txn_repo.update_transaction_status(999, "REVERSED", uow=uow) is None


# ----------------------------------------------------------------------
# list_amounts: rows for the Engine to add up
# ----------------------------------------------------------------------


def test_list_amounts_returns_exact_decimals_not_a_sum(database, txn_repo, accounts):
    (a, b), _ = accounts

    with database.unit_of_work() as uow:
        _leg(txn_repo, uow, a, "0.10", txn_type="INCOME", transfer_group_id=None)
        _leg(txn_repo, uow, a, "0.20", txn_type="INCOME", transfer_group_id=None)
        _leg(txn_repo, uow, a, "-5.00", txn_type="EXPENSE", transfer_group_id=None,
             status="REVERSED")
        _leg(txn_repo, uow, b, "999", txn_type="INCOME", transfer_group_id=None)

    posted = txn_repo.list_amounts(a, ("POSTED",))
    assert posted == [Decimal("0.1"), Decimal("0.2")]  # rows, oldest first, this account only
    assert sum(posted, Decimal("0")) == Decimal("0.3")  # exact in Decimal
    assert txn_repo.list_amounts(a, ("POSTED", "REVERSED")) == [
        Decimal("0.1"), Decimal("0.2"), Decimal("-5"),
    ]
    assert txn_repo.list_amounts(a, ("VOID",)) == []


def test_list_amounts_with_no_statuses_is_empty_and_a_bare_string_is_rejected(
    txn_repo, accounts
):
    (a, _), _ = accounts
    assert txn_repo.list_amounts(a, ()) == []
    with pytest.raises(TypeError, match="sequence of status strings"):
        txn_repo.list_amounts(a, "POSTED")


def test_list_amounts_sees_uncommitted_rows_only_through_the_unit(database, txn_repo, accounts):
    (a, _), _ = accounts

    with database.unit_of_work() as uow:
        _leg(txn_repo, uow, a, -100)
        assert txn_repo.list_amounts(a, ("POSTED",), uow=uow) == [Decimal("-100")]
        assert txn_repo.list_amounts(a, ("POSTED",)) == []  # separate connection

    assert txn_repo.list_amounts(a, ("POSTED",)) == [Decimal("-100")]


# ----------------------------------------------------------------------
# Error handling and the persistence-only boundary
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"transfer_group_id": None},  # CHECK: a TRANSFER leg must carry its pairing id
        {"account_id": 999},  # FOREIGN KEY: no such account
        {"status": "BOGUS"},  # CHECK: not a valid status
        {"txn_type": "GIFT"},  # CHECK: not a valid type
        {"reversal_of_txn_id": 999},  # FOREIGN KEY: no such transaction
    ],
)
def test_schema_constraint_violations_are_translated(database, txn_repo, accounts, overrides):
    (a, _), _ = accounts

    with pytest.raises(RepositoryIntegrityError) as excinfo:
        with database.unit_of_work() as uow:
            _leg(txn_repo, uow, overrides.get("account_id", a), -100,
                 **{k: v for k, v in overrides.items() if k != "account_id"})

    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert _count(database) == 0


def test_the_repository_applies_no_financial_rules(database, txn_repo, accounts):
    """Persistence only. Whether a transfer is balanced, an amount sensible or a
    status change legal is decided by the Financial Engine before it calls in,
    so the repository stores whatever the schema allows — including things the
    Engine would never ask for."""
    (a, _), _ = accounts

    with database.unit_of_work() as uow:
        lone_leg = _leg(txn_repo, uow, a, -100)  # a transfer leg with no matching credit
        zero = _leg(txn_repo, uow, a, 0, txn_type="EXPENSE", transfer_group_id=None)
        odd = _leg(txn_repo, uow, a, 100, txn_type="EXPENSE", transfer_group_id=None)  # inflow "expense"
        back = txn_repo.update_transaction_status(lone_leg.txn_id, "REVERSED", uow=uow)
        again = txn_repo.update_transaction_status(lone_leg.txn_id, "POSTED", uow=uow)  # un-reverse

    assert (lone_leg.amount, zero.amount, odd.amount) == (Decimal("-100"), Decimal("0"), Decimal("100"))
    assert back.status == "REVERSED" and again.status == "POSTED"
    assert _count(database) == 3
