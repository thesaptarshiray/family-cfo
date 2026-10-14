"""Financial health check-up: the rules (`financial_engine.health`) and the
Engine's gathering of a family's figures into them."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine import FinancialEngine
from financial_engine.health import (
    ATTENTION, GOOD, MISSING, FundInput, HealthInputs, LoanInput, assess, fund_kind, is_regular_plan,
)
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema
from test_api_server import _family_with_member, _get, server  # noqa: F401 (fixture)

D = Decimal


def _inputs(**overrides):
    base = dict(
        as_of=date(2026, 10, 1), member_count=2, ages=(), risk_tolerance=None,
        monthly_income=D("200000"), monthly_expenses=D("80000"), monthly_emi=D("40000"),
        monthly_investing=D("30000"), expense_rows=8, annual_premiums=D("0"), cash=D("300000"),
        funds=(FundInput("Some Flexi Cap Fund - Direct Growth", D("2000000"), "EQUITY", False),),
        stocks_value=D("500000"), largest_stock="INFY", largest_stock_pct=D("12"),
        fd_value=D("1500000"), fd_avg_rate=D("7"), epf_ppf_value=D("1000000"),
        nps_equity=D("0"), nps_debt=D("0"), gold_value=D("400000"), policy_savings_value=D("0"),
        savings_policy_count=0, term_cover=D("50000000"), health_policies=1, loans=(),
    )
    base.update(overrides)
    return HealthInputs(**base)


def _check(health, key):
    return next(c for c in health.checks if c.key == key)


def test_a_well_set_up_family_scores_well():
    h = assess(_inputs())
    assert h.score >= 75
    assert _check(h, "life").status == GOOD and _check(h, "health").status == GOOD
    assert h.savings_rate_pct == D("40.0")
    assert h.net_worth == D("5700000")


def test_no_cover_comes_first_in_the_plan():
    h = assess(_inputs(term_cover=D("0"), health_policies=0))
    assert _check(h, "life").status == ATTENTION
    assert [a.priority for a in h.actions][:2] == [1, 1]
    assert "term" in h.actions[0].title.lower() or "health" in h.actions[0].title.lower()


def test_fd_heavy_portfolio_gets_a_move_to_equity_suggestion_with_an_estimate():
    h = assess(_inputs(funds=(), stocks_value=D("100000"), fd_value=D("9000000")))
    assert _check(h, "mix").status == ATTENTION
    move = next(a for a in h.actions if "equity" in a.title.lower())
    assert move.yearly_impact > 0
    assert h.projection[-1].improved > h.projection[-1].as_is


def test_regular_plans_are_spotted_and_costed():
    h = assess(_inputs(funds=(FundInput("DSP Large Cap Fund - Regular Plan - Growth", D("1000000"), "EQUITY", True),)))
    action = next(a for a in h.actions if "direct" in a.title.lower())
    assert action.yearly_impact == D("8000")  # 0.8% of ₹10 L


def test_missing_income_is_reported_not_guessed():
    h = assess(_inputs(monthly_income=D("0"), monthly_expenses=D("0"), monthly_emi=D("0")))
    assert _check(h, "savings").status == MISSING and _check(h, "emergency").status == MISSING
    assert any("income" in g.lower() for g in h.data_gaps)


def test_age_sets_the_equity_target():
    assert assess(_inputs(ages=(30,))).equity_target_pct == D("70")
    assert assess(_inputs(ages=(80,))).equity_target_pct == D("30")
    assert assess(_inputs(risk_tolerance="CONSERVATIVE")).equity_target_pct == D("35")


def test_loan_prepayment_suggestion_carries_the_engines_numbers():
    loan = LoanInput("home loan", D("6000000"), D("8.5"), D("50000"), True,
                     prepay_amount=D("1000000"), prepay_interest_saved=D("1800000"), prepay_months_saved=40,
                     balance_by_year=tuple(D("6000000") - D("300000") * y for y in range(21)))
    h = assess(_inputs(loans=(loan,)))
    action = next(a for a in h.actions if "prepay" in a.title.lower())
    assert "₹18.0 L" in action.steps[0] and "40 months" in action.steps[0]


@pytest.mark.parametrize("name, kind", [
    ("Parag Parikh Flexi Cap Fund - Regular Plan Growth", "EQUITY"),
    ("HDFC Balanced Advantage Fund - Regular Plan - Growth", "HYBRID"),
    ("ICICI Prudential Liquid Fund", "DEBT"),
    ("Nippon India ETF Gold BeES", "GOLD"),
])
def test_fund_kind_from_name(name, kind):
    assert fund_kind(name) == kind


def test_regular_plan_detection():
    assert is_regular_plan("Kotak Mid Cap Fund Regular Growth")
    assert not is_regular_plan("Kotak Mid Cap Fund - Direct Plan - Growth")
    assert not is_regular_plan("UTI Nifty 50 Index Fund")  # plan not named: not assumed regular


# ----------------------------------------------------------------------
# Engine + API
# ----------------------------------------------------------------------


def test_engine_builds_the_check_up_from_recorded_data(tmp_path):
    database = Database(tmp_path / "h.db")
    initialize_schema(database)
    repo = FamilyRepository(database)
    family = repo.create_family("Ray")
    member = repo.create_family_member(family.family_id, "Raj", "self")
    engine = FinancialEngine(database)
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="INCOME", name="Salary", amount="200000")
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", name="Rent", amount="40000")
    engine.create_fd(member_id=member.member_id, bank_name="SBI", fd_number="1", principal="5000000",
                     interest_rate="7", issue_date=date(2025, 1, 1), maturity_date=date(2028, 1, 1),
                     payout_type="MATURITY")
    h = engine.get_financial_health(family.family_id)
    assert h.monthly_income == D("200000") and h.total_loans == 0
    assert _check(h, "mix").status == ATTENTION  # all FD, no equity
    assert len(h.projection) == 21


def test_health_route(server):
    family_id, _ = _family_with_member(server)
    status, body = _get(server, f"/api/families/{family_id}/health")
    assert status == 200 and "score" in body and len(body["projection"]) == 21
