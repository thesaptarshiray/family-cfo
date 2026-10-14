"""
Every repository method accepts an optional `uow` and, when given one, must
run on that Unit of Work's connection instead of opening its own.

`test_unit_of_work.py` proves the transaction semantics and exercises the
methods a transfer needs. This file covers the remaining public methods, so
that each one is pinned down before the shared plumbing behind them is
refactored. The same two facts are checked for each:

  - read methods, called with the unit, see rows the unit has written but not
    yet committed; the same read without the unit (a separate connection)
    does not — which proves the unit's connection was really used;
  - write methods, called with the unit, are committed with it and rolled
    back with it.
"""

from datetime import date
from decimal import Decimal

import pytest

from repository.asset_repository import AssetRepository
from repository.bank_repository import BankRepository
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "participation.db")
    initialize_schema(db)
    return db


@pytest.fixture
def family_repo(database):
    return FamilyRepository(database)


@pytest.fixture
def bank_repo(database):
    return BankRepository(database)


@pytest.fixture
def asset_repo(database):
    return AssetRepository(database)


@pytest.fixture
def household(family_repo):
    """A committed family with one member, so tests can start from a known state."""
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    return family, member


def _count(database, table):
    conn = database.connect()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# FamilyRepository
# ----------------------------------------------------------------------


def test_family_member_reads_see_uncommitted_rows_inside_the_unit(
    database, family_repo, household
):
    family, _ = household

    with database.unit_of_work() as uow:
        member = family_repo.create_family_member(family.family_id, "Asha", "spouse", uow=uow)

        assert family_repo.get_family_member(member.member_id, uow=uow) == member
        assert member in family_repo.list_family_members(family.family_id, uow=uow)
        assert family_repo.list_family_members(
            family.family_id, active_only=True, uow=uow
        ) == family_repo.list_family_members(family.family_id, uow=uow)

        # A separate connection sees none of it yet.
        assert family_repo.get_family_member(member.member_id) is None
        assert member not in family_repo.list_family_members(family.family_id)

    assert family_repo.get_family_member(member.member_id) == member
    assert member in family_repo.list_family_members(family.family_id)


def test_update_family_is_rolled_back_with_the_unit(database, family_repo, household):
    family, _ = household

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            updated = family_repo.update_family(
                family.family_id, family_name="Renamed", base_currency="USD", uow=uow
            )
            assert updated.family_name == "Renamed"
            assert family_repo.get_family(family.family_id, uow=uow) == updated
            assert family_repo.get_family(family.family_id) == family  # other connection: unchanged
            raise RuntimeError("fail after the update")

    assert family_repo.get_family(family.family_id) == family


def test_update_family_is_committed_with_the_unit(database, family_repo, household):
    family, _ = household

    with database.unit_of_work() as uow:
        updated = family_repo.update_family(family.family_id, family_name="Renamed", uow=uow)
        assert family_repo.get_family(family.family_id) == family  # not committed yet

    assert updated.family_name == "Renamed"
    assert family_repo.get_family(family.family_id) == updated


def test_update_family_member_is_rolled_back_with_the_unit(database, family_repo, household):
    _, member = household

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            updated = family_repo.update_family_member(
                member.member_id, name="Rajesh", date_of_birth=date(1990, 5, 17),
                is_active=False, uow=uow,
            )
            assert (updated.name, updated.date_of_birth, updated.is_active) == (
                "Rajesh", date(1990, 5, 17), False,
            )
            assert family_repo.get_family_member(member.member_id, uow=uow) == updated
            assert family_repo.get_family_member(member.member_id) == member  # other connection
            raise RuntimeError("fail after the update")

    assert family_repo.get_family_member(member.member_id) == member


def test_update_family_member_is_committed_with_the_unit(database, family_repo, household):
    _, member = household

    with database.unit_of_work() as uow:
        updated = family_repo.update_family_member(
            member.member_id, name="Rajesh", is_active=False, uow=uow
        )
        assert family_repo.get_family_member(member.member_id) == member  # not committed yet

    assert family_repo.get_family_member(member.member_id) == updated
    assert updated.name == "Rajesh" and updated.is_active is False


# ----------------------------------------------------------------------
# BankRepository
# ----------------------------------------------------------------------


def test_bank_ownership_read_sees_uncommitted_rows_inside_the_unit(
    database, family_repo, bank_repo, household
):
    family, member = household
    spouse = family_repo.create_family_member(family.family_id, "Asha", "spouse")
    expected = [(member.member_id, Decimal("60")), (spouse.member_id, Decimal("40"))]

    with database.unit_of_work() as uow:
        account = bank_repo.create_account(
            member.member_id, "Example Bank", "JOINT", "7777",
            ownership=[(spouse.member_id, 40), (member.member_id, 60)], uow=uow,
        )
        assert bank_repo.list_ownership(account.account_id, uow=uow) == expected
        assert bank_repo.list_ownership(account.account_id) == []  # not visible outside

    assert bank_repo.list_ownership(account.account_id) == expected


# ----------------------------------------------------------------------
# AssetRepository
# ----------------------------------------------------------------------


def test_create_asset_and_asset_reads_join_the_unit_and_roll_back(
    database, asset_repo, household
):
    _, member = household

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            asset = asset_repo.create_asset(
                member.member_id, "GOLD", "Gold coins", None, 10000, date(2025, 1, 1),
                [(member.member_id, 100)], uow=uow,
            )
            assert asset_repo.get_asset(asset.asset_id, uow=uow) == asset
            assert asset_repo.list_assets(member.member_id, uow=uow) == [asset]
            assert asset_repo.list_ownership(asset.asset_id, uow=uow) == [
                (member.member_id, Decimal("100"))
            ]

            # A separate connection sees none of it.
            assert asset_repo.get_asset(asset.asset_id) is None
            assert asset_repo.list_assets(member.member_id) == []
            assert asset_repo.list_ownership(asset.asset_id) == []
            raise RuntimeError("fail after the asset was written")

    assert asset_repo.list_assets(member.member_id) == []
    assert _count(database, "asset") == 0
    assert _count(database, "asset_ownership") == 0


def test_create_asset_is_committed_with_the_unit(database, asset_repo, household):
    _, member = household

    with database.unit_of_work() as uow:
        asset = asset_repo.create_asset(
            member.member_id, "FD", "Fixed Deposit", "Example Bank", 50000,
            date(2025, 2, 1), [(member.member_id, 100)], uow=uow,
        )
        assert asset_repo.get_asset(asset.asset_id) is None  # not committed yet

    assert asset_repo.get_asset(asset.asset_id) == asset
    assert asset_repo.list_ownership(asset.asset_id) == [(member.member_id, Decimal("100"))]


def test_create_stock_and_get_stock_join_the_unit(database, asset_repo, household):
    _, member = household

    with pytest.raises(RuntimeError):
        with database.unit_of_work() as uow:
            asset, holding = asset_repo.create_stock(
                member.member_id, "Example Ltd", "Example Broker", "EXAMPLE", "NSE",
                50000, date(2026, 2, 1), [(member.member_id, 100)],
                broker="Example Broker", current_price=250.75, uow=uow,
            )
            assert asset_repo.get_stock(asset.asset_id, uow=uow) == holding
            assert asset_repo.get_stock(asset.asset_id) is None  # separate connection
            raise RuntimeError("fail after the stock was written")

    assert _count(database, "asset") == 0
    assert _count(database, "stock_holding") == 0

    with database.unit_of_work() as uow:
        asset, holding = asset_repo.create_stock(
            member.member_id, "Example Ltd", "Example Broker", "EXAMPLE", "NSE",
            50000, date(2026, 2, 1), [(member.member_id, 100)],
            broker="Example Broker", current_price=250.75, uow=uow,
        )
    assert asset_repo.get_stock(asset.asset_id) == holding


def test_mutual_fund_reads_see_uncommitted_rows_inside_the_unit(
    database, asset_repo, household
):
    _, member = household

    with database.unit_of_work() as uow:
        asset, holding = asset_repo.create_mutual_fund(
            member.member_id, "Balanced Fund", None, 25000, date(2026, 1, 15),
            [(member.member_id, 100)],
            amc="Example AMC", scheme_name="Balanced Growth", folio_number="F-1", uow=uow,
        )
        assert asset_repo.get_mutual_fund(asset.asset_id, uow=uow) == holding
        assert asset_repo.get_asset(asset.asset_id, uow=uow) == asset
        assert asset_repo.list_assets(member.member_id, uow=uow) == [asset]
        assert asset_repo.get_mutual_fund(asset.asset_id) is None  # separate connection

    assert asset_repo.get_mutual_fund(asset.asset_id) == holding
