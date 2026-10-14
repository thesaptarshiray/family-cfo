"""CAS import: reading an NSDL CAS (`api.cas_parser`) and importing it
(`FinancialEngine.preview_statement_import` / `apply_statement_import`).

The statement below is made up, laid out the way pypdf reads a real NSDL
CAS (a wrapped scheme name, a CDSL account whose rows have no symbol and
nine balance columns, a policy spread over several lines...)."""

import base64
from datetime import date
from decimal import Decimal

import pytest

import api.server as server_module
from api.cas_parser import CasParseError, parse_cas_text
from financial_engine import FinancialEngine
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema
from test_api_server import _family_with_member, _get, _post, server  # noqa: F401 (fixture)

D = Decimal

PAGE_SUMMARY = """Consolidated Account Statement
Summary Holdings Transactions Your Account About NSDL
National Securities Depository Limited 2
NSDL ID: 100000001
ASHA  MEHTA
12 Park Street
Statement for the period from 01-Aug-2026  to  31-Aug-2026
 YOUR CONSOLIDATED PORTFOLIO VALUE  ` 4,11,000.00  Summary
PORTFOLIO COMPOSITION
ASSET CLASS Value in  ` %
Equities (E) 60,000.00 14.60%
Mutual Fund Folios (F) 3,00,000.00 72.99%
TOTAL 4,11,000.00
NSDL Demat Account ACCOUNT HOLDER
HDFC BANK LIMITED ASHA MEHTA (PAN:  [ABXXXXXX1Z])
DP ID : IN300000  Client ID: 11112222
Equities (E)
 Equity Shares
ISIN
Stock Symbol Company Name Face Value in  ` No. of Shares Market Price in  ` Value in  `
INE062A01020
SBIN.NSE STATE BANK OF INDIA 1.00 50 1,000.00 50,000.00
Sub Total 50,000.00
Mutual Funds (M)
ISIN ISIN Description No. of Units NAV in  ` Value in  `
INF204KB15V2 NIPPON INDIA ETF NIFTY IT 100.000 34.68 3,467.52
Sub Total 3,467.52
Sovereign Gold Bonds (SGB)
ISIN Issuer Name Coupon Rate/
Frequency Maturity Date No. of Units Face Value Per
IN0020230093 CENTRAL GOVERNMENT 2.50 20-Sep-2031 5 5,923.00 15,000.00 75,000.00
Sub Total 75,000.00
Total 1,28,467.52
"""

PAGE_CDSL_AND_FOLIOS = """Consolidated Account Statement
Summary Holdings Transactions Your Account About NSDL
National Securities Depository Limited 5
CDSL Demat Account ACCOUNT HOLDER
GROWW INVEST TECH PRIVATE LIMITED ASHA MEHTA . . (PAN:  [ABXXXXXX1Z])
DP ID : 12088704  Client ID: 33334444
Equities (E)
INE053F01010 INDIAN RAILWAY FINANCE CORPORATION LIMITED #
EQUITY SHARES
20.000
20.000
0.000
0.000
0.000
0.000
0.000
0.000
0.000
83.30 1,666.00
Sub Total 1,666.00
Mutual Funds (M)
INF879O01027 PPFAS MUTUAL FUND
100.000
100.000
0.000
0.000
0.000
0.000
0.000
0.000
0.000
91.18 9,118.00
Sub Total 9,118.00
Mutual Fund Folios (F)
ISIN UCC ISIN Description Folio No. No. of Units
Average
Cost Per
INF879O01019
MFPPFA0001
Parag Parikh Flexi Cap Fund - Regular
Plan Growth 5550001 1,100.000 60.0000 66,000.00 80.0000 88,000.00 22,000.00
INF579M01AF8
NOT AVAILABLE
360 ONE Quant Fund Regular Plan
Growth 5550002 1,000.000 20.0000 20,000.00 21.0000 21,000.00 1,000.00
Sub Total 1,09,000.00
Total 1,09,000.00
NSDL NATIONAL INSURANCE REPOSITORY
e-INSURANCE ACCOUNT (eIA) HOLDING DETAILS
LIFE INSURANCE POLICIES
Life Assured Insurance Company
Name Policy Name Policy No. Status Premium
Installment  `
Premium
Frequency Sum Assured  `
MRS ASHA MEHTA HDFC Life Insurance
Company Limited
HDFCLife Click2
Protect Elite 90001234 Look in Cancellation 1,500.00 Monthly 1,00,00,000.00
ASHA MEHTA Tata AIA Life Insurance
Company Limited
Tata AIA Life
Insurance Value
Income Plan
c900000001 Policy Inforce 50,000.00 Yearly 5,00,000.00
Total 1,05,00,000.00
"""

PAGE_TRANSACTIONS = """Consolidated Account Statement
Summary Holdings Transactions Your Account About NSDL
National Securities Depository Limited 7
Mutual Funds Transaction Statement for the Period from 01-Aug-2026 to 31-Aug-2026
Date Transaction Details Amount in ` Stamp Duty
ISIN : INF879O01019 - PPFAS Mutual Fund - Scheme Name : 001RG -  Parag Parikh Flexi Cap Fund - Regular Plan
Growth Folio-no -  5550001
Opening Balance 1,000.000
07-Aug-2026 Purchase Systematic Instalment No - 12/60 5,999.70 0.30 60.0000 60.0000 100.000
Closing Balance 1,100.000
***End of Statement***
Folio No.
AMC NAME
HOLDER DETAILS
5550001
PPFAS Mutual Fund
Sole Holder
Asha Mehta
5550002
360 ONE Mutual Fund
Sole Holder
Asha Mehta
"""

PAGES = [PAGE_SUMMARY, PAGE_CDSL_AND_FOLIOS, PAGE_TRANSACTIONS]


def _statement():
    return parse_cas_text(PAGES)


# ----------------------------------------------------------------------
# Reading the statement
# ----------------------------------------------------------------------


def test_parser_reads_every_section():
    st = _statement()
    assert (st.period_from, st.period_to, st.holder_name) == (date(2026, 8, 1), date(2026, 8, 31), "ASHA MEHTA")

    ppfas, quant = st.mf_folios
    assert (ppfas.isin, ppfas.folio, ppfas.units, ppfas.total_cost, ppfas.nav) == (
        "INF879O01019", "5550001", D("1100.000"), D("66000.00"), D("80.0000"))
    assert ppfas.scheme_name == "Parag Parikh Flexi Cap Fund - Regular Plan Growth"
    assert ppfas.amc == "PPFAS Mutual Fund"
    assert quant.scheme_name == "360 ONE Quant Fund Regular Plan Growth"

    (txn,) = st.mf_transactions
    assert (txn.folio, txn.txn_date, txn.amount, txn.stamp_duty, txn.units) == (
        "5550001", date(2026, 8, 7), D("5999.70"), D("0.30"), D("100.000"))

    sbin, irfc = st.equities
    assert (sbin.symbol, sbin.exchange, sbin.quantity, sbin.price) == ("SBIN", "NSE", D("50"), D("1000.00"))
    assert sbin.account.dp_name == "HDFC BANK LIMITED"
    assert (irfc.symbol, irfc.quantity, irfc.company) == (None, D("20.000"), "INDIAN RAILWAY FINANCE CORPORATION LIMITED")
    assert irfc.account.dp_name == "GROWW INVEST TECH PRIVATE LIMITED"

    etf, groww_mf = st.demat_mfs
    assert (etf.name, etf.units) == ("NIPPON INDIA ETF NIFTY IT", D("100.000"))
    assert (groww_mf.name, groww_mf.units, groww_mf.nav) == ("PPFAS MUTUAL FUND", D("100.000"), D("91.18"))

    (sgb,) = st.sgbs
    assert (sgb.units, sgb.issue_price, sgb.maturity_date) == (D("5"), D("5923.00"), date(2031, 9, 20))

    term, endowment = st.policies
    assert (term.policy_number, term.status, term.frequency) == ("90001234", "Look in Cancellation", "Monthly")
    assert term.insurer == "HDFC Life Insurance Company Limited"
    assert term.plan_name == "HDFCLife Click2 Protect Elite"
    assert (endowment.policy_number, endowment.premium, endowment.sum_assured) == (
        "c900000001", D("50000.00"), D("500000.00"))


def test_a_pdf_without_text_is_refused_with_directions():
    with pytest.raises(CasParseError, match="Save as PDF"):
        parse_cas_text(["", ""])


def test_something_that_is_not_a_cas_is_refused():
    with pytest.raises(CasParseError, match="NSDL"):
        parse_cas_text(["Monthly bank statement\nOpening balance 1,000.00"])


# ----------------------------------------------------------------------
# Importing it
# ----------------------------------------------------------------------


@pytest.fixture
def engine_and_member(tmp_path):
    database = Database(tmp_path / "cas.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Mehta")
    member = family_repo.create_family_member(family.family_id, "Asha", "self")
    return FinancialEngine(database), family.family_id, member.member_id


def _preview(engine, member_id, **kw):
    return engine.preview_statement_import(
        member_id=member_id, statement=_statement(), isin_symbols={"INE053F01010": "IRFC"},
        scheme_names={"INF879O01027": "Parag Parikh Flexi Cap Fund - Direct Growth"}, **kw,
    )


def _by_key(preview):
    return {item.key: item for item in preview.items}


def test_preview_saves_nothing_and_classifies_each_holding(engine_and_member):
    engine, family_id, member_id = engine_and_member
    items = _by_key(_preview(engine, member_id))
    assert items["MF:5550001:INF879O01019"].action == "NEW"
    assert items["DMF:33334444:INF879O01027"].title == "Parag Parikh Flexi Cap Fund - Direct Growth"
    assert items["EQ:33334444:INE053F01010"].title == "IRFC"
    assert items["INS:90001234"].action == "CHECK"  # not in force
    assert items["INS:c900000001"].needs_start_date
    assert engine.get_mf_portfolio(family_id).rows == ()


def test_apply_imports_folios_with_opening_balance_and_the_months_sip(engine_and_member):
    engine, family_id, member_id = engine_and_member
    preview = _preview(engine, member_id)
    result = engine.apply_statement_import(
        member_id=member_id, statement=_statement(),
        selected=[i.key for i in preview.items if i.selectable],
        policy_start_dates={"INS:c900000001": date(2020, 5, 10)},
        isin_symbols={"INE053F01010": "IRFC"},
    )
    assert result.items_applied == 8

    mf = engine.get_mf_portfolio(family_id)
    ppfas = next(r.mf for r in mf.rows if r.mf.folio_number == "5550001")
    assert ppfas.units == D("1100.000")
    assert ppfas.invested_amount == D("66000.00")  # 60,000 opening + 6,000 SIP
    opening, sip = ppfas.transactions
    assert opening.is_opening_balance and (opening.units, opening.amount) == (D("1000.000"), D("60000.00"))
    assert (sip.txn_type, sip.amount, sip.txn_date) == ("SIP", D("6000.00"), date(2026, 8, 7))
    assert ppfas.sip_enabled and ppfas.sip_amount == D("6000.00")
    assert ppfas.xirr is None and mf.xirr is None and mf.xirr_excluded_count == 4

    stocks = engine.get_stock_portfolio(family_id)
    assert {(r.stock.symbol, r.stock.broker, r.stock.quantity) for r in stocks.rows} == {
        ("SBIN", "HDFC Bank", D("50")), ("IRFC", "Groww Invest Tech", D("20")),
    }
    gold = engine.get_gold_portfolio(family_id)
    assert gold.rows[0].gold.current_value == D("75000.00")
    (policy,) = engine.list_insurance_policies(member_id)
    assert (policy.policy_number, policy.policy_type, policy.premium_frequency) == ("c900000001", "ENDOWMENT", "ANNUAL")
    assert (policy.insurer, policy.plan_name) == ("Tata AIA Life Insurance", "Value Income Plan")
    assert policy.review.known_plan == "Guaranteed-income plan"


def test_importing_the_same_statement_again_changes_nothing(engine_and_member):
    engine, _, member_id = engine_and_member
    preview = _preview(engine, member_id)
    engine.apply_statement_import(
        member_id=member_id, statement=_statement(),
        selected=[i.key for i in preview.items if i.selectable],
        policy_start_dates={"INS:c900000001": date(2020, 5, 10)},
        isin_symbols={"INE053F01010": "IRFC"},
    )
    again = _preview(engine, member_id)
    assert {i.action for i in again.items} == {"SAME", "CHECK"}
    assert not any(i.selectable for i in again.items)


def test_next_month_adds_only_the_new_sip(engine_and_member):
    engine, family_id, member_id = engine_and_member
    engine.apply_statement_import(
        member_id=member_id, statement=_statement(), selected=["MF:5550001:INF879O01019"],
    )
    september = PAGE_TRANSACTIONS.replace(
        "07-Aug-2026 Purchase Systematic Instalment No - 12/60 5,999.70 0.30 60.0000 60.0000 100.000",
        "07-Sep-2026 Purchase Systematic Instalment No - 13/60 5,999.70 0.30 66.6667 66.6667 90.000",
    ).replace("01-Aug-2026 to 31-Aug-2026", "01-Sep-2026 to 30-Sep-2026")
    pages = [
        PAGE_SUMMARY.replace("01-Aug-2026  to  31-Aug-2026", "01-Sep-2026  to  30-Sep-2026"),
        PAGE_CDSL_AND_FOLIOS.replace(
            "Plan Growth 5550001 1,100.000 60.0000 66,000.00",
            "Plan Growth 5550001 1,190.000 60.5042 72,000.00",
        ),
        september,
    ]
    item = _by_key(engine.preview_statement_import(member_id=member_id, statement=parse_cas_text(pages)))[
        "MF:5550001:INF879O01019"]
    assert item.action == "UPDATE" and len(item.changes) == 1 and not item.notes
    engine.apply_statement_import(
        member_id=member_id, statement=parse_cas_text(pages), selected=["MF:5550001:INF879O01019"],
    )
    ppfas = engine.get_mf_portfolio(family_id).rows[0].mf
    assert ppfas.units == D("1190.000") and ppfas.invested_amount == D("72000.00")


def test_a_new_policy_needs_its_start_date(engine_and_member):
    engine, _, member_id = engine_and_member
    with pytest.raises(ValueError, match="start date"):
        engine.apply_statement_import(
            member_id=member_id, statement=_statement(), selected=["INS:c900000001"],
        )


def test_a_share_sold_outside_the_app_is_flagged_not_imported(engine_and_member):
    engine, _, member_id = engine_and_member
    engine.create_stock(
        member_id=member_id, broker="HDFC Securities", symbol="SBIN", exchange="NSE",
        initial_txn_date=date(2024, 1, 5), initial_quantity=80, initial_price=600,
    )
    item = _by_key(_preview(engine, member_id))["EQ:11112222:INE062A01020"]
    assert item.action == "CHECK" and not item.selectable
    assert "80" in item.notes[0] and "50" in item.notes[0]


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


def test_cas_routes_over_http(server, monkeypatch):
    monkeypatch.setattr(server_module, "parse_cas_pdf", lambda data: parse_cas_text(PAGES))
    family_id, member_id = _family_with_member(server)
    pdf = base64.b64encode(b"%PDF-1.4 stand-in").decode()

    status, body = _post(server, f"/api/members/{member_id}/cas/preview", {"pdf_base64": pdf})
    assert status == 200, body
    keys = [i["key"] for i in body["preview"]["items"] if i["selectable"] and not i["needs_start_date"]]
    status, body = _post(server, f"/api/members/{member_id}/cas/apply",
                         {"pdf_base64": pdf, "selected": keys})
    assert status == 200 and body["result"]["items_applied"] == len(keys)
    status, body = _get(server, f"/api/families/{family_id}/mfs")
    assert len(body["rows"]) == 4

    status, body = _post(server, f"/api/members/{member_id}/cas/preview", {"pdf_base64": ""})
    assert status == 400 and "choose the CAS PDF" in body["error"]


# ----------------------------------------------------------------------
# Completing an imported fund's purchase details (so XIRR works)
# ----------------------------------------------------------------------


def _imported_ppfas(engine, member_id):
    engine.apply_statement_import(
        member_id=member_id, statement=_statement(), selected=["MF:5550001:INF879O01019"],
    )
    return next(m for m in engine.list_mfs(member_id) if m.folio_number == "5550001")


def test_imported_fund_is_marked_as_needing_details(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    assert fund.needs_purchase_details and fund.opening_amount == D("60000.00")
    assert fund.sip_status == "RUNNING"  # the statement showed a monthly SIP


def test_lumpsum_date_makes_xirr_exact(engine_and_member):
    engine, family_id, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    done = engine.complete_mf_purchase_details(
        asset_id=fund.asset_id, invested_by="LUMPSUM", purchase_date=date(2022, 4, 1),
        today=date(2026, 9, 30),
    )
    assert not done.needs_purchase_details and done.sip_status == "STOPPED"  # the Aug SIP row stays SIP
    first = done.transactions[0]
    assert (first.txn_date, first.txn_type, first.amount) == (date(2022, 4, 1), "LUMPSUM", D("60000.00"))
    assert done.xirr is not None
    assert engine.get_mf_portfolio(family_id).xirr_excluded_count == 0


def test_sip_with_current_xirr_reproduces_that_xirr(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    today = date(2026, 9, 30)
    done = engine.complete_mf_purchase_details(
        asset_id=fund.asset_id, invested_by="SIP_RUNNING", current_xirr_pct="14",
        sip_amount="6000", sip_date=7, today=today,
    )
    assert abs(done.xirr - D("0.14")) < D("0.001")
    assert done.transactions[0].txn_type == "SIP" and done.transactions[0].txn_date < date(2026, 8, 7)
    assert done.sip_status == "RUNNING" and done.sip_amount == D("6000")
    assert done.units == D("1100.000") and done.invested_amount == D("66000.00")  # nothing else moved


def test_stopped_sip_is_classified_stopped(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    done = engine.complete_mf_purchase_details(
        asset_id=fund.asset_id, invested_by="SIP_STOPPED", current_xirr_pct="12", today=date(2026, 9, 30),
    )
    assert done.sip_status == "STOPPED" and not done.sip_enabled


@pytest.mark.parametrize("kwargs, message", [
    (dict(invested_by="LUMPSUM"), "purchase date"),
    (dict(invested_by="LUMPSUM", purchase_date=date(2026, 9, 1)), "on or before"),
    (dict(invested_by="SIP_RUNNING"), "XIRR"),
    (dict(invested_by="SIP_RUNNING", current_xirr_pct="0"), "non-zero"),
    (dict(invested_by="SIP_RUNNING", current_xirr_pct="-20"), "does not fit"),
])
def test_purchase_details_are_checked(engine_and_member, kwargs, message):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    with pytest.raises(ValueError, match=message):
        engine.complete_mf_purchase_details(asset_id=fund.asset_id, today=date(2026, 9, 30), **kwargs)


# ----------------------------------------------------------------------
# Brokers
# ----------------------------------------------------------------------


def test_demat_funds_take_their_broker_from_the_statement(engine_and_member):
    engine, _, member_id = engine_and_member
    engine.apply_statement_import(
        member_id=member_id, statement=_statement(), selected=["DMF:33334444:INF879O01027"],
    )
    (fund,) = engine.list_mfs(member_id)
    assert fund.broker == "Groww"


def test_broker_totals_and_setting_a_broker(engine_and_member):
    engine, family_id, member_id = engine_and_member
    preview = _preview(engine, member_id)
    engine.apply_statement_import(
        member_id=member_id, statement=_statement(),
        selected=[i.key for i in preview.items if i.key.startswith(("MF", "DMF"))],
    )
    ppfas = next(m for m in engine.list_mfs(member_id) if m.folio_number == "5550001")
    engine.set_mf_broker(asset_id=ppfas.asset_id, broker=" FundsIndia ")
    engine.complete_mf_purchase_details(
        asset_id=ppfas.asset_id, invested_by="LUMPSUM", purchase_date=date(2023, 1, 2),
    )

    by_broker = {b.broker: b for b in engine.get_mf_portfolio(family_id).by_broker}
    assert set(by_broker) == {"FundsIndia", "Groww", "HDFC Bank", None}  # the ETF sits in an HDFC Bank demat
    assert by_broker["FundsIndia"].funds == 1 and by_broker["FundsIndia"].xirr is not None
    assert by_broker["FundsIndia"].invested == D("66000.00")
    assert by_broker["Groww"].xirr is None and by_broker["Groww"].xirr_excluded_count == 1
    assert list(by_broker)[-1] is None  # "not set" listed last

    engine.set_mf_broker(asset_id=ppfas.asset_id, broker="")
    assert next(m for m in engine.list_mfs(member_id) if m.asset_id == ppfas.asset_id).broker is None


# ----------------------------------------------------------------------
# CAMS / KFintech (MF Central) mutual fund CAS
# ----------------------------------------------------------------------

MF_CAS_PAGES = ["""Version-1027
Page 1 of 2
Mutual Fund Consolidated Account Statement
Financial Transactions during the period 01-Aug-2026 to 31-Aug-2026
Meera Das SEBI has mandated all Mutual Funds to issue Monthly Consolidated
Summary of Holdings as on 31-Aug-2026
Folio No Scheme Code -Scheme
NameISIN Unit Balance NAV Cost Value (/uni20B9)Gain Market Value (/uni20B9)
UTI MUTUAL FUND
777000111 NIGP-UTI Nifty 50 Index
Fund - Regular Plan
INF789F01JN2 300.000 160.0000 45,000.00 6.67% 48,000.00
UTI MUTUAL FUND - Total 48,000.00
HDFC Mutual Fund
888000222 02-HDFC Flexi Cap Fund
- Regular Plan - Growth
INF179K01608 50.000 2,000.0000 90,000.00 11.11% 1,00,000.00
HDFC Mutual Fund - Total 1,00,000.00
Portfolio Value 1,48,000.00
Transaction Details
""", """Version-1027
Page 2 of 2
Transaction Details
UTI MUTUAL FUND
UTI Nifty 50 Index Fund - Regular Plan
Folio No. : 777000111
ISIN :INF789F01JN2
Mode of Holding : Single
250.000 Opening Balance
UnitsTransactionDate Amount (/uni20B9) NAV (/uni20B9) Price (/uni20B9)
50.000159.9920159.9920Sys. Investment04-Aug-2026 7,999.60
300.000 Closing Balance
Load Structures
"""]


def test_mf_cas_is_read_with_its_holdings_and_sips():
    st = parse_cas_text(MF_CAS_PAGES)
    assert st.source == "CAMS / KFintech CAS" and st.holder_name == "Meera Das"
    uti, hdfc = st.mf_folios
    assert (uti.folio, uti.scheme_name, uti.units, uti.total_cost, uti.amc) == (
        "777000111", "UTI Nifty 50 Index Fund - Regular Plan", D("300.000"), D("45000.00"), "UTI MUTUAL FUND")
    assert hdfc.scheme_name == "HDFC Flexi Cap Fund - Regular Plan - Growth" and hdfc.nav == D("2000.0000")
    (txn,) = st.mf_transactions
    assert (txn.units, txn.nav, txn.amount + txn.stamp_duty, txn.txn_date) == (
        D("50.000"), D("159.9920"), D("8000.00"), date(2026, 8, 4))


def test_mf_cas_imports_with_the_running_sip(engine_and_member):
    engine, family_id, member_id = engine_and_member
    st = parse_cas_text(MF_CAS_PAGES)
    preview = engine.preview_statement_import(member_id=member_id, statement=st)
    engine.apply_statement_import(member_id=member_id, statement=st,
                                  selected=[i.key for i in preview.items if i.selectable])
    p = engine.get_mf_portfolio(family_id)
    uti = next(r.mf for r in p.rows if r.mf.folio_number == "777000111")
    assert uti.units == D("300.000") and uti.invested_amount == D("45000.00")
    assert uti.sip_enabled and uti.sip_amount == D("8000.00")


def test_sip_plus_lumpsum_splits_the_opening_balance(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)  # opening ₹60,000 / 1,000 units, then the Aug SIP
    today = date(2026, 9, 30)
    done = engine.complete_mf_purchase_details(
        asset_id=fund.asset_id, invested_by="SIP_RUNNING", current_xirr_pct="14",
        lumpsum_amount="20000", lumpsum_date=date(2023, 4, 10), today=today,
    )
    assert done.units == D("1100.000") and done.invested_amount == D("66000.00")  # totals unchanged
    assert done.lumpsum_invested == D("20000") and done.sip_invested == D("46000.00")
    assert done.lumpsum_units == D("333.333")  # shared out at the average cost
    assert abs(done.xirr - D("0.14")) < D("0.001")  # the whole fund matches the XIRR entered
    assert done.sip_xirr is not None and done.lumpsum_xirr is not None
    assert done.sip_value + done.lumpsum_value == (done.units * done.current_nav).quantize(D("0.01"))


@pytest.mark.parametrize("kwargs, message", [
    (dict(lumpsum_amount="70000", lumpsum_date=date(2023, 1, 1)), "more than"),
    (dict(lumpsum_amount="1000"), "lumpsum's date"),
    (dict(lumpsum_amount="1000", lumpsum_date=date(2023, 1, 1), lumpsum_units="5000"), "units"),
])
def test_sip_plus_lumpsum_is_checked(engine_and_member, kwargs, message):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    with pytest.raises(ValueError, match=message):
        engine.complete_mf_purchase_details(asset_id=fund.asset_id, invested_by="SIP_STOPPED",
                                            current_xirr_pct="12", today=date(2026, 9, 30), **kwargs)


def test_a_lumpsum_of_the_whole_opening_balance_needs_no_xirr(engine_and_member):
    """The SIP started after a lumpsum: the lumpsum is the whole opening
    balance (₹10 short for stamp duty) — it is dated, no XIRR needed."""
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    done = engine.complete_mf_purchase_details(
        asset_id=fund.asset_id, invested_by="SIP_RUNNING", lumpsum_amount="59990",
        lumpsum_date=date(2026, 6, 25), sip_amount="10000", sip_date=4, today=date(2026, 9, 30),
    )
    first = done.transactions[0]
    assert (first.txn_date, first.txn_type, first.amount, first.units) == (
        date(2026, 6, 25), "LUMPSUM", D("60000.00"), D("1000.000"))
    assert not done.needs_purchase_details and done.sip_status == "RUNNING"
    assert done.xirr is not None and done.sip_amount == D("10000")


def test_a_lumpsum_that_leaves_no_room_for_the_sip_part_explains_itself(engine_and_member):
    engine, _, member_id = engine_and_member
    fund = _imported_ppfas(engine, member_id)
    with pytest.raises(ValueError, match="lumpsum alone"):
        engine.complete_mf_purchase_details(
            asset_id=fund.asset_id, invested_by="SIP_RUNNING", current_xirr_pct="-6",
            lumpsum_amount="59000", lumpsum_date=date(2026, 6, 25), today=date(2026, 9, 30),
        )


def test_the_bilingual_layout_reads_the_same():
    """Since September 2026 NSDL prints each Hindi heading glued to the
    English one, plus Hindi-only lines; the statement must read the same."""
    hindi = {
        "Mutual Fund Folios (F)\n": "Mutual Fund Folios (F)म्यूचुअल फंड (F)\nकुल\n",
        " Equity Shares\n": " Equity Sharesइक्विटी शेयर\n",
        "Mutual Funds (M)\n": "Mutual Funds (M)म्यूचुअल � (M)\n",
        "Sovereign Gold Bonds (SGB)\n": "Sovereign Gold Bonds (SGB)सॉवरेन (SGB)\n",
        "LIFE INSURANCE POLICIES\n": "LIFE INSURANCE POLICIESजीवन बीमा\n",
    }
    pages = []
    for page in PAGES:
        for english, both in hindi.items():
            page = page.replace(english, both)
        pages.append(page)
    assert pages != PAGES
    assert parse_cas_text(pages) == _statement()
