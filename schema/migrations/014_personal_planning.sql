-- Personal planning: a goal's own inflation rate, earmarks, planning
-- preferences and dismissed suggestions.
ALTER TABLE financial_goal ADD COLUMN inflation_pct DECIMAL(5,2) CHECK (inflation_pct IS NULL OR inflation_pct BETWEEN 0 AND 20);

-- ============================================================================
-- SECTION 13D: PERSONAL PLANNING (migration 014, explicitly approved)
-- ============================================================================
-- What makes the Financial Health tab personal, alongside the V6 goal
-- tables (financial_goal, goal_allocation — a holding put towards a goal):
-- holdings earmarked for something other than a goal (the emergency fund,
-- someone's regular income, or simply "keep"), the family's planning
-- preferences, and the suggestions dismissed as "not for me".
CREATE TABLE asset_earmark (
    asset_id            INTEGER PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE ON UPDATE CASCADE,
    purpose               TEXT NOT NULL CHECK (purpose IN ('EMERGENCY','INCOME','KEEP'))
);

CREATE TABLE planning_preference (
    family_id           INTEGER PRIMARY KEY REFERENCES family(family_id) ON DELETE CASCADE ON UPDATE CASCADE,
    tax_slab_pct          DECIMAL(5,2) NOT NULL DEFAULT 30 CHECK (tax_slab_pct BETWEEN 0 AND 40),
    emergency_months        INTEGER NOT NULL DEFAULT 6 CHECK (emergency_months BETWEEN 1 AND 36),
    min_fd_amount             DECIMAL(18,2) NOT NULL DEFAULT 0 CHECK (min_fd_amount >= 0),
    avoid_direct_stocks         INTEGER NOT NULL DEFAULT 0 CHECK (avoid_direct_stocks IN (0,1))
);

CREATE TABLE planning_dismissed (
    family_id           INTEGER NOT NULL REFERENCES family(family_id) ON DELETE CASCADE ON UPDATE CASCADE,
    move_key              TEXT NOT NULL,
    dismissed_on            DATE NOT NULL,
    PRIMARY KEY (family_id, move_key)
);
