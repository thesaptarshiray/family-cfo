"""Correcting a mutual fund by hand: its details, today's figures, and its
purchases — e.g. after importing a CAS that is a month old."""

from datetime import date
from decimal import Decimal

import pytest

from test_api_server import _family_with_member, _get, _post, server  # noqa: F401 (fixture)
from test_cas_import import _imported_ppfas, engine_and_member  # noqa: F401 (fixture)

D = Decimal


def test_today_figures_go_into_the_cas_opening_balance(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)  # opening ₹60,000 / 1,000 units, then the Aug SIP (₹6,000 / 100)
    done = engine.set_mf_current_figures(asset_id=fund.asset_id, units="1180", invested="71000")
    assert done.units == D("1180.000") and done.invested_amount == D("71000.00")
    opening = next(t for t in done.transactions if t.is_opening_balance)
    assert (opening.units, opening.amount) == (D("1080.000"), D("65000.00"))
    assert done.needs_purchase_details  # still undated


def test_today_figures_add_a_purchase_once_the_fund_is_dated(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    engine.complete_mf_purchase_details(asset_id=fund.asset_id, invested_by="LUMPSUM",
                                        purchase_date=date(2022, 4, 1), today=date(2026, 9, 30))
    done = engine.set_mf_current_figures(asset_id=fund.asset_id, units="1150", invested="69000",
                                         as_of=date(2026, 9, 4))
    added = done.transactions[-1]
    assert (added.txn_date, added.units, added.amount) == (date(2026, 9, 4), D("50.000"), D("3000.00"))
    with pytest.raises(ValueError, match="redemptions"):
        engine.set_mf_current_figures(asset_id=fund.asset_id, units="100", invested="5000")


def test_a_purchase_can_be_corrected_and_deleted(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    sip = next(t for t in fund.transactions if not t.is_opening_balance)
    done = engine.update_mf_transaction(mf_txn_id=sip.mf_txn_id, txn_date=date(2026, 8, 5),
                                        txn_type="SIP", units="120", amount="7200")
    fixed = next(t for t in done.transactions if t.mf_txn_id == sip.mf_txn_id)
    assert (fixed.txn_date, fixed.units, fixed.nav) == (date(2026, 8, 5), D("120.000"), D("60.0000"))
    assert done.units == D("1120.000") and done.invested_amount == D("67200.00")
    done = engine.delete_mf_transaction(sip.mf_txn_id)
    assert done.units == D("1000.000")
    only = done.transactions[0].mf_txn_id
    with pytest.raises(ValueError, match="only purchase"):
        engine.delete_mf_transaction(only)


def test_fund_details_and_delete(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    done = engine.update_mf_holding(asset_id=fund.asset_id, member_id=member_id,
                                    scheme_name="Parag Parikh Flexi Cap - Direct Growth",
                                    folio_number="5550001/77", broker="Groww")
    assert (done.scheme_name, done.folio_number, done.broker) == (
        "Parag Parikh Flexi Cap - Direct Growth", "5550001/77", "Groww")
    engine.delete_mf(fund.asset_id)
    assert all(m.asset_id != fund.asset_id for m in engine.list_mfs(member_id))


def test_mf_edit_routes(server):  # noqa: F811
    family_id, member_id = _family_with_member(server)
    status, body = _post(server, f"/api/members/{member_id}/mfs", {
        "amc": "PPFAS", "scheme_name": "Parag Parikh Flexi Cap", "folio_number": "F1",
        "initial_txn_date": "2025-01-10", "initial_txn_type": "LUMPSUM", "initial_units": "100",
        "initial_nav": "50", "initial_amount": "5000",
    })
    assert status == 200, body
    asset_id = body["mf"]["asset_id"]
    status, body = _post(server, f"/api/mfs/{asset_id}/figures",
                         {"units": "120", "invested": "6200", "as_of": "2025-02-10"})
    assert status == 200 and Decimal(body["mf"]["units"]) == 120, body
    txn_id = body["mf"]["transactions"][-1]["mf_txn_id"]
    status, body = _post(server, f"/api/mf-transactions/{txn_id}/update",
                         {"txn_date": "2025-02-11", "txn_type": "SIP", "units": "20", "amount": "1300"})
    assert status == 200 and Decimal(body["mf"]["invested_amount"]) == 6300, body
    status, body = _post(server, f"/api/mfs/{asset_id}/update",
                         {"owner_id": member_id, "scheme_name": "PPFAS Flexi", "folio_number": "F1", "broker": "ET Money"})
    assert status == 200 and body["mf"]["broker"] == "ET Money", body
    status, _ = _post(server, f"/api/mfs/{asset_id}/delete", {})
    assert status == 200


# ---------------------------------------------------------------- next month's CAS after hand edits

def _next_month(sip=True):
    """September's statement for the PPFAS folio: one more SIP of 100 units."""
    import dataclasses
    from test_cas_import import _statement
    st = _statement()
    ppfas = st.mf_folios[0]
    aug = st.mf_transactions[0]
    sep = dataclasses.replace(aug, txn_date=date(2026, 9, 7))
    return dataclasses.replace(
        st, period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        mf_folios=(dataclasses.replace(ppfas, units=D("1200.000"), total_cost=D("72000.00"), nav=D("82.0000")),),
        mf_transactions=(sep,) if sip else (), demat_mfs=(), equities=(), sgbs=(), policies=(),
    )


def test_next_cas_replaces_a_hand_top_up_instead_of_counting_twice(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    engine.complete_mf_purchase_details(asset_id=fund.asset_id, invested_by="LUMPSUM",
                                        purchase_date=date(2022, 4, 1), today=date(2026, 9, 30))
    engine.set_mf_current_figures(asset_id=fund.asset_id, units="1200", invested="72000", as_of=date(2026, 9, 10))
    st = _next_month()
    item = next(i for i in engine.preview_statement_import(member_id=member_id, statement=st).items
                if i.key == "MF:5550001:INF879O01019")
    assert item.action == "UPDATE" and any("hand-entered top-up" in c for c in item.changes)
    engine.apply_statement_import(member_id=member_id, statement=st, selected=[item.key])
    done = next(m for m in engine.list_mfs(member_id) if m.asset_id == fund.asset_id)
    assert done.units == D("1200.000") and done.invested_amount == D("72000.00")
    assert not any(t.description == "Brought up to date by hand" for t in done.transactions)
    assert [t.txn_date for t in done.transactions][-1] == date(2026, 9, 7)


def test_next_cas_corrects_an_undated_opening_balance_to_the_statement(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    engine.set_mf_current_figures(asset_id=fund.asset_id, units="1200", invested="72000")  # folded into opening
    st = _next_month()
    engine.apply_statement_import(member_id=member_id, statement=st, selected=["MF:5550001:INF879O01019"])
    done = next(m for m in engine.list_mfs(member_id) if m.asset_id == fund.asset_id)
    assert done.units == D("1200.000") and done.invested_amount == D("72000.00")
    opening = next(t for t in done.transactions if t.is_opening_balance)
    assert opening.units == D("1000.000")


def test_a_running_sip_missing_from_the_statement_is_pointed_out(engine_and_member):
    engine, _, member_id = engine_and_member
    _imported_ppfas(engine, member_id)
    item = engine.preview_statement_import(member_id=member_id, statement=_next_month(sip=False)).items[0]
    assert any("No SIP instalment" in n for n in item.notes)
