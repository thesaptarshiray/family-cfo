# Family Finance Tracker — V6: DDL Traceability Audit &amp; Deliverables

Companion document to `family-finance-app-v6-ddl.sql`. The DDL was generated
strictly from locked V2/V3/V4/V5/V5.1 decisions — no redesign, no new
features. **The DDL has been executed against a real SQLite database during
this review and creates all 36 tables cleanly with foreign keys enabled —
see §E.**

---

## Schema-to-spec traceability audit

Legend for "Write owner": **REPO** = Repository layer only (raw insert/update
of user-entered fields); **ENGINE** = Financial Engine only (derived/cache
fields, never user-editable).

### Core

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `family` | One row per family unit | All authoritative | PK | — | REPO |
| `family_member` | Household members | All authoritative | PK, FK→family | idx on family_id | REPO |

### Bulk import

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `bulk_import_batch` | Tracks a migration/import run | All authoritative | PK, status CHECK | — | REPO (batch lifecycle) |
| `bulk_import_error` | Rows that failed validation during import | All authoritative | PK, FK→batch (CASCADE) | idx on batch_id | REPO |

### Cash

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `bank_account` | One row per account | `current_balance` derived; rest authoritative | PK, FK→member | idx on member_id | REPO (authoritative fields) / **ENGINE** (`current_balance`) |
| `account_ownership` | Joint-account splits | All authoritative (row-level); sum=100 is app-level | Composite PK, pct CHECK | via PK | REPO, atomic with `bank_account` creation |
| `txn_category` | Hierarchical income/expense categories | All authoritative | PK, self-FK, group CHECK | idx on parent | REPO |

### Transactions

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `transaction` | The ledger — every cash movement | All authoritative (immutable once POSTED) | PK, 3 FKs, self-FK, 4 CHECKs (mutual exclusivity, transfer pairing) | 7 indexes (account+date, asset, liability, category, status, transfer_group, reversal, batch) | REPO — but only via Engine-orchestrated atomic operations (`create_transfer`, `create_redemption`, `execute_reversal`) for anything beyond a plain single-leg entry |
| `recurring_rule` | Scheduled generators for SIP/EMI/premium/income/expense | All authoritative | PK, 4 FKs, frequency/type CHECKs | idx on member, is_active | REPO; fires via Engine on schedule |

### Investment holdings

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `asset` | Generic parent for every holding | `current_value` derived; rest authoritative | PK, FK→member, category/status CHECK | idx on member, category | REPO / **ENGINE** (`current_value`) |
| `asset_ownership` | Joint-holding splits | Row-level authoritative; sum=100 app-level | Composite PK, pct CHECK | via PK | REPO, atomic with `asset` creation |
| `asset_valuation` | Dated market-value history | All authoritative (append-only) | PK, FK→asset (CASCADE), unique-per-date-unless-correction | idx on (asset,date), partial unique index | REPO |
| `mf_holding` | MF-specific fields | `units`, `avg_nav` derived from `investment_lot`; rest authoritative (`current_nav` manual in v1) | PK=FK→asset | — | REPO (authoritative fields) / **ENGINE** (`units`, `avg_nav`) |
| `stock_holding` | Stock-specific fields | `quantity`, `avg_buy_price` derived; rest authoritative | PK=FK→asset, exchange CHECK | — | REPO / **ENGINE** (derived fields) |
| `fd_holding` | FD-specific fields | All authoritative | PK=FK→asset, amount/date CHECKs | idx on maturity_date | REPO |
| `gold_holding` | Gold-specific fields | All authoritative | PK=FK→asset, form CHECK | — | REPO |
| `retirement_account` | EPF/PPF fields | All authoritative | PK=FK→asset, type CHECK | — | REPO |
| `nps_account` | NPS fields | All authoritative | PK=FK→asset, tier/allocation CHECKs | — | REPO |
| `property_holding` | Property fields | `total_acquisition_cost` derived; rest authoritative | PK=FK→asset, FK→liability | idx on linked_liability | REPO / **ENGINE** (`total_acquisition_cost`) |
| `investment_lot` | FIFO cost-basis lots | All authoritative (append-only; `units_remaining`/`status` Engine-maintained) | PK, FK→asset, FK→txn, units CHECK | idx on (asset,date), status, acq_txn | REPO for creation / **ENGINE** for `units_remaining`/`status` transitions |
| `lot_consumption` | Which lots a redemption drew from | All authoritative (append-only) | PK, FK→txn (redemption), FK→lot, FK→txn (reversal) | idx on txn, lot | REPO, via Engine-computed FIFO plan |

### Liabilities

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `liability` | Generic parent for every debt | `outstanding_amount` derived; rest authoritative | PK, FK→member, type/status CHECK | idx on member | REPO / **ENGINE** (`outstanding_amount`) |
| `liability_ownership` | Joint-loan splits | Row-level authoritative; sum=100 app-level | Composite PK, pct CHECK | via PK | REPO, atomic with `liability` creation |
| `loan_rate_change` | Floating-rate history | All authoritative (append-only, never reversed in v1) | PK, FK→liability | idx on (liability,date) | REPO |
| `loan_prepayment` | Prepayment events | All authoritative (append-only) | PK, FK→liability, FK→txn ×2, mutual-exclusivity CHECK | idx on (liability,date) | REPO |
| `loan_detail` | Amortizing-loan specifics | `interest_rate` derived from `loan_rate_change`; rest authoritative | PK=FK→liability, type/amount CHECKs | — | REPO / **ENGINE** (`interest_rate`) |
| `loan_payment` | Full schedule + actuals | `actual_principal_paid`/`actual_interest_paid` Engine-set; rest authoritative | PK, FK→liability, FK→txn, 2 self-domain FKs, 2 CHECKs | 4 indexes (liability+due, status, version, batch) | REPO / **ENGINE** (status transitions, actual split, regeneration) |

### Insurance

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `insurance_policy` | Policy master data | All authoritative | PK, FK→member, type/frequency/status CHECKs | idx on member | REPO |
| `insurance_premium_payment` | Premium due/paid schedule | All authoritative | PK, FK→policy, FK→txn, status CHECK | idx on (policy,due), batch | REPO |

### Goals

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `financial_goal` | Named savings goals | All authoritative | PK, FK→family, FK→member (nullable), type/status CHECKs | idx on family, member | REPO |
| `goal_allocation` | Which assets fund which goal | Row-level authoritative; sum/ceiling rules app-level | PK, FK→goal, FK→asset, pct/type CHECKs | idx on goal, asset | REPO, validated by Engine at write time |

### Audit / Scenarios

| Table | Purpose | Authoritative vs derived | Key constraints | Indexes | Write owner |
|---|---|---|---|---|---|
| `calculation_log` | Every computed number, ever | All Engine-authored, immutable once written | PK, FK→member (nullable), confidence CHECK | idx on (type,date), scope | **ENGINE only** |
| `calculation_input_ref` | Which rows fed a calculation | All Engine-authored; **not real FKs** (generic, deferred) | PK, FK→calc_id only | idx on calc_id, (table,row) | **ENGINE only** |
| `scenario` | Hypothetical scenario definitions | All authoritative | PK, `is_hypothetical` locked TRUE | — | REPO |
| `scenario_assumption` | Scenario input parameters | All authoritative | PK, FK→scenario | idx on scenario | REPO |
| `scenario_result` | Scenario outputs | All Engine-authored, immutable once written | PK, FK→scenario, FK→goal, FK→calc_log, confidence CHECK | idx on scenario, goal, based_on | **ENGINE only** |

---

## A. Complete SQLite DDL

The complete DDL is in the attached `family-finance-app-v6-ddl.sql` — 36
tables across 13 sections (Core → Bulk Import → Cash → Transactions →
Investment Holdings → Investment Lots → Loans → Insurance → Goals →
Audit/Scenarios), `PRAGMA foreign_keys = ON` at the top. **It has been
executed end-to-end against a live SQLite database as part of this review
(see §E) and creates all 36 tables with no errors.**

## B. Constraint/invariant mapping

| Invariant (from V2–V5.1) | DDL mechanism |
|---|---|
| Asset/liability affects one or the other, never both | `CHECK (asset_id IS NULL OR liability_id IS NULL)` on `transaction` |
| Every TRANSFER leg carries its pairing id, touches no asset/liability | Two `CHECK` constraints on `transaction` |
| One non-correction valuation per asset per date | Partial `UNIQUE INDEX ... WHERE is_correction = 0` on `asset_valuation` |
| `investment_lot.units_remaining` never exceeds `units`, never negative | `CHECK (units_remaining >= 0 AND units_remaining <= units)` |
| Prepayment restructure is tenure-reduction XOR EMI-reduction, not both | `CHECK` on `loan_prepayment` |
| A loan schedule row has at most one superseding event | `CHECK` on `loan_payment` |
| `PARTIAL` loan payments always carry their actual split | `CHECK` on `loan_payment` |
| Ownership/allocation percentages are valid per-row (0,100] | `CHECK` on every `*_ownership` and `goal_allocation` table |
| `scenario.is_hypothetical` is always TRUE | `CHECK (is_hypothetical = 1)` |
| Reversal can't reference itself | `CHECK` on `transaction.reversal_of_txn_id` |
| No physical delete of posted financial history | No `ON DELETE CASCADE` on any FK pointing *at* `transaction`, `loan_payment`, `asset_valuation`, etc. from the application's perspective — deletion is simply never issued by the Repository against `POSTED`/`PAID`/committed rows (this is procedural, see §C) |
| Import batch abort cleans up its own staged rows | `ON DELETE CASCADE` from `transaction`/`asset_valuation`/`loan_payment`/`insurance_premium_payment`/`bulk_import_error` to `bulk_import_batch` |

## C. Application-level invariants SQLite cannot enforce

These require Repository or Financial Engine logic — SQLite's constraint
system has no way to express them (mostly because they're cross-row
aggregates or cross-table business rules, not single-row/single-table
facts):

1. **Ownership percentages sum to exactly 100** per asset/liability/account
   across all rows in `asset_ownership`/`liability_ownership`/
   `account_ownership` — each row's own bound (0,100] is DDL-enforced; the
   sum is not.
2. **The primary holder (`member_id` on the parent table) always has a
   corresponding ownership row** — no cross-table CHECK possible.
3. **Asset/liability category-to-detail-table pairing** — nothing stops an
   `asset` row with `category='MF'` from having no `mf_holding` row, or the
   wrong one. Must be enforced by bundling parent + detail insert into one
   atomic Repository call.
4. **All Engine-only cache fields are recomputed from source, never
   delta-patched** (`current_value`, `outstanding_amount`,
   `current_balance`, `avg_nav`, `avg_buy_price`, `loan_detail
   .interest_rate`, `mf_holding.units`, `stock_holding.quantity`,
   `property_holding.total_acquisition_cost`) — SQLite has no column-level
   write permissions; this is enforced by the Repository simply not
   exposing an increment/decrement method for these columns, only a
   full-value "set" called exclusively by the Engine.
5. **FIFO lot consumption order and the over-redemption guard** — validating
   `SUM(units_remaining) >= requested_units` before any mutation, and
   consuming oldest-`acquisition_date`-first, is Engine logic run before the
   Repository executes the resulting plan.
6. **The full transaction reversal cascade** (Part 2 of the V5/V5.1 matrix)
   — which compensating transaction(s) to create, which `loan_payment`/lot
   rows to touch, and in what order — is entirely Engine-computed; the DDL
   only provides the columns (`status`, `reversal_of_txn_id`, lot/payment
   `REVERSED` states) that the result gets written into.
7. **LIFO-only reversal eligibility** for EMI/PARTIAL/prepayment (must be
   the most recent settled installment or liability event) — Engine-checked
   before any reversal executes.
8. **Partial-payment interest-first allocation** — the arithmetic that
   produces `actual_principal_paid`/`actual_interest_paid` is Engine logic;
   the DDL only requires (via CHECK) that both are populated when
   `status = 'PARTIAL'`.
9. **Goal allocation limits** — `HARD` allocations summing to ≤100% per
   asset, and an individual goal never exceeding the member's actual
   `asset_ownership` share — both cross-row/cross-table, Engine-validated at
   write time.
10. **As-of-date-spread warning** — comparing the `valuation_date`s of a
    calculation's inputs (via `calculation_input_ref` → `asset_valuation`)
    against a threshold is an Engine-side check, not a stored constraint.
11. **Bulk import batch state machine** (`STAGED → VALIDATED → COMMITTED`/
    `ABORTED`, rows only flip to `POSTED` on commit, one end-of-batch
    recompute) — the `status` CHECK constrains valid *values*, but the
    *transition logic and timing* are Repository/Engine-orchestrated.
12. **`calculation_input_ref` referential integrity** — `source_table`/
    `source_row_id` is not a real foreign key (can't FK to "one of several
    tables" in SQLite); validity of these references is entirely
    application-enforced. This is the accepted, explicitly deferred trade-off
    from V5/V5.1 — flagged again here for visibility, not reopened.
13. **`calc_type` values** — left as open-ended `TEXT` rather than a CHECK
    enum, since the Engine may add calculation types over time without a
    schema migration; validity of specific values is Engine-side convention,
    not DDL-enforced.

## D. Assumptions made

None of these change scope or reopen decisions — they're implementation
choices the prior design documents didn't pin down at the DDL level:

- **Surrogate keys use `INTEGER PRIMARY KEY AUTOINCREMENT`** everywhere, to
  guarantee an id is never reused even after a rare legitimate delete (e.g.
  an aborted import batch's cascaded rows) — relevant for a system built
  around audit trails.
- **Currency amounts:** `DECIMAL(18,2)`. **Units/quantities** (MF units,
  stock quantity, gold grams): `DECIMAL(18,4)`, to hold fractional units
  correctly. **Percentages:** `DECIMAL(5,2)`. SQLite itself only has NUMERIC
  affinity (no true fixed-point type) — these declarations document intent
  and should be paired with fixed-precision handling in the Engine/ORM
  layer to avoid float drift.
- **Dates/timestamps** use SQLite's `DATE`/`DATETIME` type-affinity
  convention (stored as ISO-8601 `TEXT` internally) rather than a native
  date type, since SQLite has none.
- **Booleans** (`is_active`, `is_correction`, `sip_enabled`,
  `is_hypothetical`) are `INTEGER` with a `CHECK (... IN (0,1))`, the
  standard SQLite convention.
- **A few enums were given explicit value sets where prior docs left them
  descriptive rather than literally enumerated:** `bank_account.account_type`
  (`SAVINGS`/`CURRENT`/`JOINT`, normalized to uppercase from the original
  "savings/current/joint" prose); `asset_valuation.source`
  (`AMFI`/`NSE`/`MANUAL`/`OTHER`, added `OTHER` as a fallback). Flagging
  these as literal encodings of already-agreed categories, not new
  categories.
- **`family_member.relationship` and `calculation_log.calc_type`** are left
  as free `TEXT` rather than CHECK-constrained enums, since both were
  described as open/extensible in prior docs (relationship types vary by
  family; calc types may grow as the Engine grows) — constraining them now
  would be scope the design docs didn't specify.
- **`ON DELETE` behavior**, not explicitly specified table-by-table in
  earlier docs, was assigned by consistent principle: `CASCADE` only where
  a child row is meaningless without its parent and the parent itself is
  expected to sometimes legitimately go away (`*_holding` detail tables →
  `asset`; ownership tables → their parent; anything → `bulk_import_batch`,
  matching the "abort deletes staged rows" rule); `RESTRICT` everywhere
  else, matching the "never delete posted financial history" principle by
  making it the database's default posture, not just an application
  promise.

## E. Final DDL consistency audit

- **Executed successfully** against a fresh SQLite database with
  `PRAGMA foreign_keys = ON`, via Python's `sqlite3` module, during this
  review. Result: all 36 tables created, zero errors.
- **Every V5.1 change is present:** `investment_lot.status` includes
  `REVERSED`; `loan_payment.status` includes `REVERSED`; `loan_payment`
  carries `actual_principal_paid`/`actual_interest_paid`; the
  recompute-from-source principle is documented via `-- ENGINE-DERIVED
  CACHE` comments on every affected column (`current_value`,
  `outstanding_amount`, `current_balance`, `avg_nav`, `avg_buy_price`,
  `loan_detail.interest_rate`, `mf_holding.units`, `stock_holding.quantity`,
  `property_holding.total_acquisition_cost`).
- **Every V5 addition is present:** `bulk_import_error`; `import_batch_id`
  on `loan_payment` and `insurance_premium_payment`; `reversed_by_txn_id` on
  `lot_consumption` and `loan_prepayment`.
- **Every V4 addition is present:** `calculation_log.confidence_level`;
  `scenario_result.confidence_level`; `bulk_import_batch`; `import_batch_id`
  on `transaction`/`asset_valuation`.
- **Every V3 addition is present:** `transfer_group_id`/`status`/
  `reversal_of_txn_id`/`data_confidence` on `transaction`;
  `asset_valuation.data_confidence`/`is_correction`; `loan_rate_change`;
  `investment_lot`/`lot_consumption`; `scenario_result.based_on_calc_id`;
  `goal_allocation.allocation_type`.
- **Explicitly confirmed absent, as required:** no typed
  `calculation_input_ref` tables (still generic); no reversal/correction
  field on `loan_rate_change` (RATE_CHANGE reversal stays out of scope —
  correctly has no `reversed_by_txn_id` or similar).
- **Naming note:** `transaction` is a SQL reserved word in some engines; the
  DDL quotes it (`"transaction"`) everywhere it's referenced, including in
  every foreign key pointing at it, so this is handled correctly throughout.

## F. Confirmation: no architectural changes beyond V5.1

Confirmed. Every table, column, and constraint in this DDL traces directly
to a decision locked in V2 through V5.1 — each new column added since V2 is
commented in the DDL with which version introduced it. The only additions
beyond the prior documents are the implementation-level choices listed in
§D (key type, precision, date handling, `ON DELETE` behavior, and literal
enum encodings of already-agreed categories) — none of these change what
the system does, only how SQLite is asked to store and constrain it. No
table was added, removed, merged, or split; no invariant was strengthened,
weakened, or newly introduced beyond what V5.1 already specified.
