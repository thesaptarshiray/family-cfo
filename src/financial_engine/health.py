"""
Financial health: a check-up of a family's (or one member's) money, worked
out from a snapshot of what is already recorded. Pure functions — the
Engine gathers the snapshot (`HealthInputs`), this module judges it.

The rules are common Indian personal-finance rules of thumb, not personal
advice: an emergency fund of six months' outgoings, saving 30%+ of income,
EMIs under ~35-40% of income, term life cover of about 10x annual income
plus loans, a family health policy, an equity share that suits the age and
risk appetite, low-cost (direct) mutual funds, no single stock dominating.
Every suggestion says what it is based on, and every projection lists its
assumptions (`ASSUMPTIONS`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Sequence, Tuple

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_RUPEE = Decimal("1")

GOOD, OK, ATTENTION, MISSING = "GOOD", "OK", "ATTENTION", "MISSING"
_STATUS_POINTS = {GOOD: Decimal("100"), OK: Decimal("60"), ATTENTION: Decimal("20")}

# Long-run yearly returns assumed for the projection (before tax unless
# said otherwise), and the other numbers the checks use.
EQUITY_RETURN = Decimal("0.11")
DEBT_RETURN = Decimal("0.07")
EPF_RETURN = Decimal("0.0825")
GOLD_RETURN = Decimal("0.08")
CASH_RETURN = Decimal("0.03")
POLICY_RETURN = Decimal("0.05")  # traditional endowment / money-back plans
INFLATION = Decimal("0.06")
TAX_SLAB = Decimal("0.30")  # FD interest is taxed at the slab rate
REGULAR_PLAN_EXTRA_COST = Decimal("0.008")  # regular vs direct expense ratio, yearly
PROJECTION_YEARS = 20

ASSUMPTIONS = (
    f"Equity (stocks, equity funds) grows {EQUITY_RETURN * 100:.0f}% a year; FDs and debt funds "
    f"{DEBT_RETURN * 100:.0f}%; EPF {EPF_RETURN * 100:.2f}%; gold {GOLD_RETURN * 100:.0f}%; "
    f"savings-bank cash {CASH_RETURN * 100:.0f}%; endowment-type policies {POLICY_RETURN * 100:.0f}%.",
    f"Inflation {INFLATION * 100:.0f}% a year — 'in today's money' divides by it.",
    f"FD interest taxed at {TAX_SLAB * 100:.0f}% (the top slab).",
    f"A regular-plan fund costs about {REGULAR_PLAN_EXTRA_COST * 100:.1f}% a year more than the same fund's direct plan.",
    "Running SIPs continue unchanged; nothing is withdrawn; loans run on their schedules.",
    "Fund type (equity / debt / hybrid / gold) is read from the scheme name.",
)


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class FundInput:
    name: str
    value: Decimal
    kind: str  # EQUITY / DEBT / HYBRID / GOLD (from the name, see `fund_kind`)
    regular_plan: bool


@dataclass(frozen=True)
class LoanInput:
    label: str
    outstanding: Decimal
    rate: Decimal  # annual %
    monthly_payment: Decimal
    is_home_loan: bool
    # what prepaying the suggested amount would do (the Engine works these out)
    prepay_amount: Decimal = _ZERO
    prepay_interest_saved: Decimal = _ZERO
    prepay_months_saved: int = 0
    balance_by_year: Tuple[Decimal, ...] = ()  # owed at the end of year 0, 1, 2, ...


@dataclass(frozen=True)
class PersonInput:
    """One family member, for planning person by person."""

    member_id: int
    name: str
    relationship: str
    age: Optional[int]
    life_stage: Optional[str]  # EARNING / RETIRED / DEPENDENT, if set
    heir_name: Optional[str]
    retirement_age: Optional[int]
    assets: Decimal  # current value of what they own
    fd_value: Decimal
    loans: Decimal
    monthly_income: Decimal
    policies: int
    policies_without_nominee: int
    has_health_cover: bool
    checks_done: Tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthInputs:
    as_of: date
    member_count: int
    ages: Tuple[int, ...]  # of members with a date of birth
    risk_tolerance: Optional[str]  # CONSERVATIVE / MODERATE / AGGRESSIVE, if set
    monthly_income: Decimal
    monthly_expenses: Decimal  # entered rows + premiums due this month (not EMIs)
    monthly_emi: Decimal
    monthly_investing: Decimal  # SIPs and other investment rows
    expense_rows: int
    annual_premiums: Decimal
    cash: Decimal  # bank balances
    funds: Tuple[FundInput, ...]
    stocks_value: Decimal
    largest_stock: Optional[str]
    largest_stock_pct: Decimal  # of the stock portfolio
    fd_value: Decimal
    fd_avg_rate: Decimal  # annual %, weighted by principal
    epf_ppf_value: Decimal
    nps_equity: Decimal
    nps_debt: Decimal
    gold_value: Decimal
    policy_savings_value: Decimal  # surrender value + bonus of endowment / ULIP
    savings_policy_count: int
    term_cover: Decimal
    health_policies: int
    loans: Tuple[LoanInput, ...]
    # The family's own planning preferences (see `planning`): FD tax slab,
    # emergency-fund months, and FDs that must not be suggested for moving
    # (earmarked as income / emergency / keep, or for a goal due soon).
    tax_slab: Decimal = TAX_SLAB
    emergency_months: int = 6
    protected_fd: Decimal = Decimal("0")
    funds_needing_details: int = 0
    stocks_at_statement_price: int = 0
    people: Tuple[PersonInput, ...] = ()


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class HealthCheck:
    key: str
    title: str
    status: str  # GOOD / OK / ATTENTION / MISSING
    value: str  # the measured figure, ready to show
    target: str
    detail: str
    weight: int


@dataclass(frozen=True)
class HealthAction:
    priority: int  # 1 first
    title: str
    why: str
    steps: Tuple[str, ...]
    yearly_impact: Optional[Decimal] = None  # ₹ a year, estimated
    impact_note: str = ""
    key: str = ""  # stable id, for "not for me" on the personal plan


@dataclass(frozen=True)
class MixSlice:
    name: str  # Equity / Debt & FDs / Gold / Cash / Insurance savings
    value: Decimal
    pct: Decimal


@dataclass(frozen=True)
class ProjectionPoint:
    year: int
    as_is: Decimal
    improved: Decimal
    as_is_today: Decimal  # in today's money
    improved_today: Decimal


@dataclass(frozen=True)
class ChecklistItem:
    item: str
    label: str
    done: bool
    hint: str = ""


@dataclass(frozen=True)
class PersonPlan:
    member_id: int
    name: str
    age: Optional[int]
    life_stage: Optional[str]
    stage_label: str
    heir_name: Optional[str]
    net_worth: Decimal
    advice: Tuple[str, ...]
    checklist: Tuple[ChecklistItem, ...]
    inherits: Decimal  # what comes to this person from members who name them as heir (today's value)


@dataclass(frozen=True)
class InheritanceFlow:
    from_name: str
    to_name: str
    value: Decimal  # today's net worth of the person it comes from


@dataclass(frozen=True)
class RetirementView:
    name: str
    age: int
    retirement_age: int
    years: int
    as_is: Decimal
    improved: Decimal
    as_is_today: Decimal
    improved_today: Decimal


@dataclass(frozen=True)
class FinancialHealth:
    as_of: date
    score: int
    verdict: str
    net_worth: Decimal
    total_assets: Decimal
    total_loans: Decimal
    monthly_income: Decimal
    monthly_outgo: Decimal  # expenses + EMIs
    monthly_saving: Decimal  # income - outgo (what can be invested)
    savings_rate_pct: Optional[Decimal]
    mix: Tuple[MixSlice, ...]
    equity_pct: Decimal
    equity_target_pct: Decimal
    equity_target_basis: str
    checks: Tuple[HealthCheck, ...]
    actions: Tuple[HealthAction, ...]
    projection: Tuple[ProjectionPoint, ...]
    improved_changes: Tuple[str, ...]  # what the "improved" line assumes was done
    data_gaps: Tuple[str, ...]
    assumptions: Tuple[str, ...] = ASSUMPTIONS
    people: Tuple[PersonPlan, ...] = ()
    inheritance: Tuple[InheritanceFlow, ...] = ()
    retirements: Tuple[RetirementView, ...] = ()
    monthly_investing: Decimal = _ZERO  # SIPs and investment rows a month
    cash_in_bank: Decimal = _ZERO


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

_DEBT_WORDS = ("liquid", "debt", "bond", "gilt", "money market", "overnight", "duration",
               "corporate", "banking & psu", "banking and psu", "credit risk", "floater",
               "treasury", "savings fund", "arbitrage")
_HYBRID_WORDS = ("balanced advantage", "hybrid", "equity savings", "multi asset", "asset allocator",
                 "dynamic asset", "balanced")


def fund_kind(scheme_name: str) -> str:
    """EQUITY / DEBT / HYBRID / GOLD, from the scheme's name."""
    n = scheme_name.lower()
    if "gold" in n or "silver" in n:
        return "GOLD"
    if any(w in n for w in _HYBRID_WORDS):
        return "HYBRID"
    if any(w in n for w in _DEBT_WORDS):
        return "DEBT"
    return "EQUITY"


def is_regular_plan(scheme_name: str) -> bool:
    n = scheme_name.lower()
    return "direct" not in n and ("regular" in n or "reg" in n.split())


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    if whole <= _ZERO:
        return _ZERO
    return (part / whole * _HUNDRED).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _inr(value: Decimal) -> str:
    """₹12.3 L / ₹1.25 Cr / ₹45,600 — for sentences."""
    v = abs(value)
    sign = "-" if value < 0 else ""
    if v >= Decimal("10000000"):
        return f"{sign}₹{(v / Decimal('10000000')).quantize(Decimal('0.01'))} Cr"
    if v >= Decimal("100000"):
        return f"{sign}₹{(v / Decimal('100000')).quantize(Decimal('0.1'))} L"
    return f"{sign}₹{v.quantize(_RUPEE):,}"


def _round(value: Decimal) -> Decimal:
    return value.quantize(_RUPEE, rounding=ROUND_HALF_UP)


# ----------------------------------------------------------------------
# The check-up
# ----------------------------------------------------------------------


def assess(inp: HealthInputs) -> FinancialHealth:
    equity_funds = sum((f.value for f in inp.funds if f.kind == "EQUITY"), _ZERO)
    debt_funds = sum((f.value for f in inp.funds if f.kind == "DEBT"), _ZERO)
    hybrid_funds = sum((f.value for f in inp.funds if f.kind == "HYBRID"), _ZERO)
    gold_funds = sum((f.value for f in inp.funds if f.kind == "GOLD"), _ZERO)
    half = Decimal("0.5")
    equity = inp.stocks_value + equity_funds + hybrid_funds * half + inp.nps_equity
    debt = inp.fd_value + debt_funds + hybrid_funds * half + inp.epf_ppf_value + inp.nps_debt
    gold = inp.gold_value + gold_funds
    cash = inp.cash
    policies = inp.policy_savings_value
    assets = equity + debt + gold + cash + policies
    loans = sum((l.outstanding for l in inp.loans), _ZERO)
    investable = equity + debt + gold  # what an asset mix is about (not cash, not policies)

    mix = tuple(
        MixSlice(name, _round(value), _pct(value, assets))
        for name, value in (("Equity", equity), ("Debt & FDs", debt), ("Gold", gold),
                            ("Bank cash", cash), ("Insurance savings", policies))
        if value > _ZERO
    )

    outgo = inp.monthly_expenses + inp.monthly_emi
    saving = inp.monthly_income - outgo
    savings_rate = _pct(saving, inp.monthly_income) if inp.monthly_income > _ZERO else None
    equity_pct = _pct(equity, investable)

    # The equity share that suits: 100 - age (kept within 30-75%), or by
    # risk appetite, else a middle-of-the-road 50%.
    earner_ages = tuple(pp.age for pp in inp.people if pp.life_stage == "EARNING" and pp.age is not None)
    planning_ages = earner_ages or inp.ages
    if planning_ages:
        age = sorted(planning_ages)[len(planning_ages) // 2]
        target = Decimal(max(30, min(75, 100 - age)))
        whose = "the earning member" if earner_ages else "age"
        basis = f"100 minus {whose}'s age ({age}), kept between 30% and 75%"
    elif inp.risk_tolerance:
        target = {"CONSERVATIVE": Decimal("35"), "MODERATE": Decimal("55"),
                  "AGGRESSIVE": Decimal("70")}[inp.risk_tolerance]
        basis = f"a {inp.risk_tolerance.lower()} risk appetite"
    else:
        target = Decimal("50")
        basis = "a middle-of-the-road 50% (add dates of birth or a risk tolerance to tailor it)"

    checks: List[HealthCheck] = []
    actions: List[HealthAction] = []
    gaps: List[str] = []

    slab = inp.tax_slab
    want = inp.emergency_months

    # 1. Emergency fund — bank cash plus FDs (breakable at short notice)
    liquid = cash + inp.fd_value + debt_funds
    monthly_need = outgo if outgo > _ZERO else None
    if monthly_need is None:
        checks.append(HealthCheck("emergency", "Emergency fund", MISSING, "—", f"{want} months of outgoings",
                                  "Add your monthly expenses on the Monthly Cash Flow tab to check this.", 3))
    else:
        months = (liquid / monthly_need).quantize(Decimal("0.1"))
        status = GOOD if months >= want else OK if months >= Decimal(want) / 2 else ATTENTION
        checks.append(HealthCheck(
            "emergency", "Emergency fund", status,
            "over 2 years" if months > 24 else f"{months} months",
            f"{want} months of outgoings",
            f"Bank balances, FDs and debt funds ({_inr(liquid)}) against {_inr(monthly_need)} of monthly outgoings.", 3))
        if status != GOOD:
            gap = monthly_need * want - liquid
            actions.append(HealthAction(1, f"Build the emergency fund to {want} months",
                                        f"It covers {months} months of outgoings today.",
                                        (f"Park {_inr(gap)} more in a savings account, sweep-in FD or liquid fund.",
                                         "Keep it separate from investments; use it only for emergencies."),
                                        key="emergency-fund"))

    # 2. Savings rate
    if savings_rate is None:
        checks.append(HealthCheck("savings", "Savings rate", MISSING, "—", "30% or more of income",
                                  "Add your income on the Monthly Cash Flow tab.", 3))
    else:
        status = GOOD if savings_rate >= 30 else OK if savings_rate >= 15 else ATTENTION
        checks.append(HealthCheck(
            "savings", "Savings rate", status, f"{savings_rate}%", "30% or more of income",
            f"{_inr(saving)} of {_inr(inp.monthly_income)} a month is left after expenses and EMIs"
            + (f"; {_inr(inp.monthly_investing)} of it already goes into SIPs." if inp.monthly_investing else "."), 3))
        idle = saving - inp.monthly_investing
        if saving > _ZERO and idle > inp.monthly_income * Decimal("0.10"):
            actions.append(HealthAction(
                3, "Put the monthly surplus to work automatically",
                f"About {_inr(idle)} a month is not invested anywhere yet.",
                (f"Start or raise SIPs by about {_inr(_round(idle * Decimal('0.7')))} a month, "
                 "keeping the rest as a buffer.",
                 "Step SIPs up 10% every year as income grows."),
                _round(idle * Decimal("0.7") * 12 * (EQUITY_RETURN - CASH_RETURN)),
                "extra growth in the first year versus leaving it in the bank", key="surplus-sip"))
        if inp.expense_rows <= 2:
            gaps.append(f"Only {inp.expense_rows} expense row(s) are entered — add groceries, utilities, "
                        "school fees etc. on Monthly Cash Flow so the savings rate is real.")

    # 3. EMI load
    if inp.monthly_income > _ZERO and inp.monthly_emi > _ZERO:
        load = _pct(inp.monthly_emi, inp.monthly_income)
        status = GOOD if load <= 35 else OK if load <= 50 else ATTENTION
        checks.append(HealthCheck("emi", "EMI load", status, f"{load}% of income", "under 35-40%",
                                  f"{_inr(inp.monthly_emi)} of EMIs / pre-EMI interest a month.", 2))

    # 4. Life cover
    earners = [pp for pp in inp.people if pp.life_stage == "EARNING"]
    earner_income = sum((pp.monthly_income for pp in earners), _ZERO)
    annual_income = (earner_income if earners and earner_income > _ZERO else inp.monthly_income) * 12
    if annual_income > _ZERO:
        need = annual_income * 10 + loans
        status = GOOD if inp.term_cover >= need else OK if inp.term_cover >= need / 2 else ATTENTION
        checks.append(HealthCheck(
            "life", "Term life cover", status, _inr(inp.term_cover) if inp.term_cover else "None recorded",
            f"about {_inr(need)}", "10x annual income plus loans outstanding, so the family can repay the "
            "loans and replace the income.", 3))
        if status != GOOD:
            actions.append(HealthAction(
                1, "Get (or top up) a pure term life plan",
                f"Recorded term cover is {_inr(inp.term_cover)} against roughly {_inr(need)} needed.",
                (f"Buy term cover of about {_inr(need - inp.term_cover)} for the earning member(s), "
                 "running to age 60-65.",
                 "Term plans cost a fraction of endowment plans for the same cover.",
                 "If an existing term plan is being cancelled, keep the old one until the new one is issued."),
                key="life-cover"))
    else:
        gaps.append("Add income to judge how much life cover is needed.")

    # 5. Health cover
    status = GOOD if inp.health_policies > 0 else ATTENTION
    checks.append(HealthCheck("health", "Health insurance", status,
                              f"{inp.health_policies} polic{'y' if inp.health_policies == 1 else 'ies'}" if inp.health_policies else "None recorded",
                              "a family floater of ₹10-25 L", "One hospital stay can undo years of saving.", 3))
    if not inp.health_policies:
        actions.append(HealthAction(
            1, "Make sure the family has health insurance",
            "No health policy is recorded in the app.",
            ("If you have employer cover, add it on the Insurance tab — and consider a personal family floater "
             "(₹10-25 L, with a super top-up) that stays if you change jobs.",
             f"Cover parents separately if they are dependent ({inp.member_count} members are in the family)."),
            key="health-cover"))

    # 6. Asset mix
    if investable > _ZERO:
        diff = equity_pct - target
        status = GOOD if abs(diff) <= 10 else OK if abs(diff) <= 20 else ATTENTION
        checks.append(HealthCheck("mix", "Equity share", status, f"{equity_pct}% of investments",
                                  f"around {target}%", f"Target from {basis}.", 2))
        retired = [pp for pp in inp.people if pp.life_stage == "RETIRED" and pp.fd_value > 0]
        movable_fd = inp.fd_value - max(sum((pp.fd_value for pp in retired), _ZERO), inp.protected_fd)
        if diff < -10 and movable_fd > _ZERO:
            shift = _round(min(investable * (target - equity_pct) / _HUNDRED, movable_fd))
            fd_after_tax = inp.fd_avg_rate / _HUNDRED * (_ONE - slab) if inp.fd_avg_rate else DEBT_RETURN * (_ONE - slab)
            extra = _round(shift * (EQUITY_RETURN - fd_after_tax))
            actions.append(HealthAction(
                2, "Move gradually from FDs towards equity",
                f"Only {equity_pct}% of your investments are in equity against a {target}% guide; "
                f"FDs earn about {(fd_after_tax * 100).quantize(Decimal('0.1'))}% after tax — roughly inflation.",
                (f"Over 12-24 months, move about {_inr(shift)} as FDs mature into diversified equity funds "
                 "(index / flexi-cap), monthly through an STP — not in one go.",
                 "Keep the emergency fund and money needed within 3 years in FDs / debt funds.",
                 (f"FDs of {', '.join(pp.name for pp in retired)} (retired) are left out — they rightly stay in fixed income."
                  if retired else
                  "FDs of a retired or senior member can rightly stay in fixed income — mark members as retired "
                  "(Overview → edit member) and they are left out of this.")),
                extra, "more a year at the assumed returns, once moved", key="equity-shift"))

    # 7. FDs vs inflation
    if inp.fd_value > _ZERO and inp.fd_avg_rate > _ZERO:
        post_tax = (inp.fd_avg_rate * (_ONE - slab)).quantize(Decimal("0.01"))
        real = post_tax - INFLATION * _HUNDRED
        status = GOOD if real > 1 else OK if real > 0 else ATTENTION
        checks.append(HealthCheck("fd", "FD return after tax & inflation", status,
                                  f"{real:+.2f}% a year", "above 0%",
                                  f"{_inr(inp.fd_value)} in FDs at about {inp.fd_avg_rate.quantize(Decimal('0.01'))}% "
                                  f"= {post_tax}% after {slab * 100:.0f}% tax, against {INFLATION * 100:.0f}% inflation.", 1))

    # 8. Home loan vs FDs
    for loan in inp.loans:
        if loan.prepay_amount > _ZERO and loan.prepay_interest_saved > _ZERO:
            fd_after_tax_pct = inp.fd_avg_rate * (_ONE - slab)
            actions.append(HealthAction(
                2, f"Prepay part of the {loan.label} from FDs",
                f"The loan costs {loan.rate}% while FDs earn about {fd_after_tax_pct.quantize(Decimal('0.1'))}% after tax.",
                (f"Prepaying {_inr(loan.prepay_amount)} saves about {_inr(loan.prepay_interest_saved)} of interest"
                 + (f" and ends the loan {loan.prepay_months_saved} months sooner." if loan.prepay_months_saved else "."),
                 "Try other amounts with 'What if I prepay?' on the Loans tab.",
                 "For a self-occupied home, interest up to ₹2 L a year is tax-deductible — weigh that before prepaying more."),
                _round(loan.prepay_amount * (loan.rate / _HUNDRED - fd_after_tax_pct / _HUNDRED)),
                "a year better off than keeping that money in FDs", key=f"prepay:{loan.label}"))

    # 9. Fund costs
    regular = [f for f in inp.funds if f.regular_plan]
    if inp.funds:
        regular_value = sum((f.value for f in regular), _ZERO)
        status = GOOD if not regular else OK if len(regular) <= len(inp.funds) // 3 else ATTENTION
        checks.append(HealthCheck("costs", "Fund costs", status,
                                  f"{len(regular)} of {len(inp.funds)} funds in regular plans",
                                  "direct plans", "A regular plan pays a distributor commission every year "
                                  "out of your returns.", 1))
        if regular:
            yearly = _round(regular_value * REGULAR_PLAN_EXTRA_COST)
            actions.append(HealthAction(
                2, "Move mutual funds to direct plans",
                f"{len(regular)} funds worth {_inr(regular_value)} are in regular plans.",
                ("Point new SIPs at the direct plan of the same fund straight away (no tax, no exit load).",
                 "Switch existing units gradually — units held over a year are long-term: up to ₹1.25 L of equity "
                 "gains a year is tax-free; mind exit loads in the first year."),
                yearly, "a year in commissions saved, growing with the portfolio", key="direct-plans"))

    # 10. Stock concentration
    if inp.stocks_value > _ZERO and inp.largest_stock:
        status = GOOD if inp.largest_stock_pct <= 15 else OK if inp.largest_stock_pct <= 25 else ATTENTION
        checks.append(HealthCheck("stocks", "Stock concentration", status,
                                  f"{inp.largest_stock} is {inp.largest_stock_pct}%", "no stock above ~15-20%",
                                  "A single company's bad news should not sink the portfolio.", 1))
        if status == ATTENTION:
            actions.append(HealthAction(
                3, f"Trim {inp.largest_stock}",
                f"It is {inp.largest_stock_pct}% of your shares.",
                ("Book part of it over time (mind short-term capital gains) and spread into an index fund.",),
                key=f"trim:{inp.largest_stock}"))

    # 11. Endowment-type policies
    if inp.savings_policy_count:
        checks.append(HealthCheck("policies", "Insurance as investment", OK,
                                  f"{inp.savings_policy_count} polic{'y' if inp.savings_policy_count == 1 else 'ies'}, "
                                  f"{_inr(inp.annual_premiums)} a year", "keep insurance and investing separate",
                                  "Endowment / money-back plans usually return 4-6% a year and give little cover.", 1))
        actions.append(HealthAction(
            4, "Review endowment policies before paying more into them",
            f"{_inr(inp.annual_premiums)} a year goes into insurance premiums.",
            ("Don't surrender in a hurry: compare the surrender value (Insurance tab) with what continuing pays at maturity.",
             "Making a policy 'paid-up' (stop paying, keep a reduced amount) is often better than surrendering.",
             "For new money: term plan for cover + mutual funds for growth."),
            key="policies-review"))

    # 12. Gold
    if investable > _ZERO:
        gold_pct = _pct(gold, investable)
        status = GOOD if 5 <= gold_pct <= 15 else OK
        checks.append(HealthCheck("gold", "Gold", status, f"{gold_pct}% of investments", "5-10%",
                                  "A hedge for bad years; Sovereign Gold Bonds also pay 2.5% interest.", 1))

    # Data that would sharpen the check-up
    if not inp.ages:
        gaps.append("Add dates of birth (Overview → members) to tailor the equity share and retirement numbers.")
    if inp.funds_needing_details:
        gaps.append(f"{inp.funds_needing_details} mutual fund(s) still need purchase details for their XIRR (MF tab).")
    if inp.stocks_at_statement_price:
        gaps.append(f"{inp.stocks_at_statement_price} stock(s) carry the CAS price, not your buy price (Stocks tab → Edit).")

    # Score
    scored = [c for c in checks if c.status != MISSING]
    total_weight = sum(c.weight for c in scored)
    score = int((sum((_STATUS_POINTS[c.status] * c.weight for c in scored), _ZERO) / total_weight)
                .quantize(_ONE, rounding=ROUND_HALF_UP)) if total_weight else 0
    verdict = ("In good shape — fine-tune and keep going." if score >= 75
               else "A solid base, with a few important gaps to close." if score >= 50
               else "Some urgent gaps — start with the first actions below.")

    # Projection: as things are vs with the suggested changes
    loans_by_year = [l.balance_by_year for l in inp.loans]

    def owed(year: int, with_prepay: bool) -> Decimal:
        total = _ZERO
        for loan, series in zip(inp.loans, loans_by_year):
            if series:
                value = series[min(year, len(series) - 1)]
                if with_prepay and loan.prepay_amount > _ZERO:
                    # approximate: the prepaid sum comes straight off the balance
                    value = max(value - loan.prepay_amount, _ZERO)
                total += value
            else:
                total += loan.outstanding
        return total

    yearly_sip = inp.monthly_investing * 12
    shift = _ZERO
    retired_fd = sum((pp.fd_value for pp in inp.people if pp.life_stage == "RETIRED"), _ZERO)
    if investable > _ZERO and equity_pct < target - 10:
        shift = min(investable * (target - equity_pct) / _HUNDRED,
                    max(inp.fd_value - max(retired_fd, inp.protected_fd), _ZERO))
    prepay_total = sum((l.prepay_amount for l in inp.loans), _ZERO)
    fd_rate = (inp.fd_avg_rate / _HUNDRED) if inp.fd_avg_rate else DEBT_RETURN
    regular_value = sum((f.value for f in regular), _ZERO)
    fund_equity = equity_funds + hybrid_funds * half
    regular_share = regular_value / (fund_equity + debt_funds) if (fund_equity + debt_funds) > 0 else _ZERO

    def grow(eq, de, fd, ep, go, ca, po, years, eq_rate, fd_net):
        return (eq * (_ONE + eq_rate) ** years + de * (_ONE + DEBT_RETURN) ** years
                + fd * (_ONE + fd_net) ** years + ep * (_ONE + EPF_RETURN) ** years
                + go * (_ONE + GOLD_RETURN) ** years + ca * (_ONE + CASH_RETURN) ** years
                + po * (_ONE + POLICY_RETURN) ** years)

    fd_net = fd_rate * (_ONE - slab)
    other_debt = debt_funds + hybrid_funds * half + inp.nps_debt
    eq_now_cost = EQUITY_RETURN - REGULAR_PLAN_EXTRA_COST * regular_share
    to_retire = [
        (pp, (pp.retirement_age or 60) - pp.age) for pp in inp.people
        if pp.life_stage == "EARNING" and pp.age is not None and (pp.retirement_age or 60) > pp.age
    ]
    horizon = max([PROJECTION_YEARS] + [min(years, 40) for _, years in to_retire])
    points: List[ProjectionPoint] = []
    for year in range(horizon + 1):
        sip_fv = _ZERO
        if yearly_sip > 0 and year > 0:
            r = eq_now_cost
            sip_fv = yearly_sip * (((_ONE + r) ** year - _ONE) / r)
        as_is = grow(equity, other_debt, inp.fd_value, inp.epf_ppf_value, gold, cash, policies,
                     year, eq_now_cost, fd_net) + sip_fv - owed(year, False)
        moved = min(shift, max(inp.fd_value - prepay_total, _ZERO))
        sip_fv_better = yearly_sip * (((_ONE + EQUITY_RETURN) ** year - _ONE) / EQUITY_RETURN) if yearly_sip > 0 and year > 0 else _ZERO
        improved = grow(equity + moved, other_debt, max(inp.fd_value - moved - prepay_total, _ZERO),
                        inp.epf_ppf_value, gold, cash, policies, year, EQUITY_RETURN, fd_net) \
            + sip_fv_better - owed(year, True)
        deflate = (_ONE + INFLATION) ** year
        points.append(ProjectionPoint(
            year=year, as_is=_round(as_is), improved=_round(improved),
            as_is_today=_round(as_is / deflate), improved_today=_round(improved / deflate),
        ))
    changes = []
    if shift > 0:
        changes.append(f"about {_inr(min(shift, max(inp.fd_value - prepay_total, _ZERO)))} moved from FDs to equity")
    if prepay_total > 0:
        changes.append(f"{_inr(prepay_total)} of loan prepaid from FDs")
    if regular:
        changes.append("funds switched to direct plans")

    retirements = tuple(
        RetirementView(
            name=pp.name, age=pp.age, retirement_age=pp.retirement_age or 60, years=years,
            as_is=points[years].as_is, improved=points[years].improved,
            as_is_today=points[years].as_is_today, improved_today=points[years].improved_today,
        )
        for pp, years in to_retire if years <= horizon
    )
    flows = tuple(
        InheritanceFlow(pp.name, pp.heir_name, _round(pp.assets - pp.loans))
        for pp in inp.people if pp.heir_name and pp.assets - pp.loans > _ZERO
    )
    people = tuple(_person_plan(pp, inp, flows) for pp in inp.people)
    unknown_age = [pp.name for pp in inp.people if pp.age is None]
    unknown_stage = [pp.name for pp in inp.people if not pp.life_stage]
    if inp.people:
        gaps[:] = [g for g in gaps if not g.startswith("Add dates of birth")]
        if unknown_age:
            gaps.append(f"Add the date of birth of {', '.join(unknown_age)} (Overview → Edit) for age-based planning.")
        if unknown_stage:
            gaps.append(f"Mark {', '.join(unknown_stage)} as earning, retired or dependent (Overview → Edit).")

    return FinancialHealth(
        people=people, inheritance=flows, retirements=retirements,
        as_of=inp.as_of, score=score, verdict=verdict,
        net_worth=_round(assets - loans), total_assets=_round(assets), total_loans=_round(loans),
        monthly_income=_round(inp.monthly_income), monthly_outgo=_round(outgo),
        monthly_saving=_round(saving), savings_rate_pct=savings_rate,
        monthly_investing=_round(inp.monthly_investing), cash_in_bank=_round(cash),
        mix=mix, equity_pct=equity_pct, equity_target_pct=target, equity_target_basis=basis,
        checks=tuple(checks),
        actions=tuple(sorted(actions, key=lambda a: (a.priority, -(a.yearly_impact or _ZERO)))),
        projection=tuple(points), improved_changes=tuple(changes), data_gaps=tuple(gaps),
    )


# ----------------------------------------------------------------------
# Person by person
# ----------------------------------------------------------------------

STAGE_LABEL = {"EARNING": "Earning", "RETIRED": "Retired", "DEPENDENT": "Dependent", None: "Role not set"}
EDUCATION_COST_TODAY = Decimal("2500000")  # a professional degree in India, today's money
EDUCATION_INFLATION = Decimal("0.08")
_CHECKLIST = (
    ("NOMINEES_BANK", "Nominee on every bank account and FD"),
    ("NOMINEES_INVESTMENTS", "Nominee on demat, mutual fund folios, EPF / PPF / NPS"),
    ("NOMINEES_INSURANCE", "Nominee on every insurance policy"),
    ("WILL", "A registered or witnessed will"),
    ("JOINT_HOLDING", "Joint accounts set to 'either or survivor'"),
    ("PAPERS", "Family knows where papers, passwords and policy documents are"),
)


def _monthly_sip_for(target: Decimal, years: int, annual_return: Decimal) -> Decimal:
    if years <= 0:
        return _ZERO
    r = annual_return / Decimal("12")
    n = years * 12
    return _round(target * r / ((_ONE + r) ** n - _ONE))


def _person_plan(pp: PersonInput, inp: HealthInputs, flows: Sequence[InheritanceFlow]) -> PersonPlan:
    advice: List[str] = []
    stage = pp.life_stage
    senior = pp.age is not None and pp.age >= 60
    net = _round(pp.assets - pp.loans)
    incoming = sum((f.value for f in flows if f.to_name == pp.name), _ZERO)

    if stage == "EARNING":
        retire = pp.retirement_age or 60
        if pp.age is not None:
            if retire > pp.age:
                advice.append(f"{retire - pp.age} years to retirement at {retire} — the long runway is what makes "
                              "equity worthwhile; keep SIPs running through market falls.")
            else:
                advice.append("At or past the planned retirement age — move gradually towards income and safety.")
        advice.append("As the earner, your term cover and health cover protect everyone else — keep both in force.")
    elif stage == "RETIRED" or (stage is None and senior):
        advice.append("Capital safety and steady income come first: FDs, Senior Citizens' Savings Scheme "
                      "(up to ₹30 L, paid quarterly) and RBI floating-rate bonds suit this stage.")
        advice.append("Banks pay senior citizens about 0.25-0.5% more on FDs; up to ₹50,000 of interest a year is "
                      "deductible under section 80TTB (old regime). Submit Form 15H to avoid TDS if total income is below the taxable limit.")
        if pp.fd_value > _ZERO:
            advice.append(f"Holding the {_inr(pp.fd_value)} of FDs in their own name uses their (usually lower) tax slab — "
                          "keeping it there is often better than moving it to an earner's name.")
        if not pp.has_health_cover:
            advice.append("No health policy in their name is recorded: a senior-citizen health plan (or a top-up on "
                          "a family floater) matters most at this age.")
    elif stage == "DEPENDENT":
        if pp.age is not None and pp.age < 18:
            years = 18 - pp.age
            future = EDUCATION_COST_TODAY * (_ONE + EDUCATION_INFLATION) ** years
            sip = _monthly_sip_for(future, years, EQUITY_RETURN)
            advice.append(f"Higher education in about {years} years: a course costing {_inr(EDUCATION_COST_TODAY)} today "
                          f"may cost about {_inr(_round(future))} then. A dedicated SIP of about {_inr(sip)} a month "
                          "would build it (in the parent's name, earmarked"
                          + ("; Sukanya Samriddhi Yojana also suits a daughter)." if "daughter" in pp.relationship.lower() else ")."))
        elif pp.age is not None and pp.age < 25:
            advice.append("Education costs are close: keep that money in FDs / debt funds, not equity.")
        else:
            advice.append("Depends on the family's income: the earners' term and health cover should include them.")
        if pp.age is not None and pp.age >= 18 and pp.monthly_income == _ZERO:
            advice.append("An adult with little income: investments in their name are taxed at their (low) slab — "
                          "gifts from parents or spouse may be clubbed for tax, so check before moving money.")

    if pp.heir_name and net > _ZERO:
        advice.append(f"Passes to {pp.heir_name}: about {_inr(net)} today. Make sure {pp.heir_name} is the nominee "
                      "everywhere and named in a will — nominees collect, but the will decides who finally owns it.")
        advice.append(f"Once with {pp.heir_name}, FD interest is taxed at {pp.heir_name}'s slab — "
                      "plan then whether to keep it in FDs or move it to tax-efficient funds.")
    elif not pp.heir_name and net > _ZERO:
        advice.append("No heir is set: choose one (Overview → Edit) so the plan can show where this money goes.")
    if incoming > _ZERO:
        advice.append(f"Due to receive about {_inr(incoming)} (today's value) from family members who name them as heir; "
                      "plan for it, but don't count on its timing.")
    if pp.policies_without_nominee:
        advice.append(f"{pp.policies_without_nominee} of their {pp.policies} insurance polic"
                      f"{'y has' if pp.policies == 1 else 'ies have'} no nominee in the app.")
    if pp.age is None:
        advice.append("Add their date of birth to tailor this.")

    show_checklist = net > _ZERO or stage == "RETIRED" or pp.policies > 0
    checklist = tuple(
        ChecklistItem(
            item, label, item in pp.checks_done,
            hint=(f"{pp.policies_without_nominee} polic{'y' if pp.policies_without_nominee == 1 else 'ies'} without a nominee in the app"
                  if item == "NOMINEES_INSURANCE" and pp.policies_without_nominee else ""),
        )
        for item, label in _CHECKLIST
    ) if show_checklist else ()
    return PersonPlan(
        member_id=pp.member_id, name=pp.name, age=pp.age, life_stage=stage,
        stage_label=STAGE_LABEL.get(stage, "Role not set"), heir_name=pp.heir_name, net_worth=net,
        advice=tuple(advice), checklist=checklist, inherits=_round(incoming),
    )
