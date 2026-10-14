"""
A plain-language review of an insurance policy: what kind of plan it is,
its pros and cons, the choices open to its holder, and two numbers from the
policy's own terms — cover per rupee of premium, and (for traditional
LIC-style endowments) a rough estimate of the return.

Well-known plans are recognised from the plan name or insurer text (e.g.
"LIC Jeevan Anand 915"); anything else is reviewed by its type. This is
general product knowledge, not advice on the specific contract — the
policy document and the insurer's benefit illustration are what count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Sequence, Tuple

from .xirr import compute_xirr

_ZERO = Decimal("0")
_PER_YEAR = {"MONTHLY": 12, "QUARTERLY": 4, "ANNUAL": 1}
# LIC's simple reversionary bonus has mostly been ₹40-50 per ₹1,000 of sum
# assured a year on endowment plans; the estimate uses the middle of that
# and leaves out the final additional bonus (so it errs on the low side).
BONUS_PER_THOUSAND = Decimal("45")


@dataclass(frozen=True)
class PolicyReview:
    kind: str  # e.g. "Endowment with lifelong cover"
    known_plan: Optional[str]  # the recognised plan, if any
    summary: str
    pros: Tuple[str, ...]
    cons: Tuple[str, ...]
    options: Tuple[str, ...]  # what the holder can do
    cover_per_rupee: Optional[Decimal]  # sum assured / yearly premium
    estimated_return_pct: Optional[Decimal]
    estimated_return_note: str
    warnings: Tuple[str, ...]


_SAVINGS_OPTIONS = (
    "Continue to maturity if the policy is more than a few years old — early years carry most of the cost, so later premiums buy better value.",
    "Make it 'paid-up': stop paying, keep a reduced sum assured and the bonuses earned so far. Often better than surrendering.",
    "Surrender only after comparing the surrender value (Insurance tab → estimate) with the paid-up value and what continuing pays at maturity.",
    "Take a policy loan (usually up to ~90% of surrender value) for a short-term need instead of surrendering.",
    "Keep cover separate from investing for new money: a term plan for protection, mutual funds for growth.",
)

_TRADITIONAL_CONS = (
    "Returns usually work out to about 4-6% a year — close to inflation after the long lock-in.",
    "Cover is small for the premium paid: a term plan gives many times the cover for the same money.",
    "Leaving early costs dearly: surrender value in the first years is well below the premiums paid.",
    "Fixed payouts lose buying power over 15-25 years of inflation.",
)

# (keywords, name, kind, summary, pros, cons)
_CATALOGUE: Sequence[Tuple[Tuple[str, ...], str, str, str, Tuple[str, ...], Tuple[str, ...]]] = (
    (("jeevan anand",), "LIC Jeevan Anand", "Endowment with lifelong cover",
     "A participating endowment: the sum assured plus bonuses is paid at maturity, and a cover equal to the sum assured then continues for life (paid to the nominee on death).",
     ("Backed by LIC (Government of India guarantee).",
      "Bonuses are added every year and are guaranteed once declared.",
      "Cover continues for life after maturity at no extra premium.",
      "Maturity amount is tax-free under 10(10D) for most policies (premium within 10% of sum assured; from April 2023, not if total yearly premiums of such policies exceed ₹5 L).",
      "Loan against the policy is available."),
     _TRADITIONAL_CONS),
    (("jeevan labh",), "LIC Jeevan Labh", "Limited-premium endowment",
     "A participating endowment where premiums are paid for a shorter period than the policy term; sum assured plus bonuses at maturity.",
     ("Premiums stop well before maturity.", "Backed by LIC; bonuses guaranteed once declared.",
      "Tax-free maturity under 10(10D) within the limits.", "Loan facility."),
     _TRADITIONAL_CONS),
    (("new endowment", "jeevan lakshya", "bima jyoti", "jeevan azad", "dhan rekha"), "LIC endowment plan", "Endowment",
     "A savings-plus-insurance plan: a lump sum at maturity (with bonuses or guaranteed additions), or the sum assured to the nominee on death.",
     ("Backed by LIC.", "Disciplined, guaranteed-type savings.", "Loan facility and tax-free maturity within the limits."),
     _TRADITIONAL_CONS),
    (("jeevan umang",), "LIC Jeevan Umang", "Whole-life plan with yearly income",
     "Pay for a fixed term; then 8% of the sum assured is paid every year till age 100, with the sum assured and bonuses at the end.",
     ("Lifelong yearly income after the premium term.", "Backed by LIC; bonuses at maturity.",
      "Can be planned as income for retirement or for a dependent."),
     ("The fixed 8% income loses value to inflation over decades.",) + _TRADITIONAL_CONS[:3]),
    (("money back", "bima bachat", "jeevan tarun", "bima shree"), "Money-back plan", "Money-back endowment",
     "Pays a part of the sum assured at intervals during the term and the rest (with bonuses) at maturity.",
     ("Periodic payouts can be timed to goals.", "Backed by the insurer's guarantee."),
     ("Returns are usually the lowest of traditional plans (about 4-5% a year).",
      "Payouts are small and fixed; cover is low for the premium.",
      "Money paid out early earns nothing more inside the policy.")),
    (("value income", "guaranteed income", "assured income", "guaranteed savings",
      "guaranteed wealth", "smart income", "assured wealth"), "Guaranteed-income plan", "Non-participating guaranteed savings plan",
     "Premiums for a fixed term; then a guaranteed yearly income for a set number of years and/or a guaranteed lump sum. All amounts are fixed on day one.",
     ("Every payout is guaranteed and known upfront.", "Useful for a fixed future need (e.g. a child's fees in known years).",
      "Life cover during the premium term."),
     ("Guaranteed returns are typically about 5-6.5% a year — fixed even if interest rates rise.",
      "Payouts do not grow with inflation.",
      "Very costly to exit early.",
      "From April 2023, if total yearly premiums of such policies exceed ₹5 L, the gains are taxable.")),
    (("click 2 protect", "click2protect", "iprotect", "smart secure", "tech term", "saral jeevan bima", "jeevan amar",
      "e-shield", "eterm", "term plan", "protect elite", "sampoorna raksha supreme"), "Term plan", "Pure term insurance",
     "Pays the cover to the nominee if the insured dies during the term; nothing comes back otherwise (unless it is a 'return of premium' variant).",
     ("The cheapest way to get large life cover.", "Premium is fixed for the whole term when bought young.",
      "Premiums count under section 80C (old regime); the claim is tax-free."),
     ("No maturity value — it is protection, not savings.",
      "A lapsed term plan gives nothing back; keep the premium auto-paid.",
      "Cover bought today may be too small in 10-15 years; review it as income and loans grow.")),
)

_BY_TYPE = {
    "TERM": ("Pure term insurance", "Pays the cover only on death during the term.",
             ("Highest cover for the lowest premium.", "Tax-free claim; 80C on premium (old regime)."),
             ("No maturity value.", "Cover may need increasing as income and loans grow.")),
    "ENDOWMENT": ("Endowment", "Savings plus insurance: a lump sum at maturity or the sum assured on death.",
                  ("Guaranteed-type savings with life cover.", "Loan facility; tax-free maturity within the limits."),
                  _TRADITIONAL_CONS),
    "ULIP": ("Unit-linked plan (ULIP)", "Premiums buy market-linked fund units plus life cover; value moves with the markets.",
             ("Equity exposure with cover in one product.", "Switch between funds without tax.",
              "Tax-free maturity under 10(10D) if yearly premium is within ₹2.5 L (aggregate)."),
             ("Charges (premium allocation, mortality, fund management) eat into returns, especially early.",
              "5-year lock-in.", "Usually beaten by term + a direct mutual fund over the long run.")),
    "HEALTH": ("Health insurance", "Pays hospital bills up to the cover.",
               ("Protects savings from medical costs.", "Premium deductible under section 80D (old regime)."),
               ("Waiting periods for existing illnesses; room-rent limits; co-payment in senior plans.",
                "Cover from an employer ends with the job — keep a personal policy too.")),
    "MOTOR": ("Motor insurance", "Covers your vehicle and third-party liability.",
              ("Third-party cover is mandatory by law.",), ("Check the no-claim bonus before small claims.",)),
    "HOME": ("Home insurance", "Covers the building and/or contents against fire, theft, natural disasters.",
             ("Cheap protection for a large asset.",), ("Check the sum insured keeps up with rebuilding cost.",)),
}


def _estimate_endowment_return(
    sum_assured: Decimal, premium: Decimal, frequency: str, start: date, maturity: date
) -> Optional[Decimal]:
    """Yearly premiums from start to maturity against sum assured + simple
    reversionary bonus at maturity: a rough IRR, as a fraction."""
    years = maturity.year - start.year - ((maturity.month, maturity.day) < (start.month, start.day))
    if years < 3 or sum_assured <= _ZERO:
        return None
    yearly = premium * _PER_YEAR.get(frequency, 1)
    flows: List[Tuple[date, Decimal]] = []
    for k in range(years):
        on = date(start.year + k, start.month, min(start.day, 28)) if start.month == 2 else start.replace(year=start.year + k)
        flows.append((on, -yearly))
    maturity_value = sum_assured + sum_assured / Decimal("1000") * BONUS_PER_THOUSAND * years
    flows.append((maturity, maturity_value))
    return compute_xirr(flows)


def review_policy(
    *, policy_type: str, insurer: str, plan_name: Optional[str], sum_assured: Optional[Decimal],
    premium_amount: Decimal, premium_frequency: str, start_date: date, maturity_date: Optional[date],
    nominee: Optional[str], status: str,
) -> PolicyReview:
    text = f"{plan_name or ''} {insurer or ''}".lower()
    known = next((entry for entry in _CATALOGUE if any(k in text for k in entry[0])), None)
    if known:
        _, name, kind, summary, pros, cons = known
    else:
        name = None
        kind, summary, pros, cons = _BY_TYPE.get(policy_type, _BY_TYPE["ENDOWMENT"])
    is_savings = kind not in ("Pure term insurance", "Health insurance", "Motor insurance", "Home insurance")

    yearly = premium_amount * _PER_YEAR.get(premium_frequency, 1)
    cover_per_rupee = (sum_assured / yearly).quantize(Decimal("1"), rounding=ROUND_HALF_UP) if sum_assured and yearly > 0 else None

    est = None
    note = ""
    if (is_savings and maturity_date and sum_assured and name
            and name.startswith("LIC") and "income" not in kind.lower()):
        rate = _estimate_endowment_return(sum_assured, premium_amount, premium_frequency, start_date, maturity_date)
        if rate is not None:
            est = (rate * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            note = (f"Rough estimate: premiums every year to maturity, then the sum assured plus about "
                    f"₹{BONUS_PER_THOUSAND} of bonus per ₹1,000 of cover for each year (final additional bonus "
                    "left out). The insurer's benefit illustration has the real figures.")

    warnings = []
    if not (nominee or "").strip():
        warnings.append("No nominee recorded — add one, so the claim reaches the family without a court process.")
    if status == "LAPSED":
        warnings.append("The policy has lapsed: most insurers allow revival within 5 years with arrears and interest.")
    if is_savings and cover_per_rupee is not None and cover_per_rupee < 20:
        warnings.append(f"Only ₹{cover_per_rupee} of cover per ₹1 of yearly premium — it works mainly as savings, not protection.")

    return PolicyReview(
        kind=kind, known_plan=name, summary=summary, pros=tuple(pros), cons=tuple(cons),
        options=_SAVINGS_OPTIONS if is_savings else (
            "Keep it active (auto-pay the premium) and review the cover every few years.",),
        cover_per_rupee=cover_per_rupee, estimated_return_pct=est, estimated_return_note=note,
        warnings=tuple(warnings),
    )
