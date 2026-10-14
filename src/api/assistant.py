"""
"Ask Family CFO" — a chat assistant that answers questions about the
family's money, through Groq (free; open models) or Claude (paid), as
chosen in Settings.

The model is given read-only tools, one per part of the app (MFs, FDs,
loans, cash flow, health...), each returning the same figures the dashboard
shows, via the one `FinancialEngine`. It cannot change anything.

Privacy: what a tool returns goes to the chosen API, so identifiers are
removed first — PAN, account / folio / policy / FD numbers, PRAN, UAN,
demat IDs, email addresses, phone numbers and addresses (`scrub`). With
privacy mode on, people become "Person 1, 2..." and banks / insurers /
lenders / platforms "Bank A, Insurer B..." too (`_Masks`), the question
included; the answer has the real names put back on this machine.

Groq's free tier allows only a few thousand tokens a minute, so for Groq
the figures go as compact tables (`compact`): no transaction lists, whole
rupees, one header row per table.

Conversations are kept in memory on the server (lost on restart). For
Claude every turn — including its thinking and tool calls — is sent back
exactly as it came; for Groq only the questions and answers are kept.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from financial_engine import FinancialEngine

MODEL = "claude-opus-5-5"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
GROQ_MAX_WAIT = 20  # seconds the app will wait out a "too many requests" before giving up
COMPACT_LIMIT = 12000  # characters of one tool result sent to Groq (~3,000 tokens)
MAX_TOOL_ROUNDS = 8
MAX_CONVERSATIONS = 20
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Keys whose values never leave this machine. Matched on whole "_"-separated
# words of a key, so "company" is not mistaken for "pan".
_PRIVATE_WORDS = frozenset({
    "pan", "email", "phone", "mobile", "address", "pran", "uan", "ifsc", "ucc", "folio",
    "dp", "password", "token",
})
_PRIVATE_KEYS = frozenset({"client_id", "dp_id", "number", "folio_no"})


def _private_key(key: str) -> bool:
    k = key.lower()
    words = k.split("_")
    return k in _PRIVATE_KEYS or k.endswith("_number") or any(w in _PRIVATE_WORDS for w in words)


_DIGITS = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b|\b\d{9,}\b")  # a PAN, or a long account-like number
_FIGURE = re.compile(r"^-?\d+(\.\d+)?$|^\d{4}-\d{2}-\d{2}$")


def _private_values(value: Any, found: set) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if _private_key(str(k)) and isinstance(v, (str, int)) and len(str(v).strip()) >= 3:
                found.add(str(v).strip())
            _private_values(v, found)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _private_values(v, found)


def scrub(value: Any) -> Any:
    """Drop identifier fields (recursively), then mask those same values
    wherever they reappear inside other text (an FD's default name is
    "<bank> <FD number>"), and any PAN / long digit run left in free text."""
    hidden: set = set()
    _private_values(value, hidden)
    ordered = sorted(hidden, key=len, reverse=True)

    def clean(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if not _private_key(str(k))}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        if isinstance(v, str):
            if _FIGURE.match(v):
                return v  # an amount or a date: never touched
            v = _DIGITS.sub("[hidden]", v)
            for secret in ordered:
                v = v.replace(secret, "[hidden]")
            return v
        return v

    return clean(value)


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------

_MEMBER = {
    "type": ["integer", "null"],
    "description": "A member_id from the family list for one person, or null for the whole family.",
}


def _tool(name: str, description: str, extra: Optional[Dict[str, Any]] = None) -> dict:
    properties = {"member_id": _MEMBER, **(extra or {})}
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("get_net_worth", "Net worth and asset allocation: invested and current value by asset class "
          "(MF, stocks, FD, EPF/PPF, NPS, gold, savings insurance), with loans outstanding."),
    _tool("get_mutual_funds", "Mutual fund holdings: each fund's units, invested, current value, gain, XIRR, "
          "SIP amount and status (running / stopped / lumpsum), broker, category, and transactions."),
    _tool("get_stocks", "Stock holdings: quantity, average price, latest price, value, gain, sector, "
          "market cap, broker, and buy / sell history."),
    _tool("get_fixed_deposits", "Fixed deposits: bank, principal, rate, issue and maturity dates, "
          "interest earned so far, amount at maturity, upcoming maturities, totals by bank."),
    _tool("get_insurance", "Insurance policies: type, insurer, plan, sum assured, premium and frequency, "
          "next premium due date, maturity, surrender value, and a review of the plan's pros and cons."),
    _tool("get_loans", "Loans: type, lender, principal, rate, EMI, outstanding balance, tenure left, "
          "interest paid / to pay, prepayments, and the amortization summary by financial year."),
    _tool("get_provident_funds", "EPF and PPF accounts: balance, contributions, interest rate, interest earned."),
    _tool("get_nps", "NPS accounts (Tier I / II): contributions, current value, gain."),
    _tool("get_gold", "Gold holdings (physical, digital, SGB): grams, purity, cost, current value."),
    _tool("get_cash_flow", "One month's income, expenses, investments, EMIs and cash left over.",
          {"month": {"type": "string", "description": "The month as YYYY-MM, e.g. 2026-09."}}),
    _tool("get_financial_health", "The Financial Health assessment: score, checks (emergency fund, savings "
          "rate, EMI load, insurance cover, equity mix...), suggested actions, projections, each person's "
          "life stage and plan, inheritance and retirement notes."),
    _tool("get_upcoming_dues", "FD maturities and insurance premiums from today on, soonest first."),
    _tool("get_goals_and_plan", "The family's own goals (amount, date, on track or behind, monthly SIP needed), "
          "what holdings are earmarked for, area scores and personalised suggestions ('smart moves')."),
]


def _bank_accounts(engine: FinancialEngine, family_id: int, member_id: Optional[int]):
    accounts = engine.list_family_bank_accounts(family_id)
    return [a for a in accounts if member_id is None or getattr(a, "member_id", None) == member_id]


def _cash_flow(engine: FinancialEngine, family_id: int, member_id: Optional[int], month: str):
    found = re.match(r"^(\d{4})-(\d{2})$", month or "")
    if not found:
        raise ValueError("month must look like 2026-09")
    return engine.get_monthly_cash_flow(family_id, int(found.group(1)), int(found.group(2)), member_id)


# Bulky parts left out of a tool's answer (each question re-sends it): a
# loan's month-by-month schedule (its year-by-year summary stays), and the
# FD list repeated as "upcoming maturities".
_BULKY = {"get_loans": ("schedule",), "get_fixed_deposits": ("upcoming_maturities",)}


def _drop(value: Any, keys: tuple) -> Any:
    if not keys:
        return value
    if isinstance(value, dict):
        return {k: _drop(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_drop(v, keys) for v in value]
    return value


# ----------------------------------------------------------------------
# Compact form (for Groq's small per-minute limit)
# ----------------------------------------------------------------------

_COMPACT_DROP = frozenset({
    "transactions", "description", "isin", "current_nav_date", "review", "schedule",
    "upcoming_maturities", "key", "source", "is_opening_balance", "import_batch_id",
    # repeats of other columns, or bookkeeping a question never needs
    "current_nav_source", "current_price_source", "current_price_date", "kinds", "investment_type",
    "sip_units", "lumpsum_units", "opening_amount", "parts", "checklist", "amc", "exchange",
})
_NAMED_BY = ("scheme_name", "bank_name")  # rows whose "name" only repeats these
_NUMBER = re.compile(r"^-?\d+\.\d+$")


def _short_number(text: str) -> str:
    """Whole rupees for big amounts, 2 decimals for small ones, 4 for
    fractions (XIRR 0.1234 = 12.34%)."""
    value = float(text)
    if abs(value) >= 1000:
        return str(int(round(value)))
    if abs(value) >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _flatten(row: dict) -> dict:
    flat: dict = {}
    for k, v in row.items():
        if isinstance(v, dict):
            for ck, cv in v.items():
                flat[ck if ck not in flat and ck not in row else f"{k}.{ck}"] = cv
        else:
            flat[k] = v
    return flat


def compact(value: Any) -> Any:
    """The same figures, smaller: drop bulky / internal fields, shorten
    numbers, and turn each list of records into one table
    ({"cols": [...], "rows": [[...], ...]})."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _COMPACT_DROP or k == "id" or k.endswith("_id") or v in (None, "", [], {}):
                continue
            if k == "name" and any(n in value for n in _NAMED_BY):
                continue
            out[k] = compact(v)
        return out
    if isinstance(value, list):
        if len(value) >= 2 and all(isinstance(x, dict) for x in value):
            rows = [compact(_flatten(x)) for x in value]
            cols: List[str] = []
            for r in rows:
                cols += [c for c in r if c not in cols]
            return {"cols": cols, "rows": [[r.get(c) for c in cols] for r in rows]}
        return [compact(x) for x in value]
    if isinstance(value, str) and _NUMBER.match(value):
        return _short_number(value)
    return value


def _fit(data: Any, limit: int = COMPACT_LIMIT) -> str:
    """Serialise; if still over `limit`, cut the longest table's rows and say so."""
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    while len(text) > limit:
        tables: List[dict] = []

        def find(v: Any) -> None:
            if isinstance(v, dict):
                if "rows" in v and "cols" in v and len(v["rows"]) > 3:
                    tables.append(v)
                for x in v.values():
                    find(x)
            elif isinstance(v, list):
                for x in v:
                    find(x)
        find(data)
        if not tables:
            return text[:limit] + "...(cut short)"
        biggest = max(tables, key=lambda t: len(json.dumps(t["rows"])))
        keep = max(3, len(biggest["rows"]) * 2 // 3)
        dropped = len(biggest["rows"]) - keep + biggest.get("more_rows_not_shown", 0)
        biggest["rows"] = biggest["rows"][:keep]
        biggest["more_rows_not_shown"] = dropped
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return text


# ----------------------------------------------------------------------
# Privacy mode: aliases for people and institutions
# ----------------------------------------------------------------------

_INSTITUTION_KEYS = {
    "bank_name": "Bank", "bank": "Bank", "insurer": "Insurer", "lender": "Lender",
    "broker": "Platform", "institution": "Institution",
}


class _Masks:
    """Real name <-> alias, kept for a whole conversation."""

    def __init__(self) -> None:
        self.alias: Dict[str, str] = {}
        self.real: Dict[str, str] = {}
        self._counts: Dict[str, int] = {}

    def add(self, real: str, kind: str, alias: Optional[str] = None) -> str:
        real = (real or "").strip()
        if len(real) < 2 or real in self.alias:
            return self.alias.get(real, real)
        if alias is None:
            n = self._counts.get(kind, 0)
            self._counts[kind] = n + 1
            alias = f"{kind} {chr(65 + n) if n < 26 else n + 1}"
        self.alias[real] = alias
        self.real.setdefault(alias, real)
        return alias

    def add_people(self, members) -> None:
        firsts: Dict[str, int] = {}
        for m in members:
            first = m.name.split()[0]
            firsts[first.lower()] = firsts.get(first.lower(), 0) + 1
        for i, m in enumerate(members, 1):
            alias = f"Person {i}"
            self.add(m.name, "Person", alias)
            first = m.name.split()[0]
            if len(first) >= 3 and firsts[first.lower()] == 1 and first != m.name:
                self.alias.setdefault(first, alias)

    def collect(self, value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                self.collect(v, k)
        elif isinstance(value, list):
            for v in value:
                self.collect(v, key)
        elif isinstance(value, str) and key in _INSTITUTION_KEYS and not _FIGURE.match(value):
            self.add(value, _INSTITUTION_KEYS[key])

    @staticmethod
    def _swap(text: str, table: Dict[str, str]) -> str:
        if not table or not text:
            return text
        names = sorted(table, key=len, reverse=True)
        pattern = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])", re.IGNORECASE)
        lookup = {n.lower(): table[n] for n in names}
        return pattern.sub(lambda m: lookup[m.group(1).lower()], text)

    def mask(self, text: str) -> str:
        return self._swap(text, self.alias)

    def unmask(self, text: str) -> str:
        return self._swap(text, self.real)


_HANDLERS: Dict[str, Callable[..., Any]] = {
    "get_net_worth": lambda e, f, m, **_: e.get_asset_allocation(f, m),
    "get_mutual_funds": lambda e, f, m, **_: e.get_mf_portfolio(f, m),
    "get_stocks": lambda e, f, m, **_: e.get_stock_portfolio(f, m),
    "get_fixed_deposits": lambda e, f, m, **_: e.get_fd_portfolio(f, m),
    "get_insurance": lambda e, f, m, **_: e.get_insurance_portfolio(f, m),
    "get_loans": lambda e, f, m, **_: e.get_loan_portfolio(f, m),
    "get_provident_funds": lambda e, f, m, **_: e.get_pf_portfolio(f, m),
    "get_nps": lambda e, f, m, **_: e.get_nps_portfolio(f, m),
    "get_gold": lambda e, f, m, **_: e.get_gold_portfolio(f, m),
    "get_cash_flow": lambda e, f, m, month="", **_: _cash_flow(e, f, m, month),
    "get_financial_health": lambda e, f, m, **_: e.get_financial_health(f, m),
    "get_goals_and_plan": lambda e, f, m, **_: e.get_plan(f, m).plan,
    "get_upcoming_dues": lambda e, f, m, **_: [
        d for d in e.get_due_items(f)
        if m is None or d.owner_name == next(
            (x.name for x in e.list_family_members(f) if x.member_id == m), None)
    ],
}


SYSTEM_PROMPT = """You are Family CFO, the assistant inside a family's personal finance app in India. \
You answer questions about this family's own money using the tools, which return the same figures the \
app's pages show. Always fetch the figures with a tool rather than guessing; call several tools at once \
when a question spans areas.

How to answer:
- Lead with the answer. Keep it short: a few sentences or a small table, not an essay.
- Money in rupees with Indian grouping and units: ₹12,34,567 or ₹12.35 L / ₹1.20 Cr.
- Say whose money it is when the family has more than one person.
- When you compute something (a sum, a projection), say briefly how.
- If the data needed is not in the app, say what is missing and which page it is entered on.
- Tool data omits account, folio, policy and PAN numbers on purpose; never ask the user for them.
- You can only read. If asked to change something, say which page and button does it.
- For suggestions, follow Indian norms (80C / 80D / 80TTB, the new vs old tax regime, term cover, \
emergency fund) and say plainly that this is a guide, not personal financial advice. Tax rules change; \
say which year's rules you assume.
- Amounts in tool results are strings holding exact decimals.
- Some results are tables: "cols" names the columns of each row in "rows". XIRR and similar are \
fractions (0.1234 = 12.34%).
- If names look like "Person 1" or "Bank A", the user has hidden real names: use them as they are."""


class AssistantError(Exception):
    """A problem to show the user as it is (no key, key rejected, ...)."""


GroqTransport = Callable[[dict, str], dict]


def groq_post(payload: dict, api_key: str) -> dict:
    """POST one chat completion to Groq; returns the JSON reply. Raises
    `urllib.error.HTTPError` / `URLError` for the caller to explain."""
    request = urllib.request.Request(
        GROQ_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "FamilyCFO/1.0"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def _openai_tools() -> List[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
    }} for t in TOOLS]


class Assistant:
    def __init__(self, engine: FinancialEngine, api_key_source: Callable[[], str],
                 client_factory: Optional[Callable[[str], Any]] = None,
                 config_source: Optional[Callable[[], dict]] = None,
                 groq_transport: Optional[GroqTransport] = None) -> None:
        self._engine = engine
        self._api_key_source = api_key_source
        self._client_factory = client_factory
        self._config_source = config_source or (lambda: {})
        self._groq_transport = groq_transport or groq_post
        self._conversations: "OrderedDict[str, List[dict]]" = OrderedDict()
        self._masks: Dict[str, _Masks] = {}

    def _config(self) -> dict:
        config = dict(self._config_source() or {})
        if not config.get("ai_provider"):
            # Groq (free) unless only a Claude key has been saved
            config["ai_provider"] = "claude" if config.get("anthropic_api_key") and not config.get("groq_api_key") else "groq"
        return config

    def _client(self):
        key = self._api_key_source()
        if not key:
            raise AssistantError("Add your Anthropic API key in Settings to use the assistant.")
        if self._client_factory is not None:
            return self._client_factory(key)
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise AssistantError(
                "The Anthropic library is not installed: run  .venv\\Scripts\\pip install anthropic"
            ) from exc
        return anthropic.Anthropic(api_key=key, timeout=120.0)

    def _system(self, family_id: int, masks: Optional[_Masks] = None) -> str:
        members = self._engine.list_family_members(family_id)
        people = "\n".join(
            f"- member_id {m.member_id}: {m.name}"
            + (f" ({m.relationship})" if getattr(m, "relationship", None) else "")
            + (f", born {m.date_of_birth.year}" if getattr(m, "date_of_birth", None) else "")
            + (f", {m.life_stage.lower()}" if getattr(m, "life_stage", None) else "")
            for m in members
        )
        text = f"{SYSTEM_PROMPT}\n\nToday is {date.today():%d %B %Y}.\nThe family:\n{people}"
        return masks.mask(text) if masks else text

    def run_tool(self, family_id: int, name: str, args: dict, *, small: bool = False,
                 masks: Optional[_Masks] = None) -> str:
        handler = _HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown tool {name}")
        member_id = args.get("member_id")
        if member_id is not None:
            member_id = int(member_id)
            if member_id not in {m.member_id for m in self._engine.list_family_members(family_id)}:
                raise ValueError(f"no member {member_id} in this family")
        result = handler(self._engine, family_id, member_id, **{k: v for k, v in args.items() if k != "member_id"})
        from .server import _JSONEncoder  # the same Decimal/date encoding the dashboard gets
        data = scrub(_drop(json.loads(json.dumps(result, cls=_JSONEncoder)), _BULKY.get(name, ())))
        if masks is not None:
            masks.collect(data)
        text = _fit(compact(data)) if small else json.dumps(data, separators=(",", ":"))
        return masks.mask(text) if masks is not None else text

    def _conversation(self, conversation_id: Optional[str]) -> Tuple[str, List[dict]]:
        if conversation_id and conversation_id in self._conversations:
            self._conversations.move_to_end(conversation_id)
            return conversation_id, self._conversations[conversation_id]
        conversation_id = secrets.token_hex(8)
        self._conversations[conversation_id] = []
        while len(self._conversations) > MAX_CONVERSATIONS:
            old, _ = self._conversations.popitem(last=False)
            self._masks.pop(old, None)
        return conversation_id, self._conversations[conversation_id]

    def _masks_for(self, conversation_id: str, family_id: int, on: bool) -> Optional[_Masks]:
        if not on:
            return None
        masks = self._masks.get(conversation_id)
        if masks is None:
            masks = _Masks()
            masks.add_people(self._engine.list_family_members(family_id))
            # Learn every bank / insurer / lender / platform name up front, so
            # one named in the question itself is hidden too.
            for name in ("get_fixed_deposits", "get_insurance", "get_loans", "get_mutual_funds",
                         "get_stocks", "get_provident_funds"):
                try:
                    self.run_tool(family_id, name, {"member_id": None}, masks=masks)
                except Exception:  # noqa: BLE001 - a part that fails just contributes no names
                    pass
            self._masks[conversation_id] = masks
        return masks

    def ask(self, family_id: int, question: str, conversation_id: Optional[str] = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise ValueError("Type a question.")
        config = self._config()
        if config["ai_provider"] == "groq":
            return self._ask_groq(family_id, question, conversation_id, config)
        return self._ask_claude(family_id, question, conversation_id, config)

    # -- Groq (OpenAI-style chat completions, plain HTTP) ------------------

    def _ask_groq(self, family_id: int, question: str, conversation_id: Optional[str], config: dict) -> dict:
        key = str(config.get("groq_api_key") or "").strip()
        if not key:
            raise AssistantError("Add your Groq API key in Settings to use the assistant.")
        model = str(config.get("groq_model") or "").strip() or GROQ_DEFAULT_MODEL
        conversation_id, history = self._conversation(conversation_id)
        masks = self._masks_for(conversation_id, family_id, bool(config.get("privacy_mode")))
        asked = masks.mask(question) if masks else question
        messages = [{"role": "system", "content": self._system(family_id, masks)}]
        messages += history[-6:]  # the last few questions and answers, no tool data
        messages.append({"role": "user", "content": asked})
        tools_used: List[str] = []
        answer = ""
        for _ in range(MAX_TOOL_ROUNDS):
            payload = {"model": model, "messages": messages, "tools": _openai_tools(),
                       "tool_choice": "auto", "temperature": 0.2, "max_completion_tokens": 2048}
            if model.startswith("openai/gpt-oss"):
                payload["reasoning_effort"] = "medium"
            choice = self._groq_call(payload, key)["choices"][0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            if not calls:
                answer = (message.get("content") or "").strip()
                if choice.get("finish_reason") == "length":
                    answer += "\n\n(The answer was cut short.)"
                break
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
            for call in calls:
                name = (call.get("function") or {}).get("name", "")
                tools_used.append(name)
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                    content = self.run_tool(family_id, name, args, small=True, masks=masks)
                except Exception as exc:  # noqa: BLE001 - reported back to the model as a tool error
                    content = f"Error: {exc}"
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": content})
        else:
            answer = "That needed more look-ups than I allow for one question — try asking it in parts."
        history += [{"role": "user", "content": asked}, {"role": "assistant", "content": answer}]
        shown = masks.unmask(answer) if masks else answer
        return {"conversation_id": conversation_id, "answer": shown or "(no answer)",
                "tools_used": tools_used, "provider": "groq"}

    def _groq_call(self, payload: dict, key: str) -> dict:
        waited = 0
        while True:
            try:
                return self._groq_transport(payload, key)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    pass
                detail = ""
                try:
                    detail = json.loads(body).get("error", {}).get("message", "")
                except (ValueError, AttributeError):
                    pass
                if exc.code == 429:
                    try:
                        wait = int(float(exc.headers.get("retry-after") or 10))
                    except (TypeError, ValueError):
                        wait = 10
                    if waited + wait <= GROQ_MAX_WAIT:
                        time.sleep(max(wait, 1))
                        waited += max(wait, 1)
                        continue
                    raise AssistantError(
                        f"Groq's free limit is used up for the moment — try again in about {max(wait, 30)} seconds"
                        " (or ask about one thing at a time)."
                    ) from exc
                if exc.code in (401, 403):
                    raise AssistantError("Groq did not accept the API key — check it in Settings.") from exc
                if exc.code == 413 or "too large" in detail.lower():
                    raise AssistantError(
                        "That needed more data than Groq's free tier takes at once — ask about one area or one person."
                    ) from exc
                if exc.code == 404 or "model" in detail.lower() and "not" in detail.lower():
                    raise AssistantError(f"Groq does not offer the model set in Settings: {detail or exc.code}") from exc
                if exc.code >= 500:
                    raise AssistantError("Groq's service had a problem — try again in a minute.") from exc
                raise AssistantError(f"Groq rejected the request: {detail or exc.code}") from exc
            except urllib.error.URLError as exc:
                raise AssistantError("Could not reach Groq — check the internet connection.") from exc

    # -- Claude (Anthropic SDK) -------------------------------------------

    def _ask_claude(self, family_id: int, question: str, conversation_id: Optional[str], config: dict) -> dict:

        client = self._client()
        conversation_id, messages = self._conversation(conversation_id)
        masks = self._masks_for(conversation_id, family_id, bool(config.get("privacy_mode")))
        # Work on a copy; commit the turn only once it has finished, so a
        # failed turn leaves the stored conversation as it was.
        turn = list(messages) + [{"role": "user", "content": masks.mask(question) if masks else question}]
        system = self._system(family_id, masks)
        tools_used: List[str] = []
        import anthropic

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = client.beta.messages.create(
                    model=MODEL,
                    max_tokens=16000,
                    system=system,
                    tools=TOOLS,
                    messages=turn,
                    output_config={"effort": "medium"},
                    cache_control={"type": "ephemeral"},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            except anthropic.AuthenticationError as exc:
                raise AssistantError("Anthropic did not accept the API key — check it in Settings.") from exc
            except anthropic.PermissionDeniedError as exc:
                raise AssistantError("This API key is not allowed to use the model.") from exc
            except anthropic.RateLimitError as exc:
                raise AssistantError("Too many requests just now — try again in a minute.") from exc
            except anthropic.BadRequestError as exc:
                raise AssistantError(f"The request was rejected: {exc.message}") from exc
            except anthropic.APIStatusError as exc:
                raise AssistantError(f"Anthropic's service had a problem ({exc.status_code}) — try again.") from exc
            except anthropic.APIConnectionError as exc:
                raise AssistantError("Could not reach Anthropic — check the internet connection.") from exc

            if response.stop_reason == "refusal":
                answer = "Sorry — I can't help with that one."
                break
            turn.append({"role": "assistant", "content": response.content})
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_calls:
                answer = "\n".join(b.text for b in response.content if b.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    answer += "\n\n(The answer was cut short.)"
                break
            results = []
            for call in tool_calls:
                tools_used.append(call.name)
                try:
                    content = self.run_tool(family_id, call.name, dict(call.input or {}), masks=masks)
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
                except Exception as exc:  # noqa: BLE001 - reported back to Claude as a tool error
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": f"Error: {exc}", "is_error": True})
            turn.append({"role": "user", "content": results})
        else:
            answer = "That needed more look-ups than I allow for one question — try asking it in parts."

        if response.stop_reason != "refusal":  # a declined question is left out of the conversation
            messages[:] = turn
        shown = masks.unmask(answer) if masks else answer
        return {"conversation_id": conversation_id, "answer": shown or "(no answer)",
                "tools_used": tools_used, "provider": "claude"}
