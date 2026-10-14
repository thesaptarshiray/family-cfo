"""Tests for MF unit-acquisition transaction persistence."""

from datetime import date
from decimal import Decimal

import pytest

from repository.asset_repository import AssetRepository
from repository.database import Database
from repository.errors import RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.mf_transaction_repository import MfTransaction, MfTransactionRepository
from repository.schema import initialize_schema


@pytest.fixture
def txn_repo(tmp_path):
    database = Database(tmp_path / "mf_txn.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    asset_repo = AssetRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    asset, _ = asset_repo.create_mutual_fund(
        member.member_id, "Balanced Fund", "Example AMC", 0, date(2026, 1, 1),
        [(member.member_id, 100)],
        amc="Example AMC", scheme_name="Balanced Growth", folio_number="FOLIO-1",
    )
    return MfTransactionRepository(database), asset.asset_id


def test_create_transaction_returns_the_stored_row(txn_repo):
    repository, asset_id = txn_repo

    txn = repository.create_transaction(
        asset_id, date(2026, 1, 15), "SIP", Decimal("10.5"), Decimal("95.2381"),
        Decimal("1000.00"), description="January SIP",
    )

    assert txn == MfTransaction(
        mf_txn_id=1, asset_id=asset_id, txn_date=date(2026, 1, 15), txn_type="SIP",
        units=Decimal("10.5"), nav=Decimal("95.2381"), amount=Decimal("1000.00"),
        status="ACTIVE", description="January SIP",
    )
    assert repository.get_transaction(txn.mf_txn_id) == txn


def test_create_lumpsum_transaction(txn_repo):
    repository, asset_id = txn_repo

    txn = repository.create_transaction(
        asset_id, date(2026, 2, 1), "LUMPSUM", 100, "100.00", 10000,
    )

    assert txn.txn_type == "LUMPSUM"
    assert txn.description is None


def test_get_transaction_missing_returns_none(txn_repo):
    repository, _ = txn_repo
    assert repository.get_transaction(999) is None


def test_list_transactions_returns_only_that_assets_transactions_oldest_first(txn_repo):
    repository, asset_id = txn_repo
    other_asset_repo = AssetRepository(repository._db)
    family_repo = FamilyRepository(repository._db)
    member_id = family_repo.get_family_member(
        other_asset_repo.get_asset(asset_id).member_id
    ).member_id
    other_asset, _ = other_asset_repo.create_mutual_fund(
        member_id, "Other Fund", None, 0, date(2026, 1, 1), [(member_id, 100)],
        amc="Other AMC", scheme_name="Other Scheme", folio_number="FOLIO-2",
    )

    second = repository.create_transaction(asset_id, date(2026, 2, 15), "SIP", 10, 100, 1000)
    first = repository.create_transaction(asset_id, date(2026, 1, 15), "SIP", 10, 95, 950)
    repository.create_transaction(other_asset.asset_id, date(2026, 1, 20), "SIP", 5, 50, 250)

    assert repository.list_transactions(asset_id) == [first, second]


def test_list_transactions_for_unknown_asset_is_empty(txn_repo):
    repository, _ = txn_repo
    assert repository.list_transactions(999) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"txn_type": "REDEMPTION"},  # not a valid txn_type for this table
        {"units": 0},  # CHECK units > 0
        {"nav": 0},  # CHECK nav > 0
        {"amount": 0},  # CHECK amount > 0
    ],
)
def test_schema_constraint_violations_are_translated(txn_repo, overrides):
    repository, asset_id = txn_repo
    fields = dict(txn_type="SIP", units=10, nav=100, amount=1000)
    fields.update(overrides)

    with pytest.raises(RepositoryIntegrityError):
        repository.create_transaction(asset_id, date(2026, 1, 1), **fields)
    assert repository.list_transactions(asset_id) == []


def test_transaction_for_nonexistent_asset_is_rejected(txn_repo):
    repository, _ = txn_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.create_transaction(999, date(2026, 1, 1), "SIP", 10, 100, 1000)
