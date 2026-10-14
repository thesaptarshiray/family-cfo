"""
What a consolidated account statement (CAS) says, as plain data.

The API layer reads the PDF into these (`api.cas_parser`); the Engine
compares them with what is stored and imports them (`statement_import`).
Nothing here reads files or touches the database. Amounts are as printed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Tuple


@dataclass(frozen=True)
class CasMfFolio:
    """A fund held in a folio with the fund house (statement of account)."""

    isin: str
    folio: str
    scheme_name: str
    units: Decimal
    avg_cost: Decimal
    total_cost: Decimal
    nav: Decimal
    value: Decimal
    amc: Optional[str] = None


@dataclass(frozen=True)
class CasMfTransaction:
    """One line of a folio's transactions in the statement period. `units`
    is negative for a redemption or switch out."""

    isin: str
    folio: str
    txn_date: date
    description: str
    amount: Decimal
    stamp_duty: Decimal
    nav: Decimal
    units: Decimal


@dataclass(frozen=True)
class CasDematAccount:
    depository: str  # NSDL / CDSL
    dp_name: str
    dp_id: str
    client_id: str


@dataclass(frozen=True)
class CasDematMf:
    """Fund units held in a demat account. The statement gives no cost."""

    account: CasDematAccount
    isin: str
    name: str  # the scheme, or just the fund house for CDSL accounts
    units: Decimal
    nav: Decimal
    value: Decimal


@dataclass(frozen=True)
class CasEquity:
    """Shares held in a demat account. The statement gives no buy price;
    `symbol` is missing for CDSL accounts."""

    account: CasDematAccount
    isin: str
    company: str
    quantity: Decimal
    price: Decimal
    value: Decimal
    symbol: Optional[str] = None
    exchange: Optional[str] = None


@dataclass(frozen=True)
class CasSgb:
    account: CasDematAccount
    isin: str
    coupon_rate: Decimal
    maturity_date: date
    units: Decimal  # 1 unit = 1 gram
    issue_price: Decimal
    market_price: Decimal
    value: Decimal


@dataclass(frozen=True)
class CasPolicy:
    """A life policy in the e-insurance account."""

    life_assured: str
    insurer: str
    plan_name: str
    policy_number: str
    status: str
    premium: Decimal
    frequency: str  # as printed: Monthly / Quarterly / Half-Yearly / Yearly
    sum_assured: Decimal


@dataclass(frozen=True)
class CasStatement:
    source: str  # "NSDL CAS"
    period_from: date
    period_to: date
    holder_name: str
    mf_folios: Tuple[CasMfFolio, ...] = ()
    mf_transactions: Tuple[CasMfTransaction, ...] = ()
    demat_mfs: Tuple[CasDematMf, ...] = ()
    equities: Tuple[CasEquity, ...] = ()
    sgbs: Tuple[CasSgb, ...] = ()
    policies: Tuple[CasPolicy, ...] = ()
