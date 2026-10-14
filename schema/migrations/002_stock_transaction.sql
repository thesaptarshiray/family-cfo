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
    description                         TEXT
);
CREATE INDEX idx_stock_transaction_asset_date ON stock_transaction(asset_id, txn_date);
