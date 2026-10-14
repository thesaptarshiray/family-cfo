"""Correcting stock holdings and their buys / sells (the Stocks tab's Edit)."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine import FinancialEngine
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema
from test_api_server import _family_with_member, _get, _post, server  # noqa: F401 (fixture)

D = Decimal


@pytest.fixture
def setup(tmp_path):
    database = Database(tmp_path / "stocks.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Ray")
    raj = family_repo.create_family_member(family.family_id, "Raj", "self")
    asha = family_repo.create_family_member(family.family_id, "Asha", "spouse")
    engine = FinancialEngine(database)
    stock = engine.create_stock(
        member_id=raj.member_id, broker="HDFC Security", symbol="HINDPETRO", exchange="NSE",
        initial_txn_date=date(2024, 3, 1), initial_quantity=500, initial_price=300,
    )
    return engine, family.family_id, raj.member_id, asha.member_id, stock


def test_correct_a_buy_price_and_quantity(setup):
    engine, _, _, _, stock = setup
    (buy,) = stock.transactions
    fixed = engine.update_stock_transaction(
        stock_txn_id=buy.stock_txn_id, txn_date=date(2024, 2, 1), txn_type="BUY",
        quantity=350, price=280, charges=20,
    )
    assert fixed.quantity == D("350") and fixed.invested_amount == D("98020.00")
    assert fixed.transactions[0].txn_date == date(2024, 2, 1)


def test_correcting_an_imported_opening_balance_turns_xirr_on(setup):
    engine, family_id, member_id, _, _ = setup
    imported = engine.create_stock(
        member_id=member_id, broker="HDFC Bank", symbol="SBIN", exchange="NSE",
        initial_txn_date=date(2026, 8, 31), initial_quantity=130, initial_price=1060,
    )
    # mark it the way a CAS import stores it
    with engine._db.unit_of_work() as uow:  # noqa: SLF001 - test setup only
        engine._stock_txns.delete_for_asset(imported.asset_id, uow=uow)
        engine._stock_txns.create_transaction(
            imported.asset_id, date(2026, 8, 31), "BUY", 130, 1060, 0,
            is_opening_balance=True, uow=uow,
        )
        engine._refresh_stock_caches(imported.asset_id, uow)
    assert engine._load_stock_summary(imported.asset_id).xirr is None
    (opening,) = engine._load_stock_summary(imported.asset_id).transactions
    fixed = engine.update_stock_transaction(
        stock_txn_id=opening.stock_txn_id, txn_date=date(2021, 6, 15), txn_type="BUY",
        quantity=130, price=420,
    )
    assert not fixed.transactions[0].is_opening_balance
    assert fixed.xirr is not None
    assert engine.get_stock_portfolio(family_id).xirr_excluded_count == 0


def test_an_edit_that_sells_more_than_was_held_is_refused(setup):
    engine, _, _, _, stock = setup
    engine.record_stock_transaction(
        asset_id=stock.asset_id, txn_date=date(2025, 1, 10), txn_type="SELL", quantity=150, price=350,
    )
    (buy, _sell) = engine._load_stock_summary(stock.asset_id).transactions
    with pytest.raises(ValueError):
        engine.update_stock_transaction(
            stock_txn_id=buy.stock_txn_id, txn_date=date(2024, 3, 1), txn_type="BUY",
            quantity=100, price=300,
        )
    assert engine._load_stock_summary(stock.asset_id).quantity == D("350")  # nothing changed


def test_delete_an_entry_but_not_the_last_one(setup):
    engine, _, _, _, stock = setup
    engine.record_stock_transaction(
        asset_id=stock.asset_id, txn_date=date(2025, 1, 10), txn_type="BUY", quantity=10, price=310,
    )
    first, second = engine._load_stock_summary(stock.asset_id).transactions
    assert engine.delete_stock_transaction(second.stock_txn_id).quantity == D("500")
    with pytest.raises(ValueError, match="only entry"):
        engine.delete_stock_transaction(first.stock_txn_id)


def test_edit_owner_broker_and_symbol(setup):
    engine, _, _, asha_id, stock = setup
    edited = engine.update_stock_holding(
        asset_id=stock.asset_id, member_id=asha_id, broker="HDFC Bank", symbol="hindpetro",
        exchange="BSE", name="Hindustan Petroleum",
    )
    assert (edited.member_id, edited.broker, edited.symbol, edited.exchange, edited.name) == (
        asha_id, "HDFC Bank", "HINDPETRO", "BSE", "Hindustan Petroleum")


def test_renaming_into_an_existing_holding_merges_them(setup):
    engine, family_id, member_id, _, stock = setup
    other = engine.create_stock(
        member_id=member_id, broker="HDFC Bank", symbol="HINDPETRO", exchange="NSE",
        initial_txn_date=date(2025, 5, 5), initial_quantity=50, initial_price=330,
    )
    merged = engine.update_stock_holding(
        asset_id=stock.asset_id, member_id=member_id, broker="HDFC Bank", symbol="HINDPETRO",
        exchange="NSE",
    )
    assert merged.asset_id == other.asset_id and merged.quantity == D("550")
    assert len(engine.get_stock_portfolio(family_id).rows) == 1


def test_delete_a_holding(setup):
    engine, family_id, _, _, stock = setup
    engine.delete_stock(stock.asset_id)
    assert engine.get_stock_portfolio(family_id).rows == ()


def test_stock_edit_routes_over_http(server):
    family_id, member_id = _family_with_member(server)
    _, body = _post(server, f"/api/members/{member_id}/stocks", {
        "broker": "Zerodha", "symbol": "INFY", "exchange": "NSE", "initial_txn_date": "2024-01-02",
        "initial_quantity": "10", "initial_price": "1500",
    })
    stock = body["stock"]
    txn_id = stock["transactions"][0]["stock_txn_id"]
    status, body = _post(server, f"/api/stock-transactions/{txn_id}/update", {
        "txn_date": "2024-01-03", "txn_type": "BUY", "quantity": "12", "price": "1450",
    })
    assert status == 200 and Decimal(body["stock"]["quantity"]) == 12
    status, body = _post(server, f"/api/stocks/{stock['asset_id']}/update", {
        "owner_id": member_id, "broker": "Groww", "symbol": "INFY", "exchange": "NSE",
    })
    assert status == 200 and body["stock"]["broker"] == "Groww"
    status, body = _post(server, f"/api/stock-transactions/{txn_id}/delete", {})
    assert status == 400 and "only entry" in body["error"]
    status, _ = _post(server, f"/api/stocks/{stock['asset_id']}/delete", {})
    assert status == 200
    _, body = _get(server, f"/api/families/{family_id}/stocks")
    assert body["rows"] == []
