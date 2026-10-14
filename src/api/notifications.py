"""
App settings (the Claude API key, the Gmail sender) and the due-date
reminder emails.

Settings live in `family_finance_settings.json` next to the database, on
this machine only and never in git. Secrets are never sent back to the
browser: `SettingsStore.public()` says only whether each one is set.

The reminder check (`run_reminder_check`) runs when the app is opened: it
asks the Engine for the family's FD maturities and premium due dates,
applies the T-15 / T-3 rule in `financial_engine.reminders`, sends one
email listing whatever is newly due, and records what it sent in
`family_finance_reminders.json`, so each item is emailed once per window
however many times the app is opened.
"""

from __future__ import annotations

import json
import smtplib
import ssl
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Dict, List, Optional

from financial_engine import FinancialEngine
from financial_engine.reminders import FD_MATURITY, T3, Reminder, reminders_due

GMAIL_HOST = "smtp.gmail.com"
GMAIL_PORT = 465
# How often the app re-checks while it is being used (each page load asks).
CHECK_INTERVAL = timedelta(minutes=30)

_SECRET_FIELDS = ("anthropic_api_key", "gmail_app_password", "groq_api_key")
_TEXT_FIELDS = ("gmail_address", "reminder_to", "ai_provider", "groq_model")


class SettingsStore:
    """A small JSON file of settings. A corrupt or missing file reads as
    empty — it must never break the dashboard."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, name: str) -> str:
        return str(self.read().get(name) or "").strip()

    def update(self, changes: dict) -> None:
        """Apply the fields present in `changes`. A secret sent as "" is left
        as it is (the form never shows it); `clear_<name>: true` removes it."""
        state = self.read()
        for name in _TEXT_FIELDS:
            if name in changes:
                state[name] = str(changes[name] or "").strip()
        for name in _SECRET_FIELDS:
            value = str(changes.get(name) or "").strip()
            if name == "gmail_app_password":
                value = value.replace(" ", "")  # Google shows it as "abcd efgh ijkl mnop"
            if value:
                state[name] = value
            if changes.get(f"clear_{name}"):
                state.pop(name, None)
        if "reminders_enabled" in changes:
            state["reminders_enabled"] = bool(changes["reminders_enabled"])
        if "privacy_mode" in changes:
            state["privacy_mode"] = bool(changes["privacy_mode"])
        if state.get("ai_provider") not in (None, "", "groq", "claude"):
            raise ValueError("choose Groq or Claude")
        self._path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def public(self) -> dict:
        state = self.read()
        key = str(state.get("anthropic_api_key") or "")
        groq = str(state.get("groq_api_key") or "")
        return {
            "ai_provider": state.get("ai_provider") or ("claude" if key and not groq else "groq"),
            "groq_api_key_set": bool(groq),
            "groq_api_key_hint": f"…{groq[-4:]}" if len(groq) >= 8 else "",
            "groq_model": state.get("groq_model") or "",
            "privacy_mode": bool(state.get("privacy_mode")),
            "anthropic_api_key_set": bool(key),
            "anthropic_api_key_hint": f"…{key[-4:]}" if len(key) >= 8 else "",
            "gmail_address": state.get("gmail_address") or "",
            "gmail_app_password_set": bool(state.get("gmail_app_password")),
            "reminder_to": state.get("reminder_to") or "",
            "reminders_enabled": bool(state.get("reminders_enabled", True)),
        }

    def email_ready(self) -> bool:
        state = self.read()
        return bool(
            state.get("reminders_enabled", True)
            and state.get("gmail_address") and state.get("gmail_app_password")
        )

    def recipients(self) -> List[str]:
        raw = self.get("reminder_to") or self.get("gmail_address")
        return [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]


class ReminderLog:
    """What was sent (`sent`: sent key -> date sent), when the app last
    checked, and the last error."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def write(self, state: dict) -> None:
        self._path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# Sending
# ----------------------------------------------------------------------

Sender = Callable[[EmailMessage, str, str], None]


def send_via_gmail(message: EmailMessage, gmail_address: str, app_password: str) -> None:
    """Send through Gmail's SMTP server with an app password (Google account
    → Security → 2-Step Verification → App passwords)."""
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(GMAIL_HOST, GMAIL_PORT, context=context, timeout=20) as smtp:
        smtp.login(gmail_address, app_password)
        smtp.send_message(message)


def _rupees(amount: Decimal) -> str:
    """₹12,34,567 — Indian digit grouping, whole rupees."""
    whole = str(int(amount.quantize(Decimal("1"))))
    sign, whole = ("-", whole[1:]) if whole.startswith("-") else ("", whole)
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}"


def _when(days_left: int) -> str:
    if days_left == 0:
        return "today"
    if days_left == 1:
        return "tomorrow"
    return f"in {days_left} days"


def compose_reminder_email(reminders: List[Reminder], sender: str, recipients: List[str]) -> EmailMessage:
    soonest = reminders[0]
    count = len(reminders)
    subject = (
        f"Family CFO: {soonest.item.title} due {_when(soonest.days_left)}"
        if count == 1 else f"Family CFO: {count} due dates coming up (first {_when(soonest.days_left)})"
    )
    lines = ["Coming up in the next few days:", ""]
    for r in reminders:
        item = r.item
        what = "matures" if item.kind == FD_MATURITY else "premium due"
        money = f"{_rupees(item.amount)} at maturity" if item.kind == FD_MATURITY else f"premium {_rupees(item.amount)}"
        lines.append(
            f"• {item.owner_name} — {item.title}: {what} on {item.due_date.strftime('%d %b %Y')} "
            f"({_when(r.days_left)}), {money}."
        )
    if any(r.item.kind == FD_MATURITY for r in reminders):
        lines += ["", "For a maturing FD: decide whether to renew, move it to a better rate, or use the money — "
                  "an FD left alone may auto-renew at the bank's current rate."]
    if any(r.item.kind != FD_MATURITY for r in reminders):
        lines += ["", "For a premium: pay before the due date (there is usually a 15–30 day grace period, "
                  "but cover can lapse after it)."]
    final = "This is the last reminder for these." if all(r.window == T3 for r in reminders) else (
        "You will get one more reminder 3 days before each date.")
    lines += ["", final, "", "— Family CFO (sent from your own computer)"]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content("\n".join(lines))
    return message


def send_test_email(settings: SettingsStore, sender: Sender = send_via_gmail) -> str:
    address = settings.get("gmail_address")
    password = settings.read().get("gmail_app_password") or ""
    if not address or not password:
        raise ValueError("Enter your Gmail address and app password first.")
    recipients = settings.recipients()
    message = EmailMessage()
    message["Subject"] = "Family CFO: test email"
    message["From"] = address
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Your Family CFO reminders are set up.\n\n"
        "You will get an email 15 days and again 3 days before each FD maturity and insurance premium "
        "— once per window, when the app is opened."
    )
    try:
        sender(message, address, password)
    except smtplib.SMTPAuthenticationError as exc:
        raise ValueError(
            "Gmail did not accept the address / app password. Use a 16-letter app password "
            "(Google account → Security → 2-Step Verification → App passwords), not your normal password."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ValueError(f"Could not send: {exc}") from exc
    return ", ".join(recipients)


def run_reminder_check(
    engine: FinancialEngine,
    family_id: int,
    settings: SettingsStore,
    log: ReminderLog,
    *,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    sender: Sender = send_via_gmail,
    force: bool = False,
) -> List[Reminder]:
    """Send whatever reminders are newly due; returns what was sent. Checks
    at most every `CHECK_INTERVAL` unless `force`. A failed send records the
    error and is retried on a later check; nothing is marked sent unless it
    went."""
    today = today or date.today()
    now = now or datetime.now()
    if not settings.email_ready():
        return []
    state = log.read()
    last = state.get("last_check")
    if not force and last:
        try:
            if now - datetime.fromisoformat(last) < CHECK_INTERVAL:
                return []
        except ValueError:
            pass
    state["last_check"] = now.isoformat(timespec="seconds")
    sent: Dict[str, str] = state.setdefault("sent", {})
    due = reminders_due(engine.get_due_items(family_id, today=today), today, set(sent))
    if due:
        address = settings.get("gmail_address")
        password = settings.read().get("gmail_app_password") or ""
        try:
            sender(compose_reminder_email(due, address, settings.recipients()), address, password)
        except (smtplib.SMTPException, OSError) as exc:
            state["last_error"] = f"{now:%d %b %H:%M}: {exc}"
            log.write(state)
            return []
        for r in due:
            sent[r.sent_key] = today.isoformat()
        state["last_sent"] = [
            {"what": f"{r.item.owner_name} — {r.item.title}", "due_date": r.item.due_date.isoformat(),
             "window": r.window, "sent_on": today.isoformat()}
            for r in due
        ] + list(state.get("last_sent") or [])[:20]
    state["last_error"] = None
    # Forget keys for dates long past, so the file does not grow for ever.
    cutoff = (today - timedelta(days=60)).isoformat()
    state["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
    log.write(state)
    return due
