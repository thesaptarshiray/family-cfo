"""
Tests for `Database.unit_of_work()` and repository participation in it.

A Unit of Work makes several operations one atomic transaction: everything
done through it commits together when the `with` block ends cleanly, and is
rolled back together if anything raises.

The Financial Engine's own `create_transfer` does not use a Unit of Work yet
(moving its SQL behind repositories is a later step), so the transfer
scenarios here use `_transfer` below: a small stand-in that writes the two
legs of a transfer the way a repository method does — on the Unit of Work's
connection, without committing. It exists to prove the transaction semantics
on the real V6 schema, not to define how the Engine transfers money.
"""

import sqlite3
import uuid
from datetime import date
from decimal import Decimal
from typing import List, NamedTuple, Optional, Tuple

import pytest

from repository.asset_repository import AssetRepository
from repository.bank_repository import BankRepository
from repository.database import Database
from repository.errors import RepositoryError, RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


class World(NamedTuple):
    member_id: int
    family_id: int
    a: int  # account ids, opening balances 1000 / 500 / 300
    b: int
    c: int


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "uow.db")
    initialize_schema(db)
    return db


@pytest.fixture
def world(database):
    family_repo = FamilyRepository(database)
    bank_repo = BankRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    ids = [
        bank_repo.create_account(
            member.member_id,
            "Example Bank",
            "SAVINGS",
            last4,
            Decimal(opening),
            ownership=[(member.member_id, 100)],
        ).account_id
        for last4, opening in (("1111", "1000.00"), ("2222", "500.00"), ("3333", "300.00"))
    ]
    # Populate the balance caches as the Engine would, so "before" and
    # "after" are meaningful numbers rather than the schema default of 0.
    with database.transaction() as conn:
        conn.execute("UPDATE bank_account SET current_balance = opening_balance")
    return World(member.member_id, family.family_id, *ids)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _balance(database: Database, account_id: int) -> Decimal:
    return BankRepository(database).get_account(account_id).current_balance


def _ledger(database: Database) -> List[Tuple[int, Decimal, str]]:
    conn = database.connect()
    try:
        rows = conn.execute(
            'SELECT account_id, amount, transfer_group_id FROM "transaction" '
            "ORDER BY txn_id"
        ).fetchall()
    finally:
        conn.close()
    return [(r["account_id"], Decimal(str(r["amount"])), r["transfer_group_id"]) for r in rows]


def _count(database: Database, table: str) -> int:
    conn = database.connect()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _post_leg(database, uow, account_id, signed_amount, group_id) -> None:
    """One leg: a ledger row, then a full recompute of that account's balance."""
    with database.scope(uow) as conn:
        conn.execute(
            'INSERT INTO "transaction" '
            "(account_id, txn_date, amount, txn_type, transfer_group_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, "2026-01-15", str(signed_amount), "TRANSFER", group_id),
        )
        opening = conn.execute(
            "SELECT opening_balance FROM bank_account WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]
        posted = conn.execute(
            'SELECT amount FROM "transaction" WHERE account_id = ? AND status = ?',
            (account_id, "POSTED"),
        ).fetchall()
        balance = Decimal(str(opening)) + sum(
            (Decimal(str(row[0])) for row in posted), Decimal("0")
        )
        conn.execute(
            "UPDATE bank_account SET current_balance = ? WHERE account_id = ?",
            (str(balance), account_id),
        )


def _transfer(
    database: Database,
    uow,
    from_id: int,
    to_id: int,
    amount: Decimal,
    *,
    credit: Optional[Decimal] = None,
    crash_after_debit: bool = False,
) -> str:
    """Debit `from_id`, credit `to_id`, then check the two legs net to zero.

    `credit` lets a test post a credit that differs from the debit;
    `crash_after_debit` simulates a failure once the first leg is written.
    """
    group = str(uuid.uuid4())
    _post_leg(database, uow, from_id, -amount, group)
    if crash_after_debit:
        raise RuntimeError("simulated failure after the debit")
    _post_leg(database, uow, to_id, amount if credit is None else credit, group)

    with database.scope(uow) as conn:
        legs = conn.execute(
            'SELECT amount FROM "transaction" WHERE transfer_group_id = ?', (group,)
        ).fetchall()
    if sum((Decimal(str(row[0])) for row in legs), Decimal("0")) != 0:
        raise ValueError("debit and credit amounts must match")
    return group


# ----------------------------------------------------------------------
# The three transfer scenarios
# ----------------------------------------------------------------------


def test_successful_transfer_updates_both_accounts(database, world):
    with database.unit_of_work() as uow:
        group = _transfer(database, uow, world.a, world.b, Decimal("250.00"))

    (debit_acct, debit, debit_group), (credit_acct, credit, credit_group) = _ledger(database)
    assert (debit_acct, credit_acct) == (world.a, world.b)
    assert debit == Decimal("-250") and credit == Decimal("250")
    assert debit == -credit                      # the two amounts match
    assert debit_group == credit_group == group  # and belong to one transfer
    assert _balance(database, world.a) == Decimal("750")
    assert _balance(database, world.b) == Decimal("750")
    assert _balance(database, world.c) == Decimal("300")  # uninvolved account untouched

    # Durable: visible to a brand-new Database instance, not just this session.
    reopened = Database(database.db_path)
    assert _balance(reopened, world.a) == Decimal("750")
    assert _balance(reopened, world.b) == Decimal("750")


def test_transfer_failing_after_the_debit_rolls_everything_back(database, world):
    bank_repo = BankRepository(database)
    seen_inside = {}

    with pytest.raises(RuntimeError, match="simulated failure"):
        with database.unit_of_work() as uow:
            try:
                _transfer(
                    database, uow, world.a, world.b, Decimal("250.00"),
                    crash_after_debit=True,
                )
            except RuntimeError:
                # The operation had genuinely started: inside the unit the
                # debit is applied. Rollback must undo it all the same.
                seen_inside["a_balance"] = bank_repo.get_account(world.a, uow=uow).current_balance
                with database.scope(uow) as conn:
                    seen_inside["ledger_rows"] = conn.execute(
                        'SELECT COUNT(*) FROM "transaction"'
                    ).fetchone()[0]
                raise

    assert seen_inside == {"a_balance": Decimal("750"), "ledger_rows": 1}
    assert _ledger(database) == []
    assert _balance(database, world.a) == Decimal("1000")
    assert _balance(database, world.b) == Decimal("500")


def test_transfer_failing_on_the_credit_leg_rolls_back_the_debit(database, world):
    """A real database failure, not a simulated one: the credit goes to an
    account that does not exist, so the foreign key rejects it."""
    with pytest.raises(RepositoryIntegrityError) as excinfo:
        with database.unit_of_work() as uow:
            _transfer(database, uow, world.a, 999, Decimal("250.00"))

    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
    assert _ledger(database) == []
    assert _balance(database, world.a) == Decimal("1000")


def test_transfer_with_mismatched_legs_is_rolled_back(database, world):
    """The check that debit and credit match can only run after both legs are
    written, so it is the Unit of Work that guarantees a bad transfer leaves
    nothing behind."""
    with pytest.raises(ValueError, match="must match"):
        with database.unit_of_work() as uow:
            _transfer(
                database, uow, world.a, world.b, Decimal("250.00"),
                credit=Decimal("200.00"),
            )

    assert _ledger(database) == []
    assert _balance(database, world.a) == Decimal("1000")
    assert _balance(database, world.b) == Decimal("500")


def test_failure_of_one_unit_of_work_does_not_affect_another(database, world):
    with database.unit_of_work() as uow:
        first = _transfer(database, uow, world.a, world.b, Decimal("250.00"))

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            _transfer(
                database, uow, world.b, world.c, Decimal("100.00"),
                crash_after_debit=True,
            )

    # The committed transfer is exactly as it was; the failed one left no trace.
    assert [group for _, _, group in _ledger(database)] == [first, first]
    assert _balance(database, world.a) == Decimal("750")
    assert _balance(database, world.b) == Decimal("750")
    assert _balance(database, world.c) == Decimal("300")

    # And the failure did not poison anything: the next unit of work commits.
    with database.unit_of_work() as uow:
        _transfer(database, uow, world.c, world.a, Decimal("50.00"))
    assert _balance(database, world.c) == Decimal("250")
    assert _balance(database, world.a) == Decimal("800")
    assert len(_ledger(database)) == 4


# ----------------------------------------------------------------------
# Repositories participate in the same unit
# ----------------------------------------------------------------------


def test_repositories_in_one_unit_commit_together(database):
    family_repo = FamilyRepository(database)
    bank_repo = BankRepository(database)

    with database.unit_of_work() as uow:
        family = family_repo.create_family("Verma", uow=uow)
        member = family_repo.create_family_member(family.family_id, "Asha", "self", uow=uow)
        bank_repo.create_account(
            member.member_id, "Example Bank", "SAVINGS", "4444",
            ownership=[(member.member_id, 100)], uow=uow,
        )
        # Not committed yet: another connection cannot see any of it.
        assert _count(database, "family") == 0
        assert _count(database, "bank_account") == 0
        # ...but the unit itself can read its own uncommitted work.
        assert family_repo.get_family(family.family_id, uow=uow) == family

    assert _count(database, "family") == 1
    assert _count(database, "family_member") == 1
    assert _count(database, "bank_account") == 1
    assert _count(database, "account_ownership") == 1


def test_repositories_in_one_unit_roll_back_together(database):
    family_repo = FamilyRepository(database)
    bank_repo = BankRepository(database)

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            family = family_repo.create_family("Verma", uow=uow)
            member = family_repo.create_family_member(family.family_id, "Asha", "self", uow=uow)
            bank_repo.create_account(
                member.member_id, "Example Bank", "SAVINGS", "4444",
                ownership=[(member.member_id, 100)], uow=uow,
            )
            raise RuntimeError("fail after three repository calls")

    for table in ("family", "family_member", "bank_account", "account_ownership"):
        assert _count(database, table) == 0, table


def test_without_a_unit_of_work_each_repository_call_commits_on_its_own(database):
    """The behaviour the Unit of Work exists to fix, kept as a documented
    contrast: with no `uow`, a later failure cannot undo an earlier call."""
    family_repo = FamilyRepository(database)

    with pytest.raises(RuntimeError):
        family_repo.create_family("Verma")
        raise RuntimeError("fail after the first repository call")

    assert _count(database, "family") == 1


def test_asset_repository_joins_the_unit_of_work(database, world):
    asset_repo = AssetRepository(database)

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            asset_repo.create_mutual_fund(
                world.member_id, "Balanced Fund", None, 1000, date(2026, 1, 1),
                [(world.member_id, 100)],
                amc="AMC", scheme_name="Scheme", folio_number="F-1", uow=uow,
            )
            raise RuntimeError("fail after the asset was written")
    assert _count(database, "asset") == 0
    assert _count(database, "mf_holding") == 0
    assert _count(database, "asset_ownership") == 0

    with database.unit_of_work() as uow:
        asset, holding = asset_repo.create_mutual_fund(
            world.member_id, "Balanced Fund", None, 1000, date(2026, 1, 1),
            [(world.member_id, 100)],
            amc="AMC", scheme_name="Scheme", folio_number="F-1", uow=uow,
        )
    assert asset_repo.get_asset(asset.asset_id) == asset
    assert asset_repo.get_mutual_fund(asset.asset_id) == holding


def test_a_repository_call_that_fails_midway_is_undone_on_its_own(database, world):
    """`create_account` writes the account row and then its ownership rows. If
    an ownership row is rejected, the account row must not linger inside the
    unit — otherwise a caller that catches the error and carries on would
    commit half of a repository operation."""
    bank_repo = BankRepository(database)

    with database.unit_of_work() as uow:
        with pytest.raises(RepositoryIntegrityError):
            bank_repo.create_account(
                world.member_id, "Example Bank", "JOINT", "5555",
                ownership=[(world.member_id, 60), (999, 40)],  # 999: no such member
                uow=uow,
            )
        assert len(bank_repo.list_accounts(world.member_id, uow=uow)) == 3  # the three fixtures only

        bank_repo.create_account(
            world.member_id, "Example Bank", "SAVINGS", "6666",
            ownership=[(world.member_id, 100)], uow=uow,
        )

    numbers = [a.account_number_masked for a in bank_repo.list_accounts(world.member_id)]
    assert numbers == ["1111", "2222", "3333", "6666"]


# ----------------------------------------------------------------------
# The unit itself
# ----------------------------------------------------------------------


def test_unit_of_work_holds_the_write_lock_from_the_start(database):
    """`BEGIN IMMEDIATE`: another writer is turned away while the unit is
    open, so a check made inside the unit cannot be invalidated before it
    commits. The lock is released when the unit ends."""
    other = sqlite3.connect(str(database.db_path), timeout=0)
    try:
        with database.unit_of_work():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")  # released after the block
        other.rollback()
    finally:
        other.close()


def test_a_finished_unit_of_work_cannot_be_reused(database):
    family_repo = FamilyRepository(database)

    with database.unit_of_work() as uow:
        family = family_repo.create_family("Verma", uow=uow)
    with pytest.raises(RepositoryError, match="UnitOfWork is closed"):
        family_repo.get_family(family.family_id, uow=uow)

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as failed_uow:
            raise RuntimeError("boom")
    with pytest.raises(RepositoryError, match="UnitOfWork is closed"):
        family_repo.get_family(family.family_id, uow=failed_uow)


def test_a_unit_of_work_from_another_database_is_rejected(database, tmp_path):
    other_db = Database(tmp_path / "other.db")
    initialize_schema(other_db)

    with other_db.unit_of_work() as foreign_uow:
        with pytest.raises(ValueError, match="different database"):
            FamilyRepository(database).create_family("Verma", uow=foreign_uow)

    assert _count(database, "family") == 0
    assert _count(other_db, "family") == 0


def test_sqlite_errors_inside_a_unit_are_translated(database):
    with pytest.raises(RepositoryIntegrityError) as excinfo:
        with database.unit_of_work() as uow:
            with database.scope(uow) as conn:
                conn.execute(
                    "INSERT INTO family_member (family_id, name, relationship) "
                    "VALUES (999, 'Nobody', 'self')"
                )

    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
