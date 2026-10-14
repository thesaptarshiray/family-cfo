-- loan_detail is reshaped for the Loans tab. Nothing wrote to it before this
-- migration, so it is dropped and created again rather than altered column by
-- column (SQLite cannot relax a NOT NULL in place).
DROP TABLE loan_detail;
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
