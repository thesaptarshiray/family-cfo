-- Limited-pay policies (e.g. pay 10 years on a 16-year plan): premiums stop
-- after this many years even though cover runs to maturity. NULL = pay until maturity.
ALTER TABLE insurance_policy ADD COLUMN premium_paying_term_years INTEGER CHECK (premium_paying_term_years IS NULL OR premium_paying_term_years BETWEEN 1 AND 100);
