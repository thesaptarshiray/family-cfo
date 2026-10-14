-- Which platform / broker a fund is bought through (FundsIndia, Groww, ET Money...),
-- for the MF tab's broker filter and per-broker totals. NULL = not set yet.
ALTER TABLE mf_holding ADD COLUMN broker TEXT;
