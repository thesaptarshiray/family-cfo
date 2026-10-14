"""Loans: the amortization schedule (`loan_schedule`) and the Engine's loan
features. The reference loan is the construction-linked home loan from the
spreadsheet the Loans tab replaced; its figures are copied from that sheet."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine import FinancialEngine
from financial_engine.loan_schedule import (
    EMI,
    PRE_EMI,
    compute_emi,
    compute_loan_schedule,
    month_no_of,
    summarize_by_financial_year,
)
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema
from test_api_server import _family_with_member, _get, _post, server  # noqa: F401 (fixture)

D = Decimal
START = date(2025, 8, 1)
DISBURSEMENTS = [
    (START, D("4681745")),
    (date(2025, 11, 1), D("1213785")),
    (date(2026, 3, 1), D("1040000")),
    (date(2026, 5, 1), D("1040000")),
    (date(2026, 7, 1), D("1040000")),
]
PREPAYMENTS = [
    (date(2025, 8, 1), D("5000")),
    (date(2025, 9, 1), D("5000")),
    (date(2025, 10, 1), D("10000")),
    (date(2025, 11, 1), D("2000000")),
    (date(2026, 8, 1), D("400000")),
]
SHEET_EMI = D("96281.98922209132")  # the sheet's unrounded PMT


def _sheet_schedule(**overrides):
    kwargs = dict(
        start_date=START, tenure_months=360, pre_emi_months=24, initial_rate=D("7.6"),
        disbursements=DISBURSEMENTS, prepayments=PREPAYMENTS, emi=SHEET_EMI,
    )
    kwargs.update(overrides)
    return compute_loan_schedule(**kwargs)


# ----------------------------------------------------------------------
# The schedule
# ----------------------------------------------------------------------


def test_schedule_matches_the_spreadsheet():
    rows = _sheet_schedule().rows
    # (month, opening, interest, closing) straight from the sheet
    for month, opening, interest, closing in [
        (1, "0", "29651.05", "4676745"),
        (4, "4661745", "29524.39", "3875530"),
        (13, "6995530", "44305.02", "6595530"),
        (25, "6595530", "41771.69", "6541019.70"),
        (26, "6541019.70", "41426.46", "6486164.17"),
        (114, "106081.31", "671.85", "10471.17"),
        (115, "10471.17", "66.32", "0"),
    ]:
        row = rows[month - 1]
        assert row.opening == D(opening), month
        assert row.interest == D(interest), month
        assert row.closing == D(closing), month
    assert len(rows) == 115  # repaid in month 115 (Feb 2035)
    assert rows[-1].payment == D("10537.49")  # the smaller last EMI


def test_pre_emi_months_pay_interest_only():
    rows = _sheet_schedule().rows
    assert all(r.phase == PRE_EMI for r in rows[:24])
    assert all(r.principal_repaid == 0 and r.payment == r.interest for r in rows[:24])
    assert rows[24].phase == EMI


def test_emi_is_worked_out_on_the_principal_at_emi_start_when_not_given():
    schedule = _sheet_schedule(emi=None)
    assert schedule.emi_calculated
    assert schedule.emi == compute_emi(D("6595530"), D("7.6"), 336)
    assert len(schedule.rows) == 360  # runs the full tenure
    assert schedule.paid_off


def test_compute_emi_standard_values():
    # 10 lakh at 12% for 12 months: textbook EMI 88,848.79 -> rounded up
    assert compute_emi(D("1000000"), D("12"), 12) == D("88849")
    assert compute_emi(D("120000"), D("0"), 12) == D("10000")


def test_a_rate_change_keeps_the_emi_and_moves_the_payoff():
    base = _sheet_schedule()
    cut = _sheet_schedule(rate_changes=[(date(2027, 1, 1), D("7.25"))])
    assert cut.emi == base.emi
    assert len(cut.rows) < len(base.rows)
    assert cut.rows[month_no_of(START, date(2027, 1, 1)) - 1].rate == D("7.25")


def test_a_car_loan_without_moratorium_amortises_from_month_one():
    schedule = compute_loan_schedule(
        start_date=date(2026, 1, 15), tenure_months=60, pre_emi_months=0,
        initial_rate=D("9"), disbursements=[(date(2026, 1, 15), D("800000"))],
    )
    assert len(schedule.rows) == 60
    assert schedule.rows[0].phase == EMI and schedule.rows[0].principal_repaid > 0
    assert schedule.rows[-1].closing == 0


def test_prepayment_larger_than_the_balance_only_takes_what_is_owed():
    schedule = compute_loan_schedule(
        start_date=START, tenure_months=12, pre_emi_months=0, initial_rate=D("10"),
        disbursements=[(START, D("100000"))], prepayments=[(date(2025, 10, 1), D("999999"))],
    )
    assert len(schedule.rows) == 3
    assert schedule.rows[-1].closing == 0
    assert schedule.rows[-1].prepaid < D("100000")


def test_financial_years_run_april_to_march():
    years = summarize_by_financial_year(_sheet_schedule().rows)
    assert years[0].label == "FY 2025-26"  # Aug 2025 - Mar 2026
    assert years[0].interest == sum(r.interest for r in _sheet_schedule().rows[:8])
    assert years[1].label == "FY 2026-27"


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------


@pytest.fixture
def engine_and_member(tmp_path):
    database = Database(tmp_path / "loans.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Ray")
    member = family_repo.create_family_member(family.family_id, "Saptarshi", "self")
    return FinancialEngine(database), family.family_id, member.member_id


def _home_loan(engine, member_id, **overrides):
    kwargs = dict(
        member_id=member_id, loan_type="HOME_LOAN", lender="SBI", sanctioned_amount="15800000",
        interest_rate="7.6", tenure_months=360, start_date=START, pre_emi_months=24,
        first_disbursement="4681745", down_payment="1700000",
    )
    kwargs.update(overrides)
    loan = engine.create_loan(**kwargs)
    for on, amount in DISBURSEMENTS[1:]:
        engine.add_loan_disbursement(liability_id=loan.liability_id, disbursement_date=on, amount=amount)
    for on, amount in PREPAYMENTS:
        loan = engine.add_loan_prepayment(liability_id=loan.liability_id, prepayment_date=on, amount=amount)
    return loan


def test_home_loan_summary_as_of_a_month(engine_and_member):
    engine, family_id, member_id = engine_and_member
    _home_loan(engine, member_id, emi_amount="96282")
    portfolio = engine.get_loan_portfolio(family_id, today=date(2026, 9, 30))
    loan = portfolio.rows[0].loan
    assert loan.phase == PRE_EMI and loan.current_month_no == 14
    assert loan.outstanding == D("6595530")
    assert loan.this_month_payment == D("41771.69")
    assert loan.undisbursed == D("6784470")
    assert loan.prepaid_to_date == D("2420000")
    assert loan.emi == D("96282") and loan.emi_entered
    assert loan.payoff_month == date(2035, 2, 1)
    assert loan.interest_saved_by_prepayments > 0 and loan.months_saved_by_prepayments > 0
    assert portfolio.total_outstanding == D("6595530")
    assert portfolio.this_month_payment == D("41771.69")


def test_disbursements_cannot_exceed_the_sanctioned_amount(engine_and_member):
    engine, _, member_id = engine_and_member
    loan = engine.create_loan(
        member_id=member_id, loan_type="HOME_LOAN", lender="HDFC", sanctioned_amount="1000000",
        interest_rate="8", tenure_months=240, start_date=START, first_disbursement="600000",
    )
    with pytest.raises(ValueError, match="sanctioned"):
        engine.add_loan_disbursement(
            liability_id=loan.liability_id, disbursement_date=date(2026, 1, 1), amount="500000"
        )


def test_events_before_the_loan_starts_are_rejected(engine_and_member):
    engine, _, member_id = engine_and_member
    loan = engine.create_loan(
        member_id=member_id, loan_type="CAR_LOAN", lender="ICICI", sanctioned_amount="800000",
        interest_rate="9", tenure_months=60, start_date=START,
    )
    with pytest.raises(ValueError, match="first month"):
        engine.add_loan_prepayment(
            liability_id=loan.liability_id, prepayment_date=date(2025, 7, 1), amount="1000"
        )


@pytest.mark.parametrize("field, value", [
    ("loan_type", "CREDIT_CARD"), ("sanctioned_amount", "0"), ("tenure_months", 0),
    ("pre_emi_months", 360), ("interest_rate", "120"), ("lender", " "),
])
def test_invalid_loan_terms_are_rejected(engine_and_member, field, value):
    engine, _, member_id = engine_and_member
    kwargs = dict(
        member_id=member_id, loan_type="PERSONAL_LOAN", lender="Axis", sanctioned_amount="500000",
        interest_rate="11", tenure_months=360, start_date=START,
    )
    kwargs[field] = value
    with pytest.raises(ValueError):
        engine.create_loan(**kwargs)


def test_rate_change_records_the_previous_rate_and_stays_in_step(engine_and_member):
    engine, _, member_id = engine_and_member
    loan = _home_loan(engine, member_id)
    first = engine.add_loan_rate_change(
        liability_id=loan.liability_id, effective_date=date(2027, 1, 1), new_rate="7.25"
    ).rate_changes[0]
    loan = engine.add_loan_rate_change(
        liability_id=loan.liability_id, effective_date=date(2026, 6, 1), new_rate="8.0"
    )
    assert [(c.old_rate, c.new_rate) for c in loan.rate_changes] == [
        (D("7.6"), D("8.0")), (D("8.0"), D("7.25")),
    ]
    engine.delete_loan_rate_change(loan.rate_changes[0].rate_change_id)
    remaining = engine.list_loans(member_id)[0].rate_changes
    assert [(c.rate_change_id, c.old_rate) for c in remaining] == [(first.rate_change_id, D("7.6"))]


def test_what_if_prepayment_saves_interest_and_saves_nothing(engine_and_member):
    engine, _, member_id = engine_and_member
    loan = _home_loan(engine, member_id, emi_amount="96282")
    result = engine.loan_what_if(
        liability_id=loan.liability_id, amount="500000", on_date=date(2027, 9, 1)
    )
    assert result.interest_saved > 0 and result.months_saved > 0
    assert result.payoff_after < result.payoff_before
    assert engine.list_loans(member_id)[0].total_interest == result.total_interest_before


def test_loan_payments_show_on_the_monthly_cash_flow(engine_and_member):
    engine, family_id, member_id = engine_and_member
    _home_loan(engine, member_id)
    august = engine.get_monthly_cash_flow(family_id, 2026, 8)
    loan_lines = [line for line in august.expenses if line.source.startswith("LOAN")]
    assert [(line.source, line.monthly_amount) for line in loan_lines] == [
        ("LOAN_EMI", D("44305.02")), ("LOAN_PREPAYMENT", D("400000")),
    ]


def test_net_worth_subtracts_loans_outstanding(engine_and_member):
    engine, family_id, member_id = engine_and_member
    engine.create_loan(
        member_id=member_id, loan_type="PERSONAL_LOAN", lender="Axis", sanctioned_amount="300000",
        interest_rate="12", tenure_months=36, start_date=date.today(),
    )
    allocation = engine.get_asset_allocation(family_id)
    assert allocation.total_loans_outstanding > 0
    assert allocation.net_worth == allocation.total_current_value - allocation.total_loans_outstanding


def test_closed_loan_owes_nothing_and_leaves_the_totals(engine_and_member):
    engine, family_id, member_id = engine_and_member
    loan = _home_loan(engine, member_id)
    engine.update_loan(
        liability_id=loan.liability_id, member_id=member_id, loan_type="HOME_LOAN",
        lender="SBI", sanctioned_amount="15800000", interest_rate="7.6", tenure_months=360,
        start_date=START, pre_emi_months=24, status="CLOSED",
    )
    portfolio = engine.get_loan_portfolio(family_id)
    assert portfolio.rows[0].loan.outstanding == 0
    assert portfolio.total_outstanding == 0


def test_delete_loan_removes_it_with_its_events(engine_and_member):
    engine, family_id, member_id = engine_and_member
    loan = _home_loan(engine, member_id)
    engine.add_loan_rate_change(
        liability_id=loan.liability_id, effective_date=date(2027, 1, 1), new_rate="7.25"
    )
    engine.delete_loan(loan.liability_id)
    assert engine.get_loan_portfolio(family_id).rows == ()


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


def test_loan_routes_over_http(server):
    family_id, member_id = _family_with_member(server)
    status, body = _post(server, f"/api/members/{member_id}/loans", {
        "loan_type": "HOME_LOAN", "lender": "SBI", "sanctioned_amount": "15800000",
        "interest_rate": "7.6", "tenure_months": "360", "start_date": "2025-08-01",
        "pre_emi_months": "24", "first_disbursement": "4681745",
    })
    assert status == 200, body
    loan_id = body["loan"]["liability_id"]
    status, body = _post(server, f"/api/loans/{loan_id}/disbursements",
                         {"date": "2025-11-01", "amount": "1213785"})
    assert status == 200, body
    status, body = _post(server, f"/api/loans/{loan_id}/prepayments",
                         {"date": "2025-11-01", "amount": "2000000"})
    assert status == 200, body
    prepayment_id = body["loan"]["prepayments"][0]["prepayment_id"]
    status, body = _post(server, f"/api/loans/{loan_id}/what-if",
                         {"date": "2027-09-01", "amount": "500000"})
    assert status == 200 and Decimal(body["what_if"]["interest_saved"]) > 0
    status, body = _get(server, f"/api/families/{family_id}/loans")
    assert status == 200 and len(body["rows"]) == 1
    assert body["rows"][0]["loan"]["schedule"][0]["month"] == "2025-08-01"
    status, _ = _post(server, f"/api/loan-prepayments/{prepayment_id}/delete", {})
    assert status == 200
    status, body = _post(server, f"/api/loans/{loan_id}/disbursements",
                         {"date": "2025-12-01", "amount": "99999999"})
    assert status == 400 and "sanctioned" in body["error"]
    status, _ = _post(server, f"/api/loans/{loan_id}/delete", {})
    assert status == 200
