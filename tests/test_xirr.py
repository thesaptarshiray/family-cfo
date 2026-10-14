"""Tests for `financial_engine.xirr.compute_xirr`."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from financial_engine.xirr import _net_present_value, compute_xirr


def test_single_investment_and_payout_after_exactly_one_year():
    # 100000 -> 110000 over exactly 365 days is exactly 10% annualised.
    result = compute_xirr([
        (date(2025, 1, 1), Decimal("-100000")),
        (date(2026, 1, 1), Decimal("110000")),
    ])
    assert result == Decimal("0.10000")


def test_single_investment_and_payout_after_exactly_two_years():
    # 100000 -> 121000 over 730 days is exactly 10% annualised (compounded).
    # 2025-2026 span, deliberately avoiding a leap year so 2*365 == 730 exactly.
    result = compute_xirr([
        (date(2025, 1, 1), Decimal("-100000")),
        (date(2027, 1, 1), Decimal("121000")),
    ])
    assert result == Decimal("0.10000")


def test_a_loss_produces_a_negative_rate():
    result = compute_xirr([
        (date(2025, 1, 1), Decimal("-100000")),
        (date(2026, 1, 1), Decimal("90000")),
    ])
    assert result < 0
    assert result == Decimal("-0.10000")


def test_input_order_does_not_matter():
    forward = compute_xirr([
        (date(2025, 1, 1), Decimal("-100000")),
        (date(2026, 1, 1), Decimal("110000")),
    ])
    backward = compute_xirr([
        (date(2026, 1, 1), Decimal("110000")),
        (date(2025, 1, 1), Decimal("-100000")),
    ])
    assert forward == backward


# ----------------------------------------------------------------------
# Multi-cash-flow cases: verified against the equation itself, not a
# precomputed reference value — the defining property of a root is that
# plugging it back into the function gives (approximately) zero.
# ----------------------------------------------------------------------


def _assert_is_a_root(cash_flows, rate, tolerance=Decimal("1")):
    first_date = min(d for d, _ in cash_flows)
    relative = [((d - first_date).days, amount) for d, amount in cash_flows]
    npv = _net_present_value(relative, rate)
    assert abs(npv) < tolerance, f"NPV at {rate} was {npv}, not near zero"


def test_three_sip_installments_then_a_current_value():
    cash_flows = [
        (date(2025, 1, 15), Decimal("-5000")),
        (date(2025, 2, 15), Decimal("-5000")),
        (date(2025, 3, 15), Decimal("-5000")),
        (date(2025, 9, 1), Decimal("16500")),  # current value, "as if sold today"
    ]
    result = compute_xirr(cash_flows)
    assert result is not None
    _assert_is_a_root(cash_flows, result)


def test_many_monthly_sip_installments():
    start = date(2023, 1, 15)
    cash_flows = [(start + timedelta(days=30 * n), Decimal("-2000")) for n in range(24)]
    cash_flows.append((start + timedelta(days=30 * 24), Decimal("55000")))
    result = compute_xirr(cash_flows)
    assert result is not None
    _assert_is_a_root(cash_flows, result, tolerance=Decimal("5"))


def test_two_cash_flows_on_the_same_date_still_resolves():
    cash_flows = [
        (date(2025, 1, 1), Decimal("-50000")),
        (date(2025, 1, 1), Decimal("-50000")),
        (date(2026, 1, 1), Decimal("115000")),
    ]
    result = compute_xirr(cash_flows)
    assert result is not None
    _assert_is_a_root(cash_flows, result)


# ----------------------------------------------------------------------
# No-solution / insufficient-data cases: None, never an exception
# ----------------------------------------------------------------------


def test_fewer_than_two_cash_flows_is_none():
    assert compute_xirr([]) is None
    assert compute_xirr([(date(2025, 1, 1), Decimal("-1000"))]) is None


def test_all_outflows_is_none():
    result = compute_xirr([
        (date(2025, 1, 1), Decimal("-1000")),
        (date(2025, 6, 1), Decimal("-500")),
    ])
    assert result is None


def test_all_inflows_is_none():
    result = compute_xirr([
        (date(2025, 1, 1), Decimal("1000")),
        (date(2025, 6, 1), Decimal("500")),
    ])
    assert result is None


def test_zero_amounts_only_is_none():
    result = compute_xirr([
        (date(2025, 1, 1), Decimal("0")),
        (date(2025, 6, 1), Decimal("0")),
    ])
    assert result is None


# ----------------------------------------------------------------------
# Type and rounding
# ----------------------------------------------------------------------


def test_result_is_always_a_decimal():
    result = compute_xirr([
        (date(2025, 1, 1), Decimal("-100000")),
        (date(2026, 1, 1), Decimal("110000")),
    ])
    assert isinstance(result, Decimal)


def test_no_float_is_used_in_this_module():
    import ast
    import inspect

    from financial_engine import xirr as xirr_module

    tree = ast.parse(inspect.getsource(xirr_module))
    for node in ast.walk(tree):
        assert not (isinstance(node, ast.Name) and node.id == "float"), "float used"
        assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), (
            "float literal used"
        )
