"""Tests for `financial_engine.stock_position` (average-cost method)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from financial_engine.stock_position import compute_stock_position


@dataclass
class T:
    txn_date: date
    txn_type: str
    quantity: Decimal
    price: Decimal
    charges: Decimal = Decimal("0")


def D(v):
    return Decimal(v)


def test_buys_average_their_cost_including_charges():
    pos = compute_stock_position([
        T(date(2025, 1, 1), "BUY", D("10"), D("100"), D("20")),   # 1020
        T(date(2025, 2, 1), "BUY", D("10"), D("120")),            # 1200
    ])
    assert pos.quantity == D("20")
    assert pos.cost_basis == D("2220.00")
    assert pos.avg_buy_price == D("111.0000")
    assert pos.realised_gain == D("0.00")
    assert pos.cash_flows == ((date(2025, 1, 1), D("-1020")), (date(2025, 2, 1), D("-1200")))


def test_a_sell_removes_average_cost_and_books_realised_gain():
    pos = compute_stock_position([
        T(date(2025, 1, 1), "BUY", D("10"), D("100")),
        T(date(2025, 2, 1), "BUY", D("10"), D("200")),   # avg 150
        T(date(2025, 3, 1), "SELL", D("5"), D("180"), D("10")),  # proceeds 890, cost 750
    ])
    assert pos.quantity == D("15")
    assert pos.cost_basis == D("2250.00")
    assert pos.avg_buy_price == D("150.0000")
    assert pos.realised_gain == D("140.00")
    assert pos.cash_flows[-1] == (date(2025, 3, 1), D("890"))


def test_selling_everything_leaves_nothing_held():
    pos = compute_stock_position([
        T(date(2025, 1, 1), "BUY", D("3"), D("100")),
        T(date(2025, 6, 1), "SELL", D("3"), D("90")),
    ])
    assert pos.quantity == D("0") and pos.cost_basis == D("0.00")
    assert pos.avg_buy_price is None
    assert pos.realised_gain == D("-30.00")


def test_selling_more_than_held_at_that_date_is_rejected():
    with pytest.raises(ValueError, match="cannot sell 5"):
        compute_stock_position([
            T(date(2025, 1, 1), "BUY", D("3"), D("100")),
            T(date(2025, 2, 1), "SELL", D("5"), D("100")),
        ])


def test_no_transactions_is_an_empty_position():
    pos = compute_stock_position([])
    assert pos.quantity == 0 and pos.avg_buy_price is None and pos.cash_flows == ()
