"""
A small local HTTP server: a JSON API in front of `FinancialEngine`, plus the
static dashboard page.

Deliberately stdlib-only (`http.server`), single-threaded (fine for one
family, on your own machine, and it sidesteps any SQLite concurrent-access
question), and single-file. This is the "very simple API/read interface"
step of the FD vertical slice — see the product assessment for what comes
after it.

This module imports only `financial_engine`, `.auth`, and the standard
library. It never imports `repository` or `sqlite3` — every read and write
goes through one `FinancialEngine` instance, which is the whole point of
this being the first package above it.

Every route under `/api/` except `/api/auth/*` requires a valid session
(see `.auth`) — a missing or invalid session cookie gets a 401. The static
page itself (`/`) is always served; it is just HTML/JS, and its own script
discovers whether it needs to show a login screen by calling the API.

Routes:
    GET  /                                    the dashboard page
    GET  /api/auth/status                      {password_set, authenticated}
    POST /api/auth/set-password                bootstrap the one password
    POST /api/auth/login                       start a session
    POST /api/auth/logout                      end the current session
    GET  /api/families                         list families
    POST /api/families                         create a family
    GET  /api/families/<id>/members            list a family's members
    POST /api/families/<id>/members            add a member
    POST /api/members/<id>/profile              date of birth, life stage, heir, retirement age (+ name, relationship)
    POST /api/members/<id>/succession           tick a hand-over checklist item ({item, done})
    GET  /api/families/<id>/dashboard           family totals + per-member FDs
    GET  /api/members/<id>/fds                  a member's FDs
    POST /api/members/<id>/fds                  open a new FD
    POST /api/fds/<id>/update                   correct an FD (any field, incl. owner)
    POST /api/fds/<id>/delete                   remove an FD entered by mistake
    GET  /api/members/<id>/insurance             a member's insurance policies
    POST /api/members/<id>/insurance             add a policy
    GET  /api/members/<id>/mfs                    a member's MF holdings
    POST /api/members/<id>/mfs                    open a new MF holding (+ its first transaction)
    POST /api/mfs/<id>/transactions                 add a further SIP/lumpsum purchase
    POST /api/mfs/<id>/valuations                    record a current-NAV observation
    GET  /api/families/<id>/mfs[?member_id=<id>]      MF portfolio: whole family, or one member
    POST /api/families/<id>/mfs/sync                  sync NAVs from AMFI now (manual button)
    GET  /api/mfs/sync-status                          when NAVs were last synced, and any error
    GET  /api/families/<id>/fds[?member_id=<id>]       FD portfolio: whole family, or one member
    GET  /api/families/<id>/insurance[?member_id=<id>] insurance portfolio: whole family, or one member
    GET  /api/families/<id>/allocation[?member_id=<id>] invested/current value by asset class (Overview pies)
    POST /api/insurance/<id>/values                    set a policy's surrender value and accrued bonus
    POST /api/insurance/<id>/update                    correct a policy's terms, owner or status
    POST /api/insurance/<id>/surrender-estimate        estimate the surrender value (nothing saved)
    GET  /api/members/<id>/stocks                      a member's stock holdings
    POST /api/members/<id>/stocks                      open a stock holding (+ its first BUY)
    POST /api/stocks/<id>/transactions                 add a BUY or SELL
    POST /api/stocks/<id>/prices                       record a share price
    POST /api/stocks/<id>/update|delete                correct a holding (owner, broker, symbol...) / remove it
    POST /api/stock-transactions/<id>/update|delete    correct / remove one buy or sell
    POST /api/families/<id>/stocks/sync                sync closing prices from NSE/BSE now
    GET  /api/stocks/sync-status                       when stock prices were last synced
    POST /api/members/<id>/risk-tolerance              pick CONSERVATIVE / MODERATE / AGGRESSIVE
    POST /api/stock-info                               set a symbol's sector / market cap yourself
    GET  /api/families/<id>/stocks[?member_id=<id>]    stock portfolio: whole family, or one member
    GET  /api/members/<id>/pf                          a member's EPF/PPF accounts
    POST /api/members/<id>/pf                          add an EPF/PPF account (with its passbook figures)
    POST /api/pf/<id>/update                           record a newer passbook balance + contributions
    POST /api/pf/<id>/edit|delete                      correct any detail of an account / remove it
    GET  /api/families/<id>/pf[?member_id=<id>]        PF portfolio: whole family, or one member
    GET  /api/members/<id>/gold                        a member's gold holdings
    POST /api/members/<id>/gold                        record a gold purchase
    POST /api/families/<id>/gold/rate                  set today's 24K rate; revalues all the family's gold
    GET  /api/families/<id>/gold[?member_id=<id>]      gold portfolio: whole family, or one member
    GET  /api/members/<id>/nps                         a member's NPS accounts
    POST /api/members/<id>/nps                         add an NPS account (with its statement figures)
    POST /api/nps/<id>/update                          record a newer NPS statement
    GET  /api/families/<id>/nps[?member_id=<id>]       NPS portfolio: whole family, or one member
    GET  /api/families/<id>/cashflow?month=YYYY-MM[&member_id=<id>]  one month's income/expenses/investments
    POST /api/members/<id>/cashflow-items              add an income / expense / investment row
    POST /api/cashflow-items/<id>/update               change a row (incl. its owner)
    POST /api/cashflow-items/<id>/delete               remove a row
    POST /api/insurance/<id>/premium-paid              tick/untick a premium as paid ({due_date, paid})
    POST /api/mfs/<id>/sip                             set or stop a fund's monthly SIP amount
    POST /api/mfs/<id>/broker                          set the broker / platform a fund is held through ({broker})
    POST /api/mfs/<id>/update|delete|figures           correct owner / name / folio / broker; remove; set today's units + invested
    POST /api/mf-transactions/<id>/update|delete       correct / remove one purchase
    POST /api/mfs/<id>/purchase-details                date a CAS-imported fund's opening balance (lumpsum date, or SIP + current XIRR)
    GET  /api/families/<id>/bank-accounts              every member's active bank accounts
    POST /api/members/<id>/bank-accounts               add an account with its balance and date
    POST /api/bank-accounts/<id>/update|balance|close  rename / make default, re-enter balance, close
    GET  /api/families/<id>/loans[?member_id=<id>]     loans with their schedules: whole family, or one member
    GET  /api/families/<id>/health[?member_id=<id>]    financial health check-up, action plan, projection
    GET  /api/families/<id>/plan[?member_id=<id>]      the personal plan: area scores, goals, smart moves (+ the health check-up)
    POST /api/families/<id>/goals                      add a goal ({name, goal_type, target_amount, target_date, ...})
    POST /api/families/<id>/goals/<gid>/update|delete  change / remove a goal
    POST /api/assets/<id>/purpose                      what a holding is for ({goal_id, pct} or {purpose}, or neither)
    POST /api/families/<id>/planning-preferences       tax slab, emergency months, minimum FD, no direct stocks
    POST /api/families/<id>/moves/dismiss              hide / bring back a suggestion ({key, dismissed})
    POST /api/families/<id>/funding-plan/apply         assign the holdings the funding plan uses to their goals
    POST /api/members/<id>/goal-money                  may this person's money fund others' goals ({goal_money: YES / IF_NEEDED / NO})
    POST /api/members/<id>/loans                       add a loan (home, car, personal, ...)
    POST /api/loans/<id>/update                        correct a loan's terms, owner or status
    POST /api/loans/<id>/delete                        remove a loan and its events
    POST /api/loans/<id>/disbursements                 add a disbursement (a future date = expected)
    POST /api/loans/<id>/prepayments                   add a prepayment (a future date = planned)
    POST /api/loans/<id>/rate-changes                  the bank changed the rate from a date
    POST /api/loans/<id>/what-if                       try a prepayment (nothing saved)
    POST /api/loan-disbursements|loan-prepayments|loan-rate-changes/<id>/delete  remove one event
    POST /api/members/<id>/cas/preview                 read a CAS PDF ({pdf_base64}); show what importing would do
    POST /api/members/<id>/cas/apply                   import the ticked items ({pdf_base64, selected, policy_start_dates})
    POST /api/families/<id>/ask                        ask the assistant ({question, conversation_id?}) — see `assistant`
    GET  /api/settings                                 the settings, secrets only as "set" / "not set"
    POST /api/settings                                 save settings (API key, Gmail address / app password, recipients)
    POST /api/settings/test-email                      send a test email now
    GET  /api/families/<id>/reminders                  upcoming dues, what was emailed, the last error
    POST /api/families/<id>/reminders/check            run the reminder check now

The family dashboard and the family MF portfolio GETs first run the
automatic daily NAV sync (see `amfi_sync.sync_if_stale`) when the server was
given a `SyncStateStore`; a failed sync never stops the page from loading.

Error handling is intentionally simple: a lookup of something that does not
exist is reported as 404, anything else the Engine/repository rejects
(a validation rule, a schema constraint) is reported as 400 with the
message, and no exception detail is hidden — this runs on your own machine
for your own data, not as a public service.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import threading
import re
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from financial_engine import FinancialEngine

from .amfi_sync import (
    Fetcher,
    SyncStateStore,
    fetch_amfi_nav_file,
    parse_amfi_scheme_names,
    sync_family_mf_navs,
    sync_if_stale,
)
from .stock_price_sync import (
    IndexFetcher,
    LatestFetcher,
    fetch_latest_bhavcopy,
    fetch_nse_index_lists,
    parse_bhavcopy_isins,
    refresh_stock_classification_if_stale,
    sync_family_stock_prices,
    sync_stock_prices_if_stale,
)
from .cas_parser import CasParseError, parse_cas_pdf
from .assistant import Assistant
from .notifications import ReminderLog, SettingsStore, run_reminder_check, send_test_email
from financial_engine.reminders import reminder_window
from .auth import (
    AuthStore,
    SessionManager,
    clear_session_cookie_header,
    read_session_token,
    session_cookie_header,
)

_DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"

_FAMILIES_RE = re.compile(r"^/api/families/?$")
_FAMILY_MEMBERS_RE = re.compile(r"^/api/families/(\d+)/members/?$")
_FAMILY_DASHBOARD_RE = re.compile(r"^/api/families/(\d+)/dashboard/?$")
_MEMBER_FDS_RE = re.compile(r"^/api/members/(\d+)/fds/?$")
_FD_UPDATE_RE = re.compile(r"^/api/fds/(\d+)/update/?$")
_FD_DELETE_RE = re.compile(r"^/api/fds/(\d+)/delete/?$")
_MEMBER_INSURANCE_RE = re.compile(r"^/api/members/(\d+)/insurance/?$")
_MEMBER_MFS_RE = re.compile(r"^/api/members/(\d+)/mfs/?$")
_MF_TRANSACTIONS_RE = re.compile(r"^/api/mfs/(\d+)/transactions/?$")
_MF_VALUATIONS_RE = re.compile(r"^/api/mfs/(\d+)/valuations/?$")
_FAMILY_MFS_RE = re.compile(r"^/api/families/(\d+)/mfs/?$")
_FAMILY_MFS_SYNC_RE = re.compile(r"^/api/families/(\d+)/mfs/sync/?$")
_FAMILY_FDS_RE = re.compile(r"^/api/families/(\d+)/fds/?$")
_FAMILY_INSURANCE_RE = re.compile(r"^/api/families/(\d+)/insurance/?$")
_FAMILY_ALLOCATION_RE = re.compile(r"^/api/families/(\d+)/allocation/?$")
_INSURANCE_VALUES_RE = re.compile(r"^/api/insurance/(\d+)/values/?$")
_INSURANCE_UPDATE_RE = re.compile(r"^/api/insurance/(\d+)/update/?$")
_INSURANCE_SURRENDER_RE = re.compile(r"^/api/insurance/(\d+)/surrender-estimate/?$")
_MEMBER_STOCKS_RE = re.compile(r"^/api/members/(\d+)/stocks/?$")
_STOCK_TRANSACTIONS_RE = re.compile(r"^/api/stocks/(\d+)/transactions/?$")
_STOCK_PRICES_RE = re.compile(r"^/api/stocks/(\d+)/prices/?$")
_STOCK_ACTION_RE = re.compile(r"^/api/stocks/(\d+)/(update|delete)/?$")
_STOCK_TXN_ACTION_RE = re.compile(r"^/api/stock-transactions/(\d+)/(update|delete)/?$")
_FAMILY_STOCKS_RE = re.compile(r"^/api/families/(\d+)/stocks/?$")
_MEMBER_PF_RE = re.compile(r"^/api/members/(\d+)/pf/?$")
_PF_EDIT_RE = re.compile(r"^/api/pf/(\d+)/(edit|delete)/?$")
_PF_UPDATE_RE = re.compile(r"^/api/pf/(\d+)/update/?$")
_FAMILY_PF_RE = re.compile(r"^/api/families/(\d+)/pf/?$")
_MEMBER_GOLD_RE = re.compile(r"^/api/members/(\d+)/gold/?$")
_FAMILY_GOLD_RE = re.compile(r"^/api/families/(\d+)/gold/?$")
_FAMILY_GOLD_RATE_RE = re.compile(r"^/api/families/(\d+)/gold/rate/?$")
_MEMBER_NPS_RE = re.compile(r"^/api/members/(\d+)/nps/?$")
_NPS_UPDATE_RE = re.compile(r"^/api/nps/(\d+)/update/?$")
_FAMILY_NPS_RE = re.compile(r"^/api/families/(\d+)/nps/?$")
_FAMILY_CASHFLOW_RE = re.compile(r"^/api/families/(\d+)/cashflow/?$")
_MEMBER_CASHFLOW_ITEMS_RE = re.compile(r"^/api/members/(\d+)/cashflow-items/?$")
_CASHFLOW_ITEM_UPDATE_RE = re.compile(r"^/api/cashflow-items/(\d+)/update/?$")
_CASHFLOW_ITEM_DELETE_RE = re.compile(r"^/api/cashflow-items/(\d+)/delete/?$")
_PREMIUM_PAID_RE = re.compile(r"^/api/insurance/(\d+)/premium-paid/?$")
_MF_SIP_RE = re.compile(r"^/api/mfs/(\d+)/sip/?$")
_MF_BROKER_RE = re.compile(r"^/api/mfs/(\d+)/broker/?$")
_MF_ACTION_RE = re.compile(r"^/api/mfs/(\d+)/(update|delete|figures)/?$")
_MF_TXN_ACTION_RE = re.compile(r"^/api/mf-transactions/(\d+)/(update|delete)/?$")
_MF_PURCHASE_DETAILS_RE = re.compile(r"^/api/mfs/(\d+)/purchase-details/?$")
_FAMILY_BANK_ACCOUNTS_RE = re.compile(r"^/api/families/(\d+)/bank-accounts/?$")
_MEMBER_BANK_ACCOUNTS_RE = re.compile(r"^/api/members/(\d+)/bank-accounts/?$")
_BANK_ACCOUNT_ACTION_RE = re.compile(r"^/api/bank-accounts/(\d+)/(update|balance|close)/?$")
_FAMILY_HEALTH_RE = re.compile(r"^/api/families/(\d+)/health/?$")
_FAMILY_PLAN_RE = re.compile(r"^/api/families/(\d+)/plan/?$")
_FAMILY_GOALS_RE = re.compile(r"^/api/families/(\d+)/goals(?:/(\d+)/(update|delete))?/?$")
_ASSET_PURPOSE_RE = re.compile(r"^/api/assets/(\d+)/purpose/?$")
_FAMILY_PREFS_RE = re.compile(r"^/api/families/(\d+)/planning-preferences/?$")
_FAMILY_DISMISS_RE = re.compile(r"^/api/families/(\d+)/moves/dismiss/?$")
_FAMILY_FUNDING_APPLY_RE = re.compile(r"^/api/families/(\d+)/funding-plan/apply/?$")
_MEMBER_GOAL_MONEY_RE = re.compile(r"^/api/members/(\d+)/goal-money/?$")
_FAMILY_LOANS_RE = re.compile(r"^/api/families/(\d+)/loans/?$")
_MEMBER_LOANS_RE = re.compile(r"^/api/members/(\d+)/loans/?$")
_LOAN_ACTION_RE = re.compile(
    r"^/api/loans/(\d+)/(update|delete|disbursements|prepayments|rate-changes|what-if)/?$"
)
_LOAN_EVENT_DELETE_RE = re.compile(
    r"^/api/(loan-disbursements|loan-prepayments|loan-rate-changes)/(\d+)/delete/?$"
)
_MEMBER_CAS_RE = re.compile(r"^/api/members/(\d+)/cas/(preview|apply)/?$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_MF_SYNC_STATUS_PATH = "/api/mfs/sync-status"
_STOCK_SYNC_STATUS_PATH = "/api/stocks/sync-status"
_FAMILY_STOCKS_SYNC_RE = re.compile(r"^/api/families/(\d+)/stocks/sync/?$")
_MEMBER_PROFILE_RE = re.compile(r"^/api/members/(\d+)/profile/?$")
_MEMBER_SUCCESSION_RE = re.compile(r"^/api/members/(\d+)/succession/?$")
_MEMBER_RISK_RE = re.compile(r"^/api/members/(\d+)/risk-tolerance/?$")
_STOCK_INFO_PATH = "/api/stock-info"
_FAMILY_ASK_RE = re.compile(r"^/api/families/(\d+)/ask/?$")
_FAMILY_REMINDERS_RE = re.compile(r"^/api/families/(\d+)/reminders(/check)?/?$")
_SETTINGS_PATH = "/api/settings"
_TEST_EMAIL_PATH = "/api/settings/test-email"


class _JSONEncoder(json.JSONEncoder):
    """Handles the shapes the Engine actually returns: a dataclass instance
    (wherever it appears — at the top level, or nested inside a plain dict
    like `{"family": some_family}`, since `json.dumps` calls `default()` for
    each value it does not natively know how to encode), a `Decimal` as a
    string (never a float, to keep exact precision all the way to the
    browser), and a `date` in ISO-8601.
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, date):
            return obj.isoformat()
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        return super().default(obj)


class DashboardServer(HTTPServer):
    """An `HTTPServer` that carries the one `FinancialEngine`, the auth
    state, and the NAV-sync configuration every request is served from.

    `sync_state=None` disables the automatic daily sync (the manual sync
    route still works) — the default, so a server built in a test never
    reaches out to AMFI unless the test asks it to. `nav_fetcher` is
    injectable for the same reason.
    """

    def __init__(
        self,
        server_address: Tuple[str, int],
        engine: FinancialEngine,
        auth_store: AuthStore,
        *,
        sync_state: Optional[SyncStateStore] = None,
        nav_fetcher: Fetcher = fetch_amfi_nav_file,
        stock_sync_state: Optional[SyncStateStore] = None,
        stock_fetcher: LatestFetcher = fetch_latest_bhavcopy,
        index_fetcher: IndexFetcher = fetch_nse_index_lists,
        background_sync: bool = False,
        settings: Optional[SettingsStore] = None,
        reminder_log: Optional[ReminderLog] = None,
    ) -> None:
        super().__init__(server_address, DashboardRequestHandler)
        self.engine = engine
        self.auth_store = auth_store
        self.sessions = SessionManager()
        self.sync_state = sync_state
        self.nav_fetcher = nav_fetcher
        # Stock prices: same pattern — None disables the automatic daily sync.
        self.stock_sync_state = stock_sync_state
        self.stock_fetcher = stock_fetcher
        self.index_fetcher = index_fetcher
        # ISIN -> scheme name / NSE symbol, fetched at most once a day for
        # naming what a CAS lists only by ISIN: {"names": (day, dict), ...}
        self.isin_lookups: Dict[str, Tuple[date, Dict[str, str]]] = {}
        # The daily price sync on a page load: inline (tests), or — for the
        # real app — on a background thread, so a slow AMFI / NSE / BSE never
        # holds up a page: it opens at once with the last prices known.
        self.background_sync = background_sync
        self._sync_lock = threading.Lock()
        self._sync_thread: Optional[threading.Thread] = None
        # Settings (API key, Gmail) and the reminder emails: None in tests.
        self.settings = settings
        self.reminder_log = reminder_log
        self.assistant = Assistant(
            engine, lambda: settings.get("anthropic_api_key") if settings else "",
            config_source=lambda: settings.read() if settings else {},
        )
        self._reminder_thread: Optional[threading.Thread] = None

    def start_reminder_check(self, family_id: int) -> None:
        """When the app is opened: email any newly due reminders, on a
        background thread (Gmail can take a few seconds)."""
        if not self.background_sync or self.settings is None or self.reminder_log is None:
            return
        if not self.settings.email_ready():
            return
        with self._sync_lock:
            if self._reminder_thread is not None and self._reminder_thread.is_alive():
                return
            self._reminder_thread = threading.Thread(
                target=self._run_reminders, args=(family_id,), name="reminder-check", daemon=True
            )
            self._reminder_thread.start()

    def _run_reminders(self, family_id: int) -> None:
        try:
            run_reminder_check(self.engine, family_id, self.settings, self.reminder_log)
        except Exception:  # noqa: BLE001 - a reminder problem must never affect the app
            pass

    def start_background_sync(self, family_id: int) -> None:
        """Start today's NAV and stock price sync on its own thread, unless
        one is already running or both were already tried today."""
        states = [s for s in (self.sync_state, self.stock_sync_state) if s is not None]
        if not states or all(s.last_attempt_date() == date.today() for s in states):
            return
        with self._sync_lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return
            self._sync_thread = threading.Thread(
                target=self._run_syncs, args=(family_id,), name="daily-price-sync", daemon=True
            )
            self._sync_thread.start()

    def _run_syncs(self, family_id: int) -> None:
        if self.sync_state is not None:
            try:
                sync_if_stale(self.engine, family_id, self.sync_state, fetcher=self.nav_fetcher)
            except Exception:  # noqa: BLE001 - recorded in the sync state; never fatal
                pass
        if self.stock_sync_state is not None:
            try:
                sync_stock_prices_if_stale(
                    self.engine, family_id, self.stock_sync_state,
                    fetcher=self.stock_fetcher, index_fetcher=self.index_fetcher,
                )
            except Exception:  # noqa: BLE001
                pass


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardServer  # narrows the inherited attribute's type for readers

    # -- plumbing -------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # quiet by default; uncomment during development if needed

    def _send_json(
        self, status: int, payload: Any, *, extra_headers: Optional[Dict[str, str]] = None
    ) -> None:
        body = json.dumps(payload, cls=_JSONEncoder, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain_body(self) -> None:
        """Read the whole request body off the socket, once, before any
        response is sent.

        A handler that answers without consuming the body (a 401, logout,
        an unknown route) would otherwise close the connection with unread
        data still in the socket's receive buffer — which on Windows makes
        the OS send a TCP reset instead of a clean close, so the client
        intermittently sees `ConnectionAbortedError` instead of the
        response. Draining up front makes every POST path safe.
        """
        length = int(self.headers.get("Content-Length") or 0)
        self._raw_body = self.rfile.read(length) if length > 0 else b""

    def _read_json_body(self) -> dict:
        return json.loads(getattr(self, "_raw_body", b"") or b"{}")

    def _with_body(self, fn: Callable[[dict], None]) -> None:
        """Read the JSON request body and pass it to `fn`; a malformed body
        is a 400, not an unhandled exception."""
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        fn(body)

    @staticmethod
    def _parse_date(value: Optional[str], field: str) -> date:
        if not value:
            raise ValueError(f"'{field}' is required")
        return date.fromisoformat(value)

    # -- auth -------------------------------------------------------------

    def _session_token(self) -> Optional[str]:
        return read_session_token(self.headers.get("Cookie"))

    def _require_auth(self) -> bool:
        """True if the request carries a valid session; otherwise sends 401
        and returns False, so the caller can `return` immediately."""
        if self.server.sessions.is_valid(self._session_token()):
            return True
        self._send_json(401, {"error": "not authenticated"})
        return False

    def _handle_auth_status(self) -> None:
        self._send_json(
            200,
            {
                "password_set": self.server.auth_store.is_password_set(),
                "authenticated": self.server.sessions.is_valid(self._session_token()),
            },
        )

    def _handle_set_password(self, body: dict) -> None:
        try:
            self.server.auth_store.set_password(body.get("password") or "")
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        token = self.server.sessions.create_session()
        self._send_json(200, {"ok": True}, extra_headers={"Set-Cookie": session_cookie_header(token)})

    def _handle_login(self, body: dict) -> None:
        if not self.server.auth_store.verify_password(body.get("password") or ""):
            self._send_json(401, {"error": "incorrect password"})
            return
        token = self.server.sessions.create_session()
        self._send_json(200, {"ok": True}, extra_headers={"Set-Cookie": session_cookie_header(token)})

    def _handle_logout(self) -> None:
        self.server.sessions.invalidate(self._session_token())
        self._send_json(
            200, {"ok": True}, extra_headers={"Set-Cookie": clear_session_cookie_header()}
        )

    # -- routing ----------------------------------------------------------

    def _auto_sync_navs(self, family_id: int) -> None:
        """The automatic "on open" NAV sync. Silent by design: any failure
        is recorded in the sync state (see /api/mfs/sync-status), never
        raised, so the page always loads with the last-known NAVs."""
        if self.server.background_sync:
            self.server.start_background_sync(family_id)
            return
        if self.server.sync_state is None:
            return
        try:
            sync_if_stale(
                self.server.engine, family_id, self.server.sync_state,
                fetcher=self.server.nav_fetcher,
            )
        except Exception:  # noqa: BLE001 - never let a sync problem break a page load
            pass
        self._auto_sync_stock_prices(family_id)

    def _auto_sync_stock_prices(self, family_id: int) -> None:
        """The automatic daily stock price sync — silent, like the NAV one."""
        if self.server.background_sync:
            self.server.start_background_sync(family_id)
            return
        if self.server.stock_sync_state is None:
            return
        try:
            sync_stock_prices_if_stale(
                self.server.engine, family_id, self.server.stock_sync_state,
                fetcher=self.server.stock_fetcher, index_fetcher=self.server.index_fetcher,
            )
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self) -> None:  # noqa: N802 (stdlib's naming convention)
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)

        if path in ("/", "/index.html"):
            self._send_html(_DASHBOARD_HTML_PATH.read_bytes())
            return
        if path == "/api/auth/status":
            self._handle_auth_status()
            return
        if not self._require_auth():
            return

        engine = self.server.engine

        if _FAMILIES_RE.match(path):
            self._send_json(200, {"families": engine.list_families()})
            return

        match = _FAMILY_DASHBOARD_RE.match(path)
        if match:
            family_id = int(match.group(1))
            self._auto_sync_navs(family_id)
            self._handle(lambda: engine.get_family_dashboard(family_id))
            return

        match = _FAMILY_MFS_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._auto_sync_navs(family_id)
            self._handle(
                lambda: engine.get_mf_portfolio(
                    family_id, int(raw_member) if raw_member else None
                )
            )
            return

        match = _FAMILY_ALLOCATION_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._auto_sync_navs(family_id)
            self._handle(
                lambda: engine.get_asset_allocation(
                    family_id, int(raw_member) if raw_member else None
                )
            )
            return

        match = _FAMILY_FDS_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(
                lambda: engine.get_fd_portfolio(family_id, int(raw_member) if raw_member else None)
            )
            return

        match = _FAMILY_INSURANCE_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(
                lambda: engine.get_insurance_portfolio(
                    family_id, int(raw_member) if raw_member else None
                )
            )
            return

        if path == _STOCK_SYNC_STATUS_PATH:
            state = self.server.stock_sync_state.read() if self.server.stock_sync_state else {}
            self._send_json(200, {
                "auto_sync_enabled": self.server.stock_sync_state is not None,
                "last_attempt_date": state.get("last_attempt_date"),
                "last_success_date": state.get("last_success_date"),
                "last_price_date": state.get("last_nav_date"),
                "last_error": state.get("last_error"),
            })
            return

        if path == _MF_SYNC_STATUS_PATH:
            state = self.server.sync_state.read() if self.server.sync_state else {}
            self._send_json(200, {
                "auto_sync_enabled": self.server.sync_state is not None,
                "last_attempt_date": state.get("last_attempt_date"),
                "last_success_date": state.get("last_success_date"),
                "last_nav_date": state.get("last_nav_date"),
                "last_error": state.get("last_error"),
            })
            return

        match = _FAMILY_MEMBERS_RE.match(path)
        if match:
            # Every page load asks for the members: the moment to check reminders.
            self.server.start_reminder_check(int(match.group(1)))
            self._handle(lambda: {"members": engine.list_family_members(int(match.group(1)))})
            return

        if path == _SETTINGS_PATH:
            self._handle(lambda: self._settings().public())
            return

        match = _FAMILY_REMINDERS_RE.match(path)
        if match and not match.group(2):
            family_id = int(match.group(1))
            self._handle(lambda: self._reminder_status(family_id))
            return

        match = _MEMBER_FDS_RE.match(path)
        if match:
            self._handle(lambda: {"fds": engine.list_fds(int(match.group(1)))})
            return

        match = _MEMBER_INSURANCE_RE.match(path)
        if match:
            self._handle(lambda: {"policies": engine.list_insurance_policies(int(match.group(1)))})
            return

        match = _MEMBER_MFS_RE.match(path)
        if match:
            self._handle(lambda: {"mfs": engine.list_mfs(int(match.group(1)))})
            return

        match = _MEMBER_STOCKS_RE.match(path)
        if match:
            self._handle(lambda: {"stocks": engine.list_stocks(int(match.group(1)))})
            return

        match = _MEMBER_PF_RE.match(path)
        if match:
            self._handle(lambda: {"accounts": engine.list_pf_accounts(int(match.group(1)))})
            return

        match = _MEMBER_GOLD_RE.match(path)
        if match:
            self._handle(lambda: {"gold": engine.list_gold(int(match.group(1)))})
            return

        match = _MEMBER_NPS_RE.match(path)
        if match:
            self._handle(lambda: {"accounts": engine.list_nps_accounts(int(match.group(1)))})
            return

        match = _FAMILY_BANK_ACCOUNTS_RE.match(path)
        if match:
            self._handle(lambda: {"accounts": engine.list_family_bank_accounts(int(match.group(1)))})
            return

        match = _FAMILY_CASHFLOW_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            raw_month = (query.get("month") or [date.today().strftime("%Y-%m")])[0]

            def cash_flow():
                month_match = _MONTH_RE.match(raw_month)
                if not month_match:
                    raise ValueError("month must look like 2026-10")
                return engine.get_monthly_cash_flow(
                    family_id, int(month_match.group(1)), int(month_match.group(2)),
                    int(raw_member) if raw_member else None,
                )
            self._handle(cash_flow)
            return

        match = _FAMILY_NPS_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(
                lambda: engine.get_nps_portfolio(family_id, int(raw_member) if raw_member else None)
            )
            return

        match = _FAMILY_HEALTH_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(
                lambda: engine.get_financial_health(family_id, int(raw_member) if raw_member else None)
            )
            return

        match = _FAMILY_LOANS_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(
                lambda: engine.get_loan_portfolio(family_id, int(raw_member) if raw_member else None)
            )
            return

        match = _FAMILY_PLAN_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(lambda: engine.get_plan(family_id, int(raw_member) if raw_member else None))
            return

        match = _FAMILY_GOLD_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(
                lambda: engine.get_gold_portfolio(family_id, int(raw_member) if raw_member else None)
            )
            return

        match = _FAMILY_PF_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._handle(
                lambda: engine.get_pf_portfolio(family_id, int(raw_member) if raw_member else None)
            )
            return

        match = _FAMILY_STOCKS_RE.match(path)
        if match:
            family_id = int(match.group(1))
            raw_member = (query.get("member_id") or [""])[0]
            self._auto_sync_stock_prices(family_id)
            self._handle(
                lambda: engine.get_stock_portfolio(
                    family_id, int(raw_member) if raw_member else None
                )
            )
            return

        self._send_json(404, {"error": f"no route for GET {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        self._drain_body()

        if path == "/api/auth/set-password":
            self._with_body(self._handle_set_password)
            return
        if path == "/api/auth/login":
            self._with_body(self._handle_login)
            return
        if path == "/api/auth/logout":
            self._handle_logout()
            return
        if not self._require_auth():
            return

        engine = self.server.engine

        if _FAMILIES_RE.match(path):
            def create_family(body: dict) -> None:
                self._handle(
                    lambda: {
                        "family": engine.create_family(
                            body["family_name"], body.get("base_currency")
                        )
                    },
                    client_error_status=400,
                )
            self._with_body(create_family)
            return

        match = _FAMILY_MEMBERS_RE.match(path)
        if match:
            family_id = int(match.group(1))

            def create_member(body: dict) -> None:
                self._handle(
                    lambda: {
                        "member": engine.create_family_member(
                            family_id, body["name"], body["relationship"]
                        )
                    },
                    client_error_status=400,
                )
            self._with_body(create_member)
            return

        match = _MEMBER_FDS_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_fd(body: dict) -> None:
                self._handle(
                    lambda: {"fd": self._create_fd(engine, member_id, body)},
                    client_error_status=400,
                )
            self._with_body(create_fd)
            return

        match = _FD_UPDATE_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def update_fd(body: dict) -> None:
                self._handle(
                    lambda: {"fd": engine.update_fd(
                        asset_id=asset_id,
                        member_id=int(body["owner_id"]),
                        bank_name=body["bank_name"],
                        fd_number=body["fd_number"],
                        principal=body["principal"],
                        interest_rate=body.get("interest_rate") or None,
                        issue_date=self._parse_date(body.get("issue_date"), "issue_date"),
                        maturity_date=self._parse_date(body.get("maturity_date"), "maturity_date"),
                        payout_type=body["payout_type"],
                        maturity_value=body.get("maturity_value") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(update_fd)
            return

        match = _FD_DELETE_RE.match(path)
        if match:
            asset_id = int(match.group(1))
            self._handle(
                lambda: (engine.delete_fd(asset_id), {"deleted": asset_id})[1],
                client_error_status=400,
            )
            return

        match = _MEMBER_INSURANCE_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_policy(body: dict) -> None:
                self._handle(
                    lambda: {"policy": self._create_insurance_policy(engine, member_id, body)},
                    client_error_status=400,
                )
            self._with_body(create_policy)
            return

        match = _MEMBER_MFS_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_mf(body: dict) -> None:
                self._handle(
                    lambda: {"mf": self._create_mf(engine, member_id, body)},
                    client_error_status=400,
                )
            self._with_body(create_mf)
            return

        match = _MEMBER_STOCKS_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_stock(body: dict) -> None:
                self._handle(
                    lambda: {"stock": engine.create_stock(
                        member_id=member_id,
                        broker=body["broker"],
                        symbol=body["symbol"],
                        exchange=body["exchange"],
                        initial_txn_date=self._parse_date(
                            body.get("initial_txn_date"), "initial_txn_date"
                        ),
                        initial_quantity=body["initial_quantity"],
                        initial_price=body["initial_price"],
                        initial_charges=body.get("initial_charges") or 0,
                        name=body.get("name") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(create_stock)
            return

        match = _MEMBER_PF_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_pf(body: dict) -> None:
                self._handle(
                    lambda: {"account": engine.create_pf_account(
                        member_id=member_id,
                        account_type=body["account_type"],
                        account_number=body["account_number"],
                        opened_date=self._parse_date(body.get("opened_date"), "opened_date"),
                        interest_rate=body["interest_rate"],
                        employee_contribution=body["employee_contribution"],
                        employer_contribution=body.get("employer_contribution") or None,
                        balance=body["balance"],
                        balance_date=self._parse_date(body.get("balance_date"), "balance_date"),
                        institution=body.get("institution") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(create_pf)
            return

        match = _MEMBER_GOLD_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_gold(body: dict) -> None:
                self._handle(
                    lambda: {"gold": engine.create_gold(
                        member_id=member_id,
                        form=body["form"],
                        purity=body.get("purity") or "24K",
                        quantity_grams=body["quantity_grams"],
                        purchase_rate_per_gram=body["purchase_rate_per_gram"],
                        purchase_date=self._parse_date(body.get("purchase_date"), "purchase_date"),
                        name=body.get("name") or None,
                        institution=body.get("institution") or None,
                        sgb_interest_rate=body.get("sgb_interest_rate") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(create_gold)
            return

        match = _MEMBER_CASHFLOW_ITEMS_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def add_item(body: dict) -> None:
                self._handle(
                    lambda: {"item": engine.add_cash_flow_item(
                        member_id=member_id, flow_type=body["flow_type"], name=body.get("name") or "",
                        amount=body["amount"], frequency=body.get("frequency") or "MONTHLY",
                        category=body.get("category") or None,
                        payment_mode=body.get("payment_mode") or None,
                        account_id=int(body["account_id"]) if body.get("account_id") else None,
                        spent_on=self._parse_date(body["spent_on"], "spent_on") if body.get("spent_on") else None,
                    )},
                    client_error_status=400,
                )
            self._with_body(add_item)
            return

        match = _CASHFLOW_ITEM_UPDATE_RE.match(path)
        if match:
            item_id = int(match.group(1))

            def update_item(body: dict) -> None:
                self._handle(
                    lambda: {"item": engine.update_cash_flow_item(
                        item_id=item_id, member_id=int(body["owner_id"]),
                        flow_type=body["flow_type"], name=body.get("name") or "",
                        amount=body["amount"], frequency=body.get("frequency") or "MONTHLY",
                        category=body.get("category") or None,
                        payment_mode=body.get("payment_mode") or None,
                        account_id=int(body["account_id"]) if body.get("account_id") else None,
                        spent_on=self._parse_date(body["spent_on"], "spent_on") if body.get("spent_on") else None,
                    )},
                    client_error_status=400,
                )
            self._with_body(update_item)
            return

        match = _CASHFLOW_ITEM_DELETE_RE.match(path)
        if match:
            item_id = int(match.group(1))
            self._handle(
                lambda: (engine.delete_cash_flow_item(item_id), {"deleted": item_id})[1],
                client_error_status=400,
            )
            return

        match = _PREMIUM_PAID_RE.match(path)
        if match:
            policy_id = int(match.group(1))

            def premium_paid(body: dict) -> None:
                self._handle(
                    lambda: (engine.set_premium_paid(
                        policy_id=policy_id,
                        due_date=self._parse_date(body.get("due_date"), "due_date"),
                        paid=bool(body.get("paid")),
                    ), {"policy_id": policy_id, "paid": bool(body.get("paid"))})[1],
                    client_error_status=400,
                )
            self._with_body(premium_paid)
            return

        match = _MEMBER_BANK_ACCOUNTS_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def add_account(body: dict) -> None:
                self._handle(
                    lambda: {"account": engine.add_bank_account(
                        member_id=member_id, bank_name=body.get("bank_name") or "",
                        account_last4=body.get("account_last4") or "",
                        balance=body.get("balance") or "0",
                        balance_as_of=self._parse_date(body.get("balance_as_of"), "balance_as_of"),
                        make_default=bool(body.get("make_default")),
                    )},
                    client_error_status=400,
                )
            self._with_body(add_account)
            return

        match = _BANK_ACCOUNT_ACTION_RE.match(path)
        if match:
            account_id, action = int(match.group(1)), match.group(2)

            def account_action(body: dict) -> None:
                def run():
                    if action == "update":
                        return {"account": engine.update_bank_account(
                            account_id=account_id, bank_name=body.get("bank_name") or "",
                            account_last4=body.get("account_last4") or "",
                            make_default=bool(body.get("make_default")),
                        )}
                    if action == "balance":
                        return {"account": engine.set_bank_balance(
                            account_id=account_id, balance=body["balance"],
                            as_of=self._parse_date(body.get("as_of"), "as_of"),
                        )}
                    engine.close_bank_account(account_id)
                    return {"closed": account_id}
                self._handle(run, client_error_status=400)
            self._with_body(account_action)
            return

        match = _MF_SIP_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def set_sip(body: dict) -> None:
                self._handle(
                    lambda: {"mf": engine.update_mf_sip(
                        asset_id=asset_id,
                        sip_amount=body.get("sip_amount") or None,
                        sip_date=int(body["sip_date"]) if body.get("sip_date") else None,
                    )},
                    client_error_status=400,
                )
            self._with_body(set_sip)
            return

        match = _MF_ACTION_RE.match(path)
        if match:
            asset_id, action = int(match.group(1)), match.group(2)

            def mf_action(body: dict) -> None:
                if action == "delete":
                    self._handle(lambda: engine.delete_mf(asset_id) or {"ok": True}, client_error_status=400)
                elif action == "figures":
                    self._handle(lambda: {"mf": engine.set_mf_current_figures(
                        asset_id=asset_id, units=body.get("units") or "0", invested=body.get("invested") or "0",
                        as_of=self._parse_date(body["as_of"], "as_of") if body.get("as_of") else None,
                    )}, client_error_status=400)
                else:
                    self._handle(lambda: {"mf": engine.update_mf_holding(
                        asset_id=asset_id, member_id=int(body["owner_id"]),
                        scheme_name=body.get("scheme_name") or "", folio_number=body.get("folio_number") or "",
                        broker=body.get("broker"),
                    )}, client_error_status=400)
            self._with_body(mf_action)
            return

        match = _MF_TXN_ACTION_RE.match(path)
        if match:
            txn_id, action = int(match.group(1)), match.group(2)

            def mf_txn_action(body: dict) -> None:
                if action == "delete":
                    self._handle(lambda: {"mf": engine.delete_mf_transaction(txn_id)}, client_error_status=400)
                    return
                self._handle(lambda: {"mf": engine.update_mf_transaction(
                    mf_txn_id=txn_id, txn_date=self._parse_date(body.get("txn_date"), "txn_date"),
                    txn_type=body.get("txn_type") or "", units=body.get("units") or "0",
                    amount=body.get("amount") or "0",
                )}, client_error_status=400)
            self._with_body(mf_txn_action)
            return

        match = _MF_BROKER_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def set_broker(body: dict) -> None:
                self._handle(
                    lambda: {"mf": engine.set_mf_broker(asset_id=asset_id, broker=body.get("broker"))},
                    client_error_status=400,
                )
            self._with_body(set_broker)
            return

        match = _MF_PURCHASE_DETAILS_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def purchase_details(body: dict) -> None:
                self._handle(
                    lambda: {"mf": engine.complete_mf_purchase_details(
                        asset_id=asset_id,
                        invested_by=body.get("invested_by") or "",
                        purchase_date=(
                            self._parse_date(body["purchase_date"], "purchase_date")
                            if body.get("purchase_date") else None
                        ),
                        amount=body.get("amount") or None,
                        current_xirr_pct=body.get("current_xirr_pct") or None,
                        sip_amount=body.get("sip_amount") or None,
                        sip_date=int(body["sip_date"]) if body.get("sip_date") else None,
                        lumpsum_amount=body.get("lumpsum_amount") or None,
                        lumpsum_date=(self._parse_date(body["lumpsum_date"], "lumpsum_date")
                                      if body.get("lumpsum_date") else None),
                        lumpsum_units=body.get("lumpsum_units") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(purchase_details)
            return

        match = _MEMBER_NPS_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_nps(body: dict) -> None:
                self._handle(
                    lambda: {"account": engine.create_nps_account(
                        member_id=member_id,
                        pran=body["pran"],
                        tier=body["tier"],
                        opened_date=self._parse_date(body.get("opened_date"), "opened_date"),
                        employee_contribution=body["employee_contribution"],
                        employer_contribution=body.get("employer_contribution") or None,
                        equity_pct=body.get("equity_pct") or 0,
                        corporate_debt_pct=body.get("corporate_debt_pct") or 0,
                        gov_bond_pct=body.get("gov_bond_pct") or 0,
                        balance=body["balance"],
                        balance_date=self._parse_date(body.get("balance_date"), "balance_date"),
                        institution=body.get("institution") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(create_nps)
            return

        match = _NPS_UPDATE_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def update_nps(body: dict) -> None:
                self._handle(
                    lambda: {"account": engine.update_nps_account(
                        asset_id=asset_id,
                        balance=body["balance"],
                        balance_date=self._parse_date(body.get("balance_date"), "balance_date"),
                        employee_contribution=body["employee_contribution"],
                        employer_contribution=body.get("employer_contribution") or None,
                        equity_pct=body.get("equity_pct"),
                        corporate_debt_pct=body.get("corporate_debt_pct"),
                        gov_bond_pct=body.get("gov_bond_pct"),
                    )},
                    client_error_status=400,
                )
            self._with_body(update_nps)
            return

        match = _MEMBER_LOANS_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def create_loan(body: dict) -> None:
                self._handle(
                    lambda: {"loan": engine.create_loan(
                        member_id=member_id,
                        first_disbursement=body.get("first_disbursement") or None,
                        **self._loan_terms(body),
                    )},
                    client_error_status=400,
                )
            self._with_body(create_loan)
            return

        match = _LOAN_ACTION_RE.match(path)
        if match:
            liability_id, action = int(match.group(1)), match.group(2)

            def loan_action(body: dict) -> None:
                if action == "update":
                    run = lambda: {"loan": engine.update_loan(  # noqa: E731
                        liability_id=liability_id, member_id=int(body["owner_id"]),
                        status=body.get("status") or "ACTIVE", **self._loan_terms(body),
                    )}
                elif action == "delete":
                    run = lambda: engine.delete_loan(liability_id) or {"ok": True}  # noqa: E731
                elif action == "disbursements":
                    run = lambda: {"loan": engine.add_loan_disbursement(  # noqa: E731
                        liability_id=liability_id,
                        disbursement_date=self._parse_date(body.get("date"), "date"),
                        amount=body["amount"], note=body.get("note") or None,
                    )}
                elif action == "prepayments":
                    run = lambda: {"loan": engine.add_loan_prepayment(  # noqa: E731
                        liability_id=liability_id,
                        prepayment_date=self._parse_date(body.get("date"), "date"),
                        amount=body["amount"],
                    )}
                elif action == "rate-changes":
                    run = lambda: {"loan": engine.add_loan_rate_change(  # noqa: E731
                        liability_id=liability_id,
                        effective_date=self._parse_date(body.get("date"), "date"),
                        new_rate=body["rate"], reason=body.get("reason") or None,
                    )}
                else:
                    run = lambda: {"what_if": engine.loan_what_if(  # noqa: E731
                        liability_id=liability_id,
                        amount=body.get("amount") or 0,
                        on_date=self._parse_date(body.get("date"), "date"),
                        extra_monthly=body.get("extra_monthly") or 0,
                    )}
                self._handle(run, client_error_status=400)
            self._with_body(loan_action)
            return

        match = _LOAN_EVENT_DELETE_RE.match(path)
        if match:
            kind, event_id = match.group(1), int(match.group(2))
            delete = {
                "loan-disbursements": engine.delete_loan_disbursement,
                "loan-prepayments": engine.delete_loan_prepayment,
                "loan-rate-changes": engine.delete_loan_rate_change,
            }[kind]
            self._handle(lambda: delete(event_id) or {"ok": True}, client_error_status=400)
            return

        match = _MEMBER_CAS_RE.match(path)
        if match:
            member_id, step = int(match.group(1)), match.group(2)

            def statement_import(body: dict) -> None:
                def run():
                    try:
                        data = base64.b64decode(body.get("pdf_base64") or "", validate=True)
                    except (binascii.Error, ValueError):
                        raise ValueError("the file did not arrive intact; choose it again")
                    if not data:
                        raise ValueError("choose the CAS PDF first")
                    statement = parse_cas_pdf(data)
                    names, symbols = self._isin_lookups(statement)
                    if step == "preview":
                        return {"preview": engine.preview_statement_import(
                            member_id=member_id, statement=statement,
                            scheme_names=names, isin_symbols=symbols,
                        )}
                    starts = {
                        key: self._parse_date(value, f"start date of {key}")
                        for key, value in (body.get("policy_start_dates") or {}).items() if value
                    }
                    return {"result": engine.apply_statement_import(
                        member_id=member_id, statement=statement,
                        selected=list(body.get("selected") or []), policy_start_dates=starts,
                        scheme_names=names, isin_symbols=symbols,
                    )}
                self._handle(run, client_error_status=400)
            self._with_body(statement_import)
            return

        match = _FAMILY_GOLD_RATE_RE.match(path)
        if match:
            family_id = int(match.group(1))

            def set_gold_rate(body: dict) -> None:
                self._handle(
                    lambda: engine.record_gold_rate(
                        family_id=family_id,
                        rate_24k_per_gram=body["rate_24k_per_gram"],
                        as_of=self._parse_date(body.get("as_of"), "as_of"),
                    ),
                    client_error_status=400,
                )
            self._with_body(set_gold_rate)
            return

        match = _PF_EDIT_RE.match(path)
        if match:
            asset_id, action = int(match.group(1)), match.group(2)

            def pf_edit(body: dict) -> None:
                if action == "delete":
                    self._handle(lambda: engine.delete_pf_account(asset_id) or {"ok": True},
                                 client_error_status=400)
                    return
                self._handle(
                    lambda: {"account": engine.edit_pf_account(
                        asset_id=asset_id,
                        member_id=int(body["owner_id"]),
                        account_number=body.get("account_number") or "",
                        institution=body.get("institution") or None,
                        opened_date=self._parse_date(body.get("opened_date"), "opened_date"),
                        interest_rate=body["interest_rate"],
                        employee_contribution=body["employee_contribution"],
                        employer_contribution=body.get("employer_contribution") or None,
                        balance=body["balance"],
                        balance_date=self._parse_date(body.get("balance_date"), "balance_date"),
                    )},
                    client_error_status=400,
                )
            self._with_body(pf_edit)
            return

        match = _PF_UPDATE_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def update_pf(body: dict) -> None:
                self._handle(
                    lambda: {"account": engine.update_pf_account(
                        asset_id=asset_id,
                        balance=body["balance"],
                        balance_date=self._parse_date(body.get("balance_date"), "balance_date"),
                        employee_contribution=body["employee_contribution"],
                        employer_contribution=body.get("employer_contribution") or None,
                        interest_rate=body.get("interest_rate") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(update_pf)
            return

        match = _STOCK_TRANSACTIONS_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def add_stock_transaction(body: dict) -> None:
                self._handle(
                    lambda: {"stock": engine.record_stock_transaction(
                        asset_id=asset_id,
                        txn_date=self._parse_date(body.get("txn_date"), "txn_date"),
                        txn_type=body["txn_type"],
                        quantity=body["quantity"],
                        price=body["price"],
                        charges=body.get("charges") or 0,
                        description=body.get("description") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(add_stock_transaction)
            return

        match = _STOCK_ACTION_RE.match(path)
        if match:
            asset_id, action = int(match.group(1)), match.group(2)

            def stock_action(body: dict) -> None:
                if action == "delete":
                    self._handle(lambda: engine.delete_stock(asset_id) or {"ok": True},
                                 client_error_status=400)
                    return
                self._handle(
                    lambda: {"stock": engine.update_stock_holding(
                        asset_id=asset_id, member_id=int(body["owner_id"]),
                        broker=body.get("broker") or "", symbol=body.get("symbol") or "",
                        exchange=body.get("exchange") or "NSE", name=body.get("name") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(stock_action)
            return

        match = _STOCK_TXN_ACTION_RE.match(path)
        if match:
            txn_id, action = int(match.group(1)), match.group(2)

            def stock_txn_action(body: dict) -> None:
                if action == "delete":
                    self._handle(lambda: {"stock": engine.delete_stock_transaction(txn_id)},
                                 client_error_status=400)
                    return
                self._handle(
                    lambda: {"stock": engine.update_stock_transaction(
                        stock_txn_id=txn_id,
                        txn_date=self._parse_date(body.get("txn_date"), "txn_date"),
                        txn_type=body.get("txn_type") or "",
                        quantity=body["quantity"], price=body["price"],
                        charges=body.get("charges") or 0,
                    )},
                    client_error_status=400,
                )
            self._with_body(stock_txn_action)
            return

        match = _STOCK_PRICES_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def add_stock_price(body: dict) -> None:
                self._handle(
                    lambda: {"stock": engine.record_stock_price(
                        asset_id=asset_id,
                        price_date=self._parse_date(body.get("price_date"), "price_date"),
                        price=body["price"],
                    )},
                    client_error_status=400,
                )
            self._with_body(add_stock_price)
            return

        match = _MF_TRANSACTIONS_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def add_mf_transaction(body: dict) -> None:
                self._handle(
                    lambda: {"mf": self._record_mf_transaction(engine, asset_id, body)},
                    client_error_status=400,
                )
            self._with_body(add_mf_transaction)
            return

        match = _INSURANCE_UPDATE_RE.match(path)
        if match:
            policy_id = int(match.group(1))

            def update_policy(body: dict) -> None:
                self._handle(
                    lambda: {"policy": engine.update_insurance_policy(
                        policy_id=policy_id,
                        member_id=int(body["owner_id"]),
                        policy_type=body["policy_type"],
                        insurer=body.get("insurer") or "",
                        policy_number=body.get("policy_number") or "",
                        premium_amount=body["premium_amount"],
                        premium_frequency=body["premium_frequency"],
                        start_date=self._parse_date(body.get("start_date"), "start_date"),
                        sum_assured=body.get("sum_assured") or None,
                        maturity_date=(
                            self._parse_date(body["maturity_date"], "maturity_date")
                            if body.get("maturity_date") else None
                        ),
                        status=body.get("status") or "ACTIVE",
                        plan_name=body.get("plan_name") or None,
                        nominee=body.get("nominee") or None,
                        premium_paying_term_years=body.get("premium_paying_term_years") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(update_policy)
            return

        match = _INSURANCE_SURRENDER_RE.match(path)
        if match:
            policy_id = int(match.group(1))

            def estimate_surrender(body: dict) -> None:
                self._handle(
                    lambda: {"estimate": engine.estimate_surrender_value(
                        policy_id,
                        premium_paying_term_years=body.get("premium_paying_term_years"),
                        installments_paid=body.get("installments_paid"),
                        gsv_factor_pct=body.get("gsv_factor_pct"),
                        bonus_cash_value_factor=body.get("bonus_cash_value_factor"),
                        ssv_factor=body.get("ssv_factor"),
                        discount_rate_pct=body.get("discount_rate_pct"),
                        fund_value=body.get("fund_value"),
                        accrued_bonus=body.get("accrued_bonus"),
                    )},
                    client_error_status=400,
                )
            self._with_body(estimate_surrender)
            return

        match = _INSURANCE_VALUES_RE.match(path)
        if match:
            policy_id = int(match.group(1))

            def set_insurance_values(body: dict) -> None:
                self._handle(
                    lambda: {"policy": engine.update_insurance_values(
                        policy_id,
                        current_surrender_value=body.get("current_surrender_value") or None,
                        accrued_bonus=body.get("accrued_bonus") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(set_insurance_values)
            return

        match = _MF_VALUATIONS_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def add_mf_valuation(body: dict) -> None:
                self._handle(
                    lambda: {"mf": self._record_mf_valuation(engine, asset_id, body)},
                    client_error_status=400,
                )
            self._with_body(add_mf_valuation)
            return

        match = _MEMBER_PROFILE_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def profile(body: dict) -> None:
                self._handle(
                    lambda: {"member": engine.update_member_profile(
                        member_id=member_id, name=body.get("name") or "",
                        relationship=body.get("relationship") or "",
                        date_of_birth=(self._parse_date(body["date_of_birth"], "date_of_birth")
                                       if body.get("date_of_birth") else None),
                        life_stage=body.get("life_stage") or None,
                        heir_member_id=int(body["heir_member_id"]) if body.get("heir_member_id") else None,
                        retirement_age=int(body["retirement_age"]) if body.get("retirement_age") else None,
                    )},
                    client_error_status=400,
                )
            self._with_body(profile)
            return

        match = _MEMBER_SUCCESSION_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def succession(body: dict) -> None:
                self._handle(
                    lambda: engine.set_succession_check(
                        member_id=member_id, item=body.get("item") or "", done=bool(body.get("done")),
                    ) or {"ok": True},
                    client_error_status=400,
                )
            self._with_body(succession)
            return

        match = _MEMBER_RISK_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def set_risk(body: dict) -> None:
                self._handle(
                    lambda: {"member": engine.set_member_risk_tolerance(
                        member_id, body.get("risk_tolerance") or None
                    )},
                    client_error_status=400,
                )
            self._with_body(set_risk)
            return

        if path == _STOCK_INFO_PATH:
            def set_info(body: dict) -> None:
                self._handle(
                    lambda: {"info": engine.set_stock_classification(
                        symbol=body.get("symbol") or "", sector=body.get("sector") or None,
                        cap_category=body.get("cap_category") or None,
                    )},
                    client_error_status=400,
                )
            self._with_body(set_info)
            return

        match = _FAMILY_GOALS_RE.match(path)
        if match:
            family_id = int(match.group(1))
            goal_id = int(match.group(2)) if match.group(2) else None
            goal_verb = match.group(3)

            def goal_action(body: dict) -> None:
                if goal_verb == "delete":
                    self._handle(lambda: engine.delete_goal(goal_id) or {"ok": True}, client_error_status=400)
                    return
                self._handle(lambda: {"goal": engine.save_goal(
                    family_id=family_id, goal_id=goal_id, name=body.get("name") or "",
                    goal_type=body.get("goal_type") or "", target_amount=body.get("target_amount") or "0",
                    target_date=self._parse_date(body.get("target_date"), "target_date"),
                    member_id=int(body["member_id"]) if body.get("member_id") else None,
                    priority=int(body.get("priority") or 2), inflation_pct=body.get("inflation_pct") or None,
                    status=body.get("status") or "ACTIVE",
                )}, client_error_status=400)
            self._with_body(goal_action)
            return

        match = _ASSET_PURPOSE_RE.match(path)
        if match:
            asset_id = int(match.group(1))

            def set_purpose(body: dict) -> None:
                self._handle(lambda: engine.set_asset_purpose(
                    asset_id=asset_id, purpose=body.get("purpose") or None,
                    goal_id=int(body["goal_id"]) if body.get("goal_id") else None,
                    pct=body.get("pct") or "100",
                ) or {"ok": True}, client_error_status=400)
            self._with_body(set_purpose)
            return

        match = _FAMILY_PREFS_RE.match(path)
        if match:
            family_id = int(match.group(1))

            def save_prefs(body: dict) -> None:
                self._handle(lambda: {"preferences": engine.save_planning_preferences(
                    family_id=family_id, tax_slab_pct=body.get("tax_slab_pct") or "0",
                    emergency_months=int(body.get("emergency_months") or 6),
                    min_fd_amount=body.get("min_fd_amount") or "0",
                    avoid_direct_stocks=bool(body.get("avoid_direct_stocks")),
                )}, client_error_status=400)
            self._with_body(save_prefs)
            return

        match = _FAMILY_DISMISS_RE.match(path)
        if match:
            family_id = int(match.group(1))

            def dismiss(body: dict) -> None:
                self._handle(lambda: engine.set_move_dismissed(
                    family_id=family_id, key=body.get("key") or "", dismissed=body.get("dismissed", True) is not False,
                ) or {"ok": True}, client_error_status=400)
            self._with_body(dismiss)
            return

        match = _FAMILY_FUNDING_APPLY_RE.match(path)
        if match:
            family_id = int(match.group(1))
            self._handle(lambda: {"assigned": engine.apply_funding_plan(family_id)}, client_error_status=400)
            return

        match = _MEMBER_GOAL_MONEY_RE.match(path)
        if match:
            member_id = int(match.group(1))

            def goal_money(body: dict) -> None:
                self._handle(lambda: engine.set_member_goal_money(
                    member_id=member_id, goal_money=body.get("goal_money") or "") or {"ok": True},
                    client_error_status=400)
            self._with_body(goal_money)
            return

        match = _FAMILY_ASK_RE.match(path)
        if match:
            family_id = int(match.group(1))

            def ask(body: dict) -> None:
                self._handle(
                    lambda: self.server.assistant.ask(
                        family_id, str(body.get("question") or ""), body.get("conversation_id") or None
                    ),
                    client_error_status=400,
                )
            self._with_body(ask)
            return

        if path == _SETTINGS_PATH:
            def save_settings(body: dict) -> None:
                def run():
                    self._settings().update(body)
                    return self._settings().public()
                self._handle(run, client_error_status=400)
            self._with_body(save_settings)
            return

        if path == _TEST_EMAIL_PATH:
            self._handle(lambda: {"sent_to": send_test_email(self._settings())}, client_error_status=400)
            return

        match = _FAMILY_REMINDERS_RE.match(path)
        if match and match.group(2):
            family_id = int(match.group(1))

            def check_now() -> dict:
                settings = self._settings()
                if not settings.email_ready():
                    raise ValueError("Set up Gmail in Settings (and switch reminders on) first.")
                sent = run_reminder_check(engine, family_id, settings, self.server.reminder_log, force=True)
                status = self._reminder_status(family_id)
                if status["last_error"]:
                    raise ValueError(f"Could not send: {status['last_error']}")
                return {"sent": len(sent), **status}

            self._handle(check_now, client_error_status=400)
            return

        match = _FAMILY_STOCKS_SYNC_RE.match(path)
        if match:
            family_id = int(match.group(1))

            def manual_stock_sync() -> dict:
                if engine.get_family(family_id) is None:
                    raise ValueError(f"family {family_id} does not exist")
                result = sync_family_stock_prices(
                    engine, family_id, fetcher=self.server.stock_fetcher
                )
                if self.server.stock_sync_state is not None:
                    self.server.stock_sync_state.record(date.today(), result)
                classification_error = refresh_stock_classification_if_stale(
                    engine, fetcher=self.server.index_fetcher
                )
                return {"sync": result, "classification_error": classification_error}

            self._handle(manual_stock_sync)
            return

        match = _FAMILY_MFS_SYNC_RE.match(path)
        if match:
            family_id = int(match.group(1))

            def manual_sync() -> dict:
                if engine.get_family(family_id) is None:
                    raise ValueError(f"family {family_id} does not exist")
                result = sync_family_mf_navs(engine, family_id, fetcher=self.server.nav_fetcher)
                if self.server.sync_state is not None:
                    self.server.sync_state.record(date.today(), result)
                return {"sync": result}

            # Always 200 when the request itself was fine: an unreachable AMFI
            # is reported in the result's `error`, not as this server failing.
            self._handle(manual_sync)
            return

        self._send_json(404, {"error": f"no route for POST {path}"})

    def _settings(self) -> SettingsStore:
        if self.server.settings is None:
            raise ValueError("settings are not available on this server")
        return self.server.settings

    def _reminder_status(self, family_id: int) -> dict:
        today = date.today()
        log = self.server.reminder_log.read() if self.server.reminder_log else {}
        upcoming = [
            {**dataclasses.asdict(item), "days_left": (item.due_date - today).days,
             "window": reminder_window(item.due_date, today)}
            for item in self.server.engine.get_due_items(family_id, today=today)
            if (item.due_date - today).days <= 60
        ]
        return {
            "email_ready": bool(self.server.settings and self.server.settings.email_ready()),
            "upcoming": upcoming,
            "last_sent": log.get("last_sent") or [],
            "last_check": log.get("last_check"),
            "last_error": log.get("last_error"),
        }

    def _cached_lookup(self, kind: str, fetch) -> Dict[str, str]:
        """A day-cached ISIN lookup; an unreachable source gives {} (the
        preview then just shows the ISIN), never an error."""
        cached = self.server.isin_lookups.get(kind)
        if cached and cached[0] == date.today():
            return cached[1]
        try:
            table = fetch()
        except Exception:  # noqa: BLE001 - naming is a nicety, never a failure
            return {}
        self.server.isin_lookups[kind] = (date.today(), table)
        return table

    def _isin_lookups(self, statement) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Scheme names for demat funds listed only by fund house, and NSE
        symbols for shares listed without one — fetched only when needed."""
        names: Dict[str, str] = {}
        symbols: Dict[str, str] = {}
        if any(d.name.upper().endswith("MUTUAL FUND") for d in statement.demat_mfs):
            names = self._cached_lookup(
                "names", lambda: parse_amfi_scheme_names(self.server.nav_fetcher())
            )
        if any(not e.symbol for e in statement.equities):
            symbols = self._cached_lookup(
                "symbols", lambda: parse_bhavcopy_isins(self.server.stock_fetcher("NSE"))
            )
        return names, symbols

    def _loan_terms(self, body: dict) -> dict:
        """The loan fields shared by add and edit."""
        return dict(
            loan_type=body["loan_type"],
            lender=body.get("lender") or "",
            sanctioned_amount=body["sanctioned_amount"],
            interest_rate=body["interest_rate"],
            tenure_months=int(body["tenure_months"]),
            start_date=self._parse_date(body.get("start_date"), "start_date"),
            pre_emi_months=int(body.get("pre_emi_months") or 0),
            emi_amount=body.get("emi_amount") or None,
            interest_type=body.get("interest_type") or "FLOATING",
            down_payment=body.get("down_payment") or None,
        )

    def _create_fd(self, engine: FinancialEngine, member_id: int, body: dict):
        return engine.create_fd(
            member_id=member_id,
            bank_name=body["bank_name"],
            fd_number=body["fd_number"],
            principal=body["principal"],
            interest_rate=body.get("interest_rate") or None,
            issue_date=self._parse_date(body.get("issue_date"), "issue_date"),
            maturity_date=self._parse_date(body.get("maturity_date"), "maturity_date"),
            payout_type=body["payout_type"],
            maturity_value=body.get("maturity_value") or None,
            name=body.get("name") or None,
        )

    def _create_insurance_policy(self, engine: FinancialEngine, member_id: int, body: dict):
        return engine.create_insurance_policy(
            member_id=member_id,
            policy_type=body["policy_type"],
            insurer=body["insurer"],
            policy_number=body["policy_number"],
            premium_amount=body["premium_amount"],
            premium_frequency=body["premium_frequency"],
            start_date=self._parse_date(body.get("start_date"), "start_date"),
            sum_assured=body.get("sum_assured") or None,
            maturity_date=(
                self._parse_date(body["maturity_date"], "maturity_date")
                if body.get("maturity_date")
                else None
            ),
            current_surrender_value=body.get("current_surrender_value") or None,
            accrued_bonus=body.get("accrued_bonus") or None,
            plan_name=body.get("plan_name") or None,
            nominee=body.get("nominee") or None,
            premium_paying_term_years=body.get("premium_paying_term_years") or None,
        )

    def _create_mf(self, engine: FinancialEngine, member_id: int, body: dict):
        return engine.create_mutual_fund(
            member_id=member_id,
            amc=body["amc"],
            scheme_name=body["scheme_name"],
            folio_number=body["folio_number"],
            initial_txn_date=self._parse_date(body.get("initial_txn_date"), "initial_txn_date"),
            initial_txn_type=body["initial_txn_type"],
            initial_units=body["initial_units"],
            initial_nav=body["initial_nav"],
            initial_amount=body["initial_amount"],
            isin=body.get("isin") or None,
            sip_enabled=bool(body.get("sip_enabled") or body.get("sip_amount")),
            sip_amount=body.get("sip_amount") or None,
            sip_date=int(body["sip_date"]) if body.get("sip_date") else None,
            name=body.get("name") or None,
            broker=body.get("broker") or None,
        )

    def _record_mf_transaction(self, engine: FinancialEngine, asset_id: int, body: dict):
        return engine.record_mf_transaction(
            asset_id=asset_id,
            txn_date=self._parse_date(body.get("txn_date"), "txn_date"),
            txn_type=body["txn_type"],
            units=body["units"],
            nav=body["nav"],
            amount=body["amount"],
            description=body.get("description") or None,
        )

    def _record_mf_valuation(self, engine: FinancialEngine, asset_id: int, body: dict):
        return engine.record_mf_valuation(
            asset_id=asset_id,
            valuation_date=self._parse_date(body.get("valuation_date"), "valuation_date"),
            nav=body["nav"],
            source=body.get("source") or "MANUAL",
        )

    def _handle(self, action, *, client_error_status: int = 404) -> None:
        """Run `action`; on `ValueError`/`KeyError` respond with
        `client_error_status`, on anything else respond 400 (a rejected
        write, most often a schema constraint from the repository layer,
        surfaced without needing to import its exception types here)."""
        try:
            self._send_json(200, action())
        except (ValueError, KeyError) as exc:
            self._send_json(client_error_status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see module docstring
            self._send_json(400, {"error": str(exc)})


def create_server(
    engine: FinancialEngine,
    auth_store: AuthStore,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    sync_state: Optional[SyncStateStore] = None,
    nav_fetcher: Fetcher = fetch_amfi_nav_file,
    stock_sync_state: Optional[SyncStateStore] = None,
    stock_fetcher: LatestFetcher = fetch_latest_bhavcopy,
    index_fetcher: IndexFetcher = fetch_nse_index_lists,
    background_sync: bool = False,
    settings: Optional[SettingsStore] = None,
    reminder_log: Optional[ReminderLog] = None,
) -> DashboardServer:
    """Build and bind the server without serving — the entry point for tests
    and for anything that wants to run it on its own thread."""
    return DashboardServer(
        (host, port), engine, auth_store, sync_state=sync_state, nav_fetcher=nav_fetcher,
        stock_sync_state=stock_sync_state, stock_fetcher=stock_fetcher,
        index_fetcher=index_fetcher, background_sync=background_sync,
        settings=settings, reminder_log=reminder_log,
    )


def run_server(
    engine: FinancialEngine,
    auth_store: AuthStore,
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    sync_state: Optional[SyncStateStore] = None,
    stock_sync_state: Optional[SyncStateStore] = None,
    settings: Optional[SettingsStore] = None,
    reminder_log: Optional[ReminderLog] = None,
) -> None:
    """Build the server and serve forever (blocks). See `run_dashboard.py`."""
    server = create_server(
        engine, auth_store, host, port, sync_state=sync_state, stock_sync_state=stock_sync_state,
        background_sync=True, settings=settings, reminder_log=reminder_log,
    )
    actual_host, actual_port = server.server_address[:2]
    print(f"Family Finance dashboard: http://{actual_host}:{actual_port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
