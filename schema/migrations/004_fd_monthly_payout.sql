-- FD monthly interest payout (migration 004, explicitly approved).
-- SQLite cannot alter a CHECK constraint in place, so fd_holding is rebuilt
-- with 'MONTHLY' allowed, its rows copied across unchanged, and its index
-- re-created. Nothing references fd_holding, so the swap is self-contained.
CREATE TABLE fd_holding_new (
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
INSERT INTO fd_holding_new
    (asset_id, fd_number, bank_name, principal, interest_rate, issue_date,
     maturity_date, payout_type, maturity_value)
SELECT asset_id, fd_number, bank_name, principal, interest_rate, issue_date,
       maturity_date, payout_type, maturity_value
FROM fd_holding;
DROP TABLE fd_holding;
ALTER TABLE fd_holding_new RENAME TO fd_holding;
CREATE INDEX idx_fd_holding_maturity ON fd_holding(maturity_date);
