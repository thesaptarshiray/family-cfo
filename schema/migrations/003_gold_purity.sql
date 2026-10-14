-- Gold purity (migration 003, explicitly approved): jewellery is usually
-- 22K and must be valued at a 22K rate, not the 24K rate SGB/digital use.
ALTER TABLE gold_holding ADD COLUMN purity TEXT NOT NULL DEFAULT '24K' CHECK (purity IN ('24K','22K','18K'));
