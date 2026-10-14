# Family CFO

A local-first personal finance platform for Indian families: investments, insurance, loans and
monthly cash flow for every family member in one place, with financial health scores, goal
planning and an AI assistant that never sees your sensitive identifiers.

![Family CFO demo dashboard](images/family-cfo-demo.png)

*Screenshots use made-up demo data.*

## Engineering highlights

- **Layered architecture, enforced by tests.** `repository → financial_engine → api → dashboard`.
  Architecture tests in `tests/architecture` fail the build if a layer reaches past its
  neighbour, or if the financial engine uses float arithmetic (all money is `Decimal`).
- **Privacy-preserving LLM assistant.** "Ask Family CFO" works with Groq or Claude. PAN, account,
  folio, FD and policy numbers are never sent; an optional privacy mode also masks people's and
  banks' names. Users are guided to enable zero data retention.
- **Local-first by design.** A stdlib HTTP server and SQLite on the user's own machine. The app
  goes online only for daily price files (AMFI NAVs, NSE/BSE closes), the chosen chat service,
  and Gmail reminders.
- **Safe schema evolution.** Numbered SQL migrations upgrade existing databases automatically on
  startup, after an automatic backup.
- **Real-world data ingestion.** A parser for NSDL and CAMS/KFintech CAS statements imports SIPs,
  lumpsums and purchases on their real dates; returns are computed with XIRR.
- **Tested with synthetic data only.** A pytest suite covers the repository, engine and
  architecture rules; no real financial data is ever committed.

**Tech:** Python 3.10+, SQLite, standard-library HTTP server, vanilla JavaScript, pytest,
pypdf, Groq / Anthropic APIs.

**How it was built:** I designed the architecture, data model and engineering rules, and used
Claude Code to implement them in small, tested steps (working notes in `CLAUDE.md`).

---
