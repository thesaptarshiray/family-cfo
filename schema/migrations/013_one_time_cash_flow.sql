-- One-time (ad hoc) rows on the Monthly Cash Flow tab: a repair, a gift, a
-- medical bill... counted only in the month of `spent_on`. SQLite cannot
-- widen a CHECK in place, so cash_flow_item is rebuilt with 'ONCE' allowed
-- and the new date column; every existing row is copied over unchanged.
CREATE TABLE cash_flow_item_new (
    item_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id             INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    flow_type               TEXT NOT NULL CHECK (flow_type IN ('INCOME','EXPENSE','INVESTMENT')),
    name                      TEXT NOT NULL,
    amount                      DECIMAL(18,2) NOT NULL CHECK (amount > 0),
    frequency                     TEXT NOT NULL CHECK (frequency IN ('MONTHLY','QUARTERLY','ANNUAL','ONCE')),
    category                        TEXT,
    payment_mode                      TEXT CHECK (payment_mode IN ('CASH','CARD','UPI','BANK_TRANSFER','AUTO_DEBIT')),
    account_id                          INTEGER REFERENCES bank_account(account_id) ON DELETE SET NULL ON UPDATE CASCADE,
    spent_on                              DATE
);
INSERT INTO cash_flow_item_new (item_id, member_id, flow_type, name, amount, frequency, category, payment_mode, account_id)
    SELECT item_id, member_id, flow_type, name, amount, frequency, category, payment_mode, account_id FROM cash_flow_item;
DROP TABLE cash_flow_item;
ALTER TABLE cash_flow_item_new RENAME TO cash_flow_item;
CREATE INDEX idx_cash_flow_item_member ON cash_flow_item(member_id);
