"""Tests for generic investment assets and ownership shares."""

from datetime import date
from decimal import Decimal

import pytest

from repository.asset_repository import Asset, AssetRepository
from repository.database import Database
from repository.errors import RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


@pytest.fixture
def asset_repo(tmp_path):
    database = Database(tmp_path / "asset.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    return AssetRepository(database), member.member_id


def test_create_asset_uses_engine_cache_default(asset_repo):
    repository, member_id = asset_repo

    asset = repository.create_asset(
        member_id=member_id,
        category="MF",
        name="Balanced Fund",
        institution="Example AMC",
        invested_amount=Decimal("25000.00"),
        purchase_date=date(2026, 1, 15),
        ownership=[(member_id, Decimal("100"))],
    )

    assert asset == Asset(
        asset_id=1,
        member_id=member_id,
        category="MF",
        name="Balanced Fund",
        institution="Example AMC",
        invested_amount=Decimal("25000"),
        current_value=Decimal("0"),
        purchase_date=date(2026, 1, 15),
        status="ACTIVE",
    )


def test_get_and_list_assets(asset_repo):
    repository, member_id = asset_repo
    first = repository.create_asset(
        member_id, "GOLD", "Gold", None, 10000, date(2025, 1, 1),
        ownership=[(member_id, 100)],
    )
    second = repository.create_asset(
        member_id, "FD", "Fixed Deposit", "Example Bank", 50000, date(2025, 2, 1),
        ownership=[(member_id, 100)],
    )

    assert repository.get_asset(first.asset_id) == first
    assert repository.list_assets(member_id) == [first, second]


def test_asset_ownership_is_read_in_member_order(asset_repo):
    repository, primary_member_id = asset_repo
    family_repo = FamilyRepository(repository._db)
    family = family_repo.get_family_member(primary_member_id).family_id
    second_member = family_repo.create_family_member(family, "Asha", "spouse")

    asset = repository.create_asset(
        primary_member_id,
        "PROPERTY",
        "Home",
        None,
        1000000,
        date(2024, 6, 1),
        ownership=[(second_member.member_id, 40), (primary_member_id, 60)],
    )

    assert repository.list_ownership(asset.asset_id) == [
        (primary_member_id, Decimal("60")),
        (second_member.member_id, Decimal("40")),
    ]


def test_asset_ownership_must_sum_to_100(asset_repo):
    repository, member_id = asset_repo

    with pytest.raises(ValueError, match="sum to 100"):
        repository.create_asset(
            member_id, "STOCK", "Shares", "Example Broker", 1000, date.today(),
            ownership=[(member_id, 90)],
        )

    assert repository.list_assets(member_id) == []


def test_asset_requires_primary_holder_ownership(asset_repo):
    repository, member_id = asset_repo

    with pytest.raises(ValueError, match="primary holder"):
        repository.create_asset(
            member_id, "FD", "Deposit", "Example Bank", 1000, date.today(),
            ownership=[(999, 100)],
        )


def test_nonexistent_asset_owner_is_rejected(asset_repo):
    repository, member_id = asset_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.create_asset(
            member_id, "MF", "Fund", "Example AMC", 1000, date.today(),
            ownership=[(member_id, 50), (999, 50)],
        )

    assert repository.list_assets(member_id) == []
