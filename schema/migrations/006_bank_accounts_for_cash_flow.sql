-- Bank accounts for the Monthly Cash Flow tab (migration 006, explicitly
-- approved). A member's account holds the balance they entered and the date
-- it was true (opening_balance / balance_as_of); the Engine estimates today's
-- balance from the monthly rows since then. One account per member can be the
-- default, used by rows that do not name an account (FD interest, SIPs,
-- premiums). Cash-flow rows gain a category, how they are paid, and the
-- account the money leaves or arrives in.
ALTER TABLE bank_account ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1));
ALTER TABLE bank_account ADD COLUMN balance_as_of DATE;
ALTER TABLE cash_flow_item ADD COLUMN category TEXT;
ALTER TABLE cash_flow_item ADD COLUMN payment_mode TEXT CHECK (payment_mode IN ('CASH','CARD','UPI','BANK_TRANSFER','AUTO_DEBIT'));
ALTER TABLE cash_flow_item ADD COLUMN account_id INTEGER REFERENCES bank_account(account_id) ON DELETE SET NULL ON UPDATE CASCADE;
