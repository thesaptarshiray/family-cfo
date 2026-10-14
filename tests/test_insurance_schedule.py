"""Tests for `financial_engine.insurance_schedule.compute_premium_schedule`."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine.insurance_schedule import _add_months, compute_premium_schedule


def schedule(**overrides):
    fields = dict(
        start_date=date(2026, 1, 15),
        premium_amount=Decimal("1000"),
        premium_frequency="MONTHLY",
        maturity_date=None,
        status="ACTIVE",
        as_of=date(2026, 1, 15),
    )
    fields.update(overrides)
    return compute_premium_schedule(**fields)


# ----------------------------------------------------------------------
# _add_months: the day-clamping helper
# ----------------------------------------------------------------------


def test_add_months_within_the_same_year():
    assert _add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)
    assert _add_months(date(2026, 1, 15), 11) == date(2026, 12, 15)


def test_add_months_rolls_over_the_year():
    assert _add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert _add_months(date(2026, 1, 1), 24) == date(2028, 1, 1)


def test_add_months_clamps_the_day_to_the_target_months_length():
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)  # Feb 2026: not a leap year
    assert _add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)  # 2028 is a leap year
    assert _add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)


def test_add_months_with_zero_is_unchanged():
    assert _add_months(date(2026, 5, 20), 0) == date(2026, 5, 20)


# ----------------------------------------------------------------------
# Before the policy starts
# ----------------------------------------------------------------------


def test_before_start_date_nothing_is_due_yet():
    result = schedule(as_of=date(2025, 12, 1))
    assert result.installments_due_so_far == 0
    assert result.next_due_date == date(2026, 1, 15)  # the start date itself
    assert result.total_premium_due_to_date == Decimal("0")


# ----------------------------------------------------------------------
# On and around each due date (MONTHLY)
# ----------------------------------------------------------------------


def test_on_the_start_date_the_first_installment_is_due():
    result = schedule(as_of=date(2026, 1, 15))
    assert result.installments_due_so_far == 1
    assert result.next_due_date == date(2026, 2, 15)
    assert result.total_premium_due_to_date == Decimal("1000")


def test_the_day_before_a_due_date_does_not_count_it():
    result = schedule(as_of=date(2026, 1, 14))
    assert result.installments_due_so_far == 0
    assert result.next_due_date == date(2026, 1, 15)


def test_a_month_and_a_day_later_two_installments_are_due():
    result = schedule(as_of=date(2026, 2, 16))
    assert result.installments_due_so_far == 2
    assert result.next_due_date == date(2026, 3, 15)
    assert result.total_premium_due_to_date == Decimal("2000")


def test_month_end_start_date_does_not_skip_or_duplicate_a_due_date():
    # Jan 31 -> Feb's due date clamps to Feb 28 (2026 is not a leap year).
    result = schedule(start_date=date(2026, 1, 31), as_of=date(2026, 2, 28))
    assert result.installments_due_so_far == 2
    assert result.next_due_date == date(2026, 3, 31)


# ----------------------------------------------------------------------
# QUARTERLY / ANNUAL frequencies
# ----------------------------------------------------------------------


def test_quarterly_frequency():
    result = schedule(premium_frequency="QUARTERLY", as_of=date(2026, 7, 20))
    # due dates: Jan 15, Apr 15, Jul 15 -> 3 due by Jul 20
    assert result.installments_due_so_far == 3
    assert result.next_due_date == date(2026, 10, 15)
    assert result.total_premium_due_to_date == Decimal("3000")


def test_annual_frequency():
    result = schedule(premium_frequency="ANNUAL", as_of=date(2028, 6, 1))
    # due dates: 2026-01-15, 2027-01-15, 2028-01-15 -> 3 due by mid-2028
    assert result.installments_due_so_far == 3
    assert result.next_due_date == date(2029, 1, 15)


# ----------------------------------------------------------------------
# Maturity and non-active status stop the schedule
# ----------------------------------------------------------------------


def test_next_due_date_is_none_once_past_maturity():
    result = schedule(
        maturity_date=date(2026, 3, 15), as_of=date(2026, 3, 15)
    )
    # Mar 15 is the maturity date itself: no premium is due that day or
    # after, so only Jan and Feb count, even though as_of has reached Mar 15.
    assert result.installments_due_so_far == 2
    assert result.next_due_date is None


def test_next_due_date_is_none_before_maturity_if_the_next_one_lands_on_it():
    result = schedule(maturity_date=date(2026, 2, 15), as_of=date(2026, 1, 20))
    assert result.installments_due_so_far == 1
    assert result.next_due_date is None  # Feb 15 is the maturity date itself: no premium after


@pytest.mark.parametrize("status", ["LAPSED", "MATURED", "CLAIMED"])
def test_next_due_date_is_none_for_any_non_active_status(status):
    result = schedule(status=status, as_of=date(2026, 6, 1))
    assert result.next_due_date is None
    assert result.installments_due_so_far > 0  # past dues are still counted, just no next one


def test_active_policy_with_no_maturity_date_keeps_a_next_due_date():
    result = schedule(maturity_date=None, as_of=date(2030, 1, 1))
    assert result.next_due_date is not None


def test_installments_due_so_far_does_not_run_on_past_a_maturity_reached_long_ago():
    # A policy that matured years before as_of must not keep accumulating
    # "due" installments for years it was no longer active.
    result = schedule(
        start_date=date(2015, 1, 15), maturity_date=date(2020, 1, 15),
        as_of=date(2026, 6, 1),
    )
    assert result.installments_due_so_far == 60  # Jan 2015..Dec 2019, monthly: 60 installments
    assert result.next_due_date is None


# ----------------------------------------------------------------------
# total_premium_due_to_date is a schedule projection, not payment history
# ----------------------------------------------------------------------


def test_total_due_is_exactly_installments_times_premium_amount():
    result = schedule(premium_amount=Decimal("2549.99"), as_of=date(2026, 4, 15))
    assert result.installments_due_so_far == 4
    assert result.total_premium_due_to_date == Decimal("2549.99") * 4
