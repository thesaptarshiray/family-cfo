"""
Tests for `DerivedCacheRepository`: it stores a cache value the Financial
Engine computed, on a caller's Unit of Work, and does nothing else.
"""

from datetime import date
from decimal import Decimal

import pytest

from repository.asset_repository import AssetRepository
from repository.bank_repository import BankAccount, BankRepository
from repository.database import Database
from repository.derived_cache import DerivedCacheRepository
from repository.errors import RepositoryError
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema
from repository.transaction_repository import TransactionRepository


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "cache.db")
    initialize_schema(db)
    return db


@pytest.fixture
def cache_repo(database):
    return DerivedCacheRepository(database)


@pytest.fixture
def bank_repo(database):
    return BankRepository(database)


@pytest.fixture
def accounts(database, bank_repo):
    """Two committed accounts, opening balances 1000 and 500, cache still 0."""
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    return [
        bank_repo.create_account(
            member.member_id, "Example Bank", "SAVINGS", last4, Decimal(opening),
            ownership=[(member.member_id, 100)],
        )
        for last4, opening in (("1111", "1000.00"), ("2222", "500.00"))
    ]


def _cached(bank_repo, account_id):
    return bank_repo.get_account(account_id).current_balance


# ----------------------------------------------------------------------
# What it stores
# ----------------------------------------------------------------------


def test_stores_the_value_it_is_given(database, cache_repo, bank_repo, accounts):
    a, _ = accounts

    with database.unit_of_work() as uow:
        result = cache_repo.set_bank_account_current_balance(
            a.account_id, Decimal("750.00"), uow=uow
        )

    assert result is None
    assert _cached(bank_repo, a.account_id) == Decimal("750")


def test_touches_only_current_balance_of_the_named_account(
    database, cache_repo, bank_repo, accounts
):
    a, b = accounts

    with database.unit_of_work() as uow:
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("750"), uow=uow)

    after_a = bank_repo.get_account(a.account_id)
    assert after_a == BankAccount(
        account_id=a.account_id, member_id=a.member_id, bank_name=a.bank_name,
        account_type=a.account_type, account_number_masked=a.account_number_masked,
        opening_balance=a.opening_balance,  # source data untouched
        current_balance=Decimal("750"), is_active=a.is_active,
    )
    assert bank_repo.get_account(b.account_id) == b  # the other account untouched


def test_replaces_the_value_and_never_adjusts_it(database, cache_repo, bank_repo, accounts):
    """Setting is absolute: the same value twice is that value, not double it,
    and a new value replaces the old one whether higher or lower."""
    a, _ = accounts

    with database.unit_of_work() as uow:
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("100"), uow=uow)
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("100"), uow=uow)
        assert _cached(bank_repo, a.account_id) == Decimal("0")  # not committed yet
    assert _cached(bank_repo, a.account_id) == Decimal("100")

    with database.unit_of_work() as uow:
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("40"), uow=uow)
    assert _cached(bank_repo, a.account_id) == Decimal("40")


@pytest.mark.parametrize("supplied", [Decimal("1234.50"), 1234, "1234.50"])
def test_accepts_any_money_input_and_stores_it_exactly(
    database, cache_repo, bank_repo, accounts, supplied
):
    a, _ = accounts

    with database.unit_of_work() as uow:
        cache_repo.set_bank_account_current_balance(a.account_id, supplied, uow=uow)

    assert _cached(bank_repo, a.account_id) == Decimal(str(supplied))


def test_applies_no_financial_rules_and_does_not_check_the_ledger(
    database, cache_repo, bank_repo, accounts
):
    """It stores what the Engine decided. It does not recompute the balance,
    compare it with the ledger, or refuse a value the Engine's rules would."""
    a, _ = accounts  # no ledger rows exist at all

    with database.unit_of_work() as uow:
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("999999"), uow=uow)
    assert _cached(bank_repo, a.account_id) == Decimal("999999")

    with database.unit_of_work() as uow:  # even a negative balance: the Engine's rule, not its
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("-5"), uow=uow)
    assert _cached(bank_repo, a.account_id) == Decimal("-5")


# ----------------------------------------------------------------------
# Unit of Work behaviour
# ----------------------------------------------------------------------


def test_requires_a_unit_of_work(database, cache_repo, bank_repo, accounts):
    a, _ = accounts

    with pytest.raises(TypeError):  # `uow` has no default
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("1"))
    with pytest.raises(ValueError, match="requires a UnitOfWork"):  # explicit None
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("1"), uow=None)

    assert _cached(bank_repo, a.account_id) == Decimal("0")


def test_is_rolled_back_with_the_unit(database, cache_repo, bank_repo, accounts):
    a, _ = accounts

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            cache_repo.set_bank_account_current_balance(a.account_id, Decimal("750"), uow=uow)
            assert bank_repo.get_account(a.account_id, uow=uow).current_balance == Decimal("750")
            assert _cached(bank_repo, a.account_id) == Decimal("0")  # separate connection
            raise RuntimeError("fail after the cache was written")

    assert _cached(bank_repo, a.account_id) == Decimal("0")


def test_is_committed_with_the_unit_and_is_durable(database, cache_repo, accounts):
    a, _ = accounts

    with database.unit_of_work() as uow:
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("750"), uow=uow)

    reopened = BankRepository(Database(database.db_path))
    assert _cached(reopened, a.account_id) == Decimal("750")


def test_a_ledger_row_and_its_cache_update_commit_or_roll_back_together(
    database, cache_repo, bank_repo, accounts
):
    """The atomicity the Engine relies on: the source change and the cache
    computed from it live or die in the same Unit of Work."""
    a, _ = accounts
    txn_repo = TransactionRepository(database)

    def post(uow):
        txn_repo.create_transaction(
            account_id=a.account_id, txn_date=date(2026, 1, 15), amount=Decimal("-250"),
            txn_type="EXPENSE", uow=uow,
        )
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("750"), uow=uow)

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            post(uow)
            raise RuntimeError("fail after ledger row and cache were written")
    assert txn_repo.list_amounts(a.account_id, ("POSTED",)) == []
    assert _cached(bank_repo, a.account_id) == Decimal("0")

    with database.unit_of_work() as uow:
        post(uow)
    assert txn_repo.list_amounts(a.account_id, ("POSTED",)) == [Decimal("-250")]
    assert _cached(bank_repo, a.account_id) == Decimal("750")


# ----------------------------------------------------------------------
# Missing account
# ----------------------------------------------------------------------


def test_a_missing_account_is_an_error_not_a_silent_no_op(
    database, cache_repo, bank_repo, accounts
):
    a, _ = accounts

    with pytest.raises(RepositoryError, match="bank_account 999 does not exist"):
        with database.unit_of_work() as uow:
            cache_repo.set_bank_account_current_balance(999, Decimal("1"), uow=uow)

    # ...and inside a unit, a caller that handles the error can carry on.
    with database.unit_of_work() as uow:
        cache_repo.set_bank_account_current_balance(a.account_id, Decimal("750"), uow=uow)
        with pytest.raises(RepositoryError):
            cache_repo.set_bank_account_current_balance(999, Decimal("1"), uow=uow)
    assert _cached(bank_repo, a.account_id) == Decimal("750")


# ----------------------------------------------------------------------
# set_asset_current_value: the same setter, generalised across every asset
# category (proven here with one FD; MF/stock will use the identical method)
# ----------------------------------------------------------------------


@pytest.fixture
def fd_asset(database):
    """One committed FD asset, current_value still at the schema default (0)."""
    asset_repo = AssetRepository(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    asset, _ = asset_repo.create_fd(
        member.member_id, "Example Bank FD", "Example Bank", Decimal("100000.00"),
        date(2026, 1, 1), [(member.member_id, 100)],
        fd_number="FD-001", bank_name="Example Bank",
        interest_rate=Decimal("7.00"), maturity_date=date(2027, 1, 1),
        payout_type="MATURITY", maturity_value=Decimal("107000.00"),
    )
    return asset_repo, asset


def _asset_cached(asset_repo, asset_id):
    return asset_repo.get_asset(asset_id).current_value


def test_set_asset_current_value_stores_the_value_it_is_given(database, cache_repo, fd_asset):
    asset_repo, asset = fd_asset

    with database.unit_of_work() as uow:
        result = cache_repo.set_asset_current_value(asset.asset_id, Decimal("103400.00"), uow=uow)

    assert result is None
    assert _asset_cached(asset_repo, asset.asset_id) == Decimal("103400")


def test_set_asset_current_value_touches_only_the_named_asset(database, cache_repo, fd_asset):
    asset_repo, asset = fd_asset
    other_repo = AssetRepository(database)
    family_repo = FamilyRepository(database)
    family_id = family_repo.get_family_member(asset.member_id).family_id
    other_member = family_repo.create_family_member(family_id, "Asha", "spouse")
    other_asset, _ = other_repo.create_fd(
        other_member.member_id, "Other FD", "Other Bank", 50000, date(2026, 1, 1),
        [(other_member.member_id, 100)], fd_number="FD-XYZ", bank_name="Other Bank",
        interest_rate=6, maturity_date=date(2027, 1, 1), payout_type="MATURITY",
    )

    with database.unit_of_work() as uow:
        cache_repo.set_asset_current_value(asset.asset_id, Decimal("103400"), uow=uow)

    assert _asset_cached(asset_repo, asset.asset_id) == Decimal("103400")
    assert _asset_cached(other_repo, other_asset.asset_id) == Decimal("0")  # untouched


def test_set_asset_current_value_requires_a_unit_of_work(database, cache_repo, fd_asset):
    asset_repo, asset = fd_asset

    with pytest.raises(TypeError):
        cache_repo.set_asset_current_value(asset.asset_id, Decimal("1"))
    with pytest.raises(ValueError, match="requires a UnitOfWork"):
        cache_repo.set_asset_current_value(asset.asset_id, Decimal("1"), uow=None)

    assert _asset_cached(asset_repo, asset.asset_id) == Decimal("0")


def test_set_asset_current_value_is_rolled_back_with_the_unit(database, cache_repo, fd_asset):
    asset_repo, asset = fd_asset

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            cache_repo.set_asset_current_value(asset.asset_id, Decimal("103400"), uow=uow)
            assert asset_repo.get_asset(asset.asset_id, uow=uow).current_value == Decimal("103400")
            assert _asset_cached(asset_repo, asset.asset_id) == Decimal("0")  # separate connection
            raise RuntimeError("fail after the cache was written")

    assert _asset_cached(asset_repo, asset.asset_id) == Decimal("0")


def test_set_asset_current_value_is_committed_with_the_unit_and_is_durable(database, fd_asset):
    asset_repo, asset = fd_asset
    cache_repo = DerivedCacheRepository(database)

    with database.unit_of_work() as uow:
        cache_repo.set_asset_current_value(asset.asset_id, Decimal("103400"), uow=uow)

    reopened = AssetRepository(Database(database.db_path))
    assert _asset_cached(reopened, asset.asset_id) == Decimal("103400")


def test_set_asset_current_value_on_a_missing_asset_is_an_error(database, cache_repo, fd_asset):
    with pytest.raises(RepositoryError, match="asset 999 does not exist"):
        with database.unit_of_work() as uow:
            cache_repo.set_asset_current_value(999, Decimal("1"), uow=uow)


# ----------------------------------------------------------------------
# set_asset_invested_amount and set_mf_units_and_avg_nav — the MF setters
# ----------------------------------------------------------------------


@pytest.fixture
def mf_asset(database):
    """One committed MF asset, invested_amount/units/avg_nav still at the
    schema defaults (0, 0, null)."""
    asset_repo = AssetRepository(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    asset, holding = asset_repo.create_mutual_fund(
        member.member_id, "Balanced Fund", None, 0, date(2026, 1, 1),
        [(member.member_id, 100)],
        amc="Example AMC", scheme_name="Balanced Growth", folio_number="FOLIO-1",
    )
    return asset_repo, asset


def test_set_asset_invested_amount_stores_the_value_it_is_given(database, cache_repo, mf_asset):
    asset_repo, asset = mf_asset

    with database.unit_of_work() as uow:
        cache_repo.set_asset_invested_amount(asset.asset_id, Decimal("5000"), uow=uow)

    assert asset_repo.get_asset(asset.asset_id).invested_amount == Decimal("5000")


def test_set_asset_invested_amount_requires_a_unit_of_work(database, cache_repo, mf_asset):
    _, asset = mf_asset
    with pytest.raises(TypeError):
        cache_repo.set_asset_invested_amount(asset.asset_id, Decimal("1"))
    with pytest.raises(ValueError, match="requires a UnitOfWork"):
        cache_repo.set_asset_invested_amount(asset.asset_id, Decimal("1"), uow=None)


def test_set_asset_invested_amount_on_a_missing_asset_is_an_error(database, cache_repo):
    with pytest.raises(RepositoryError, match="asset 999 does not exist"):
        with database.unit_of_work() as uow:
            cache_repo.set_asset_invested_amount(999, Decimal("1"), uow=uow)


def test_set_mf_units_and_avg_nav_stores_both_values_together(database, cache_repo, mf_asset):
    asset_repo, asset = mf_asset

    with database.unit_of_work() as uow:
        cache_repo.set_mf_units_and_avg_nav(
            asset.asset_id, Decimal("10.5"), Decimal("95.2381"), uow=uow
        )

    holding = asset_repo.get_mutual_fund(asset.asset_id)
    assert holding.units == Decimal("10.5")
    assert holding.avg_nav == Decimal("95.2381")


def test_set_mf_units_and_avg_nav_requires_a_unit_of_work(database, cache_repo, mf_asset):
    _, asset = mf_asset
    with pytest.raises(TypeError):
        cache_repo.set_mf_units_and_avg_nav(asset.asset_id, Decimal("1"), Decimal("1"))
    with pytest.raises(ValueError, match="requires a UnitOfWork"):
        cache_repo.set_mf_units_and_avg_nav(asset.asset_id, Decimal("1"), Decimal("1"), uow=None)


def test_set_mf_units_and_avg_nav_is_rolled_back_with_the_unit(database, cache_repo, mf_asset):
    asset_repo, asset = mf_asset

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            cache_repo.set_mf_units_and_avg_nav(asset.asset_id, Decimal("10"), Decimal("100"), uow=uow)
            raise RuntimeError("fail after the cache was written")

    holding = asset_repo.get_mutual_fund(asset.asset_id)
    assert holding.units == Decimal("0")
    assert holding.avg_nav is None


def test_set_mf_units_and_avg_nav_on_a_missing_holding_is_an_error(database, cache_repo):
    with pytest.raises(RepositoryError, match="mf_holding for asset 999 does not exist"):
        with database.unit_of_work() as uow:
            cache_repo.set_mf_units_and_avg_nav(999, Decimal("1"), Decimal("1"), uow=uow)
