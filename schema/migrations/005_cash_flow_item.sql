
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
    frequency                     TEXT NOT NULL CHECK (frequency IN ('MONTHLY','QUARTERLY','ANNUAL'))
);
CREATE INDEX idx_cash_flow_item_member ON cash_flow_item(member_id);
