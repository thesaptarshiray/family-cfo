"""
Insurance surrender value: a pure estimate, no persistence, no I/O.

Only the insurer can quote the exact figure — its Special Surrender Value
factors are proprietary — so everything here is an estimate built from the
published regulatory minimums, with every insurer-specific factor
overridable by the caller (from the policy document):

  - Traditional (ENDOWMENT): the insurer pays the higher of
      GSV = GSV factor x premiums paid + accrued bonus x bonus cash-value factor
      SSV = paid-up value x SSV factor,
            paid-up value = sum assured x installments paid / installments payable
                            + accrued bonus
    The default GSV factor follows the IRDAI (2013) non-linked scale; the
    default SSV factor discounts the paid-up value (payable at maturity) to
    today at `discount_rate_pct`. Nothing is payable until two full years'
    premiums have been paid.

  - ULIP: fund value minus the IRDAI (2010) discontinuance charge for the
    policy year, which depends on whether the annual premium is above
    Rs 25,000. Surrendered in the first 5 years, the money moves to the
    discontinued policy fund and is paid out only after the lock-in.

Decimal throughout, never `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Tuple

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_CENTS = Decimal("0.01")
_FACTOR = Decimal("0.0001")

SURRENDER_POLICY_TYPES = frozenset({"ENDOWMENT", "ULIP"})
DEFAULT_DISCOUNT_RATE_PCT = Decimal("7")
_ACQUIRE_YEARS = 2

# IRDAI (2010) ULIP discontinuance charges for policy years 1-4:
# (pct of the lower of annual premium and fund value, cap in rupees).
_ULIP_CHARGES_SMALL = ((20, 3000), (15, 2000), (10, 1500), (5, 1000))  # AP <= 25,000
_ULIP_CHARGES_LARGE = ((6, 6000), (4, 5000), (3, 4000), (2, 2000))     # AP > 25,000
_ULIP_SMALL_PREMIUM_LIMIT = Decimal("25000")


@dataclass(frozen=True)
class SurrenderEstimate:
    """The estimate and everything it was built from, so the page can show
    the working. Fields that do not apply to `method` are `None`."""

    method: str  # "TRADITIONAL" or "ULIP"
    policy_year: int
    estimate: Decimal
    # traditional
    premiums_paid: Optional[Decimal] = None
    installments_paid: Optional[int] = None
    installments_payable: Optional[int] = None
    gsv_factor_pct: Optional[Decimal] = None
    bonus_cash_value_factor: Optional[Decimal] = None
    gsv: Optional[Decimal] = None
    paid_up_value: Optional[Decimal] = None
    ssv_factor: Optional[Decimal] = None
    ssv: Optional[Decimal] = None
    # ULIP
    fund_value: Optional[Decimal] = None
    discontinuance_charge: Optional[Decimal] = None
    notes: Tuple[str, ...] = ()


def policy_year_on(start: date, as_of: date) -> int:
    """1 during the first year after `start`, 2 in the second, and so on."""
    years = as_of.year - start.year - ((as_of.month, as_of.day) < (start.month, start.day))
    return max(years, 0) + 1


def default_gsv_factor_pct(policy_year: int, term_years: int) -> Decimal:
    """IRDAI 2013 non-linked minimum GSV scale, as % of premiums paid: 0 in
    year 1, 30% in years 2-3, 50% in years 4-7, then rising linearly to 90%,
    which applies in the last two policy years."""
    if policy_year <= 1:
        return _ZERO
    if policy_year >= term_years - 1:
        return Decimal("90")
    if policy_year <= 3:
        return Decimal("30")
    if policy_year <= 7:
        return Decimal("50")
    span = (term_years - 1) - 7
    return (Decimal("50") + Decimal("40") * Decimal(policy_year - 7) / Decimal(span)).quantize(_CENTS)


def estimate_traditional(
    *,
    start_date: date,
    maturity_date: Optional[date],
    as_of: date,
    premium_amount: Decimal,
    installments_per_year: int,
    premium_paying_term_years: int,
    installments_paid: int,
    sum_assured: Optional[Decimal],
    accrued_bonus: Decimal,
    gsv_factor_pct: Optional[Decimal] = None,
    bonus_cash_value_factor: Optional[Decimal] = None,
    ssv_factor: Optional[Decimal] = None,
    discount_rate_pct: Optional[Decimal] = None,
) -> SurrenderEstimate:
    if premium_paying_term_years <= 0:
        raise ValueError("premium-paying term must be at least 1 year")
    if installments_paid < 0:
        raise ValueError("installments paid cannot be negative")
    notes = []
    year = policy_year_on(start_date, as_of)
    payable = premium_paying_term_years * installments_per_year
    paid = min(installments_paid, payable)
    premiums_paid = premium_amount * paid

    if Decimal(paid) < Decimal(_ACQUIRE_YEARS * installments_per_year):
        return SurrenderEstimate(
            method="TRADITIONAL", policy_year=year, estimate=_ZERO,
            premiums_paid=premiums_paid, installments_paid=paid, installments_payable=payable,
            notes=("No surrender value yet: at least 2 full years' premiums must be paid.",),
        )

    term_years = (
        policy_year_on(start_date, maturity_date) - 1 if maturity_date else premium_paying_term_years
    )
    if gsv_factor_pct is None:
        gsv_factor_pct = default_gsv_factor_pct(year, term_years)
        notes.append(f"GSV factor {gsv_factor_pct}% from the standard IRDAI scale for policy year {year}.")
    bonus_part = _ZERO
    if accrued_bonus > _ZERO:
        if bonus_cash_value_factor is None:
            notes.append("Bonus not included in GSV: enter the bonus cash-value factor from your policy document.")
        else:
            bonus_part = accrued_bonus * bonus_cash_value_factor
    gsv = (premiums_paid * gsv_factor_pct / _HUNDRED + bonus_part).quantize(_CENTS, ROUND_HALF_UP)

    if sum_assured is None:
        notes.append("Sum assured not recorded, so the paid-up value counts the bonus only.")
    paid_up = (
        (sum_assured or _ZERO) * Decimal(paid) / Decimal(payable) + accrued_bonus
    ).quantize(_CENTS, ROUND_HALF_UP)
    ssv: Optional[Decimal] = None
    if ssv_factor is None:
        if maturity_date is None:
            notes.append("SSV skipped: no maturity date, and no SSV factor given.")
        else:
            rate = discount_rate_pct if discount_rate_pct is not None else DEFAULT_DISCOUNT_RATE_PCT
            years_left = Decimal(max((maturity_date - as_of).days, 0)) / Decimal("365")
            ssv_factor = (Decimal(1) / (Decimal(1) + rate / _HUNDRED) ** years_left).quantize(_FACTOR)
            notes.append(
                f"SSV factor {ssv_factor} = paid-up value discounted {years_left.quantize(Decimal('0.1'))}"
                f" years at {rate}%; your insurer's own factor may differ."
            )
    if ssv_factor is not None:
        ssv = (paid_up * ssv_factor).quantize(_CENTS, ROUND_HALF_UP)

    estimate = max(gsv, ssv) if ssv is not None else gsv
    return SurrenderEstimate(
        method="TRADITIONAL", policy_year=year, estimate=estimate,
        premiums_paid=premiums_paid, installments_paid=paid, installments_payable=payable,
        gsv_factor_pct=gsv_factor_pct, bonus_cash_value_factor=bonus_cash_value_factor,
        gsv=gsv, paid_up_value=paid_up, ssv_factor=ssv_factor, ssv=ssv, notes=tuple(notes),
    )


def estimate_ulip(
    *, start_date: date, as_of: date, fund_value: Decimal, annual_premium: Decimal
) -> SurrenderEstimate:
    if fund_value < _ZERO:
        raise ValueError("fund value cannot be negative")
    year = policy_year_on(start_date, as_of)
    table = _ULIP_CHARGES_SMALL if annual_premium <= _ULIP_SMALL_PREMIUM_LIMIT else _ULIP_CHARGES_LARGE
    notes = []
    if year <= len(table):
        pct, cap = table[year - 1]
        charge = min(
            Decimal(pct) / _HUNDRED * min(annual_premium, fund_value), Decimal(cap)
        ).quantize(_CENTS, ROUND_HALF_UP)
        notes.append(f"Policy year {year}: discontinuance charge {pct}% (max Rs {cap}).")
    else:
        charge = _ZERO
        notes.append(f"Policy year {year}: no discontinuance charge.")
    if year <= 5:
        notes.append(
            "Within the 5-year lock-in the money moves to the discontinued policy fund "
            "and is paid out only after year 5."
        )
    return SurrenderEstimate(
        method="ULIP", policy_year=year, estimate=(fund_value - charge).quantize(_CENTS),
        fund_value=fund_value, discontinuance_charge=charge, notes=tuple(notes),
    )
