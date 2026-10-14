"""
Stock position: a pure calculation, no persistence, no I/O.

Replays a holding's BUY/SELL transactions in date order using the
average-cost method (the usual way Indian brokers show "avg price"):

  - BUY adds the shares and their full cost (quantity x price + charges) to
    the cost basis;
  - SELL removes shares at the current average cost, and the difference
    between what the sale brought in (quantity x price - charges) and that
    removed cost is realised gain.

A SELL of more shares than are held at that point in time is rejected —
the Engine calls this after inserting a transaction, so an impossible sell
rolls the whole entry back. Never uses `float`, never touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Protocol, Sequence, Tuple

_ZERO = Decimal("0")
_CENTS = Decimal("0.01")
_PRICE_PRECISION = Decimal("0.0001")  # matches stock_holding.avg_buy_price's DECIMAL(18,4)


class StockTxnLike(Protocol):
    txn_date: date
    txn_type: str
    quantity: Decimal
    price: Decimal
    charges: Decimal


@dataclass(frozen=True)
class StockPosition:
    quantity: Decimal
    cost_basis: Decimal            # what the shares still held cost, charges included
    avg_buy_price: Optional[Decimal]  # cost_basis / quantity; None when nothing is held
    realised_gain: Decimal
    cash_flows: Tuple[Tuple[date, Decimal], ...]  # money out negative, money in positive (for XIRR)


def compute_stock_position(transactions: Sequence[StockTxnLike]) -> StockPosition:
    quantity = _ZERO
    cost = _ZERO
    realised = _ZERO
    flows: List[Tuple[date, Decimal]] = []
    for t in transactions:
        if t.txn_type == "BUY":
            outlay = t.quantity * t.price + t.charges
            quantity += t.quantity
            cost += outlay
            flows.append((t.txn_date, -outlay))
        elif t.txn_type == "SELL":
            if t.quantity > quantity:
                raise ValueError(
                    f"cannot sell {t.quantity} shares on {t.txn_date}: only {quantity} held then"
                )
            removed_cost = cost * t.quantity / quantity
            proceeds = t.quantity * t.price - t.charges
            realised += proceeds - removed_cost
            quantity -= t.quantity
            cost = cost - removed_cost if quantity != _ZERO else _ZERO
            flows.append((t.txn_date, proceeds))
        else:
            raise ValueError(f"unknown stock transaction type {t.txn_type!r}")

    cost_basis = cost.quantize(_CENTS, rounding=ROUND_HALF_UP)
    return StockPosition(
        quantity=quantity,
        cost_basis=cost_basis,
        avg_buy_price=(cost / quantity).quantize(_PRICE_PRECISION) if quantity != _ZERO else None,
        realised_gain=realised.quantize(_CENTS, rounding=ROUND_HALF_UP),
        cash_flows=tuple(flows),
    )
