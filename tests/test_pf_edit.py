"""Correcting and removing EPF / PPF accounts (the PF tab's Edit / Delete)."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine import FinancialEngine
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


@pytest.fixture
def setup(tmp_path):
    database = Database(tmp_path / "pf.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Ray")
    raj = family_repo.create_family_member(family.family_id, "Raj", "self")
    asha = family_repo.create_family_member(family.family_id, "Asha", "spouse")
    engine = FinancialEngine(database)
    account = engine.create_pf_account(
        member_id=raj.member_id, account_type="EPF", account_number="UAN1",
        opened_date=date(2012, 7, 1), interest_rate="8.25", employee_contribution="1000000",
        employer_contribution="900000", balance="3000000", balance_date=date(2026, 3, 31),
    )
    return engine, family.family_id, raj.member_id, asha.member_id, account


def test_edit_every_detail(setup):
    engine, _, _, asha, account = setup
    edited = engine.edit_pf_account(
        asset_id=account.asset_id, member_id=asha, account_number="UAN-100", institution="EPFO",
        opened_date=date(2011, 4, 1), interest_rate="8.15", employee_contribution="1100000",
        employer_contribution="950000", balance="3200000", balance_date=date(2026, 3, 31),
    )
    assert (edited.member_id, edited.account_number, edited.institution, edited.opened_date) == (
        asha, "UAN-100", "EPFO", date(2011, 4, 1))
    assert edited.interest_rate == Decimal("8.15")
    assert edited.total_contributions == Decimal("2050000")
    assert edited.balance == Decimal("3200000")  # same date: the correction wins


def test_edit_needs_an_account_number(setup):
    engine, _, raj, _, account = setup
    with pytest.raises(ValueError, match="account number"):
        engine.edit_pf_account(
            asset_id=account.asset_id, member_id=raj, account_number=" ", opened_date=date(2012, 7, 1),
            interest_rate="8.25", employee_contribution="1", balance="1", balance_date=date(2026, 3, 31),
        )


def test_delete(setup):
    engine, family_id, _, _, account = setup
    engine.delete_pf_account(account.asset_id)
    assert engine.get_pf_portfolio(family_id).rows == ()
