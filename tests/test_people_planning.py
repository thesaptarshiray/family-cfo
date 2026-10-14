"""Planning person by person: member profiles (age, stage of life, heir,
retirement age), the hand-over checklist, how Financial Health uses them,
and the insurance policy review."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine import FinancialEngine
from financial_engine.health import PersonInput, assess
from financial_engine.policy_review import review_policy
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema
from test_api_server import _family_with_member, _get, _post, server  # noqa: F401 (fixture)
from test_health import _inputs

D = Decimal


@pytest.fixture
def family(tmp_path):
    database = Database(tmp_path / "people.db")
    initialize_schema(database)
    repo = FamilyRepository(database)
    fam = repo.create_family("Ray")
    me = repo.create_family_member(fam.family_id, "Saptarshi", "Self")
    mother = repo.create_family_member(fam.family_id, "Ratna Ray", "Mother")
    son = repo.create_family_member(fam.family_id, "Saptajeet Ray", "Son")
    return FinancialEngine(database), fam.family_id, me.member_id, mother.member_id, son.member_id


def _profile(engine, member_id, name, relationship, **kw):
    args = dict(date_of_birth=None, life_stage=None, heir_member_id=None, retirement_age=None)
    args.update(kw)
    return engine.update_member_profile(member_id=member_id, name=name, relationship=relationship, **args)


def test_member_profile_is_saved(family):
    engine, _, me, mother, _ = family
    m = _profile(engine, mother, "Ratna Ray", "Mother", date_of_birth=date(1958, 6, 1),
                 life_stage="RETIRED", heir_member_id=me)
    assert (m.date_of_birth, m.life_stage, m.heir_member_id) == (date(1958, 6, 1), "RETIRED", me)


@pytest.mark.parametrize("kw, message", [
    (dict(life_stage="STUDENT"), "earning"),
    (dict(retirement_age=90), "40-80"),
    (dict(date_of_birth=date(2999, 1, 1)), "future"),
])
def test_member_profile_is_checked(family, kw, message):
    engine, _, me, _, _ = family
    with pytest.raises(ValueError, match=message):
        _profile(engine, me, "Saptarshi", "Self", **kw)


def test_heir_must_be_someone_else_in_the_family(family, tmp_path):
    engine, _, me, _, _ = family
    with pytest.raises(ValueError, match="own heir"):
        _profile(engine, me, "Saptarshi", "Self", heir_member_id=me)
    other = engine.create_family_member(engine.create_family("Other").family_id, "X", "self")
    with pytest.raises(ValueError, match="same family"):
        _profile(engine, me, "Saptarshi", "Self", heir_member_id=other.member_id)


def test_retired_members_fds_stay_out_of_the_equity_move_and_pass_to_the_heir(family):
    engine, family_id, me, mother, son = family
    _profile(engine, me, "Saptarshi", "Self", date_of_birth=date(1988, 2, 1), life_stage="EARNING",
             retirement_age=60)
    _profile(engine, mother, "Ratna Ray", "Mother", date_of_birth=date(1958, 6, 1),
             life_stage="RETIRED", heir_member_id=me)
    _profile(engine, son, "Saptajeet Ray", "Son", date_of_birth=date(2018, 5, 1), life_stage="DEPENDENT")
    engine.add_cash_flow_item(member_id=me, flow_type="INCOME", name="Salary", amount="200000")
    for member, number, amount in ((mother, "M1", "9000000"), (me, "S1", "1000000")):
        engine.create_fd(member_id=member, bank_name="SBI", fd_number=number, principal=amount,
                         interest_rate="7", issue_date=date(2025, 1, 1), maturity_date=date(2028, 1, 1),
                         payout_type="MATURITY")
    engine.set_succession_check(member_id=mother, item="WILL", done=True)

    h = engine.get_financial_health(family_id, today=date(2026, 10, 1))
    move = next(a for a in h.actions if "equity" in a.title.lower())
    mine = engine.get_fd_portfolio(family_id, me).total_current_value  # ₹10 L plus interest so far
    assert f"₹{(mine / 100000).quantize(D('0.1'))} L" in move.steps[0]  # at most my own FDs, never her ₹90 L
    assert any("Ratna Ray" in s and "left out" in s for s in move.steps)
    (flow,) = h.inheritance
    assert (flow.from_name, flow.to_name) == ("Ratna Ray", "Saptarshi") and flow.value > D("9000000")
    people = {pp.name: pp for pp in h.people}
    assert any("Senior Citizens" in a for a in people["Ratna Ray"].advice)
    assert next(c for c in people["Ratna Ray"].checklist if c.item == "WILL").done
    assert people["Saptarshi"].inherits == flow.value
    assert any("education" in a.lower() for a in people["Saptajeet Ray"].advice)
    (retire,) = h.retirements
    assert (retire.name, retire.years) == ("Saptarshi", 22) and len(h.projection) == 23
    assert h.equity_target_pct == D("62")  # 100 - 38, from the earner


def test_succession_check_rejects_unknown_items(family):
    engine, _, me, _, _ = family
    with pytest.raises(ValueError, match="unknown"):
        engine.set_succession_check(member_id=me, item="HOROSCOPE", done=True)


def test_life_cover_is_judged_on_the_earners_income():
    earner = PersonInput(1, "A", "Self", 40, "EARNING", None, 60, D("0"), D("0"), D("0"),
                         D("100000"), 0, 0, False)
    retired = PersonInput(2, "B", "Mother", 70, "RETIRED", "A", None, D("0"), D("0"), D("0"),
                          D("100000"), 0, 0, False)
    h = assess(_inputs(monthly_income=D("200000"), term_cover=D("0"), people=(earner, retired)))
    life = next(c for c in h.checks if c.key == "life")
    assert life.target == "about ₹1.20 Cr"  # 10 x the earner's ₹12 L, not the family's ₹24 L


# ----------------------------------------------------------------------
# Policy review
# ----------------------------------------------------------------------


def test_jeevan_anand_is_recognised_with_a_rough_return():
    r = review_policy(policy_type="ENDOWMENT", insurer="LIC India", plan_name="Jeevan Anand 915",
                      sum_assured=D("500000"), premium_amount=D("22000"), premium_frequency="ANNUAL",
                      start_date=date(2015, 3, 1), maturity_date=date(2036, 3, 1), nominee=None, status="ACTIVE")
    assert r.known_plan == "LIC Jeevan Anand"
    assert D("3") < r.estimated_return_pct < D("7")
    assert r.cover_per_rupee == D("23")
    assert any("nominee" in w.lower() for w in r.warnings)
    assert r.pros and r.cons and r.options


def test_term_plan_review_has_no_return_estimate():
    r = review_policy(policy_type="TERM", insurer="HDFC Life", plan_name="Click 2 Protect Super",
                      sum_assured=D("20000000"), premium_amount=D("4157"), premium_frequency="MONTHLY",
                      start_date=date(2024, 1, 1), maturity_date=date(2048, 1, 1), nominee="Sohini",
                      status="ACTIVE")
    assert r.kind == "Pure term insurance" and r.estimated_return_pct is None and not r.warnings


def test_unknown_plan_is_reviewed_by_its_type():
    r = review_policy(policy_type="ULIP", insurer="Some Life", plan_name="Wealth Plus",
                      sum_assured=D("1000000"), premium_amount=D("100000"), premium_frequency="ANNUAL",
                      start_date=date(2022, 1, 1), maturity_date=date(2032, 1, 1), nominee="X", status="ACTIVE")
    assert r.kind in ("Unit-linked plan (ULIP)", "Non-participating guaranteed savings plan")


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


def test_profile_and_checklist_routes(server):
    family_id, member_id = _family_with_member(server)
    status, body = _post(server, f"/api/members/{member_id}/profile", {
        "name": "Raj", "relationship": "self", "date_of_birth": "1985-04-02", "life_stage": "EARNING",
        "retirement_age": "58",
    })
    assert status == 200 and body["member"]["retirement_age"] == 58
    status, _ = _post(server, f"/api/members/{member_id}/succession", {"item": "WILL", "done": True})
    assert status == 200
    status, body = _post(server, f"/api/members/{member_id}/insurance", {
        "policy_type": "ENDOWMENT", "insurer": "LIC", "plan_name": "Jeevan Labh 936", "nominee": "Asha",
        "policy_number": "1", "premium_amount": "30000", "premium_frequency": "ANNUAL",
        "start_date": "2020-01-01", "sum_assured": "500000",
    })
    assert status == 200 and body["policy"]["review"]["known_plan"] == "LIC Jeevan Labh"
    _, body = _get(server, f"/api/families/{family_id}/health")
    assert body["people"][0]["checklist"][3]["done"] is True
