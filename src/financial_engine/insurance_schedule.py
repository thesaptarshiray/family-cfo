"""
Insurance premium schedule: a pure calculation, no persistence, no I/O.

Like FD (see `fd_valuation`), a policy's premium schedule is deterministic —
computable from its own terms (`start_date`, `premium_frequency`) as of any
date, with no need to look anything up.

Important naming distinction, matched by every field name here: this module
computes what the schedule *says* should be due by now, not what has
actually been paid. There is no payment-confirmation ledger yet (the schema
has `insurance_premium_payment` for that; wiring it up is a fast-follow, the
same way FD's actual interest-credit events were deferred). Everything here
is named `..._due_...`, never `..._paid_...`, on purpose — a field called
"installments paid" would claim to know something this module cannot.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

_INTERVAL_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "ANNUAL": 12}


@dataclass(frozen=True)
class PremiumSchedule:
    """Where a policy stands in its premium schedule, as of some date.

    `installments_due_so_far` counts due dates at or before `as_of`,
    starting from `start_date` itself (the first premium is due on the
    policy's start date). `next_due_date` is `None` once the policy has
    passed `maturity_date` or is not `ACTIVE` — there is nothing more to be
    due. `total_premium_due_to_date` is simply
    `installments_due_so_far * premium_amount`: a schedule projection, not a
    record of what was actually paid.
    """

    installments_due_so_far: int
    next_due_date: Optional[date]
    total_premium_due_to_date: Decimal


def _add_months(d: date, months: int) -> date:
    """`d` plus `months` calendar months, clamping the day to the target
    month's length (e.g. Jan 31 + 1 month -> Feb 28/29, never an error)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def compute_annual_premium(premium_amount: Decimal, premium_frequency: str) -> Decimal:
    """What the policy costs per year: `premium_amount` times the number of
    installments in twelve months (12 monthly, 4 quarterly, 1 annual)."""
    return premium_amount * Decimal(12 // _INTERVAL_MONTHS[premium_frequency])


def premium_due_dates_between(
    *,
    start_date: date,
    premium_frequency: str,
    maturity_date: Optional[date],
    first: date,
    last: date,
) -> "list[date]":
    """Every premium due date from `first` to `last` inclusive — e.g. one
    calendar month, for the "have you paid this month's premium?" check.
    Same rules as `compute_premium_schedule`: the first premium is due on
    `start_date`, and none is due on or after `maturity_date`."""
    interval = _INTERVAL_MONTHS[premium_frequency]
    dates = []
    n = 0
    while True:
        due = _add_months(start_date, n * interval)
        if due > last or (maturity_date is not None and due >= maturity_date):
            return dates
        if due >= first:
            dates.append(due)
        n += 1


def compute_premium_schedule(
    *,
    start_date: date,
    premium_amount: Decimal,
    premium_frequency: str,
    maturity_date: Optional[date],
    status: str,
    as_of: date,
) -> PremiumSchedule:
    """Return where the policy stands in its schedule as of `as_of`.

    No premium is treated as due on or after `maturity_date` — the last
    payable installment is strictly before it, on the reasoning that a
    policy does not collect a premium the same day it matures and pays out.
    That rule is applied consistently to both `installments_due_so_far` and
    `next_due_date`, not just the latter.
    """
    interval = _INTERVAL_MONTHS[premium_frequency]

    if as_of < start_date:
        due_so_far = 0
    else:
        # A due date is `_add_months(start_date, n * interval)` for
        # n = 0, 1, 2, .... That sequence is strictly increasing (each step
        # moves to a later month, and day-of-month clamping never makes a
        # later month's date earlier), so a starting estimate from the raw
        # month difference, corrected by a short bounded walk, always lands
        # on the exact count — including across day-of-month clamping (e.g.
        # a 31st-of-the-month start date, where a naive day comparison would
        # misfire against a 28/29/30-day month).
        months_elapsed = (as_of.year - start_date.year) * 12 + (as_of.month - start_date.month)
        n = max(0, months_elapsed // interval)
        while n > 0 and _add_months(start_date, n * interval) > as_of:
            n -= 1
        while _add_months(start_date, (n + 1) * interval) <= as_of:
            n += 1
        due_so_far = n + 1

    if maturity_date is not None:
        while due_so_far > 0 and _add_months(
            start_date, (due_so_far - 1) * interval
        ) >= maturity_date:
            due_so_far -= 1

    next_due = _add_months(start_date, due_so_far * interval)
    if status != "ACTIVE" or (maturity_date is not None and next_due >= maturity_date):
        next_due = None

    return PremiumSchedule(
        installments_due_so_far=due_so_far,
        next_due_date=next_due,
        total_premium_due_to_date=Decimal(due_so_far) * premium_amount,
    )
