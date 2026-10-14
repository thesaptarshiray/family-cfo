"""Tests for dated market-value history persistence."""

from datetime import date
from decimal import Decimal

import pytest

from repository.asset_repository import AssetRepository
from repository.asset_valuation_repository import AssetValuation, AssetValuationRepository
from repository.database import Database
from repository.errors import RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


@pytest.fixture
def valuation_repo(tmp_path):
    database = Database(tmp_path / "valuation.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    asset_repo = AssetRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    asset, _ = asset_repo.create_mutual_fund(
        member.member_id, "Balanced Fund", None, 0, date(2026, 1, 1),
        [(member.member_id, 100)],
        amc="Example AMC", scheme_name="Balanced Growth", folio_number="FOLIO-1",
    )
    return AssetValuationRepository(database), asset.asset_id


def test_record_valuation_returns_the_stored_row(valuation_repo):
    repository, asset_id = valuation_repo

    valuation = repository.record_valuation(
        asset_id, date(2026, 3, 1), Decimal("102.5000"), "AMFI"
    )

    assert valuation == AssetValuation(
        valuation_id=1, asset_id=asset_id, valuation_date=date(2026, 3, 1),
        market_value=Decimal("102.5"), source="AMFI",
        data_confidence="ACTUAL", is_correction=False,
    )


def test_get_latest_valuation_returns_the_most_recent_by_date(valuation_repo):
    repository, asset_id = valuation_repo
    repository.record_valuation(asset_id, date(2026, 1, 1), 100, "MANUAL")
    latest = repository.record_valuation(asset_id, date(2026, 3, 1), 105, "AMFI")
    repository.record_valuation(asset_id, date(2026, 2, 1), 102, "AMFI")

    assert repository.get_latest_valuation(asset_id) == latest


def test_get_latest_valuation_with_none_recorded_is_none(valuation_repo):
    repository, asset_id = valuation_repo
    assert repository.get_latest_valuation(asset_id) is None


def test_a_same_day_correction_entered_after_the_original_wins(valuation_repo):
    repository, asset_id = valuation_repo
    repository.record_valuation(asset_id, date(2026, 3, 1), 100, "AMFI")
    correction = repository.record_valuation(
        asset_id, date(2026, 3, 1), Decimal("101"), "MANUAL", is_correction=True
    )

    assert repository.get_latest_valuation(asset_id) == correction


def test_list_valuations_returns_history_oldest_first(valuation_repo):
    repository, asset_id = valuation_repo
    first = repository.record_valuation(asset_id, date(2026, 1, 1), 100, "MANUAL")
    second = repository.record_valuation(asset_id, date(2026, 2, 1), 102, "AMFI")

    assert repository.list_valuations(asset_id) == [first, second]


def test_a_second_noncorrected_valuation_on_the_same_date_is_rejected(valuation_repo):
    repository, asset_id = valuation_repo
    repository.record_valuation(asset_id, date(2026, 3, 1), 100, "AMFI")

    with pytest.raises(RepositoryIntegrityError):
        repository.record_valuation(asset_id, date(2026, 3, 1), 101, "AMFI")


def test_record_valuation_rejects_invalid_source(valuation_repo):
    repository, asset_id = valuation_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.record_valuation(asset_id, date(2026, 1, 1), 100, "GOOGLE_FINANCE")


def test_record_valuation_for_nonexistent_asset_is_rejected(valuation_repo):
    repository, _ = valuation_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.record_valuation(999, date(2026, 1, 1), 100, "MANUAL")
