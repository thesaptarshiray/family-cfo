"""
Daily stock price sync from the exchanges' public end-of-day files.

NSE and BSE both publish every listed security's end-of-day prices as a
"bhavcopy" CSV (no login, no API key) in the same layout since 2024:

    TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,...,ClsPric,...
    2026-09-28,2026-09-28,CM,NSE,STK,1594,INE009A01021,INFY,EQ,...,1003.20,...

NSE's is zipped, BSE's plain. A file exists only for trading days and only
after the market closes, so the latest one is found by trying today and then
up to a week back.

Holdings are matched by symbol (as the user entered it, upper-cased) on the
exchange they are listed with; a symbol missing there is looked up on the
other exchange. The closing price is recorded through the Engine with source
"NSE" for NSE prices and "OTHER" for BSE prices (the schema's
`asset_valuation.source` has no BSE value; the Stocks tab shows it as BSE).

Like `amfi_sync`, this is network I/O and so lives in `api`, recording
prices only through `FinancialEngine`; the same `SyncStateStore` /
`SyncResult` bookkeeping is reused, with its own state file.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, List, Optional, Tuple

from financial_engine import FinancialEngine

from .amfi_sync import SyncResult, SyncStateStore

NSE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
BSE_URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{ymd}_F_0000.CSV"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "*/*",
}
_REFERER = {"NSE": "https://www.nseindia.com/", "BSE": "https://www.bseindia.com/"}
_SOURCE = {"NSE": "NSE", "BSE": "OTHER"}
# NSE series preference when a symbol trades in several (EQ = normal equity).
_SERIES_RANK = {"EQ": 0, "BE": 1, "BZ": 2, "SM": 3, "ST": 4}
_LOOKBACK_DAYS = 7

PriceLookup = Dict[str, Tuple[Decimal, date]]
# exchange ("NSE"/"BSE") -> the latest available bhavcopy CSV text
LatestFetcher = Callable[[str], str]


# ----------------------------------------------------------------------
# Parsing: pure, no network
# ----------------------------------------------------------------------


def parse_bhavcopy(csv_text: str) -> PriceLookup:
    """Return `{symbol: (closing price, trade date)}` for every equity row
    (`FinInstrmTp` STK). When a symbol appears in several series, the
    normal-equity one wins. Rows whose price or date does not parse are
    skipped."""
    lookup: PriceLookup = {}
    ranks: Dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        if (row.get("FinInstrmTp") or "").strip() != "STK":
            continue
        symbol = (row.get("TckrSymb") or "").strip().upper()
        try:
            price = Decimal((row.get("ClsPric") or "").strip())
            traded = date.fromisoformat((row.get("TradDt") or "").strip())
        except (InvalidOperation, ValueError):
            continue
        if not symbol or price <= 0:
            continue
        rank = _SERIES_RANK.get((row.get("SctySrs") or "").strip(), 9)
        if symbol not in lookup or rank < ranks[symbol]:
            lookup[symbol] = (price, traded)
            ranks[symbol] = rank
    return lookup


def parse_bhavcopy_isins(csv_text: str) -> Dict[str, str]:
    """Return `{isin: symbol}` for every equity row — used to find the
    symbol of a share that a statement lists only by ISIN."""
    symbols: Dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        if (row.get("FinInstrmTp") or "").strip() != "STK":
            continue
        isin = (row.get("ISIN") or "").strip().upper()
        symbol = (row.get("TckrSymb") or "").strip().upper()
        if isin and symbol and (row.get("SctySrs") or "").strip() in ("EQ", "BE", "BZ", ""):
            symbols.setdefault(isin, symbol)
    return symbols


# ----------------------------------------------------------------------
# Fetching: the only network calls
# ----------------------------------------------------------------------


def fetch_bhavcopy(exchange: str, day: date, timeout: float = 20) -> Optional[str]:
    """The exchange's bhavcopy for `day` as CSV text, or `None` if there is
    none (a holiday, a weekend, or not yet published). Raises on any other
    network failure."""
    url = (NSE_URL if exchange == "NSE" else BSE_URL).format(ymd=day.strftime("%Y%m%d"))
    request = urllib.request.Request(url, headers={**_HEADERS, "Referer": _REFERER[exchange]})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return None
        raise
    if exchange == "NSE":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            payload = archive.read(archive.namelist()[0])
    return payload.decode("utf-8", errors="replace")


def fetch_latest_bhavcopy(exchange: str, today: Optional[date] = None) -> str:
    """The most recent bhavcopy, trying today and then up to a week back."""
    today = today or date.today()
    for back in range(_LOOKBACK_DAYS + 1):
        text = fetch_bhavcopy(exchange, today - timedelta(days=back))
        if text:
            return text
    raise RuntimeError(f"no {exchange} price file found for the last {_LOOKBACK_DAYS} days")


# ----------------------------------------------------------------------
# The sync itself
# ----------------------------------------------------------------------


def _label(stock) -> str:
    return f"{stock.symbol} ({stock.exchange}, {stock.broker})"


def sync_family_stock_prices(
    engine: FinancialEngine,
    family_id: int,
    *,
    fetcher: LatestFetcher = fetch_latest_bhavcopy,
) -> SyncResult:
    """Record the latest closing price for every ACTIVE stock holding.

    Each exchange's file is fetched at most once, and only if a holding
    needs it. A holding already carrying this exact synced price for this
    exact date is left alone. Failures are returned in `error`, never
    raised.
    """
    holdings = [
        stock
        for member in engine.list_family_members(family_id)
        for stock in engine.list_stocks(member.member_id)
        if stock.status == "ACTIVE" and stock.quantity > 0
    ]
    if not holdings:
        return SyncResult(attempted=True)

    lookups: Dict[str, PriceLookup] = {}
    errors: Dict[str, str] = {}

    def lookup_for(exchange: str) -> PriceLookup:
        if exchange not in lookups and exchange not in errors:
            try:
                lookups[exchange] = parse_bhavcopy(fetcher(exchange))
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                errors[exchange] = f"could not fetch {exchange} prices: {exc}"
        return lookups.get(exchange, {})

    updated: List[str] = []
    unchanged: List[str] = []
    unmatched: List[str] = []
    price_dates: List[date] = []
    try:
        for stock in holdings:
            home = stock.exchange
            other = "BSE" if home == "NSE" else "NSE"
            exchange, match = home, lookup_for(home).get(stock.symbol.upper())
            if match is None:
                exchange, match = other, lookup_for(other).get(stock.symbol.upper())
            if match is None:
                unmatched.append(f"{_label(stock)}: symbol not found on NSE or BSE")
                continue
            price, traded = match
            price_dates.append(traded)
            source = _SOURCE[exchange]
            if (
                stock.current_price_source == source
                and stock.current_price_date == traded
                and stock.current_price == price
            ):
                unchanged.append(_label(stock))
                continue
            engine.record_stock_price(
                asset_id=stock.asset_id, price_date=traded, price=price, source=source
            )
            updated.append(_label(stock))
    except Exception as exc:  # noqa: BLE001
        return SyncResult(
            attempted=True, updated=tuple(updated), unchanged=tuple(unchanged),
            unmatched=tuple(unmatched), error=f"sync stopped partway: {exc}",
        )

    # An exchange that could not be reached only matters if nothing synced.
    error = None
    if errors and not updated and not unchanged:
        error = "; ".join(errors.values())
    return SyncResult(
        attempted=True,
        nav_date=max(price_dates) if price_dates else None,
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        unmatched=tuple(unmatched),
        error=error,
    )


def sync_stock_prices_if_stale(
    engine: FinancialEngine,
    family_id: int,
    state: SyncStateStore,
    *,
    today: Optional[date] = None,
    fetcher: LatestFetcher = fetch_latest_bhavcopy,
    index_fetcher: Optional["IndexFetcher"] = None,
) -> SyncResult:
    """At most one automatic attempt per calendar day, like the NAV sync;
    with an `index_fetcher`, also refreshes sector / market-cap data when
    it is a month old."""
    today = today or date.today()
    if state.last_attempt_date() == today:
        return SyncResult(attempted=False)
    result = sync_family_stock_prices(engine, family_id, fetcher=fetcher)
    state.record(today, result)
    if index_fetcher is not None:
        refresh_stock_classification_if_stale(engine, today=today, fetcher=index_fetcher)
    return result


# ----------------------------------------------------------------------
# Sector and market-cap classification from NSE's index lists
# ----------------------------------------------------------------------

NSE_INDEX_URL = "https://nsearchives.nseindia.com/content/indices/{name}.csv"
# Nifty 500 gives company and industry; the other three give SEBI's
# large (top 100) / mid (101-250) / small (251+) split within it.
INDEX_LISTS = {
    "ind_nifty500list": None,
    "ind_nifty100list": "LARGE",
    "ind_niftymidcap150list": "MID",
    "ind_niftysmallcap250list": "SMALL",
}
CLASSIFICATION_MAX_AGE_DAYS = 30

IndexFetcher = Callable[[], Dict[str, str]]


def parse_nse_index_lists(lists: Dict[str, str]) -> List[Tuple[str, str, str, Optional[str]]]:
    """`(symbol, company, sector, cap)` for every Nifty 500 company, from the
    CSVs keyed by list name (columns: Company Name, Industry, Symbol, ...)."""
    caps: Dict[str, str] = {}
    for name, cap in INDEX_LISTS.items():
        if cap is None:
            continue
        for row in csv.DictReader(io.StringIO(lists.get(name, ""))):
            symbol = (row.get("Symbol") or "").strip().upper()
            if symbol:
                caps[symbol] = cap
    rows = []
    for row in csv.DictReader(io.StringIO(lists.get("ind_nifty500list", ""))):
        symbol = (row.get("Symbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append((
            symbol,
            (row.get("Company Name") or "").strip() or None,
            (row.get("Industry") or "").strip() or "Unclassified",
            caps.get(symbol, "SMALL"),
        ))
    return rows


def fetch_nse_index_lists(timeout: float = 20) -> Dict[str, str]:
    lists = {}
    for name in INDEX_LISTS:
        request = urllib.request.Request(
            NSE_INDEX_URL.format(name=name), headers={**_HEADERS, "Referer": _REFERER["NSE"]}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            lists[name] = response.read().decode("utf-8", errors="replace")
    return lists


def refresh_stock_classification_if_stale(
    engine: FinancialEngine,
    *,
    today: Optional[date] = None,
    fetcher: IndexFetcher = fetch_nse_index_lists,
) -> Optional[str]:
    """Reload sector / market-cap data from NSE when it is missing or more
    than a month old (index membership changes twice a year). Returns an
    error message instead of raising; None when fine or not due."""
    today = today or date.today()
    last = engine.stock_classification_refreshed_on()
    if last is not None and (today - last).days < CLASSIFICATION_MAX_AGE_DAYS:
        return None
    try:
        rows = parse_nse_index_lists(fetcher())
        if not rows:
            return "NSE index lists had no usable rows"
        engine.update_stock_classification_from_nse(rows, today)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return f"could not refresh sector data from NSE: {exc}"
    return None

