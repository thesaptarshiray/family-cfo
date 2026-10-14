"""
Personal planning: the family's goals, what each holding is for, and
"smart moves" that follow from them — a pure calculation (no I/O, no SQL).

Where `health` judges the finances against general rules of thumb, this
module judges them against what the family has said it wants:

  * **Goals** — an amount in today's money by a date. Holdings put towards a
    goal grow at their own expected return (running SIPs included); the goal
    is on track when that reaches the inflated cost by the date. Otherwise
    the monthly SIP that closes the gap is worked out, invested to suit the
    time left (more equity for far goals, safety for near ones).
  * **Earmarks** — holdings set aside as the emergency fund, as someone's
    regular income, or simply "keep". They are never suggested for moving.
  * **Preferences** — tax slab (for FD returns after tax), emergency months,
    a minimum to keep in FDs, no direct stocks, and dismissed suggestions.
  * **Smart moves** — short, specific suggestions with a key (so "not for
    me" can hide one for good), e.g. FD payout interest → a SIP for the
    goal most behind; a maturing FD → the goal that needs it; a retired
    parent's FDs → income and safety ideas, never "move to stocks".
  * **Area scores** — Protection, Emergency, Goals, Growth, Loans, Savings,
    0-100 each, so the page can show at a glance where the family lags.
  * **Funding plan** — how every goal can be paid for, in this order: the
    emergency fund is kept aside first; then each goal (by priority, then
    date) takes the goal owner's own free holdings (for a family goal, the
    money of members whose money may be used — "YES"), suited to the time
    left; then a SIP from the monthly salary surplus; and only for what is
    still short, other members' money marked "IF_NEEDED" (e.g. a retired
    parent) — shown separately as a backup, never applied by itself. Money
    earmarked as someone's income, emergency or "keep" is never used. What
    still does not fit comes with options: step the SIP up, a later date,
    or a smaller amount.

Expected returns are the same as `health`'s assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .health import (
    ATTENTION, CASH_RETURN, DEBT_RETURN, EPF_RETURN, EQUITY_RETURN, GOLD_RETURN, GOOD, MISSING, OK,
    HealthAction, HealthCheck, _inr,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_TWELVE = Decimal("12")
_RUPEE = Decimal("1")
HYBRID_RETURN = (EQUITY_RETURN + DEBT_RETURN) / 2
NPS_RETURN = Decimal("0.09")

GOAL_TYPES = ("EDUCATION", "RETIREMENT", "PROPERTY", "EMERGENCY_FUND", "TRAVEL", "OTHER")
GOAL_INFLATION = {  # % a year, when the goal does not set its own
    "EDUCATION": Decimal("10"), "RETIREMENT": Decimal("6"), "PROPERTY": Decimal("7"),
    "EMERGENCY_FUND": Decimal("6"), "TRAVEL": Decimal("6"), "OTHER": Decimal("6"),
}
PURPOSES = ("EMERGENCY", "INCOME", "KEEP")
_EQUITY_SHARE = {"EQUITY_FUND": _ONE, "STOCK": _ONE, "HYBRID_FUND": Decimal("0.5")}
SCSS_LIMIT = Decimal("3000000")
SENIOR_AGE = 60


def _round(value: Decimal) -> Decimal:
    return value.quantize(_RUPEE, rounding=ROUND_HALF_UP)


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class HoldingInput:
    """One holding: what it is, what it is worth, and what it is for."""

    asset_id: int
    owner_name: str
    owner_stage: Optional[str]  # EARNING / RETIRED / DEPENDENT
    owner_age: Optional[int]
    kind: str  # FD / EQUITY_FUND / DEBT_FUND / HYBRID_FUND / GOLD / STOCK / EPF / PPF / NPS
    label: str  # "HDFC Bank FD", "Parag Parikh Flexi Cap"
    value: Decimal
    monthly_sip: Decimal = _ZERO
    fd_rate: Decimal = _ZERO  # annual %
    fd_payout: Optional[str] = None  # QUARTERLY / MONTHLY when interest is paid out
    fd_payout_interest: Decimal = _ZERO  # per payout
    maturity_date: Optional[date] = None
    amount_at_maturity: Decimal = _ZERO
    goal_id: Optional[int] = None
    goal_pct: Decimal = _ZERO  # share put towards the goal, %
    earmark: Optional[str] = None  # EMERGENCY / INCOME / KEEP


@dataclass(frozen=True)
class GoalInput:
    goal_id: int
    name: str
    goal_type: str
    owner_name: Optional[str]
    target_today: Decimal
    target_date: date
    priority: int  # 1 high, 2 medium, 3 low
    inflation_pct: Optional[Decimal]


@dataclass(frozen=True)
class PlanInputs:
    as_of: date
    goals: Tuple[GoalInput, ...]
    holdings: Tuple[HoldingInput, ...]
    monthly_outgo: Decimal  # expenses + EMIs
    monthly_free: Decimal  # left after outgoings and existing SIPs / investments
    cash_in_bank: Decimal
    tax_slab_pct: Decimal = Decimal("30")
    emergency_months: int = 6
    min_fd_amount: Decimal = _ZERO
    avoid_direct_stocks: bool = False
    dismissed: frozenset = frozenset()
    checks: Tuple[HealthCheck, ...] = ()  # from `health.assess`, for the area scores
    actions: Tuple[HealthAction, ...] = ()  # from `health.assess`, kept where still relevant
    has_loans: bool = False
    # Whose money may fund goals that are not their own: (name, YES / IF_NEEDED / NO).
    money_policy: Tuple[Tuple[str, str], ...] = ()


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class GoalHolding:
    asset_id: int
    label: str
    owner_name: str
    value: Decimal  # the share put towards the goal


@dataclass(frozen=True)
class GoalView:
    goal_id: int
    name: str
    goal_type: str
    owner_name: Optional[str]
    priority: int
    target_today: Decimal
    target_date: date
    inflation_pct: Decimal
    years_left: Decimal
    future_cost: Decimal  # what it will cost then
    have_now: Decimal  # holdings put towards it, today
    projected: Decimal  # what they (and their SIPs) grow to by the date
    funded_pct: int  # projected / future cost
    status: str  # ON_TRACK / CLOSE / BEHIND / DUE
    monthly_needed: Decimal  # extra SIP that closes the gap
    equity_pct_suggested: int  # how the extra money should be invested
    equity_pct_now: Optional[int]  # of the holdings put towards it
    mix_note: str
    holdings: Tuple[GoalHolding, ...]


@dataclass(frozen=True)
class SmartMove:
    key: str  # stable, for "not for me"
    kind: str  # GOAL / FD / SENIOR / EMERGENCY / PROTECT / COST / LOAN / SETUP / OTHER
    title: str
    line: str  # one short sentence
    steps: Tuple[str, ...] = ()
    amount: Optional[Decimal] = None  # ₹ (a month for SIPs) the move is about
    amount_note: str = ""  # "/ month", "once"
    impact: Optional[Decimal] = None  # ₹ gained, estimated
    impact_note: str = ""  # "more by 2035", "a year"
    priority: int = 2  # 1 first


@dataclass(frozen=True)
class AreaScore:
    key: str
    label: str
    score: Optional[int]  # 0-100; None = not enough data
    status: str  # GOOD / OK / ATTENTION / MISSING
    headline: str  # a few words


@dataclass(frozen=True)
class PurposeTotal:
    purpose: str  # GOALS / EMERGENCY / INCOME / KEEP / FREE
    value: Decimal


@dataclass(frozen=True)
class FundingUse:
    asset_id: int
    label: str
    owner_name: str
    value: Decimal  # today's value used
    pct: Decimal  # of the holding


@dataclass(frozen=True)
class GoalFunding:
    goal_id: int
    name: str
    gap_before: Decimal  # still needed (at the goal date) beyond what is already put towards it
    from_holdings: Tuple[FundingUse, ...]  # own / allowed free holdings — applied by "Apply"
    sip_from_salary: Decimal  # a month
    backup: Tuple[FundingUse, ...]  # "only if needed" money that would cover the rest
    shortfall: Decimal  # at the goal date, after own money and the SIP
    shortfall_after_backup: Decimal
    result: str  # COVERED / COVERED_WITH_BACKUP / SHORT
    options: Tuple[str, ...]  # every way to close a shortfall, best first
    recommendation: Tuple[str, ...] = ()  # the suggested way, in a few short lines
    best_fix: str = ""  # options[0], when there is a shortfall
    plan_funded_pct: int = 100  # how far own money + the SIP get, at the goal date
    sip_fund: str = ""  # what kind of fund the SIP suits


@dataclass(frozen=True)
class FundingPlan:
    monthly_free: Decimal
    sip_total: Decimal
    monthly_left: Decimal
    emergency_reserved: Decimal
    goals: Tuple[GoalFunding, ...]
    applicable: int  # holdings "Apply" would assign


@dataclass(frozen=True)
class Plan:
    as_of: date
    score: Optional[int]
    areas: Tuple[AreaScore, ...]
    goals: Tuple[GoalView, ...]
    moves: Tuple[SmartMove, ...]
    hidden: Tuple[SmartMove, ...]  # dismissed as "not for me" (can be brought back)
    purposes: Tuple[PurposeTotal, ...]
    emergency_months_have: Optional[Decimal]
    emergency_months_target: int
    funding: Optional[FundingPlan] = None


# ----------------------------------------------------------------------
# Money maths
# ----------------------------------------------------------------------


def _years(start: date, end: date) -> Decimal:
    return (Decimal((end - start).days) / Decimal("365.25")).quantize(Decimal("0.01"))


def _grow(value: Decimal, rate: Decimal, years: Decimal) -> Decimal:
    if years <= 0:
        return value
    return value * (_ONE + rate) ** years


def _monthly_rate(annual: Decimal) -> Decimal:
    return (_ONE + annual) ** (_ONE / _TWELVE) - _ONE


def _sip_value(monthly: Decimal, annual: Decimal, years: Decimal) -> Decimal:
    """What `monthly` invested at the start of every month grows to."""
    months = (years * _TWELVE).to_integral_value()
    if monthly <= 0 or months <= 0:
        return _ZERO
    i = _monthly_rate(annual)
    return monthly * (((_ONE + i) ** months - _ONE) / i) * (_ONE + i)


def _sip_needed(gap: Decimal, annual: Decimal, years: Decimal) -> Decimal:
    if gap <= 0:
        return _ZERO
    months = (years * _TWELVE).to_integral_value()
    if months <= 1:
        return gap
    per_rupee = _sip_value(_ONE, annual, years)
    return gap / per_rupee if per_rupee > 0 else gap


def _equity_for(years: Decimal) -> int:
    """How much of new money for a goal suits equity, by time left."""
    if years >= 10:
        return 75
    if years >= 7:
        return 65
    if years >= 5:
        return 50
    if years >= 3:
        return 30
    return 0


def _blended(equity_pct: int) -> Decimal:
    e = Decimal(equity_pct) / _HUNDRED
    return e * EQUITY_RETURN + (_ONE - e) * DEBT_RETURN


def _holding_return(h: HoldingInput, tax: Decimal) -> Decimal:
    if h.kind == "FD":
        rate = h.fd_rate / _HUNDRED if h.fd_rate else DEBT_RETURN
        return rate * (_ONE - tax)
    return {
        "EQUITY_FUND": EQUITY_RETURN, "STOCK": EQUITY_RETURN, "HYBRID_FUND": HYBRID_RETURN,
        "DEBT_FUND": DEBT_RETURN, "GOLD": GOLD_RETURN, "EPF": EPF_RETURN, "PPF": Decimal("0.071"),
        "NPS": NPS_RETURN,
    }.get(h.kind, DEBT_RETURN)


def _year_of(d: date) -> str:
    return f"{d:%b %Y}"


# ----------------------------------------------------------------------
# Goals
# ----------------------------------------------------------------------


def _goal_view(g: GoalInput, holdings: Sequence[HoldingInput], inp: PlanInputs) -> GoalView:
    tax = inp.tax_slab_pct / _HUNDRED
    years = max(_years(inp.as_of, g.target_date), _ZERO)
    infl = g.inflation_pct if g.inflation_pct is not None else GOAL_INFLATION.get(g.goal_type, Decimal("6"))
    future = g.target_today * (_ONE + infl / _HUNDRED) ** years
    have = projected = equity_value = _ZERO
    parts: List[GoalHolding] = []
    for h in holdings:
        share = h.goal_pct / _HUNDRED
        value = h.value * share
        have += value
        rate = _holding_return(h, tax)
        projected += _grow(value, rate, years) + _sip_value(h.monthly_sip * share, rate, years)
        equity_value += value * _EQUITY_SHARE.get(h.kind, _ZERO)
        parts.append(GoalHolding(h.asset_id, h.label, h.owner_name, _round(value)))
    funded = int((projected / future * _HUNDRED).to_integral_value()) if future > 0 else 0
    eq_suggest = _equity_for(years)
    needed = _sip_needed(future - projected, _blended(eq_suggest), years) if years > 0 else _ZERO
    if years <= 0:
        status = "DUE"
    elif funded >= 100:
        status = "ON_TRACK"
    elif funded >= 75:
        status = "CLOSE"
    else:
        status = "BEHIND"
    eq_now = int((equity_value / have * _HUNDRED).to_integral_value()) if have > 0 else None
    note = ""
    if eq_now is not None and years < 3 and eq_now > 30:
        note = (f"{eq_now}% of it is in equity but the money is needed within {max(1, int(years) or 1)} "
                f"year{'s' if years >= 2 else ''} — move it to safety (FD / short-term debt) over the coming months.")
    elif eq_now is not None and years >= 7 and eq_now < 30 and have > 0:
        note = (f"Only {eq_now}% of it is in equity for a goal {int(years)} years away — "
                f"growth assets would do more of the work.")
    return GoalView(
        goal_id=g.goal_id, name=g.name, goal_type=g.goal_type, owner_name=g.owner_name, priority=g.priority or 2,
        target_today=_round(g.target_today), target_date=g.target_date, inflation_pct=infl,
        years_left=years, future_cost=_round(future), have_now=_round(have), projected=_round(projected),
        funded_pct=min(funded, 999), status=status, monthly_needed=_round(needed),
        equity_pct_suggested=eq_suggest, equity_pct_now=eq_now, mix_note=note, holdings=tuple(parts),
    )


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------


def make_plan(inp: PlanInputs) -> Plan:
    tax = inp.tax_slab_pct / _HUNDRED
    by_goal: Dict[int, List[HoldingInput]] = {}
    for h in inp.holdings:
        if h.goal_id is not None:
            by_goal.setdefault(h.goal_id, []).append(h)
    goals = tuple(sorted(
        (_goal_view(g, by_goal.get(g.goal_id, []), inp) for g in inp.goals),
        key=lambda v: (v.target_date, v.priority),
    ))
    goal_by_id = {g.goal_id: g for g in goals}
    moves: List[SmartMove] = []

    # ---- where the money is (for the "what is it for" strip)
    def total(pred) -> Decimal:
        return sum((h.value * (h.goal_pct / _HUNDRED if h.goal_id is not None else _ONE)
                    for h in inp.holdings if pred(h)), _ZERO)

    in_goals = total(lambda h: h.goal_id is not None)
    emergency_held = total(lambda h: h.earmark == "EMERGENCY")
    income_held = total(lambda h: h.earmark == "INCOME")
    keep_held = total(lambda h: h.earmark == "KEEP")
    everything = sum((h.value for h in inp.holdings), _ZERO)
    free = max(everything - in_goals - emergency_held - income_held - keep_held, _ZERO)
    purposes = tuple(PurposeTotal(p, _round(v)) for p, v in (
        ("GOALS", in_goals), ("EMERGENCY", emergency_held), ("INCOME", income_held),
        ("KEEP", keep_held), ("FREE", free)) if v > 0)

    # Holdings that may be suggested for moving: not earmarked, not
    # someone's income, not a retired person's, not for a goal due soon.
    def movable(h: HoldingInput) -> bool:
        if h.earmark is not None or h.owner_stage == "RETIRED":
            return False
        if h.goal_id is not None:
            g = goal_by_id.get(h.goal_id)
            return g is not None and g.years_left >= 5
        return True

    behind = [g for g in goals if g.status in ("BEHIND", "CLOSE") and g.years_left > 0]
    long_behind = sorted([g for g in behind if g.years_left >= 5], key=lambda g: (g.priority, g.funded_pct))

    # ---- 0. No goals yet
    if not goals:
        moves.append(SmartMove(
            "add-goals", "SETUP", "Add your goals",
            "Tell the app what the money is for — child's education, a house, retirement — "
            "and every suggestion is measured against them.",
            ("Health tab → Plan settings → Add a goal (amount in today's money and a date).",
             "Then put existing FDs / funds towards each goal, and mark emergency or income money."),
            priority=1))

    # ---- 1. Each goal's recommended way (see `make_funding_plan`)
    free_month = max(inp.monthly_free, _ZERO)
    funding = make_funding_plan(inp, goals) if goals else None
    by_id = {gv.goal_id: gv for gv in goals}
    for f in (funding.goals if funding else ()):
        gv = by_id[f.goal_id]
        if f.sip_from_salary <= 0 and f.result == "COVERED":
            continue
        title = (f"{f.name}: start {_inr(f.sip_from_salary)} a month" if f.sip_from_salary > 0
                 else f"{f.name}: choose a fix")
        line = " ".join(f.recommendation)
        if f.result != "COVERED" and f.best_fix:
            line += f" Still {_inr(f.shortfall)} short — best fix: {f.best_fix[0].lower() + f.best_fix[1:]}"
        moves.append(SmartMove(
            f"goal-plan:{f.goal_id}", "GOAL", title, line, tuple(f.options[1:]),
            f.sip_from_salary if f.sip_from_salary > 0 else None, "/ month" if f.sip_from_salary > 0 else "",
            priority=1 if gv.priority == 1 else 2))
    for g in goals:
        if g.mix_note and g.status not in ("BEHIND", "CLOSE"):
            moves.append(SmartMove(
                f"goal-mix:{g.goal_id}", "GOAL", f"Rebalance: {g.name}", g.mix_note,
                ("Use an STP (systematic transfer) over 3-6 months rather than one switch.",),
                priority=2))

    # ---- 2. FD payout interest -> a SIP
    payout_fds = [h for h in inp.holdings if h.kind == "FD" and h.fd_payout in ("QUARTERLY", "MONTHLY")
                  and h.fd_payout_interest > 0 and h.earmark != "INCOME" and h.owner_stage != "RETIRED"]
    if payout_fds:
        monthly = sum((h.fd_payout_interest / (3 if h.fd_payout == "QUARTERLY" else 1) for h in payout_fds), _ZERO)
        target = long_behind[0] if long_behind else None
        years = target.years_left if target else Decimal("10")
        gain = _sip_value(monthly, EQUITY_RETURN, years) - _sip_value(monthly, CASH_RETURN, years)
        moves.append(SmartMove(
            "fd-interest-sip", "FD", "Turn FD interest into a SIP",
            f"Your FDs pay out about {_inr(_round(monthly))} a month of interest into the bank"
            + (f" — put it to work for {target.name}." if target else " — put it to work instead of letting it sit."),
            tuple(f"{h.label} ({h.owner_name}): {_inr(h.fd_payout_interest)} every "
                  f"{'quarter' if h.fd_payout == 'QUARTERLY' else 'month'}" for h in payout_fds[:4])
            + ("Set a monthly SIP for the same amount on the day after the interest lands "
               "(or an STP from a liquid fund for quarterly payouts).",),
            _round(monthly), "/ month", _round(gain),
            f"more by {_year_of(target.target_date)}" if target else "more in 10 years than leaving it in savings",
            priority=2))

    # ---- 3. FDs maturing soon
    fd_total = sum((h.value for h in inp.holdings if h.kind == "FD"), _ZERO)
    soon = sorted((h for h in inp.holdings if h.kind == "FD" and h.maturity_date
                   and inp.as_of <= h.maturity_date <= inp.as_of + timedelta(days=120)),
                  key=lambda h: h.maturity_date)
    for h in soon:
        amount = h.amount_at_maturity or h.value
        key = f"maturity:{h.asset_id}:{h.maturity_date.isoformat()}"
        when = f"{h.maturity_date:%d %b %Y}"
        goal = goal_by_id.get(h.goal_id) if h.goal_id else None
        if h.earmark == "INCOME" or h.owner_stage == "RETIRED":
            continue  # covered by that person's income card below
        if h.earmark in ("EMERGENCY", "KEEP"):
            moves.append(SmartMove(
                key, "FD", f"Renew: {h.label}",
                f"{_inr(amount)} matures on {when}; it is set aside "
                + ("as emergency money" if h.earmark == "EMERGENCY" else "to keep") + " — renew at the best rate.",
                ("Compare a few banks' rates before it auto-renews.",),
                _round(amount), "matures", priority=3))
        elif goal is not None and goal.years_left < 3:
            moves.append(SmartMove(
                key, "FD", f"Keep for {goal.name}: {h.label}",
                f"{_inr(amount)} matures on {when} and {goal.name} is due {_year_of(goal.target_date)} — renew it to mature just before then.",
                (), _round(amount), "matures", priority=2))
        elif inp.min_fd_amount > 0 and fd_total - h.value < inp.min_fd_amount:
            moves.append(SmartMove(
                key, "FD", f"Renew: {h.label}",
                f"{_inr(amount)} matures on {when}; renewing keeps your FDs above the {_inr(inp.min_fd_amount)} you want to hold.",
                (), _round(amount), "matures", priority=3))
        elif long_behind and movable(h):
            g = long_behind[0]
            gain = _grow(amount, _blended(g.equity_pct_suggested), g.years_left) - _grow(
                amount, _holding_return(h, tax), g.years_left)
            moves.append(SmartMove(
                key, "FD", f"Route to {g.name}: {h.label}",
                f"{_inr(amount)} matures on {when} — {g.name} is behind; put this towards it.",
                (f"Park it in a liquid fund and move it into the goal's investments through an STP over 6-12 months "
                 f"({g.equity_pct_suggested}% equity suits a goal {int(g.years_left)} years away).",
                 "Then set this FD's goal on the Health tab (Plan settings → What each holding is for)."),
                _round(amount), "matures", _round(gain), f"more by {_year_of(g.target_date)}", priority=2))
        else:
            moves.append(SmartMove(
                key, "FD", f"Decide before it renews: {h.label}",
                f"{_inr(amount)} matures on {when}. Give it a job — a goal, the emergency fund, or renew at the best rate.",
                (), _round(amount), "matures", priority=3))

    # ---- 4. Retired members and income money: income & safety, never "move to stocks"
    seniors: Dict[str, List[HoldingInput]] = {}
    for h in inp.holdings:
        if h.kind == "FD" and (h.owner_stage == "RETIRED" or h.earmark == "INCOME"):
            seniors.setdefault(h.owner_name, []).append(h)
    for name, fds in seniors.items():
        value = sum((h.value for h in fds), _ZERO)
        age = next((h.owner_age for h in fds if h.owner_age is not None), None)
        yearly = sum((h.value * h.fd_rate / _HUNDRED for h in fds), _ZERO)
        next_year = [h for h in fds if h.maturity_date and h.maturity_date <= inp.as_of + timedelta(days=365)]
        steps = []
        if age is not None and age >= SENIOR_AGE:
            steps.append(f"Senior Citizens' Savings Scheme: up to {_inr(SCSS_LIMIT)} per person at a government-set rate "
                         "(about 8.2% lately), interest paid quarterly — a safer, often better home for maturing FDs.")
            steps.append("Old tax regime: Section 80TTB exempts up to ₹50,000 a year of interest for a senior; "
                         "submit Form 15H to each bank so no TDS is cut if total income is below the taxable limit.")
        steps.append(f"About {_inr(_round(yearly))} a year of interest comes from these FDs"
                     + (f"; {len(next_year)} of them mature in the next 12 months — renew in a ladder (1, 2, 3 years) "
                        "so they don't all reset at one rate." if next_year else "."))
        steps.append("Keep this money in fixed income — it is income, not long-term growth money.")
        moves.append(SmartMove(
            f"senior:{name}", "SENIOR", f"{name}'s FD income",
            f"{_inr(_round(value))} in FDs giving regular income — keep it safe and tax-light.",
            tuple(steps), _round(yearly), "/ year interest", priority=2))

    # ---- 5. Emergency fund
    target_need = inp.monthly_outgo * inp.emergency_months
    have_emergency = inp.cash_in_bank + emergency_held
    months_have: Optional[Decimal] = None
    if inp.monthly_outgo > 0:
        months_have = (have_emergency / inp.monthly_outgo).quantize(Decimal("0.1"))
        if have_emergency < target_need:
            gap = target_need - have_emergency
            spare = [h for h in inp.holdings if h.kind in ("FD", "DEBT_FUND") and movable(h)]
            spare_value = sum((h.value for h in spare), _ZERO)
            line = (f"Bank balance plus emergency-marked money covers {months_have} months of outgoings; "
                    f"you want {inp.emergency_months}.")
            steps = ((f"Mark about {_inr(_round(gap))} of FDs / liquid funds as Emergency (Plan settings) — "
                      "you already have unassigned FDs that cover it.",) if spare_value >= gap else
                     (f"Build it up: park {_inr(_round(gap))} more in a sweep-in FD or liquid fund.",))
            moves.append(SmartMove("emergency", "EMERGENCY", "Complete the emergency fund", line, steps,
                                   _round(gap), "to set aside", priority=1))

    # ---- 6. Idle money with no goals asking for it
    if not behind and goals and free_month > inp.monthly_outgo * Decimal("0.1"):
        moves.append(SmartMove(
            "surplus", "OTHER", "Put the idle monthly surplus to work",
            f"About {_inr(_round(free_month))} a month is left uninvested and every goal is on track.",
            ("Raise an existing SIP or start one for your longest goal; step it up 10% a year.",),
            _round(free_month * Decimal("0.7")), "/ month", priority=3))

    # ---- 7. General checks that still apply (protection, costs, loans, policies)
    keep_kinds = {
        "life": "PROTECT", "health": "PROTECT", "direct": "COST", "prepay": "LOAN", "policies": "OTHER",
        "trim": "OTHER",
    }
    for a in inp.actions:
        kind = next((v for k, v in keep_kinds.items() if a.key.startswith(k)), None)
        if kind is None or (a.key.startswith("trim") and inp.avoid_direct_stocks):
            continue
        moves.append(SmartMove(a.key, kind, a.title, a.why, a.steps, None, "", a.yearly_impact,
                               a.impact_note, priority=a.priority))

    dismissed = [m for m in moves if m.key in inp.dismissed]
    moves = [m for m in moves if m.key not in inp.dismissed]
    moves.sort(key=lambda m: (m.priority, -(m.impact or _ZERO)))

    areas = _areas(inp, goals, months_have, funding)
    weights = {"protection": 3, "emergency": 2, "goals": 3, "growth": 2, "loans": 1, "savings": 2}
    scored = [(a, weights[a.key]) for a in areas if a.score is not None]
    score = (int((Decimal(sum(a.score * w for a, w in scored)) / Decimal(sum(w for _, w in scored)))
                 .to_integral_value(rounding=ROUND_HALF_UP)) if scored else None)
    return Plan(
        funding=funding,
        as_of=inp.as_of, score=score, areas=areas, goals=goals, moves=tuple(moves),
        hidden=tuple(dismissed), purposes=purposes, emergency_months_have=months_have,
        emergency_months_target=inp.emergency_months,
    )


# ----------------------------------------------------------------------
# Area scores
# ----------------------------------------------------------------------

_POINTS = {GOOD: 100, OK: 60, ATTENTION: 20}


def _status_of(score: int) -> str:
    return GOOD if score >= 75 else OK if score >= 45 else ATTENTION


def _from_checks(checks: Sequence[HealthCheck], keys: Iterable[str]) -> Optional[int]:
    found = [c for c in checks if c.key in keys and c.status != MISSING]
    if not found:
        return None
    return int((Decimal(sum(_POINTS[c.status] * c.weight for c in found))
                / Decimal(sum(c.weight for c in found))).to_integral_value(rounding=ROUND_HALF_UP))


def _areas(inp: PlanInputs, goals: Sequence[GoalView], months_have: Optional[Decimal],
           funding: Optional["FundingPlan"] = None) -> Tuple[AreaScore, ...]:
    checks = {c.key: c for c in inp.checks}
    out: List[AreaScore] = []

    s = _from_checks(inp.checks, ("life", "health"))
    weak = [checks[k].title.lower() for k in ("life", "health") if k in checks and checks[k].status == ATTENTION]
    out.append(AreaScore("protection", "Protection", s, _status_of(s) if s is not None else MISSING,
                         "Covered" if s is not None and not weak else
                         ("Add income to judge" if s is None else "Gap in " + " & ".join(weak))))

    if months_have is None:
        out.append(AreaScore("emergency", "Emergency fund", None, MISSING, "Add monthly expenses"))
    else:
        s = min(100, int(months_have / Decimal(inp.emergency_months) * 100))
        out.append(AreaScore("emergency", "Emergency fund", s, _status_of(s),
                             f"{months_have} of {inp.emergency_months} months"))

    active = [g for g in goals if g.status != "DUE"]
    if not active:
        out.append(AreaScore("goals", "Goals", None, MISSING, "Add your goals"))
    else:
        w = {1: 3, 2: 2, 3: 1}
        planned = {f.goal_id: f for f in (funding.goals if funding else ())}
        pct = {g.goal_id: (planned[g.goal_id].plan_funded_pct if g.goal_id in planned else min(g.funded_pct, 100))
               for g in active}
        s = int((Decimal(sum(pct[g.goal_id] * w.get(g.priority, 2) for g in active))
                 / Decimal(sum(w.get(g.priority, 2) for g in active))).to_integral_value(rounding=ROUND_HALF_UP))
        ok = sum(1 for g in active if pct[g.goal_id] >= 100)
        out.append(AreaScore("goals", "Goals", s, _status_of(s),
                             f"{ok} of {len(active)} reachable" if len(active) > 1
                             else ("Reachable" if s >= 100 else f"{s}% reachable")))

    s = _from_checks(inp.checks, ("mix", "fd", "costs", "gold", "stocks", "policies"))
    bad = [checks[k].title for k in ("costs", "fd", "mix", "policies") if k in checks and checks[k].status == ATTENTION]
    out.append(AreaScore("growth", "Growth", s, _status_of(s) if s is not None else MISSING,
                         "Working well" if s is not None and not bad else (bad[0] if bad else "Add investments")))

    if not inp.has_loans:
        out.append(AreaScore("loans", "Loans", 100, GOOD, "No loans"))
    else:
        s = _from_checks(inp.checks, ("emi",))
        out.append(AreaScore("loans", "Loans", s if s is not None else None,
                             _status_of(s) if s is not None else MISSING,
                             checks["emi"].value if "emi" in checks else "Add income"))

    s = _from_checks(inp.checks, ("savings",))
    out.append(AreaScore("savings", "Savings rate", s, _status_of(s) if s is not None else MISSING,
                         checks["savings"].value + " saved" if "savings" in checks and s is not None
                         else "Add income & expenses"))
    return tuple(out)


# ----------------------------------------------------------------------
# Funding plan
# ----------------------------------------------------------------------

STEP_UP = Decimal("0.10")  # a SIP raised 10% every year


def _fund_for(years: Decimal) -> str:
    """The kind of fund a new SIP for a goal this far away suits."""
    if years >= 7:
        return "a Nifty 50 index or flexi-cap fund"
    if years >= 5:
        return "a balanced-advantage (hybrid) fund"
    if years >= 3:
        return "a conservative-hybrid or short-duration debt fund"
    return "a recurring deposit or liquid fund"


def _names(uses: Sequence["FundingUse"]) -> str:
    labels = [u.label for u in uses]
    return ", ".join(labels[:2]) + (f" and {len(labels) - 2} more" if len(labels) > 2 else "")
_LOCKED = ("EPF", "PPF", "NPS")  # retirement money: only for retirement goals


def _policy(inp: PlanInputs) -> Dict[str, str]:
    return dict(inp.money_policy)


def _order_for(years: Decimal, goal_type: str, avoid_stocks: bool) -> Tuple[str, ...]:
    """Which kinds of holding suit a goal this far away, best first."""
    if goal_type == "RETIREMENT":
        base = ("EPF", "PPF", "NPS", "EQUITY_FUND", "HYBRID_FUND", "DEBT_FUND", "FD", "GOLD", "STOCK")
    elif years >= 7:
        base = ("EQUITY_FUND", "HYBRID_FUND", "STOCK", "FD", "DEBT_FUND", "GOLD")
    elif years >= 3:
        base = ("HYBRID_FUND", "DEBT_FUND", "FD", "EQUITY_FUND", "GOLD", "STOCK")
    else:
        base = ("FD", "DEBT_FUND", "HYBRID_FUND")  # no equity for money needed soon
    return tuple(k for k in base if not (k == "STOCK" and avoid_stocks))


def _stepped_value(monthly: Decimal, annual: Decimal, years: Decimal) -> Decimal:
    """What a SIP that starts at `monthly` and rises STEP_UP a year grows to."""
    months = int((years * _TWELVE).to_integral_value())
    if monthly <= 0 or months <= 0:
        return _ZERO
    i = _monthly_rate(annual)
    total = _ZERO
    for m in range(months):
        amount = monthly * (_ONE + STEP_UP) ** (m // 12)
        total += amount * (_ONE + i) ** (months - m)
    return total


def make_funding_plan(inp: PlanInputs, goals: Sequence[GoalView]) -> FundingPlan:
    tax = inp.tax_slab_pct / _HUNDRED
    policy = _policy(inp)
    # Free holdings and how much of each is still unused (today's value)
    left: Dict[int, Decimal] = {}
    free: List[HoldingInput] = []
    for h in inp.holdings:
        if h.goal_id is None and h.earmark is None and h.value > 0:
            free.append(h)
            left[h.asset_id] = h.value

    # 1. The emergency fund first, from liquid money of people whose money may be used.
    need = max(inp.monthly_outgo * inp.emergency_months - inp.cash_in_bank
               - sum((h.value for h in inp.holdings if h.earmark == "EMERGENCY"), _ZERO), _ZERO)
    reserved = _ZERO
    for h in sorted(free, key=lambda h: (h.kind != "DEBT_FUND", h.kind != "FD", h.value)):
        if need <= 0:
            break
        if h.kind not in ("FD", "DEBT_FUND") or policy.get(h.owner_name, "YES") != "YES":
            continue
        take = min(left[h.asset_id], need)
        left[h.asset_id] -= take
        need -= take
        reserved += take

    monthly_left = max(inp.monthly_free, _ZERO)
    plans: List[GoalFunding] = []
    applicable = 0
    for g in sorted(goals, key=lambda g: (g.priority, g.target_date)):
        years = g.years_left
        gap = max(g.future_cost - g.projected, _ZERO)
        gap_before = gap
        if years <= 0 or gap <= 0:
            done = gap <= 0
            plans.append(GoalFunding(
                g.goal_id, g.name, _round(gap), (), _ZERO, (), _ZERO, _ZERO, "COVERED" if done else "SHORT",
                () if done else ("The date has passed — update the goal's date or amount.",),
                ("Already covered by the money set aside for it.",) if done else (),
                "" if done else "The date has passed — update the goal's date or amount.",
                100 if done else g.funded_pct))
            continue
        order = _order_for(years, g.goal_type, inp.avoid_direct_stocks)

        def own(h: HoldingInput) -> bool:
            if g.owner_name:  # a personal goal: the person's own money
                return h.owner_name == g.owner_name
            return policy.get(h.owner_name, "YES") == "YES"

        def backup_ok(h: HoldingInput) -> bool:
            if own(h):
                return False
            return policy.get(h.owner_name, "YES") in ("YES", "IF_NEEDED")

        def take_from(pred, gap: Decimal, commit: bool) -> Tuple[List[FundingUse], Decimal]:
            uses: List[FundingUse] = []
            ranked = sorted((h for h in free if pred(h) and h.kind in order and left[h.asset_id] > 0
                             and (h.kind not in _LOCKED or g.goal_type == "RETIREMENT")),
                            key=lambda h: (order.index(h.kind), -left[h.asset_id]))
            for h in ranked:
                if gap <= 0:
                    break
                rate_h = _holding_return(h, tax)
                full = h.value * (_ONE + rate_h) ** years + _sip_value(h.monthly_sip, rate_h, years)
                available = left[h.asset_id] / h.value  # share of the holding still unused
                share = min(available, gap / full) if full > 0 else _ZERO
                take = share * h.value
                if take < Decimal("1000"):
                    continue
                gap -= share * full
                if commit:
                    left[h.asset_id] -= take
                uses.append(FundingUse(h.asset_id, h.label, h.owner_name, _round(take),
                                       (share * _HUNDRED).quantize(Decimal("0.01"))))
                cover[h.asset_id] = full
            return uses, max(gap, _ZERO)

        cover: Dict[int, Decimal] = {}  # what a whole holding grows to by the date
        # 2. own / allowed holdings
        uses, gap = take_from(own, gap, commit=True)
        applicable += len(uses)
        # 3. a SIP from the salary surplus
        rate = _blended(g.equity_pct_suggested)
        monthly_before = monthly_left
        sip = _ZERO
        if gap > 0 and monthly_left > 0:
            sip = min(_sip_needed(gap, rate, years), monthly_left)
            sip = sip.quantize(Decimal("100"), rounding=ROUND_HALF_UP) if sip >= 100 else sip.quantize(_RUPEE)
            sip = min(sip, monthly_left)
            gap = max(gap - _sip_value(sip, rate, years), _ZERO)
            monthly_left -= sip
        if gap <= max(Decimal("1000"), g.future_cost / Decimal("200")):
            gap = _ZERO  # within ₹1,000 / 0.5% — rounding, not a real gap
        shortfall = gap
        # 4. backup: "only if needed" money for what is still short (not applied)
        backup, after = take_from(backup_ok, gap, commit=False) if gap > 0 else ([], _ZERO)
        ranked: List[Tuple[int, str]] = []  # (rank, text): lower is better
        if shortfall > 0:
            from_own = sum((u.pct / _HUNDRED * cover[u.asset_id] for u in uses), _ZERO)
            base_gap = gap_before - from_own
            per_rupee = _stepped_value(_ONE, rate, years)
            if per_rupee > 0:
                start = _round(base_gap / per_rupee)
                fits = start <= monthly_before
                ranked.append((1 if fits else 6,
                               f"Start the SIP at {_inr(start)} a month and raise it 10% every year"
                               + (" — it fits what you can save today." if fits else
                                  f" (more than the {_inr(_round(monthly_before))} a month free today).")))
            for extra in range(1, 21):
                yrs = years + extra
                future = g.target_today * (_ONE + g.inflation_pct / _HUNDRED) ** yrs
                have = ((g.projected + from_own) * (_ONE + rate) ** extra + _sip_value(sip, rate, yrs))
                if have >= future:
                    ranked.append((2 if extra <= 3 else 5,
                                   f"Move the date {extra} year{'s' if extra > 1 else ''} later "
                                   f"({g.target_date.year + extra}) — your own money and SIP then cover it."))
                    break
            if backup and after <= 0:
                who = ", ".join(sorted({u.owner_name for u in backup}))
                ranked.append((3, f"Use {_inr(_round(sum((u.value for u in backup), _ZERO)))} of {who}'s savings "
                                  "for the rest — only if they agree."))
            affordable = (g.future_cost - shortfall) / (_ONE + g.inflation_pct / _HUNDRED) ** years
            if affordable >= g.target_today / Decimal("10"):
                ranked.append((4, f"Aim for {_inr(_round(affordable))} (today's money) instead of "
                                  f"{_inr(g.target_today)}."))
        options = [text for _, text in sorted(ranked, key=lambda r: r[0])]
        result = "COVERED" if shortfall <= 0 else "COVERED_WITH_BACKUP" if backup and after <= 0 else "SHORT"
        fund = _fund_for(years)
        lines: List[str] = []
        if g.have_now > 0:
            lines.append(f"Keep the {_inr(g.have_now)} already set aside for it.")
        if uses:
            used = sum((u.value for u in uses), _ZERO)
            lines.append(f"Use {_inr(used)} you already have — {_names(uses)}.")
        if sip > 0:
            lines.append(f"Start a SIP of {_inr(sip)} a month in {fund}.")
        if not lines:
            lines.append("Nothing free to use yet — see the fix below.")
        reached = g.future_cost - shortfall
        plans.append(GoalFunding(
            g.goal_id, g.name, _round(gap_before), tuple(uses), sip, tuple(backup), _round(shortfall),
            _round(after), result, tuple(options), tuple(lines), options[0] if options else "",
            min(100, int((reached / g.future_cost * _HUNDRED).to_integral_value())) if g.future_cost > 0 else 100,
            fund,
        ))
    by_goal = {gid: i for i, gid in enumerate(x.goal_id for x in goals)}
    plans.sort(key=lambda f: by_goal.get(f.goal_id, 0))
    sip_total = sum((f.sip_from_salary for f in plans), _ZERO)
    return FundingPlan(
        monthly_free=_round(max(inp.monthly_free, _ZERO)), sip_total=_round(sip_total),
        monthly_left=_round(monthly_left), emergency_reserved=_round(reserved), goals=tuple(plans),
        applicable=applicable,
    )
