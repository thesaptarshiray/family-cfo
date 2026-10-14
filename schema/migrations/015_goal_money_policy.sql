-- Whose money may fund family goals (the goal funding plan).
-- ============================================================================
-- SECTION 13E: WHOSE MONEY FUNDS GOALS (migration 015, explicitly approved)
-- ============================================================================
-- Per person: may their money be used for goals that are not their own?
-- YES (normally), IF_NEEDED (only to cover a shortfall, always shown
-- separately — e.g. a retired parent), NO. No row = the default for the
-- person's stage of life (earning: YES; otherwise IF_NEEDED).
CREATE TABLE planning_member_policy (
    member_id           INTEGER PRIMARY KEY REFERENCES family_member(member_id) ON DELETE CASCADE ON UPDATE CASCADE,
    goal_money            TEXT NOT NULL CHECK (goal_money IN ('YES','IF_NEEDED','NO'))
);
