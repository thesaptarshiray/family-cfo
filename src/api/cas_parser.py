"""
Read an NSDL consolidated account statement (CAS) PDF into a
`CasStatement` — the Engine decides what to do with it.

The PDF must have its password removed (open it in Chrome or Edge, enter
the password, Print -> Save as PDF). A PDF made with "Microsoft Print to
PDF" is only pictures of the pages and has no text to read; that is
reported as an error, never guessed at.

`parse_cas_text(pages)` does the reading from the pages' text and needs no
PDF at all (that is what the tests use); `parse_cas_pdf(data)` extracts the
text with pypdf first.

What is read, section by section:
  - the statement period and the holder's name (page 2);
  - "Mutual Fund Folios (F)": ISIN, folio, scheme, units, average cost,
    total cost, NAV, value; the fund house of each folio comes from the
    "Folio No. / AMC NAME" list near the end;
  - each demat account (NSDL or CDSL): its broker (DP), and its equities,
    fund units and Sovereign Gold Bonds;
  - the life policies in the e-insurance account;
  - the folio transactions of the month ("Mutual Funds Transaction
    Statement").
Other sections (bonds, government securities, ...) are not read.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

from financial_engine.cas_statement import (
    CasDematAccount,
    CasDematMf,
    CasEquity,
    CasMfFolio,
    CasMfTransaction,
    CasPolicy,
    CasSgb,
    CasStatement,
)

UPLOAD_HELP = (
    "Upload the CAS PDF without its password: open it in Chrome or Edge, enter the password, "
    "then Print → Save as PDF. Don't use \"Microsoft Print to PDF\" — it turns the pages into "
    "pictures the app can't read."
)

_ISIN = re.compile(r"^(IN[A-Z0-9]{10})\b")
_NUMBER = re.compile(r"^\(?-?[\d,]*\d(?:\.\d+)?\)?$")
_DATE = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4}$")
_PAGE_NOISE = (
    re.compile(r"^Consolidated Account Statement$"),
    re.compile(r"^Summary Holdings Transactions Your Account About NSDL$"),
    re.compile(r"^National Securities Depository Limited \d+$"),
)
_SECTION = re.compile(r"^([A-Za-z ]+?) \((E|M|SGB|F|P|C|Z|I|S|G|O|A|SD|SF|N)\)$")
_FREQUENCIES = ("Half-Yearly", "Half Yearly", "Monthly", "Quarterly", "Yearly", "Annually", "Annual", "Single")


class CasParseError(ValueError):
    """The file is not a readable NSDL CAS."""


def _num(token: str) -> Decimal:
    text = token.replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    try:
        value = Decimal(text.strip("()"))
    except InvalidOperation as exc:
        raise CasParseError(f"not a number: {token}") from exc
    return -value if negative else value


def _date(token: str) -> date:
    return datetime.strptime(token, "%d-%b-%Y").date()


def _split_tail(text: str) -> Tuple[List[str], List[str]]:
    """(words, trailing numbers) of a line."""
    tokens = text.split()
    tail: List[str] = []
    while tokens and _NUMBER.match(tokens[-1]) and not _DATE.match(tokens[-1]):
        tail.insert(0, tokens.pop())
    return tokens, tail


# Since September 2026 the NSDL CAS is bilingual: each Hindi heading is
# printed right after the English one, often on the same text line
# ("Mutual Fund Folios (F)म्यूचुअल फंड फोलियो (F)"). The Hindi (Devanagari,
# plus the "�" the PDF extracts for some of its glyphs) is removed, and a
# section code left doubled by it ("(F) (F)") is kept once.
_HINDI = re.compile(r"[ऀ-ॿ�​-‍◌]+")
_DOUBLED_CODE = re.compile(r"(\([A-Z]+\))(?:\s*\1)+$")


def _clean_lines(pages: Sequence[str]) -> List[str]:
    lines: List[str] = []
    for page in pages:
        for raw in (page or "").splitlines():
            line = _HINDI.sub(" ", raw.replace("`", " "))
            line = re.sub(r"\s+", " ", line).strip()
            line = _DOUBLED_CODE.sub(r"\1", line)
            if line and not any(p.match(line) for p in _PAGE_NOISE):
                lines.append(line)
    return lines


def _squash(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _strip_holder(text: str, holder: str) -> str:
    """"HDFC BANK LIMITED ASHA MEHTA" -> "HDFC BANK LIMITED"."""
    words = holder.split()
    if not words:
        return text.strip()
    pattern = r"\s*(?:MR|MRS|MS|DR)?\.?\s*" + r"\s+".join(map(re.escape, words)) + r"[\s.]*$"
    return re.sub(pattern, "", text.strip(), flags=re.IGNORECASE).strip()


# ----------------------------------------------------------------------
# Holdings
# ----------------------------------------------------------------------


def _parse_holdings(lines: List[str], holder: str):
    folios: List[Tuple[str, str, str, List[str]]] = []  # isin, ucc words..., tail
    demat_mfs: List[CasDematMf] = []
    equities: List[CasEquity] = []
    sgbs: List[CasSgb] = []
    raw_folios: List[str] = []
    skipped: List[str] = []

    start = next((i for i, l in enumerate(lines) if l.startswith("PORTFOLIO COMPOSITION")), None)
    if start is None:
        raise CasParseError("no holdings found — is this an NSDL CAS?")
    end = next(
        (i for i, l in enumerate(lines) if i > start and (
            l.startswith("NSDL NATIONAL INSURANCE REPOSITORY") or l == "Transactions"
            or l.startswith("Mutual Funds Transaction Statement"))),
        len(lines),
    )

    account: Optional[CasDematAccount] = None
    section: Optional[str] = None
    record: List[str] = []
    i = start
    pending_account: Optional[Tuple[str, List[str]]] = None

    def flush() -> None:
        nonlocal record
        if record:
            text = " ".join(record)
            if section == "F":
                raw_folios.append(text)
            elif section in ("E", "M", "SGB") and account is not None:
                _parse_demat_record(text, section, account, demat_mfs, equities, sgbs)
            elif section is not None:
                skipped.append(text)
        record = []

    while i < end:
        line = lines[i]
        i += 1
        header = re.match(r"^(NSDL|CDSL) Demat Account ACCOUNT HOLDER$", line)
        if header:
            flush()
            pending_account = (header.group(1), [])
            section = None
            continue
        if pending_account is not None:
            depository, parts = pending_account
            dp_ids = re.search(r"DP ID\s*:\s*(\S+)\s+Client ID\s*:\s*(\S+)", line)
            if dp_ids:
                dp_text = " ".join(parts)
                dp_text = dp_text.split("(PAN:")[0]
                account = CasDematAccount(
                    depository=depository, dp_name=_strip_holder(dp_text, holder),
                    dp_id=dp_ids.group(1), client_id=dp_ids.group(2),
                )
                pending_account = None
            else:
                parts.append(line)
            continue
        sec = _SECTION.match(line)
        if sec:
            flush()
            section = sec.group(2)
            if section == "F":
                account = None
            continue
        if line in ("Equity Shares",) or line.startswith(("Sub Total", "Total", "ISIN")):
            flush()
            continue
        if _ISIN.match(line):
            flush()
            record = [line]
        elif record:
            record.append(line)
        else:
            continue
        # a record is complete once a line ends in two or more numbers
        _, tail = _split_tail(line)
        if len(tail) >= 2:
            flush()
    flush()
    return raw_folios, demat_mfs, equities, sgbs, skipped


def _parse_demat_record(text, section, account, demat_mfs, equities, sgbs) -> None:
    words, tail = _split_tail(text)
    isin = words[0]
    rest = words[1:]
    if section == "SGB":
        # ISIN ISSUER... coupon maturity units face market value
        date_idx = next((k for k, w in enumerate(rest) if _DATE.match(w)), None)
        if date_idx is None or len(tail) < 4:
            raise CasParseError(f"could not read the gold bond row: {text}")
        coupon = _num(rest[date_idx - 1])
        sgbs.append(CasSgb(
            account=account, isin=isin, coupon_rate=coupon, maturity_date=_date(rest[date_idx]),
            units=_num(tail[-4]), issue_price=_num(tail[-3]), market_price=_num(tail[-2]),
            value=_num(tail[-1]),
        ))
        return
    if account.depository == "NSDL":
        if section == "E":
            # ISIN SYMBOL.EXCH COMPANY... face qty price value
            symbol, exchange = None, None
            if rest and "." in rest[0]:
                symbol, exchange = rest[0].rsplit(".", 1)
                rest = rest[1:]
            if len(tail) < 4:
                raise CasParseError(f"could not read the share row: {text}")
            equities.append(CasEquity(
                account=account, isin=isin, company=_squash(" ".join(rest)),
                quantity=_num(tail[-3]), price=_num(tail[-2]), value=_num(tail[-1]),
                symbol=symbol, exchange=exchange if exchange in ("NSE", "BSE") else None,
            ))
        else:  # M: ISIN DESCRIPTION units nav value
            demat_mfs.append(CasDematMf(
                account=account, isin=isin, name=_squash(" ".join(rest)),
                units=_num(tail[-3]), nav=_num(tail[-2]), value=_num(tail[-1]),
            ))
        return
    # CDSL: ISIN NAME current free lent safekeep locked pledge-setup pledged earmarked pledgee price value
    name = _squash(re.sub(r"#?\s*EQUITY SHARES$", "", " ".join(rest)).replace("#", ""))
    if len(tail) < 3:
        raise CasParseError(f"could not read the demat row: {text}")
    current = _num(tail[0])
    price, value = _num(tail[-2]), _num(tail[-1])
    if section == "E":
        equities.append(CasEquity(account=account, isin=isin, company=name, quantity=current,
                                  price=price, value=value))
    else:
        demat_mfs.append(CasDematMf(account=account, isin=isin, name=name, units=current,
                                    nav=price, value=value))


def _parse_folio(text: str, amcs: Dict[str, str]) -> CasMfFolio:
    words, tail = _split_tail(text)
    if len(tail) < 7:
        raise CasParseError(f"could not read the fund row: {text}")
    folio = tail[-7]
    units, avg, cost, nav, value = (_num(t) for t in tail[-6:-1])
    isin = words[0]
    name_words = words[1:] + tail[:-7]
    if name_words[:2] == ["NOT", "AVAILABLE"]:
        name_words = name_words[2:]
    elif name_words and re.match(r"^(?=.*\d)[A-Z0-9]{6,}$", name_words[0]):
        name_words = name_words[1:]  # the exchange UCC, e.g. MFDSPM0009
    return CasMfFolio(
        isin=isin, folio=folio, scheme_name=_squash(" ".join(name_words)), units=units,
        avg_cost=avg, total_cost=cost, nav=nav, value=value, amc=amcs.get(folio),
    )


def _parse_amcs(lines: List[str]) -> Dict[str, str]:
    """Folio -> fund house, from the "Folio No. / AMC NAME" list."""
    amcs: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        if lines[i] == "Folio No." and i + 1 < len(lines) and lines[i + 1] == "AMC NAME":
            i += 2
            while i < len(lines) and not lines[i].startswith(("NSDL NATIONAL INSURANCE", "Load structure", "Folio No.")):
                if re.match(r"^[0-9][0-9/ ]*$", lines[i]):
                    folio = lines[i].replace(" ", "")
                    j, name = i + 1, []
                    while j < len(lines) and not lines[j].startswith(("Sole Holder", "Joint", "First Holder")):
                        name.append(lines[j])
                        j += 1
                    if name:
                        amcs[folio] = _squash(" ".join(name))
                    i = j
                i += 1
            continue
        i += 1
    return amcs


# ----------------------------------------------------------------------
# Insurance
# ----------------------------------------------------------------------


def _parse_policies(lines: List[str], holder: str) -> List[CasPolicy]:
    try:
        start = lines.index("LIFE INSURANCE POLICIES")
    except ValueError:
        return []
    i = start + 1
    while i < len(lines) and not lines[i].startswith("Installment"):
        i += 1
    while i < len(lines) and not lines[i].endswith(("Assured", "Assured `")):
        i += 1
    i += 1
    freq = "|".join(re.escape(f) for f in _FREQUENCIES)
    end_line = re.compile(rf"^(.*?)\s([\d,]+\.\d{{2}})\s({freq})\s([\d,]+\.\d{{2}})$", re.IGNORECASE)
    policies: List[CasPolicy] = []
    buffer: List[str] = []
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith(("Total", "Note:")):
            break
        match = end_line.match(line)
        if not match:
            buffer.append(line)
            continue
        head_words = (" ".join(buffer) + " " + match.group(1)).split()
        buffer = []
        # the policy number is the last word with a digit; the status follows it
        idx = max((k for k, w in enumerate(head_words) if re.search(r"\d", w)), default=None)
        if idx is None:
            continue
        before = " ".join(head_words[:idx])
        status = " ".join(head_words[idx + 1:])
        cut = re.search(r"\bLimited\b", before, re.IGNORECASE)
        if cut:
            people_and_insurer, plan = before[:cut.end()], before[cut.end():]
        else:
            people_and_insurer, plan = before, ""
        insurer = _strip_holder_prefix(people_and_insurer, holder)
        assured = people_and_insurer[: len(people_and_insurer) - len(insurer)].strip() or holder
        policies.append(CasPolicy(
            life_assured=_squash(assured), insurer=_squash(insurer), plan_name=_squash(plan),
            policy_number=head_words[idx], status=_squash(status), premium=_num(match.group(2)),
            frequency=match.group(3), sum_assured=_num(match.group(4)),
        ))
    return policies


def _strip_holder_prefix(text: str, holder: str) -> str:
    words = holder.split()
    pattern = r"^\s*(?:MR|MRS|MS|DR)?\.?\s*" + r"\s+".join(map(re.escape, words)) + r"\s+"
    stripped = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return stripped.strip()


# ----------------------------------------------------------------------
# Folio transactions
# ----------------------------------------------------------------------


def _parse_mf_transactions(lines: List[str]) -> List[CasMfTransaction]:
    start = next((i for i, l in enumerate(lines) if l.startswith("Mutual Funds Transaction Statement")), None)
    if start is None:
        return []
    txns: List[CasMfTransaction] = []
    isin = folio = None
    header: List[str] = []
    for line in lines[start + 1:]:
        if line.startswith("***End of Statement"):
            break
        if line.startswith("ISIN :") or header:
            header.append(line)
            joined = " ".join(header)
            found = re.search(r"Folio-no\s*-\s*(\S+)", joined)
            if found:
                isin = re.search(r"ISIN\s*:\s*(IN[A-Z0-9]{10})", joined).group(1)
                folio = found.group(1)
                header = []
            continue
        first = line.split(" ", 1)[0]
        if not _DATE.match(first) or isin is None:
            continue
        words, tail = _split_tail(line[len(first):].strip())
        if len(tail) == 5:
            amount, stamp, nav, _price, units = tail
        elif len(tail) == 4:
            amount, nav, _price, units = tail
            stamp = "0"
        else:
            continue
        txns.append(CasMfTransaction(
            isin=isin, folio=folio, txn_date=_date(first), description=" ".join(words),
            amount=abs(_num(amount)), stamp_duty=_num(stamp), nav=_num(nav), units=_num(units),
        ))
    return txns


# ----------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------


def parse_cas_text(pages: Sequence[str]) -> CasStatement:
    lines = _clean_lines(pages)
    if not lines:
        raise CasParseError(
            "This PDF has no readable text — it was probably saved with \"Microsoft Print to PDF\". "
            + UPLOAD_HELP
        )
    joined = "\n".join(lines)
    if "Mutual Fund Consolidated Account Statement" in joined and "Summary of Holdings" in joined:
        return _parse_mf_cas(lines, joined)
    period = re.search(r"Statement for the period from (\d{2}-\w{3}-\d{4}) to (\d{2}-\w{3}-\d{4})", joined)
    if not period or "NSDL" not in joined:
        raise CasParseError(
            "This does not look like an NSDL CAS or a CAMS / KFintech mutual fund CAS."
        )
    holder_idx = next((i for i, l in enumerate(lines) if l.startswith("NSDL ID:")), None)
    holder = _squash(lines[holder_idx + 1]) if holder_idx is not None else ""

    raw_folios, demat_mfs, equities, sgbs, _skipped = _parse_holdings(lines, holder)
    amcs = _parse_amcs(lines)
    return CasStatement(
        source="NSDL CAS",
        period_from=_date(period.group(1)),
        period_to=_date(period.group(2)),
        holder_name=holder,
        mf_folios=tuple(_parse_folio(text, amcs) for text in raw_folios),
        mf_transactions=tuple(_parse_mf_transactions(lines)),
        demat_mfs=tuple(demat_mfs),
        equities=tuple(equities),
        sgbs=tuple(sgbs),
        policies=tuple(_parse_policies(lines, holder)),
    )


# ----------------------------------------------------------------------
# CAMS / KFintech (MF Central) mutual fund CAS
# ----------------------------------------------------------------------
#
# Mutual funds only, by folio. "Summary of Holdings" gives each folio's
# units, NAV, cost and market value; "Transaction Details" gives the month's
# transactions, whose units / NAV / price come out of the PDF run together
# ("46.870170.6774170.6774Sys. Investment04-Aug-2026 7,999.60") and are
# separated by their fixed decimals (3, 4, 4). Amounts are net of the 0.005%
# stamp duty, so a purchase's gross amount is grossed back up.

_STAMP_DUTY = Decimal("0.00005")
_MF_CAS_NOISE = re.compile(
    r"^(Version-\d+|Page \d+ of \d+|Summary of Holdings as on .*|Folio No Scheme Code.*|NameISIN Unit Balance.*|"
    r"Transaction Details|Guardian|Holder +Name.*|Primary Holder.*|2nd Holder.*|3rd Holder.*|.*Unique Client Code.*|"
    r"Mode of Holding.*|UnitsTransactionDate.*)$"
)
_HOLDING_LINE = re.compile(
    r"^(IN[A-Z0-9]{10}) ([\d,]+\.\d+) ([\d,]+\.\d+) ([\d,]+\.\d{2}) (-?[\d.]+)% ([\d,]+\.\d{2})$"
)
_TXN = re.compile(
    r"^(-?[\d,]+?\.\d{3})(-?[\d,]+?\.\d{4})(-?[\d,]+?\.\d{4})(.*?)(\d{2}-[A-Za-z]{3}-\d{4}) "
    r"(\(?-?[\d,]+\.\d{2}\)?)$"
)


def _parse_mf_cas(lines: List[str], joined: str) -> CasStatement:
    period = re.search(r"during the period (\d{2}-\w{3}-\d{4}) to (\d{2}-\w{3}-\d{4})", joined)
    if not period:
        raise CasParseError("Could not find the statement period in this mutual fund CAS.")
    holder = next((_squash(l.split(" SEBI ")[0]) for l in lines if " SEBI has mandated" in l), "")

    folios: List[CasMfFolio] = []
    start = next(i for i, l in enumerate(lines) if l.startswith("Summary of Holdings"))
    end = next((i for i, l in enumerate(lines) if i > start and l.startswith("Portfolio Value")), len(lines))
    amc = None
    record: List[str] = []
    for line in lines[start + 1:end]:
        if _MF_CAS_NOISE.match(line):
            continue
        if line.endswith(" - Total") or re.search(r" - Total [\d,]+\.\d{2}$", line):
            amc, record = None, []
            continue
        held = _HOLDING_LINE.match(line)
        if held and record:
            first = record[0].split(" ", 1)
            folio = first[0]
            name = _squash(" ".join([first[1] if len(first) > 1 else ""] + record[1:]))
            name = re.sub(r"^[A-Z0-9]+-", "", name, count=1)  # the RTA's scheme code, e.g. "NIGP-"
            folios.append(CasMfFolio(
                isin=held.group(1), folio=folio, scheme_name=name, units=_num(held.group(2)),
                avg_cost=(_num(held.group(4)) / _num(held.group(2))).quantize(Decimal("0.0001"))
                if _num(held.group(2)) else Decimal("0"),
                total_cost=_num(held.group(4)), nav=_num(held.group(3)), value=_num(held.group(6)),
                amc=amc,
            ))
            record = []
            continue
        if re.match(r"^\d[\d/]*\s+\S", line):  # a folio number starts a holding
            record = [line]
        elif record:
            record.append(line)
        elif not re.search(r"\d", line):
            amc = _squash(line)

    txns: List[CasMfTransaction] = []
    t_start = next((i for i, l in enumerate(lines) if l == "Transaction Details" and i > end), None)
    if t_start is not None:
        folio = isin = None
        buffer: List[str] = []
        for line in lines[t_start + 1:]:
            if line.startswith("Load Structures"):
                break
            got = re.match(r"^Folio No\. : (\S+)", line)
            if got:
                folio, buffer = got.group(1), []
                continue
            got = re.match(r"^ISIN :\s*(IN[A-Z0-9]{10})", line)
            if got:
                isin = got.group(1)
                continue
            if "Opening Balance" in line or "Closing Balance" in line:
                buffer = []
                continue
            if buffer or re.match(r"^-?[\d,]+\.\d{3}-?[\d,]+\.\d{4}", line):
                buffer.append(line)
                found = _TXN.match(" ".join(buffer))
                if found and folio and isin:
                    units = _num(found.group(1))
                    amount = _num(found.group(6))
                    gross = (abs(amount) / (Decimal("1") - _STAMP_DUTY)).quantize(Decimal("0.01")) if units > 0 else abs(amount)
                    txns.append(CasMfTransaction(
                        isin=isin, folio=folio, txn_date=_date(found.group(5)),
                        description=_squash(found.group(4)), amount=abs(amount),
                        stamp_duty=gross - abs(amount), nav=_num(found.group(2)), units=units,
                    ))
                    buffer = []
    return CasStatement(
        source="CAMS / KFintech CAS",
        period_from=_date(period.group(1)), period_to=_date(period.group(2)),
        holder_name=holder, mf_folios=tuple(folios), mf_transactions=tuple(txns),
    )


def extract_pdf_pages(data: bytes) -> List[str]:
    """The text of each page (pypdf). Raises `CasParseError` for a file that
    is not a PDF or is still password-protected."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise CasParseError("The PDF reader (pypdf) is not installed: run pip install pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, ValueError, OSError) as exc:
        raise CasParseError(f"Not a PDF the app can open ({exc}).") from exc
    if reader.is_encrypted:
        raise CasParseError("This PDF still has its password. " + UPLOAD_HELP)
    return [page.extract_text() or "" for page in reader.pages]


def parse_cas_pdf(data: bytes) -> CasStatement:
    return parse_cas_text(extract_pdf_pages(data))
