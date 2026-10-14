-- Importing a CAS (consolidated account statement): a holding seen for the
-- first time starts from one opening-balance row carrying everything bought
-- before the statement (its dates are not in the statement, so XIRR is not
-- worked out for that holding), and every imported row records the import
-- it came from (bulk_import_batch).
ALTER TABLE mf_transaction ADD COLUMN is_opening_balance INTEGER NOT NULL DEFAULT 0 CHECK (is_opening_balance IN (0,1));
ALTER TABLE mf_transaction ADD COLUMN import_batch_id INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE SET NULL ON UPDATE CASCADE;
ALTER TABLE stock_transaction ADD COLUMN is_opening_balance INTEGER NOT NULL DEFAULT 0 CHECK (is_opening_balance IN (0,1));
ALTER TABLE stock_transaction ADD COLUMN import_batch_id INTEGER REFERENCES bulk_import_batch(batch_id) ON DELETE SET NULL ON UPDATE CASCADE;
