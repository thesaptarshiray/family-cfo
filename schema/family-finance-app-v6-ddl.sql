-- ============================================================================
-- Family Finance Tracker — SQLite DDL (V6, + one V6.1 addition)
-- Generated strictly from locked V2 / V3 / V4 / V5 / V5.1 decisions.
-- No redesign. No new features. typed calculation_input_ref and RATE_CHANGE
-- reversal remain explicitly out of scope (see the companion audit doc).
-- V6.1: one table added after V6 was locked — mf_transaction (Section 8B),
-- explicitly approved when building the MF page. See its own comment there.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ============================================================================
-- SECTION 1: CORE
-- ============================================================================

CREATE TABLE family (
    family_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    family_name     TEXT NOT NULL,
    base_currency   TEXT NOT NULL DEFAULT 'INR'
);

CREATE TABLE family_member (
    member_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id       INTEGER NOT NULL REFERENCES family(family_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    name            TEXT NOT NULL,
    relationship    TEXT NOT NULL,            -- free text: self/spouse/child/parent/... (not enum-constrained, see Assumptions)
    date_of_birth   DATE,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    risk_tolerance  TEXT CHECK (risk_tolerance IN ('CONSERVATIVE','MODERATE','AGGRESSIVE')),  -- migration 007
    life_stage      TEXT CHECK (life_stage IN ('EARNING','RETIRED','DEPENDENT')),
    heir_member_id  INTEGER REFERENCES family_member(member_id) ON DELETE SET NULL ON UPDATE CASCADE,
    retirement_age  INTEGER CHECK (retirement_age IS NULL OR retirement_age BETWEEN 40 AND 80)  -- migration 011
);
CREATE INDEX idx_family_member_family ON family_member(family_id);

-- ============================================================================
-- SECTION 2: BULK IMPORT (created early — referenced by transaction/valuation/
-- loan_payment/insurance_premium_payment)
-- ============================================================================

CREATE TABLE bulk_import_batch (
    batch_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_description  TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('STAGED','VALIDATED','COMMITTED','ABORTED')),
    started_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at        DATETIME,
    row_count           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE bulk_import_error (
    error_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES bulk_import_batch(batch_id) ON DELETE CASCADE ON UPDATE CASCADE,
    raw_row_data    TEXT NOT NULL,            -- original row, as JSON, unmodified
    error_reason    TEXT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_bulk_import_error_batch ON bulk_import_error(batch_id);

-- ============================================================================
-- SECTION 3: CASH — BANK ACCOUNTS, OWNERSHIP, CATEGORIES
-- ============================================================================

CREATE TABLE bank_account (
    account_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id               INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,  -- primary/registered holder metadata (V4 decision 1)
    bank_name                TEXT NOT NULL,
    account_type             TEXT NOT NULL CHECK (account_type IN ('SAVINGS','CURRENT','JOINT')),
    account_number_masked    TEXT NOT NULL,   -- last 4 digits only — never store full account number
    opening_balance           DECIMAL(18,2) NOT NULL DEFAULT 0,
    current_balance           DECIMAL(18,2) NOT NULL DEFAULT 0, -- ENGINE-DERIVED CACHE: full recompute only, never delta-patched (V5.1 change 1)
    is_active                INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    is_default               INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),  -- migration 006: member's default account for cash flow
    balance_as_of            DATE  -- migration 006: date opening_balance (the balance the user entered) was true
);
CREATE INDEX idx_bank_account_member ON bank_account(member_id);

CREATE TABLE account_ownership (
    account_id      INTEGER NOT NULL REFERENCES bank_account(account_id) ON DELETE CASCADE ON UPDATE CASCADE,
    member_id       INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    ownership_pct   DECIMAL(5,2) NOT NULL CHECK (ownership_pct > 0 AND ownership_pct <= 100),
    PRIMARY KEY (account_id, member_id)
    -- Cross-row invariant "SUM(ownership_pct) per account = 100" and "primary
    -- holder always has a row here" are APPLICATION-LEVEL (Repository), see
    -- companion audit §C — SQLite cannot express a same-table aggregate CHECK.
);

CREATE TABLE txn_category (
    category_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_category_id     INTEGER REFERENCES txn_category(category_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    name                    TEXT NOT NULL,
    category_group          TEXT NOT NULL CHECK (category_group IN ('INCOME','EXPENSE','INVESTMENT','LOAN','TRANSFER'))
);
CREATE INDEX idx_txn_category_parent ON txn_category(parent_category_id);

-- ============================================================================
-- SECTION 4: INVESTMENT HOLDINGS — ASSET PARENT + OWNERSHIP
-- (created before `transaction` and `liability`, since property_holding and
-- transaction both reference asset/liability across sections)
-- ============================================================================

CREATE TABLE asset (
    asset_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id           INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE, -- primary/registered holder metadata
    category            TEXT NOT NULL CHECK (category IN ('MF','STOCK','FD','GOLD','EPF','PPF','NPS','PROPERTY')),
    name                 TEXT NOT NULL,
    institution           TEXT,
    invested_amount       DECIMAL(18,2) NOT NULL DEFAULT 0,
    current_value         DECIMAL(18,2) NOT NULL DEFAULT 0,  -- ENGINE-DERIVED CACHE: recomputed from holding state + latest valuation/NAV, never incremented/decremented (V5.1 change 1)
    purchase_date          DATE NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CLOSED','MATURED'))
);
CREATE INDEX idx_asset_member ON asset(member_id);
CREATE INDEX idx_asset_category ON asset(category);

CREATE TABLE asset_ownership (
    asset_id        INTEGER NOT NULL REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    member_id       INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    ownership_pct   DECIMAL(5,2) NOT NULL CHECK (ownership_pct > 0 AND ownership_pct <= 100),
    PRIMARY KEY (asset_id, member_id)
    -- SUM=100 and "primary holder has a row" invariants: APPLICATION-LEVEL, see §C.
);

-- ============================================================================
-- SECTION 5: LIABILITIES — PARENT + OWNERSHIP
-- ============================================================================

CREATE TABLE liability (
    liability_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id             INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE, -- primary/registered holder metadata
    type                   TEXT NOT NULL CHECK (type IN ('HOME_LOAN','PERSONAL_LOAN','CAR_LOAN','EDUCATION_LOAN','CREDIT_CARD','OTHER')),
    lender                 TEXT NOT NULL,
    sanctioned_amount       DECIMAL(18,2) NOT NULL,
    disbursed_amount        DECIMAL(18,2) NOT NULL,
    outstanding_amount      DECIMAL(18,2) NOT NULL DEFAULT 0, -- ENGINE-DERIVED CACHE: recomputed from loan_payment/prepayment history, never delta-patched (V5.1 change 1)
    start_date              DATE NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CLOSED'))
);
CREATE INDEX idx_liability_member ON liability(member_id);

CREATE TABLE liability_ownership (
    liability_id    INTEGER NOT NULL REFERENCES liability(liability_id) ON DELETE CASCADE ON UPDATE CASCADE,
    member_id       INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    ownership_pct   DECIMAL(5,2) NOT NULL CHECK (ownership_pct > 0 AND ownership_pct <= 100),
    PRIMARY KEY (liability_id, member_id)
    -- SUM=100 and "primary holder has a row" invariants: APPLICATION-LEVEL, see §C.
);

-- ============================================================================
-- SECTION 6: TRANSACTION LEDGER + RECURRING RULES
-- ============================================================================

CREATE TABLE "transaction" (
    txn_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id           INTEGER NOT NULL REFERENCES bank_account(account_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    txn_date              DATE NOT NULL,
    amount                 DECIMAL(18,2) NOT NULL,      -- signed: negative = outflow
    txn_type               TEXT NOT NULL CHECK (txn_type IN ('SIP','LUMPSUM','REDEMPTION','SELL','EMI','PREPAYMENT','INCOME','EXPENSE','TRANSFER','INTEREST_CREDIT','DIVIDEND')),
    category_id            INTEGER REFERENCES txn_category(category_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    asset_id                INTEGER REFERENCES asset(asset_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    liability_id             INTEGER REFERENCES liability(liability_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    transfer_group_id        TEXT,                       -- shared UUID across both legs of a TRANSFER (V3; replaces linked_txn_id)
    status                   TEXT NOT NULL DEFAULT 'POSTED' CHECK (status IN ('POSTED','REVERSED','VOID')),
    reversal_of_txn_id       INTEGER REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    data_confidence          TEXT NOT NULL DEFAULT 'ACTUAL' CHECK (data_confidence IN ('ACTUAL','RECONSTRUCTED','ESTIMATED')),
    import_batch_id           INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE CASCADE ON UPDATE CASCADE,
    description                TEXT,
    reference_no                TEXT,

    CHECK (asset_id IS NULL OR liability_id IS NULL),                       -- a transaction affects an asset XOR a liability, never both
    CHECK (txn_type != 'TRANSFER' OR transfer_group_id IS NOT NULL),        -- every TRANSFER leg must carry its pairing id
    CHECK (txn_type != 'TRANSFER' OR (asset_id IS NULL AND liability_id IS NULL)), -- transfers never touch asset/liability
    CHECK (reversal_of_txn_id IS NULL OR reversal_of_txn_id != txn_id)      -- cannot reference itself
);
CREATE INDEX idx_txn_account_date ON "transaction"(account_id, txn_date);
CREATE INDEX idx_txn_asset ON "transaction"(asset_id);
CREATE INDEX idx_txn_liability ON "transaction"(liability_id);
CREATE INDEX idx_txn_category ON "transaction"(category_id);
CREATE INDEX idx_txn_status ON "transaction"(status);
CREATE INDEX idx_txn_transfer_group ON "transaction"(transfer_group_id);
CREATE INDEX idx_txn_reversal_of ON "transaction"(reversal_of_txn_id);
CREATE INDEX idx_txn_import_batch ON "transaction"(import_batch_id);

CREATE TABLE recurring_rule (
    rule_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id            INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    rule_type             TEXT NOT NULL CHECK (rule_type IN ('INCOME','EXPENSE','SIP','EMI','PREMIUM')),
    account_id             INTEGER NOT NULL REFERENCES bank_account(account_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    category_id             INTEGER REFERENCES txn_category(category_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    asset_id                 INTEGER REFERENCES asset(asset_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    liability_id               INTEGER REFERENCES liability(liability_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    amount                      DECIMAL(18,2) NOT NULL,
    frequency                    TEXT NOT NULL CHECK (frequency IN ('MONTHLY','QUARTERLY','ANNUAL')),
    day_of_period                  INTEGER NOT NULL CHECK (day_of_period BETWEEN 1 AND 31),
    start_date                      DATE NOT NULL,
    end_date                         DATE,
    is_active                         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    last_generated_date                DATE
);
CREATE INDEX idx_recurring_rule_member ON recurring_rule(member_id);
CREATE INDEX idx_recurring_rule_active ON recurring_rule(is_active);

-- ============================================================================
-- SECTION 7: ASSET VALUATION HISTORY
-- ============================================================================

CREATE TABLE asset_valuation (
    valuation_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id              INTEGER NOT NULL REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    valuation_date          DATE NOT NULL,
    market_value              DECIMAL(18,2) NOT NULL,
    source                      TEXT NOT NULL CHECK (source IN ('AMFI','NSE','MANUAL','OTHER')),
    data_confidence              TEXT NOT NULL DEFAULT 'ACTUAL' CHECK (data_confidence IN ('ACTUAL','RECONSTRUCTED','ESTIMATED')),
    is_correction                  INTEGER NOT NULL DEFAULT 0 CHECK (is_correction IN (0,1)),
    import_batch_id                  INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX idx_asset_valuation_asset_date ON asset_valuation(asset_id, valuation_date);
CREATE UNIQUE INDEX uq_asset_valuation_noncorrected
    ON asset_valuation(asset_id, valuation_date)
    WHERE is_correction = 0;                 -- one non-correction valuation per asset per date

-- ============================================================================
-- SECTION 8: TYPE-SPECIFIC HOLDING DETAIL TABLES (1:1 with asset)
-- ============================================================================

CREATE TABLE mf_holding (
    asset_id                INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    amc                       TEXT NOT NULL,
    scheme_name                TEXT NOT NULL,
    folio_number                 TEXT NOT NULL,
    isin                          TEXT,
    units                          DECIMAL(18,4) NOT NULL DEFAULT 0,  -- ENGINE-DERIVED CACHE: sum of ACTIVE investment_lot.units_remaining (V5.1: investment_lot authoritative)
    avg_nav                          DECIMAL(18,4),                      -- ENGINE-DERIVED CACHE: weighted avg from investment_lot, not independently editable (V4 decision 3)
    current_nav                        DECIMAL(18,4),                    -- manual entry in v1 (V3 decision)
    sip_enabled                          INTEGER NOT NULL DEFAULT 0 CHECK (sip_enabled IN (0,1)),
    sip_amount                             DECIMAL(18,2),
    sip_date                                 INTEGER CHECK (sip_date BETWEEN 1 AND 31),
    sip_installments_done                      INTEGER,
    broker                TEXT  -- migration 010: platform / broker it is bought through
);

CREATE TABLE stock_holding (
    asset_id            INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    broker                TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    exchange                  TEXT NOT NULL CHECK (exchange IN ('NSE','BSE')),
    quantity                    DECIMAL(18,4) NOT NULL DEFAULT 0, -- ENGINE-DERIVED CACHE: sum of ACTIVE investment_lot.units_remaining
    avg_buy_price                  DECIMAL(18,4),                    -- ENGINE-DERIVED CACHE: weighted avg from investment_lot (V4 decision 3)
    current_price                    DECIMAL(18,4)                     -- manual entry in v1
);

CREATE TABLE fd_holding (
    asset_id            INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    fd_number             TEXT NOT NULL,
    bank_name               TEXT NOT NULL,
    principal                 DECIMAL(18,2) NOT NULL CHECK (principal > 0),
    interest_rate               DECIMAL(5,2) NOT NULL CHECK (interest_rate >= 0),
    issue_date                    DATE NOT NULL,
    maturity_date                   DATE NOT NULL,
    payout_type                       TEXT NOT NULL CHECK (payout_type IN ('MATURITY','QUARTERLY','MONTHLY','MANUAL')),
    maturity_value                      DECIMAL(18,2),

    CHECK (maturity_date > issue_date)
);
CREATE INDEX idx_fd_holding_maturity ON fd_holding(maturity_date);

CREATE TABLE gold_holding (
    asset_id                INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    form                       TEXT NOT NULL CHECK (form IN ('PHYSICAL','DIGITAL','SGB')),
    quantity_grams               DECIMAL(18,4) NOT NULL CHECK (quantity_grams > 0),
    purchase_rate_per_gram          DECIMAL(18,2) NOT NULL,
    sgb_interest_rate                  DECIMAL(5,2),
    purity                                TEXT NOT NULL DEFAULT '24K' CHECK (purity IN ('24K','22K','18K'))  -- migration 003; last because it is appended
);

CREATE TABLE retirement_account (
    asset_id                INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    account_type               TEXT NOT NULL CHECK (account_type IN ('EPF','PPF')),
    account_number                TEXT NOT NULL,
    employee_contribution            DECIMAL(18,2) NOT NULL DEFAULT 0,
    employer_contribution               DECIMAL(18,2),
    interest_rate                          DECIMAL(5,2) NOT NULL
);

CREATE TABLE nps_account (
    asset_id             INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    pran                   TEXT NOT NULL,
    tier                      TEXT NOT NULL CHECK (tier IN ('I','II')),
    employee_contribution       DECIMAL(18,2) NOT NULL DEFAULT 0,
    employer_contribution          DECIMAL(18,2),
    equity_pct                        DECIMAL(5,2) NOT NULL DEFAULT 0 CHECK (equity_pct BETWEEN 0 AND 100),
    corporate_debt_pct                   DECIMAL(5,2) NOT NULL DEFAULT 0 CHECK (corporate_debt_pct BETWEEN 0 AND 100),
    gov_bond_pct                            DECIMAL(5,2) NOT NULL DEFAULT 0 CHECK (gov_bond_pct BETWEEN 0 AND 100)
);

CREATE TABLE property_holding (
    asset_id                   INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    property_type                 TEXT NOT NULL CHECK (property_type IN ('RESIDENTIAL','COMMERCIAL','LAND')),
    address                          TEXT NOT NULL,
    area_sqft                          DECIMAL(10,2),
    purchase_price                        DECIMAL(18,2) NOT NULL,
    stamp_duty                               DECIMAL(18,2) NOT NULL DEFAULT 0,
    registration_cost                           DECIMAL(18,2) NOT NULL DEFAULT 0,
    other_purchase_cost                            DECIMAL(18,2) NOT NULL DEFAULT 0,
    total_acquisition_cost                            DECIMAL(18,2) NOT NULL DEFAULT 0, -- ENGINE-DERIVED CACHE: purchase_price + stamp_duty + registration_cost + other_purchase_cost
    possession_date                                      DATE,
    linked_liability_id                                     INTEGER REFERENCES liability(liability_id) ON DELETE RESTRICT ON UPDATE CASCADE
);
CREATE INDEX idx_property_holding_liability ON property_holding(linked_liability_id);

-- ============================================================================
-- SECTION 8B: MF TRANSACTIONS (V6.1 addition, explicitly approved — see below)
-- ============================================================================
-- This table is the one deliberate deviation from the "no new features"
-- principle stated at the top of this file. It was added when building the
-- MF page: SIP/lumpsum purchases need per-transaction history (date, units,
-- NAV, amount), but the existing acquisition path for that shape of data —
-- investment_lot.acquisition_txn_id -> "transaction".account_id — requires a
-- bank_account row on every entry, which the product does not want for MF
-- (which bank account funded a SIP is irrelevant here). mf_transaction is a
-- lightweight, bank-independent equivalent: an append-only record of MF unit
-- acquisitions only (no redemption/sell — that stays out of scope until the
-- FIFO/lot block this project has otherwise deferred). mf_holding.units and
-- avg_nav are derived from this table's ACTIVE rows the same way the DDL
-- comments above already describe investment_lot deriving them — just
-- sourced from here instead, for MF specifically.

CREATE TABLE mf_transaction (
    mf_txn_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id              INTEGER NOT NULL REFERENCES asset(asset_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    txn_date                DATE NOT NULL,
    txn_type                  TEXT NOT NULL CHECK (txn_type IN ('SIP','LUMPSUM')),
    units                       DECIMAL(18,4) NOT NULL CHECK (units > 0),
    nav                          DECIMAL(18,4) NOT NULL CHECK (nav > 0),   -- price per unit on txn_date
    amount                         DECIMAL(18,2) NOT NULL CHECK (amount > 0),  -- units * nav, not DB-enforced (rounding)
    status                           TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','REVERSED')),
    description                        TEXT,
    is_opening_balance    INTEGER NOT NULL DEFAULT 0 CHECK (is_opening_balance IN (0,1)),  -- migration 009: everything before an imported statement, in one row
    import_batch_id       INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE SET NULL ON UPDATE CASCADE  -- migration 009
);
CREATE INDEX idx_mf_transaction_asset_date ON mf_transaction(asset_id, txn_date);
CREATE INDEX idx_mf_transaction_status ON mf_transaction(status);

-- ============================================================================
-- SECTION 8C: STOCK TRANSACTIONS (migration 002, explicitly approved)
-- ============================================================================
-- Same reasoning as mf_transaction (Section 8B): investment_lot requires a
-- bank-account "transaction" row per entry, which this product does not
-- want. Unlike MF this also records SELLs; the Engine applies the
-- average-cost method over ACTIVE rows in date order to derive
-- stock_holding.quantity/avg_buy_price and the realised gain.
CREATE TABLE stock_transaction (
    stock_txn_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id              INTEGER NOT NULL REFERENCES asset(asset_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    txn_date                DATE NOT NULL,
    txn_type                  TEXT NOT NULL CHECK (txn_type IN ('BUY','SELL')),
    quantity                    DECIMAL(18,4) NOT NULL CHECK (quantity > 0),
    price                         DECIMAL(18,4) NOT NULL CHECK (price > 0),       -- per share, before charges
    charges                         DECIMAL(18,2) NOT NULL DEFAULT 0 CHECK (charges >= 0),  -- brokerage, STT, stamp duty etc.
    status                            TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','REVERSED')),
    description                         TEXT,
    is_opening_balance    INTEGER NOT NULL DEFAULT 0 CHECK (is_opening_balance IN (0,1)),  -- migration 009: everything before an imported statement, in one row
    import_batch_id       INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE SET NULL ON UPDATE CASCADE  -- migration 009
);
CREATE INDEX idx_stock_transaction_asset_date ON stock_transaction(asset_id, txn_date);

-- ============================================================================
-- SECTION 9: INVESTMENT LOTS (V4 decision 5, V5.1 change 2)
-- ============================================================================

CREATE TABLE investment_lot (
    lot_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id                  INTEGER NOT NULL REFERENCES asset(asset_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    acquisition_txn_id           INTEGER NOT NULL REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    acquisition_date                DATE NOT NULL,
    units                              DECIMAL(18,4) NOT NULL CHECK (units > 0),
    price_per_unit                       DECIMAL(18,4) NOT NULL,
    units_remaining                         DECIMAL(18,4) NOT NULL,
    status                                     TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','EXHAUSTED','REVERSED')),  -- REVERSED added V5.1 change 2

    CHECK (units_remaining >= 0 AND units_remaining <= units)
);
CREATE INDEX idx_investment_lot_asset ON investment_lot(asset_id, acquisition_date);
CREATE INDEX idx_investment_lot_status ON investment_lot(status);
CREATE INDEX idx_investment_lot_acq_txn ON investment_lot(acquisition_txn_id);

CREATE TABLE lot_consumption (
    consumption_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    redemption_txn_id          INTEGER NOT NULL REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    lot_id                        INTEGER NOT NULL REFERENCES investment_lot(lot_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    units_consumed                   DECIMAL(18,4) NOT NULL CHECK (units_consumed > 0),
    realized_gain                       DECIMAL(18,2),         -- nullable — left unset until deferred tax-computation phase (V4 decision 5)
    reversed_by_txn_id                     INTEGER REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE  -- V5 addition
);
CREATE INDEX idx_lot_consumption_txn ON lot_consumption(redemption_txn_id);
CREATE INDEX idx_lot_consumption_lot ON lot_consumption(lot_id);

-- ============================================================================
-- SECTION 10: LOANS — RATE HISTORY, DETAIL, SCHEDULE, PREPAYMENTS
-- ============================================================================

CREATE TABLE loan_rate_change (
    rate_change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    liability_id            INTEGER NOT NULL REFERENCES liability(liability_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    effective_date             DATE NOT NULL,
    old_rate                     DECIMAL(5,2) NOT NULL,
    new_rate                        DECIMAL(5,2) NOT NULL,
    reason                             TEXT,
    created_at                           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_loan_rate_change_liability_date ON loan_rate_change(liability_id, effective_date);

CREATE TABLE loan_prepayment (
    prepayment_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    liability_id                INTEGER NOT NULL REFERENCES liability(liability_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    prepayment_date                DATE NOT NULL,
    amount                            DECIMAL(18,2) NOT NULL CHECK (amount > 0),
    principal_reduction                  DECIMAL(18,2) NOT NULL,
    revised_outstanding                     DECIMAL(18,2) NOT NULL,
    revised_tenure_months                      INTEGER,
    revised_emi                                   DECIMAL(18,2),
    txn_id                                           INTEGER REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    reversed_by_txn_id                                  INTEGER REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE,  -- V5 addition

    CHECK (revised_tenure_months IS NULL OR revised_emi IS NULL)   -- a restructure shortens tenure XOR lowers EMI, not both
);
CREATE INDEX idx_loan_prepayment_liability_date ON loan_prepayment(liability_id, prepayment_date);

CREATE TABLE loan_detail (
    liability_id              INTEGER PRIMARY KEY REFERENCES liability(liability_id) ON DELETE CASCADE ON UPDATE CASCADE,
    interest_rate                DECIMAL(5,2) NOT NULL DEFAULT 0,  -- ENGINE-DERIVED CACHE: rate in force today: initial_interest_rate, or the latest loan_rate_change.new_rate (migration 008)
    interest_type                   TEXT NOT NULL CHECK (interest_type IN ('FIXED','FLOATING')),
    original_tenure_months             INTEGER NOT NULL CHECK (original_tenure_months > 0),
    emi_amount                            DECIMAL(18,2) CHECK (emi_amount IS NULL OR emi_amount > 0),  -- the bank's EMI as entered; NULL = the Engine works it out (migration 008)
    emi_start_date                           DATE NOT NULL,                                               -- first EMI month; the months before it are pre-EMI (interest only)
    initial_interest_rate                       DECIMAL(5,2) NOT NULL CHECK (initial_interest_rate >= 0),  -- migration 008
    down_payment                                   DECIMAL(18,2) CHECK (down_payment IS NULL OR down_payment >= 0)  -- own contribution, home loans (migration 008)
);

-- ============================================================================
-- SECTION 10B: LOAN DISBURSEMENTS (migration 008, explicitly approved)
-- ============================================================================
-- A construction-linked home loan is paid out in stages. Each stage is one
-- row; liability.disbursed_amount is their total, kept by the Engine. A
-- future-dated row is a planned stage and is included in the projection.
CREATE TABLE loan_disbursement (
    disbursement_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    liability_id          INTEGER NOT NULL REFERENCES liability(liability_id) ON DELETE CASCADE ON UPDATE CASCADE,
    disbursement_date       DATE NOT NULL,
    amount                    DECIMAL(18,2) NOT NULL CHECK (amount > 0),
    note                        TEXT
);
CREATE INDEX idx_loan_disbursement_liability_date ON loan_disbursement(liability_id, disbursement_date);

CREATE TABLE loan_payment (
    payment_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    liability_id                    INTEGER NOT NULL REFERENCES liability(liability_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    installment_no                      INTEGER NOT NULL,
    due_date                               DATE NOT NULL,
    scheduled_emi                            DECIMAL(18,2) NOT NULL,
    scheduled_principal                        DECIMAL(18,2) NOT NULL,
    scheduled_interest                            DECIMAL(18,2) NOT NULL,
    payment_date                                     DATE,
    actual_amount_paid                                  DECIMAL(18,2),
    status                                                 TEXT NOT NULL DEFAULT 'DUE' CHECK (status IN ('DUE','PAID','MISSED','PARTIAL','SUPERSEDED','REVERSED')),  -- REVERSED added V5.1 change 3
    txn_id                                                    INTEGER REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    schedule_version                                             INTEGER NOT NULL DEFAULT 1,
    superseded_by_rate_change_id                                    INTEGER REFERENCES loan_rate_change(rate_change_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    superseded_by_prepayment_id                                        INTEGER REFERENCES loan_prepayment(prepayment_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    import_batch_id                                                       INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE CASCADE ON UPDATE CASCADE,
    actual_principal_paid                                                    DECIMAL(18,2),  -- V5 addition, Engine-set via interest-first rule (Part 4)
    actual_interest_paid                                                        DECIMAL(18,2), -- V5 addition, Engine-set via interest-first rule (Part 4)

    CHECK (superseded_by_rate_change_id IS NULL OR superseded_by_prepayment_id IS NULL), -- at most one superseding event type
    CHECK (status != 'PARTIAL' OR (actual_principal_paid IS NOT NULL AND actual_interest_paid IS NOT NULL))
);
CREATE INDEX idx_loan_payment_liability_due ON loan_payment(liability_id, due_date);
CREATE INDEX idx_loan_payment_status ON loan_payment(status);
CREATE INDEX idx_loan_payment_schedule_version ON loan_payment(liability_id, schedule_version);
CREATE INDEX idx_loan_payment_import_batch ON loan_payment(import_batch_id);

-- ============================================================================
-- SECTION 11: INSURANCE
-- ============================================================================

CREATE TABLE insurance_policy (
    policy_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id               INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,  -- insured person
    policy_type                TEXT NOT NULL CHECK (policy_type IN ('TERM','ENDOWMENT','ULIP','HEALTH','MOTOR','HOME')),
    insurer                       TEXT NOT NULL,
    policy_number                    TEXT NOT NULL,
    sum_assured                         DECIMAL(18,2),
    premium_amount                         DECIMAL(18,2) NOT NULL CHECK (premium_amount > 0),
    premium_frequency                         TEXT NOT NULL CHECK (premium_frequency IN ('MONTHLY','QUARTERLY','ANNUAL')),
    start_date                                   DATE NOT NULL,
    maturity_date                                   DATE,
    current_surrender_value                            DECIMAL(18,2),  -- nullable — endowment/ULIP only; deliberately kept outside `asset` (V2 design note)
    status                                                 TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','LAPSED','MATURED','CLAIMED')),
    accrued_bonus                                            DECIMAL(18,2),  -- nullable — declared bonus accrued so far; last because migration 001 appends it
    plan_name       TEXT,
    nominee         TEXT,  -- migration 011
    premium_paying_term_years INTEGER CHECK (premium_paying_term_years IS NULL OR premium_paying_term_years BETWEEN 1 AND 100)  -- migration 012: limited pay; NULL = until maturity
);
CREATE INDEX idx_insurance_policy_member ON insurance_policy(member_id);

CREATE TABLE insurance_premium_payment (
    payment_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id                INTEGER NOT NULL REFERENCES insurance_policy(policy_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    due_date                    DATE NOT NULL,
    payment_date                   DATE,
    amount                            DECIMAL(18,2) NOT NULL CHECK (amount > 0),
    status                               TEXT NOT NULL DEFAULT 'DUE' CHECK (status IN ('DUE','PAID','MISSED')),
    txn_id                                  INTEGER REFERENCES "transaction"(txn_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    import_batch_id                            INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX idx_insurance_premium_policy_due ON insurance_premium_payment(policy_id, due_date);
CREATE INDEX idx_insurance_premium_import_batch ON insurance_premium_payment(import_batch_id);

-- ============================================================================
-- SECTION 12: GOALS
-- ============================================================================

CREATE TABLE financial_goal (
    goal_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id              INTEGER NOT NULL REFERENCES family(family_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    member_id                 INTEGER REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,  -- nullable = family goal
    goal_name                    TEXT NOT NULL,
    goal_type                       TEXT NOT NULL CHECK (goal_type IN ('EDUCATION','RETIREMENT','PROPERTY','EMERGENCY_FUND','TRAVEL','OTHER')),
    target_amount                      DECIMAL(18,2) NOT NULL CHECK (target_amount > 0),
    target_date                           DATE NOT NULL,
    priority                                 INTEGER NOT NULL DEFAULT 0,
    status                                      TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','ACHIEVED','ABANDONED')),
    inflation_pct   DECIMAL(5,2) CHECK (inflation_pct IS NULL OR inflation_pct BETWEEN 0 AND 20)  -- migration 014: NULL = the usual rate for the goal type
);
CREATE INDEX idx_financial_goal_family ON financial_goal(family_id);
CREATE INDEX idx_financial_goal_member ON financial_goal(member_id);

CREATE TABLE goal_allocation (
    allocation_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id                  INTEGER NOT NULL REFERENCES financial_goal(goal_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    asset_id                    INTEGER NOT NULL REFERENCES asset(asset_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    allocated_pct                  DECIMAL(5,2) NOT NULL CHECK (allocated_pct > 0 AND allocated_pct <= 100),
    allocation_type                   TEXT NOT NULL CHECK (allocation_type IN ('HARD','SOFT'))
    -- Cross-row invariants (HARD sum <= 100 per asset; individual-goal ceiling
    -- bounded by asset_ownership share) are APPLICATION-LEVEL, see §C.
);
CREATE INDEX idx_goal_allocation_goal ON goal_allocation(goal_id);
CREATE INDEX idx_goal_allocation_asset ON goal_allocation(asset_id);

-- ============================================================================
-- SECTION 13: AUDIT / TRACEABILITY + SCENARIOS
-- ============================================================================

CREATE TABLE calculation_log (
    calc_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calc_type               TEXT NOT NULL,      -- open-ended (NET_WORTH, XIRR, CASH_FLOW, ...) — not enum-constrained, see Assumptions
    scope_member_id            INTEGER REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,  -- nullable = whole family
    as_of_date                    DATE NOT NULL,
    computed_at                      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    result_value                        DECIMAL(18,2) NOT NULL,
    engine_version                         TEXT NOT NULL,
    confidence_level                          TEXT NOT NULL CHECK (confidence_level IN ('ACTUAL','RECONSTRUCTED','ESTIMATED'))  -- V4 addition
);
CREATE INDEX idx_calculation_log_type_date ON calculation_log(calc_type, as_of_date);
CREATE INDEX idx_calculation_log_scope ON calculation_log(scope_member_id);

-- NOTE: calculation_input_ref stays GENERIC (source_table/source_row_id) —
-- typed per-source ref tables were explicitly deferred (V5/V5.1). No real FK
-- is possible on source_row_id as a result; see companion audit §C.
CREATE TABLE calculation_input_ref (
    ref_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    calc_id             INTEGER NOT NULL REFERENCES calculation_log(calc_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    source_table           TEXT NOT NULL,      -- e.g. 'transaction', 'asset_valuation' — application-enforced values only
    source_row_id             INTEGER NOT NULL  -- NOT a real FK — polymorphic, application-validated only
);
CREATE INDEX idx_calculation_input_ref_calc ON calculation_input_ref(calc_id);
CREATE INDEX idx_calculation_input_ref_source ON calculation_input_ref(source_table, source_row_id);

CREATE TABLE scenario (
    scenario_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL,
    description                TEXT,
    created_at                    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_hypothetical                  INTEGER NOT NULL DEFAULT 1 CHECK (is_hypothetical = 1)  -- always TRUE, per design
);

CREATE TABLE scenario_assumption (
    assumption_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id              INTEGER NOT NULL REFERENCES scenario(scenario_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    parameter                   TEXT NOT NULL,
    value                          DECIMAL(18,4) NOT NULL
);
CREATE INDEX idx_scenario_assumption_scenario ON scenario_assumption(scenario_id);

CREATE TABLE scenario_result (
    result_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id              INTEGER NOT NULL REFERENCES scenario(scenario_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    goal_id                     INTEGER REFERENCES financial_goal(goal_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    computed_at                    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    projected_value                   DECIMAL(18,2) NOT NULL,
    projected_date                       DATE,
    assumptions_snapshot                    TEXT NOT NULL,   -- JSON dump, for reproducibility
    based_on_calc_id                           INTEGER REFERENCES calculation_log(calc_id) ON DELETE RESTRICT ON UPDATE CASCADE,  -- V3 addition
    confidence_level                              TEXT NOT NULL CHECK (confidence_level IN ('ACTUAL','RECONSTRUCTED','ESTIMATED'))  -- V4 addition
);
CREATE INDEX idx_scenario_result_scenario ON scenario_result(scenario_id);
CREATE INDEX idx_scenario_result_goal ON scenario_result(goal_id);
CREATE INDEX idx_scenario_result_based_on ON scenario_result(based_on_calc_id);

-- ============================================================================
-- SECTION 13B: MONTHLY CASH FLOW ITEMS (migration 005, explicitly approved)
-- ============================================================================
-- A member's regular income / expense / investment entered by hand (salary,
-- rent, grocery, cook, RD...), for the Monthly Cash Flow tab. recurring_rule
-- is not used because it requires a bank account per rule, which this
-- product does not want. Every item belongs to one member (no shared rows).
-- The Engine converts QUARTERLY/ANNUAL amounts to a monthly equivalent.
CREATE TABLE cash_flow_item (
    item_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id             INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    flow_type               TEXT NOT NULL CHECK (flow_type IN ('INCOME','EXPENSE','INVESTMENT')),
    name                      TEXT NOT NULL,
    amount                      DECIMAL(18,2) NOT NULL CHECK (amount > 0),
    frequency                     TEXT NOT NULL CHECK (frequency IN ('MONTHLY','QUARTERLY','ANNUAL','ONCE')),  -- ONCE: migration 013
    category                        TEXT,  -- migration 006
    payment_mode                      TEXT CHECK (payment_mode IN ('CASH','CARD','UPI','BANK_TRANSFER','AUTO_DEBIT')),  -- migration 006
    account_id                          INTEGER REFERENCES bank_account(account_id) ON DELETE SET NULL ON UPDATE CASCADE,  -- migration 006
    spent_on                              DATE  -- migration 013: the date of a ONCE (ad hoc) row
);
CREATE INDEX idx_cash_flow_item_member ON cash_flow_item(member_id);

-- ============================================================================
-- SECTION 8D: STOCK REFERENCE DATA (migration 007, explicitly approved)
-- ============================================================================
-- Sector and SEBI market-cap category per symbol, for the Stocks tab's
-- portfolio snapshot. Filled automatically from NSE's free index lists
-- (Nifty 500 for the industry; Nifty 100 / Midcap 150 / Smallcap 250 for
-- large / mid / small) with source 'NSE'; a user's own entry is 'MANUAL' and
-- is never overwritten by the automatic refresh.
CREATE TABLE stock_info (
    symbol              TEXT PRIMARY KEY,
    company_name          TEXT,
    sector                  TEXT,
    cap_category              TEXT CHECK (cap_category IN ('LARGE','MID','SMALL')),
    source                      TEXT NOT NULL CHECK (source IN ('NSE','MANUAL')),
    updated_on                    DATE NOT NULL
);

-- ============================================================================
-- SECTION 13C: SUCCESSION CHECKLIST (migration 011, explicitly approved)
-- ============================================================================
-- Per person, the hand-over items the Financial Health tab asks about
-- (nominees, a will, joint accounts, where papers are kept), ticked by the user.
CREATE TABLE succession_check (
    member_id           INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE CASCADE ON UPDATE CASCADE,
    item                  TEXT NOT NULL CHECK (item IN ('NOMINEES_BANK','NOMINEES_INVESTMENTS','NOMINEES_INSURANCE','WILL','JOINT_HOLDING','PAPERS')),
    done                    INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0,1)),
    updated_on                DATE NOT NULL,
    PRIMARY KEY (member_id, item)
);

-- ============================================================================
-- SECTION 13D: PERSONAL PLANNING (migration 014, explicitly approved)
-- ============================================================================
-- What makes the Financial Health tab personal, alongside the V6 goal
-- tables (financial_goal, goal_allocation — a holding put towards a goal):
-- holdings earmarked for something other than a goal (the emergency fund,
-- someone's regular income, or simply "keep"), the family's planning
-- preferences, and the suggestions dismissed as "not for me".
CREATE TABLE asset_earmark (
    asset_id            INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    purpose               TEXT NOT NULL CHECK (purpose IN ('EMERGENCY','INCOME','KEEP'))
);

CREATE TABLE planning_preference (
    family_id           INTEGER PRIMARY KEY REFERENCES family(family_id) ON DELETE CASCADE ON UPDATE CASCADE,
    tax_slab_pct          DECIMAL(5,2) NOT NULL DEFAULT 30 CHECK (tax_slab_pct BETWEEN 0 AND 40),
    emergency_months        INTEGER NOT NULL DEFAULT 6 CHECK (emergency_months BETWEEN 1 AND 36),
    min_fd_amount             DECIMAL(18,2) NOT NULL DEFAULT 0 CHECK (min_fd_amount >= 0),
    avoid_direct_stocks         INTEGER NOT NULL DEFAULT 0 CHECK (avoid_direct_stocks IN (0,1))
);

CREATE TABLE planning_dismissed (
    family_id           INTEGER NOT NULL REFERENCES family(family_id) ON DELETE CASCADE ON UPDATE CASCADE,
    move_key              TEXT NOT NULL,
    dismissed_on            DATE NOT NULL,
    PRIMARY KEY (family_id, move_key)
);

-- ============================================================================
-- SECTION 13E: WHOSE MONEY FUNDS GOALS (migration 015, explicitly approved)
-- ============================================================================
-- Per person: may their money be used for goals that are not their own?
-- YES (normally), IF_NEEDED (only to cover a shortfall, always shown
-- separately — e.g. a retired parent), NO. No row = the default for the
-- person's stage of life (earning: YES; otherwise IF_NEEDED).
CREATE TABLE planning_member_policy (
    member_id           INTEGER PRIMARY KEY REFERENCES family_member(member_id) ON DELETE CASCADE ON UPDATE CASCADE,
    goal_money            TEXT NOT NULL CHECK (goal_money IN ('YES','IF_NEEDED','NO'))
);
