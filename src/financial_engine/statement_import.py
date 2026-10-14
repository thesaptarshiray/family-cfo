"""
Importing a consolidated account statement (CAS) — part of `FinancialEngine`
(mixed in), kept in its own module because it is long.

The same plan drives both steps: `preview_statement_import` works out, for
every holding in the statement, what importing it would do and shows that
without saving anything; `apply_statement_import` works the plan out again
from the same statement and carries out only the items the user ticked, all
in one unit of work (everything or nothing), recorded as one
`bulk_import_batch`.

Matching (so importing the same statement twice changes nothing):
  - a folio fund by ISIN + folio number; a demat fund by ISIN + demat
    account; shares by symbol (the one holding of that symbol, preferring
    the same broker); an SGB by its ISIN; a policy by its number;
  - a fund transaction already there on the same date with the same units
    is skipped.

Reconciling a folio fund with the statement's closing units and cost (the
statement is the record):
  - a top-up entered by hand ("Update figures" between statements) is
    replaced by the statement's real purchases, so nothing is counted twice;
  - while the fund still has its undated CAS opening balance, that row is
    corrected so the totals match the statement exactly;
  - a running SIP with no instalment in the statement is pointed out.

A holding seen for the first time starts from one opening-balance row with
what it held before the statement period (at cost for folio funds; demat
funds and shares have no cost in the statement, so at the statement's
price). Its XIRR is then left out, since earlier purchase dates are unknown.
Anything the app cannot record (a redemption, fewer units than the app
has, an unknown share symbol, a policy that is not in force) is listed for
the user to handle and never imported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from repository.errors import RepositoryIntegrityError
from repository.unit_of_work import UnitOfWork

from .cas_statement import (
    CasDematAccount,
    CasDematMf,
    CasEquity,
    CasMfFolio,
    CasMfTransaction,
    CasPolicy,
    CasSgb,
    CasStatement,
)

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_NAV = Decimal("0.0001")
# The purchase "Update figures" adds on the MF tab; the next import replaces it
# with the statement's real purchases.
HAND_TOP_UP = "Brought up to date by hand"
_SIP_WORDS = re.compile(r"\b(SIP|systematic)\b|\bsys\.", re.IGNORECASE)
_ACRONYMS = {
    "ICICI", "HDFC", "SBI", "UTI", "DSP", "PPFAS", "AIA", "LIC", "IDFC", "HSBC", "IIFL",
    "ETF", "IT", "UCO", "NHPC", "MOIL", "RVNL", "IRCTC", "IRFC", "NSE", "BSE", "ONE",
}
_POLICY_FREQUENCY = {
    "MONTHLY": "MONTHLY", "QUARTERLY": "QUARTERLY", "YEARLY": "ANNUAL", "ANNUAL": "ANNUAL",
    "ANNUALLY": "ANNUAL",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def pretty_name(text: str) -> str:
    """"HDFC BANK LIMITED" -> "HDFC Bank": title case, known acronyms kept,
    the company-form words at the end dropped."""
    words = re.sub(r"\s+", " ", (text or "").replace("#", " ")).strip().split(" ")
    while words and words[-1].upper().strip(".,") in ("LIMITED", "LTD", "PRIVATE", "PVT", "COMPANY"):
        words.pop()
    out = []
    for word in words:
        bare = word.upper().strip("().,-")
        if bare in _ACRONYMS or (bare.isalpha() and not re.search("[AEIOU]", bare)):
            out.append(word.upper())
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


# Demat brokers whose usual short name differs from their DP name.
_PLATFORMS = (("groww", "Groww"), ("zerodha", "Zerodha"), ("upstox", "Upstox"), ("angel", "Angel One"))


def _platform(dp_name: str) -> str:
    """The broker a demat account belongs to, by its usual name."""
    low = (dp_name or "").lower()
    return next((short for key, short in _PLATFORMS if key in low), pretty_name(dp_name))


def _money(value: Decimal) -> str:
    """₹12,34,567.89 — Indian digit grouping (lakh / crore)."""
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value).quantize(_CENT):f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"


def _tidy(name: str) -> str:
    """A name printed all in capitals, in normal case."""
    return pretty_name(name) if name.isupper() else name


@dataclass(frozen=True)
class StatementImportItem:
    """One holding in the statement and what importing it would do.

    `action`: NEW (added), UPDATE (new transactions added to a holding the
    app has), SAME (nothing to do) or CHECK (not imported — `notes` say
    why). Only NEW and UPDATE can be ticked (`selectable`); a NEW policy
    also needs its start date (`needs_start_date`), which the statement
    does not give."""

    key: str
    section: str  # MF / STOCK / GOLD / INSURANCE
    action: str
    title: str
    detail: str
    changes: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    selectable: bool = False
    needs_start_date: bool = False


@dataclass(frozen=True)
class StatementImportPreview:
    member_id: int
    source: str
    period_from: date
    period_to: date
    holder_name: str
    items: Tuple[StatementImportItem, ...]


@dataclass(frozen=True)
class StatementImportResult:
    batch_id: int
    items_applied: int
    rows_written: int


@dataclass
class _Planned:
    item: StatementImportItem
    ops: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)


class StatementImportMixin:
    """CAS import for `FinancialEngine`; uses its repositories and helpers."""

    # -------------------------------------------------- public

    def preview_statement_import(
        self, *, member_id: int, statement: CasStatement,
        scheme_names: Optional[Mapping[str, str]] = None,
        isin_symbols: Optional[Mapping[str, str]] = None,
    ) -> StatementImportPreview:
        """What importing `statement` for `member_id` would do; saves
        nothing. `scheme_names` (ISIN -> scheme) and `isin_symbols` (ISIN ->
        NSE symbol) fill in names the statement leaves out, when known."""
        plan = self._plan_statement(member_id, statement, scheme_names or {}, isin_symbols or {})
        return StatementImportPreview(
            member_id=member_id, source=statement.source,
            period_from=statement.period_from, period_to=statement.period_to,
            holder_name=statement.holder_name, items=tuple(p.item for p in plan),
        )

    def apply_statement_import(
        self, *, member_id: int, statement: CasStatement, selected: Sequence[str],
        policy_start_dates: Optional[Mapping[str, date]] = None,
        scheme_names: Optional[Mapping[str, str]] = None,
        isin_symbols: Optional[Mapping[str, str]] = None,
    ) -> StatementImportResult:
        """Import the ticked items (`selected` item keys) in one go."""
        plan = self._plan_statement(member_id, statement, scheme_names or {}, isin_symbols or {})
        wanted = set(selected)
        chosen = [p for p in plan if p.item.key in wanted and p.item.selectable]
        if not chosen:
            raise ValueError("nothing selected to import")
        start_dates = dict(policy_start_dates or {})
        for p in chosen:
            if p.item.needs_start_date and not start_dates.get(p.item.key):
                raise ValueError(f"enter the start date of policy {p.item.title}")
        with self._db.unit_of_work() as uow:
            batch = self._imports.create_batch(
                f"{statement.source} {statement.period_from:%d-%b-%Y} to "
                f"{statement.period_to:%d-%b-%Y} — {statement.holder_name}",
                uow=uow,
            )
            rows = 0
            for p in chosen:
                refs: Dict[str, int] = {}
                touched_mf, touched_stock = set(), set()
                for kind, op in p.ops:
                    rows += self._apply_op(
                        kind, op, member_id, batch.batch_id, refs, start_dates.get(p.item.key),
                        touched_mf, touched_stock, uow,
                    )
                for asset_id in touched_mf:
                    self._refresh_mf_caches(asset_id, uow)
                for asset_id in touched_stock:
                    self._refresh_stock_caches(asset_id, uow)
            self._imports.complete_batch(batch.batch_id, rows, uow=uow)
        return StatementImportResult(batch_id=batch.batch_id, items_applied=len(chosen), rows_written=rows)

    # -------------------------------------------------- planning

    def _plan_statement(
        self, member_id: int, st: CasStatement, names: Mapping[str, str], symbols: Mapping[str, str]
    ) -> List[_Planned]:
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        mfs, stocks, golds = [], [], []
        for asset in self._assets.list_assets(member_id):
            if asset.category == "MF":
                mfs.append((asset, self._assets.get_mutual_fund(asset.asset_id)))
            elif asset.category == "STOCK":
                stocks.append((asset, self._assets.get_stock(asset.asset_id)))
            elif asset.category == "GOLD":
                golds.append(asset)
        policies = self._insurance.list_policies(member_id)

        plan: List[_Planned] = []
        for folio in st.mf_folios:
            txns = [
                t for t in st.mf_transactions
                if t.isin == folio.isin and _norm(t.folio) == _norm(folio.folio)
            ]
            plan.append(self._plan_folio(st, folio, txns, mfs))
        for dmf in st.demat_mfs:
            plan.append(self._plan_demat_mf(st, dmf, mfs, names))
        for eq in st.equities:
            plan.append(self._plan_equity(st, eq, stocks, symbols))
        for sgb in st.sgbs:
            plan.append(self._plan_sgb(st, sgb, golds))
        for policy in st.policies:
            plan.append(self._plan_policy(policy, policies))
        return plan

    def _mf_units_now(self, asset_id: int) -> Decimal:
        return sum(
            (t.units for t in self._mf_txns.list_transactions(asset_id) if t.status == "ACTIVE"), _ZERO
        )

    def _plan_folio(self, st, f: CasMfFolio, txns: List[CasMfTransaction], mfs) -> _Planned:
        key = f"MF:{f.folio}:{f.isin}"
        title = _tidy(f.scheme_name)
        found = next(
            (a for a, h in mfs if (h.isin or "").upper() == f.isin and _norm(h.folio_number) == _norm(f.folio)),
            None,
        ) or next(
            (a for a, h in mfs if not h.isin and _norm(h.folio_number) == _norm(f.folio)
             and _norm(h.scheme_name) == _norm(f.scheme_name)),
            None,
        )
        detail = f"Folio {f.folio} · {f.units} units · cost {_money(f.total_cost)} · value {_money(f.value)}"
        purchases = [t for t in txns if t.units > _ZERO]
        notes = [
            f"{t.txn_date:%d %b %Y}: {t.description} ({t.units} units) — not a purchase, enter it yourself"
            for t in txns if t.units <= _ZERO
        ]
        planned = _Planned(StatementImportItem(key, "MF", "SAME", title, detail))

        def purchase_op(t: CasMfTransaction, ref: str) -> Tuple[str, Dict[str, Any]]:
            return ("mf_txn", dict(
                ref=ref, txn_date=t.txn_date,
                txn_type="SIP" if _SIP_WORDS.search(t.description) else "LUMPSUM",
                units=t.units, nav=t.nav, amount=t.amount + t.stamp_duty,
                description=t.description, opening=False,
            ))

        if found is None:
            new_units = sum((t.units for t in purchases), _ZERO)
            new_cost = sum((t.amount + t.stamp_duty for t in purchases), _ZERO)
            opening_units = f.units - new_units
            opening_cost = f.total_cost - new_cost
            changes = []
            ops: List[Tuple[str, Dict[str, Any]]] = [("mf_create", dict(
                amc=pretty_name(f.amc) if f.amc else pretty_name(" ".join(f.scheme_name.split()[:2])) + " Mutual Fund",
                scheme_name=title, folio=f.folio, isin=f.isin,
                first_date=st.period_from - timedelta(days=1),
            ))]
            if opening_units > _ZERO:
                cost = opening_cost if opening_cost > _ZERO else (opening_units * f.avg_cost).quantize(_CENT)
                ops.append(("mf_txn", dict(
                    ref="new", txn_date=st.period_from - timedelta(days=1), txn_type="LUMPSUM",
                    units=opening_units, nav=(cost / opening_units).quantize(_NAV), amount=cost,
                    description=f"Opening balance from {st.source} ({st.period_to:%b %Y})", opening=True,
                )))
                changes.append(f"Opening balance: {opening_units} units costing {_money(cost)}")
            for t in purchases:
                ops.append(purchase_op(t, "new"))
                changes.append(f"{t.txn_date:%d %b %Y}: {t.units} units for {_money(t.amount + t.stamp_duty)}")
            sips = [t for t in purchases if _SIP_WORDS.search(t.description)]
            if sips:
                last = sips[-1]
                ops.append(("mf_sip", dict(ref="new", amount=(last.amount + last.stamp_duty).quantize(_CENT),
                                           day=last.txn_date.day)))
                changes.append(f"SIP of {_money(last.amount + last.stamp_duty)} on day {last.txn_date.day}")
            ops.append(("mf_nav", dict(ref="new", on=st.period_to, nav=f.nav)))
            changes.append(f"NAV {f.nav} on {st.period_to:%d %b %Y}")
            notes.append("XIRR shows — until purchase history is added (dates before this statement are unknown).")
            planned.item = StatementImportItem(key, "MF", "NEW", title, detail, tuple(changes), tuple(notes), True)
            planned.ops = ops
            return planned

        ref = str(found.asset_id)
        existing = [t for t in self._mf_txns.list_transactions(found.asset_id) if t.status == "ACTIVE"]
        hand = [e for e in existing if e.description == HAND_TOP_UP]
        kept = [e for e in existing if e.description != HAND_TOP_UP]
        missing = [
            t for t in purchases
            if not any(e.txn_date == t.txn_date and e.units == t.units for e in kept)
        ]
        ops: List[Tuple[str, Dict[str, Any]]] = []
        changes: List[str] = []
        for h in hand:
            ops.append(("mf_txn_delete", dict(ref=ref, txn_id=h.mf_txn_id)))
            changes.append(f"Your hand-entered top-up of {h.units} units ({_money(h.amount)}) is replaced "
                           f"by the statement's real purchases")
        for t in missing:
            ops.append(purchase_op(t, ref))
            changes.append(f"{t.txn_date:%d %b %Y}: {t.units} units for {_money(t.amount + t.stamp_duty)}")
        units_after = sum((e.units for e in kept), _ZERO) + sum((t.units for t in missing), _ZERO)
        cost_after = sum((e.amount for e in kept), _ZERO) + sum((t.amount + t.stamp_duty for t in missing), _ZERO)
        opening = next((e for e in kept if e.is_opening_balance), None)
        if opening is not None and (units_after != f.units or cost_after != f.total_cost):
            new_units = opening.units + (f.units - units_after)
            new_cost = opening.amount + (f.total_cost - cost_after)
            if new_units > _ZERO and new_cost > _ZERO:
                ops.append(("mf_txn_adjust", dict(ref=ref, txn_id=opening.mf_txn_id, units=new_units, amount=new_cost)))
                changes.append(f"Opening balance corrected to {new_units} units costing {_money(new_cost)} "
                               f"to match the statement")
                units_after = f.units
        if units_after != f.units:
            notes.append(f"After import the app will have {units_after} units; the statement says {f.units}.")
        holding = next((h for a, h in mfs if a.asset_id == found.asset_id), None)
        if holding is not None and holding.sip_enabled and purchases is not None and not any(
            _SIP_WORDS.search(t.description) for t in purchases
        ) and (st.period_to - st.period_from).days >= 27:
            notes.append("No SIP instalment in this statement — if the SIP has stopped, mark it stopped "
                         "(MF tab → Set or stop a SIP).")
        if ops:
            ops.append(("mf_nav", dict(ref=ref, on=st.period_to, nav=f.nav)))
            planned.ops = ops
            planned.item = StatementImportItem(key, "MF", "UPDATE", title, detail, tuple(changes), tuple(notes), True)
        elif notes:
            planned.item = StatementImportItem(key, "MF", "CHECK", title, detail, (), tuple(notes))
        return planned

    @staticmethod
    def _demat_folio(account: CasDematAccount) -> str:
        return f"Demat {account.dp_id}-{account.client_id}"

    def _plan_demat_mf(self, st, d: CasDematMf, mfs, names: Mapping[str, str]) -> _Planned:
        key = f"DMF:{d.account.client_id}:{d.isin}"
        folio = self._demat_folio(d.account)
        broker = pretty_name(d.account.dp_name)
        fund_house_only = d.name.upper().endswith("MUTUAL FUND")
        scheme = names.get(d.isin) or (
            f"{pretty_name(d.name)} ({d.isin})" if fund_house_only else pretty_name(d.name)
        )
        amc = pretty_name(d.name) if fund_house_only else pretty_name(" ".join(d.name.split()[:2])) + " Mutual Fund"
        title = scheme
        detail = f"{broker} demat · {d.units} units · value {_money(d.value)}"
        found = next(
            (a for a, h in mfs if (h.isin or "").upper() == d.isin and h.folio_number == folio), None
        )
        nav = (d.value / d.units).quantize(_NAV) if d.units else d.nav  # the printed NAV is rounded
        if found is None:
            amount = (d.units * nav).quantize(_CENT)  # so the gain starts at exactly 0
            return _Planned(
                StatementImportItem(
                    key, "MF", "NEW", title, detail,
                    (f"Opening balance: {d.units} units at NAV {nav} ({_money(amount)})",),
                    ("The statement has no cost for demat units, so they are counted at this NAV "
                     "(gain starts at 0). XIRR shows — until purchase history is added.",),
                    True,
                ),
                [("mf_create", dict(amc=amc, scheme_name=scheme, folio=folio, isin=d.isin,
                                    first_date=st.period_to, broker=_platform(d.account.dp_name))),
                 ("mf_txn", dict(ref="new", txn_date=st.period_to, txn_type="LUMPSUM", units=d.units,
                                 nav=nav, amount=amount,
                                 description=f"Opening balance from {st.source} ({st.period_to:%b %Y})",
                                 opening=True)),
                 ("mf_nav", dict(ref="new", on=st.period_to, nav=nav))],
            )
        diff = d.units - self._mf_units_now(found.asset_id)
        if diff > _ZERO:
            amount = (diff * nav).quantize(_CENT)
            return _Planned(
                StatementImportItem(
                    key, "MF", "UPDATE", title, detail,
                    (f"{diff} more units, counted at NAV {nav} ({_money(amount)})",),
                    ("The statement has no purchase price for demat units; the month-end NAV is used.",),
                    True,
                ),
                [("mf_txn", dict(ref=str(found.asset_id), txn_date=st.period_to, txn_type="LUMPSUM",
                                 units=diff, nav=nav, amount=amount,
                                 description=f"Units added in demat per {st.source} ({st.period_to:%b %Y})",
                                 opening=False)),
                 ("mf_nav", dict(ref=str(found.asset_id), on=st.period_to, nav=nav))],
            )
        if diff < _ZERO:
            return _Planned(StatementImportItem(
                key, "MF", "CHECK", title, detail, (),
                (f"The statement has {-diff} fewer units than the app — a sale? Enter it yourself.",),
            ))
        return _Planned(StatementImportItem(key, "MF", "SAME", title, detail))

    def _plan_equity(self, st, e: CasEquity, stocks, symbols: Mapping[str, str]) -> _Planned:
        key = f"EQ:{e.account.client_id}:{e.isin}"
        broker = pretty_name(e.account.dp_name)
        symbol = (e.symbol or symbols.get(e.isin) or "").upper()
        title = f"{symbol or pretty_name(e.company)}"
        detail = f"{pretty_name(e.company)} · {broker} · {e.quantity} shares at {_money(e.price)}"
        if not symbol:
            return _Planned(StatementImportItem(
                key, "STOCK", "CHECK", title, detail, (),
                (f"No NSE symbol for ISIN {e.isin} — add it on the Stocks tab yourself.",),
            ))
        same = [(a, h) for a, h in stocks if h.symbol.upper() == symbol]
        found = next((a for a, h in same if _norm(h.broker) == _norm(broker)), None)
        if found is None and len(same) == 1:
            found = same[0][0]
        if found is None:
            return _Planned(
                StatementImportItem(
                    key, "STOCK", "NEW", title, detail,
                    (f"Opening balance: {e.quantity} shares at {_money(e.price)}",),
                    ("The statement has no buy price, so the shares are counted at this price "
                     "(gain starts at 0) — edit it on the Stocks tab. XIRR shows — until then.",),
                    True,
                ),
                [("stock_create", dict(broker=broker, symbol=symbol, exchange=e.exchange or "NSE",
                                       name=pretty_name(e.company), first_date=st.period_to)),
                 ("stock_txn", dict(ref="new", txn_date=st.period_to, quantity=e.quantity, price=e.price,
                                    description=f"Opening balance from {st.source} ({st.period_to:%b %Y})",
                                    opening=True))],
            )
        held = sum(
            ((t.quantity if t.txn_type == "BUY" else -t.quantity)
             for t in self._stock_txns.list_transactions(found.asset_id) if t.status == "ACTIVE"),
            _ZERO,
        )
        diff = e.quantity - held
        if diff > _ZERO:
            return _Planned(
                StatementImportItem(
                    key, "STOCK", "UPDATE", title, detail,
                    (f"{diff} more shares, at {_money(e.price)}",),
                    ("The statement has no buy price; the month-end price is used.",), True,
                ),
                [("stock_txn", dict(ref=str(found.asset_id), txn_date=st.period_to, quantity=diff,
                                    price=e.price,
                                    description=f"Shares added per {st.source} ({st.period_to:%b %Y})",
                                    opening=False))],
            )
        if diff < _ZERO:
            return _Planned(StatementImportItem(
                key, "STOCK", "CHECK", title, detail, (),
                (f"The app has {held} shares, the statement {e.quantity} — a sale? Enter it on the Stocks tab.",),
            ))
        return _Planned(StatementImportItem(key, "STOCK", "SAME", title, detail))

    def _plan_sgb(self, st, s: CasSgb, golds) -> _Planned:
        key = f"SGB:{s.isin}"
        name = f"SGB {s.isin}"
        title = f"Sovereign Gold Bond {s.isin}"
        detail = f"{s.units} g · matures {s.maturity_date:%d %b %Y} · value {_money(s.value)}"
        found = next((a for a in golds if s.isin in (a.name or "")), None)
        if found is None:
            bought = s.maturity_date.replace(year=s.maturity_date.year - 8)
            return _Planned(
                StatementImportItem(
                    key, "GOLD", "NEW", title, detail,
                    (f"{s.units} g bought at the issue price {_money(s.issue_price)}/g on {bought:%d %b %Y} "
                     f"(an SGB runs 8 years), {s.coupon_rate}% a year",
                     f"Valued at {_money(s.market_price)}/g on {st.period_to:%d %b %Y}"),
                    (), True,
                ),
                [("gold_create", dict(name=name, grams=s.units, rate=s.issue_price, bought=bought,
                                      coupon=s.coupon_rate)),
                 ("gold_rate", dict(ref="new", on=st.period_to, rate=s.market_price))],
            )
        holding = self._assets.get_gold(found.asset_id)
        latest = self._valuations.get_latest_valuation(found.asset_id)
        if holding.quantity_grams != s.units:
            return _Planned(StatementImportItem(
                key, "GOLD", "CHECK", title, detail, (),
                (f"The app has {holding.quantity_grams} g, the statement {s.units} g.",),
            ))
        if latest is not None and latest.valuation_date >= st.period_to:
            return _Planned(StatementImportItem(key, "GOLD", "SAME", title, detail))
        return _Planned(
            StatementImportItem(key, "GOLD", "UPDATE", title, detail,
                                (f"Value at {_money(s.market_price)}/g on {st.period_to:%d %b %Y}",), (), True),
            [("gold_rate", dict(ref=str(found.asset_id), on=st.period_to, rate=s.market_price))],
        )

    def _plan_policy(self, p: CasPolicy, policies) -> _Planned:
        key = f"INS:{p.policy_number}"
        insurer = pretty_name(re.sub(r"\s*company\s+limited\s*$", "", p.insurer, flags=re.IGNORECASE))
        plan = p.plan_name
        if _norm(plan).startswith(_norm(insurer)) and len(plan) > len(insurer):
            plan = plan[len(insurer):].strip()  # "Tata AIA Life Insurance Value Income Plan"
        title = f"{insurer} · {plan} ({p.policy_number})"
        detail = f"{p.status} · premium {_money(p.premium)} {p.frequency.lower()} · cover {_money(p.sum_assured)}"
        found = next((x for x in policies if _norm(x.policy_number) == _norm(p.policy_number)), None)
        if found is not None:
            notes = []
            if found.premium_amount != p.premium:
                notes.append(f"Premium in the app {_money(found.premium_amount)}, statement {_money(p.premium)}.")
            if (found.sum_assured or _ZERO) != p.sum_assured:
                notes.append(f"Cover in the app {_money(found.sum_assured or _ZERO)}, statement {_money(p.sum_assured)}.")
            return _Planned(StatementImportItem(key, "INSURANCE", "CHECK" if notes else "SAME",
                                                title, detail, (), tuple(notes)))
        if "inforce" not in _norm(p.status):
            return _Planned(StatementImportItem(
                key, "INSURANCE", "CHECK", title, detail, (),
                (f"Status is \"{p.status}\", so it is not imported. Add it on the Insurance tab if it should be.",),
            ))
        frequency = _POLICY_FREQUENCY.get(re.sub(r"[^A-Z]", "", p.frequency.upper()))
        if frequency is None:
            return _Planned(StatementImportItem(
                key, "INSURANCE", "CHECK", title, detail, (),
                (f"The app has no \"{p.frequency}\" premium; add the policy on the Insurance tab.",),
            ))
        plan_lower = plan.lower()
        policy_type = (
            "TERM" if re.search(r"protect|term", plan_lower)
            else "ULIP" if re.search(r"ulip|wealth|fortune|invest", plan_lower)
            else "ENDOWMENT"
        )
        return _Planned(
            StatementImportItem(
                key, "INSURANCE", "NEW", title, detail,
                (f"Type: {policy_type.title()} (from the plan name — change it on the Insurance tab if wrong)",),
                ("The statement has no start date: enter it below.",), True, True,
            ),
            [("policy_create", dict(policy_type=policy_type, insurer=insurer, plan_name=plan,
                                    policy_number=p.policy_number, premium=p.premium,
                                    frequency=frequency, sum_assured=p.sum_assured))],
        )

    # -------------------------------------------------- applying

    def _record_price(self, asset_id: int, on: date, value: Decimal, source: str, uow: UnitOfWork) -> None:
        try:
            self._valuations.record_valuation(asset_id, on, value, source, uow=uow)
        except RepositoryIntegrityError:
            self._valuations.record_valuation(asset_id, on, value, source, is_correction=True, uow=uow)

    def _refresh_mf_caches(self, asset_id: int, uow: UnitOfWork) -> None:
        units, avg_nav = self._mf_units_and_avg_nav(asset_id, uow=uow)
        self._cache.set_mf_units_and_avg_nav(asset_id, units, avg_nav, uow=uow)
        self._cache.set_asset_invested_amount(asset_id, self._mf_invested_amount(asset_id, uow=uow), uow=uow)
        asset = self._assets.get_asset(asset_id, uow=uow)
        self._cache.set_asset_current_value(
            asset_id, self._current_value_for_asset(asset, date.today(), uow=uow), uow=uow
        )

    def _apply_op(
        self, kind: str, op: Dict[str, Any], member_id: int, batch_id: int, refs: Dict[str, int],
        start_date: Optional[date], touched_mf: set, touched_stock: set, uow: UnitOfWork,
    ) -> int:
        """Carry out one planned write; returns the rows it added."""
        owner = [(member_id, Decimal("100"))]
        ref = refs.get(op.get("ref", ""), None) if op.get("ref") == "new" else (
            int(op["ref"]) if op.get("ref") else None
        )
        if kind == "mf_create":
            asset, _ = self._assets.create_mutual_fund(
                member_id, op["scheme_name"], op["amc"], 0, op["first_date"], owner,
                amc=op["amc"], scheme_name=op["scheme_name"], folio_number=op["folio"],
                isin=op["isin"], broker=op.get("broker"), uow=uow,
            )
            refs["new"] = asset.asset_id
            touched_mf.add(asset.asset_id)
            return 1
        if kind == "mf_txn":
            self._mf_txns.create_transaction(
                ref, op["txn_date"], op["txn_type"], op["units"], op["nav"], op["amount"],
                description=op["description"], is_opening_balance=op["opening"],
                import_batch_id=batch_id, uow=uow,
            )
            self._record_valuation_from_transaction(ref, op["txn_date"], op["nav"], uow)
            touched_mf.add(ref)
            return 1
        if kind == "mf_txn_delete":
            self._mf_txns.delete_transaction(op["txn_id"], uow=uow)
            touched_mf.add(ref)
            return 0
        if kind == "mf_txn_adjust":
            row = self._mf_txns.get_transaction(op["txn_id"], uow=uow)
            self._mf_txns.update_transaction(
                row.mf_txn_id, txn_date=row.txn_date, txn_type=row.txn_type,
                nav=(op["amount"] / op["units"]).quantize(_NAV), amount=op["amount"],
                description=row.description, is_opening_balance=row.is_opening_balance,
                units=op["units"], uow=uow,
            )
            touched_mf.add(ref)
            return 0
        if kind == "mf_sip":
            self._assets.update_mf_sip(ref, sip_enabled=True, sip_amount=op["amount"], sip_date=op["day"], uow=uow)
            return 0
        if kind == "mf_nav":
            self._record_price(ref, op["on"], op["nav"], "AMFI", uow)
            touched_mf.add(ref)
            return 1
        if kind == "stock_create":
            asset, _ = self._assets.create_stock(
                member_id, op["name"], op["broker"], op["symbol"], op["exchange"], 0,
                op["first_date"], owner, broker=op["broker"], uow=uow,
            )
            refs["new"] = asset.asset_id
            return 1
        if kind == "stock_txn":
            self._stock_txns.create_transaction(
                ref, op["txn_date"], "BUY", op["quantity"], op["price"], 0,
                description=op["description"], is_opening_balance=op["opening"],
                import_batch_id=batch_id, uow=uow,
            )
            self._record_price(ref, op["txn_date"], op["price"], "NSE", uow)
            touched_stock.add(ref)
            return 1
        if kind == "gold_create":
            invested = (op["grams"] * op["rate"]).quantize(_CENT)
            asset, _ = self._assets.create_gold(
                member_id, op["name"], "RBI", invested, op["bought"], owner,
                form="SGB", purity="24K", quantity_grams=op["grams"],
                purchase_rate_per_gram=op["rate"], sgb_interest_rate=op["coupon"], uow=uow,
            )
            refs["new"] = asset.asset_id
            self._set_gold_rate(asset.asset_id, op["bought"], op["rate"], uow)
            return 1
        if kind == "gold_rate":
            self._set_gold_rate(ref, op["on"], op["rate"], uow)
            return 1
        if kind == "policy_create":
            self._insurance.create_policy(
                member_id, op["policy_type"], op["insurer"], op["policy_number"], op["premium"],
                op["frequency"], start_date, sum_assured=op["sum_assured"], plan_name=op.get("plan_name"),
                uow=uow,
            )
            return 1
        raise ValueError(f"unknown import step {kind}")
