"""Persistence for generic investment assets and ownership shares."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple, Union

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

Money =Union[Decimal, int, str]
OwnershipInput = Tuple[int, Money]


@dataclass(frozen=True)
class Asset:
    """One row from ``asset``."""

    asset_id: int
    member_id: int
    category: str
    name: str
    institution: Optional[str]
    invested_amount: Decimal
    current_value: Decimal
    purchase_date: date
    status: str


@dataclass(frozen=True)
class MutualFundHolding:
    """Fields stored in ``mf_holding``."""

    asset_id: int
    amc: str
    scheme_name: str
    folio_number: str
    isin: Optional[str]
    units: Decimal
    avg_nav: Optional[Decimal]
    current_nav: Optional[Decimal]
    sip_enabled: bool
    sip_amount: Optional[Decimal]
    sip_date: Optional[int]
    sip_installments_done: Optional[int]
    broker: Optional[str] = None  # migration 010


@dataclass(frozen=True)
class StockHolding:
    """Fields stored in ``stock_holding``."""

    asset_id: int
    broker: str
    symbol: str
    exchange: str
    quantity: Decimal
    avg_buy_price: Optional[Decimal]
    current_price: Optional[Decimal]


@dataclass(frozen=True)
class FdHolding:
    """Fields stored in ``fd_holding``."""

    asset_id: int
    fd_number: str
    bank_name: str
    principal: Decimal
    interest_rate: Decimal
    issue_date: date
    maturity_date: date
    payout_type: str
    maturity_value: Optional[Decimal]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _asset_from_row(row: sqlite3.Row) -> Asset:
    return Asset(
        asset_id=row["asset_id"],
        member_id=row["member_id"],
        category=row["category"],
        name=row["name"],
        institution=row["institution"],
        invested_amount=_decimal(row["invested_amount"]),
        current_value=_decimal(row["current_value"]),
        purchase_date=date.fromisoformat(row["purchase_date"]),
        status=row["status"],
    )


def _optional_decimal(value: object) -> Optional[Decimal]:
    return None if value is None else _decimal(value)


def _mutual_fund_from_row(row: sqlite3.Row) -> MutualFundHolding:
    return MutualFundHolding(
        asset_id=row["asset_id"],
        amc=row["amc"],
        scheme_name=row["scheme_name"],
        folio_number=row["folio_number"],
        isin=row["isin"],
        units=_decimal(row["units"]),
        avg_nav=_optional_decimal(row["avg_nav"]),
        current_nav=_optional_decimal(row["current_nav"]),
        sip_enabled=bool(row["sip_enabled"]),
        sip_amount=_optional_decimal(row["sip_amount"]),
        sip_date=row["sip_date"],
        sip_installments_done=row["sip_installments_done"],
        broker=row["broker"],
    )


def _stock_from_row(row: sqlite3.Row) -> StockHolding:
    return StockHolding(
        asset_id=row["asset_id"],
        broker=row["broker"],
        symbol=row["symbol"],
        exchange=row["exchange"],
        quantity=_decimal(row["quantity"]),
        avg_buy_price=_optional_decimal(row["avg_buy_price"]),
        current_price=_optional_decimal(row["current_price"]),
    )


@dataclass(frozen=True)
class RetirementAccount:
    """Fields stored in ``retirement_account`` (EPF or PPF).

    The contribution columns hold running totals to date, as printed on the
    latest passbook/statement — replaced on each update, not added to.
    """

    asset_id: int
    account_type: str
    account_number: str
    employee_contribution: Decimal
    employer_contribution: Optional[Decimal]
    interest_rate: Decimal


def _retirement_from_row(row: sqlite3.Row) -> RetirementAccount:
    return RetirementAccount(
        asset_id=row["asset_id"],
        account_type=row["account_type"],
        account_number=row["account_number"],
        employee_contribution=_decimal(row["employee_contribution"]),
        employer_contribution=_optional_decimal(row["employer_contribution"]),
        interest_rate=_decimal(row["interest_rate"]),
    )


_RETIREMENT_SELECT = (
    "SELECT asset_id, account_type, account_number, employee_contribution, "
    "employer_contribution, interest_rate FROM retirement_account WHERE asset_id = ?"
)


@dataclass(frozen=True)
class GoldHolding:
    """Fields stored in ``gold_holding``: one purchase of gold."""

    asset_id: int
    form: str
    quantity_grams: Decimal
    purchase_rate_per_gram: Decimal
    sgb_interest_rate: Optional[Decimal]
    purity: str


def _gold_from_row(row: sqlite3.Row) -> GoldHolding:
    return GoldHolding(
        asset_id=row["asset_id"],
        form=row["form"],
        quantity_grams=_decimal(row["quantity_grams"]),
        purchase_rate_per_gram=_decimal(row["purchase_rate_per_gram"]),
        sgb_interest_rate=_optional_decimal(row["sgb_interest_rate"]),
        purity=row["purity"],
    )


_GOLD_SELECT = (
    "SELECT asset_id, form, quantity_grams, purchase_rate_per_gram, sgb_interest_rate, "
    "purity FROM gold_holding WHERE asset_id = ?"
)


@dataclass(frozen=True)
class NpsAccount:
    """Fields stored in ``nps_account``. Contributions are running totals
    from the latest statement; the three percentages are the asset mix."""

    asset_id: int
    pran: str
    tier: str
    employee_contribution: Decimal
    employer_contribution: Optional[Decimal]
    equity_pct: Decimal
    corporate_debt_pct: Decimal
    gov_bond_pct: Decimal


def _nps_from_row(row: sqlite3.Row) -> NpsAccount:
    return NpsAccount(
        asset_id=row["asset_id"],
        pran=row["pran"],
        tier=row["tier"],
        employee_contribution=_decimal(row["employee_contribution"]),
        employer_contribution=_optional_decimal(row["employer_contribution"]),
        equity_pct=_decimal(row["equity_pct"]),
        corporate_debt_pct=_decimal(row["corporate_debt_pct"]),
        gov_bond_pct=_decimal(row["gov_bond_pct"]),
    )


_NPS_SELECT = (
    "SELECT asset_id, pran, tier, employee_contribution, employer_contribution, "
    "equity_pct, corporate_debt_pct, gov_bond_pct FROM nps_account WHERE asset_id = ?"
)


def _fd_from_row(row: sqlite3.Row) -> FdHolding:
    return FdHolding(
        asset_id=row["asset_id"],
        fd_number=row["fd_number"],
        bank_name=row["bank_name"],
        principal=_decimal(row["principal"]),
        interest_rate=_decimal(row["interest_rate"]),
        issue_date=date.fromisoformat(row["issue_date"]),
        maturity_date=date.fromisoformat(row["maturity_date"]),
        payout_type=row["payout_type"],
        maturity_value=_optional_decimal(row["maturity_value"]),
    )


class AssetRepository(BaseRepository):
    """Create and read assets with atomic ownership rows.

    Every method takes an optional keyword `uow`. Without one, the call is its
    own transaction, as before. With one, the call joins that Unit of Work: it
    runs on the unit's connection and leaves committing to the caller.
    """

    def create_asset(
        self,
        member_id: int,
        category: str,
        name: str,
        institution: Optional[str],
        invested_amount: Money,
        purchase_date: date,
        ownership: Sequence[OwnershipInput],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Asset:
        """Create an asset and its complete ownership split atomically."""
        ownership_rows = [
            (owner_id, _decimal(ownership_pct))
            for owner_id, ownership_pct in ownership
        ]
        owner_ids = [owner_id for owner_id, _ in ownership_rows]
        if member_id not in owner_ids:
            raise ValueError("primary holder must have an ownership row")
        if len(owner_ids) != len(set(owner_ids)):
            raise ValueError("ownership members must be unique")
        if sum((share for _, share in ownership_rows), Decimal("0")) != Decimal("100"):
            raise ValueError("ownership percentages must sum to 100")

        with translate_db_errors():
            with self._db.scope(uow) as conn:
                cursor = conn.execute(
                    "INSERT INTO asset "
                    "(member_id, category, name, institution, invested_amount, "
                    "purchase_date) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        member_id,
                        category,
                        name,
                        institution,
                        str(_decimal(invested_amount)),
                        purchase_date.isoformat(),
                    ),
                )
                asset_id = cursor.lastrowid
                conn.executemany(
                    "INSERT INTO asset_ownership "
                    "(asset_id, member_id, ownership_pct) VALUES (?, ?, ?)",
                    [
                        (asset_id, owner_id, str(ownership_pct))
                        for owner_id, ownership_pct in ownership_rows
                    ],
                )
                row = conn.execute(
                    "SELECT asset_id, member_id, category, name, institution, "
                    "invested_amount, current_value, purchase_date, status "
                    "FROM asset WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
        return _asset_from_row(row)

    def create_mutual_fund(
        self,
        member_id: int,
        name: str,
        institution: Optional[str],
        invested_amount: Money,
        purchase_date: date,
        ownership: Sequence[OwnershipInput],
        *,
        amc: str,
        scheme_name: str,
        folio_number: str,
        isin: Optional[str] = None,
        current_nav: Optional[Money] = None,
        sip_enabled: bool = False,
        sip_amount: Optional[Money] = None,
        sip_date: Optional[int] = None,
        sip_installments_done: Optional[int] = None,
        broker: Optional[str] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, MutualFundHolding]:
        """Create an MF parent and its required detail row atomically."""
        ownership_rows = self._validate_ownership(member_id, ownership)
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                asset_id = self._insert_asset(
                    conn, member_id, "MF", name, institution,
                    invested_amount, purchase_date, ownership_rows,
                )
                conn.execute(
                    "INSERT INTO mf_holding "
                    "(asset_id, amc, scheme_name, folio_number, isin, current_nav, "
                    "sip_enabled, sip_amount, sip_date, sip_installments_done, broker) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        asset_id, amc, scheme_name, folio_number, isin,
                        None if current_nav is None else str(_decimal(current_nav)),
                        int(sip_enabled),
                        None if sip_amount is None else str(_decimal(sip_amount)),
                        sip_date, sip_installments_done, broker,
                    ),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = self._fetch_mutual_fund_in_connection(conn, asset_id)
        return _asset_from_row(asset_row), _mutual_fund_from_row(holding_row)

    def create_stock(
        self,
        member_id: int,
        name: str,
        institution: Optional[str],
        symbol: str,
        exchange: str,
        invested_amount: Money,
        purchase_date: date,
        ownership: Sequence[OwnershipInput],
        *,
        broker: str,
        current_price: Optional[Money] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, StockHolding]:
        """Create a stock parent and its required detail row atomically."""
        ownership_rows = self._validate_ownership(member_id, ownership)
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                asset_id = self._insert_asset(
                    conn, member_id, "STOCK", name, institution,
                    invested_amount, purchase_date, ownership_rows,
                )
                conn.execute(
                    "INSERT INTO stock_holding "
                    "(asset_id, broker, symbol, exchange, current_price) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        asset_id, broker, symbol, exchange,
                        None if current_price is None else str(_decimal(current_price)),
                    ),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = self._fetch_stock_in_connection(conn, asset_id)
        return _asset_from_row(asset_row), _stock_from_row(holding_row)

    def create_fd(
        self,
        member_id: int,
        name: str,
        institution: Optional[str],
        principal: Money,
        issue_date: date,
        ownership: Sequence[OwnershipInput],
        *,
        fd_number: str,
        bank_name: str,
        interest_rate: Money,
        maturity_date: date,
        payout_type: str,
        maturity_value: Optional[Money] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, FdHolding]:
        """Create an FD parent and its required detail row atomically.

        `principal` is stored both as the parent asset's `invested_amount`
        and as `fd_holding.principal` (the same fact for a single-lumpsum
        FD); `issue_date` likewise doubles as the parent's `purchase_date`.
        """
        ownership_rows = self._validate_ownership(member_id, ownership)
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                asset_id = self._insert_asset(
                    conn, member_id, "FD", name, institution,
                    principal, issue_date, ownership_rows,
                )
                conn.execute(
                    "INSERT INTO fd_holding "
                    "(asset_id, fd_number, bank_name, principal, interest_rate, "
                    "issue_date, maturity_date, payout_type, maturity_value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        asset_id, fd_number, bank_name, str(_decimal(principal)),
                        str(_decimal(interest_rate)), issue_date.isoformat(),
                        maturity_date.isoformat(), payout_type,
                        None if maturity_value is None else str(_decimal(maturity_value)),
                    ),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = self._fetch_fd_in_connection(conn, asset_id)
        return _asset_from_row(asset_row), _fd_from_row(holding_row)

    def create_retirement_account(
        self,
        member_id: int,
        name: str,
        institution: Optional[str],
        opened_date: date,
        ownership: Sequence[OwnershipInput],
        *,
        account_type: str,
        account_number: str,
        employee_contribution: Money,
        employer_contribution: Optional[Money],
        interest_rate: Money,
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, RetirementAccount]:
        """Create an EPF/PPF parent (category = `account_type`) and its
        `retirement_account` row atomically. The parent's `invested_amount`
        starts at 0; the Engine sets it from the contributions."""
        ownership_rows = self._validate_ownership(member_id, ownership)
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                asset_id = self._insert_asset(
                    conn, member_id, account_type, name, institution,
                    0, opened_date, ownership_rows,
                )
                conn.execute(
                    "INSERT INTO retirement_account "
                    "(asset_id, account_type, account_number, employee_contribution, "
                    "employer_contribution, interest_rate) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        asset_id, account_type, account_number,
                        str(_decimal(employee_contribution)),
                        None if employer_contribution is None
                        else str(_decimal(employer_contribution)),
                        str(_decimal(interest_rate)),
                    ),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = conn.execute(_RETIREMENT_SELECT, (asset_id,)).fetchone()
        return _asset_from_row(asset_row), _retirement_from_row(holding_row)

    def update_retirement_account(
        self,
        asset_id: int,
        *,
        employee_contribution: Money,
        employer_contribution: Optional[Money],
        interest_rate: Money,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[RetirementAccount]:
        """Replace the contribution totals and rate from a newer statement."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE retirement_account SET employee_contribution = ?, "
                    "employer_contribution = ?, interest_rate = ? WHERE asset_id = ?",
                    (
                        str(_decimal(employee_contribution)),
                        None if employer_contribution is None
                        else str(_decimal(employer_contribution)),
                        str(_decimal(interest_rate)),
                        asset_id,
                    ),
                )
                row = conn.execute(_RETIREMENT_SELECT, (asset_id,)).fetchone()
        return None if row is None else _retirement_from_row(row)

    def update_retirement_details(
        self,
        asset_id: int,
        *,
        member_id: int,
        name: str,
        institution: Optional[str],
        opened_date: date,
        account_number: str,
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """Correct an EPF/PPF account's owner, number, where it is held and
        when it was opened (its single, 100% ownership row follows the owner)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE asset SET member_id = ?, name = ?, institution = ?, purchase_date = ? "
                    "WHERE asset_id = ?",
                    (member_id, name, institution, opened_date.isoformat(), asset_id),
                )
                conn.execute("DELETE FROM asset_ownership WHERE asset_id = ?", (asset_id,))
                conn.execute(
                    "INSERT INTO asset_ownership (asset_id, member_id, ownership_pct) "
                    "VALUES (?, ?, '100')",
                    (asset_id, member_id),
                )
                conn.execute(
                    "UPDATE retirement_account SET account_number = ? WHERE asset_id = ?",
                    (account_number, asset_id),
                )

    def get_retirement_account(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[RetirementAccount]:
        row = self._fetch_one(_RETIREMENT_SELECT, (asset_id,), uow)
        return None if row is None else _retirement_from_row(row)

    def create_gold(
        self,
        member_id: int,
        name: str,
        institution: Optional[str],
        invested_amount: Money,
        purchase_date: date,
        ownership: Sequence[OwnershipInput],
        *,
        form: str,
        purity: str,
        quantity_grams: Money,
        purchase_rate_per_gram: Money,
        sgb_interest_rate: Optional[Money] = None,
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, GoldHolding]:
        """Create a GOLD parent and its `gold_holding` row atomically."""
        ownership_rows = self._validate_ownership(member_id, ownership)
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                asset_id = self._insert_asset(
                    conn, member_id, "GOLD", name, institution,
                    invested_amount, purchase_date, ownership_rows,
                )
                conn.execute(
                    "INSERT INTO gold_holding "
                    "(asset_id, form, quantity_grams, purchase_rate_per_gram, "
                    "sgb_interest_rate, purity) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        asset_id, form, str(_decimal(quantity_grams)),
                        str(_decimal(purchase_rate_per_gram)),
                        None if sgb_interest_rate is None else str(_decimal(sgb_interest_rate)),
                        purity,
                    ),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = conn.execute(_GOLD_SELECT, (asset_id,)).fetchone()
        return _asset_from_row(asset_row), _gold_from_row(holding_row)

    def get_gold(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[GoldHolding]:
        row = self._fetch_one(_GOLD_SELECT, (asset_id,), uow)
        return None if row is None else _gold_from_row(row)

    def create_nps_account(
        self,
        member_id: int,
        name: str,
        institution: Optional[str],
        opened_date: date,
        ownership: Sequence[OwnershipInput],
        *,
        pran: str,
        tier: str,
        employee_contribution: Money,
        employer_contribution: Optional[Money],
        equity_pct: Money,
        corporate_debt_pct: Money,
        gov_bond_pct: Money,
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, NpsAccount]:
        """Create an NPS parent and its `nps_account` row atomically."""
        ownership_rows = self._validate_ownership(member_id, ownership)
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                asset_id = self._insert_asset(
                    conn, member_id, "NPS", name, institution, 0, opened_date, ownership_rows,
                )
                conn.execute(
                    "INSERT INTO nps_account (asset_id, pran, tier, employee_contribution, "
                    "employer_contribution, equity_pct, corporate_debt_pct, gov_bond_pct) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        asset_id, pran, tier, str(_decimal(employee_contribution)),
                        None if employer_contribution is None
                        else str(_decimal(employer_contribution)),
                        str(_decimal(equity_pct)), str(_decimal(corporate_debt_pct)),
                        str(_decimal(gov_bond_pct)),
                    ),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = conn.execute(_NPS_SELECT, (asset_id,)).fetchone()
        return _asset_from_row(asset_row), _nps_from_row(holding_row)

    def update_nps_account(
        self,
        asset_id: int,
        *,
        employee_contribution: Money,
        employer_contribution: Optional[Money],
        equity_pct: Money,
        corporate_debt_pct: Money,
        gov_bond_pct: Money,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[NpsAccount]:
        """Replace contribution totals and asset mix from a newer statement."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE nps_account SET employee_contribution = ?, employer_contribution = ?, "
                    "equity_pct = ?, corporate_debt_pct = ?, gov_bond_pct = ? WHERE asset_id = ?",
                    (
                        str(_decimal(employee_contribution)),
                        None if employer_contribution is None
                        else str(_decimal(employer_contribution)),
                        str(_decimal(equity_pct)), str(_decimal(corporate_debt_pct)),
                        str(_decimal(gov_bond_pct)), asset_id,
                    ),
                )
                row = conn.execute(_NPS_SELECT, (asset_id,)).fetchone()
        return None if row is None else _nps_from_row(row)

    def get_nps_account(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[NpsAccount]:
        row = self._fetch_one(_NPS_SELECT, (asset_id,), uow)
        return None if row is None else _nps_from_row(row)

    def update_fd(
        self,
        asset_id: int,
        *,
        member_id: int,
        name: str,
        institution: Optional[str],
        fd_number: str,
        bank_name: str,
        principal: Money,
        interest_rate: Money,
        issue_date: date,
        maturity_date: date,
        payout_type: str,
        maturity_value: Optional[Money],
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, FdHolding]:
        """Correct an FD in place: its terms, its parent asset's identity
        fields, and — if the owner changed — its (single, 100%) ownership row.
        `invested_amount`/`current_value` are Engine caches, set separately."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE asset SET member_id = ?, name = ?, institution = ?, "
                    "purchase_date = ? WHERE asset_id = ?",
                    (member_id, name, institution, issue_date.isoformat(), asset_id),
                )
                conn.execute("DELETE FROM asset_ownership WHERE asset_id = ?", (asset_id,))
                conn.execute(
                    "INSERT INTO asset_ownership (asset_id, member_id, ownership_pct) "
                    "VALUES (?, ?, '100')",
                    (asset_id, member_id),
                )
                conn.execute(
                    "UPDATE fd_holding SET fd_number = ?, bank_name = ?, principal = ?, "
                    "interest_rate = ?, issue_date = ?, maturity_date = ?, payout_type = ?, "
                    "maturity_value = ? WHERE asset_id = ?",
                    (
                        fd_number, bank_name, str(_decimal(principal)), str(_decimal(interest_rate)),
                        issue_date.isoformat(), maturity_date.isoformat(), payout_type,
                        None if maturity_value is None else str(_decimal(maturity_value)),
                        asset_id,
                    ),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = self._fetch_fd_in_connection(conn, asset_id)
        return _asset_from_row(asset_row), _fd_from_row(holding_row)

    def update_stock(
        self,
        asset_id: int,
        *,
        member_id: int,
        name: str,
        broker: str,
        symbol: str,
        exchange: str,
        uow: Optional[UnitOfWork] = None,
    ) -> Tuple[Asset, StockHolding]:
        """Correct a stock holding's owner, name, broker, symbol or exchange
        (its single, 100% ownership row follows the owner)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE asset SET member_id = ?, name = ?, institution = ? WHERE asset_id = ?",
                    (member_id, name, broker, asset_id),
                )
                conn.execute("DELETE FROM asset_ownership WHERE asset_id = ?", (asset_id,))
                conn.execute(
                    "INSERT INTO asset_ownership (asset_id, member_id, ownership_pct) "
                    "VALUES (?, ?, '100')",
                    (asset_id, member_id),
                )
                conn.execute(
                    "UPDATE stock_holding SET broker = ?, symbol = ?, exchange = ? WHERE asset_id = ?",
                    (broker, symbol, exchange, asset_id),
                )
                asset_row = self._fetch_asset_in_connection(conn, asset_id)
                holding_row = self._fetch_stock_in_connection(conn, asset_id)
        return _asset_from_row(asset_row), _stock_from_row(holding_row)

    def update_mf_sip(
        self,
        asset_id: int,
        *,
        sip_enabled: bool,
        sip_amount: Optional[Money],
        sip_date: Optional[int],
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[MutualFundHolding]:
        """Set (or stop) a fund's running SIP: amount per month and debit day."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE mf_holding SET sip_enabled = ?, sip_amount = ?, sip_date = ? "
                    "WHERE asset_id = ?",
                    (
                        int(sip_enabled),
                        None if sip_amount is None else str(_decimal(sip_amount)),
                        sip_date, asset_id,
                    ),
                )
                row = self._fetch_mutual_fund_in_connection(conn, asset_id)
        return None if row is None else _mutual_fund_from_row(row)

    def update_mutual_fund(
        self,
        asset_id: int,
        *,
        member_id: int,
        scheme_name: str,
        folio_number: str,
        broker: Optional[str],
        uow: Optional[UnitOfWork] = None,
    ) -> None:
        """Correct a fund's owner, scheme name, folio or broker (its single,
        100% ownership row follows the owner)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE asset SET member_id = ?, name = ? WHERE asset_id = ?",
                    (member_id, scheme_name, asset_id),
                )
                conn.execute("DELETE FROM asset_ownership WHERE asset_id = ?", (asset_id,))
                conn.execute(
                    "INSERT INTO asset_ownership (asset_id, member_id, ownership_pct) "
                    "VALUES (?, ?, '100')",
                    (asset_id, member_id),
                )
                conn.execute(
                    "UPDATE mf_holding SET scheme_name = ?, folio_number = ?, broker = ? WHERE asset_id = ?",
                    (scheme_name, folio_number, broker, asset_id),
                )

    def update_mf_broker(
        self, asset_id: int, broker: Optional[str], *, uow: Optional[UnitOfWork] = None
    ) -> None:
        """Set (or clear, with None) the broker a fund is held through."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE mf_holding SET broker = ? WHERE asset_id = ?", (broker, asset_id)
                )

    def delete_asset(self, asset_id: int, *, uow: Optional[UnitOfWork] = None) -> None:
        """Delete an asset; its detail row, ownership and valuation history go
        with it (ON DELETE CASCADE). Assets referenced by transactions or
        goals are protected by ON DELETE RESTRICT and raise instead."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute("DELETE FROM asset WHERE asset_id = ?", (asset_id,))

    def get_asset(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[Asset]:
        row = self._fetch_one(
            "SELECT asset_id, member_id, category, name, institution, "
            "invested_amount, current_value, purchase_date, status "
            "FROM asset WHERE asset_id = ?",
            (asset_id,),
            uow,
        )
        return None if row is None else _asset_from_row(row)

    def list_assets(
        self, member_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[Asset]:
        rows = self._fetch_all(
            "SELECT asset_id, member_id, category, name, institution, "
            "invested_amount, current_value, purchase_date, status "
            "FROM asset WHERE member_id = ? ORDER BY asset_id",
            (member_id,),
            uow,
        )
        return [_asset_from_row(row) for row in rows]

    def list_ownership(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> List[Tuple[int, Decimal]]:
        rows = self._fetch_all(
            "SELECT member_id, ownership_pct FROM asset_ownership "
            "WHERE asset_id = ? ORDER BY member_id",
            (asset_id,),
            uow,
        )
        return [(row["member_id"], _decimal(row["ownership_pct"])) for row in rows]

    def get_mutual_fund(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[MutualFundHolding]:
        row = self._fetch_one(
            "SELECT asset_id, amc, scheme_name, folio_number, isin, units, avg_nav, "
            "current_nav, sip_enabled, sip_amount, sip_date, sip_installments_done, broker "
            "FROM mf_holding WHERE asset_id = ?",
            (asset_id,),
            uow,
        )
        return None if row is None else _mutual_fund_from_row(row)

    def get_stock(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[StockHolding]:
        row = self._fetch_one(
            "SELECT asset_id, broker, symbol, exchange, quantity, avg_buy_price, "
            "current_price FROM stock_holding WHERE asset_id = ?",
            (asset_id,),
            uow,
        )
        return None if row is None else _stock_from_row(row)

    def get_fd(
        self, asset_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[FdHolding]:
        row = self._fetch_one(
            "SELECT asset_id, fd_number, bank_name, principal, interest_rate, "
            "issue_date, maturity_date, payout_type, maturity_value "
            "FROM fd_holding WHERE asset_id = ?",
            (asset_id,),
            uow,
        )
        return None if row is None else _fd_from_row(row)

    @staticmethod
    def _validate_ownership(
        member_id: int, ownership: Sequence[OwnershipInput]
    ) -> List[OwnershipInput]:
        ownership_rows = [
            (owner_id, _decimal(ownership_pct))
            for owner_id, ownership_pct in ownership
        ]
        owner_ids = [owner_id for owner_id, _ in ownership_rows]
        if member_id not in owner_ids:
            raise ValueError("primary holder must have an ownership row")
        if len(owner_ids) != len(set(owner_ids)):
            raise ValueError("ownership members must be unique")
        if sum((share for _, share in ownership_rows), Decimal("0")) != Decimal("100"):
            raise ValueError("ownership percentages must sum to 100")
        return ownership_rows

    @staticmethod
    def _insert_asset(
        conn: sqlite3.Connection,
        member_id: int,
        category: str,
        name: str,
        institution: Optional[str],
        invested_amount: Money,
        purchase_date: date,
        ownership_rows: Sequence[OwnershipInput],
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO asset "
            "(member_id, category, name, institution, invested_amount, purchase_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                member_id, category, name, institution,
                str(_decimal(invested_amount)), purchase_date.isoformat(),
            ),
        )
        asset_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO asset_ownership "
            "(asset_id, member_id, ownership_pct) VALUES (?, ?, ?)",
            [
                (asset_id, owner_id, str(ownership_pct))
                for owner_id, ownership_pct in ownership_rows
            ],
        )
        return asset_id

    @staticmethod
    def _fetch_asset_in_connection(
        conn: sqlite3.Connection, asset_id: int
    ) -> sqlite3.Row:
        return conn.execute(
            "SELECT asset_id, member_id, category, name, institution, invested_amount, "
            "current_value, purchase_date, status FROM asset WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()

    @staticmethod
    def _fetch_mutual_fund_in_connection(
        conn: sqlite3.Connection, asset_id: int
    ) -> sqlite3.Row:
        return conn.execute(
            "SELECT asset_id, amc, scheme_name, folio_number, isin, units, avg_nav, "
            "current_nav, sip_enabled, sip_amount, sip_date, sip_installments_done, broker "
            "FROM mf_holding WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()

    @staticmethod
    def _fetch_stock_in_connection(
        conn: sqlite3.Connection, asset_id: int
    ) -> sqlite3.Row:
        return conn.execute(
            "SELECT asset_id, broker, symbol, exchange, quantity, avg_buy_price, "
            "current_price FROM stock_holding WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()

    @staticmethod
    def _fetch_fd_in_connection(
        conn: sqlite3.Connection, asset_id: int
    ) -> sqlite3.Row:
        return conn.execute(
            "SELECT asset_id, fd_number, bank_name, principal, interest_rate, "
            "issue_date, maturity_date, payout_type, maturity_value "
            "FROM fd_holding WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()