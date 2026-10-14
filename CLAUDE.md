# Family CFO — desktop app

A personal family-finance app for Indian households: a local Python server
(`http://127.0.0.1:8000`) plus a single-page dashboard in the browser.
Everything runs on the user's own Windows laptop; data never leaves it
except for the deliberate outbound calls listed under Privacy.

This folder is the **desktop app only**. The Android app is a separate
project in `..\..\family-cfo-android` with its own `CLAUDE.md`. Do not edit
it from here, and do not change this app for the Android app's sake.

## Run and test

- Start: `.venv\Scripts\python.exe run_dashboard.py`. This opens the browser;
  `--no-browser` skips that. The desktop shortcut "Family CFO" runs the same
  command, minimised. If the app is already running, it just opens the browser.
- Restart after code changes: stop whatever is listening on port 8000, then
  start it again, minimised:
  `Start-Process .venv\Scripts\python.exe -ArgumentList run_dashboard.py,--no-browser -WindowStyle Minimized`.
  Check it with `GET /api/auth/status`.
- Tests: `.venv\Scripts\python.exe -m pytest -q`. There are about 570 tests,
  taking about 5 minutes. Run the targeted files while iterating and the full
  suite before reporting work done.
- The venv runs Python 3.14. Dependencies are `pypdf` (CAS PDFs) and
  `anthropic` (optional Claude chat).
- Batch (`.bat`) files cannot be executed from the agent's shell here. Call
  `java` or `python` directly instead.

## Architecture (enforced by `tests/architecture`)

`repository` (SQL only) → `financial_engine` (business rules) → `api`
(stdlib `http.server`, JSON) → `src/api/dashboard.html` (one page, vanilla JS).

- **repository/**: SQLite access only, with no business rules and no
  aggregates in SQL. ENGINE-DERIVED CACHE columns are written only through
  `DerivedCacheRepository`.
- **financial_engine/**: money is `Decimal`; float arithmetic is
  forbidden, and a test checks for it. No SQL here.
  - `FinancialEngine` is in `engine.py`. CAS import is a mixin in
    `statement_import.py`.
  - The pure modules are `xirr`, `loan_schedule`, `fd_valuation`,
    `insurance_schedule`, `surrender_value`, `health`, `policy_review` and
    `reminders`.
- **api/**: imports only `financial_engine` and the stdlib (plus
  `anthropic`, lazily). Never import `repository` or `sqlite3` here.
  - `server.py` holds the routes; its module docstring lists every route.
  - Other modules: `cas_parser.py` (NSDL plus CAMS/KFintech CAS),
    `amfi_sync.py` and `stock_price_sync.py` (daily prices),
    `assistant.py` (chat), `notifications.py` (settings and Gmail reminders),
    `auth.py`.
- `UnitOfWork` does not nest. Multi-step writes call repositories inside one
  uow.

## Schema changes

- Add `schema/migrations/NNN_name.sql`. The latest is `015`. Also fold the
  change into `schema/family-finance-app-v6-ddl.sql`, so a fresh database
  starts at the latest version.
- Add an "undo" for it, newest first, in
  `tests/repository/test_schema.py::_pre_migration_ddl`.
- SQLite cannot widen a CHECK in place. Rebuild the table instead; see 013.
- `run_dashboard.py` backs up the database as `family_finance.db.backup-vN`
  before migrating.
- Before migrating real data, try the migration on a copy of
  `family_finance.db`.

## Privacy rules (important)

- Never commit or package user data. That covers `family_finance.db*`,
  `family_finance_*.json` (auth, settings with API keys and Gmail app
  password, reminders, sync state), and any CAS or statement PDF or Excel
  file in this folder.
- Tests use made-up data only, e.g. "ASHA MEHTA" or "Sharma". Never put real
  names, folios or account numbers in code, tests or comments.
- Outbound calls:
  - AMFI NAV file and NSE/BSE bhavcopies (daily);
  - Groq or Anthropic, only when the user asks the chat a question;
  - Gmail SMTP, for reminders.
- The chat strips PAN, account, folio, FD and policy numbers before sending
  (`assistant.scrub`). The optional privacy mode also aliases people and
  institutions.
- Commit or push only when the user asks. Nothing has been committed since
  `232fd23`.

## Features (all built)

- Overview, with net worth by asset class. Savings-type insurance counts at
  surrender value plus bonus, which is the user's choice.
- **Mutual Funds**:
  - CAS import: NSDL and CAMS/KFintech, idempotent, reconciles each month.
  - SIP running, SIP stopped and lumpsum, with sub-pages.
  - Broker filter.
  - "Details needed" purchase dating for XIRR.
  - Per-fund Edit (details, today's figures, purchases).
- Stocks (NSE/BSE prices, sectors and risk), FDs, PF/EPF, Gold, NPS.
- **Insurance**:
  - plan review with pros and cons;
  - surrender calculator with a bonus field;
  - premium-paying term (limited pay).
- **Loans**: amortization, prepayments, rate changes, what-if.
- **Monthly Cash Flow**:
  - bank accounts;
  - premium ticks;
  - one-time (ad hoc) rows that count only in their own month.
- **Financial Health**: a personal plan (`financial_engine/planning.py`, `GET /plan`).
  - Area scores: Protection, Emergency, Goals, Growth, Loans, Savings.
  - Goals: the V6 `financial_goal` table plus `inflation_pct`. Holdings are
    put towards a goal via `goal_allocation`.
  - Earmarks: EMERGENCY / INCOME / KEEP, in `asset_earmark`. Earmarked money
    is never suggested for moving.
  - Preferences: tax slab, emergency months, minimum FD, no direct stocks.
  - Funding plan ("How to fund your goals"): emergency first, then own money,
    salary SIP, and "only if needed" members (`planning_member_policy`, 015)
    as a backup; Apply assigns own holdings.
  - "Smart moves" with keys, which can be dismissed as "Not for me".
  - Also: the general check-up, projection, members' ages and life stage,
    heirs, and a succession checklist.
- **Reminders & Settings**:
  - Gmail reminders for FD maturities and premiums, sent at T-15 and T-3,
    once per window, when the app is opened (not for SIPs);
  - "Ask Family CFO" chat, using Groq (free, default) or Claude.

## Packaging for friends

`packaging/` holds `Setup Family CFO.bat` and `START HERE.txt`. The share zip
(`..\..\Family-CFO-share.zip`) contains those two files, `run_dashboard.py`,
`pyproject.toml`, `src/` and `schema/`, with no data files. Audit the zip for
personal data before handing it over.

## Working with the user

- The user is not a programmer. Explain in plain words, briefly, with the
  steps to do.
- Show money in Indian style: ₹12,34,567, or ₹12.35 L / ₹1.20 Cr for big
  figures.
- Advice pages carry "a guide, not personal financial advice".
- Verify UI changes on a **copy** of the real database, e.g. with Playwright
  and the installed Edge (`channel="msedge"`).
- Never point anything at the live `family_finance.db` while testing.

## Open items

- Tax page: proposed. Waiting for the user's unprotected ITR-V
  acknowledgement sample PDF.
- No Groq key or Gmail app password has been entered yet, so the chat and
  emails have not been tried live.
- Redemptions and switch-outs are not recorded yet. The CAS preview lists
  them for manual entry.
