"""Tests for mutual-fund, stock and FD asset detail records."""

from datetime import date
from decimal import Decimal

import pytest

from repository.asset_repository import (
    AssetRepository,
    FdHolding,
    MutualFundHolding,
    StockHolding,
)
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


@pytest.fixture
def asset_repo(tmp_path):
    database = Database(tmp_path / "holding.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    return AssetRepository(database), member.member_id


def test_create_mutual_fund_creates_parent_and_detail(asset_repo):
    repository, member_id = asset_repo

    asset, holding = repository.create_mutual_fund(
        member_id=member_id,
        name="Balanced Fund",
        institution="Example AMC",
        invested_amount=25000,
        purchase_date=date(2026, 1, 15),
        ownership=[(member_id, 100)],
        amc="Example AMC",
        scheme_name="Example Balanced Growth",
        folio_number="FOLIO-123",
        isin="INF000000001",
        current_nav=Decimal("125.50"),
        sip_enabled=True,
        sip_amount=2000,
        sip_date=10,
    )

    assert asset.category == "MF"
    assert holding == MutualFundHolding(
        asset_id=asset.asset_id,
        amc="Example AMC",
        scheme_name="Example Balanced Growth",
        folio_number="FOLIO-123",
        isin="INF000000001",
        units=Decimal("0"),
        avg_nav=None,
        current_nav=Decimal("125.5"),
        sip_enabled=True,
        sip_amount=Decimal("2000"),
        sip_date=10,
        sip_installments_done=None,
    )
    assert repository.get_mutual_fund(asset.asset_id) == holding


def test_create_stock_creates_parent_and_detail(asset_repo):
    repository, member_id = asset_repo

    asset, holding = repository.create_stock(
        member_id, "Example Ltd", "Example Broker", "EXAMPLE", "NSE",
        50000, date(2026, 2, 1), [(member_id, 100)], broker="Example Broker",
        current_price=250.75,
    )

    assert asset.category == "STOCK"
    assert holding == StockHolding(
        asset_id=asset.asset_id,
        broker="Example Broker",
        symbol="EXAMPLE",
        exchange="NSE",
        quantity=Decimal("0"),
        avg_buy_price=None,
        current_price=Decimal("250.75"),
    )
    assert repository.get_stock(asset.asset_id) == holding


def test_mutual_fund_detail_failure_rolls_back_parent(asset_repo):
    repository, member_id = asset_repo

    with pytest.raises(Exception):
        repository.create_mutual_fund(
            member_id, "Not MF", None, 1000, date.today(), [(member_id, 100)],
            amc="AMC", scheme_name="Scheme", folio_number="Folio", sip_date=32,
        )

    assert repository.list_assets(member_id) == []


def test_stock_rejects_invalid_exchange_and_leaves_no_parent(asset_repo):
    repository, member_id = asset_repo

    with pytest.raises(Exception):
        repository.create_stock(
            member_id, "Example Ltd", "Broker", "EXAMPLE", "OTC", 1000,
            date.today(), [(member_id, 100)],
        )

    assert repository.list_assets(member_id) == []


def test_create_fd_creates_parent_and_detail(asset_repo):
    repository, member_id = asset_repo

    asset, holding = repository.create_fd(
        member_id, "Example Bank FD", "Example Bank", Decimal("100000.00"),
        date(2026, 1, 1), [(member_id, 100)],
        fd_number="FD-001", bank_name="Example Bank",
        interest_rate=Decimal("7.00"), maturity_date=date(2027, 1, 1),
        payout_type="MATURITY", maturity_value=Decimal("107000.00"),
    )

    assert asset.category == "FD"
    assert asset.invested_amount == Decimal("100000")
    assert asset.purchase_date == date(2026, 1, 1)
    assert holding == FdHolding(
        asset_id=asset.asset_id,
        fd_number="FD-001",
        bank_name="Example Bank",
        principal=Decimal("100000"),
        interest_rate=Decimal("7"),
        issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1),
        payout_type="MATURITY",
        maturity_value=Decimal("107000"),
    )
    assert repository.get_fd(asset.asset_id) == holding


def test_create_fd_without_a_maturity_value_leaves_it_null(asset_repo):
    repository, member_id = asset_repo

    _, holding = repository.create_fd(
        member_id, "Example Bank FD", "Example Bank", 50000,
        date(2026, 1, 1), [(member_id, 100)],
        fd_number="FD-002", bank_name="Example Bank",
        interest_rate="6.5", maturity_date=date(2027, 7, 1),
        payout_type="QUARTERLY",
    )

    assert holding.maturity_value is None
    assert holding.payout_type == "QUARTERLY"


def test_get_fd_missing_returns_none(asset_repo):
    repository, _ = asset_repo
    assert repository.get_fd(999) is None


def test_fd_rejects_invalid_payout_type_and_leaves_no_parent(asset_repo):
    repository, member_id = asset_repo

    with pytest.raises(Exception):
        repository.create_fd(
            member_id, "Example Bank FD", "Example Bank", 1000,
            date.today(), [(member_id, 100)],
            fd_number="FD-003", bank_name="Example Bank",
            interest_rate=6, maturity_date=date(2027, 1, 1),
            payout_type="ANNUAL",  # not a valid payout_type
        )

    assert repository.list_assets(member_id) == []


def test_fd_rejects_maturity_date_not_after_issue_date(asset_repo):
    repository, member_id = asset_repo
    same_day = date(2026, 5, 1)

    with pytest.raises(Exception):
        repository.create_fd(
            member_id, "Example Bank FD", "Example Bank", 1000,
            same_day, [(member_id, 100)],
            fd_number="FD-004", bank_name="Example Bank",
            interest_rate=6, maturity_date=same_day,
            payout_type="MATURITY",
        )

    assert repository.list_assets(member_id) == []


def test_fd_ownership_rules_are_enforced_like_other_assets(asset_repo):
    repository, member_id = asset_repo

    with pytest.raises(ValueError, match="sum to 100"):
        repository.create_fd(
            member_id, "Example Bank FD", "Example Bank", 1000,
            date.today(), [(member_id, 60)],
            fd_number="FD-005", bank_name="Example Bank",
            interest_rate=6, maturity_date=date(2099, 1, 1),
            payout_type="MATURITY",
        )
    assert repository.list_assets(member_id) == []
