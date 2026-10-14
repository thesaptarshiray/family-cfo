"""Tests for `api.stock_price_sync`: parsing NSE/BSE bhavcopies and syncing.

No network: the samples below are real rows copied from NSE's and BSE's
bhavcopies for 2026-09-28, and every sync test injects them through
`fetcher`.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

import api.stock_price_sync as sps
from api.amfi_sync import SyncStateStore
from api.stock_price_sync import (
    fetch_latest_bhavcopy,
    parse_bhavcopy,
    sync_family_stock_prices,
    sync_stock_prices_if_stale,
)
from financial_engine import FinancialEngine
from repository.database import Database
from repository.schema import initialize_schema

HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
NSE = "\n".join([
    HEADER,
    "2026-09-28,2026-09-28,CM,NSE,STK,11536,INE467B01029,TCS,EQ,,,,,TATA CONSULTANCY SERV LT,2100.00,2100.00,2051.00,2070.70,2070.70,2082.00,,2070.70,,,2457049,5094158751.40,109560,F1,1,,,,,",
    "2026-09-28,2026-09-28,CM,NSE,STK,1594,INE009A01021,INFY,EQ,,,,,INFOSYS LIMITED,1002.00,1009.50,986.60,1003.20,1003.20,1000.20,,1003.20,,,8567672,8557694675.90,166272,F1,1,,,,,",
    "2026-09-28,2026-09-28,CM,NSE,STK,12346,INE086A01029,ELECTCAST,BL,,,,,ELECTROSTEEL CASTINGS LTD,71.20,71.40,71.20,71.28,71.40,70.00,,71.40,,,18762000,1337378840.00,3,F1,999999999,,,,,",
    "2026-09-28,2026-09-28,CM,NSE,STK,928,INE086A01029,ELECTCAST,EQ,,,,,ELECTROSTEEL CASTINGS LTD,72.34,73.71,70.10,72.35,72.40,72.34,,72.35,,,19655083,1420823478.17,23747,F1,1,,,,,",
])
BSE = "\n".join([
    HEADER,
    "2026-09-28,2026-09-28,CM,BSE,STK,532540,INE467B01029,TCS,A,,,,,TATA CONSULTANCY SERVICES LTD.,2088.10,2099.00,2051.20,2071.70,2071.70,2083.95,,2070.70,,,162446,336938758.00,12071,F1,1,,,,,",
    "2026-09-28,2026-09-28,CM,BSE,STK,500112,INE062A01020,SBIN,A,,,,,STATE BANK OF INDIA,981.85,981.85,956.30,961.90,961.90,982.50,,962.00,,,1167052,1126078398.00,14549,F1,1,,,,,",
])
DAY = date(2026, 9, 28)


# ----------------------------------------------------------------------
# parse_bhavcopy
# ----------------------------------------------------------------------


def test_parses_closing_price_and_trade_date():
    lookup = parse_bhavcopy(NSE)
    assert lookup["INFY"] == (Decimal("1003.20"), DAY)
    assert lookup["TCS"] == (Decimal("2070.70"), DAY)


def test_normal_equity_series_wins_over_block_deals():
    assert parse_bhavcopy(NSE)["ELECTCAST"][0] == Decimal("72.35")  # EQ, not the BL row


def test_garbage_gives_an_empty_lookup():
    assert parse_bhavcopy("") == {}
    assert parse_bhavcopy("<html>blocked</html>") == {}


def test_latest_file_walks_back_over_holidays(monkeypatch):
    asked = []

    def fake(exchange, day, timeout=20):
        asked.append(day)
        return NSE if day == DAY else None

    monkeypatch.setattr(sps, "fetch_bhavcopy", fake)
    assert fetch_latest_bhavcopy("NSE", today=DAY + timedelta(days=2)) == NSE
    assert asked == [DAY + timedelta(days=2), DAY + timedelta(days=1), DAY]
    monkeypatch.setattr(sps, "fetch_bhavcopy", lambda exchange, day, timeout=20: None)
    with pytest.raises(RuntimeError, match="no NSE price file"):
        fetch_latest_bhavcopy("NSE", today=DAY)


# ----------------------------------------------------------------------
# sync_family_stock_prices
# ----------------------------------------------------------------------


@pytest.fixture
def family(tmp_path):
    database = Database(tmp_path / "sync.db")
    initialize_schema(database)
    engine = FinancialEngine(database)
    fam = engine.create_family("Ray")
    me = engine.create_family_member(fam.family_id, "Saptarshi", "self")
    return engine, fam, me


def _stock(engine, member_id, symbol, exchange="NSE", price="1000"):
    return engine.create_stock(
        member_id=member_id, broker="Zerodha", symbol=symbol, exchange=exchange,
        initial_txn_date=date(2025, 1, 10), initial_quantity=Decimal("10"),
        initial_price=Decimal(price),
    )


def _fetcher(calls=None):
    files = {"NSE": NSE, "BSE": BSE}

    def fetch(exchange):
        if calls is not None:
            calls.append(exchange)
        return files[exchange]
    return fetch


def test_sync_records_closing_prices_and_only_fetches_what_it_needs(family):
    engine, fam, me = family
    _stock(engine, me.member_id, "INFY")
    calls = []

    result = sync_family_stock_prices(engine, fam.family_id, fetcher=_fetcher(calls))

    assert result.error is None and len(result.updated) == 1
    assert result.nav_date == DAY
    assert calls == ["NSE"]  # BSE file never needed
    infy = engine.list_stocks(me.member_id)[0]
    assert infy.current_price == Decimal("1003.20")
    assert infy.current_price_source == "NSE" and infy.current_price_date == DAY
    assert infy.current_value == Decimal("10032.00")


def test_bse_holding_uses_bse_and_falls_back_across_exchanges(family):
    engine, fam, me = family
    _stock(engine, me.member_id, "SBIN", exchange="NSE")   # only in our BSE sample
    _stock(engine, me.member_id, "TCS", exchange="BSE")
    sync_family_stock_prices(engine, fam.family_id, fetcher=_fetcher())
    by_symbol = {s.symbol: s for s in engine.list_stocks(me.member_id)}
    assert by_symbol["TCS"].current_price == Decimal("2071.70")
    assert by_symbol["TCS"].current_price_source == "OTHER"  # shown as BSE
    assert by_symbol["SBIN"].current_price == Decimal("961.90")


def test_unknown_symbols_are_reported_and_resync_is_idempotent(family):
    engine, fam, me = family
    _stock(engine, me.member_id, "INFY")
    _stock(engine, me.member_id, "NOSUCH")
    first = sync_family_stock_prices(engine, fam.family_id, fetcher=_fetcher())
    second = sync_family_stock_prices(engine, fam.family_id, fetcher=_fetcher())
    assert len(first.updated) == 1 and "not found" in first.unmatched[0]
    assert second.updated == () and len(second.unchanged) == 1


def test_fetch_failure_is_an_error_not_an_exception(family):
    engine, fam, me = family
    _stock(engine, me.member_id, "INFY")

    def boom(exchange):
        raise OSError("NSE unreachable")

    result = sync_family_stock_prices(engine, fam.family_id, fetcher=boom)
    assert "NSE unreachable" in result.error
    assert engine.list_stocks(me.member_id)[0].current_price == Decimal("1000")


def test_no_stocks_means_nothing_fetched(family):
    engine, fam, _ = family
    calls = []
    assert sync_family_stock_prices(engine, fam.family_id, fetcher=_fetcher(calls)).error is None
    assert calls == []


def test_auto_sync_at_most_once_a_day(family, tmp_path):
    engine, fam, me = family
    _stock(engine, me.member_id, "INFY")
    state = SyncStateStore(tmp_path / "stock_state.json")
    calls = []
    assert sync_stock_prices_if_stale(engine, fam.family_id, state, today=DAY,
                                      fetcher=_fetcher(calls)).attempted
    assert not sync_stock_prices_if_stale(engine, fam.family_id, state, today=DAY,
                                          fetcher=_fetcher(calls)).attempted
    assert calls == ["NSE"]
    assert state.read()["last_nav_date"] == "2026-09-28"


# ----------------------------------------------------------------------
# Sector / market-cap classification from NSE's index lists
# ----------------------------------------------------------------------

from api.stock_price_sync import (  # noqa: E402
    parse_nse_index_lists,
    refresh_stock_classification_if_stale,
)

LIST_HEADER = "Company Name,Industry,Symbol,Series,ISIN Code\n"
LISTS = {
    "ind_nifty500list": LIST_HEADER
    + "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n"
    + "Electrosteel Castings Ltd.,Capital Goods,ELECTCAST,EQ,INE086A01029\n"
    + "360 ONE WAM Ltd.,Financial Services,360ONE,EQ,INE466L01038\n",
    "ind_nifty100list": LIST_HEADER + "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n",
    "ind_niftymidcap150list": LIST_HEADER + "360 ONE WAM Ltd.,Financial Services,360ONE,EQ,INE466L01038\n",
    "ind_niftysmallcap250list": LIST_HEADER + "Electrosteel Castings Ltd.,Capital Goods,ELECTCAST,EQ,INE086A01029\n",
}


def test_index_lists_give_sector_and_sebi_cap():
    rows = {r[0]: r for r in parse_nse_index_lists(LISTS)}
    assert rows["INFY"] == ("INFY", "Infosys Ltd.", "Information Technology", "LARGE")
    assert rows["360ONE"][3] == "MID" and rows["ELECTCAST"][3] == "SMALL"


def test_classification_refreshes_monthly_and_reports_errors(family):
    engine, _, _ = family
    calls = []

    def fetch():
        calls.append(1)
        return LISTS

    assert refresh_stock_classification_if_stale(engine, today=DAY, fetcher=fetch) is None
    assert engine.stock_classification_refreshed_on() == DAY
    assert refresh_stock_classification_if_stale(engine, today=DAY + timedelta(days=10), fetcher=fetch) is None
    assert calls == [1]  # not due again yet
    assert refresh_stock_classification_if_stale(engine, today=DAY + timedelta(days=31), fetcher=fetch) is None
    assert calls == [1, 1]

    def boom():
        raise OSError("NSE down")

    error = refresh_stock_classification_if_stale(engine, today=DAY + timedelta(days=90), fetcher=boom)
    assert "NSE down" in error
