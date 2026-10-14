"""Due-date reminder emails (T-15 / T-3, once per window) and the
"Ask Family CFO" assistant — with Gmail and Claude replaced by fakes."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api.assistant import Assistant, AssistantError, scrub
from api.notifications import ReminderLog, SettingsStore, run_reminder_check
from financial_engine import FinancialEngine
from financial_engine.reminders import FD_MATURITY, T3, T15, DueItem, reminder_window, reminders_due
from repository.database import Database
from repository.schema import initialize_schema
from test_api_server import _get, _post, _start_server  # noqa: F401

TODAY = date(2026, 10, 2)


def _item(days, key="FD:1"):
    return DueItem(key=key, kind=FD_MATURITY, owner_name="Raj", title="HDFC Bank FD",
                   due_date=TODAY + timedelta(days=days), amount=Decimal("105000"))


# ---------------------------------------------------------------- the rule

@pytest.mark.parametrize("days, window", [
    (16, None), (15, T15), (4, T15), (3, T3), (0, T3), (-1, None),
])
def test_windows(days, window):
    assert reminder_window(TODAY + timedelta(days=days), TODAY) == window


def test_one_reminder_per_window_however_often_the_app_opens():
    item = _item(10)
    first = reminders_due([item], TODAY, set())
    assert [r.window for r in first] == [T15]
    sent = {first[0].sent_key}
    # Opened again the same day, and a few days later — still inside T-15.
    assert reminders_due([item], TODAY, sent) == []
    assert reminders_due([item], TODAY + timedelta(days=5), sent) == []
    # Inside T-3: one more.
    later = reminders_due([item], TODAY + timedelta(days=8), sent)
    assert [r.window for r in later] == [T3]


def test_a_missed_t15_window_is_not_sent_late():
    assert [r.window for r in reminders_due([_item(2)], TODAY, set())] == [T3]


def test_next_years_premium_on_the_same_policy_is_reminded_afresh():
    this_year = _item(10, key="POLICY:4")
    next_year = DueItem(**{**this_year.__dict__, "due_date": this_year.due_date.replace(year=2027)})
    sent = {reminders_due([this_year], TODAY, set())[0].sent_key}
    assert reminders_due([next_year], date(2027, 10, 2), sent)


# ---------------------------------------------------------------- the check, end to end

@pytest.fixture
def engine(tmp_path):
    database = Database(tmp_path / "r.db")
    initialize_schema(database)
    return FinancialEngine(database)


def _family(engine):
    family = engine.create_family("Sharma")
    member = engine.create_family_member(family.family_id, "Raj", "self")
    return family.family_id, member.member_id


def _settings(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"gmail_address": "family@example.com", "gmail_app_password": "abcd efgh ijkl mnop",
                  "reminder_to": "a@example.com, b@example.com", "reminders_enabled": True})
    return store


def test_reminder_check_emails_once_and_records_it(engine, tmp_path):
    family_id, member_id = _family(engine)
    engine.create_fd(member_id=member_id, bank_name="HDFC Bank", fd_number="FD-0001", principal="100000",
                     interest_rate="7", issue_date=date(2025, 10, 12), maturity_date=TODAY + timedelta(days=10),
                     payout_type="MATURITY")
    engine.create_fd(member_id=member_id, bank_name="SBI", fd_number="FD-0002", principal="50000",
                     interest_rate="7", issue_date=date(2026, 1, 1), maturity_date=TODAY + timedelta(days=200),
                     payout_type="MATURITY")
    settings, log = _settings(tmp_path), ReminderLog(tmp_path / "log.json")
    outbox = []
    send = lambda message, address, password: outbox.append((message, address, password))  # noqa: E731

    now = datetime(2026, 10, 2, 9, 0)
    sent = run_reminder_check(engine, family_id, settings, log, today=TODAY, now=now, sender=send)
    assert [r.item.title for r in sent] == ["HDFC Bank FD"]
    message, address, password = outbox[0]
    assert password == "abcdefghijklmnop"  # spaces as Google shows them are dropped
    assert message["To"] == "a@example.com, b@example.com"
    assert "HDFC Bank FD" in message["Subject"] and "in 10 days" in message["Subject"]
    assert "SBI" not in message.get_content()

    # Opened again later the same day, and the next day: nothing new.
    assert run_reminder_check(engine, family_id, settings, log, today=TODAY,
                              now=now + timedelta(hours=2), sender=send) == []
    assert run_reminder_check(engine, family_id, settings, log, today=TODAY + timedelta(days=1),
                              now=now + timedelta(days=1), sender=send) == []
    # T-3: one more email.
    later = run_reminder_check(engine, family_id, settings, log, today=TODAY + timedelta(days=8),
                               now=now + timedelta(days=8), sender=send)
    assert [r.window for r in later] == [T3] and len(outbox) == 2


def test_a_failed_send_is_retried_and_not_marked_sent(engine, tmp_path):
    family_id, member_id = _family(engine)
    engine.create_insurance_policy(member_id=member_id, policy_type="TERM", insurer="LIC", policy_number="P1",
                                   premium_amount="12000", premium_frequency="ANNUAL",
                                   start_date=date(2020, 10, 9), sum_assured="5000000",
                                   maturity_date=date(2050, 10, 9))
    settings, log = _settings(tmp_path), ReminderLog(tmp_path / "log.json")

    def broken(*_):
        raise OSError("network down")

    now = datetime(2026, 10, 2, 9, 0)
    assert run_reminder_check(engine, family_id, settings, log, today=TODAY, now=now, sender=broken) == []
    assert "network down" in log.read()["last_error"]
    outbox = []
    sent = run_reminder_check(engine, family_id, settings, log, today=TODAY, now=now + timedelta(hours=1),
                              sender=lambda *a: outbox.append(a))
    assert len(sent) == 1 and sent[0].item.kind == "PREMIUM" and log.read()["last_error"] is None


def test_no_email_without_gmail_set_up_or_with_reminders_off(engine, tmp_path):
    family_id, _ = _family(engine)
    store = SettingsStore(tmp_path / "s.json")
    assert run_reminder_check(engine, family_id, store, ReminderLog(tmp_path / "l.json"),
                              sender=lambda *a: pytest.fail("sent")) == []
    store.update({"gmail_address": "x@example.com", "gmail_app_password": "p", "reminders_enabled": False})
    assert not store.email_ready()


def test_settings_never_hand_secrets_back(tmp_path):
    store = _settings(tmp_path)
    store.update({"anthropic_api_key": "sk-ant-secret-1234"})
    public = store.public()
    assert "sk-ant-secret" not in json.dumps(public) and "abcd" not in json.dumps(public)
    assert public["anthropic_api_key_set"] and public["anthropic_api_key_hint"] == "…1234"
    store.update({"anthropic_api_key": ""})  # blank = keep
    assert store.get("anthropic_api_key") == "sk-ant-secret-1234"
    store.update({"clear_anthropic_api_key": True})
    assert store.get("anthropic_api_key") == ""


# ---------------------------------------------------------------- the assistant

def test_scrub_removes_identifiers_but_keeps_figures():
    data = {"bank_name": "HDFC", "fd_number": "FD-1", "account_number": "1234", "company_name": "Infosys",
            "pan": "ABCDE1234F", "member_email": "x@y.z", "principal": "100000",
            "rows": [{"folio": "123/45", "policy_number": "P9", "notes": "PAN ABCDE1234F a/c 123456789012"}]}
    clean = scrub(data)
    assert clean == {"bank_name": "HDFC", "company_name": "Infosys", "principal": "100000",
                     "rows": [{"notes": "PAN [hidden] a/c [hidden]"}]}


class _FakeClient:
    """Stands in for `anthropic.Anthropic`: replays scripted responses and
    records each request."""

    def __init__(self, responses):
        self.requests = []
        self._responses = list(responses)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


def _text(text):
    return SimpleNamespace(type="text", text=text)


def test_assistant_looks_up_figures_with_tools_and_keeps_the_conversation(engine):
    family_id, member_id = _family(engine)
    engine.create_fd(member_id=member_id, bank_name="HDFC Bank", fd_number="SECRET-FD-77", principal="100000",
                     interest_rate="7", issue_date=date(2025, 10, 12), maturity_date=date(2026, 12, 12),
                     payout_type="MATURITY")
    client = _FakeClient([
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", id="t1", name="get_fixed_deposits", input={"member_id": None})]),
        SimpleNamespace(stop_reason="end_turn", content=[_text("You have **one FD**: ₹1,00,000 at HDFC Bank.")]),
        SimpleNamespace(stop_reason="end_turn", content=[_text("It matures on 12 Dec 2026.")]),
    ])
    bot = Assistant(engine, lambda: "sk-test", client_factory=lambda key: client,
                    config_source=lambda: {"ai_provider": "claude"})

    first = bot.ask(family_id, "What FDs do we have?")
    assert first["answer"].startswith("You have **one FD**") and first["tools_used"] == ["get_fixed_deposits"]
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5" and request["fallbacks"] == "default"
    assert "Raj" in request["system"]
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and "HDFC Bank" in tool_result["content"]
    assert "SECRET-FD-77" not in tool_result["content"]

    second = bot.ask(family_id, "When does it mature?", first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"]
    history = client.requests[2]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant", "user"]


def test_assistant_needs_a_key(engine):
    family_id, _ = _family(engine)
    with pytest.raises(AssistantError, match="API key"):
        Assistant(engine, lambda: "").ask(family_id, "hi")


def test_ask_route_without_a_key_explains_what_to_do(tmp_path):
    import http.cookiejar
    import urllib.request

    srv, thread = _start_server(tmp_path, settings=SettingsStore(tmp_path / "s.json"),
                                reminder_log=ReminderLog(tmp_path / "l.json"))
    srv.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        _post(srv, "/api/auth/set-password", {"password": "test-password-123"})
        _, created = _post(srv, "/api/families", {"family_name": "Sharma"})
        family_id = created["family"]["family_id"]
        status, body = _post(srv, f"/api/families/{family_id}/ask", {"question": "net worth?"})
        assert status == 400 and "API key" in body["error"]
        status, body = _post(srv, "/api/settings", {"gmail_address": "f@example.com", "gmail_app_password": "x y"})
        assert status == 200 and body["gmail_app_password_set"] and "x" not in json.dumps(body).replace("example", "")
        status, body = _get(srv, f"/api/families/{family_id}/reminders")
        assert status == 200 and body["email_ready"] and body["upcoming"] == []
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def test_scrub_never_alters_amounts_or_dates():
    clean = scrub({"account_number": "1234", "balance": "112345.00", "as_of": "2026-01-23", "name": "SBI 1234"})
    assert clean == {"balance": "112345.00", "as_of": "2026-01-23", "name": "SBI [hidden]"}


def test_a_limited_pay_policy_stops_asking_for_premiums(engine):
    family_id, member_id = _family(engine)
    pol = engine.create_insurance_policy(
        member_id=member_id, policy_type="ENDOWMENT", insurer="Tata AIA", policy_number="P2",
        premium_amount="100000", premium_frequency="ANNUAL", start_date=date(2020, 10, 15),
        maturity_date=date(2040, 10, 15), sum_assured="1000000", premium_paying_term_years=5,
    )
    assert pol.next_due_date is None and pol.installments_due_so_far == 5
    assert pol.premiums_end_date == date(2025, 10, 15)
    assert not [i for i in engine.get_due_items(family_id, today=TODAY) if i.kind == "PREMIUM"]
    with pytest.raises(ValueError, match="longer than the policy term"):
        engine.update_insurance_policy(
            policy_id=pol.policy_id, member_id=member_id, policy_type="ENDOWMENT", insurer="Tata AIA",
            policy_number="P2", premium_amount="100000", premium_frequency="ANNUAL",
            start_date=date(2020, 10, 15), maturity_date=date(2040, 10, 15), premium_paying_term_years=25,
        )


# ---------------------------------------------------------------- Groq (free) + privacy mode

class _FakeGroq:
    def __init__(self, replies):
        self.payloads, self._replies = [], list(replies)

    def __call__(self, payload, key):
        self.payloads.append(json.loads(json.dumps(payload)))
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _groq_msg(content=None, calls=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
        finish = "tool_calls"
    return {"choices": [{"message": message, "finish_reason": finish}]}


def test_groq_answers_from_compact_tables_with_names_hidden(engine):
    family_id, member_id = _family(engine)
    engine.create_fd(member_id=member_id, bank_name="HDFC Bank", fd_number="SECRET-FD-77", principal="100000",
                     interest_rate="7", issue_date=date(2025, 10, 12), maturity_date=date(2026, 12, 12),
                     payout_type="MATURITY")
    fake = _FakeGroq([
        _groq_msg(calls=[{"id": "c1", "type": "function",
                          "function": {"name": "get_fixed_deposits", "arguments": "{\"member_id\": null}"}}]),
        _groq_msg("Person 1 has one FD at Bank A: ₹1,00,000, maturing 12 Dec 2026."),
    ])
    bot = Assistant(engine, lambda: "", groq_transport=fake,
                    config_source=lambda: {"ai_provider": "groq", "groq_api_key": "gsk_test", "privacy_mode": True})
    result = bot.ask(family_id, "What FDs does Raj have at HDFC Bank?")
    assert result["answer"] == "Raj has one FD at HDFC Bank: ₹1,00,000, maturing 12 Dec 2026."
    sent = json.dumps(fake.payloads)
    assert "Raj" not in sent and "HDFC" not in sent and "SECRET-FD-77" not in sent
    assert "Person 1" in sent and "Bank A" in sent
    tool_msg = fake.payloads[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    assert '"principal":"100000"' in tool_msg["content"] or "100000" in tool_msg["content"]
    assert fake.payloads[0]["model"] == "openai/gpt-oss-120b"


def test_groq_free_limit_is_explained(engine):
    import io
    import urllib.error
    family_id, _ = _family(engine)
    busy = urllib.error.HTTPError("u", 429, "Too Many", {"retry-after": "55"}, io.BytesIO(b"{}"))
    bad_key = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
    config = lambda: {"ai_provider": "groq", "groq_api_key": "gsk_test"}  # noqa: E731
    with pytest.raises(AssistantError, match="free limit"):
        Assistant(engine, lambda: "", groq_transport=_FakeGroq([busy]), config_source=config).ask(family_id, "hi")
    with pytest.raises(AssistantError, match="key"):
        Assistant(engine, lambda: "", groq_transport=_FakeGroq([bad_key]), config_source=config).ask(family_id, "hi")
    with pytest.raises(AssistantError, match="Groq API key"):
        Assistant(engine, lambda: "", config_source=lambda: {"ai_provider": "groq"}).ask(family_id, "hi")


def test_compact_keeps_figures_as_tables():
    from api.assistant import compact
    data = {"rows": [{"owner_name": "A", "fd": {"bank_name": "X", "principal": "100000.00", "xirr": "0.123456",
                                                 "asset_id": 3, "transactions": [1, 2]}},
                     {"owner_name": "B", "fd": {"bank_name": "Y", "principal": "2500.55", "xirr": None}}]}
    assert compact(data) == {"rows": {"cols": ["owner_name", "bank_name", "principal", "xirr"],
                                      "rows": [["A", "X", "100000", "0.1235"], ["B", "Y", "2501", None]]}}


# ---------------------------------------------------------------- one-time (ad hoc) cash-flow rows

def test_a_one_time_expense_counts_only_in_its_month_and_once_in_the_bank(engine):
    family_id, member_id = _family(engine)
    today = date(2026, 10, 9)
    engine.add_bank_account(member_id=member_id, bank_name="SBI", balance="100000",
                            balance_as_of=date(2026, 9, 1), make_default=True)
    engine.add_cash_flow_item(member_id=member_id, flow_type="EXPENSE", name="Car repair", amount="15000",
                              frequency="ONCE", spent_on=date(2026, 10, 3), payment_mode="UPI")
    october = engine.get_monthly_cash_flow(family_id, 2026, 10, today=today)
    november = engine.get_monthly_cash_flow(family_id, 2026, 11, today=today)
    line = next(l for l in october.expenses if l.label == "Car repair")
    assert line.monthly_amount == Decimal("15000") and line.spent_on == date(2026, 10, 3)
    assert not [l for l in november.expenses if l.label == "Car repair"]
    assert october.accounts[0].estimated_balance == Decimal("85000")  # taken once, on its date
    assert october.accounts[0].monthly_net == 0  # not a regular monthly flow
    with pytest.raises(ValueError, match="date"):
        engine.add_cash_flow_item(member_id=member_id, flow_type="EXPENSE", name="Gift", amount="500",
                                  frequency="ONCE")
