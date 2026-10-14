"""Personal planning: goals, earmarks, preferences and smart moves
(`financial_engine.planning`), and the Engine / routes around them."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine.planning import GoalInput, HoldingInput, PlanInputs, make_plan
from test_api_server import _family_with_member, _get, _post, server  # noqa: F401 (fixture)
from test_assistant_reminders import _family, engine  # noqa: F401 (fixture)

D = Decimal
TODAY = date(2026, 10, 14)


def _inputs(**kw):
    base = dict(as_of=TODAY, goals=(), holdings=(), monthly_outgo=D("80000"), monthly_free=D("30000"),
                cash_in_bank=D("500000"))
    base.update(kw)
    return PlanInputs(**base)


def _goal(gid=1, years=10, amount="2000000", gtype="EDUCATION", priority=1):
    return GoalInput(gid, "College", gtype, None, D(amount), TODAY.replace(year=TODAY.year + years), priority, None)


def _fd(asset_id=1, value="1000000", owner_stage="EARNING", **kw):
    return HoldingInput(asset_id=asset_id, owner_name="Raj", owner_stage=owner_stage, owner_age=40, kind="FD",
                        label="SBI FD", value=D(value), fd_rate=D("7"), **kw)


def test_a_goal_far_away_with_only_fds_is_behind_and_gets_a_sip():
    plan = make_plan(_inputs(goals=(_goal(),), holdings=(_fd(goal_id=1, goal_pct=D("100")),)))
    g = plan.goals[0]
    assert g.future_cost == D("5187485")  # ₹20 L at 10% for 10 years
    assert g.status == "BEHIND" and g.monthly_needed > 0 and g.equity_pct_suggested == 75
    assert g.equity_pct_now == 0 and "growth assets" in g.mix_note
    move = next(m for m in plan.moves if m.key == "goal-plan:1")
    f = plan.funding.goals[0]
    assert move.amount == f.sip_from_salary and move.amount_note == "/ month"
    assert "index or flexi-cap" in f.sip_fund and f.recommendation and f.best_fix == (f.options[0] if f.options else "")


def test_a_well_funded_goal_is_on_track():
    plan = make_plan(_inputs(goals=(_goal(amount="500000"),),
                             holdings=(_fd(value="1000000", goal_id=1, goal_pct=D("100")),)))
    assert plan.goals[0].status == "ON_TRACK" and plan.goals[0].monthly_needed == 0
    assert not any(m.key == "goal-plan:1" for m in plan.moves)


def test_fd_payout_interest_becomes_a_sip_for_the_goal_most_behind():
    fd = _fd(asset_id=2, fd_payout="QUARTERLY", fd_payout_interest=D("15000"))
    plan = make_plan(_inputs(goals=(_goal(),), holdings=(fd,)))
    move = next(m for m in plan.moves if m.key == "fd-interest-sip")
    assert move.amount == D("5000") and "College" in move.line and move.impact > 0


def test_a_retired_parent_gets_income_advice_not_equity():
    fd = _fd(asset_id=3, value="2000000", owner_stage="RETIRED", fd_payout="QUARTERLY",
             fd_payout_interest=D("35000"), maturity_date=date(2026, 12, 1), amount_at_maturity=D("2000000"))
    fd = HoldingInput(**{**fd.__dict__, "owner_age": 72})
    plan = make_plan(_inputs(goals=(_goal(),), holdings=(fd,)))
    keys = [m.key for m in plan.moves]
    assert "senior:Raj" in keys and "fd-interest-sip" not in keys
    assert not any(k.startswith("maturity:3") for k in keys)  # folded into the income card
    senior = next(m for m in plan.moves if m.key == "senior:Raj")
    assert any("Senior Citizens' Savings Scheme" in s for s in senior.steps)


def test_earmarked_emergency_money_counts_and_dismissed_moves_are_hidden():
    plan = make_plan(_inputs(cash_in_bank=D("100000"), holdings=(_fd(value="200000", earmark="EMERGENCY"),)))
    assert plan.emergency_months_have == D("3.8")
    assert any(m.key == "emergency" for m in plan.moves)
    hidden = make_plan(_inputs(cash_in_bank=D("100000"), holdings=(_fd(value="200000", earmark="EMERGENCY"),),
                               dismissed=frozenset({"emergency"})))
    assert not any(m.key == "emergency" for m in hidden.moves) and hidden.hidden[0].key == "emergency"


def test_a_maturing_free_fd_is_routed_to_a_long_goal_behind():
    fd = _fd(asset_id=4, maturity_date=date(2026, 12, 20), amount_at_maturity=D("1100000"))
    plan = make_plan(_inputs(goals=(_goal(),), holdings=(fd,)))
    move = next(m for m in plan.moves if m.key.startswith("maturity:4"))
    assert move.title.startswith("Route to College") and move.impact > 0


def test_no_goals_asks_for_them_and_scores_the_areas():
    plan = make_plan(_inputs())
    assert plan.moves[0].key == "add-goals"
    assert {a.key for a in plan.areas} == {"protection", "emergency", "goals", "growth", "loans", "savings"}
    assert next(a for a in plan.areas if a.key == "goals").score is None


# ---------------------------------------------------------------- the Engine

def test_goals_purposes_and_preferences_through_the_engine(engine):  # noqa: F811
    family_id, member_id = _family(engine)
    fd = engine.create_fd(member_id=member_id, bank_name="SBI", fd_number="F1", principal="500000",
                          interest_rate="7", issue_date=date(2025, 1, 1), maturity_date=date(2030, 1, 1),
                          payout_type="MATURITY")
    goal = engine.save_goal(family_id=family_id, name="House", goal_type="PROPERTY",
                            target_amount="5000000", target_date=date(2035, 1, 1), priority=1)
    engine.set_asset_purpose(asset_id=fd.asset_id, goal_id=goal.goal_id)
    engine.save_planning_preferences(family_id=family_id, tax_slab_pct="20", emergency_months=9,
                                     min_fd_amount="100000", avoid_direct_stocks=True)
    page = engine.get_plan(family_id)
    assert page.plan.goals[0].have_now > 0 and page.holdings[0].goal_id == goal.goal_id
    assert page.preferences.emergency_months == 9 and page.plan.emergency_months_target == 9
    # the health check-up uses the family's own settings
    assert next(c for c in page.health.checks if c.key == "emergency").target == "9 months of outgoings"
    with pytest.raises(ValueError, match="either"):
        engine.set_asset_purpose(asset_id=fd.asset_id, goal_id=goal.goal_id, purpose="KEEP")
    # a holding put towards a goal can still be deleted
    engine.delete_fd(fd.asset_id)
    assert engine.get_plan(family_id).plan.goals[0].have_now == 0
    engine.delete_goal(goal.goal_id)
    assert engine.list_goals(family_id) == []


def test_goal_is_validated(engine):  # noqa: F811
    family_id, _ = _family(engine)
    with pytest.raises(ValueError, match="future"):
        engine.save_goal(family_id=family_id, name="Old", goal_type="OTHER", target_amount="1000",
                         target_date=date(2020, 1, 1))
    with pytest.raises(ValueError, match="type"):
        engine.save_goal(family_id=family_id, name="X", goal_type="YACHT", target_amount="1000",
                         target_date=date(2040, 1, 1))


def test_plan_routes(server):  # noqa: F811
    family_id, member_id = _family_with_member(server)
    status, body = _post(server, f"/api/families/{family_id}/goals", {
        "name": "Retire", "goal_type": "RETIREMENT", "target_amount": "20000000", "target_date": "2050-04-01",
        "member_id": member_id, "priority": 1})
    assert status == 200, body
    goal_id = body["goal"]["goal_id"]
    status, body = _post(server, f"/api/families/{family_id}/planning-preferences",
                         {"tax_slab_pct": "10", "emergency_months": 4})
    assert status == 200, body
    status, page = _get(server, f"/api/families/{family_id}/plan")
    assert status == 200 and page["plan"]["goals"][0]["name"] == "Retire", page
    key = page["plan"]["moves"][0]["key"]
    status, _ = _post(server, f"/api/families/{family_id}/moves/dismiss", {"key": key, "dismissed": True})
    status, page = _get(server, f"/api/families/{family_id}/plan")
    assert key in [m["key"] for m in page["plan"]["hidden"]]
    status, _ = _post(server, f"/api/families/{family_id}/goals/{goal_id}/delete", {})
    assert status == 200


# ---------------------------------------------------------------- funding plan

def _holding(asset_id, owner, kind, value, stage="EARNING", **kw):
    return HoldingInput(asset_id=asset_id, owner_name=owner, owner_stage=stage, owner_age=40, kind=kind,
                        label=f"{owner} {kind}", value=D(value), fd_rate=D("7") if kind == "FD" else D("0"), **kw)


def _family_goal(amount, years, gid=1, gtype="OTHER", owner=None):
    return GoalInput(gid, "Car", gtype, owner, D(amount), TODAY.replace(year=TODAY.year + years), 1, D("6"))


POLICY = (("Raj", "YES"), ("Mother", "IF_NEEDED"))


def test_small_goal_uses_own_money_only():
    plan = make_plan(_inputs(goals=(_family_goal("200000", 2),), cash_in_bank=D("10000000"), money_policy=POLICY,
                             holdings=(_holding(1, "Raj", "FD", "500000"),
                                       _holding(2, "Mother", "FD", "3000000", stage="RETIRED"))))
    f = plan.funding.goals[0]
    assert f.result == "COVERED" and [u.owner_name for u in f.from_holdings] == ["Raj"] and not f.backup


def test_big_goal_shows_the_parents_money_only_as_backup():
    plan = make_plan(_inputs(goals=(_family_goal("1000000", 2),), cash_in_bank=D("10000000"), money_policy=POLICY,
                             monthly_free=D("10000"),
                             holdings=(_holding(1, "Raj", "FD", "300000"),
                                       _holding(2, "Mother", "FD", "3000000", stage="RETIRED"))))
    f = plan.funding.goals[0]
    assert f.result == "COVERED_WITH_BACKUP" and f.shortfall > 0 and f.shortfall_after_backup == 0
    assert {u.owner_name for u in f.from_holdings} == {"Raj"} and {u.owner_name for u in f.backup} == {"Mother"}
    assert f.sip_from_salary == D("10000")
    assert any("Mother's savings" in o for o in f.options)


def test_income_money_is_never_used_and_a_shortfall_gets_options():
    plan = make_plan(_inputs(goals=(_family_goal("800000", 4),), cash_in_bank=D("10000000"), money_policy=POLICY,
                             monthly_free=D("12000"),
                             holdings=(_holding(2, "Mother", "FD", "3000000", stage="RETIRED", earmark="INCOME"),)))
    f = plan.funding.goals[0]
    assert f.result == "SHORT" and not f.backup
    assert any("10% every year" in o for o in f.options) and any("later" in o for o in f.options)


def test_emergency_money_is_kept_aside_first():
    plan = make_plan(_inputs(goals=(_family_goal("400000", 2),), cash_in_bank=D("0"), monthly_outgo=D("50000"),
                             monthly_free=D("0"), money_policy=POLICY,
                             holdings=(_holding(1, "Raj", "FD", "500000"),)))
    assert plan.funding.emergency_reserved == D("300000")
    assert sum(u.value for u in plan.funding.goals[0].from_holdings) <= D("200000")


def test_apply_assigns_own_holdings_through_the_engine(engine):  # noqa: F811
    family_id, member_id = _family(engine)
    fund = engine.create_fd(member_id=member_id, bank_name="SBI", fd_number="F9", principal="900000",
                            interest_rate="7", issue_date=date(2025, 1, 1), maturity_date=date(2031, 1, 1),
                            payout_type="MATURITY")
    goal = engine.save_goal(family_id=family_id, name="Bike", goal_type="OTHER", target_amount="100000",
                            target_date=date(2028, 1, 1))
    page = engine.get_plan(family_id)
    assert page.member_policies[0].goal_money == "YES" and page.member_policies[0].is_default
    assert engine.apply_funding_plan(family_id) == 1
    after = engine.get_plan(family_id)
    assert after.plan.goals[0].status == "ON_TRACK"
    holding = next(h for h in after.holdings if h.asset_id == fund.asset_id)
    assert holding.goal_id == goal.goal_id and D("0") < holding.goal_pct < D("100")
    engine.set_member_goal_money(member_id=member_id, goal_money="NO")
    assert engine.get_plan(family_id).member_policies[0].goal_money == "NO"


def test_the_best_fix_comes_first_and_fits_the_budget_when_it_can():
    plan = make_plan(_inputs(goals=(_family_goal("800000", 4),), cash_in_bank=D("10000000"), money_policy=POLICY,
                             monthly_free=D("12000")))
    f = plan.funding.goals[0]
    assert f.result == "SHORT" and f.best_fix == f.options[0]
    assert f.plan_funded_pct < 100 and f.recommendation[0].startswith("Start a SIP")
    move = next(m for m in plan.moves if m.key == "goal-plan:1")
    assert "best fix" in move.line
