"""
Smoke tests for the local dashboard server: the server actually serves the
dashboard page, and the API routes reach the Financial Engine correctly over
real HTTP. Not exhaustive of the Engine's own behaviour — that is covered in
`tests/test_financial_engine.py` — this only proves the routing, request
parsing, and JSON encoding work.
"""

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal

import pytest

from api.auth import AuthStore
from api.server import create_server
from financial_engine import FinancialEngine
from repository.database import Database
from repository.schema import initialize_schema

_TEST_PASSWORD = "test-password-123"


def _start_server(tmp_path, **server_kwargs):
    """Build, bind and start a server on its own thread. Not authenticated —
    `server.opener` is unset, so requests through it carry no session cookie
    until something logs in (see the `server` fixture, which does).

    `server_kwargs` pass through to `create_server` (e.g. `sync_state`,
    `nav_fetcher`). By default neither is given, so auto-sync is off and no
    test ever reaches out to AMFI unless it asks to.
    """
    database = Database(tmp_path / "api.db")
    initialize_schema(database)
    engine = FinancialEngine(database)
    auth_store = AuthStore(tmp_path / "auth.json")
    srv = create_server(  # port 0: OS picks one
        engine, auth_store, host="127.0.0.1", port=0, **server_kwargs
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


@pytest.fixture
def unauthenticated_server(tmp_path):
    """A running server with no password set yet and no session — for
    testing the auth gate itself."""
    srv, thread = _start_server(tmp_path)
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


@pytest.fixture
def server(tmp_path):
    """A running server, already past the one-time password setup and
    logged in — what every non-auth-specific test wants, so it can exercise
    the FD/family/member routes without also testing the login flow."""
    srv, thread = _start_server(tmp_path)
    srv.opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    try:
        status, body = _post(srv, "/api/auth/set-password", {"password": _TEST_PASSWORD})
        assert status == 200, body  # fail fast if the fixture itself is broken
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def _url(server, path: str) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


def _request(server, path: str, *, method: str = "GET", payload=None):
    """Every HTTP call the tests make goes through here. Uses
    `server.opener` (a cookie-jar-backed opener, carrying the session
    established by the `server` fixture) when the server has one, and plain
    `urllib.request` — no cookies at all — otherwise.

    No retry on `ConnectionAbortedError`: an earlier version retried it,
    believing it was Windows socket-timing noise. It was a server bug — POST
    handlers that answered without reading the request body (a 401, logout)
    made Windows reset the connection. The server now drains every body
    first (`_drain_body`), and a connection abort here should fail loudly.
    """
    opener = getattr(server, "opener", None)
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(_url(server, path), data=data, method=method, headers=headers)
    try:
        with open_fn(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _get(server, path: str):
    status, raw, _ = _request(server, path)
    return status, json.loads(raw)


def _get_raw(server, path: str):
    status, raw, headers = _request(server, path)
    return status, raw, headers.get("Content-Type")


def _post(server, path: str, payload: dict):
    status, raw, _ = _request(server, path, method="POST", payload=payload)
    return status, json.loads(raw)


# ----------------------------------------------------------------------
# The dashboard page itself
# ----------------------------------------------------------------------


def test_root_serves_the_dashboard_html(server):
    status, body, content_type = _get_raw(server, "/")
    assert status == 200
    assert "text/html" in content_type
    assert b"<title>Family CFO" in body


# ----------------------------------------------------------------------
# Families and members
# ----------------------------------------------------------------------


def test_families_start_empty(server):
    status, body = _get(server, "/api/families")
    assert status == 200
    assert body == {"families": []}


def test_create_family_then_list_it(server):
    status, body = _post(server, "/api/families", {"family_name": "Sharma"})
    assert status == 200
    assert body["family"]["family_name"] == "Sharma"
    family_id = body["family"]["family_id"]

    status, body = _get(server, "/api/families")
    assert status == 200
    assert [f["family_id"] for f in body["families"]] == [family_id]


def test_create_family_member(server):
    _, created = _post(server, "/api/families", {"family_name": "Sharma"})
    family_id = created["family"]["family_id"]

    status, body = _post(
        server, f"/api/families/{family_id}/members",
        {"name": "Raj", "relationship": "self"},
    )
    assert status == 200
    assert body["member"]["name"] == "Raj"

    status, body = _get(server, f"/api/families/{family_id}/members")
    assert status == 200
    assert [m["name"] for m in body["members"]] == ["Raj"]


def test_create_family_member_for_unknown_family_is_a_client_error(server):
    status, body = _post(
        server, "/api/families/999/members", {"name": "Raj", "relationship": "self"}
    )
    assert status == 400
    assert "error" in body


def test_unknown_route_is_404(server):
    status, body = _get(server, "/api/nonsense")
    assert status == 404


# ----------------------------------------------------------------------
# FDs, end to end over HTTP
# ----------------------------------------------------------------------


def _family_with_member(server):
    _, created = _post(server, "/api/families", {"family_name": "Sharma"})
    family_id = created["family"]["family_id"]
    _, created = _post(
        server, f"/api/families/{family_id}/members", {"name": "Raj", "relationship": "self"}
    )
    return family_id, created["member"]["member_id"]


def test_create_fd_over_http_and_read_it_back(server):
    family_id, member_id = _family_with_member(server)

    status, body = _post(
        server, f"/api/members/{member_id}/fds",
        {
            "bank_name": "HDFC", "fd_number": "FD-1", "principal": "100000",
            "interest_rate": "7.0", "issue_date": "2020-01-01",
            "maturity_date": "2021-01-01", "payout_type": "MATURITY",
            "maturity_value": "107000",
        },
    )
    assert status == 200
    assert body["fd"]["current_value"] == "107000.00"  # matured years ago
    assert body["fd"]["gain_loss"] == "7000.00"

    status, body = _get(server, f"/api/members/{member_id}/fds")
    assert status == 200
    assert len(body["fds"]) == 1
    assert body["fds"][0]["fd_number"] == "FD-1"


def test_create_fd_with_missing_field_is_a_client_error(server):
    _, member_id = _family_with_member(server)

    status, body = _post(
        server, f"/api/members/{member_id}/fds",
        {"bank_name": "HDFC"},  # missing required fields
    )
    assert status == 400
    assert "error" in body


def test_create_fd_with_invalid_dates_is_a_client_error_not_a_500(server):
    _, member_id = _family_with_member(server)

    status, body = _post(
        server, f"/api/members/{member_id}/fds",
        {
            "bank_name": "HDFC", "fd_number": "FD-2", "principal": "1000",
            "interest_rate": "6", "issue_date": "2026-01-01",
            "maturity_date": "2026-01-01",  # not after issue_date
            "payout_type": "MATURITY",
        },
    )
    assert status == 400
    assert "error" in body


def test_family_dashboard_reflects_a_newly_created_fd(server):
    family_id, member_id = _family_with_member(server)
    _post(
        server, f"/api/members/{member_id}/fds",
        {
            "bank_name": "HDFC", "fd_number": "FD-1", "principal": "100000",
            "interest_rate": "7.0", "issue_date": "2020-01-01",
            "maturity_date": "2021-01-01", "payout_type": "MATURITY",
            "maturity_value": "107000",
        },
    )

    status, body = _get(server, f"/api/families/{family_id}/dashboard")
    assert status == 200
    assert body["total_invested"] == "100000"
    assert body["total_current_value"] == "107000.00"
    assert body["by_category"] == {"FD": "107000.00"}
    assert len(body["members"]) == 1
    assert body["members"][0]["fds"][0]["bank_name"] == "HDFC"


def test_family_dashboard_for_unknown_family_is_404(server):
    status, body = _get(server, "/api/families/999/dashboard")
    assert status == 404
    assert "error" in body


# ----------------------------------------------------------------------
# JSON encoding: Decimal as an exact string, never a float
# ----------------------------------------------------------------------


def test_decimal_amounts_are_encoded_as_exact_strings(server):
    _, member_id = _family_with_member(server)
    _post(
        server, f"/api/members/{member_id}/fds",
        {
            "bank_name": "HDFC", "fd_number": "FD-1", "principal": "100000.10",
            "interest_rate": "7.25", "issue_date": "2026-01-01",
            "maturity_date": "2027-01-01", "payout_type": "QUARTERLY",
        },
    )

    status, body = _get(server, f"/api/members/{member_id}/fds")
    fd = body["fds"][0]
    assert isinstance(fd["principal"], str)  # a JSON string, not a float
    assert Decimal(fd["principal"]) == Decimal("100000.10")


# ----------------------------------------------------------------------
# Auth: the one-time password bootstrap, login/logout, and the gate itself
# ----------------------------------------------------------------------


def test_root_page_needs_no_auth(unauthenticated_server):
    status, _, content_type = _get_raw(unauthenticated_server, "/")
    assert status == 200
    assert "text/html" in content_type


def test_auth_status_before_any_password_is_set(unauthenticated_server):
    status, body = _get(unauthenticated_server, "/api/auth/status")
    assert status == 200
    assert body == {"password_set": False, "authenticated": False}


def test_protected_routes_are_401_with_no_session(unauthenticated_server):
    status, body = _get(unauthenticated_server, "/api/families")
    assert status == 401
    assert "error" in body

    status, body = _post(unauthenticated_server, "/api/families", {"family_name": "Sharma"})
    assert status == 401


def test_set_password_then_the_status_reflects_it_and_logs_in(unauthenticated_server):
    status, body = _get(unauthenticated_server, "/api/auth/status")
    assert body["password_set"] is False

    # No opener/cookie jar on this fixture: use a plain urllib call and
    # thread the returned cookie through manually, to prove the response
    # really does carry a usable session cookie (not just a 200).
    import urllib.request as _u
    req = _u.Request(
        _url(unauthenticated_server, "/api/auth/set-password"),
        data=json.dumps({"password": "a-real-password"}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with _u.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie and set_cookie.startswith("session=")

    req2 = _u.Request(
        _url(unauthenticated_server, "/api/families"), headers={"Cookie": set_cookie.split(";")[0]}
    )
    with _u.urlopen(req2, timeout=5) as resp:
        assert resp.status == 200  # the returned cookie is a working session


def test_set_password_twice_is_rejected(unauthenticated_server):
    _post(unauthenticated_server, "/api/auth/set-password", {"password": "first-password"})
    status, body = _post(
        unauthenticated_server, "/api/auth/set-password", {"password": "second-password"}
    )
    assert status == 400
    assert "error" in body


def test_short_password_is_rejected(unauthenticated_server):
    status, body = _post(unauthenticated_server, "/api/auth/set-password", {"password": "ab"})
    assert status == 400
    assert unauthenticated_server.auth_store.is_password_set() is False


def test_login_with_the_correct_password_grants_a_session(unauthenticated_server):
    _post(unauthenticated_server, "/api/auth/set-password", {"password": "a-real-password"})

    status, body = _get(unauthenticated_server, "/api/auth/status")
    assert body == {"password_set": True, "authenticated": False}  # a fresh request, no cookie

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    unauthenticated_server.opener = opener
    status, body = _post(unauthenticated_server, "/api/auth/login", {"password": "a-real-password"})
    assert status == 200
    assert body == {"ok": True}

    status, body = _get(unauthenticated_server, "/api/auth/status")
    assert body["authenticated"] is True
    status, _ = _get(unauthenticated_server, "/api/families")
    assert status == 200


def test_login_with_the_wrong_password_is_401(unauthenticated_server):
    _post(unauthenticated_server, "/api/auth/set-password", {"password": "a-real-password"})

    status, body = _post(unauthenticated_server, "/api/auth/login", {"password": "wrong"})
    assert status == 401
    assert "error" in body


def test_logout_ends_the_session(server):
    status, _ = _get(server, "/api/families")
    assert status == 200  # the `server` fixture starts out logged in

    status, body = _post(server, "/api/auth/logout", {})
    assert status == 200
    assert body == {"ok": True}

    status, _ = _get(server, "/api/families")
    assert status == 401  # the same opener/cookie no longer works


def test_an_unrelated_cookie_value_is_not_a_valid_session(unauthenticated_server):
    import urllib.request as _u
    req = _u.Request(
        _url(unauthenticated_server, "/api/families"),
        headers={"Cookie": "session=made-up-token-nobody-issued"},
    )
    try:
        with _u.urlopen(req, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 401


# ----------------------------------------------------------------------
# Insurance, end to end over HTTP
# ----------------------------------------------------------------------


def test_create_insurance_policy_over_http_and_read_it_back(server):
    _, member_id = _family_with_member(server)

    status, body = _post(
        server, f"/api/members/{member_id}/insurance",
        {
            "policy_type": "TERM", "insurer": "LIC", "policy_number": "POL-1",
            "premium_amount": "12000", "premium_frequency": "ANNUAL",
            "start_date": "2020-01-15", "sum_assured": "5000000",
            "maturity_date": "2050-01-15",
        },
    )
    assert status == 200
    assert body["policy"]["insurer"] == "LIC"
    assert body["policy"]["installments_due_so_far"] > 0

    status, body = _get(server, f"/api/members/{member_id}/insurance")
    assert status == 200
    assert len(body["policies"]) == 1
    assert body["policies"][0]["policy_number"] == "POL-1"


def test_create_insurance_policy_with_missing_field_is_a_client_error(server):
    _, member_id = _family_with_member(server)

    status, body = _post(
        server, f"/api/members/{member_id}/insurance", {"policy_type": "TERM"}
    )
    assert status == 400
    assert "error" in body


def test_family_dashboard_includes_insurance_but_not_in_totals_over_http(server):
    family_id, member_id = _family_with_member(server)
    _post(
        server, f"/api/members/{member_id}/insurance",
        {
            "policy_type": "TERM", "insurer": "LIC", "policy_number": "POL-1",
            "premium_amount": "999999", "premium_frequency": "ANNUAL",
            "start_date": "2020-01-01",
        },
    )

    status, body = _get(server, f"/api/families/{family_id}/dashboard")
    assert status == 200
    assert body["total_invested"] == "0"  # the policy's premium is not "invested"
    assert body["members"][0]["insurance_policies"][0]["policy_number"] == "POL-1"


def test_insurance_routes_require_auth(unauthenticated_server):
    status, _ = _get(unauthenticated_server, "/api/members/1/insurance")
    assert status == 401
    status, _ = _post(unauthenticated_server, "/api/members/1/insurance", {})
    assert status == 401


# ----------------------------------------------------------------------
# MF, end to end over HTTP
# ----------------------------------------------------------------------


def _create_mf_payload(**overrides):
    payload = {
        "amc": "Example AMC", "scheme_name": "Balanced Growth", "folio_number": "FOLIO-1",
        "initial_txn_date": "2025-01-15", "initial_txn_type": "SIP",
        "initial_units": "10.5", "initial_nav": "95.2381", "initial_amount": "1000.00",
    }
    payload.update(overrides)
    return payload


def test_create_mf_over_http_and_read_it_back(server):
    _, member_id = _family_with_member(server)

    status, body = _post(server, f"/api/members/{member_id}/mfs", _create_mf_payload())
    assert status == 200
    assert body["mf"]["units"] == "10.5"
    assert Decimal(body["mf"]["invested_amount"]) == Decimal("1000.00")
    assert body["mf"]["current_nav"] == "95.2381"

    status, body = _get(server, f"/api/members/{member_id}/mfs")
    assert status == 200
    assert len(body["mfs"]) == 1
    assert body["mfs"][0]["folio_number"] == "FOLIO-1"


def test_create_mf_with_missing_field_is_a_client_error(server):
    _, member_id = _family_with_member(server)

    status, body = _post(server, f"/api/members/{member_id}/mfs", {"amc": "Example AMC"})
    assert status == 400
    assert "error" in body


def test_add_mf_transaction_over_http(server):
    _, member_id = _family_with_member(server)
    _, created = _post(server, f"/api/members/{member_id}/mfs", _create_mf_payload())
    asset_id = created["mf"]["asset_id"]

    status, body = _post(
        server, f"/api/mfs/{asset_id}/transactions",
        {
            "txn_date": "2025-02-15", "txn_type": "SIP", "units": "9.8",
            "nav": "102.04", "amount": "1000.00", "description": "February SIP",
        },
    )
    assert status == 200
    assert body["mf"]["units"] == "20.3"
    assert len(body["mf"]["transactions"]) == 2


def test_add_mf_valuation_over_http(server):
    _, member_id = _family_with_member(server)
    _, created = _post(server, f"/api/members/{member_id}/mfs", _create_mf_payload())
    asset_id = created["mf"]["asset_id"]

    status, body = _post(
        server, f"/api/mfs/{asset_id}/valuations",
        {"valuation_date": "2026-06-01", "nav": "115.50"},
    )
    assert status == 200
    assert Decimal(body["mf"]["current_nav"]) == Decimal("115.50")
    assert Decimal(body["mf"]["current_value"]) == Decimal("1212.75")  # 10.5 * 115.50


def test_mf_transaction_for_unknown_asset_is_a_client_error(server):
    status, body = _post(
        server, "/api/mfs/999/transactions",
        {"txn_date": "2026-01-01", "txn_type": "SIP", "units": "1", "nav": "100", "amount": "100"},
    )
    assert status == 400
    assert "error" in body


def test_family_dashboard_reflects_a_newly_created_mf(server):
    family_id, member_id = _family_with_member(server)
    _post(server, f"/api/members/{member_id}/mfs", _create_mf_payload())

    status, body = _get(server, f"/api/families/{family_id}/dashboard")
    assert status == 200
    assert Decimal(body["total_invested"]) == Decimal("1000.00")
    assert body["by_category"]["MF"] == body["total_current_value"]
    assert body["members"][0]["mfs"][0]["folio_number"] == "FOLIO-1"


def test_mf_routes_require_auth(unauthenticated_server):
    status, _ = _get(unauthenticated_server, "/api/members/1/mfs")
    assert status == 401
    status, _ = _post(unauthenticated_server, "/api/members/1/mfs", {})
    assert status == 401
    status, _ = _post(unauthenticated_server, "/api/mfs/1/transactions", {})
    assert status == 401
    status, _ = _post(unauthenticated_server, "/api/mfs/1/valuations", {})
    assert status == 401


# ----------------------------------------------------------------------
# MF portfolio view and NAV sync over HTTP (AMFI fetch always injected)
# ----------------------------------------------------------------------

_AMFI_SAMPLE = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;"
    "Plan;Option;Net Asset Value;Date\n"
    "135762;INF846K01WO1;-;Axis Children's Fund;Direct Plan;Growth Option;"
    "120.0000;25-Sep-2026\n"
)


def _logged_in(srv):
    srv.opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    status, body = _post(srv, "/api/auth/set-password", {"password": _TEST_PASSWORD})
    assert status == 200, body
    return srv


@pytest.fixture
def sync_server(tmp_path):
    """A logged-in server with auto-sync on and the AMFI fetch replaced by a
    fixed sample. `srv.fetch_calls` counts how often it was asked for."""
    from api.amfi_sync import SyncStateStore

    calls = []

    def fake_fetch():
        calls.append(1)
        return _AMFI_SAMPLE

    srv, thread = _start_server(
        tmp_path, sync_state=SyncStateStore(tmp_path / "sync.json"), nav_fetcher=fake_fetch
    )
    srv.fetch_calls = calls
    try:
        yield _logged_in(srv)
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def test_family_mf_portfolio_family_wide_and_member_wise(server):
    family_id, raj = _family_with_member(server)
    _, created = _post(
        server, f"/api/families/{family_id}/members", {"name": "Asha", "relationship": "spouse"}
    )
    asha = created["member"]["member_id"]
    _post(server, f"/api/members/{raj}/mfs", _create_mf_payload(folio_number="F-RAJ"))
    _post(server, f"/api/members/{asha}/mfs", _create_mf_payload(folio_number="F-ASHA"))

    status, body = _get(server, f"/api/families/{family_id}/mfs")
    assert status == 200
    assert {(r["owner_name"], r["mf"]["folio_number"]) for r in body["rows"]} == {
        ("Raj", "F-RAJ"), ("Asha", "F-ASHA"),
    }
    assert Decimal(body["total_invested"]) == Decimal("2000")

    status, body = _get(server, f"/api/families/{family_id}/mfs?member_id={asha}")
    assert status == 200
    assert [r["mf"]["folio_number"] for r in body["rows"]] == ["F-ASHA"]
    assert Decimal(body["total_invested"]) == Decimal("1000")


def test_family_mf_portfolio_for_unknown_family_is_404(server):
    status, body = _get(server, "/api/families/999/mfs")
    assert status == 404


def test_manual_sync_updates_matching_holdings(sync_server):
    family_id, member_id = _family_with_member(sync_server)
    _post(sync_server, f"/api/members/{member_id}/mfs", _create_mf_payload(isin="INF846K01WO1"))

    status, body = _post(sync_server, f"/api/families/{family_id}/mfs/sync", {})
    assert status == 200
    assert body["sync"]["error"] is None
    assert len(body["sync"]["updated"]) == 1
    assert body["sync"]["nav_date"] == "2026-09-25"

    _, mfs = _get(sync_server, f"/api/members/{member_id}/mfs")
    assert Decimal(mfs["mfs"][0]["current_nav"]) == Decimal("120")
    assert mfs["mfs"][0]["current_nav_source"] == "AMFI"


def test_manual_sync_reports_a_fetch_failure_without_a_500(tmp_path):
    def failing():
        raise OSError("AMFI unreachable")

    srv, thread = _start_server(tmp_path, nav_fetcher=failing)
    try:
        _logged_in(srv)
        family_id, _ = _family_with_member(srv)
        status, body = _post(srv, f"/api/families/{family_id}/mfs/sync", {})
        assert status == 200
        assert "AMFI unreachable" in body["sync"]["error"]
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def test_opening_the_dashboard_auto_syncs_once_a_day(sync_server):
    family_id, member_id = _family_with_member(sync_server)
    _post(sync_server, f"/api/members/{member_id}/mfs", _create_mf_payload(isin="INF846K01WO1"))

    _get(sync_server, f"/api/families/{family_id}/dashboard")
    _get(sync_server, f"/api/families/{family_id}/mfs")
    _get(sync_server, f"/api/families/{family_id}/dashboard")

    assert len(sync_server.fetch_calls) == 1  # not once per page load
    _, mfs = _get(sync_server, f"/api/members/{member_id}/mfs")
    assert mfs["mfs"][0]["current_nav_source"] == "AMFI"

    status, state = _get(sync_server, "/api/mfs/sync-status")
    assert state["auto_sync_enabled"] is True
    assert state["last_nav_date"] == "2026-09-25"
    assert state["last_error"] is None


def test_a_failing_auto_sync_never_breaks_the_dashboard(tmp_path):
    from api.amfi_sync import SyncStateStore

    def failing():
        raise OSError("AMFI down")

    srv, thread = _start_server(
        tmp_path, sync_state=SyncStateStore(tmp_path / "sync.json"), nav_fetcher=failing
    )
    try:
        _logged_in(srv)
        family_id, member_id = _family_with_member(srv)
        _post(srv, f"/api/members/{member_id}/mfs", _create_mf_payload(isin="INF846K01WO1"))

        status, body = _get(srv, f"/api/families/{family_id}/dashboard")
        assert status == 200  # rendered with last-known NAVs
        _, state = _get(srv, "/api/mfs/sync-status")
        assert "AMFI down" in state["last_error"]
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def test_without_a_sync_state_there_is_no_auto_sync(server):
    status, state = _get(server, "/api/mfs/sync-status")
    assert status == 200
    assert state["auto_sync_enabled"] is False


def test_family_fd_and_insurance_portfolios_family_wide_and_member_wise(server):
    family_id, raj = _family_with_member(server)
    _, created = _post(
        server, f"/api/families/{family_id}/members", {"name": "Asha", "relationship": "spouse"}
    )
    asha = created["member"]["member_id"]
    for member, fd_number, principal in ((raj, "FD-RAJ", "100000"), (asha, "FD-ASHA", "50000")):
        status, _ = _post(server, f"/api/members/{member}/fds", {
            "bank_name": "HDFC", "fd_number": fd_number, "principal": principal,
            "interest_rate": "7", "issue_date": "2025-01-01", "maturity_date": "2030-01-01",
            "payout_type": "MATURITY",
        })
        assert status == 200
    status, _ = _post(server, f"/api/members/{asha}/insurance", {
        "policy_type": "TERM", "insurer": "LIC", "policy_number": "POL-1",
        "premium_amount": "1000", "premium_frequency": "MONTHLY",
        "start_date": "2024-01-01", "sum_assured": "5000000",
    })
    assert status == 200

    status, body = _get(server, f"/api/families/{family_id}/fds")
    assert status == 200
    assert {(r["owner_name"], r["fd"]["fd_number"]) for r in body["rows"]} == {
        ("Raj", "FD-RAJ"), ("Asha", "FD-ASHA"),
    }
    assert Decimal(body["total_principal"]) == Decimal("150000")
    status, body = _get(server, f"/api/families/{family_id}/fds?member_id={asha}")
    assert Decimal(body["total_principal"]) == Decimal("50000")

    status, body = _get(server, f"/api/families/{family_id}/insurance")
    assert status == 200
    assert Decimal(body["total_sum_assured"]) == Decimal("5000000")
    assert Decimal(body["total_annual_premium"]) == Decimal("12000")
    status, body = _get(server, f"/api/families/{family_id}/insurance?member_id={raj}")
    assert body["rows"] == [] and Decimal(body["total_annual_premium"]) == 0


def test_fd_and_insurance_portfolios_404_for_unknown_family_or_outside_member(server):
    family_id, _ = _family_with_member(server)
    assert _get(server, "/api/families/999/fds")[0] == 404
    assert _get(server, "/api/families/999/insurance")[0] == 404
    assert _get(server, f"/api/families/{family_id}/fds?member_id=999")[0] == 404


def test_sync_routes_require_auth(unauthenticated_server):
    assert _get(unauthenticated_server, "/api/families/1/fds")[0] == 401
    assert _get(unauthenticated_server, "/api/families/1/insurance")[0] == 401
    assert _get(unauthenticated_server, "/api/families/1/mfs")[0] == 401
    assert _post(unauthenticated_server, "/api/families/1/mfs/sync", {})[0] == 401
    assert _get(unauthenticated_server, "/api/mfs/sync-status")[0] == 401


# ----------------------------------------------------------------------
# Regression: every POST drains its body before answering
# ----------------------------------------------------------------------


def test_rejected_posts_with_large_bodies_never_reset_the_connection(unauthenticated_server):
    """A POST the server rejects without needing its body (here a 401) used
    to be answered with the body still unread, which on Windows made the
    connection reset (ConnectionAbortedError) instead of delivering the
    401. A large body sent many times makes that race near-certain if the
    server ever stops draining bodies again."""
    big = {"padding": "x" * 200_000}
    for _ in range(25):
        status, _ = _post(unauthenticated_server, "/api/families", big)
        assert status == 401


def test_malformed_json_body_is_a_400_not_a_server_error(unauthenticated_server):
    req = urllib.request.Request(
        _url(unauthenticated_server, "/api/auth/set-password"),
        data=b"{not valid json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status, body = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        status, body = exc.code, json.loads(exc.read())
    assert status == 400
    assert "error" in body


def test_allocation_and_insurance_values_over_http(server):
    family_id, raj = _family_with_member(server)
    status, _ = _post(server, f"/api/members/{raj}/fds", {
        "bank_name": "HDFC", "fd_number": "FD-1", "principal": "100000",
        "interest_rate": "7", "issue_date": "2025-01-01", "maturity_date": "2030-01-01",
        "payout_type": "MATURITY",
    })
    assert status == 200
    status, created = _post(server, f"/api/members/{raj}/insurance", {
        "policy_type": "ENDOWMENT", "insurer": "LIC", "policy_number": "POL-1",
        "premium_amount": "10000", "premium_frequency": "ANNUAL",
        "start_date": "2022-01-01", "accrued_bonus": "3000",
    })
    assert status == 200 and Decimal(created["policy"]["accrued_bonus"]) == Decimal("3000")
    policy_id = created["policy"]["policy_id"]

    status, body = _post(server, f"/api/insurance/{policy_id}/values",
                         {"current_surrender_value": "12000", "accrued_bonus": "3500"})
    assert status == 200
    assert Decimal(body["policy"]["accrued_bonus"]) == Decimal("3500")

    status, body = _get(server, f"/api/families/{family_id}/allocation")
    assert status == 200
    by_cat = {s["category"]: s for s in body["slices"]}
    assert set(by_cat) == {"FD", "INSURANCE"}
    assert Decimal(by_cat["INSURANCE"]["current_value"]) == Decimal("15500")
    assert Decimal(body["total_current_value"]) == sum(Decimal(s["current_value"]) for s in body["slices"])

    status, body = _get(server, f"/api/families/{family_id}/allocation?member_id={raj}")
    assert status == 200 and [m["name"] for m in body["members"]] == ["Raj"]


def test_insurance_values_for_unknown_policy_and_allocation_for_unknown_family(server):
    assert _post(server, "/api/insurance/999/values", {"accrued_bonus": "1"})[0] == 400
    assert _get(server, "/api/families/999/allocation")[0] == 404


def test_allocation_and_insurance_values_require_auth(unauthenticated_server):
    assert _get(unauthenticated_server, "/api/families/1/allocation")[0] == 401
    assert _post(unauthenticated_server, "/api/insurance/1/surrender-estimate", {})[0] == 401
    assert _post(unauthenticated_server, "/api/insurance/1/values", {})[0] == 401


def test_stocks_over_http(server):
    family_id, raj = _family_with_member(server)
    status, body = _post(server, f"/api/members/{raj}/stocks", {
        "symbol": "infy", "exchange": "NSE", "broker": "Zerodha",
        "initial_txn_date": "2025-01-10", "initial_quantity": "10", "initial_price": "1500",
    })
    assert status == 200, body
    asset_id = body["stock"]["asset_id"]

    status, body = _post(server, f"/api/stocks/{asset_id}/transactions", {
        "txn_type": "SELL", "txn_date": "2025-02-01", "quantity": "4", "price": "1600",
    })
    assert status == 200 and Decimal(body["stock"]["realised_gain"]) == Decimal("400")
    status, body = _post(server, f"/api/stocks/{asset_id}/prices",
                         {"price_date": "2026-09-01", "price": "2000"})
    assert status == 200 and Decimal(body["stock"]["current_value"]) == Decimal("12000")

    status, body = _post(server, f"/api/stocks/{asset_id}/transactions", {
        "txn_type": "SELL", "txn_date": "2025-03-01", "quantity": "100", "price": "1",
    })
    assert status == 400 and "cannot sell" in body["error"]

    status, body = _get(server, f"/api/families/{family_id}/stocks?member_id={raj}")
    assert status == 200 and [r["stock"]["symbol"] for r in body["rows"]] == ["INFY"]
    assert _get(server, "/api/families/999/stocks")[0] == 404


def test_pf_over_http(server):
    family_id, raj = _family_with_member(server)
    status, body = _post(server, f"/api/members/{raj}/pf", {
        "account_type": "EPF", "account_number": "UAN-1", "opened_date": "2018-07-01",
        "interest_rate": "8.25", "employee_contribution": "300000",
        "employer_contribution": "250000", "balance": "720000", "balance_date": "2026-03-31",
    })
    assert status == 200, body
    asset_id = body["account"]["asset_id"]
    status, body = _post(server, f"/api/pf/{asset_id}/update", {
        "balance": "800000", "balance_date": "2026-09-01",
        "employee_contribution": "330000", "employer_contribution": "275000",
    })
    assert status == 200 and Decimal(body["account"]["interest_earned"]) == Decimal("195000")
    status, body = _get(server, f"/api/families/{family_id}/pf")
    assert status == 200 and Decimal(body["total_balance"]) == Decimal("800000")
    assert _post(server, f"/api/members/{raj}/pf", {"account_type": "EPF"})[0] == 400
    assert _get(server, "/api/families/999/pf")[0] == 404


def test_gold_over_http(server):
    family_id, raj = _family_with_member(server)
    status, body = _post(server, f"/api/members/{raj}/gold", {
        "form": "PHYSICAL", "purity": "22K", "quantity_grams": "20",
        "purchase_rate_per_gram": "5000", "purchase_date": "2023-04-01",
    })
    assert status == 200, body
    status, body = _post(server, f"/api/families/{family_id}/gold/rate",
                         {"rate_24k_per_gram": "9600", "as_of": "2026-09-29"})
    assert status == 200 and Decimal(body["total_current_value"]) == Decimal("176000")
    status, body = _get(server, f"/api/families/{family_id}/gold?member_id={raj}")
    assert status == 200 and body["rows"][0]["gold"]["purity"] == "22K"
    assert _post(server, f"/api/members/{raj}/gold", {"form": "SGB", "purity": "22K",
                 "quantity_grams": "1", "purchase_rate_per_gram": "1",
                 "purchase_date": "2024-01-01"})[0] == 400


def test_nps_over_http(server):
    family_id, raj = _family_with_member(server)
    status, body = _post(server, f"/api/members/{raj}/nps", {
        "pran": "110012345678", "tier": "I", "opened_date": "2019-04-01",
        "employee_contribution": "400000", "employer_contribution": "200000",
        "equity_pct": "50", "corporate_debt_pct": "30", "gov_bond_pct": "20",
        "balance": "820000", "balance_date": "2026-03-31",
    })
    assert status == 200, body
    asset_id = body["account"]["asset_id"]
    status, body = _post(server, f"/api/nps/{asset_id}/update", {
        "balance": "900000", "balance_date": "2026-09-01", "employee_contribution": "420000",
        "employer_contribution": "210000",
    })
    assert status == 200 and Decimal(body["account"]["gain_loss"]) == Decimal("270000")
    status, body = _get(server, f"/api/families/{family_id}/nps")
    assert status == 200 and Decimal(body["tier1_balance"]) == Decimal("900000")
    assert _post(server, f"/api/members/{raj}/nps", {"pran": "x", "tier": "IX",
                 "opened_date": "2020-01-01", "employee_contribution": "1",
                 "balance": "1", "balance_date": "2020-01-01"})[0] == 400


def test_surrender_estimate_over_http_then_save(server):
    family_id, raj = _family_with_member(server)
    _, created = _post(server, f"/api/members/{raj}/insurance", {
        "policy_type": "ENDOWMENT", "insurer": "LIC", "policy_number": "E-1",
        "premium_amount": "50000", "premium_frequency": "ANNUAL", "start_date": "2016-01-01",
        "maturity_date": "2036-01-01", "sum_assured": "1000000", "accrued_bonus": "400000",
    })
    policy_id = created["policy"]["policy_id"]
    status, body = _post(server, f"/api/insurance/{policy_id}/surrender-estimate",
                         {"ssv_factor": "0.5"})
    assert status == 200, body
    estimate = body["estimate"]["estimate"]
    assert Decimal(estimate) > 0
    status, body = _post(server, f"/api/insurance/{policy_id}/values",
                         {"current_surrender_value": estimate, "accrued_bonus": "400000"})
    assert status == 200
    assert Decimal(body["policy"]["current_surrender_value"]) == Decimal(estimate)
    assert _post(server, "/api/insurance/999/surrender-estimate", {})[0] == 400


def test_fd_update_and_delete_over_http(server):
    family_id, raj = _family_with_member(server)
    _, body = _post(server, f"/api/members/{raj}/fds", {
        "bank_name": "ICIC", "fd_number": "1", "principal": "100000", "interest_rate": "7",
        "issue_date": "2025-01-01", "maturity_date": "2027-01-01", "payout_type": "QUARTERLY",
    })
    asset_id = body["fd"]["asset_id"]
    status, body = _post(server, f"/api/fds/{asset_id}/update", {
        "owner_id": raj, "bank_name": "ICICI Bank", "fd_number": "1", "principal": "200000",
        "interest_rate": "7", "issue_date": "2025-01-01", "maturity_date": "2027-01-01",
        "payout_type": "QUARTERLY",
    })
    assert status == 200, body
    assert body["fd"]["bank_name"] == "ICICI Bank" and Decimal(body["fd"]["principal"]) == 200000
    assert Decimal(body["fd"]["payout_interest"]) == Decimal("3500")
    assert _post(server, f"/api/fds/{asset_id}/update", {"owner_id": raj})[0] == 400
    status, _ = _post(server, f"/api/fds/{asset_id}/delete", {})
    assert status == 200
    _, body = _get(server, f"/api/families/{family_id}/fds")
    assert body["rows"] == []
    assert _post(server, f"/api/fds/{asset_id}/delete", {})[0] == 400


def test_monthly_cash_flow_over_http(server):
    family_id, raj = _family_with_member(server)
    status, body = _post(server, f"/api/members/{raj}/cashflow-items",
                         {"flow_type": "INCOME", "name": "Salary", "amount": "100000"})
    assert status == 200, body
    item_id = body["item"]["item_id"]
    _post(server, f"/api/members/{raj}/cashflow-items",
          {"flow_type": "EXPENSE", "name": "Grocery", "amount": "12000", "frequency": "MONTHLY"})
    _, pol = _post(server, f"/api/members/{raj}/insurance", {
        "policy_type": "TERM", "insurer": "LIC", "policy_number": "T-1",
        "premium_amount": "12000", "premium_frequency": "ANNUAL", "start_date": "2020-10-05",
    })
    policy_id = pol["policy"]["policy_id"]

    status, cf = _get(server, f"/api/families/{family_id}/cashflow?month=2026-10&member_id={raj}")
    assert status == 200
    assert Decimal(cf["surplus"]) == Decimal("88000")
    assert cf["expenses"][-1]["source"] == "PREMIUM" and cf["expenses"][-1]["paid"] is False

    status, _ = _post(server, f"/api/insurance/{policy_id}/premium-paid",
                      {"due_date": "2026-10-05", "paid": True})
    assert status == 200
    _, cf = _get(server, f"/api/families/{family_id}/cashflow?month=2026-10")
    assert Decimal(cf["surplus"]) == Decimal("76000")

    status, body = _post(server, f"/api/cashflow-items/{item_id}/update",
                         {"owner_id": raj, "flow_type": "INCOME", "name": "Salary", "amount": "110000"})
    assert status == 200 and Decimal(body["item"]["amount"]) == 110000
    assert _post(server, f"/api/cashflow-items/{item_id}/delete", {})[0] == 200
    assert _get(server, f"/api/families/{family_id}/cashflow?month=oct")[0] == 404
    assert _post(server, f"/api/members/{raj}/cashflow-items", {"flow_type": "INCOME"})[0] == 400


def test_mf_sip_over_http(server):
    _, raj = _family_with_member(server)
    _, body = _post(server, f"/api/members/{raj}/mfs", _create_mf_payload(sip_amount="5000", sip_date="7"))
    assert body["mf"]["sip_enabled"] is True and Decimal(body["mf"]["sip_amount"]) == 5000
    asset_id = body["mf"]["asset_id"]
    status, body = _post(server, f"/api/mfs/{asset_id}/sip", {})
    assert status == 200 and body["mf"]["sip_enabled"] is False


def test_bank_accounts_and_row_accounts_over_http(server):
    family_id, raj = _family_with_member(server)
    status, body = _post(server, f"/api/members/{raj}/bank-accounts", {
        "bank_name": "HDFC Bank", "account_last4": "4321", "balance": "250000",
        "balance_as_of": "2026-09-01",
    })
    assert status == 200, body
    account_id = body["account"]["account_id"]
    assert body["account"]["is_default"] is True

    status, body = _post(server, f"/api/members/{raj}/cashflow-items", {
        "flow_type": "EXPENSE", "category": "Grocery", "amount": "12000",
        "payment_mode": "UPI", "account_id": str(account_id),
    })
    assert status == 200 and body["item"]["name"] == "Grocery"
    item_id = body["item"]["item_id"]

    status, cf = _get(server, f"/api/families/{family_id}/cashflow?month=2026-09")
    assert status == 200
    assert cf["accounts"][0]["bank_name"] == "HDFC Bank"
    assert cf["expenses"][0]["account_label"] == "HDFC Bank ••4321"
    assert Decimal(cf["accounts"][0]["monthly_net"]) == Decimal("-12000")

    status, _ = _post(server, f"/api/cashflow-items/{item_id}/update", {
        "owner_id": raj, "flow_type": "EXPENSE", "category": "Grocery", "amount": "12000",
        "payment_mode": "CASH",
    })
    assert status == 200
    status, body = _post(server, f"/api/bank-accounts/{account_id}/balance",
                         {"balance": "300000", "as_of": "2026-09-29"})
    assert status == 200 and Decimal(body["account"]["opening_balance"]) == 300000
    status, _ = _post(server, f"/api/bank-accounts/{account_id}/update",
                      {"bank_name": "HDFC Bank Ltd", "account_last4": "4321"})
    assert status == 200
    _, accts = _get(server, f"/api/families/{family_id}/bank-accounts")
    assert [a["bank_name"] for a in accts["accounts"]] == ["HDFC Bank Ltd"]
    assert _post(server, f"/api/bank-accounts/{account_id}/close", {})[0] == 200
    _, accts = _get(server, f"/api/families/{family_id}/bank-accounts")
    assert accts["accounts"] == []
    assert _post(server, "/api/bank-accounts/999/balance", {"balance": "1", "as_of": "2026-01-01"})[0] == 400
    assert _post(server, f"/api/members/{raj}/bank-accounts", {"bank_name": ""})[0] == 400


def test_mf_split_over_http(server):
    family_id, raj = _family_with_member(server)
    _, body = _post(server, f"/api/members/{raj}/mfs",
                    _create_mf_payload(initial_txn_type="LUMPSUM", sip_amount="2000"))
    assert body["mf"]["investment_type"] == "SIP + LUMPSUM"
    _, p = _get(server, f"/api/families/{family_id}/mfs")
    assert Decimal(p["total_lumpsum_invested"]) == Decimal("1000")


# ----------------------------------------------------------------------
# Stock price sync (NSE / BSE bhavcopy)
# ----------------------------------------------------------------------

_BHAV_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
_NSE_SAMPLE = _BHAV_HEADER + "\n" + (
    "2026-09-28,2026-09-28,CM,NSE,STK,1594,INE009A01021,INFY,EQ,,,,,INFOSYS LIMITED,1002.00,"
    "1009.50,986.60,1003.20,1003.20,1000.20,,1003.20,,,8567672,8557694675.90,166272,F1,1,,,,,"
)


_INDEX_HEADER = "Company Name,Industry,Symbol,Series,ISIN Code\n"
_INFY_LIST_ROW = "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n"
_NSE_INDEX_SAMPLE = {
    "ind_nifty500list": _INDEX_HEADER + _INFY_LIST_ROW,
    "ind_nifty100list": _INDEX_HEADER + _INFY_LIST_ROW,
    "ind_niftymidcap150list": _INDEX_HEADER,
    "ind_niftysmallcap250list": _INDEX_HEADER,
}


@pytest.fixture
def stock_sync_server(tmp_path):
    from api.amfi_sync import SyncStateStore

    calls = []

    def fake_fetch(exchange):
        calls.append(exchange)
        return _NSE_SAMPLE

    srv, thread = _start_server(
        tmp_path, stock_sync_state=SyncStateStore(tmp_path / "stock_sync.json"),
        stock_fetcher=fake_fetch, index_fetcher=lambda: _NSE_INDEX_SAMPLE,
    )
    srv.fetch_calls = calls
    try:
        yield _logged_in(srv)
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def _family_with_infy(srv):
    family_id, raj = _family_with_member(srv)
    status, _ = _post(srv, f"/api/members/{raj}/stocks", {
        "symbol": "INFY", "exchange": "NSE", "broker": "Zerodha",
        "initial_txn_date": "2025-01-10", "initial_quantity": "10", "initial_price": "1500",
    })
    assert status == 200
    return family_id


def test_manual_stock_sync_updates_prices(stock_sync_server):
    family_id = _family_with_infy(stock_sync_server)
    status, body = _post(stock_sync_server, f"/api/families/{family_id}/stocks/sync", {})
    assert status == 200 and body["sync"]["error"] is None
    assert body["sync"]["nav_date"] == "2026-09-28" and len(body["sync"]["updated"]) == 1
    _, p = _get(stock_sync_server, f"/api/families/{family_id}/stocks")
    assert Decimal(p["rows"][0]["stock"]["current_price"]) == Decimal("1003.20")
    _, status_body = _get(stock_sync_server, "/api/stocks/sync-status")
    assert status_body["auto_sync_enabled"] is True
    assert status_body["last_price_date"] == "2026-09-28"


def test_stock_auto_sync_runs_once_a_day_on_page_loads(stock_sync_server):
    family_id = _family_with_infy(stock_sync_server)
    for _ in range(3):
        assert _get(stock_sync_server, f"/api/families/{family_id}/stocks")[0] == 200
    assert _get(stock_sync_server, f"/api/families/{family_id}/allocation")[0] == 200
    assert stock_sync_server.fetch_calls == ["NSE"]


def test_stock_sync_is_off_unless_configured(server):
    _, body = _get(server, "/api/stocks/sync-status")
    assert body["auto_sync_enabled"] is False


def test_stock_sync_routes_need_auth(unauthenticated_server):
    assert _post(unauthenticated_server, "/api/families/1/stocks/sync", {})[0] == 401
    assert _get(unauthenticated_server, "/api/stocks/sync-status")[0] == 401


def test_stock_groups_snapshot_risk_and_classification_over_http(stock_sync_server):
    srv = stock_sync_server
    family_id = _family_with_infy(srv)
    _, members = _get(srv, f"/api/families/{family_id}/members")
    raj = members["members"][0]["member_id"]
    # same stock, same broker again -> one holding
    status, _ = _post(srv, f"/api/members/{raj}/stocks", {
        "symbol": "infy", "exchange": "NSE", "broker": "Zerodha",
        "initial_txn_date": "2025-06-10", "initial_quantity": "10", "initial_price": "1300",
    })
    assert status == 200
    _post(srv, f"/api/families/{family_id}/stocks/sync", {})  # also loads sector data

    status, p = _get(srv, f"/api/families/{family_id}/stocks")
    assert status == 200
    [group] = p["groups"]
    assert group["symbol"] == "INFY" and Decimal(group["quantity"]) == 20
    assert Decimal(group["avg_buy_price"]) == Decimal("1400")
    assert group["sector"] == "Information Technology" and group["cap_category"] == "LARGE"
    assert p["analysis"]["risk_checks"][0]["fits"] is None

    status, body = _post(srv, f"/api/members/{raj}/risk-tolerance", {"risk_tolerance": "AGGRESSIVE"})
    assert status == 200 and body["member"]["risk_tolerance"] == "AGGRESSIVE"
    _, p = _get(srv, f"/api/families/{family_id}/stocks")
    assert p["analysis"]["risk_checks"][0]["risk_tolerance"] == "AGGRESSIVE"

    status, body = _post(srv, "/api/stock-info",
                         {"symbol": "INFY", "sector": "Software", "cap_category": "LARGE"})
    assert status == 200 and body["info"]["source"] == "MANUAL"
    assert _post(srv, "/api/stock-info", {"symbol": "INFY", "cap_category": "HUGE"})[0] == 400
    assert _post(srv, f"/api/members/{raj}/risk-tolerance", {"risk_tolerance": "YOLO"})[0] == 400


def test_insurance_update_over_http(server):
    family_id, raj = _family_with_member(server)
    _, created = _post(server, f"/api/members/{raj}/insurance", {
        "policy_type": "TERM", "insurer": "LIC", "policy_number": "P-1",
        "premium_amount": "1200", "premium_frequency": "ANNUAL", "start_date": "2020-01-01",
    })
    policy_id = created["policy"]["policy_id"]
    status, body = _post(server, f"/api/insurance/{policy_id}/update", {
        "owner_id": raj, "policy_type": "HEALTH", "insurer": "Star Health", "policy_number": "H-1",
        "premium_amount": "1800", "premium_frequency": "MONTHLY", "start_date": "2025-01-10",
        "status": "ACTIVE",
    })
    assert status == 200, body
    assert body["policy"]["insurer"] == "Star Health" and body["policy"]["premium_frequency"] == "MONTHLY"
    assert _post(server, f"/api/insurance/{policy_id}/update", {"owner_id": raj})[0] == 400
    assert _post(server, "/api/insurance/999/update", {
        "owner_id": raj, "policy_type": "TERM", "insurer": "X", "policy_number": "1",
        "premium_amount": "1", "premium_frequency": "ANNUAL", "start_date": "2020-01-01",
    })[0] == 400


def test_background_sync_never_holds_up_a_page(tmp_path):
    """The real app syncs prices on a background thread: a slow AMFI makes
    no page wait, and the sync still happens (once)."""
    import time
    from api.amfi_sync import SyncStateStore

    calls = []

    def slow_fetch():
        calls.append(1)
        time.sleep(2)
        return _AMFI_SAMPLE

    srv, thread = _start_server(
        tmp_path, sync_state=SyncStateStore(tmp_path / "sync.json"), nav_fetcher=slow_fetch,
        background_sync=True,
    )
    try:
        srv = _logged_in(srv)
        family_id, _ = _family_with_member(srv)
        started = time.time()
        status, _ = _get(srv, f"/api/families/{family_id}/dashboard")
        assert status == 200 and time.time() - started < 1.5  # did not wait the 2 s
        _get(srv, f"/api/families/{family_id}/mfs")  # a second page load starts no second sync
        deadline = time.time() + 10
        while not srv.sync_state.last_attempt_date() and time.time() < deadline:
            time.sleep(0.1)
        assert srv.sync_state.last_attempt_date() is not None and len(calls) == 1
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()
