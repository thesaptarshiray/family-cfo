-- Accrued bonus on a policy (e.g. LIC reversionary bonus), updated manually
-- from the insurer's statement. Nullable: most policies have none.
ALTER TABLE insurance_policy ADD COLUMN accrued_bonus DECIMAL(18,2);
