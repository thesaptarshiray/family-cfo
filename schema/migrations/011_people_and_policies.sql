-- Planning per person (Financial Health tab): where each member is in life,
-- who inherits from them, and when an earning member means to retire; and
-- the plan name and nominee of each insurance policy.
ALTER TABLE family_member ADD COLUMN life_stage TEXT CHECK (life_stage IN ('EARNING','RETIRED','DEPENDENT'));
ALTER TABLE family_member ADD COLUMN heir_member_id INTEGER REFERENCES family_member(member_id) ON DELETE SET NULL ON UPDATE CASCADE;
ALTER TABLE family_member ADD COLUMN retirement_age INTEGER CHECK (retirement_age IS NULL OR retirement_age BETWEEN 40 AND 80);
ALTER TABLE insurance_policy ADD COLUMN plan_name TEXT;
ALTER TABLE insurance_policy ADD COLUMN nominee TEXT;

-- ============================================================================
-- SECTION 13C: SUCCESSION CHECKLIST (migration 011, explicitly approved)
-- ============================================================================
-- Per person, the hand-over items the Financial Health tab asks about
-- (nominees, a will, joint accounts, where papers are kept), ticked by the user.
CREATE TABLE succession_check (
    member_id           INTEGER NOT NULL REFERENCES family_member(member_id) ON DELETE CASCADE ON UPDATE CASCADE,
    item                  TEXT NOT NULL CHECK (item IN ('NOMINEES_BANK','NOMINEES_INVESTMENTS','NOMINEES_INSURANCE','WILL','JOINT_HOLDING','PAPERS')),
    done                    INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0,1)),
    updated_on                DATE NOT NULL,
    PRIMARY KEY (member_id, item)
);
