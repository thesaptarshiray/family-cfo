
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
-- Each member's own risk tolerance, picked directly on the Stocks tab.
ALTER TABLE family_member ADD COLUMN risk_tolerance TEXT CHECK (risk_tolerance IN ('CONSERVATIVE','MODERATE','AGGRESSIVE'));
