"""
Loan amortization: a month-by-month schedule worked out from a loan's terms
and its events (disbursements, prepayments, rate changes). Pure functions,
no persistence — the schedule is recomputed on every read, never stored.

The month is the unit. Month 1 is the calendar month of the loan's start
date; an event belongs to the calendar month its date falls in. Within a
month:

  - interest = opening principal x annual rate / 12, rounded to the paisa;
    money disbursed in month 1 counts as the opening principal (it earns
    interest from the start), a later disbursement is added at the month's
    end, so its interest starts the month after;
  - during the pre-EMI months (a construction-linked home loan's moratorium)
    only that interest is paid and the principal does not go down;
  - from the first EMI month a fixed EMI is paid: interest first, the rest
    repays principal. The EMI is the bank's figure when one is given,
    otherwise the standard reducing-balance EMI on the principal at that
    point over the months left of the tenure. It then stays fixed: a
    prepayment or a later disbursement changes how long the loan runs, not
    the EMI, and so does a rate change (what Indian banks do by default);
  - a prepayment comes off the principal at the month's end.

This matches the loan spreadsheet the Loans tab replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Dict, List, Optional, Sequence, Tuple

PRE_EMI = "PRE_EMI"
EMI = "EMI"

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_RUPEE = Decimal("1")
_MONTHLY = Decimal("1200")  # annual % -> monthly fraction
# However long prepayments stop and later disbursements extend a loan, the
# schedule never runs past the tenure (or the last disbursement) by more
# than this; `paid_off` is False if the balance is still owed then.
_OVERRUN_MONTHS = 360


@dataclass(frozen=True)
class ScheduleRow:
    month_no: int
    month: date  # first day of the calendar month
    phase: str  # PRE_EMI / EMI
    opening: Decimal
    disbursed: Decimal
    prepaid: Decimal
    rate: Decimal  # annual %, in force this month
    interest: Decimal
    payment: Decimal  # pre-EMI interest, or the EMI (the last one smaller)
    principal_repaid: Decimal  # by the EMI; prepayments are separate
    closing: Decimal


@dataclass(frozen=True)
class LoanSchedule:
    rows: Tuple[ScheduleRow, ...]
    emi: Optional[Decimal]  # None if the loan never reaches its EMI phase
    emi_calculated: bool  # True: worked out here, not the bank's figure
    first_emi_month_no: int
    paid_off: bool
    emi_below_interest: bool  # some month's EMI did not cover its interest


@dataclass(frozen=True)
class FinancialYearRow:
    """One Indian financial year (April-March) of the schedule — what the
    home-loan tax deductions (interest u/s 24(b), principal u/s 80C) go by."""

    label: str  # "FY 2025-26"
    start_year: int
    disbursed: Decimal
    interest: Decimal
    principal_repaid: Decimal
    prepaid: Decimal
    payment: Decimal
    closing: Decimal


def month_no_of(start: date, on: date) -> int:
    """The loan month `on` falls in: 1 for the start's own calendar month."""
    return (on.year - start.year) * 12 + (on.month - start.month) + 1


def month_start(start: date, month_no: int) -> date:
    index = start.month - 1 + month_no - 1
    return date(start.year + index // 12, index % 12 + 1, 1)


def compute_emi(principal: Decimal, annual_rate_pct: Decimal, months: int) -> Decimal:
    """Reducing-balance EMI, rounded up to the rupee (as banks quote it;
    rounding up means the last EMI never runs past the tenure)."""
    if principal <= _ZERO:
        return _ZERO
    months = max(months, 1)
    r = annual_rate_pct / _MONTHLY
    if r == _ZERO:
        emi = principal / Decimal(months)
    else:
        growth = (Decimal("1") + r) ** months
        emi = principal * r * growth / (growth - Decimal("1"))
    return emi.quantize(_RUPEE, rounding=ROUND_UP)


def _by_month(start: date, events: Sequence[Tuple[date, Decimal]]) -> Dict[int, Decimal]:
    totals: Dict[int, Decimal] = {}
    for on, amount in events:
        k = month_no_of(start, on)
        totals[k] = totals.get(k, _ZERO) + Decimal(amount)
    return totals


def compute_loan_schedule(
    *,
    start_date: date,
    tenure_months: int,
    pre_emi_months: int,
    initial_rate: Decimal,
    disbursements: Sequence[Tuple[date, Decimal]],
    prepayments: Sequence[Tuple[date, Decimal]] = (),
    rate_changes: Sequence[Tuple[date, Decimal]] = (),
    emi: Optional[Decimal] = None,
) -> LoanSchedule:
    """The schedule from month 1 until the principal is repaid (see the
    module docstring for the rules). Events dated before the start date
    count in month 1."""
    disbursed = _by_month(start_date, disbursements)
    prepaid = _by_month(start_date, prepayments)
    rates: Dict[int, Decimal] = {}
    for on, rate in sorted(rate_changes, key=lambda change: change[0]):
        rates[max(month_no_of(start_date, on), 1)] = Decimal(rate)
    for events in (disbursed, prepaid):
        early = sum((v for k, v in events.items() if k < 1), _ZERO)
        for k in [k for k in events if k < 1]:
            del events[k]
        if early:
            events[1] = events.get(1, _ZERO) + early
    last_disbursement = max(disbursed, default=1)
    limit = max(tenure_months, last_disbursement) + _OVERRUN_MONTHS

    rows: List[ScheduleRow] = []
    balance = _ZERO
    rate = Decimal(initial_rate)
    emi_value = None if emi is None else Decimal(emi)
    calculated = False
    below_interest = False
    first_emi = pre_emi_months + 1
    for k in range(1, limit + 1):
        rate = rates.get(k, rate)
        opening = balance
        new_money = disbursed.get(k, _ZERO)
        base = opening + new_money if k == 1 else opening
        interest = (base * rate / _MONTHLY).quantize(_CENT, rounding=ROUND_HALF_UP)
        if k < first_emi:
            phase, payment, repaid = PRE_EMI, interest, _ZERO
        else:
            phase = EMI
            if emi_value is None or (calculated and emi_value == _ZERO):
                emi_value = compute_emi(base, rate, tenure_months - pre_emi_months)
                calculated = True
            if base > _ZERO and emi_value < interest:
                below_interest = True
            payment = min(emi_value, base + interest)
            repaid = max(payment - interest, _ZERO)
        available = base - repaid + (new_money if k > 1 else _ZERO)
        prepay = min(prepaid.get(k, _ZERO), available)
        closing = (available - prepay).quantize(_CENT)
        rows.append(ScheduleRow(
            month_no=k, month=month_start(start_date, k), phase=phase,
            opening=opening, disbursed=new_money, prepaid=prepay, rate=rate,
            interest=interest, payment=payment, principal_repaid=repaid, closing=closing,
        ))
        balance = closing
        if closing == _ZERO and k >= last_disbursement:
            break

    return LoanSchedule(
        rows=tuple(rows),
        emi=emi_value,
        emi_calculated=calculated,
        first_emi_month_no=first_emi,
        paid_off=balance == _ZERO,
        emi_below_interest=below_interest,
    )


def financial_year_of(month: date) -> int:
    """The year an Indian financial year starts in (April-March)."""
    return month.year if month.month >= 4 else month.year - 1


def summarize_by_financial_year(rows: Sequence[ScheduleRow]) -> Tuple[FinancialYearRow, ...]:
    years: Dict[int, List[ScheduleRow]] = {}
    for row in rows:
        years.setdefault(financial_year_of(row.month), []).append(row)
    return tuple(
        FinancialYearRow(
            label=f"FY {year}-{str(year + 1)[2:]}",
            start_year=year,
            disbursed=sum((r.disbursed for r in group), _ZERO),
            interest=sum((r.interest for r in group), _ZERO),
            principal_repaid=sum((r.principal_repaid for r in group), _ZERO),
            prepaid=sum((r.prepaid for r in group), _ZERO),
            payment=sum((r.payment for r in group), _ZERO),
            closing=group[-1].closing,
        )
        for year, group in sorted(years.items())
    )
