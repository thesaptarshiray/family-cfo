"""Financial Engine primitives.

This module is intentionally narrow in scope: it orchestrates atomic
multi-table financial operations on a shared `UnitOfWork`, and owns the
coordination and validation rules described in
`family-finance-app-v6-ddl-audit.md`.

The Repository layer is meant to be the only layer that knows SQL, and the
move there is staged. Ledger (`"transaction"`) persistence goes through
`TransactionRepository`; bank-account rows are read through `BankRepository`;
the `current_balance` cache — a value this module calculates, never one it
looks up — is written through `DerivedCacheRepository`, which only stores
the value it is given. The one SQL statement left in this module is the
account-existence check in `_get_account_row`, bridged through
`Database.scope(uow)` so it still runs inside the same Unit of Work as the
repository calls. It moves behind a repository in a later step.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple, Union

from repository.asset_repository import (
    Asset, AssetRepository, FdHolding, GoldHolding, MutualFundHolding, NpsAccount,
    RetirementAccount,
    StockHolding,
)
from repository.asset_valuation_repository import AssetValuationRepository
from repository.bank_repository import BankAccount, BankRepository
from repository.cash_flow_repository import CashFlowItem, CashFlowRepository
from repository.database import Database
from repository.derived_cache import DerivedCacheRepository
from repository.errors import RepositoryIntegrityError
from repository.family_repository import Family, FamilyMember, FamilyRepository
from repository.import_batch_repository import ImportBatchRepository
from repository.succession_repository import SuccessionRepository
from repository.planning_repository import Goal, PlanningPreference, PlanningRepository
from repository.insurance_repository import InsurancePolicy, InsurancePolicyRepository
from repository.loan_repository import (
    Loan, LoanDisbursement, LoanRateChange, LoanRepository,
)
from repository.mf_transaction_repository import MfTransaction, MfTransactionRepository
from repository.stock_info_repository import StockInfo, StockInfoRepository
from repository.stock_transaction_repository import StockTransaction, StockTransactionRepository
from repository.transaction_repository import Transaction, TransactionRepository
from repository.unit_of_work import UnitOfWork

from .fd_valuation import (
    CUMULATIVE_PAYOUT_TYPE,
    compute_fd_current_value,
    derive_fd_interest_rate,
    compute_fd_interest,
    compute_fd_payout_interest,
)
from .loan_schedule import (
    EMI,
    PRE_EMI,
    FinancialYearRow,
    ScheduleRow,
    compute_loan_schedule,
    month_no_of,
    month_start,
    summarize_by_financial_year,
)
from .insurance_schedule import (
    compute_annual_premium,
    compute_premium_schedule,
    premium_due_dates_between,
)
from .stock_position import compute_stock_position
from .policy_review import PolicyReview, review_policy
from .planning import GOAL_TYPES, PURPOSES, GoalInput, HoldingInput, Plan, PlanInputs, make_plan
from .reminders import FD_MATURITY, PREMIUM, DueItem
from .health import (
    FinancialHealth, FundInput, HealthInputs, LoanInput, PersonInput, assess, fund_kind, is_regular_plan,
)
from .statement_import import HAND_TOP_UP, StatementImportMixin
from .surrender_value import (
    SURRENDER_POLICY_TYPES,
    SurrenderEstimate,
    estimate_traditional,
    estimate_ulip,
)
from .xirr import compute_xirr

# The ledger statuses whose rows count toward an account's balance.
LEDGER_EFFECTIVE_STATUSES = ("POSTED",)

Money = Union[Decimal, int, str]
_ZERO = Decimal("0")
# Policies that build cash value (surrender value, bonus) and so count as an
# investment; TERM/HEALTH/MOTOR/HOME are pure protection — a cost, not an asset.
SAVINGS_POLICY_TYPES = frozenset({"ENDOWMENT", "ULIP"})
INSURANCE_CATEGORY = "INSURANCE"
# Valued at the balance on their latest statement/passbook, recorded in
# asset_valuation (market_value = the whole balance, not a unit price).
STATEMENT_VALUED_CATEGORIES = frozenset({"EPF", "PPF", "NPS"})
# A purity's share of pure (24K) gold: 22K jewellery is 22/24 gold, so it is
# valued at 22/24 of the 24K rate. SGB and digital gold are always 24K.
GOLD_PURITY_FACTOR = {
    "24K": Decimal("1"),
    "22K": Decimal("22") / Decimal("24"),
    "18K": Decimal("18") / Decimal("24"),
}
_CENT = Decimal("0.01")
_NAV_PRECISION = Decimal("0.0001")  # matches mf_holding.avg_nav's DECIMAL(18,4)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class FdSummary:
    """One FD, flattened for display: its terms plus its computed value.

    `current_value` is always freshly computed as of today (see
    `FinancialEngine._current_value_for_asset`), not read back from the
    persisted `asset.current_value` cache — that cache is set once at
    creation (proving the write path works) but is not kept fresh by a
    periodic job yet, so a stale read would silently understate an older
    FD's value. `gain_loss`/`gain_loss_pct` are derived, never stored.
    """

    asset_id: int
    member_id: int
    name: str
    status: str
    bank_name: str
    fd_number: str
    principal: Decimal
    interest_rate: Decimal
    issue_date: date
    maturity_date: date
    payout_type: str
    maturity_value: Optional[Decimal]
    current_value: Decimal
    gain_loss: Decimal
    gain_loss_pct: Decimal
    # For FDs that pay interest out (QUARTERLY / MONTHLY): the amount of
    # each payout. None for cumulative (MATURITY) and MANUAL FDs.
    payout_interest: Optional[Decimal]
    # Interest from issue to today (capped at maturity), and over the whole
    # term, with each as % of principal — see `compute_fd_interest`. For
    # payout FDs this interest has gone to the bank account, so it is NOT
    # part of current_value (which stays at principal).
    interest_earned_so_far: Decimal
    interest_earned_pct: Decimal
    total_interest_by_maturity: Decimal
    total_interest_pct: Decimal
    # Principal plus all its interest to maturity — what the FD pays in all
    # (for payout FDs, part of it has already reached the bank account).
    amount_at_maturity: Decimal = Decimal("0")


@dataclass(frozen=True)
class InsurancePolicySummary:
    """One insurance policy, flattened for display: its terms plus where it
    stands in its premium schedule.

    `installments_due_so_far`/`next_due_date`/`total_premium_due_to_date`
    are schedule projections from the policy's own terms (see
    `insurance_schedule`), not a record of what was actually paid — there is
    no payment-confirmation ledger wired up yet. Named `..._due_...`
    throughout, never `..._paid_...`, so this is never mistaken for one.
    """

    policy_id: int
    member_id: int
    policy_type: str
    insurer: str
    policy_number: str
    sum_assured: Optional[Decimal]
    premium_amount: Decimal
    premium_frequency: str
    start_date: date
    maturity_date: Optional[date]
    current_surrender_value: Optional[Decimal]
    status: str
    installments_due_so_far: int
    next_due_date: Optional[date]
    total_premium_due_to_date: Decimal
    accrued_bonus: Optional[Decimal]
    # ENDOWMENT/ULIP build cash value and count as an investment (see
    # `SAVINGS_POLICY_TYPES`); pure-protection policies do not.
    is_savings_policy: bool
    plan_name: Optional[str] = None
    nominee: Optional[str] = None
    # Limited pay: premiums stop after this many years (None = until maturity),
    # on `premiums_end_date`.
    premium_paying_term_years: Optional[int] = None
    premiums_end_date: Optional[date] = None
    review: Optional["PolicyReview"] = None  # pros, cons, options — see `policy_review`


@dataclass(frozen=True)
class MfSummary:
    """One mutual fund holding: its identity, its transaction history, and
    everything computed from that history and its latest known NAV.

    `units`/`avg_nav`/`invested_amount` are sums over `transactions` (ACTIVE
    ones only) — real aggregates now, not a pass-through of what was typed
    once (contrast FD's `principal`, still a single directly-entered term).
    `current_nav`/`current_nav_source`/`current_nav_date` come from the
    latest `asset_valuation` row for this asset, not `mf_holding.current_nav`
    (which stays unused — `asset_valuation` gives a NAV history and a source
    for free, which is exactly what the product's own requirement list
    calls "Current NAV source"). `xirr` is `None` when it cannot be computed
    yet (see `financial_engine.xirr`) — most commonly, no transaction yet.
    """

    asset_id: int
    member_id: int
    name: str
    status: str
    amc: str
    scheme_name: str
    folio_number: str
    isin: Optional[str]
    sip_enabled: bool
    sip_amount: Optional[Decimal]
    units: Decimal
    avg_nav: Optional[Decimal]
    current_nav: Optional[Decimal]
    current_nav_source: Optional[str]
    current_nav_date: Optional[date]
    invested_amount: Decimal
    current_value: Decimal
    gain_loss: Decimal
    gain_loss_pct: Decimal
    xirr: Optional[Decimal]
    transactions: Tuple[MfTransaction, ...]
    # How the money went in: SIP instalments vs lumpsum purchases (ACTIVE
    # transactions), and the fund's kind — "SIP", "LUMPSUM" or "SIP + LUMPSUM"
    # (a running SIP counts as SIP even before its first instalment).
    sip_invested: Decimal = Decimal("0")
    lumpsum_invested: Decimal = Decimal("0")
    investment_type: str = "LUMPSUM"
    # RUNNING (an SIP amount is set), STOPPED (invested by SIP, no longer
    # running, units still held) or None (never an SIP).
    sip_status: Optional[str] = None
    sip_date: Optional[int] = None
    # Imported from a CAS and its purchases before the statement are still
    # one undated opening balance: see `complete_mf_purchase_details`.
    needs_purchase_details: bool = False
    opening_amount: Optional[Decimal] = None
    broker: Optional[str] = None  # FundsIndia, Groww, ET Money...; None = not set
    # Which views the fund shows under: SIP_RUNNING / SIP_STOPPED / LUMPSUM,
    # or UNSET while its imported opening balance awaits purchase details.
    kinds: Tuple[str, ...] = ()
    # A fund with both SIP and lumpsum purchases, part by part: units,
    # current value and XIRR of the SIP purchases and of the lumpsums.
    sip_units: Decimal = Decimal("0")
    lumpsum_units: Decimal = Decimal("0")
    sip_value: Decimal = Decimal("0")
    lumpsum_value: Decimal = Decimal("0")
    sip_xirr: Optional[Decimal] = None
    lumpsum_xirr: Optional[Decimal] = None


@dataclass(frozen=True)
class MemberInvestmentSummary:
    """One family member's holdings and totals.

    FD and MF are populated among investments; stock and gold/NPS/retirement/
    property have no creation path exposed through the Engine yet (see the
    accompanying assessment). The totals sum generically across every asset
    category a member holds (via `_current_value_for_asset`'s dispatch), so
    they need no change when the others arrive.

    `insurance_policies` rides alongside but is deliberately excluded from
    `total_invested`/`total_current_value`: insurance is not an investment
    in the same sense (see `InsurancePolicySummary` and the accompanying
    assessment), and whether a policy's `current_surrender_value` should
    ever count toward a member's or family's total is a decision this block
    does not make. MF, unlike insurance, does count toward these totals —
    it genuinely is an investment.
    """

    member_id: int
    name: str
    fds: Tuple[FdSummary, ...]
    mfs: Tuple[MfSummary, ...]
    insurance_policies: Tuple[InsurancePolicySummary, ...]
    total_invested: Decimal
    total_current_value: Decimal
    total_gain_loss: Decimal


@dataclass(frozen=True)
class MfPortfolioRow:
    """One holding in a portfolio view, with its owner's name alongside —
    needed once holdings from several members share one table."""

    owner_name: str
    mf: MfSummary


@dataclass(frozen=True)
class MfBrokerTotal:
    """ACTIVE funds held through one broker (`broker` None = not set yet),
    with their own XIRR (funds still awaiting purchase details left out,
    as in `MfPortfolio`)."""

    broker: Optional[str]
    funds: int
    invested: Decimal
    current_value: Decimal
    gain_loss: Decimal
    gain_loss_pct: Decimal
    xirr: Optional[Decimal]
    xirr_excluded_count: int


@dataclass(frozen=True)
class MfKindTotal:
    """ACTIVE funds of one kind (a fund with SIPs and lumpsums counts under
    both). `monthly_sip` is the running SIPs' total per month."""

    kind: str  # SIP_RUNNING / SIP_STOPPED / LUMPSUM / UNSET
    funds: int
    invested: Decimal
    current_value: Decimal
    gain_loss: Decimal
    gain_loss_pct: Decimal
    monthly_sip: Decimal


@dataclass(frozen=True)
class MfPortfolio:
    """MF holdings across a whole family, or narrowed to one member, with
    totals and a combined XIRR computed here rather than in the UI.

    `xirr` treats every ACTIVE transaction across the included holdings as
    one cash-flow series against their combined current value — the
    portfolio's own annualised return, not an average of per-holding XIRRs
    (which would ignore how much was invested in each, and when).
    """

    family_id: int
    member_id: Optional[int]
    rows: Tuple[MfPortfolioRow, ...]
    total_invested: Decimal
    total_current_value: Decimal
    total_gain_loss: Decimal
    total_gain_loss_pct: Decimal
    xirr: Optional[Decimal]
    total_sip_invested: Decimal = Decimal("0")
    total_lumpsum_invested: Decimal = Decimal("0")
    # Holdings left out of `xirr`: those started from an imported
    # statement's opening balance (their earlier purchase dates are unknown).
    xirr_excluded_count: int = 0
    by_broker: Tuple[MfBrokerTotal, ...] = ()  # largest current value first; "not set" last
    by_kind: Tuple[MfKindTotal, ...] = ()


@dataclass(frozen=True)
class StockSummary:
    """One stock holding, derived from its BUY/SELL history (average-cost
    method, see `stock_position`) and its latest known price.

    `invested_amount` is the cost basis of the shares still held (charges
    included); `gain_loss` is unrealised, on those shares; `realised_gain`
    is what past sells made. `xirr` covers every buy and sell plus today's
    value — the holding's whole return.
    """

    asset_id: int
    member_id: int
    name: str
    status: str
    broker: str
    symbol: str
    exchange: str
    quantity: Decimal
    avg_buy_price: Optional[Decimal]
    current_price: Optional[Decimal]
    current_price_source: Optional[str]
    current_price_date: Optional[date]
    invested_amount: Decimal
    current_value: Decimal
    gain_loss: Decimal
    gain_loss_pct: Decimal
    realised_gain: Decimal
    xirr: Optional[Decimal]
    transactions: Tuple[StockTransaction, ...]


@dataclass(frozen=True)
class StockPortfolioRow:
    owner_name: str
    stock: StockSummary


@dataclass(frozen=True)
class StockPortfolio:
    """Stocks across a whole family, or one member, with totals and a
    combined XIRR over every ACTIVE holding's buys, sells and current value."""

    family_id: int
    member_id: Optional[int]
    rows: Tuple[StockPortfolioRow, ...]
    total_invested: Decimal
    total_current_value: Decimal
    total_gain_loss: Decimal
    total_gain_loss_pct: Decimal
    total_realised_gain: Decimal
    xirr: Optional[Decimal]
    groups: Tuple["StockGroup", ...] = ()
    analysis: Optional["StockAnalysis"] = None
    xirr_excluded_count: int = 0  # as in MfPortfolio


@dataclass(frozen=True)
class StockGroupPart:
    """One person/broker holding inside a grouped stock row."""

    asset_id: int
    owner_name: str
    broker: str
    exchange: str
    quantity: Decimal
    avg_buy_price: Optional[Decimal]
    invested_amount: Decimal
    current_value: Decimal


@dataclass(frozen=True)
class StockGroup:
    """Every holding of one symbol in view (across brokers, and across
    members in the family view) as one row. `avg_buy_price` is the
    weighted average cost of all shares still held (charges included) —
    invested / quantity."""

    symbol: str
    company_name: Optional[str]
    sector: str
    cap_category: str  # LARGE / MID / SMALL / UNKNOWN
    quantity: Decimal
    avg_buy_price: Optional[Decimal]
    current_price: Optional[Decimal]
    invested_amount: Decimal
    current_value: Decimal
    gain_loss: Decimal
    gain_loss_pct: Decimal
    realised_gain: Decimal
    weight_pct: Decimal  # share of the portfolio's current value
    parts: Tuple[StockGroupPart, ...]


@dataclass(frozen=True)
class SplitSlice:
    name: str
    value: Decimal
    pct: Decimal


@dataclass(frozen=True)
class RiskCheck:
    """One member's risk tolerance against a rule-of-thumb reading of their
    stock portfolio. `fits` is None when no tolerance has been picked."""

    member_id: int
    name: str
    risk_tolerance: Optional[str]
    portfolio_risk: str  # MODERATE / HIGH / VERY_HIGH
    stock_share_of_wealth_pct: Decimal
    fits: Optional[bool]
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class StockAnalysis:
    """The Stocks tab's one-look snapshot, all computed here."""

    stock_count: int
    sector_split: Tuple[SplitSlice, ...]
    cap_split: Tuple[SplitSlice, ...]
    largest_symbol: Optional[str]
    largest_pct: Decimal
    top5_pct: Decimal
    effective_stocks: Decimal  # 1 / sum(weight^2): how many equal-sized stocks it behaves like
    diversification: str  # LOW / MEDIUM / GOOD
    sector_warnings: Tuple[str, ...]
    winners: Tuple[StockGroup, ...]
    losers: Tuple[StockGroup, ...]
    risk_checks: Tuple[RiskCheck, ...]
    classification_date: Optional[date]  # when sector / cap data was last refreshed


@dataclass(frozen=True)
class PfSummary:
    """One EPF or PPF account, valued from its latest statement.

    `total_contributions` (employee + employer, running totals from the
    passbook) is what was invested; `balance` is the passbook balance as of
    `balance_date`; `interest_earned` is the difference. `maturity_date` is
    PPF's lock-in end (15 full financial years after the one it was opened
    in); EPF has none.
    """

    asset_id: int
    member_id: int
    name: str
    status: str
    account_type: str
    account_number: str
    institution: Optional[str]
    opened_date: date
    interest_rate: Decimal
    employee_contribution: Decimal
    employer_contribution: Optional[Decimal]
    total_contributions: Decimal
    balance: Decimal
    balance_date: Optional[date]
    interest_earned: Decimal
    interest_earned_pct: Decimal
    maturity_date: Optional[date]


@dataclass(frozen=True)
class PfPortfolioRow:
    owner_name: str
    account: PfSummary


@dataclass(frozen=True)
class PfPortfolio:
    """EPF and PPF accounts across a family, or one member, with totals
    (ACTIVE accounts only)."""

    family_id: int
    member_id: Optional[int]
    rows: Tuple[PfPortfolioRow, ...]
    total_contributions: Decimal
    total_balance: Decimal
    total_interest_earned: Decimal
    total_interest_earned_pct: Decimal
    epf_balance: Decimal
    ppf_balance: Decimal


@dataclass(frozen=True)
class GoldSummary:
    """One gold purchase, valued at the latest rate for its purity.

    `current_rate_per_gram` is already adjusted for purity (e.g. 22/24 of
    the 24K rate for 22K). `xirr` treats the purchase as the only outflow.
    """

    asset_id: int
    member_id: int
    name: str
    status: str
    form: str
    purity: str
    quantity_grams: Decimal
    purchase_rate_per_gram: Decimal
    purchase_date: date
    sgb_interest_rate: Optional[Decimal]
    invested_amount: Decimal
    current_rate_per_gram: Optional[Decimal]
    rate_date: Optional[date]
    current_value: Decimal
    gain_loss: Decimal
    gain_loss_pct: Decimal
    xirr: Optional[Decimal]


@dataclass(frozen=True)
class GoldPortfolioRow:
    owner_name: str
    gold: GoldSummary


@dataclass(frozen=True)
class GoldPortfolio:
    """Gold across a family, or one member, with totals (ACTIVE only).
    `latest_rate_24k`/`latest_rate_date` is the most recent rate entered,
    expressed as 24K per gram."""

    family_id: int
    member_id: Optional[int]
    rows: Tuple[GoldPortfolioRow, ...]
    total_grams: Decimal
    total_invested: Decimal
    total_current_value: Decimal
    total_gain_loss: Decimal
    total_gain_loss_pct: Decimal
    xirr: Optional[Decimal]
    latest_rate_24k: Optional[Decimal]
    latest_rate_date: Optional[date]


@dataclass(frozen=True)
class NpsSummary:
    """One NPS account (Tier I or II), valued from its latest statement.
    Invested = your + employer contributions to date; gain = balance minus
    that. The equity / corporate-debt / government-bond split is as last
    recorded."""

    asset_id: int
    member_id: int
    name: str
    status: str
    pran: str
    tier: str
    institution: Optional[str]
    opened_date: date
    employee_contribution: Decimal
    employer_contribution: Optional[Decimal]
    total_contributions: Decimal
    equity_pct: Decimal
    corporate_debt_pct: Decimal
    gov_bond_pct: Decimal
    balance: Decimal
    balance_date: Optional[date]
    gain_loss: Decimal
    gain_loss_pct: Decimal


@dataclass(frozen=True)
class NpsPortfolioRow:
    owner_name: str
    account: NpsSummary


@dataclass(frozen=True)
class NpsPortfolio:
    family_id: int
    member_id: Optional[int]
    rows: Tuple[NpsPortfolioRow, ...]
    total_contributions: Decimal
    total_balance: Decimal
    total_gain_loss: Decimal
    total_gain_loss_pct: Decimal
    tier1_balance: Decimal
    tier2_balance: Decimal


@dataclass(frozen=True)
class CashFlowLine:
    """One line of a month's cash flow.

    `source`: FD_INTEREST and SIP are derived from the FD / MF tabs; ITEM is
    a row the user entered; PREMIUM is an insurance premium due this month.
    `amount`/`frequency` are as entered or as paid (e.g. Rs 49,750
    QUARTERLY); `monthly_amount` is what the month counts. A PREMIUM counts
    in full, and only once ticked as paid (`paid`); `counted` says whether
    the line is in the totals.
    """

    source: str
    flow_type: str  # INCOME / EXPENSE / INVESTMENT
    member_id: int
    owner_name: str
    label: str
    amount: Decimal
    frequency: str
    monthly_amount: Decimal
    counted: bool
    item_id: Optional[int] = None
    policy_id: Optional[int] = None
    due_date: Optional[date] = None
    paid: Optional[bool] = None
    category: Optional[str] = None
    payment_mode: Optional[str] = None
    # The bank account the money leaves / arrives in: the row's own account,
    # else the member's default. None for CASH rows, or when the member has
    # no default account yet.
    account_id: Optional[int] = None
    account_label: Optional[str] = None
    # A one-time (ad hoc) row's date: counted only in that month.
    spent_on: Optional[date] = None


@dataclass(frozen=True)
class BankAccountSummary:
    """A member's bank account on the cash-flow tab. `estimated_balance` is
    the balance entered (as of `balance_as_of`) plus, for each full month
    since, that month's rows paid into / out of this account, minus premiums
    ticked paid from it since then — an estimate; re-enter the real balance
    now and then to reset it."""

    account_id: int
    member_id: int
    owner_name: str
    bank_name: str
    account_last4: str
    is_default: bool
    is_active: bool
    entered_balance: Decimal
    balance_as_of: Optional[date]
    months_elapsed: int
    monthly_net: Decimal
    estimated_balance: Decimal


@dataclass(frozen=True)
class MonthlyCashFlow:
    """A month's income, expenses and investments for a family or one
    member. `surplus` = income - expenses - investments: the cash that
    should be left in the bank that month."""

    family_id: int
    member_id: Optional[int]
    year: int
    month: int
    income: Tuple[CashFlowLine, ...]
    expenses: Tuple[CashFlowLine, ...]
    investments: Tuple[CashFlowLine, ...]
    total_income: Decimal
    total_expenses: Decimal
    total_investments: Decimal
    surplus: Decimal
    accounts: Tuple[BankAccountSummary, ...] = ()
    total_cash_in_bank: Decimal = Decimal("0")
    # Net monthly bank flow of rows with no account to go to (member has no
    # default account) — shown so nothing silently drops out of the balance.
    unassigned_monthly_net: Decimal = Decimal("0")


@dataclass(frozen=True)
class FdPortfolioRow:
    owner_name: str
    fd: FdSummary


@dataclass(frozen=True)
class FdBankTotal:
    """ACTIVE FDs with one bank: what was put in, and `amount_at_maturity`
    summed over them (see `FdSummary.amount_at_maturity`)."""

    bank_name: str
    principal: Decimal
    amount_at_maturity: Decimal


@dataclass(frozen=True)
class FdPortfolio:
    """FDs across a whole family, or narrowed to one member, with totals.

    `total_value_at_maturity` is each ACTIVE FD's value on its own maturity
    date (the bank-quoted maturity value when known; principal for payout
    FDs, whose interest is paid out rather than accumulated).
    `next_maturity` is the ACTIVE FD maturing soonest on or after today.
    """

    family_id: int
    member_id: Optional[int]
    rows: Tuple[FdPortfolioRow, ...]
    total_principal: Decimal
    total_current_value: Decimal
    total_gain_loss: Decimal
    total_gain_loss_pct: Decimal
    total_value_at_maturity: Decimal
    next_maturity: Optional[FdPortfolioRow]
    total_interest_earned_so_far: Decimal
    total_interest_earned_pct: Decimal
    total_interest_by_maturity: Decimal
    total_interest_pct: Decimal
    # Largest first by amount at maturity.
    by_bank: Tuple[FdBankTotal, ...] = ()
    # ACTIVE FDs maturing today or later, soonest first.
    upcoming_maturities: Tuple[FdPortfolioRow, ...] = ()


@dataclass(frozen=True)
class InsurancePortfolioRow:
    owner_name: str
    policy: InsurancePolicySummary


@dataclass(frozen=True)
class InsurancePortfolio:
    """Insurance policies across a whole family, or one member, with totals.

    Insurance is cover, not an investment, so these are not current-value
    totals and are never added into net worth: `total_sum_assured` is the
    cover, `total_annual_premium` the yearly cost, and
    `total_premium_due_to_date` a schedule projection (not a payment record
    — see `InsurancePolicySummary`). `total_surrender_value` sums only the
    policies that have one recorded. Totals count ACTIVE policies only.
    """

    family_id: int
    member_id: Optional[int]
    rows: Tuple[InsurancePortfolioRow, ...]
    total_sum_assured: Decimal
    total_annual_premium: Decimal
    total_premium_due_to_date: Decimal
    total_surrender_value: Decimal
    total_accrued_bonus: Decimal
    next_premium: Optional[InsurancePortfolioRow]


@dataclass(frozen=True)
class AllocationSlice:
    """One asset class's share of a portfolio. Percentages are of the
    portfolio's total invested / total current value respectively."""

    category: str
    invested: Decimal
    current_value: Decimal
    invested_pct: Decimal
    current_value_pct: Decimal


@dataclass(frozen=True)
class AllocationMember:
    member_id: int
    name: str
    invested: Decimal
    current_value: Decimal
    gain_loss: Decimal


@dataclass(frozen=True)
class AssetAllocation:
    """Invested and current value by asset class, for the whole family or
    one member — what the Overview's two pie charts draw.

    Every ACTIVE asset counts, plus ACTIVE savings-type insurance policies
    (`SAVINGS_POLICY_TYPES`) as the `INSURANCE` class: invested is the
    premiums due to date, current value the surrender value plus accrued
    bonus. Pure-protection policies are left out — their premiums are a
    cost, not an investment. `slices` are ordered by current value, largest
    first, and omit classes with nothing in them. `members` is the same
    totals per member (only the selected one when narrowed).
    """

    family_id: int
    member_id: Optional[int]
    slices: Tuple[AllocationSlice, ...]
    members: Tuple[AllocationMember, ...]
    total_invested: Decimal
    total_current_value: Decimal
    total_gain_loss: Decimal
    # Principal owed today on ACTIVE loans, and current value minus it.
    total_loans_outstanding: Decimal = Decimal("0")
    net_worth: Decimal = Decimal("0")


@dataclass(frozen=True)
class LoanPrepaymentRow:
    """A prepayment and what it did in the schedule: `applied` is what came
    off the principal (never more than was owed), `outstanding_after` the
    principal left that month, `months_left_after` how long the loan then
    runs."""

    prepayment_id: int
    prepayment_date: date
    amount: Decimal
    applied: Decimal
    outstanding_after: Decimal
    months_left_after: int


@dataclass(frozen=True)
class LoanSummary:
    """One loan with its amortization schedule (see `loan_schedule` for the
    rules) and figures read off it.

    "To date" figures cover every month up to and including the current
    one, and `outstanding` is the principal at the end of this month, so a
    prepayment made this month already shows. Future-dated disbursements and
    prepayments are plans, and are part of the projection (`schedule`,
    `payoff_month`, `total_interest`). `interest_saved_by_prepayments` /
    `months_saved_by_prepayments` compare with the same loan without any
    prepayment. A CLOSED loan keeps its schedule for reference but owes
    nothing.
    """

    liability_id: int
    member_id: int
    loan_type: str
    lender: str
    status: str
    interest_type: str
    sanctioned_amount: Decimal
    total_disbursed: Decimal  # every disbursement entered, planned ones too
    disbursed_to_date: Decimal
    undisbursed: Decimal  # sanctioned, not yet entered as a disbursement
    down_payment: Optional[Decimal]
    start_date: date
    tenure_months: int
    pre_emi_months: int
    first_emi_month: date
    original_end_month: date
    initial_interest_rate: Decimal
    interest_rate: Decimal  # in force this month
    emi: Optional[Decimal]
    emi_entered: bool  # the bank's EMI as entered, not worked out
    current_month_no: int  # 0 before the loan starts
    phase: str  # NOT_STARTED / PRE_EMI / EMI / REPAID / CLOSED
    outstanding: Decimal
    this_month_payment: Decimal
    this_month_interest: Decimal
    interest_paid_to_date: Decimal
    principal_repaid_to_date: Decimal
    prepaid_to_date: Decimal
    total_paid_to_date: Decimal  # pre-EMI interest + EMIs + prepayments
    total_interest: Decimal  # over the whole projected life
    total_payment: Decimal  # every payment and prepayment, whole life
    payoff_month: Optional[date]  # None if not repaid within the schedule
    months_remaining: int
    interest_saved_by_prepayments: Decimal
    months_saved_by_prepayments: int
    warnings: Tuple[str, ...]
    schedule: Tuple[ScheduleRow, ...]
    financial_years: Tuple[FinancialYearRow, ...]
    disbursements: Tuple[LoanDisbursement, ...]
    prepayments: Tuple[LoanPrepaymentRow, ...]
    rate_changes: Tuple[LoanRateChange, ...]


@dataclass(frozen=True)
class LoanPortfolioRow:
    owner_name: str
    loan: LoanSummary


@dataclass(frozen=True)
class LoanPortfolio:
    """Loans across a whole family, or one member. Totals count ACTIVE
    loans; `this_month_payment` is this month's EMIs and pre-EMI interest."""

    family_id: int
    member_id: Optional[int]
    rows: Tuple[LoanPortfolioRow, ...]
    total_outstanding: Decimal
    total_sanctioned: Decimal
    total_disbursed_to_date: Decimal
    this_month_payment: Decimal
    total_interest_paid_to_date: Decimal
    total_interest_remaining: Decimal


@dataclass(frozen=True)
class LoanWhatIf:
    """A prepayment tried out on a loan, nothing saved: the loan as it is
    now against the loan with `amount` prepaid on `on_date` (and, if given,
    `extra_monthly` prepaid every month from then on)."""

    liability_id: int
    amount: Decimal
    on_date: date
    extra_monthly: Decimal
    emi_before: Optional[Decimal]
    emi_after: Optional[Decimal]
    payoff_before: Optional[date]
    payoff_after: Optional[date]
    months_saved: int
    total_interest_before: Decimal
    total_interest_after: Decimal
    interest_saved: Decimal


@dataclass(frozen=True)
class FamilyDashboard:
    """Family-wide totals and a per-member breakdown."""

    family_id: int
    family_name: str
    base_currency: str
    total_invested: Decimal
    total_current_value: Decimal
    total_gain_loss: Decimal
    by_category: Dict[str, Decimal]
    members: Tuple[MemberInvestmentSummary, ...]


@dataclass(frozen=True)
class PlanHolding:
    """A holding in the plan-settings editor: what it is and what it is for."""

    asset_id: int
    owner_name: str
    kind: str
    label: str
    value: Decimal
    goal_id: Optional[int]
    goal_pct: Decimal
    purpose: Optional[str]  # EMERGENCY / INCOME / KEEP


@dataclass(frozen=True)
class MemberMoneyPolicy:
    member_id: int
    name: str
    goal_money: str  # YES / IF_NEEDED / NO
    is_default: bool


@dataclass(frozen=True)
class PlanPage:
    """Everything the Financial Health tab shows (see `get_plan`)."""

    plan: Plan
    health: FinancialHealth
    goal_records: Tuple[Goal, ...]
    holdings: Tuple[PlanHolding, ...]
    preferences: PlanningPreference
    member_policies: Tuple[MemberMoneyPolicy, ...] = ()


class FinancialEngine(StatementImportMixin):
    """Coordinate atomic financial actions above the repository layer.

    This is the single entry point above the repository layer: the API/UI
    package (and eventually the AI/CFO layer) is expected to call only
    `FinancialEngine`, never a repository directly — the architecture-
    boundary tests enforce this the moment such a package exists. Plain
    family/member CRUD has no financial meaning of its own, but it is
    exposed here as a thin delegate to `FamilyRepository` for exactly that
    reason: it is the only door above this layer.

    - `create_transfer` updates both legs of a transfer atomically
    - `execute_reversal` performs the reversal cascade for posted entries
    - `create_fd` / `list_fds` create and read fixed deposits, with their
      value computed by `fd_valuation` — the deterministic, formula-based
      strategy for this asset category (see the FD product assessment)
    - `get_member_investment_summary` / `get_family_dashboard` aggregate
      current value across a member's or family's holdings

    Redemption and sell are intentionally absent: they require investment
    lots and FIFO cost-basis accounting, and will be added together with that
    block rather than as a cash-only operation.
    """

    def __init__(self, database: Database) -> None:
        self._db = database
        self._bank = BankRepository(database)
        self._txns = TransactionRepository(database)
        self._cache = DerivedCacheRepository(database)
        self._family = FamilyRepository(database)
        self._assets = AssetRepository(database)
        self._insurance = InsurancePolicyRepository(database)
        self._mf_txns = MfTransactionRepository(database)
        self._stock_txns = StockTransactionRepository(database)
        self._stock_info = StockInfoRepository(database)
        self._cash_flow = CashFlowRepository(database)
        self._planning = PlanningRepository(database)
        self._valuations = AssetValuationRepository(database)
        self._loans = LoanRepository(database)
        self._imports = ImportBatchRepository(database)
        self._succession = SuccessionRepository(database)

    def _get_account_row(self, uow: UnitOfWork, account_id: int):
        with self._db.scope(uow) as conn:
            return conn.execute(
                "SELECT account_id, member_id, opening_balance, current_balance "
                "FROM bank_account WHERE account_id = ?",
                (account_id,),
            ).fetchone()

    def _account_balance_from_opening_and_txns(
        self, account_id: int, uow: Optional[UnitOfWork] = None
    ) -> Decimal:
        account = self._bank.get_account(account_id, uow=uow)
        if account is None:
            raise ValueError(f"account {account_id} does not exist")

        amounts = self._txns.list_amounts(account_id, LEDGER_EFFECTIVE_STATUSES, uow=uow)
        return account.opening_balance + sum(amounts, Decimal("0"))

    def create_transfer(
        self,
        *,
        from_account_id: int,
        to_account_id: int,
        amount: Decimal,
        description: Optional[str] = None,
    ) -> tuple[Transaction, Transaction]:
        """Create a paired transfer between two bank accounts.

        The engine uses the account's opening balance as the baseline and then
        applies all posted ledger amounts to derive the current balance. This
        matches the schema's "engine-derived cache" design while keeping the
        repository as the library boundary for database access.
        """
        if from_account_id == to_account_id:
            raise ValueError("source and destination accounts must differ")
        if amount <= 0:
            raise ValueError("transfer amount must be positive")

        amount = Decimal(str(amount))

        with self._db.unit_of_work() as uow:
            from_row = self._get_account_row(uow, from_account_id)
            to_row = self._get_account_row(uow, to_account_id)
            if from_row is None or to_row is None:
                raise ValueError("both source and destination accounts must exist")

            transfer_group_id = str(uuid.uuid4())
            txn_date = date.today()

            from_txn = self._txns.create_transaction(
                account_id=from_account_id,
                txn_date=txn_date,
                amount=-amount,
                txn_type="TRANSFER",
                transfer_group_id=transfer_group_id,
                description=description,
                uow=uow,
            )
            to_txn = self._txns.create_transaction(
                account_id=to_account_id,
                txn_date=txn_date,
                amount=amount,
                txn_type="TRANSFER",
                transfer_group_id=transfer_group_id,
                description=description,
                uow=uow,
            )

            from_balance = self._account_balance_from_opening_and_txns(from_account_id, uow)
            to_balance = self._account_balance_from_opening_and_txns(to_account_id, uow)

            self._cache.set_bank_account_current_balance(from_account_id, from_balance, uow=uow)
            self._cache.set_bank_account_current_balance(to_account_id, to_balance, uow=uow)

        return from_txn, to_txn

    def execute_reversal(
        self,
        *,
        txn_id: int,
        reason: str,
    ) -> Transaction:
        """Reverse a posted transaction.

        The minimal implementation here handles the simplest reversal case:
        inverse the original signed amount on the same account, attach the
        reversal link, and recompute the account cache from posted ledger rows.
        """
        if not reason or not reason.strip():
            raise ValueError("reversal reason is required")

        with self._db.unit_of_work() as uow:
            original = self._txns.get_transaction(txn_id, uow=uow)
            if original is None:
                raise ValueError(f"transaction {txn_id} does not exist")
            if original.status != "POSTED":
                raise ValueError("only posted transactions can be reversed")

            reversal_amount = original.amount * Decimal("-1")
            description = (
                "Reversal of transfer"
                if original.txn_type == "TRANSFER"
                else f"Reversal of {original.description or 'transaction'}"
            )
            reversal_txn = self._txns.create_transaction(
                account_id=original.account_id,
                txn_date=date.today(),
                amount=reversal_amount,
                txn_type=original.txn_type,
                transfer_group_id=original.transfer_group_id,
                status="REVERSED",
                reversal_of_txn_id=txn_id,
                description=description,
                uow=uow,
            )

            self._txns.update_transaction_status(txn_id, "REVERSED", uow=uow)

            account_balance = self._account_balance_from_opening_and_txns(
                original.account_id, uow
            )
            self._cache.set_bank_account_current_balance(
                original.account_id, account_balance, uow=uow
            )

        return reversal_txn

    def get_account_balance(self, account_id: int) -> Decimal:
        """Return the canonical current balance for a bank account.

        The DDL marks `current_balance` as engine-derived cache data; the engine
        is the only layer that should recompute and set it from source
        transactions.
        """
        return self._account_balance_from_opening_and_txns(account_id)

    # ------------------------------------------------------------------
    # Family / member — plain CRUD, no financial meaning of its own.
    # Delegated here only so the API/UI layer never imports the repository.
    # ------------------------------------------------------------------

    def create_family(
        self, family_name: str, base_currency: Optional[str] = None
    ) -> Family:
        return self._family.create_family(family_name, base_currency)

    def get_family(self, family_id: int) -> Optional[Family]:
        return self._family.get_family(family_id)

    def list_families(self) -> List[Family]:
        return self._family.list_families()

    def create_family_member(
        self, family_id: int, name: str, relationship: str
    ) -> FamilyMember:
        return self._family.create_family_member(family_id, name, relationship)

    LIFE_STAGES = ("EARNING", "RETIRED", "DEPENDENT")
    SUCCESSION_ITEMS = ("NOMINEES_BANK", "NOMINEES_INVESTMENTS", "NOMINEES_INSURANCE", "WILL", "JOINT_HOLDING", "PAPERS")

    def update_member_profile(
        self, *, member_id: int, name: str, relationship: str, date_of_birth: Optional[date],
        life_stage: Optional[str], heir_member_id: Optional[int], retirement_age: Optional[int],
    ) -> FamilyMember:
        """A member's details for planning: date of birth, where they are in
        life (earning / retired / dependent), who inherits from them, and —
        for an earner — the age they mean to retire at."""
        member = self._family.get_family_member(member_id)
        if member is None:
            raise ValueError(f"family member {member_id} does not exist")
        if not (name or "").strip() or not (relationship or "").strip():
            raise ValueError("name and relationship are required")
        if date_of_birth is not None and date_of_birth > date.today():
            raise ValueError("date of birth cannot be in the future")
        if life_stage is not None and life_stage not in self.LIFE_STAGES:
            raise ValueError("choose earning, retired or dependent")
        if heir_member_id is not None:
            heir = self._family.get_family_member(heir_member_id)
            if heir is None or heir.family_id != member.family_id:
                raise ValueError("the heir must be a member of the same family")
            if heir_member_id == member_id:
                raise ValueError("a member cannot be their own heir")
        if retirement_age is not None and not 40 <= int(retirement_age) <= 80:
            raise ValueError("retirement age must be 40-80")
        return self._family.update_member_profile(
            member_id, name=name.strip(), relationship=relationship.strip(), date_of_birth=date_of_birth,
            life_stage=life_stage, heir_member_id=heir_member_id,
            retirement_age=None if retirement_age is None else int(retirement_age),
        )

    def set_succession_check(self, *, member_id: int, item: str, done: bool) -> None:
        if item not in self.SUCCESSION_ITEMS:
            raise ValueError("unknown checklist item")
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        self._succession.set_check(member_id, item, bool(done), date.today())

    def list_family_members(self, family_id: int) -> List[FamilyMember]:
        return self._family.list_family_members(family_id, active_only=True)

    # ------------------------------------------------------------------
    # Fixed deposits: create, read, and the FD-specific valuation strategy
    # ------------------------------------------------------------------

    def _current_value_for_asset(
        self, asset: Asset, as_of: date, uow: Optional[UnitOfWork] = None
    ) -> Decimal:
        """Dispatch to the right valuation strategy for `asset`'s category.

        FD is deterministic and computed live, as of `as_of` — see
        `fd_valuation`. MF is market-linked: units (summed live from
        `mf_transaction`) times the latest `asset_valuation` NAV. Every other
        category currently has no computed strategy of its own, so it falls
        back to the persisted `asset.current_value` cache, which today holds
        the schema default of 0 for all of them. Adding a category's own
        valuation later means adding one branch here, not touching the
        aggregation methods that call this.

        `uow` is threaded through to every read here so this also gives the
        right answer when called from inside an in-progress Unit of Work —
        e.g. `create_mutual_fund` computing an initial current_value from a
        transaction it just inserted but has not committed yet.
        """
        if asset.category == "FD":
            holding = self._assets.get_fd(asset.asset_id, uow=uow)
            return compute_fd_current_value(
                principal=holding.principal,
                interest_rate=holding.interest_rate,
                issue_date=holding.issue_date,
                maturity_date=holding.maturity_date,
                payout_type=holding.payout_type,
                maturity_value=holding.maturity_value,
                as_of=as_of,
            )
        if asset.category == "MF":
            units, _ = self._mf_units_and_avg_nav(asset.asset_id, uow=uow)
            valuation = self._valuations.get_latest_valuation(asset.asset_id, uow=uow)
            current_nav = valuation.market_value if valuation is not None else None
            return units * current_nav if current_nav is not None else _ZERO
        if asset.category in STATEMENT_VALUED_CATEGORIES:
            valuation = self._valuations.get_latest_valuation(asset.asset_id, uow=uow)
            return valuation.market_value if valuation is not None else _ZERO
        if asset.category == "GOLD":
            holding = self._assets.get_gold(asset.asset_id, uow=uow)
            valuation = self._valuations.get_latest_valuation(asset.asset_id, uow=uow)
            if valuation is None:
                return _ZERO
            return (holding.quantity_grams * valuation.market_value).quantize(_CENT)
        if asset.category == "STOCK":
            position = compute_stock_position(self._active_stock_txns(asset.asset_id, uow=uow))
            valuation = self._valuations.get_latest_valuation(asset.asset_id, uow=uow)
            if valuation is None:
                return _ZERO
            return (position.quantity * valuation.market_value).quantize(Decimal("0.01"))
        return asset.current_value

    def _fd_summary(self, asset: Asset, holding: FdHolding, as_of: date) -> FdSummary:
        current_value = compute_fd_current_value(
            principal=holding.principal,
            interest_rate=holding.interest_rate,
            issue_date=holding.issue_date,
            maturity_date=holding.maturity_date,
            payout_type=holding.payout_type,
            maturity_value=holding.maturity_value,
            as_of=as_of,
        )
        gain_loss = current_value - asset.invested_amount
        gain_loss_pct = (
            (gain_loss / asset.invested_amount * Decimal("100"))
            if asset.invested_amount != _ZERO
            else _ZERO
        )
        earned, total_interest = compute_fd_interest(
            principal=holding.principal,
            interest_rate=holding.interest_rate,
            issue_date=holding.issue_date,
            maturity_date=holding.maturity_date,
            payout_type=holding.payout_type,
            maturity_value=holding.maturity_value,
            as_of=as_of,
        )
        return FdSummary(
            asset_id=asset.asset_id,
            member_id=asset.member_id,
            name=asset.name,
            status=asset.status,
            bank_name=holding.bank_name,
            fd_number=holding.fd_number,
            principal=holding.principal,
            interest_rate=holding.interest_rate,
            issue_date=holding.issue_date,
            maturity_date=holding.maturity_date,
            payout_type=holding.payout_type,
            maturity_value=holding.maturity_value,
            current_value=current_value,
            gain_loss=gain_loss,
            gain_loss_pct=gain_loss_pct.quantize(Decimal("0.01")),
            payout_interest=compute_fd_payout_interest(
                principal=holding.principal,
                interest_rate=holding.interest_rate,
                payout_type=holding.payout_type,
            ),
            interest_earned_so_far=earned,
            interest_earned_pct=self._pct(earned, holding.principal),
            total_interest_by_maturity=total_interest,
            total_interest_pct=self._pct(total_interest, holding.principal),
            amount_at_maturity=holding.principal + total_interest,
        )

    def create_fd(
        self,
        *,
        member_id: int,
        bank_name: str,
        fd_number: str,
        principal: Money,
        interest_rate: Optional[Money],
        issue_date: date,
        maturity_date: date,
        payout_type: str,
        maturity_value: Optional[Money] = None,
        name: Optional[str] = None,
        institution: Optional[str] = None,
        ownership: Optional[Sequence[Tuple[int, Money]]] = None,
    ) -> FdSummary:
        """Open a new FD: persist it, compute its value, cache it — atomically.

        `name`/`institution` default from `bank_name`/`fd_number` when not
        given, so the common case (one owner, no separate display name) needs
        no redundant input — a convenience that belongs here, at the Engine's
        API-facing edge, not in the repository's plain persistence mapping.
        `ownership` defaults to the member owning it outright; joint FDs are
        supported by the data model (as for every other asset type) but this
        block's own UI only offers single-owner creation — see the
        assessment for why.
        """
        principal = _decimal(principal)
        interest_rate = self._fd_rate(principal, interest_rate, issue_date, maturity_date,
                                      payout_type, maturity_value)
        display_name = name or f"{bank_name} FD {fd_number}"
        display_institution = institution or bank_name
        owners = ownership if ownership is not None else [(member_id, Decimal("100"))]

        with self._db.unit_of_work() as uow:
            asset, holding = self._assets.create_fd(
                member_id,
                display_name,
                display_institution,
                principal,
                issue_date,
                owners,
                fd_number=fd_number,
                bank_name=bank_name,
                interest_rate=interest_rate,
                maturity_date=maturity_date,
                payout_type=payout_type,
                maturity_value=maturity_value,
                uow=uow,
            )
            current_value = compute_fd_current_value(
                principal=holding.principal,
                interest_rate=holding.interest_rate,
                issue_date=holding.issue_date,
                maturity_date=holding.maturity_date,
                payout_type=holding.payout_type,
                maturity_value=holding.maturity_value,
                as_of=date.today(),
            )
            self._cache.set_asset_current_value(asset.asset_id, current_value, uow=uow)

        return self._fd_summary(asset, holding, date.today())

    def update_fd(
        self,
        *,
        asset_id: int,
        member_id: int,
        bank_name: str,
        fd_number: str,
        principal: Money,
        interest_rate: Optional[Money],
        issue_date: date,
        maturity_date: date,
        payout_type: str,
        maturity_value: Optional[Money] = None,
    ) -> FdSummary:
        """Correct a mistake in an FD: every term can change, including the
        owner. Checked by the same schema rules as `create_fd`, and its
        cached invested/current value is recomputed — atomically."""
        principal = _decimal(principal)
        interest_rate = self._fd_rate(principal, interest_rate, issue_date, maturity_date,
                                      payout_type, maturity_value)
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "FD":
                raise ValueError(f"FD {asset_id} does not exist")
            if self._family.get_family_member(member_id) is None:
                raise ValueError(f"family member {member_id} does not exist")
            old = self._assets.get_fd(asset_id, uow=uow)
            # Keep a custom name; regenerate the default one from the new terms.
            name = (
                f"{bank_name} FD {fd_number}"
                if asset.name == f"{old.bank_name} FD {old.fd_number}"
                else asset.name
            )
            institution = bank_name if asset.institution == old.bank_name else asset.institution
            updated_asset, holding = self._assets.update_fd(
                asset_id,
                member_id=member_id, name=name, institution=institution,
                fd_number=fd_number, bank_name=bank_name, principal=principal,
                interest_rate=interest_rate, issue_date=issue_date,
                maturity_date=maturity_date, payout_type=payout_type,
                maturity_value=maturity_value, uow=uow,
            )
            self._cache.set_asset_invested_amount(asset_id, principal, uow=uow)
            self._cache.set_asset_current_value(
                asset_id, self._current_value_for_asset(updated_asset, date.today(), uow=uow), uow=uow
            )
        return self._fd_summary(self._assets.get_asset(asset_id), holding, date.today())

    def delete_fd(self, asset_id: int) -> None:
        """Remove an FD entered by mistake (e.g. twice), with its history."""
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "FD":
                raise ValueError(f"FD {asset_id} does not exist")
            self._planning.clear_asset(asset_id, uow=uow)

            self._assets.delete_asset(asset_id, uow=uow)

    @staticmethod
    def _fd_rate(principal, interest_rate, issue_date, maturity_date, payout_type, maturity_value):
        """The FD's interest rate: as given, or — for a cumulative FD entered
        with only its maturity amount — worked out from that amount."""
        if interest_rate not in (None, ""):
            return interest_rate
        if payout_type != CUMULATIVE_PAYOUT_TYPE:
            raise ValueError(
                "enter the interest rate: an FD that pays interest out matures at its principal, "
                "so the rate cannot be worked out from the maturity amount"
            )
        if maturity_value in (None, ""):
            raise ValueError("enter either the interest rate or the maturity amount")
        return derive_fd_interest_rate(
            principal=_decimal(principal), maturity_value=_decimal(maturity_value),
            issue_date=issue_date, maturity_date=maturity_date,
        )

    def list_fds(self, member_id: int) -> List[FdSummary]:
        """Return `member_id`'s FDs, with a freshly computed current value."""
        today = date.today()
        summaries = []
        for asset in self._assets.list_assets(member_id):
            if asset.category != "FD":
                continue
            holding = self._assets.get_fd(asset.asset_id)
            summaries.append(self._fd_summary(asset, holding, today))
        return summaries

    # ------------------------------------------------------------------
    # Insurance policies
    # ------------------------------------------------------------------

    @staticmethod
    def _premiums_end(policy: InsurancePolicy) -> Optional[date]:
        """No premium falls on or after this date: the maturity date, or —
        for a limited-pay policy — the end of its premium-paying term,
        whichever comes first."""
        end = policy.maturity_date
        if policy.premium_paying_term_years:
            years = int(policy.premium_paying_term_years)
            try:
                term_end = policy.start_date.replace(year=policy.start_date.year + years)
            except ValueError:  # 29 Feb
                term_end = policy.start_date.replace(year=policy.start_date.year + years, day=28)
            end = term_end if end is None else min(end, term_end)
        return end

    @staticmethod
    def _check_paying_term(years: Optional[Money], start_date: date, maturity_date: Optional[date]) -> Optional[int]:
        if years in (None, ""):
            return None
        whole = int(_decimal(years))
        if not 1 <= whole <= 100:
            raise ValueError("premium-paying term must be 1 to 100 years")
        if maturity_date is not None and whole * 365 > (maturity_date - start_date).days + 182:
            raise ValueError("the premium-paying term cannot be longer than the policy term")
        return whole

    def _insurance_summary(self, policy: InsurancePolicy, as_of: date) -> InsurancePolicySummary:
        schedule = compute_premium_schedule(
            start_date=policy.start_date,
            premium_amount=policy.premium_amount,
            premium_frequency=policy.premium_frequency,
            maturity_date=self._premiums_end(policy),
            status=policy.status,
            as_of=as_of,
        )
        return InsurancePolicySummary(
            policy_id=policy.policy_id,
            member_id=policy.member_id,
            policy_type=policy.policy_type,
            insurer=policy.insurer,
            policy_number=policy.policy_number,
            sum_assured=policy.sum_assured,
            premium_amount=policy.premium_amount,
            premium_frequency=policy.premium_frequency,
            start_date=policy.start_date,
            maturity_date=policy.maturity_date,
            current_surrender_value=policy.current_surrender_value,
            status=policy.status,
            installments_due_so_far=schedule.installments_due_so_far,
            next_due_date=schedule.next_due_date,
            total_premium_due_to_date=schedule.total_premium_due_to_date,
            accrued_bonus=policy.accrued_bonus,
            is_savings_policy=policy.policy_type in SAVINGS_POLICY_TYPES,
            plan_name=policy.plan_name,
            nominee=policy.nominee,
            premium_paying_term_years=policy.premium_paying_term_years,
            premiums_end_date=self._premiums_end(policy) if policy.premium_paying_term_years else None,
            review=review_policy(
                policy_type=policy.policy_type, insurer=policy.insurer, plan_name=policy.plan_name,
                sum_assured=policy.sum_assured, premium_amount=policy.premium_amount,
                premium_frequency=policy.premium_frequency, start_date=policy.start_date,
                maturity_date=policy.maturity_date, nominee=policy.nominee, status=policy.status,
            ),
        )

    def create_insurance_policy(
        self,
        *,
        member_id: int,
        policy_type: str,
        insurer: str,
        policy_number: str,
        premium_amount: Money,
        premium_frequency: str,
        start_date: date,
        sum_assured: Optional[Money] = None,
        maturity_date: Optional[date] = None,
        current_surrender_value: Optional[Money] = None,
        accrued_bonus: Optional[Money] = None,
        plan_name: Optional[str] = None,
        nominee: Optional[str] = None,
        premium_paying_term_years: Optional[Money] = None,
    ) -> InsurancePolicySummary:
        """Add a policy and return it with its premium schedule computed.

        A single-table insert (unlike `create_fd`, there is no separate
        detail table to keep in step), so no explicit `UnitOfWork` is needed
        here — `InsurancePolicyRepository.create_policy` is already atomic on
        its own.
        """
        policy = self._insurance.create_policy(
            member_id, policy_type, insurer, policy_number, premium_amount,
            premium_frequency, start_date, sum_assured=sum_assured,
            maturity_date=maturity_date, current_surrender_value=current_surrender_value,
            accrued_bonus=accrued_bonus, plan_name=(plan_name or "").strip() or None,
            nominee=(nominee or "").strip() or None,
            premium_paying_term_years=self._check_paying_term(premium_paying_term_years, start_date, maturity_date),
        )
        return self._insurance_summary(policy, date.today())

    def update_insurance_values(
        self,
        policy_id: int,
        *,
        current_surrender_value: Optional[Money],
        accrued_bonus: Optional[Money],
    ) -> InsurancePolicySummary:
        """Record the latest surrender value and accrued bonus from the
        insurer's statement. Both replace the previous values; `None` clears."""
        for label, value in (("surrender value", current_surrender_value),
                             ("accrued bonus", accrued_bonus)):
            if value is not None and _decimal(value) < _ZERO:
                raise ValueError(f"{label} cannot be negative")
        policy = self._insurance.update_policy_values(
            policy_id,
            current_surrender_value=current_surrender_value,
            accrued_bonus=accrued_bonus,
        )
        if policy is None:
            raise ValueError(f"insurance policy {policy_id} does not exist")
        return self._insurance_summary(policy, date.today())

    def update_insurance_policy(
        self,
        *,
        policy_id: int,
        member_id: int,
        policy_type: str,
        insurer: str,
        policy_number: str,
        premium_amount: Money,
        premium_frequency: str,
        start_date: date,
        sum_assured: Optional[Money] = None,
        maturity_date: Optional[date] = None,
        status: str = "ACTIVE",
        plan_name: Optional[str] = None,
        nominee: Optional[str] = None,
        premium_paying_term_years: Optional[Money] = None,
    ) -> InsurancePolicySummary:
        """Correct a mistake in a policy — every term, including who is
        insured and its status (e.g. MATURED / LAPSED). Checked by the same
        schema rules as a new policy; surrender value and bonus are kept."""
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        if not (insurer or "").strip() or not (policy_number or "").strip():
            raise ValueError("insurer and policy number are required")
        policy = self._insurance.update_policy(
            policy_id, member_id=member_id, policy_type=policy_type, insurer=insurer.strip(),
            policy_number=policy_number.strip(), premium_amount=premium_amount,
            premium_frequency=premium_frequency, start_date=start_date,
            sum_assured=sum_assured, maturity_date=maturity_date, status=status,
            plan_name=(plan_name or "").strip() or None, nominee=(nominee or "").strip() or None,
            premium_paying_term_years=self._check_paying_term(premium_paying_term_years, start_date, maturity_date),
        )
        if policy is None:
            raise ValueError(f"insurance policy {policy_id} does not exist")
        return self._insurance_summary(policy, date.today())

    def estimate_surrender_value(
        self,
        policy_id: int,
        *,
        as_of: Optional[date] = None,
        premium_paying_term_years: Optional[int] = None,
        installments_paid: Optional[int] = None,
        gsv_factor_pct: Optional[Money] = None,
        bonus_cash_value_factor: Optional[Money] = None,
        ssv_factor: Optional[Money] = None,
        discount_rate_pct: Optional[Money] = None,
        fund_value: Optional[Money] = None,
        accrued_bonus: Optional[Money] = None,
    ) -> SurrenderEstimate:
        """Estimate what surrendering `policy_id` would pay today — see
        `surrender_value`. Read-only: saving the figure is a separate,
        deliberate `update_insurance_values` call.

        Anything not given defaults from the policy itself: the premium-
        paying term to the policy term, installments paid to the schedule's
        installments due so far, the bonus to the recorded accrued bonus.
        """
        policy = self._insurance.get_policy(policy_id)
        if policy is None:
            raise ValueError(f"insurance policy {policy_id} does not exist")
        if policy.policy_type not in SURRENDER_POLICY_TYPES:
            raise ValueError(
                f"{policy.policy_type} policies have no surrender value (pure protection cover)"
            )
        as_of = as_of or date.today()
        opt = lambda v: None if v in (None, "") else _decimal(v)  # noqa: E731
        annual_premium = compute_annual_premium(policy.premium_amount, policy.premium_frequency)

        if policy.policy_type == "ULIP":
            if fund_value in (None, ""):
                raise ValueError("enter the fund value from your ULIP statement")
            return estimate_ulip(
                start_date=policy.start_date, as_of=as_of,
                fund_value=_decimal(fund_value), annual_premium=annual_premium,
            )

        summary = self._insurance_summary(policy, as_of)
        per_year = int(compute_annual_premium(Decimal(1), policy.premium_frequency))
        if premium_paying_term_years in (None, "") and policy.premium_paying_term_years:
            premium_paying_term_years = policy.premium_paying_term_years
        if premium_paying_term_years in (None, ""):
            if policy.maturity_date is None:
                raise ValueError("enter the premium-paying term (the policy has no maturity date)")
            premium_paying_term_years = max(
                (policy.maturity_date - policy.start_date).days // 365, 1
            )
        return estimate_traditional(
            start_date=policy.start_date,
            maturity_date=policy.maturity_date,
            as_of=as_of,
            premium_amount=policy.premium_amount,
            installments_per_year=per_year,
            premium_paying_term_years=int(premium_paying_term_years),
            installments_paid=(
                summary.installments_due_so_far
                if installments_paid in (None, "") else int(installments_paid)
            ),
            sum_assured=policy.sum_assured,
            accrued_bonus=(
                (policy.accrued_bonus or _ZERO) if accrued_bonus in (None, "") else _decimal(accrued_bonus)
            ),
            gsv_factor_pct=opt(gsv_factor_pct),
            bonus_cash_value_factor=opt(bonus_cash_value_factor),
            ssv_factor=opt(ssv_factor),
            discount_rate_pct=opt(discount_rate_pct),
        )

    def list_insurance_policies(self, member_id: int) -> List[InsurancePolicySummary]:
        """Return `member_id`'s policies, with a freshly computed schedule."""
        today = date.today()
        return [
            self._insurance_summary(policy, today)
            for policy in self._insurance.list_policies(member_id)
        ]

    # ------------------------------------------------------------------
    # Mutual funds: transaction-based holdings (see mf_transaction's DDL
    # comment for why this doesn't use investment_lot/"transaction")
    # ------------------------------------------------------------------

    def _mf_units_and_avg_nav(
        self, asset_id: int, uow: Optional[UnitOfWork] = None
    ) -> Tuple[Decimal, Optional[Decimal]]:
        """Sum units and the units-weighted average NAV across `asset_id`'s
        ACTIVE transactions. `avg_nav` is `None` when there are none yet."""
        transactions = [
            t for t in self._mf_txns.list_transactions(asset_id, uow=uow) if t.status == "ACTIVE"
        ]
        total_units = sum((t.units for t in transactions), _ZERO)
        if total_units == _ZERO:
            return _ZERO, None
        weighted_cost = sum((t.units * t.nav for t in transactions), _ZERO)
        return total_units, (weighted_cost / total_units).quantize(_NAV_PRECISION)

    def _mf_invested_amount(self, asset_id: int, uow: Optional[UnitOfWork] = None) -> Decimal:
        """Sum of what was actually paid across ACTIVE transactions — not
        `units * avg_nav`, which could drift from it by rounding."""
        transactions = [
            t for t in self._mf_txns.list_transactions(asset_id, uow=uow) if t.status == "ACTIVE"
        ]
        return sum((t.amount for t in transactions), _ZERO)

    def _mf_xirr(
        self, transactions: Sequence[MfTransaction], current_value: Decimal, as_of: date
    ) -> Optional[Decimal]:
        active = [t for t in transactions if t.status == "ACTIVE"]
        if any(t.is_opening_balance for t in active):
            return None  # purchase dates before an imported statement are unknown
        cash_flows = [(t.txn_date, -t.amount) for t in active]
        if current_value > _ZERO:
            cash_flows.append((as_of, current_value))
        return compute_xirr(cash_flows)

    def _record_valuation_from_transaction(
        self, asset_id: int, txn_date: date, nav: Decimal, uow: UnitOfWork
    ) -> None:
        """Best-effort: also treat a transaction's own NAV as a data point in
        the asset's valuation history, since it is a real observed price on a
        real date. Silently skipped if a valuation already exists for that
        exact date (the schema allows only one non-correction valuation per
        asset per date) — an explicit valuation for that day, if any, is left
        standing rather than overwritten."""
        try:
            self._valuations.record_valuation(
                asset_id, txn_date, nav, "MANUAL", uow=uow
            )
        except RepositoryIntegrityError:
            pass

    def _mf_summary(
        self,
        asset: Asset,
        holding: MutualFundHolding,
        transactions: List[MfTransaction],
        as_of: date,
    ) -> MfSummary:
        active = [t for t in transactions if t.status == "ACTIVE"]
        total_units = sum((t.units for t in active), _ZERO)
        avg_nav = (
            (sum((t.units * t.nav for t in active), _ZERO) / total_units).quantize(_NAV_PRECISION)
            if total_units != _ZERO
            else None
        )
        invested_amount = sum((t.amount for t in active), _ZERO)

        valuation = self._valuations.get_latest_valuation(asset.asset_id)
        current_nav = valuation.market_value if valuation is not None else None
        current_value = total_units * current_nav if current_nav is not None else _ZERO

        gain_loss = current_value - invested_amount
        gain_loss_pct = (
            (gain_loss / invested_amount * Decimal("100")) if invested_amount != _ZERO else _ZERO
        )

        return MfSummary(
            asset_id=asset.asset_id,
            member_id=asset.member_id,
            name=asset.name,
            status=asset.status,
            amc=holding.amc,
            scheme_name=holding.scheme_name,
            folio_number=holding.folio_number,
            isin=holding.isin,
            sip_enabled=holding.sip_enabled,
            sip_amount=holding.sip_amount,
            units=total_units,
            avg_nav=avg_nav,
            current_nav=current_nav,
            current_nav_source=valuation.source if valuation is not None else None,
            current_nav_date=valuation.valuation_date if valuation is not None else None,
            invested_amount=invested_amount,
            current_value=current_value,
            gain_loss=gain_loss,
            gain_loss_pct=gain_loss_pct.quantize(Decimal("0.01")),
            xirr=self._mf_xirr(active, current_value, as_of),
            transactions=tuple(transactions),
            sip_invested=sum((t.amount for t in active if t.txn_type == "SIP"), _ZERO),
            lumpsum_invested=sum((t.amount for t in active if t.txn_type == "LUMPSUM"), _ZERO),
            investment_type=self._mf_investment_type(holding, active),
            sip_status=(
                "RUNNING" if holding.sip_enabled
                else "STOPPED" if any(t.txn_type == "SIP" for t in active) else None
            ),
            sip_date=holding.sip_date,
            needs_purchase_details=any(t.is_opening_balance for t in active),
            opening_amount=next((t.amount for t in active if t.is_opening_balance), None),
            broker=holding.broker,
            kinds=self._mf_kinds(holding, active),
            **self._mf_parts(active, current_nav, as_of),
        )

    def _mf_parts(self, active: Sequence[MfTransaction], nav: Optional[Decimal], as_of: date) -> dict:
        """Units, value and XIRR of a fund's SIP purchases and of its lumpsums."""
        out = {}
        for kind, prefix in (("SIP", "sip"), ("LUMPSUM", "lumpsum")):
            rows = [t for t in active if t.txn_type == kind]
            units = sum((t.units for t in rows), _ZERO)
            value = (units * nav).quantize(_CENT) if nav is not None else _ZERO
            out[f"{prefix}_units"] = units
            out[f"{prefix}_value"] = value
            out[f"{prefix}_xirr"] = (
                self._mf_xirr(rows, value, as_of) if rows and not any(t.is_opening_balance for t in active) else None
            )
        return out

    @staticmethod
    def _mf_kinds(holding: MutualFundHolding, active: Sequence[MfTransaction]) -> Tuple[str, ...]:
        if any(t.is_opening_balance for t in active):
            return ("UNSET", "SIP_RUNNING") if holding.sip_enabled else ("UNSET",)
        kinds = []
        if holding.sip_enabled:
            kinds.append("SIP_RUNNING")
        elif any(t.txn_type == "SIP" for t in active):
            kinds.append("SIP_STOPPED")
        if any(t.txn_type == "LUMPSUM" for t in active):
            kinds.append("LUMPSUM")
        return tuple(kinds)

    @staticmethod
    def _mf_investment_type(holding: MutualFundHolding, active: Sequence[MfTransaction]) -> str:
        has_sip = holding.sip_enabled or any(t.txn_type == "SIP" for t in active)
        has_lumpsum = any(t.txn_type == "LUMPSUM" for t in active)
        if has_sip and has_lumpsum:
            return "SIP + LUMPSUM"
        return "SIP" if has_sip else "LUMPSUM"

    def create_mutual_fund(
        self,
        *,
        member_id: int,
        amc: str,
        scheme_name: str,
        folio_number: str,
        initial_txn_date: date,
        initial_txn_type: str,
        initial_units: Money,
        initial_nav: Money,
        initial_amount: Money,
        isin: Optional[str] = None,
        sip_enabled: bool = False,
        sip_amount: Optional[Money] = None,
        sip_date: Optional[int] = None,
        name: Optional[str] = None,
        institution: Optional[str] = None,
        ownership: Optional[Sequence[Tuple[int, Money]]] = None,
        broker: Optional[str] = None,
    ) -> MfSummary:
        """Open a new MF holding with its first transaction — atomically.

        A holding with zero transactions has zero units and is meaningless
        to value, so (mirroring `create_fd`'s "everything in one call"
        shape) this always creates the holding together with its first
        purchase, rather than leaving an empty shell to fill in later.
        `name`/`institution` default from `scheme_name`/`amc`, the same
        convenience `create_fd` already offers.
        """
        display_name = name or scheme_name
        display_institution = institution or amc
        owners = ownership if ownership is not None else [(member_id, Decimal("100"))]

        with self._db.unit_of_work() as uow:
            asset, holding = self._assets.create_mutual_fund(
                member_id,
                display_name,
                display_institution,
                0,  # invested_amount: overwritten below once the transaction is known
                initial_txn_date,
                owners,
                amc=amc,
                scheme_name=scheme_name,
                folio_number=folio_number,
                isin=isin,
                sip_enabled=sip_enabled,
                sip_amount=sip_amount,
                sip_date=sip_date,
                broker=(broker or "").strip() or None,
                uow=uow,
            )
            txn = self._mf_txns.create_transaction(
                asset.asset_id, initial_txn_date, initial_txn_type,
                initial_units, initial_nav, initial_amount, uow=uow,
            )
            self._record_valuation_from_transaction(
                asset.asset_id, initial_txn_date, txn.nav, uow
            )

            units, avg_nav = self._mf_units_and_avg_nav(asset.asset_id, uow=uow)
            invested_amount = self._mf_invested_amount(asset.asset_id, uow=uow)
            self._cache.set_mf_units_and_avg_nav(asset.asset_id, units, avg_nav, uow=uow)
            self._cache.set_asset_invested_amount(asset.asset_id, invested_amount, uow=uow)

            current_value = self._current_value_for_asset(
                self._assets.get_asset(asset.asset_id, uow=uow), initial_txn_date, uow=uow
            )
            self._cache.set_asset_current_value(asset.asset_id, current_value, uow=uow)

        return self._mf_summary(
            self._assets.get_asset(asset.asset_id),
            self._assets.get_mutual_fund(asset.asset_id),
            [txn],
            date.today(),
        )

    def record_mf_transaction(
        self,
        *,
        asset_id: int,
        txn_date: date,
        txn_type: str,
        units: Money,
        nav: Money,
        amount: Money,
        description: Optional[str] = None,
    ) -> MfSummary:
        """Add a further SIP/lumpsum purchase to an existing MF holding.

        Recomputes units/avg_nav/invested_amount from the full transaction
        history and re-persists them, atomically with the new transaction
        row — the "log this month's SIP" action.
        """
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "MF":
                raise ValueError(f"MF holding {asset_id} does not exist")

            self._mf_txns.create_transaction(
                asset_id, txn_date, txn_type, units, nav, amount,
                description=description, uow=uow,
            )
            self._record_valuation_from_transaction(asset_id, txn_date, _decimal(nav), uow)

            new_units, new_avg_nav = self._mf_units_and_avg_nav(asset_id, uow=uow)
            new_invested = self._mf_invested_amount(asset_id, uow=uow)
            self._cache.set_mf_units_and_avg_nav(asset_id, new_units, new_avg_nav, uow=uow)
            self._cache.set_asset_invested_amount(asset_id, new_invested, uow=uow)

            current_value = self._current_value_for_asset(asset, date.today(), uow=uow)
            self._cache.set_asset_current_value(asset_id, current_value, uow=uow)

        return self._mf_summary(
            self._assets.get_asset(asset_id),
            self._assets.get_mutual_fund(asset_id),
            self._mf_txns.list_transactions(asset_id),
            date.today(),
        )

    def record_mf_valuation(
        self, *, asset_id: int, valuation_date: date, nav: Money, source: str = "MANUAL"
    ) -> MfSummary:
        """Record a NAV observation for `asset_id` and refresh its cached
        current value from it — the manual "update current NAV" action.

        If a valuation already exists for `valuation_date` (most often one a
        transaction auto-seeded — see `_record_valuation_from_transaction`),
        this deliberate, user-initiated entry is recorded as a correction
        (the schema's own mechanism for exactly this) rather than being
        rejected or silently dropped: `get_latest_valuation` already prefers
        a same-date correction entered after the original.
        """
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "MF":
                raise ValueError(f"MF holding {asset_id} does not exist")

            try:
                self._valuations.record_valuation(
                    asset_id, valuation_date, nav, source, uow=uow
                )
            except RepositoryIntegrityError:
                self._valuations.record_valuation(
                    asset_id, valuation_date, nav, source, is_correction=True, uow=uow
                )
            current_value = self._current_value_for_asset(asset, date.today(), uow=uow)
            self._cache.set_asset_current_value(asset_id, current_value, uow=uow)

        return self._mf_summary(
            self._assets.get_asset(asset_id),
            self._assets.get_mutual_fund(asset_id),
            self._mf_txns.list_transactions(asset_id),
            date.today(),
        )

    def list_mfs(self, member_id: int) -> List[MfSummary]:
        """Return `member_id`'s MF holdings, freshly computed."""
        today = date.today()
        summaries = []
        for asset in self._assets.list_assets(member_id):
            if asset.category != "MF":
                continue
            holding = self._assets.get_mutual_fund(asset.asset_id)
            transactions = self._mf_txns.list_transactions(asset.asset_id)
            summaries.append(self._mf_summary(asset, holding, transactions, today))
        return summaries

    def _mf_by_broker(self, mfs: Sequence[MfSummary], today: date) -> Tuple[MfBrokerTotal, ...]:
        groups: Dict[Optional[str], List[MfSummary]] = {}
        for mf in mfs:
            groups.setdefault(mf.broker, []).append(mf)
        totals = []
        for broker, funds in groups.items():
            invested = sum((f.invested_amount for f in funds), _ZERO)
            current = sum((f.current_value for f in funds), _ZERO)
            flows: List[Tuple[date, Decimal]] = []
            value = _ZERO
            excluded = 0
            for f in funds:
                active = [t for t in f.transactions if t.status == "ACTIVE"]
                if any(t.is_opening_balance for t in active):
                    excluded += 1
                    continue
                value += f.current_value
                flows.extend((t.txn_date, -t.amount) for t in active)
            if value > _ZERO:
                flows.append((today, value))
            totals.append(MfBrokerTotal(
                broker=broker, funds=len(funds), invested=invested, current_value=current,
                gain_loss=current - invested, gain_loss_pct=self._pct(current - invested, invested),
                xirr=compute_xirr(flows), xirr_excluded_count=excluded,
            ))
        totals.sort(key=lambda b: (b.broker is None, -b.current_value))
        return tuple(totals)

    def _mf_by_kind(self, mfs: Sequence[MfSummary]) -> Tuple[MfKindTotal, ...]:
        totals = []
        for kind in ("SIP_RUNNING", "SIP_STOPPED", "LUMPSUM", "UNSET"):
            funds = [f for f in mfs if kind in f.kinds]
            invested = sum((f.invested_amount for f in funds), _ZERO)
            current = sum((f.current_value for f in funds), _ZERO)
            totals.append(MfKindTotal(
                kind=kind, funds=len(funds), invested=invested, current_value=current,
                gain_loss=current - invested, gain_loss_pct=self._pct(current - invested, invested),
                monthly_sip=sum((f.sip_amount or _ZERO for f in funds if f.sip_enabled), _ZERO),
            ))
        return tuple(totals)

    def set_mf_broker(self, *, asset_id: int, broker: Optional[str]) -> MfSummary:
        """Set the broker / platform a fund is held through (blank = not set)."""
        name = (broker or "").strip() or None
        if name is not None and len(name) > 60:
            raise ValueError("broker name is too long")
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "MF":
                raise ValueError(f"MF holding {asset_id} does not exist")
            self._assets.update_mf_broker(asset_id, name, uow=uow)
        return self._mf_summary(
            self._assets.get_asset(asset_id), self._assets.get_mutual_fund(asset_id),
            self._mf_txns.list_transactions(asset_id), date.today(),
        )

    # -- correcting a fund by hand ---------------------------------------

    def _require_mf(self, asset_id: int, uow: UnitOfWork) -> Asset:
        asset = self._assets.get_asset(asset_id, uow=uow)
        if asset is None or asset.category != "MF":
            raise ValueError(f"MF holding {asset_id} does not exist")
        return asset

    def _fresh_mf(self, asset_id: int) -> MfSummary:
        return self._mf_summary(
            self._assets.get_asset(asset_id), self._assets.get_mutual_fund(asset_id),
            self._mf_txns.list_transactions(asset_id), date.today(),
        )

    def update_mf_holding(
        self, *, asset_id: int, member_id: int, scheme_name: str, folio_number: str,
        broker: Optional[str] = None,
    ) -> MfSummary:
        """Correct a fund's owner, name, folio or broker."""
        name = (scheme_name or "").strip()
        folio = (folio_number or "").strip()
        if not name:
            raise ValueError("enter the scheme name")
        if not folio:
            raise ValueError("enter the folio number")
        broker_name = (broker or "").strip() or None
        with self._db.unit_of_work() as uow:
            asset = self._require_mf(asset_id, uow)
            owner = self._family.get_family_member(member_id, uow=uow)
            current = self._family.get_family_member(asset.member_id, uow=uow)
            if owner is None or current is None or owner.family_id != current.family_id:
                raise ValueError("the owner must be a member of the same family")
            self._assets.update_mutual_fund(
                asset_id, member_id=member_id, scheme_name=name, folio_number=folio,
                broker=broker_name, uow=uow,
            )
        return self._fresh_mf(asset_id)

    def update_mf_transaction(
        self, *, mf_txn_id: int, txn_date: date, txn_type: str, units: Money, amount: Money,
    ) -> MfSummary:
        """Correct one purchase: its date, SIP / lumpsum, units and amount
        (the NAV is amount / units). A CAS opening balance stays one."""
        if txn_type not in ("SIP", "LUMPSUM"):
            raise ValueError("type must be SIP or LUMPSUM")
        units_d, amount_d = _decimal(units), _decimal(amount)
        if units_d <= _ZERO or amount_d <= _ZERO:
            raise ValueError("units and amount must be positive")
        if txn_date > date.today():
            raise ValueError("the date cannot be in the future")
        with self._db.unit_of_work() as uow:
            txn = self._mf_txns.get_transaction(mf_txn_id, uow=uow)
            if txn is None:
                raise ValueError(f"MF transaction {mf_txn_id} does not exist")
            self._require_mf(txn.asset_id, uow)
            self._mf_txns.update_transaction(
                mf_txn_id, txn_date=txn_date, txn_type=txn_type,
                nav=(amount_d / units_d).quantize(_NAV_PRECISION), amount=amount_d,
                description=txn.description, is_opening_balance=txn.is_opening_balance,
                units=units_d, uow=uow,
            )
            self._refresh_mf_caches(txn.asset_id, uow)
        return self._fresh_mf(txn.asset_id)

    def delete_mf_transaction(self, mf_txn_id: int) -> MfSummary:
        """Remove one purchase entered by mistake (not the fund's last one —
        delete the fund instead)."""
        with self._db.unit_of_work() as uow:
            txn = self._mf_txns.get_transaction(mf_txn_id, uow=uow)
            if txn is None:
                raise ValueError(f"MF transaction {mf_txn_id} does not exist")
            self._require_mf(txn.asset_id, uow)
            active = [t for t in self._mf_txns.list_transactions(txn.asset_id, uow=uow) if t.status == "ACTIVE"]
            if len(active) <= 1:
                raise ValueError("this is the fund's only purchase — delete the whole fund instead")
            self._mf_txns.delete_transaction(mf_txn_id, uow=uow)
            self._refresh_mf_caches(txn.asset_id, uow)
        return self._fresh_mf(txn.asset_id)

    def set_mf_current_figures(
        self, *, asset_id: int, units: Money, invested: Money, as_of: Optional[date] = None,
    ) -> MfSummary:
        """Bring a fund to the units and amount invested your fund app shows
        today — e.g. after a CAS that is a month old.

        While the fund still has a CAS opening balance (dates unknown), the
        difference goes into that row. Otherwise the difference is added as
        a purchase on `as_of` (a SIP if one is running); a smaller figure
        than now means a redemption, which is not tracked yet — edit the
        purchases instead."""
        as_of = as_of or date.today()
        units_d, invested_d = _decimal(units), _decimal(invested)
        if units_d <= _ZERO or invested_d <= _ZERO:
            raise ValueError("units and amount invested must be positive")
        with self._db.unit_of_work() as uow:
            self._require_mf(asset_id, uow)
            holding = self._assets.get_mutual_fund(asset_id, uow=uow)
            active = [t for t in self._mf_txns.list_transactions(asset_id, uow=uow) if t.status == "ACTIVE"]
            d_units = units_d - sum((t.units for t in active), _ZERO)
            d_amount = invested_d - sum((t.amount for t in active), _ZERO)
            if d_units == _ZERO and d_amount == _ZERO:
                return self._fresh_mf(asset_id)
            opening = next((t for t in active if t.is_opening_balance), None)
            if opening is not None:
                new_units, new_amount = opening.units + d_units, opening.amount + d_amount
                if new_units <= _ZERO or new_amount <= _ZERO:
                    raise ValueError("those figures are below what was bought after the statement; check them")
                self._mf_txns.update_transaction(
                    opening.mf_txn_id, txn_date=opening.txn_date, txn_type=opening.txn_type,
                    nav=(new_amount / new_units).quantize(_NAV_PRECISION), amount=new_amount,
                    description=opening.description, is_opening_balance=True, units=new_units, uow=uow,
                )
            else:
                if d_units <= _ZERO or d_amount <= _ZERO:
                    raise ValueError(
                        "those figures are lower than the purchases recorded — redemptions are not tracked "
                        "yet, so correct or delete the purchases below instead"
                    )
                self._mf_txns.create_transaction(
                    asset_id, as_of, "SIP" if holding.sip_enabled else "LUMPSUM", d_units,
                    (d_amount / d_units).quantize(_NAV_PRECISION), d_amount,
                    description=HAND_TOP_UP, uow=uow,
                )
            self._refresh_mf_caches(asset_id, uow)
        return self._fresh_mf(asset_id)

    def delete_mf(self, asset_id: int) -> None:
        """Remove a fund and all its purchases and NAV history."""
        with self._db.unit_of_work() as uow:
            self._require_mf(asset_id, uow)
            self._mf_txns.delete_for_asset(asset_id, uow=uow)
            self._planning.clear_asset(asset_id, uow=uow)

            self._assets.delete_asset(asset_id, uow=uow)

    def get_mf_portfolio(
        self, family_id: int, member_id: Optional[int] = None
    ) -> MfPortfolio:
        """Every MF holding in the family, or only `member_id`'s, with
        totals and a combined XIRR.

        Only ACTIVE holdings count toward the totals, matching
        `get_member_investment_summary`. `member_id` must belong to
        `family_id`.
        """
        members = self._portfolio_members(family_id, member_id)
        today = date.today()
        rows: List[MfPortfolioRow] = []
        cash_flows: List[Tuple[date, Decimal]] = []
        total_invested = _ZERO
        total_current_value = _ZERO
        xirr_value = _ZERO
        xirr_excluded = 0
        for member in members:
            for mf in self.list_mfs(member.member_id):
                rows.append(MfPortfolioRow(owner_name=member.name, mf=mf))
                if mf.status != "ACTIVE":
                    continue
                total_invested += mf.invested_amount
                total_current_value += mf.current_value
                active = [t for t in mf.transactions if t.status == "ACTIVE"]
                if any(t.is_opening_balance for t in active):
                    xirr_excluded += 1  # no purchase dates before its imported statement
                    continue
                xirr_value += mf.current_value
                cash_flows.extend((t.txn_date, -t.amount) for t in active)

        if xirr_value > _ZERO:
            cash_flows.append((today, xirr_value))
        total_gain_loss = total_current_value - total_invested
        total_gain_loss_pct = (
            (total_gain_loss / total_invested * Decimal("100")).quantize(Decimal("0.01"))
            if total_invested != _ZERO
            else _ZERO
        )
        return MfPortfolio(
            family_id=family_id,
            member_id=member_id,
            rows=tuple(rows),
            total_invested=total_invested,
            total_current_value=total_current_value,
            total_gain_loss=total_gain_loss,
            total_gain_loss_pct=total_gain_loss_pct,
            xirr=compute_xirr(cash_flows),
            xirr_excluded_count=xirr_excluded,
            by_broker=self._mf_by_broker([r.mf for r in rows if r.mf.status == "ACTIVE"], today),
            by_kind=self._mf_by_kind([r.mf for r in rows if r.mf.status == "ACTIVE"]),
            total_sip_invested=sum(
                (r.mf.sip_invested for r in rows if r.mf.status == "ACTIVE"), _ZERO
            ),
            total_lumpsum_invested=sum(
                (r.mf.lumpsum_invested for r in rows if r.mf.status == "ACTIVE"), _ZERO
            ),
        )

    # ------------------------------------------------------------------
    # Stocks: BUY/SELL history (stock_transaction), average-cost position,
    # and a price history in asset_valuation (market_value = price/share)
    # ------------------------------------------------------------------

    def _active_stock_txns(
        self, asset_id: int, uow: Optional[UnitOfWork] = None
    ) -> List[StockTransaction]:
        return [
            t for t in self._stock_txns.list_transactions(asset_id, uow=uow)
            if t.status == "ACTIVE"
        ]

    def _stock_summary(
        self, asset: Asset, holding: StockHolding, transactions: List[StockTransaction], as_of: date
    ) -> StockSummary:
        position = compute_stock_position([t for t in transactions if t.status == "ACTIVE"])
        valuation = self._valuations.get_latest_valuation(asset.asset_id)
        price = valuation.market_value if valuation is not None else None
        current_value = (
            (position.quantity * price).quantize(Decimal("0.01")) if price is not None else _ZERO
        )
        gain_loss = current_value - position.cost_basis
        cash_flows = list(position.cash_flows)
        if current_value > _ZERO:
            cash_flows.append((as_of, current_value))
        if any(t.is_opening_balance for t in transactions if t.status == "ACTIVE"):
            cash_flows = []  # buy dates before an imported statement are unknown
        return StockSummary(
            asset_id=asset.asset_id,
            member_id=asset.member_id,
            name=asset.name,
            status=asset.status,
            broker=holding.broker,
            symbol=holding.symbol,
            exchange=holding.exchange,
            quantity=position.quantity,
            avg_buy_price=position.avg_buy_price,
            current_price=price,
            current_price_source=valuation.source if valuation is not None else None,
            current_price_date=valuation.valuation_date if valuation is not None else None,
            invested_amount=position.cost_basis,
            current_value=current_value,
            gain_loss=gain_loss,
            gain_loss_pct=self._pct(gain_loss, position.cost_basis),
            realised_gain=position.realised_gain,
            xirr=compute_xirr(cash_flows),
            transactions=tuple(transactions),
        )

    def _refresh_stock_caches(self, asset_id: int, uow: UnitOfWork) -> None:
        """Recompute quantity/avg price/invested/current value from the full
        history and persist them. Raises `ValueError` (rolling the unit of
        work back) if the history now contains an impossible sell."""
        position = compute_stock_position(self._active_stock_txns(asset_id, uow=uow))
        self._cache.set_stock_quantity_and_avg_price(
            asset_id, position.quantity, position.avg_buy_price, uow=uow
        )
        self._cache.set_asset_invested_amount(asset_id, position.cost_basis, uow=uow)
        asset = self._assets.get_asset(asset_id, uow=uow)
        current_value = self._current_value_for_asset(asset, date.today(), uow=uow)
        self._cache.set_asset_current_value(asset_id, current_value, uow=uow)

    def _load_stock_summary(self, asset_id: int) -> StockSummary:
        return self._stock_summary(
            self._assets.get_asset(asset_id),
            self._assets.get_stock(asset_id),
            self._stock_txns.list_transactions(asset_id),
            date.today(),
        )

    def _require_stock(self, asset_id: int, uow: UnitOfWork) -> Asset:
        asset = self._assets.get_asset(asset_id, uow=uow)
        if asset is None or asset.category != "STOCK":
            raise ValueError(f"stock holding {asset_id} does not exist")
        return asset

    def create_stock(
        self,
        *,
        member_id: int,
        broker: str,
        symbol: str,
        exchange: str,
        initial_txn_date: date,
        initial_quantity: Money,
        initial_price: Money,
        initial_charges: Money = 0,
        name: Optional[str] = None,
    ) -> StockSummary:
        """Open a stock holding with its first BUY — atomically, like
        `create_mutual_fund`. The buy price also seeds the price history.
        `symbol` is stored upper-cased (it is how NSE/BSE list it)."""
        symbol = symbol.strip().upper()
        existing = self._find_stock_holding(member_id, symbol, broker)
        if existing is not None:
            # One holding per person and broker: a repeat purchase is a BUY on it.
            return self.record_stock_transaction(
                asset_id=existing, txn_date=initial_txn_date, txn_type="BUY",
                quantity=initial_quantity, price=initial_price, charges=initial_charges,
            )
        with self._db.unit_of_work() as uow:
            asset, _ = self._assets.create_stock(
                member_id, name or symbol, broker, symbol, exchange,
                0, initial_txn_date, [(member_id, Decimal("100"))],
                broker=broker, uow=uow,
            )
            self._stock_txns.create_transaction(
                asset.asset_id, initial_txn_date, "BUY",
                initial_quantity, initial_price, initial_charges, uow=uow,
            )
            self._record_valuation_from_transaction(
                asset.asset_id, initial_txn_date, _decimal(initial_price), uow
            )
            self._refresh_stock_caches(asset.asset_id, uow)
        return self._load_stock_summary(asset.asset_id)

    def record_stock_transaction(
        self,
        *,
        asset_id: int,
        txn_date: date,
        txn_type: str,
        quantity: Money,
        price: Money,
        charges: Money = 0,
        description: Optional[str] = None,
    ) -> StockSummary:
        """Add a BUY or SELL. A SELL of more shares than were held on that
        date is rejected and nothing is saved."""
        with self._db.unit_of_work() as uow:
            self._require_stock(asset_id, uow)
            self._stock_txns.create_transaction(
                asset_id, txn_date, txn_type, quantity, price, charges,
                description=description, uow=uow,
            )
            self._record_valuation_from_transaction(asset_id, txn_date, _decimal(price), uow)
            self._refresh_stock_caches(asset_id, uow)
        return self._load_stock_summary(asset_id)

    def record_stock_price(
        self, *, asset_id: int, price_date: date, price: Money, source: str = "MANUAL"
    ) -> StockSummary:
        """Record a share price observation — the manual "update price"
        action. Same-date re-entry is stored as a correction, exactly like
        `record_mf_valuation`."""
        if _decimal(price) <= _ZERO:
            raise ValueError("price must be positive")
        with self._db.unit_of_work() as uow:
            self._require_stock(asset_id, uow)
            try:
                self._valuations.record_valuation(asset_id, price_date, price, source, uow=uow)
            except RepositoryIntegrityError:
                self._valuations.record_valuation(
                    asset_id, price_date, price, source, is_correction=True, uow=uow
                )
            self._refresh_stock_caches(asset_id, uow)
        return self._load_stock_summary(asset_id)

    _STOCK_TXN_TYPES = ("BUY", "SELL")

    def update_stock_holding(
        self, *, asset_id: int, member_id: int, broker: str, symbol: str, exchange: str,
        name: Optional[str] = None,
    ) -> StockSummary:
        """Correct a holding's owner, broker, symbol, exchange or name. If
        that makes it the same person, stock and broker as another holding,
        the two are merged into that one (one holding per stock and broker)."""
        symbol = (symbol or "").strip().upper()
        broker = (broker or "").strip()
        if not symbol or not broker:
            raise ValueError("symbol and broker are required")
        if exchange not in ("NSE", "BSE"):
            raise ValueError("exchange must be NSE or BSE")
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        twin = self._find_stock_holding(member_id, symbol, broker)
        with self._db.unit_of_work() as uow:
            self._require_stock(asset_id, uow)
            if twin is not None and twin != asset_id:
                self._stock_txns.reassign_asset(asset_id, twin, uow=uow)
                self._planning.clear_asset(asset_id, uow=uow)

                self._assets.delete_asset(asset_id, uow=uow)
                self._refresh_stock_caches(twin, uow)
                keep = twin
            else:
                self._assets.update_stock(
                    asset_id, member_id=member_id, name=(name or "").strip() or symbol,
                    broker=broker, symbol=symbol, exchange=exchange, uow=uow,
                )
                self._refresh_stock_caches(asset_id, uow)
                keep = asset_id
        return self._load_stock_summary(keep)

    def update_stock_transaction(
        self, *, stock_txn_id: int, txn_date: date, txn_type: str, quantity: Money, price: Money,
        charges: Money = 0, description: Optional[str] = None,
    ) -> StockSummary:
        """Correct one buy or sell (e.g. the real buy price of shares imported
        from a CAS). The holding is worked out again from its whole history;
        a change that would sell more shares than were held is refused."""
        if txn_type not in self._STOCK_TXN_TYPES:
            raise ValueError("type must be BUY or SELL")
        if _decimal(quantity) <= _ZERO or _decimal(price) <= _ZERO:
            raise ValueError("quantity and price must be positive")
        if _decimal(charges or 0) < _ZERO:
            raise ValueError("charges cannot be negative")
        with self._db.unit_of_work() as uow:
            txn = self._stock_txns.get_transaction(stock_txn_id, uow=uow)
            if txn is None:
                raise ValueError(f"stock transaction {stock_txn_id} does not exist")
            note = description if description is not None else (
                None if txn.is_opening_balance else txn.description
            )
            self._stock_txns.update_transaction(
                stock_txn_id, txn_date=txn_date, txn_type=txn_type, quantity=quantity, price=price,
                charges=charges or 0, description=note, uow=uow,
            )
            self._refresh_stock_caches(txn.asset_id, uow)
        return self._load_stock_summary(txn.asset_id)

    def delete_stock_transaction(self, stock_txn_id: int) -> StockSummary:
        """Remove one buy or sell entered by mistake (not the holding's last
        one: delete the holding instead)."""
        with self._db.unit_of_work() as uow:
            txn = self._stock_txns.get_transaction(stock_txn_id, uow=uow)
            if txn is None:
                raise ValueError(f"stock transaction {stock_txn_id} does not exist")
            if len(self._active_stock_txns(txn.asset_id, uow=uow)) <= 1:
                raise ValueError("this is the holding's only entry; delete the holding instead")
            self._stock_txns.delete_transaction(stock_txn_id, uow=uow)
            self._refresh_stock_caches(txn.asset_id, uow)
        return self._load_stock_summary(txn.asset_id)

    def delete_stock(self, asset_id: int) -> None:
        """Remove a holding and all its buys, sells and prices."""
        with self._db.unit_of_work() as uow:
            self._require_stock(asset_id, uow)
            self._stock_txns.delete_for_asset(asset_id, uow=uow)
            self._planning.clear_asset(asset_id, uow=uow)

            self._assets.delete_asset(asset_id, uow=uow)

    def _find_stock_holding(self, member_id: int, symbol: str, broker: str) -> Optional[int]:
        """The member's ACTIVE holding of `symbol` with `broker`, if any."""
        wanted = (symbol.strip().upper(), broker.strip().lower())
        for asset in self._assets.list_assets(member_id):
            if asset.category != "STOCK" or asset.status != "ACTIVE":
                continue
            holding = self._assets.get_stock(asset.asset_id)
            if (holding.symbol.upper(), holding.broker.strip().lower()) == wanted:
                return asset.asset_id
        return None

    def merge_duplicate_stock_holdings(self, family_id: Optional[int] = None) -> List[str]:
        """Fold every member's duplicate holdings of the same symbol with the
        same broker into the oldest one: their buys/sells move over, the
        newest known price is kept, the duplicates are removed and the
        holding's quantity / average price recomputed. Returns one line per
        merge. Safe to run any number of times."""
        families = [family_id] if family_id is not None else [f.family_id for f in self.list_families()]
        merged: List[str] = []
        for fid in families:
            for member in self._family.list_family_members(fid):
                groups: Dict[Tuple[str, str], List[Asset]] = {}
                for asset in self._assets.list_assets(member.member_id):
                    if asset.category != "STOCK" or asset.status != "ACTIVE":
                        continue
                    holding = self._assets.get_stock(asset.asset_id)
                    key = (holding.symbol.upper(), holding.broker.strip().lower())
                    groups.setdefault(key, []).append(asset)
                for (symbol, _), assets in groups.items():
                    if len(assets) < 2:
                        continue
                    keep, duplicates = assets[0], assets[1:]
                    with self._db.unit_of_work() as uow:
                        latest = self._valuations.get_latest_valuation(keep.asset_id, uow=uow)
                        for dup in duplicates:
                            dup_latest = self._valuations.get_latest_valuation(dup.asset_id, uow=uow)
                            if dup_latest is not None and (
                                latest is None or dup_latest.valuation_date > latest.valuation_date
                            ):
                                latest = dup_latest
                            self._stock_txns.reassign_asset(dup.asset_id, keep.asset_id, uow=uow)
                            self._planning.clear_asset(dup.asset_id, uow=uow)

                            self._assets.delete_asset(dup.asset_id, uow=uow)
                        if latest is not None:
                            try:
                                self._valuations.record_valuation(
                                    keep.asset_id, latest.valuation_date, latest.market_value,
                                    latest.source, uow=uow,
                                )
                            except RepositoryIntegrityError:
                                pass  # the kept holding already has a price for that date
                        self._refresh_stock_caches(keep.asset_id, uow)
                    merged.append(f"{member.name}: {len(assets)} {symbol} holdings merged into one")
        return merged

    def set_member_risk_tolerance(self, member_id: int, risk_tolerance: Optional[str]) -> FamilyMember:
        if risk_tolerance not in (None, "CONSERVATIVE", "MODERATE", "AGGRESSIVE"):
            raise ValueError("risk tolerance must be CONSERVATIVE, MODERATE or AGGRESSIVE")
        member = self._family.set_risk_tolerance(member_id, risk_tolerance)
        if member is None:
            raise ValueError(f"family member {member_id} does not exist")
        return member

    def set_stock_classification(
        self, *, symbol: str, sector: Optional[str], cap_category: Optional[str]
    ) -> StockInfo:
        """The user's own sector / market-cap for a symbol (kept over NSE's)."""
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if cap_category not in (None, "LARGE", "MID", "SMALL"):
            raise ValueError("market cap must be LARGE, MID or SMALL")
        return self._stock_info.set_manual(symbol, (sector or "").strip() or None, cap_category, date.today())

    def stock_classification_refreshed_on(self) -> Optional[date]:
        return self._stock_info.last_nse_refresh()

    def update_stock_classification_from_nse(self, rows, today: Optional[date] = None) -> int:
        """Store NSE's `(symbol, company, sector, cap)` rows (see
        `api.stock_price_sync.parse_nse_index_lists`); the user's own
        entries are kept."""
        return self._stock_info.upsert_nse(rows, today or date.today())

    def list_stocks(self, member_id: int) -> List[StockSummary]:
        today = date.today()
        return [
            self._stock_summary(
                asset, self._assets.get_stock(asset.asset_id),
                self._stock_txns.list_transactions(asset.asset_id), today,
            )
            for asset in self._assets.list_assets(member_id)
            if asset.category == "STOCK"
        ]

    def get_stock_portfolio(
        self, family_id: int, member_id: Optional[int] = None
    ) -> StockPortfolio:
        """Every stock holding in the family, or only `member_id`'s, with
        totals and a combined XIRR. Only ACTIVE holdings count."""
        members = self._portfolio_members(family_id, member_id)
        today = date.today()
        rows: List[StockPortfolioRow] = []
        cash_flows: List[Tuple[date, Decimal]] = []
        invested = current = realised = xirr_value = _ZERO
        xirr_excluded = 0
        for member in members:
            for stock in self.list_stocks(member.member_id):
                rows.append(StockPortfolioRow(owner_name=member.name, stock=stock))
                if stock.status != "ACTIVE":
                    continue
                invested += stock.invested_amount
                current += stock.current_value
                realised += stock.realised_gain
                active = [t for t in stock.transactions if t.status == "ACTIVE"]
                if any(t.is_opening_balance for t in active):
                    xirr_excluded += 1  # no buy dates before its imported statement
                    continue
                xirr_value += stock.current_value
                cash_flows.extend(compute_stock_position(active).cash_flows)
        if xirr_value > _ZERO:
            cash_flows.append((today, xirr_value))
        gain_loss = current - invested
        info = self._stock_info.all_info()
        groups = self._stock_groups(rows, info, current)
        return StockPortfolio(
            family_id=family_id,
            member_id=member_id,
            rows=tuple(rows),
            total_invested=invested,
            total_current_value=current,
            total_gain_loss=gain_loss,
            total_gain_loss_pct=self._pct(gain_loss, invested),
            total_realised_gain=realised,
            xirr=compute_xirr(cash_flows),
            xirr_excluded_count=xirr_excluded,
            groups=groups,
            analysis=self._stock_analysis(family_id, members, rows, groups, info, current),
        )

    @staticmethod
    def _classify(symbol: str, info: Dict[str, StockInfo]) -> Tuple[Optional[str], str, str]:
        """(company, sector, cap) for a symbol. With NSE data loaded, a symbol
        outside the Nifty 500 is small cap by SEBI's ranking (beyond the top
        250); with no data yet, it is UNKNOWN."""
        row = info.get(symbol.upper())
        if row is not None:
            return row.company_name, row.sector or "Unclassified", row.cap_category or "UNKNOWN"
        has_nse = any(r.source == "NSE" for r in info.values())
        return None, "Unclassified", "SMALL" if has_nse else "UNKNOWN"

    def _stock_groups(
        self, rows: Sequence[StockPortfolioRow], info: Dict[str, StockInfo], total_value: Decimal
    ) -> Tuple[StockGroup, ...]:
        by_symbol: Dict[str, List[StockPortfolioRow]] = {}
        for row in rows:
            if row.stock.status == "ACTIVE":
                by_symbol.setdefault(row.stock.symbol.upper(), []).append(row)
        groups: List[StockGroup] = []
        for symbol, members_rows in by_symbol.items():
            stocks = [r.stock for r in members_rows]
            quantity = sum((s.quantity for s in stocks), _ZERO)
            invested = sum((s.invested_amount for s in stocks), _ZERO)
            value = sum((s.current_value for s in stocks), _ZERO)
            priced = [s for s in stocks if s.current_price is not None]
            latest = max(priced, key=lambda s: s.current_price_date or date.min) if priced else None
            company, sector, cap = self._classify(symbol, info)
            gain = value - invested
            groups.append(StockGroup(
                symbol=symbol,
                company_name=company or (stocks[0].name if stocks[0].name != symbol else None),
                sector=sector, cap_category=cap, quantity=quantity,
                avg_buy_price=(invested / quantity).quantize(Decimal("0.0001")) if quantity else None,
                current_price=latest.current_price if latest else None,
                invested_amount=invested, current_value=value, gain_loss=gain,
                gain_loss_pct=self._pct(gain, invested),
                realised_gain=sum((s.realised_gain for s in stocks), _ZERO),
                weight_pct=self._pct(value, total_value),
                parts=tuple(
                    StockGroupPart(
                        asset_id=r.stock.asset_id, owner_name=r.owner_name, broker=r.stock.broker,
                        exchange=r.stock.exchange, quantity=r.stock.quantity,
                        avg_buy_price=r.stock.avg_buy_price,
                        invested_amount=r.stock.invested_amount, current_value=r.stock.current_value,
                    )
                    for r in members_rows
                ),
            ))
        return tuple(sorted(groups, key=lambda g: (-g.current_value, g.symbol)))

    def _split(self, groups, key, total: Decimal) -> Tuple[SplitSlice, ...]:
        values: Dict[str, Decimal] = {}
        for g in groups:
            values[key(g)] = values.get(key(g), _ZERO) + g.current_value
        return tuple(sorted(
            (SplitSlice(name=k, value=v, pct=self._pct(v, total)) for k, v in values.items()),
            key=lambda sl: (-sl.value, sl.name),
        ))

    _RISK_RANK = {"MODERATE": 0, "HIGH": 1, "VERY_HIGH": 2}
    # tolerance -> (highest portfolio risk that fits, max % of wealth in stocks)
    _TOLERANCE_LIMITS = {
        "CONSERVATIVE": ("MODERATE", Decimal("15")),
        "MODERATE": ("HIGH", Decimal("40")),
        "AGGRESSIVE": ("VERY_HIGH", Decimal("100")),
    }

    def _portfolio_risk(self, groups: Sequence[StockGroup], total: Decimal) -> Tuple[str, List[str]]:
        """A rule-of-thumb risk reading from market-cap mix and
        concentration: each warning sign adds a point; 0 = MODERATE (direct
        equity is never low risk), 1-2 = HIGH, 3+ = VERY_HIGH."""
        held = [g for g in groups if g.quantity > 0]
        caps = {sl.name: sl.pct for sl in self._split(held, lambda g: g.cap_category, total)}
        weights = sorted((self._pct(g.current_value, total) for g in held), reverse=True)
        points, reasons = 0, []
        small, mid = caps.get("SMALL", _ZERO), caps.get("MID", _ZERO)
        if small > 40:
            points += 2
            reasons.append(f"{small}% in small caps")
        elif small > 20:
            points += 1
            reasons.append(f"{small}% in small caps")
        if mid > 50:
            points += 1
            reasons.append(f"{mid}% in mid caps")
        if weights and weights[0] > 25:
            points += 1
            reasons.append(f"largest stock is {weights[0]}% of the portfolio")
        if 0 < len(held) < 5:
            points += 1
            reasons.append(f"only {len(held)} stock{'s' if len(held) > 1 else ''}")
        level = "MODERATE" if points == 0 else "HIGH" if points <= 2 else "VERY_HIGH"
        return level, reasons

    def _stock_analysis(
        self, family_id, members, rows, groups: Tuple[StockGroup, ...],
        info: Dict[str, StockInfo], total: Decimal,
    ) -> StockAnalysis:
        held = [g for g in groups if g.quantity > 0]
        weights = [self._pct(g.current_value, total) for g in held]
        hhi = sum(((w / Decimal("100")) ** 2 for w in weights), _ZERO)
        effective = (Decimal("1") / hhi).quantize(Decimal("0.1")) if hhi else _ZERO
        sectors = self._split(held, lambda g: g.sector, total)
        warnings = tuple(
            f"{sl.name} is {sl.pct}% of the portfolio (above 25%)"
            for sl in sectors if sl.pct > 25 and sl.name != "Unclassified"
        )
        by_gain = sorted(held, key=lambda g: g.gain_loss_pct, reverse=True)

        checks: List[RiskCheck] = []
        for member in members:
            own = [r for r in rows if r.owner_name == member.name and r.stock.status == "ACTIVE"]
            if not own:
                continue
            own_total = sum((r.stock.current_value for r in own), _ZERO)
            own_groups = self._stock_groups(own, info, own_total)
            level, reasons = self._portfolio_risk(own_groups, own_total)
            wealth = self.get_asset_allocation(family_id, member.member_id).total_current_value
            share = self._pct(own_total, wealth)
            tolerance = member.risk_tolerance
            fits: Optional[bool] = None
            if tolerance:
                max_level, max_share = self._TOLERANCE_LIMITS[tolerance]
                fits = self._RISK_RANK[level] <= self._RISK_RANK[max_level] and share <= max_share
                if share > max_share:
                    reasons.append(
                        f"stocks are {share}% of this member's wealth "
                        f"(a {tolerance.lower()} investor usually keeps it under {max_share}%)"
                    )
            checks.append(RiskCheck(
                member_id=member.member_id, name=member.name, risk_tolerance=tolerance,
                portfolio_risk=level, stock_share_of_wealth_pct=share, fits=fits,
                reasons=tuple(reasons),
            ))

        return StockAnalysis(
            stock_count=len(held),
            sector_split=sectors,
            cap_split=self._split(held, lambda g: g.cap_category, total),
            largest_symbol=held[0].symbol if held else None,
            largest_pct=weights[0] if weights else _ZERO,
            top5_pct=sum(weights[:5], _ZERO),
            effective_stocks=effective,
            diversification="LOW" if effective < 5 else "MEDIUM" if effective < 10 else "GOOD",
            sector_warnings=warnings,
            winners=tuple(g for g in by_gain[:3] if g.gain_loss > 0),
            losers=tuple(g for g in reversed(by_gain[-3:]) if g.gain_loss < 0),
            risk_checks=tuple(checks),
            classification_date=self._stock_info.last_nse_refresh(),
        )

    # ------------------------------------------------------------------
    # PF (EPF / PPF): contributions and balance from the latest passbook
    # ------------------------------------------------------------------

    @staticmethod
    def _ppf_maturity(opened: date) -> date:
        """PPF matures after 15 complete financial years (April-March)
        following the one it was opened in."""
        fy_end_year = opened.year + 1 if opened.month >= 4 else opened.year
        return date(fy_end_year + 15, 4, 1)

    def _record_statement_value(
        self, asset_id: int, as_of: date, value: Money, uow: UnitOfWork
    ) -> None:
        """Record a statement balance; a second entry for the same date is
        stored as a correction (the newer one wins), as for MF NAVs."""
        if _decimal(value) < _ZERO:
            raise ValueError("balance cannot be negative")
        try:
            self._valuations.record_valuation(asset_id, as_of, value, "MANUAL", uow=uow)
        except RepositoryIntegrityError:
            self._valuations.record_valuation(
                asset_id, as_of, value, "MANUAL", is_correction=True, uow=uow
            )

    def _pf_summary(self, asset: Asset, account: RetirementAccount) -> PfSummary:
        contributions = account.employee_contribution + (account.employer_contribution or _ZERO)
        valuation = self._valuations.get_latest_valuation(asset.asset_id)
        balance = valuation.market_value if valuation is not None else _ZERO
        interest = balance - contributions
        return PfSummary(
            asset_id=asset.asset_id,
            member_id=asset.member_id,
            name=asset.name,
            status=asset.status,
            account_type=account.account_type,
            account_number=account.account_number,
            institution=asset.institution,
            opened_date=asset.purchase_date,
            interest_rate=account.interest_rate,
            employee_contribution=account.employee_contribution,
            employer_contribution=account.employer_contribution,
            total_contributions=contributions,
            balance=balance,
            balance_date=valuation.valuation_date if valuation is not None else None,
            interest_earned=interest,
            interest_earned_pct=self._pct(interest, contributions),
            maturity_date=(
                self._ppf_maturity(asset.purchase_date) if account.account_type == "PPF" else None
            ),
        )

    def _refresh_pf_caches(self, asset_id: int, uow: UnitOfWork) -> None:
        account = self._assets.get_retirement_account(asset_id, uow=uow)
        contributions = account.employee_contribution + (account.employer_contribution or _ZERO)
        self._cache.set_asset_invested_amount(asset_id, contributions, uow=uow)
        asset = self._assets.get_asset(asset_id, uow=uow)
        self._cache.set_asset_current_value(
            asset_id, self._current_value_for_asset(asset, date.today(), uow=uow), uow=uow
        )

    @staticmethod
    def _check_pf_inputs(account_type: str, employee: Money, employer: Optional[Money]) -> None:
        if account_type not in ("EPF", "PPF"):
            raise ValueError("account type must be EPF or PPF")
        if account_type == "PPF" and employer not in (None, "", 0) and _decimal(employer) != _ZERO:
            raise ValueError("PPF has no employer contribution")
        for label, value in (("your contribution", employee), ("employer contribution", employer)):
            if value is not None and _decimal(value) < _ZERO:
                raise ValueError(f"{label} cannot be negative")

    def create_pf_account(
        self,
        *,
        member_id: int,
        account_type: str,
        account_number: str,
        opened_date: date,
        interest_rate: Money,
        employee_contribution: Money,
        balance: Money,
        balance_date: date,
        employer_contribution: Optional[Money] = None,
        institution: Optional[str] = None,
    ) -> PfSummary:
        """Add an EPF or PPF account with its current passbook figures."""
        self._check_pf_inputs(account_type, employee_contribution, employer_contribution)
        if account_type == "PPF":
            employer_contribution = None
        with self._db.unit_of_work() as uow:
            asset, _ = self._assets.create_retirement_account(
                member_id,
                f"{account_type} {account_number}",
                institution or ("EPFO" if account_type == "EPF" else None),
                opened_date,
                [(member_id, Decimal("100"))],
                account_type=account_type,
                account_number=account_number,
                employee_contribution=employee_contribution,
                employer_contribution=employer_contribution,
                interest_rate=interest_rate,
                uow=uow,
            )
            self._record_statement_value(asset.asset_id, balance_date, balance, uow)
            self._refresh_pf_caches(asset.asset_id, uow)
        return self._pf_summary(
            self._assets.get_asset(asset.asset_id),
            self._assets.get_retirement_account(asset.asset_id),
        )

    def update_pf_account(
        self,
        *,
        asset_id: int,
        balance: Money,
        balance_date: date,
        employee_contribution: Money,
        employer_contribution: Optional[Money] = None,
        interest_rate: Optional[Money] = None,
    ) -> PfSummary:
        """Record a newer passbook: balance as of `balance_date` and the
        contribution totals to date (both replace the previous figures)."""
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category not in ("EPF", "PPF"):
                raise ValueError(f"PF account {asset_id} does not exist")
            current = self._assets.get_retirement_account(asset_id, uow=uow)
            self._check_pf_inputs(current.account_type, employee_contribution, employer_contribution)
            self._assets.update_retirement_account(
                asset_id,
                employee_contribution=employee_contribution,
                employer_contribution=(
                    None if current.account_type == "PPF" else employer_contribution
                ),
                interest_rate=interest_rate if interest_rate not in (None, "") else current.interest_rate,
                uow=uow,
            )
            self._record_statement_value(asset_id, balance_date, balance, uow)
            self._refresh_pf_caches(asset_id, uow)
        return self._pf_summary(
            self._assets.get_asset(asset_id), self._assets.get_retirement_account(asset_id)
        )

    def edit_pf_account(
        self,
        *,
        asset_id: int,
        member_id: int,
        account_number: str,
        opened_date: date,
        interest_rate: Money,
        employee_contribution: Money,
        balance: Money,
        balance_date: date,
        employer_contribution: Optional[Money] = None,
        institution: Optional[str] = None,
    ) -> PfSummary:
        """Correct anything about an EPF/PPF account: owner, number, where it
        is held, when it was opened, the rate, the contribution totals and
        the balance (as of `balance_date`). The type (EPF / PPF) stays."""
        number = (account_number or "").strip()
        if not number:
            raise ValueError("account number / UAN is required")
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category not in ("EPF", "PPF"):
                raise ValueError(f"PF account {asset_id} does not exist")
            current = self._assets.get_retirement_account(asset_id, uow=uow)
            self._check_pf_inputs(current.account_type, employee_contribution, employer_contribution)
            if _decimal(interest_rate) < _ZERO:
                raise ValueError("interest rate cannot be negative")
            self._assets.update_retirement_details(
                asset_id, member_id=member_id, name=f"{current.account_type} {number}",
                institution=(institution or "").strip() or None, opened_date=opened_date,
                account_number=number, uow=uow,
            )
            self._assets.update_retirement_account(
                asset_id,
                employee_contribution=employee_contribution,
                employer_contribution=None if current.account_type == "PPF" else employer_contribution,
                interest_rate=interest_rate, uow=uow,
            )
            self._record_statement_value(asset_id, balance_date, balance, uow)
            self._refresh_pf_caches(asset_id, uow)
        return self._pf_summary(
            self._assets.get_asset(asset_id), self._assets.get_retirement_account(asset_id)
        )

    def delete_pf_account(self, asset_id: int) -> None:
        """Remove an EPF/PPF account entered by mistake (its balances go too)."""
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category not in ("EPF", "PPF"):
                raise ValueError(f"PF account {asset_id} does not exist")
            self._planning.clear_asset(asset_id, uow=uow)

            self._assets.delete_asset(asset_id, uow=uow)

    def list_pf_accounts(self, member_id: int) -> List[PfSummary]:
        return [
            self._pf_summary(asset, self._assets.get_retirement_account(asset.asset_id))
            for asset in self._assets.list_assets(member_id)
            if asset.category in ("EPF", "PPF")
        ]

    def get_pf_portfolio(self, family_id: int, member_id: Optional[int] = None) -> PfPortfolio:
        members = self._portfolio_members(family_id, member_id)
        rows: List[PfPortfolioRow] = []
        contributions = balance = epf = ppf = _ZERO
        for member in members:
            for account in self.list_pf_accounts(member.member_id):
                rows.append(PfPortfolioRow(owner_name=member.name, account=account))
                if account.status != "ACTIVE":
                    continue
                contributions += account.total_contributions
                balance += account.balance
                if account.account_type == "EPF":
                    epf += account.balance
                else:
                    ppf += account.balance
        interest = balance - contributions
        return PfPortfolio(
            family_id=family_id,
            member_id=member_id,
            rows=tuple(rows),
            total_contributions=contributions,
            total_balance=balance,
            total_interest_earned=interest,
            total_interest_earned_pct=self._pct(interest, contributions),
            epf_balance=epf,
            ppf_balance=ppf,
        )

    # ------------------------------------------------------------------
    # Gold: one asset per purchase; asset_valuation holds the per-gram rate
    # for that holding's purity (market_value = rupees per gram)
    # ------------------------------------------------------------------

    def _gold_summary(self, asset: Asset, holding: GoldHolding, as_of: date) -> GoldSummary:
        valuation = self._valuations.get_latest_valuation(asset.asset_id)
        rate = valuation.market_value if valuation is not None else None
        current_value = (holding.quantity_grams * rate).quantize(_CENT) if rate is not None else _ZERO
        invested = asset.invested_amount
        gain_loss = current_value - invested
        flows = [(asset.purchase_date, -invested)]
        if current_value > _ZERO:
            flows.append((as_of, current_value))
        return GoldSummary(
            asset_id=asset.asset_id,
            member_id=asset.member_id,
            name=asset.name,
            status=asset.status,
            form=holding.form,
            purity=holding.purity,
            quantity_grams=holding.quantity_grams,
            purchase_rate_per_gram=holding.purchase_rate_per_gram,
            purchase_date=asset.purchase_date,
            sgb_interest_rate=holding.sgb_interest_rate,
            invested_amount=invested,
            current_rate_per_gram=rate,
            rate_date=valuation.valuation_date if valuation is not None else None,
            current_value=current_value,
            gain_loss=gain_loss,
            gain_loss_pct=self._pct(gain_loss, invested),
            xirr=compute_xirr(flows),
        )

    def _set_gold_rate(self, asset_id: int, as_of: date, rate_per_gram: Decimal, uow: UnitOfWork) -> None:
        try:
            self._valuations.record_valuation(asset_id, as_of, rate_per_gram, "MANUAL", uow=uow)
        except RepositoryIntegrityError:
            self._valuations.record_valuation(
                asset_id, as_of, rate_per_gram, "MANUAL", is_correction=True, uow=uow
            )
        asset = self._assets.get_asset(asset_id, uow=uow)
        self._cache.set_asset_current_value(
            asset_id, self._current_value_for_asset(asset, date.today(), uow=uow), uow=uow
        )

    def create_gold(
        self,
        *,
        member_id: int,
        form: str,
        purity: str,
        quantity_grams: Money,
        purchase_rate_per_gram: Money,
        purchase_date: date,
        name: Optional[str] = None,
        institution: Optional[str] = None,
        sgb_interest_rate: Optional[Money] = None,
    ) -> GoldSummary:
        """Record one gold purchase. Invested = grams x purchase rate; the
        purchase rate also starts its rate history."""
        if purity not in GOLD_PURITY_FACTOR:
            raise ValueError("purity must be 24K, 22K or 18K")
        if form in ("SGB", "DIGITAL") and purity != "24K":
            raise ValueError(f"{form} gold is always 24K")
        if form != "SGB":
            sgb_interest_rate = None
        grams = _decimal(quantity_grams)
        rate = _decimal(purchase_rate_per_gram)
        if grams <= _ZERO or rate <= _ZERO:
            raise ValueError("grams and purchase rate must be positive")
        invested = (grams * rate).quantize(_CENT)
        label = {"PHYSICAL": "Physical gold", "DIGITAL": "Digital gold", "SGB": "SGB"}.get(form, form)
        with self._db.unit_of_work() as uow:
            asset, _ = self._assets.create_gold(
                member_id, name or f"{label} {grams}g {purity}", institution, invested,
                purchase_date, [(member_id, Decimal("100"))],
                form=form, purity=purity, quantity_grams=grams,
                purchase_rate_per_gram=rate, sgb_interest_rate=sgb_interest_rate, uow=uow,
            )
            self._set_gold_rate(asset.asset_id, purchase_date, rate, uow)
        return self._gold_summary(
            self._assets.get_asset(asset.asset_id), self._assets.get_gold(asset.asset_id), date.today()
        )

    def record_gold_rate(
        self, *, family_id: int, rate_24k_per_gram: Money, as_of: date
    ) -> "GoldPortfolio":
        """Revalue every ACTIVE gold holding in the family from one 24K rate
        per gram, each at its own purity's share of it."""
        rate_24k = _decimal(rate_24k_per_gram)
        if rate_24k <= _ZERO:
            raise ValueError("gold rate must be positive")
        members = self._portfolio_members(family_id, None)
        with self._db.unit_of_work() as uow:
            for member in members:
                for asset in self._assets.list_assets(member.member_id, uow=uow):
                    if asset.category != "GOLD" or asset.status != "ACTIVE":
                        continue
                    holding = self._assets.get_gold(asset.asset_id, uow=uow)
                    rate = (rate_24k * GOLD_PURITY_FACTOR[holding.purity]).quantize(_CENT)
                    self._set_gold_rate(asset.asset_id, as_of, rate, uow)
        return self.get_gold_portfolio(family_id)

    def list_gold(self, member_id: int) -> List[GoldSummary]:
        today = date.today()
        return [
            self._gold_summary(asset, self._assets.get_gold(asset.asset_id), today)
            for asset in self._assets.list_assets(member_id)
            if asset.category == "GOLD"
        ]

    def get_gold_portfolio(self, family_id: int, member_id: Optional[int] = None) -> GoldPortfolio:
        members = self._portfolio_members(family_id, member_id)
        today = date.today()
        rows: List[GoldPortfolioRow] = []
        flows: List[Tuple[date, Decimal]] = []
        grams = invested = current = _ZERO
        latest: Optional[Tuple[date, Decimal]] = None
        for member in members:
            for gold in self.list_gold(member.member_id):
                rows.append(GoldPortfolioRow(owner_name=member.name, gold=gold))
                if gold.current_rate_per_gram is not None and (
                    latest is None or gold.rate_date > latest[0]
                ):
                    latest = (
                        gold.rate_date,
                        (gold.current_rate_per_gram / GOLD_PURITY_FACTOR[gold.purity]).quantize(_CENT),
                    )
                if gold.status != "ACTIVE":
                    continue
                grams += gold.quantity_grams
                invested += gold.invested_amount
                current += gold.current_value
                flows.append((gold.purchase_date, -gold.invested_amount))
        if current > _ZERO:
            flows.append((today, current))
        gain_loss = current - invested
        return GoldPortfolio(
            family_id=family_id,
            member_id=member_id,
            rows=tuple(rows),
            total_grams=grams,
            total_invested=invested,
            total_current_value=current,
            total_gain_loss=gain_loss,
            total_gain_loss_pct=self._pct(gain_loss, invested),
            xirr=compute_xirr(flows),
            latest_rate_24k=latest[1] if latest else None,
            latest_rate_date=latest[0] if latest else None,
        )

    # ------------------------------------------------------------------
    # NPS: contributions, asset mix and balance from the latest statement
    # ------------------------------------------------------------------

    @staticmethod
    def _check_nps_inputs(
        tier: str, employee: Money, employer: Optional[Money],
        equity: Money, corporate: Money, gov: Money,
    ) -> None:
        if tier not in ("I", "II"):
            raise ValueError("tier must be I or II")
        for label, value in (("your contribution", employee), ("employer contribution", employer)):
            if value is not None and _decimal(value) < _ZERO:
                raise ValueError(f"{label} cannot be negative")
        mix = [_decimal(equity), _decimal(corporate), _decimal(gov)]
        if any(v < _ZERO or v > Decimal("100") for v in mix):
            raise ValueError("each allocation % must be between 0 and 100")
        if sum(mix, _ZERO) not in (_ZERO, Decimal("100")):
            raise ValueError("equity + corporate debt + government bonds must add up to 100%")

    def _nps_summary(self, asset: Asset, account: NpsAccount) -> NpsSummary:
        contributions = account.employee_contribution + (account.employer_contribution or _ZERO)
        valuation = self._valuations.get_latest_valuation(asset.asset_id)
        balance = valuation.market_value if valuation is not None else _ZERO
        gain = balance - contributions
        return NpsSummary(
            asset_id=asset.asset_id,
            member_id=asset.member_id,
            name=asset.name,
            status=asset.status,
            pran=account.pran,
            tier=account.tier,
            institution=asset.institution,
            opened_date=asset.purchase_date,
            employee_contribution=account.employee_contribution,
            employer_contribution=account.employer_contribution,
            total_contributions=contributions,
            equity_pct=account.equity_pct,
            corporate_debt_pct=account.corporate_debt_pct,
            gov_bond_pct=account.gov_bond_pct,
            balance=balance,
            balance_date=valuation.valuation_date if valuation is not None else None,
            gain_loss=gain,
            gain_loss_pct=self._pct(gain, contributions),
        )

    def _refresh_nps_caches(self, asset_id: int, uow: UnitOfWork) -> None:
        account = self._assets.get_nps_account(asset_id, uow=uow)
        contributions = account.employee_contribution + (account.employer_contribution or _ZERO)
        self._cache.set_asset_invested_amount(asset_id, contributions, uow=uow)
        asset = self._assets.get_asset(asset_id, uow=uow)
        self._cache.set_asset_current_value(
            asset_id, self._current_value_for_asset(asset, date.today(), uow=uow), uow=uow
        )

    def create_nps_account(
        self,
        *,
        member_id: int,
        pran: str,
        tier: str,
        opened_date: date,
        employee_contribution: Money,
        balance: Money,
        balance_date: date,
        employer_contribution: Optional[Money] = None,
        equity_pct: Money = 0,
        corporate_debt_pct: Money = 0,
        gov_bond_pct: Money = 0,
        institution: Optional[str] = None,
    ) -> NpsSummary:
        """Add an NPS account with its current statement figures."""
        self._check_nps_inputs(
            tier, employee_contribution, employer_contribution,
            equity_pct, corporate_debt_pct, gov_bond_pct,
        )
        with self._db.unit_of_work() as uow:
            asset, _ = self._assets.create_nps_account(
                member_id, f"NPS Tier {tier} {pran}", institution, opened_date,
                [(member_id, Decimal("100"))],
                pran=pran, tier=tier,
                employee_contribution=employee_contribution,
                employer_contribution=employer_contribution,
                equity_pct=equity_pct, corporate_debt_pct=corporate_debt_pct,
                gov_bond_pct=gov_bond_pct, uow=uow,
            )
            self._record_statement_value(asset.asset_id, balance_date, balance, uow)
            self._refresh_nps_caches(asset.asset_id, uow)
        return self._nps_summary(
            self._assets.get_asset(asset.asset_id), self._assets.get_nps_account(asset.asset_id)
        )

    def update_nps_account(
        self,
        *,
        asset_id: int,
        balance: Money,
        balance_date: date,
        employee_contribution: Money,
        employer_contribution: Optional[Money] = None,
        equity_pct: Optional[Money] = None,
        corporate_debt_pct: Optional[Money] = None,
        gov_bond_pct: Optional[Money] = None,
    ) -> NpsSummary:
        """Record a newer statement: balance, contribution totals, and (if
        given) the asset mix; an omitted mix keeps the previous one."""
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "NPS":
                raise ValueError(f"NPS account {asset_id} does not exist")
            current = self._assets.get_nps_account(asset_id, uow=uow)
            mix_given = any(v not in (None, "") for v in (equity_pct, corporate_debt_pct, gov_bond_pct))
            equity = equity_pct or 0 if mix_given else current.equity_pct
            corporate = corporate_debt_pct or 0 if mix_given else current.corporate_debt_pct
            gov = gov_bond_pct or 0 if mix_given else current.gov_bond_pct
            self._check_nps_inputs(
                current.tier, employee_contribution, employer_contribution, equity, corporate, gov
            )
            self._assets.update_nps_account(
                asset_id,
                employee_contribution=employee_contribution,
                employer_contribution=employer_contribution,
                equity_pct=equity, corporate_debt_pct=corporate, gov_bond_pct=gov,
                uow=uow,
            )
            self._record_statement_value(asset_id, balance_date, balance, uow)
            self._refresh_nps_caches(asset_id, uow)
        return self._nps_summary(self._assets.get_asset(asset_id), self._assets.get_nps_account(asset_id))

    def list_nps_accounts(self, member_id: int) -> List[NpsSummary]:
        return [
            self._nps_summary(asset, self._assets.get_nps_account(asset.asset_id))
            for asset in self._assets.list_assets(member_id)
            if asset.category == "NPS"
        ]

    def get_nps_portfolio(self, family_id: int, member_id: Optional[int] = None) -> NpsPortfolio:
        members = self._portfolio_members(family_id, member_id)
        rows: List[NpsPortfolioRow] = []
        contributions = balance = tier1 = tier2 = _ZERO
        for member in members:
            for account in self.list_nps_accounts(member.member_id):
                rows.append(NpsPortfolioRow(owner_name=member.name, account=account))
                if account.status != "ACTIVE":
                    continue
                contributions += account.total_contributions
                balance += account.balance
                if account.tier == "I":
                    tier1 += account.balance
                else:
                    tier2 += account.balance
        gain = balance - contributions
        return NpsPortfolio(
            family_id=family_id,
            member_id=member_id,
            rows=tuple(rows),
            total_contributions=contributions,
            total_balance=balance,
            total_gain_loss=gain,
            total_gain_loss_pct=self._pct(gain, contributions),
            tier1_balance=tier1,
            tier2_balance=tier2,
        )

    # ------------------------------------------------------------------
    # Loans: terms and events stored, schedule computed on every read
    # ------------------------------------------------------------------

    LOAN_TYPES = ("HOME_LOAN", "CAR_LOAN", "PERSONAL_LOAN", "EDUCATION_LOAN", "OTHER")
    _LOAN_TYPE_LABEL = {
        "HOME_LOAN": "Home loan", "CAR_LOAN": "Car loan", "PERSONAL_LOAN": "Personal loan",
        "EDUCATION_LOAN": "Education loan", "OTHER": "Loan",
    }

    @staticmethod
    def _check_loan_terms(
        loan_type: str, lender: str, sanctioned_amount: Money, interest_rate: Money,
        tenure_months: int, pre_emi_months: int, emi_amount: Optional[Money],
        interest_type: str, down_payment: Optional[Money],
    ) -> None:
        if loan_type not in FinancialEngine.LOAN_TYPES:
            raise ValueError("loan type must be HOME_LOAN, CAR_LOAN, PERSONAL_LOAN, EDUCATION_LOAN or OTHER")
        if not (lender or "").strip():
            raise ValueError("lender is required")
        if _decimal(sanctioned_amount) <= _ZERO:
            raise ValueError("sanctioned amount must be positive")
        if not _ZERO <= _decimal(interest_rate) < Decimal("100"):
            raise ValueError("interest rate must be between 0 and 100%")
        if not 1 <= int(tenure_months) <= 600:
            raise ValueError("tenure must be 1-600 months")
        if not 0 <= int(pre_emi_months) < int(tenure_months):
            raise ValueError("pre-EMI months must be 0 or more, and less than the tenure")
        if emi_amount is not None and _decimal(emi_amount) <= _ZERO:
            raise ValueError("EMI must be positive")
        if interest_type not in ("FIXED", "FLOATING"):
            raise ValueError("interest type must be FIXED or FLOATING")
        if down_payment is not None and _decimal(down_payment) < _ZERO:
            raise ValueError("down payment cannot be negative")

    def _require_loan(self, liability_id: int, uow: Optional[UnitOfWork] = None) -> Loan:
        loan = self._loans.get_loan(liability_id, uow=uow)
        if loan is None:
            raise ValueError(f"loan {liability_id} does not exist")
        return loan

    @staticmethod
    def _pre_emi_months(loan: Loan) -> int:
        return month_no_of(loan.start_date, loan.emi_start_date) - 1

    def _loan_schedule(self, loan: Loan, uow: Optional[UnitOfWork] = None, *, prepayments=None,
                       emi: Optional[Decimal] = None):
        """The loan's schedule. `prepayments` replaces the stored ones (for
        comparisons); `emi` replaces the entered one."""
        disbursements = self._loans.list_disbursements(loan.liability_id, uow=uow)
        if prepayments is None:
            prepayments = [
                (p.prepayment_date, p.amount)
                for p in self._loans.list_prepayments(loan.liability_id, uow=uow)
            ]
        return compute_loan_schedule(
            start_date=loan.start_date,
            tenure_months=loan.tenure_months,
            pre_emi_months=self._pre_emi_months(loan),
            initial_rate=loan.initial_interest_rate,
            disbursements=[(d.disbursement_date, d.amount) for d in disbursements],
            prepayments=prepayments,
            rate_changes=[
                (c.effective_date, c.new_rate)
                for c in self._loans.list_rate_changes(loan.liability_id, uow=uow)
            ],
            emi=emi if emi is not None else loan.emi_amount,
        )

    def _rate_on(self, loan: Loan, on: date, changes: Sequence[LoanRateChange]) -> Decimal:
        rate = loan.initial_interest_rate
        for change in changes:
            if month_no_of(loan.start_date, change.effective_date) <= month_no_of(loan.start_date, on):
                rate = change.new_rate
        return rate

    def _loan_summary(self, loan: Loan, today: date) -> LoanSummary:
        disbursements = tuple(self._loans.list_disbursements(loan.liability_id))
        prepayments = self._loans.list_prepayments(loan.liability_id)
        changes = tuple(self._loans.list_rate_changes(loan.liability_id))
        schedule = self._loan_schedule(loan)
        rows = schedule.rows
        current = month_no_of(loan.start_date, today)
        done = [r for r in rows if r.month_no <= current]
        this_month = rows[current - 1] if 1 <= current <= len(rows) else None
        closed = loan.status == "CLOSED"

        if closed:
            phase = "CLOSED"
        elif current < 1:
            phase = "NOT_STARTED"
        elif this_month is None:
            phase = "REPAID"
        else:
            phase = this_month.phase

        total_interest = sum((r.interest for r in rows), _ZERO)
        prepaid_total = sum((r.prepaid for r in rows), _ZERO)
        interest_saved, months_saved = _ZERO, 0
        if prepayments:
            baseline = self._loan_schedule(loan, prepayments=[])
            interest_saved = sum((r.interest for r in baseline.rows), _ZERO) - total_interest
            months_saved = len(baseline.rows) - len(rows)

        by_month = {r.month_no: r for r in rows}
        prepayment_rows = []
        for p in prepayments:
            k = month_no_of(loan.start_date, p.prepayment_date)
            row = by_month.get(max(k, 1))
            prepayment_rows.append(LoanPrepaymentRow(
                prepayment_id=p.prepayment_id, prepayment_date=p.prepayment_date, amount=p.amount,
                applied=min(p.amount, row.prepaid) if row else _ZERO,
                outstanding_after=row.closing if row else _ZERO,
                months_left_after=len(rows) - row.month_no if row else 0,
            ))

        warnings: List[str] = []
        if schedule.emi_below_interest:
            warnings.append(
                "At some point the EMI does not cover the month's interest, so the loan is "
                "not being repaid then. Check the EMI and the interest rate."
            )
        if not schedule.paid_off:
            warnings.append("At this EMI the loan is not repaid within the schedule.")
        elif rows and len(rows) > loan.tenure_months:
            warnings.append(
                f"At this EMI the loan runs {len(rows) - loan.tenure_months} months past its "
                "original tenure."
            )

        outstanding = _ZERO if closed or not done else done[-1].closing
        paid = sum((r.payment for r in done), _ZERO)
        prepaid_to_date = sum((r.prepaid for r in done), _ZERO)
        total_disbursed = sum((d.amount for d in disbursements), _ZERO)
        return LoanSummary(
            liability_id=loan.liability_id,
            member_id=loan.member_id,
            loan_type=loan.loan_type,
            lender=loan.lender,
            status=loan.status,
            interest_type=loan.interest_type,
            sanctioned_amount=loan.sanctioned_amount,
            total_disbursed=total_disbursed,
            disbursed_to_date=sum(
                (d.amount for d in disbursements if d.disbursement_date <= today), _ZERO
            ),
            undisbursed=max(loan.sanctioned_amount - total_disbursed, _ZERO),
            down_payment=loan.down_payment,
            start_date=loan.start_date,
            tenure_months=loan.tenure_months,
            pre_emi_months=self._pre_emi_months(loan),
            first_emi_month=month_start(loan.start_date, schedule.first_emi_month_no),
            original_end_month=month_start(loan.start_date, loan.tenure_months),
            initial_interest_rate=loan.initial_interest_rate,
            interest_rate=self._rate_on(loan, today, changes),
            emi=schedule.emi,
            emi_entered=loan.emi_amount is not None,
            current_month_no=max(current, 0),
            phase=phase,
            outstanding=outstanding,
            this_month_payment=this_month.payment if this_month and not closed else _ZERO,
            this_month_interest=this_month.interest if this_month and not closed else _ZERO,
            interest_paid_to_date=sum((r.interest for r in done), _ZERO),
            principal_repaid_to_date=sum((r.principal_repaid for r in done), _ZERO),
            prepaid_to_date=prepaid_to_date,
            total_paid_to_date=paid + prepaid_to_date,
            total_interest=total_interest,
            total_payment=sum((r.payment for r in rows), _ZERO) + prepaid_total,
            payoff_month=rows[-1].month if rows and schedule.paid_off else None,
            months_remaining=0 if closed else max(len(rows) - max(current, 0), 0),
            interest_saved_by_prepayments=interest_saved,
            months_saved_by_prepayments=months_saved,
            warnings=tuple(warnings),
            schedule=rows,
            financial_years=summarize_by_financial_year(rows),
            disbursements=disbursements,
            prepayments=tuple(prepayment_rows),
            rate_changes=changes,
        )

    def _refresh_loan_caches(self, liability_id: int, uow: UnitOfWork) -> None:
        """Bring every value the Engine keeps for a loan in line with its
        rows: total disbursed, today's principal and rate, each prepayment's
        effect, and each rate change's previous rate."""
        loan = self._require_loan(liability_id, uow)
        today = date.today()
        disbursements = self._loans.list_disbursements(liability_id, uow=uow)
        self._loans.set_disbursed_amount(
            liability_id, sum((d.amount for d in disbursements), _ZERO), uow=uow
        )
        schedule = self._loan_schedule(loan, uow)
        rows = schedule.rows
        current = month_no_of(loan.start_date, today)
        done = [r for r in rows if r.month_no <= current]
        outstanding = _ZERO if loan.status == "CLOSED" or not done else done[-1].closing
        self._cache.set_liability_outstanding(liability_id, outstanding, uow=uow)
        changes = self._loans.list_rate_changes(liability_id, uow=uow)
        self._cache.set_loan_interest_rate(liability_id, self._rate_on(loan, today, changes), uow=uow)
        previous = loan.initial_interest_rate
        for change in changes:
            if change.old_rate != previous:
                self._loans.set_rate_change_old_rate(change.rate_change_id, previous, uow=uow)
            previous = change.new_rate
        by_month = {r.month_no: r for r in rows}
        for p in self._loans.list_prepayments(liability_id, uow=uow):
            row = by_month.get(max(month_no_of(loan.start_date, p.prepayment_date), 1))
            self._loans.set_prepayment_effect(
                p.prepayment_id,
                min(p.amount, row.prepaid) if row else _ZERO,
                row.closing if row else _ZERO,
                len(rows) - row.month_no if row else None,
                uow=uow,
            )

    def _check_event_date(self, loan: Loan, on: date) -> None:
        if month_no_of(loan.start_date, on) < 1:
            raise ValueError(f"the date must be in or after the loan's first month ({loan.start_date})")

    def create_loan(
        self,
        *,
        member_id: int,
        loan_type: str,
        lender: str,
        sanctioned_amount: Money,
        interest_rate: Money,
        tenure_months: int,
        start_date: date,
        pre_emi_months: int = 0,
        emi_amount: Optional[Money] = None,
        first_disbursement: Optional[Money] = None,
        interest_type: str = "FLOATING",
        down_payment: Optional[Money] = None,
    ) -> LoanSummary:
        """Add a loan. `first_disbursement` is paid out on `start_date` — the
        whole sanctioned amount when not given (a car or personal loan); a
        construction-linked home loan adds its later stages as disbursements.
        `emi_amount` is the bank's EMI; left out, the Engine works it out."""
        self._check_loan_terms(
            loan_type, lender, sanctioned_amount, interest_rate, tenure_months, pre_emi_months,
            emi_amount, interest_type, down_payment,
        )
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        first = _decimal(sanctioned_amount if first_disbursement in (None, "") else first_disbursement)
        if first <= _ZERO or first > _decimal(sanctioned_amount):
            raise ValueError("the first disbursement must be positive and at most the sanctioned amount")
        with self._db.unit_of_work() as uow:
            loan = self._loans.create_loan(
                member_id, loan_type, lender.strip(), sanctioned_amount, start_date,
                interest_type=interest_type, tenure_months=int(tenure_months),
                emi_amount=emi_amount,
                emi_start_date=month_start(start_date, int(pre_emi_months) + 1),
                initial_interest_rate=interest_rate, down_payment=down_payment, uow=uow,
            )
            self._loans.add_disbursement(loan.liability_id, start_date, first, "Initial", uow=uow)
            self._refresh_loan_caches(loan.liability_id, uow)
        return self._loan_summary(self._require_loan(loan.liability_id), date.today())

    def update_loan(
        self,
        *,
        liability_id: int,
        member_id: int,
        loan_type: str,
        lender: str,
        sanctioned_amount: Money,
        interest_rate: Money,
        tenure_months: int,
        start_date: date,
        pre_emi_months: int = 0,
        emi_amount: Optional[Money] = None,
        interest_type: str = "FLOATING",
        down_payment: Optional[Money] = None,
        status: str = "ACTIVE",
    ) -> LoanSummary:
        """Correct a loan's terms (`interest_rate` is the rate it started
        at; later changes are rate-change events), owner or status."""
        self._check_loan_terms(
            loan_type, lender, sanctioned_amount, interest_rate, tenure_months, pre_emi_months,
            emi_amount, interest_type, down_payment,
        )
        if status not in ("ACTIVE", "CLOSED"):
            raise ValueError("status must be ACTIVE or CLOSED")
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        with self._db.unit_of_work() as uow:
            self._require_loan(liability_id, uow)
            disbursed = sum(
                (d.amount for d in self._loans.list_disbursements(liability_id, uow=uow)), _ZERO
            )
            if disbursed > _decimal(sanctioned_amount):
                raise ValueError("the sanctioned amount cannot be less than what has been disbursed")
            dates = (
                [d.disbursement_date for d in self._loans.list_disbursements(liability_id, uow=uow)]
                + [p.prepayment_date for p in self._loans.list_prepayments(liability_id, uow=uow)]
                + [c.effective_date for c in self._loans.list_rate_changes(liability_id, uow=uow)]
            )
            if any(month_no_of(start_date, d) < 1 for d in dates):
                raise ValueError("the loan cannot start after its own disbursements or prepayments")
            self._loans.update_loan(
                liability_id, member_id, loan_type, lender.strip(), sanctioned_amount, start_date,
                status, interest_type=interest_type, tenure_months=int(tenure_months),
                emi_amount=emi_amount,
                emi_start_date=month_start(start_date, int(pre_emi_months) + 1),
                initial_interest_rate=interest_rate, down_payment=down_payment, uow=uow,
            )
            self._refresh_loan_caches(liability_id, uow)
        return self._loan_summary(self._require_loan(liability_id), date.today())

    def delete_loan(self, liability_id: int) -> None:
        with self._db.unit_of_work() as uow:
            self._require_loan(liability_id, uow)
            self._loans.delete_loan(liability_id, uow=uow)

    def add_loan_disbursement(
        self, *, liability_id: int, disbursement_date: date, amount: Money,
        note: Optional[str] = None,
    ) -> LoanSummary:
        """A stage paid out (or, dated in the future, expected)."""
        if _decimal(amount) <= _ZERO:
            raise ValueError("amount must be positive")
        with self._db.unit_of_work() as uow:
            loan = self._require_loan(liability_id, uow)
            self._check_event_date(loan, disbursement_date)
            disbursed = sum(
                (d.amount for d in self._loans.list_disbursements(liability_id, uow=uow)), _ZERO
            )
            if disbursed + _decimal(amount) > loan.sanctioned_amount:
                raise ValueError(
                    f"that would disburse more than the sanctioned ₹{loan.sanctioned_amount} "
                    f"(₹{loan.sanctioned_amount - disbursed} is left)"
                )
            self._loans.add_disbursement(
                liability_id, disbursement_date, amount, (note or "").strip() or None, uow=uow
            )
            self._refresh_loan_caches(liability_id, uow)
        return self._loan_summary(self._require_loan(liability_id), date.today())

    def delete_loan_disbursement(self, disbursement_id: int) -> None:
        with self._db.unit_of_work() as uow:
            disbursement = self._loans.get_disbursement(disbursement_id, uow=uow)
            if disbursement is None:
                raise ValueError(f"disbursement {disbursement_id} does not exist")
            others = [
                d for d in self._loans.list_disbursements(disbursement.liability_id, uow=uow)
                if d.disbursement_id != disbursement_id
            ]
            if not others:
                raise ValueError("a loan needs at least one disbursement; delete the loan instead")
            self._loans.delete_disbursement(disbursement_id, uow=uow)
            self._refresh_loan_caches(disbursement.liability_id, uow)

    def add_loan_prepayment(
        self, *, liability_id: int, prepayment_date: date, amount: Money
    ) -> LoanSummary:
        """A part-prepayment (or, dated in the future, a planned one)."""
        if _decimal(amount) <= _ZERO:
            raise ValueError("amount must be positive")
        with self._db.unit_of_work() as uow:
            loan = self._require_loan(liability_id, uow)
            self._check_event_date(loan, prepayment_date)
            self._loans.add_prepayment(liability_id, prepayment_date, amount, uow=uow)
            self._refresh_loan_caches(liability_id, uow)
        return self._loan_summary(self._require_loan(liability_id), date.today())

    def delete_loan_prepayment(self, prepayment_id: int) -> None:
        with self._db.unit_of_work() as uow:
            liability_id = self._loans.get_prepayment_loan_id(prepayment_id, uow=uow)
            if liability_id is None:
                raise ValueError(f"prepayment {prepayment_id} does not exist")
            self._loans.delete_prepayment(prepayment_id, uow=uow)
            self._refresh_loan_caches(liability_id, uow)

    def add_loan_rate_change(
        self, *, liability_id: int, effective_date: date, new_rate: Money,
        reason: Optional[str] = None,
    ) -> LoanSummary:
        """The bank changed the rate from `effective_date`'s month on. The
        EMI stays the same; the tenure moves."""
        if not _ZERO <= _decimal(new_rate) < Decimal("100"):
            raise ValueError("interest rate must be between 0 and 100%")
        with self._db.unit_of_work() as uow:
            loan = self._require_loan(liability_id, uow)
            self._check_event_date(loan, effective_date)
            changes = self._loans.list_rate_changes(liability_id, uow=uow)
            old = self._rate_on(loan, effective_date, changes)
            self._loans.add_rate_change(
                liability_id, effective_date, old, new_rate, (reason or "").strip() or None, uow=uow
            )
            self._refresh_loan_caches(liability_id, uow)
        return self._loan_summary(self._require_loan(liability_id), date.today())

    def delete_loan_rate_change(self, rate_change_id: int) -> None:
        with self._db.unit_of_work() as uow:
            liability_id = self._loans.get_rate_change_loan_id(rate_change_id, uow=uow)
            if liability_id is None:
                raise ValueError(f"rate change {rate_change_id} does not exist")
            self._loans.delete_rate_change(rate_change_id, uow=uow)
            self._refresh_loan_caches(liability_id, uow)

    def list_loans(self, member_id: int, *, today: Optional[date] = None) -> List[LoanSummary]:
        return [
            self._loan_summary(loan, today or date.today())
            for loan in self._loans.list_loans(member_id)
        ]

    def get_loan_portfolio(
        self, family_id: int, member_id: Optional[int] = None, *, today: Optional[date] = None
    ) -> LoanPortfolio:
        members = self._portfolio_members(family_id, member_id)
        rows: List[LoanPortfolioRow] = []
        outstanding = sanctioned = disbursed = payment = interest_paid = interest_left = _ZERO
        for member in members:
            for loan in self.list_loans(member.member_id, today=today):
                rows.append(LoanPortfolioRow(owner_name=member.name, loan=loan))
                if loan.status != "ACTIVE":
                    continue
                outstanding += loan.outstanding
                sanctioned += loan.sanctioned_amount
                disbursed += loan.disbursed_to_date
                payment += loan.this_month_payment
                interest_paid += loan.interest_paid_to_date
                interest_left += loan.total_interest - loan.interest_paid_to_date
        return LoanPortfolio(
            family_id=family_id, member_id=member_id, rows=tuple(rows),
            total_outstanding=outstanding, total_sanctioned=sanctioned,
            total_disbursed_to_date=disbursed, this_month_payment=payment,
            total_interest_paid_to_date=interest_paid, total_interest_remaining=interest_left,
        )

    def loan_what_if(
        self, *, liability_id: int, amount: Money, on_date: date, extra_monthly: Money = 0
    ) -> LoanWhatIf:
        """Try a prepayment on a loan without saving it (see `LoanWhatIf`)."""
        lump, extra = _decimal(amount or 0), _decimal(extra_monthly or 0)
        if lump < _ZERO or extra < _ZERO or (lump == _ZERO and extra == _ZERO):
            raise ValueError("enter a prepayment amount, a monthly extra, or both")
        loan = self._require_loan(liability_id)
        self._check_event_date(loan, on_date)
        before = self._loan_schedule(loan)
        planned = [
            (p.prepayment_date, p.amount) for p in self._loans.list_prepayments(liability_id)
        ]
        if lump > _ZERO:
            planned.append((on_date, lump))
        if extra > _ZERO:
            first = month_no_of(loan.start_date, on_date)
            planned += [
                (month_start(loan.start_date, k), extra)
                for k in range(first, len(before.rows) + 1)
            ]
        after = self._loan_schedule(loan, prepayments=planned)
        interest_before = sum((r.interest for r in before.rows), _ZERO)
        interest_after = sum((r.interest for r in after.rows), _ZERO)
        return LoanWhatIf(
            liability_id=liability_id, amount=lump, on_date=on_date, extra_monthly=extra,
            emi_before=before.emi, emi_after=after.emi,
            payoff_before=before.rows[-1].month if before.paid_off else None,
            payoff_after=after.rows[-1].month if after.paid_off else None,
            months_saved=len(before.rows) - len(after.rows),
            total_interest_before=interest_before, total_interest_after=interest_after,
            interest_saved=interest_before - interest_after,
        )

    # ------------------------------------------------------------------
    # Monthly cash flow: income, expenses, investments for one month
    # ------------------------------------------------------------------

    _PER_YEAR = {"MONTHLY": 12, "QUARTERLY": 4, "ANNUAL": 1}
    _FLOW_TYPES = ("INCOME", "EXPENSE", "INVESTMENT")

    def update_mf_sip(
        self, *, asset_id: int, sip_amount: Optional[Money], sip_date: Optional[int] = None
    ) -> MfSummary:
        """Set a fund's running SIP amount per month, or stop it (`None`)."""
        amount = None if sip_amount in (None, "") else _decimal(sip_amount)
        if amount is not None and amount <= _ZERO:
            raise ValueError("SIP amount must be positive")
        if sip_date is not None and not 1 <= int(sip_date) <= 31:
            raise ValueError("SIP date must be a day of the month (1-31)")
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "MF":
                raise ValueError(f"MF holding {asset_id} does not exist")
            self._assets.update_mf_sip(
                asset_id, sip_enabled=amount is not None, sip_amount=amount,
                sip_date=None if sip_date is None else int(sip_date), uow=uow,
            )
        return self._mf_summary(
            self._assets.get_asset(asset_id), self._assets.get_mutual_fund(asset_id),
            self._mf_txns.list_transactions(asset_id), date.today(),
        )

    MF_INVESTED_BY = ("LUMPSUM", "SIP_RUNNING", "SIP_STOPPED")

    def complete_mf_purchase_details(
        self,
        *,
        asset_id: int,
        invested_by: str,
        purchase_date: Optional[date] = None,
        amount: Optional[Money] = None,
        current_xirr_pct: Optional[Money] = None,
        sip_amount: Optional[Money] = None,
        sip_date: Optional[int] = None,
        lumpsum_amount: Optional[Money] = None,
        lumpsum_date: Optional[date] = None,
        lumpsum_units: Optional[Money] = None,
        today: Optional[date] = None,
    ) -> MfSummary:
        """Turn a fund's imported opening balance into a dated purchase, so
        its XIRR can be worked out.

        SIP + lumpsum in one fund: with SIP_RUNNING / SIP_STOPPED, also give
        `lumpsum_amount` and `lumpsum_date` (and `lumpsum_units` if known —
        otherwise the units are shared out at the fund's average cost). The
        lumpsum becomes its own dated purchase; the rest of the opening
        balance is the SIP part, dated to match `current_xirr_pct`.

        LUMPSUM: it was bought on `purchase_date` (for `amount`, default the
        statement's cost). SIP_RUNNING / SIP_STOPPED: `current_xirr_pct` is
        the XIRR the fund app shows today; the opening balance is dated on
        the one day that gives exactly that XIRR for today's value, alongside
        the instalments recorded after it. That date is an equivalent, not a
        real purchase: exact today, and a close guide as later SIPs add up.
        A running SIP also gets its monthly amount / day when given."""
        if invested_by not in self.MF_INVESTED_BY:
            raise ValueError("choose Lumpsum, SIP (running) or SIP (stopped)")
        today = today or date.today()
        with self._db.unit_of_work() as uow:
            asset = self._assets.get_asset(asset_id, uow=uow)
            if asset is None or asset.category != "MF":
                raise ValueError(f"MF holding {asset_id} does not exist")
            holding = self._assets.get_mutual_fund(asset_id, uow=uow)
            active = [t for t in self._mf_txns.list_transactions(asset_id, uow=uow) if t.status == "ACTIVE"]
            opening = next((t for t in active if t.is_opening_balance), None)
            if opening is None:
                raise ValueError("this fund has no imported opening balance to complete")
            later = [t for t in active if t.mf_txn_id != opening.mf_txn_id]
            first_later = min((t.txn_date for t in later), default=None)

            if invested_by == "LUMPSUM":
                if purchase_date is None:
                    raise ValueError("enter the purchase date")
                if purchase_date > opening.txn_date or purchase_date > today:
                    raise ValueError(f"the purchase date must be on or before {opening.txn_date:%d %b %Y}")
                cost = opening.amount if amount in (None, "") else _decimal(amount)
                if cost <= _ZERO:
                    raise ValueError("amount must be positive")
                on, kind = purchase_date, "LUMPSUM"
                note = "Lumpsum purchase (date entered after the CAS import)"
            elif lumpsum_amount not in (None, "") and self._lumpsum_is_everything(
                _decimal(lumpsum_amount), opening.amount
            ):
                # The whole opening balance was the lumpsum (a few rupees off is
                # stamp duty): it is simply dated, and the SIPs are the later
                # instalments on their own dates — no XIRR needed.
                if lumpsum_date is None:
                    raise ValueError("enter the lumpsum's date")
                if lumpsum_date > opening.txn_date or lumpsum_date > today:
                    raise ValueError(f"the lumpsum date must be on or before {opening.txn_date:%d %b %Y}")
                on, kind, cost = lumpsum_date, "LUMPSUM", opening.amount
                note = "Lumpsum (the whole balance before the CAS import; SIPs started after it)"
            else:
                if current_xirr_pct in (None, ""):
                    raise ValueError("enter the fund's current XIRR %")
                rate = _decimal(current_xirr_pct) / Decimal("100")
                value_now = self._current_value_for_asset(asset, today, uow=uow)  # before any row changes
                sip_units, sip_cost = opening.units, opening.amount
                known = list(later)
                has_lump = lumpsum_amount not in (None, "")
                if has_lump:
                    lump = _decimal(lumpsum_amount)
                    if lumpsum_date is None:
                        raise ValueError("enter the lumpsum's date")
                    if lumpsum_date > opening.txn_date:
                        raise ValueError(f"the lumpsum date must be on or before {opening.txn_date:%d %b %Y}")
                    if lump <= _ZERO:
                        raise ValueError("the lumpsum must be positive")
                    if lump > opening.amount:
                        raise ValueError(
                            f"the lumpsum is more than the fund's earlier cost of ₹{opening.amount}"
                        )
                    lump_units = (
                        _decimal(lumpsum_units) if lumpsum_units not in (None, "")
                        else (opening.units * lump / opening.amount).quantize(Decimal("0.001"))
                    )
                    if not _ZERO < lump_units < opening.units:
                        raise ValueError(f"the lumpsum units must be less than {opening.units}")
                    known.append(self._mf_txns.create_transaction(
                        asset_id, lumpsum_date, "LUMPSUM", lump_units,
                        (lump / lump_units).quantize(_NAV_PRECISION), lump,
                        description="Lumpsum (entered with the purchase details)", uow=uow,
                    ))
                    sip_units, sip_cost = opening.units - lump_units, opening.amount - lump
                try:
                    on = self._equivalent_purchase_date(value_now, sip_cost, known, rate, today)
                except ValueError as exc:
                    if has_lump and ("too high" in str(exc) or "future" in str(exc)):
                        raise ValueError(
                            f"the lumpsum alone, at {current_xirr_pct}% a year, comes to more than (or nearly all of) what the fund "
                            f"is worth today, so the ₹{sip_cost} SIP part cannot be fitted in. If all of the "
                            f"₹{opening.amount} was the lumpsum, enter that as the lumpsum amount; otherwise "
                            f"check the lumpsum amount, its date and the XIRR."
                        ) from exc
                    raise
                if first_later is not None and on > first_later:
                    raise ValueError(
                        f"at {current_xirr_pct}% the earlier money would have gone in after "
                        f"{first_later:%d %b %Y}; check the XIRR"
                    )
                cost, kind = sip_cost, "SIP"
                note = (f"SIPs before the CAS import, dated to match the {_decimal(current_xirr_pct)}% "
                        f"XIRR entered on {today:%d %b %Y}")
            units = opening.units if kind == "LUMPSUM" else sip_units
            self._mf_txns.update_transaction(
                opening.mf_txn_id, txn_date=on, txn_type=kind,
                nav=(cost / units).quantize(_NAV_PRECISION), amount=cost,
                description=note, is_opening_balance=False, units=units, uow=uow,
            )
            if invested_by == "SIP_RUNNING":
                per_month = holding.sip_amount if sip_amount in (None, "") else _decimal(sip_amount)
                if per_month is not None and per_month <= _ZERO:
                    raise ValueError("SIP amount must be positive")
                day = holding.sip_date if sip_date in (None, "") else int(sip_date)
                if day is not None and not 1 <= day <= 31:
                    raise ValueError("SIP date must be a day of the month (1-31)")
                self._assets.update_mf_sip(
                    asset_id, sip_enabled=True, sip_amount=per_month, sip_date=day, uow=uow
                )
            else:
                self._assets.update_mf_sip(
                    asset_id, sip_enabled=False, sip_amount=holding.sip_amount,
                    sip_date=holding.sip_date, uow=uow,
                )
            self._refresh_mf_caches(asset_id, uow)
        return self._mf_summary(
            self._assets.get_asset(asset_id), self._assets.get_mutual_fund(asset_id),
            self._mf_txns.list_transactions(asset_id), today,
        )

    @staticmethod
    def _lumpsum_is_everything(lump: Decimal, opening_amount: Decimal) -> bool:
        """A lumpsum within ₹100 or 1% of the opening balance is all of it."""
        gap = opening_amount - lump
        return gap <= max(Decimal("100"), opening_amount / Decimal("100")) and lump <= opening_amount * Decimal("1.01")

    @staticmethod
    def _equivalent_purchase_date(value: Decimal, opening_amount: Decimal, later, rate: Decimal, today: date) -> date:
        """The date D for the opening amount C such that the fund's XIRR is
        exactly `rate`: C*(1+r)^((today-D)/365) + the later purchases grown
        the same way = today's value. Solved in closed form."""
        if rate <= Decimal("-1"):
            raise ValueError("XIRR must be above -100%")
        if rate == _ZERO:
            raise ValueError("enter a non-zero XIRR")
        if value <= _ZERO:
            raise ValueError("the fund has no current value yet (no NAV); sync NAVs first")
        growth = Decimal("1") + rate
        grown_later = sum(
            (t.amount * growth ** (Decimal((today - t.txn_date).days) / Decimal("365")) for t in later),
            _ZERO,
        )
        left = value - grown_later
        if left <= _ZERO:
            raise ValueError(
                "that XIRR is too high: the purchases after the opening balance would already be worth "
                "more than the fund is today. Check the XIRR (a loss is entered with a minus sign)."
            )
        days = Decimal("365") * (left / opening_amount).ln() / growth.ln()
        if days < _ZERO:
            raise ValueError(
                "that XIRR does not fit the fund's cost and value (it would mean investing in the future)"
            )
        whole = int(days.to_integral_value())
        if whole > 365 * 40:
            raise ValueError("that XIRR would date the investment over 40 years back; check it")
        return today - timedelta(days=whole)

    _PAYMENT_MODES = ("CASH", "CARD", "UPI", "BANK_TRANSFER", "AUTO_DEBIT")

    def _check_cash_flow_item(
        self, member_id: int, flow_type: str, name: str, amount: Money, frequency: str,
        category: Optional[str], payment_mode: Optional[str], account_id: Optional[int],
        spent_on: Optional[date] = None,
    ) -> Tuple[str, Optional[int]]:
        """Validate a row; returns (name to store, account to store). A CASH
        row never names a bank account; a named account must be one of the
        member's own active accounts."""
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        if flow_type not in self._FLOW_TYPES:
            raise ValueError("type must be INCOME, EXPENSE or INVESTMENT")
        if frequency not in self._PER_YEAR and frequency != "ONCE":
            raise ValueError("frequency must be MONTHLY, QUARTERLY, ANNUAL or ONCE")
        if frequency == "ONCE" and spent_on is None:
            raise ValueError("enter the date of the one-time amount")
        if payment_mode is not None and payment_mode not in self._PAYMENT_MODES:
            raise ValueError("paid by must be CASH, CARD, UPI, BANK_TRANSFER or AUTO_DEBIT")
        name = (name or "").strip() or (category or "").strip()
        if not name:
            raise ValueError("name or category is required")
        if _decimal(amount) <= _ZERO:
            raise ValueError("amount must be positive")
        if payment_mode == "CASH":
            return name, None
        if account_id is not None:
            account = self._bank.get_account(int(account_id))
            if account is None or account.member_id != member_id or not account.is_active:
                raise ValueError("choose one of this member's own bank accounts")
            return name, account.account_id
        return name, None

    def add_cash_flow_item(
        self, *, member_id: int, flow_type: str, name: str, amount: Money, frequency: str = "MONTHLY",
        category: Optional[str] = None, payment_mode: Optional[str] = None,
        account_id: Optional[int] = None, spent_on: Optional[date] = None,
    ) -> CashFlowItem:
        """Add a row. A regular row (MONTHLY / QUARTERLY / ANNUAL) counts every
        month; a ONCE (ad hoc) row counts only in the month of `spent_on`."""
        name, account_id = self._check_cash_flow_item(
            member_id, flow_type, name, amount, frequency, category, payment_mode, account_id, spent_on
        )
        return self._cash_flow.create_item(
            member_id, flow_type, name, amount, frequency,
            category=category or None, payment_mode=payment_mode, account_id=account_id,
            spent_on=spent_on if frequency == "ONCE" else None,
        )

    def update_cash_flow_item(
        self, *, item_id: int, member_id: int, flow_type: str, name: str, amount: Money,
        frequency: str = "MONTHLY", category: Optional[str] = None,
        payment_mode: Optional[str] = None, account_id: Optional[int] = None,
        spent_on: Optional[date] = None,
    ) -> CashFlowItem:
        name, account_id = self._check_cash_flow_item(
            member_id, flow_type, name, amount, frequency, category, payment_mode, account_id, spent_on
        )
        item = self._cash_flow.update_item(
            item_id, member_id, flow_type, name, amount, frequency,
            category=category or None, payment_mode=payment_mode, account_id=account_id,
            spent_on=spent_on if frequency == "ONCE" else None,
        )
        if item is None:
            raise ValueError(f"cash-flow item {item_id} does not exist")
        return item

    # -------------------------------------------------- bank accounts

    def add_bank_account(
        self, *, member_id: int, bank_name: str, balance: Money, balance_as_of: date,
        account_last4: str = "", make_default: bool = False,
    ) -> BankAccount:
        """Add a member's account with its balance on `balance_as_of`. A
        member's first account becomes their default automatically."""
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        if not (bank_name or "").strip():
            raise ValueError("bank name is required")
        last4 = (account_last4 or "").strip()
        if last4 and (not last4.isdigit() or len(last4) > 4):
            raise ValueError("account number: last 4 digits only")
        with self._db.unit_of_work() as uow:
            existing = [a for a in self._bank.list_accounts(member_id, uow=uow) if a.is_active]
            account = self._bank.create_account(
                member_id, bank_name.strip(), "SAVINGS", last4, _decimal(balance),
                [(member_id, Decimal("100"))], balance_as_of=balance_as_of, uow=uow,
            )
            if make_default or not any(a.is_default for a in existing):
                self._bank.set_default(member_id, account.account_id, uow=uow)
        return self._bank.get_account(account.account_id)

    def list_family_bank_accounts(self, family_id: int) -> List[BankAccount]:
        """Every member's active accounts — for the account pickers."""
        return [
            account
            for member in self._portfolio_members(family_id, None)
            for account in self._bank.list_accounts(member.member_id)
            if account.is_active
        ]

    def _require_account(self, account_id: int) -> BankAccount:
        account = self._bank.get_account(account_id)
        if account is None:
            raise ValueError(f"bank account {account_id} does not exist")
        return account

    def update_bank_account(
        self, *, account_id: int, bank_name: str, account_last4: str = "",
        make_default: bool = False,
    ) -> BankAccount:
        account = self._require_account(account_id)
        last4 = (account_last4 or "").strip()
        if last4 and (not last4.isdigit() or len(last4) > 4):
            raise ValueError("account number: last 4 digits only")
        if not (bank_name or "").strip():
            raise ValueError("bank name is required")
        with self._db.unit_of_work() as uow:
            self._bank.update_details(
                account_id, bank_name=bank_name.strip(), account_number_masked=last4, uow=uow
            )
            if make_default:
                self._bank.set_default(account.member_id, account_id, uow=uow)
        return self._bank.get_account(account_id)

    def set_bank_balance(self, *, account_id: int, balance: Money, as_of: date) -> BankAccount:
        """Re-enter the real balance (from the bank app): the estimate
        restarts from here."""
        self._require_account(account_id)
        return self._bank.set_entered_balance(account_id, _decimal(balance), as_of)

    def close_bank_account(self, account_id: int) -> None:
        """Stop using an account (kept for history). Rows that named it fall
        back to the member's default account."""
        account = self._require_account(account_id)
        with self._db.unit_of_work() as uow:
            self._bank.set_active(account_id, False, uow=uow)
            if account.is_default:
                others = [
                    a for a in self._bank.list_accounts(account.member_id, uow=uow)
                    if a.is_active and a.account_id != account_id
                ]
                if others:
                    self._bank.set_default(account.member_id, others[0].account_id, uow=uow)

    def delete_cash_flow_item(self, item_id: int) -> None:
        if not self._cash_flow.delete_item(item_id):
            raise ValueError(f"cash-flow item {item_id} does not exist")

    def set_premium_paid(self, *, policy_id: int, due_date: date, paid: bool) -> None:
        """Tick (or untick) "premium paid" for one installment. `due_date`
        must be one of the policy's actual due dates."""
        policy = self._insurance.get_policy(policy_id)
        if policy is None:
            raise ValueError(f"insurance policy {policy_id} does not exist")
        dues = premium_due_dates_between(
            start_date=policy.start_date, premium_frequency=policy.premium_frequency,
            maturity_date=self._premiums_end(policy), first=due_date, last=due_date,
        )
        if not dues:
            raise ValueError(f"no premium of policy {policy.policy_number} is due on {due_date}")
        self._cash_flow.set_payment_status(
            policy_id, due_date, policy.premium_amount,
            "PAID" if paid else "DUE", date.today() if paid else None,
        )

    def _monthly_equivalent(self, amount: Decimal, frequency: str) -> Decimal:
        if frequency == "ONCE":
            return amount
        return (amount * Decimal(self._PER_YEAR[frequency]) / Decimal("12")).quantize(
            Decimal("0.01")
        )

    @staticmethod
    def _months_between(earlier: date, later: date) -> int:
        """Whole months from `earlier` to `later` (0 if later is earlier)."""
        months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
        if later.day < earlier.day:
            months -= 1
        return max(months, 0)

    def _cash_flow_lines(self, members, first: date, last: date) -> List[CashFlowLine]:
        """Every cash-flow line of `members` for the month `first`..`last`."""
        lines: List[CashFlowLine] = []
        for member in members:
            name = member.name
            accounts = {
                a.account_id: a for a in self._bank.list_accounts(member.member_id) if a.is_active
            }
            default = next((a for a in accounts.values() if a.is_default), None)

            def label_of(account) -> Optional[str]:
                if account is None:
                    return None
                return account.bank_name + (f" ••{account.account_number_masked}"
                                            if account.account_number_masked else "")

            def via_default(**kw) -> CashFlowLine:
                return CashFlowLine(
                    member_id=member.member_id, owner_name=name,
                    account_id=default.account_id if default else None,
                    account_label=label_of(default), **kw,
                )

            for fd in self.list_fds(member.member_id):
                if (
                    fd.status == "ACTIVE" and fd.payout_interest is not None
                    and fd.issue_date <= last and fd.maturity_date > first
                ):
                    per_month = (
                        fd.payout_interest / Decimal("3") if fd.payout_type == "QUARTERLY"
                        else fd.payout_interest
                    ).quantize(Decimal("0.01"))
                    lines.append(via_default(
                        source="FD_INTEREST", flow_type="INCOME",
                        label=f"FD interest — {fd.bank_name} {fd.fd_number}",
                        amount=fd.payout_interest, frequency=fd.payout_type,
                        monthly_amount=per_month, counted=True, category="FD interest",
                    ))
            for mf in self.list_mfs(member.member_id):
                if mf.status == "ACTIVE" and mf.sip_enabled and mf.sip_amount:
                    lines.append(via_default(
                        source="SIP", flow_type="INVESTMENT", label=f"SIP — {mf.scheme_name}",
                        amount=mf.sip_amount, frequency="MONTHLY",
                        monthly_amount=mf.sip_amount, counted=True, category="MF SIP",
                        payment_mode="AUTO_DEBIT",
                    ))
            for loan in self._loans.list_loans(member.member_id):
                if loan.status != "ACTIVE":
                    continue
                rows = self._loan_schedule(loan).rows
                k = month_no_of(loan.start_date, first)
                if not 1 <= k <= len(rows):
                    continue
                row = rows[k - 1]
                kind = self._LOAN_TYPE_LABEL[loan.loan_type]
                if row.payment > _ZERO:
                    pre_emi = row.phase == PRE_EMI
                    lines.append(via_default(
                        source="LOAN_EMI", flow_type="EXPENSE",
                        label=f"{'Pre-EMI interest' if pre_emi else 'EMI'} — {kind}, {loan.lender}",
                        amount=row.payment, frequency="MONTHLY", monthly_amount=row.payment,
                        counted=True, category="Loan EMI", payment_mode="AUTO_DEBIT",
                    ))
                if row.prepaid > _ZERO:
                    lines.append(via_default(
                        source="LOAN_PREPAYMENT", flow_type="EXPENSE",
                        label=f"Prepayment — {kind}, {loan.lender}",
                        amount=row.prepaid, frequency="ONCE", monthly_amount=row.prepaid,
                        counted=True, category="Loan prepayment",
                    ))
            for item in self._cash_flow.list_items(member.member_id):
                if item.frequency == "ONCE" and not (item.spent_on and first <= item.spent_on <= last):
                    continue  # a one-time row belongs to its own month only
                if item.payment_mode == "CASH":
                    account = None
                else:
                    account = accounts.get(item.account_id) or default
                lines.append(CashFlowLine(
                    source="ITEM", flow_type=item.flow_type, member_id=member.member_id,
                    owner_name=name, label=item.name, amount=item.amount,
                    frequency=item.frequency,
                    monthly_amount=self._monthly_equivalent(item.amount, item.frequency),
                    counted=True, item_id=item.item_id, category=item.category,
                    payment_mode=item.payment_mode,
                    account_id=account.account_id if account else None,
                    account_label=label_of(account), spent_on=item.spent_on,
                ))
            for policy in self._insurance.list_policies(member.member_id):
                if policy.status != "ACTIVE":
                    continue
                for due in premium_due_dates_between(
                    start_date=policy.start_date, premium_frequency=policy.premium_frequency,
                    maturity_date=self._premiums_end(policy), first=first, last=last,
                ):
                    payment = self._cash_flow.get_payment(policy.policy_id, due)
                    paid = payment is not None and payment.status == "PAID"
                    lines.append(via_default(
                        source="PREMIUM",
                        flow_type="INVESTMENT" if policy.policy_type in SAVINGS_POLICY_TYPES else "EXPENSE",
                        label=f"Premium — {policy.insurer} {policy.policy_number}",
                        amount=policy.premium_amount, frequency=policy.premium_frequency,
                        monthly_amount=policy.premium_amount, counted=paid,
                        policy_id=policy.policy_id, due_date=due, paid=paid,
                        category="Insurance premium",
                    ))
        return lines

    @staticmethod
    def _month_bounds(year: int, month: int) -> Tuple[date, date]:
        first = date(year, month, 1)
        return first, date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)

    @staticmethod
    def _signed(line: CashFlowLine) -> Decimal:
        return line.monthly_amount if line.flow_type == "INCOME" else -line.monthly_amount

    def _bank_accounts_for(self, members, today: date) -> Tuple[List[BankAccountSummary], Decimal]:
        """Estimated balances for `members`' active accounts, and the net
        monthly bank flow that has no account to go to."""
        summaries: List[BankAccountSummary] = []
        _, this_last = self._month_bounds(today.year, today.month)
        current_lines = self._cash_flow_lines(members, date(today.year, today.month, 1), this_last)
        def one_time(l: CashFlowLine) -> bool:
            return l.source == "ITEM" and l.frequency == "ONCE"

        unassigned = sum(
            (self._signed(l) for l in current_lines
             if l.source != "PREMIUM" and l.payment_mode != "CASH" and l.account_id is None
             and not one_time(l)),
            _ZERO,
        )
        for member in members:
            for account in self._bank.list_accounts(member.member_id):
                if not account.is_active:
                    continue
                as_of = account.balance_as_of or today
                months = self._months_between(as_of, today)
                balance = account.opening_balance
                # Regular rows: one month's flow for each whole month elapsed,
                # using that month's lines (FDs and SIPs start and stop).
                for k in range(1, months + 1):
                    index = as_of.month - 1 + k
                    year, month = as_of.year + index // 12, index % 12 + 1
                    first, last = self._month_bounds(year, month)
                    balance += sum(
                        (self._signed(l) for l in self._cash_flow_lines([member], first, last)
                         if l.source != "PREMIUM" and l.account_id == account.account_id and not one_time(l)),
                        _ZERO,
                    )
                # One-time rows: each on its own date, after the balance date.
                default_id = next((a.account_id for a in self._bank.list_accounts(member.member_id)
                                   if a.is_active and a.is_default), None)
                for item in self._cash_flow.list_items(member.member_id):
                    if (item.frequency == "ONCE" and item.spent_on and as_of < item.spent_on <= today
                            and item.payment_mode != "CASH"
                            and (item.account_id or default_id) == account.account_id):
                        balance += item.amount if item.flow_type == "INCOME" else -item.amount
                # Premiums: those ticked paid, due after the balance date.
                if account.is_default:
                    for policy in self._insurance.list_policies(member.member_id):
                        for due in premium_due_dates_between(
                            start_date=policy.start_date,
                            premium_frequency=policy.premium_frequency,
                            maturity_date=self._premiums_end(policy),
                            first=as_of + timedelta(days=1), last=today,
                        ):
                            payment = self._cash_flow.get_payment(policy.policy_id, due)
                            if payment is not None and payment.status == "PAID":
                                balance -= payment.amount
                monthly_net = sum(
                    (self._signed(l) for l in current_lines
                     if l.member_id == member.member_id and l.source != "PREMIUM"
                     and l.account_id == account.account_id and not one_time(l)),
                    _ZERO,
                )
                summaries.append(BankAccountSummary(
                    account_id=account.account_id, member_id=member.member_id,
                    owner_name=member.name, bank_name=account.bank_name,
                    account_last4=account.account_number_masked, is_default=account.is_default,
                    is_active=account.is_active, entered_balance=account.opening_balance,
                    balance_as_of=account.balance_as_of, months_elapsed=months,
                    monthly_net=monthly_net, estimated_balance=balance,
                ))
        return summaries, unassigned

    def get_monthly_cash_flow(
        self, family_id: int, year: int, month: int, member_id: Optional[int] = None,
        *, today: Optional[date] = None,
    ) -> MonthlyCashFlow:
        """One month's cash flow — see `CashFlowLine` for where each line
        comes from. FD interest counts payout FDs running that month (a
        quarterly payout / 3); SIPs count running SIPs; entered rows count
        at their monthly equivalent; premiums due that month count in full
        once marked paid (savings policies as investment, protection as
        expense). Also returns the members' bank accounts with today's
        estimated balance (see `BankAccountSummary`)."""
        if not 1 <= month <= 12:
            raise ValueError("month must be 1-12")
        members = self._portfolio_members(family_id, member_id)
        first, last = self._month_bounds(year, month)
        lines = self._cash_flow_lines(members, first, last)

        def group(flow_type: str) -> Tuple[CashFlowLine, ...]:
            return tuple(line for line in lines if line.flow_type == flow_type)

        def total(group_lines) -> Decimal:
            return sum((line.monthly_amount for line in group_lines if line.counted), _ZERO)

        income, expenses, investments = group("INCOME"), group("EXPENSE"), group("INVESTMENT")
        total_income, total_expenses, total_investments = total(income), total(expenses), total(investments)
        accounts, unassigned = self._bank_accounts_for(members, today or date.today())
        return MonthlyCashFlow(
            family_id=family_id, member_id=member_id, year=year, month=month,
            income=income, expenses=expenses, investments=investments,
            total_income=total_income, total_expenses=total_expenses,
            total_investments=total_investments,
            surplus=total_income - total_expenses - total_investments,
            accounts=tuple(accounts),
            total_cash_in_bank=sum((a.estimated_balance for a in accounts), _ZERO),
            unassigned_monthly_net=unassigned,
        )

    # ------------------------------------------------------------------
    # Portfolio aggregation
    # ------------------------------------------------------------------

    def _portfolio_members(
        self, family_id: int, member_id: Optional[int]
    ) -> List[FamilyMember]:
        """The family's active members, or just `member_id` — which must
        belong to `family_id`. Shared by every per-category portfolio view."""
        if self._family.get_family(family_id) is None:
            raise ValueError(f"family {family_id} does not exist")
        members = self._family.list_family_members(family_id, active_only=True)
        if member_id is not None:
            members = [m for m in members if m.member_id == member_id]
            if not members:
                raise ValueError(f"member {member_id} is not in family {family_id}")
        return members

    @staticmethod
    def _pct(part: Decimal, whole: Decimal) -> Decimal:
        if whole == _ZERO:
            return _ZERO
        return (part / whole * Decimal("100")).quantize(Decimal("0.01"))

    def get_asset_allocation(
        self, family_id: int, member_id: Optional[int] = None
    ) -> AssetAllocation:
        """Invested and current value by asset class, and net worth after
        loans — see `AssetAllocation`."""
        members = self._portfolio_members(family_id, member_id)
        today = date.today()
        invested: Dict[str, Decimal] = {}
        current: Dict[str, Decimal] = {}
        member_rows: List[AllocationMember] = []

        def add(category: str, inv: Decimal, cur: Decimal) -> None:
            invested[category] = invested.get(category, _ZERO) + inv
            current[category] = current.get(category, _ZERO) + cur

        for member in members:
            m_inv = m_cur = _ZERO
            for asset in self._assets.list_assets(member.member_id):
                if asset.status != "ACTIVE":
                    continue
                cur = self._current_value_for_asset(asset, today)
                add(asset.category, asset.invested_amount, cur)
                m_inv += asset.invested_amount
                m_cur += cur
            for policy in self.list_insurance_policies(member.member_id):
                if policy.status != "ACTIVE" or not policy.is_savings_policy:
                    continue
                cur = (policy.current_surrender_value or _ZERO) + (policy.accrued_bonus or _ZERO)
                add(INSURANCE_CATEGORY, policy.total_premium_due_to_date, cur)
                m_inv += policy.total_premium_due_to_date
                m_cur += cur
            member_rows.append(AllocationMember(
                member_id=member.member_id, name=member.name,
                invested=m_inv, current_value=m_cur, gain_loss=m_cur - m_inv,
            ))

        total_invested = sum(invested.values(), _ZERO)
        total_current = sum(current.values(), _ZERO)
        loans_outstanding = sum(
            (
                loan.outstanding
                for member in members
                for loan in self.list_loans(member.member_id, today=today)
                if loan.status == "ACTIVE"
            ),
            _ZERO,
        )
        slices = sorted(
            (
                AllocationSlice(
                    category=category,
                    invested=invested[category],
                    current_value=current[category],
                    invested_pct=self._pct(invested[category], total_invested),
                    current_value_pct=self._pct(current[category], total_current),
                )
                for category in invested
                if invested[category] != _ZERO or current[category] != _ZERO
            ),
            key=lambda s: (-s.current_value, -s.invested, s.category),
        )
        return AssetAllocation(
            family_id=family_id,
            member_id=member_id,
            slices=tuple(slices),
            members=tuple(member_rows),
            total_invested=total_invested,
            total_current_value=total_current,
            total_gain_loss=total_current - total_invested,
            total_loans_outstanding=loans_outstanding,
            net_worth=total_current - loans_outstanding,
        )

    def get_fd_portfolio(
        self, family_id: int, member_id: Optional[int] = None
    ) -> FdPortfolio:
        """Every FD in the family, or only `member_id`'s, with totals.
        Only ACTIVE FDs count toward the totals."""
        members = self._portfolio_members(family_id, member_id)
        today = date.today()
        rows: List[FdPortfolioRow] = []
        total_principal = _ZERO
        total_current_value = _ZERO
        total_at_maturity = _ZERO
        interest_earned = interest_total = _ZERO
        next_maturity: Optional[FdPortfolioRow] = None
        for member in members:
            for fd in self.list_fds(member.member_id):
                row = FdPortfolioRow(owner_name=member.name, fd=fd)
                rows.append(row)
                if fd.status != "ACTIVE":
                    continue
                total_principal += fd.principal
                total_current_value += fd.current_value
                interest_earned += fd.interest_earned_so_far
                interest_total += fd.total_interest_by_maturity
                total_at_maturity += compute_fd_current_value(
                    principal=fd.principal,
                    interest_rate=fd.interest_rate,
                    issue_date=fd.issue_date,
                    maturity_date=fd.maturity_date,
                    payout_type=fd.payout_type,
                    maturity_value=fd.maturity_value,
                    as_of=fd.maturity_date,
                )
                if fd.maturity_date >= today and (
                    next_maturity is None or fd.maturity_date < next_maturity.fd.maturity_date
                ):
                    next_maturity = row

        total_gain_loss = total_current_value - total_principal
        banks: Dict[str, Tuple[Decimal, Decimal]] = {}
        for row in rows:
            if row.fd.status == "ACTIVE":
                principal, at_maturity = banks.get(row.fd.bank_name, (_ZERO, _ZERO))
                banks[row.fd.bank_name] = (
                    principal + row.fd.principal, at_maturity + row.fd.amount_at_maturity
                )
        by_bank = sorted(
            (FdBankTotal(bank_name=name, principal=p, amount_at_maturity=m)
             for name, (p, m) in banks.items()),
            key=lambda b: (-b.amount_at_maturity, b.bank_name),
        )
        upcoming = sorted(
            (r for r in rows if r.fd.status == "ACTIVE" and r.fd.maturity_date >= today),
            key=lambda r: r.fd.maturity_date,
        )
        return FdPortfolio(
            family_id=family_id,
            member_id=member_id,
            rows=tuple(rows),
            total_principal=total_principal,
            total_current_value=total_current_value,
            total_gain_loss=total_gain_loss,
            total_gain_loss_pct=self._pct(total_gain_loss, total_principal),
            total_value_at_maturity=total_at_maturity,
            next_maturity=next_maturity,
            total_interest_earned_so_far=interest_earned,
            total_interest_earned_pct=self._pct(interest_earned, total_principal),
            total_interest_by_maturity=interest_total,
            total_interest_pct=self._pct(interest_total, total_principal),
            by_bank=tuple(by_bank),
            upcoming_maturities=tuple(upcoming),
        )

    def get_insurance_portfolio(
        self, family_id: int, member_id: Optional[int] = None
    ) -> InsurancePortfolio:
        """Every policy in the family, or only `member_id`'s, with cover and
        premium totals. Only ACTIVE policies count toward the totals."""
        members = self._portfolio_members(family_id, member_id)
        rows: List[InsurancePortfolioRow] = []
        total_sum_assured = _ZERO
        total_annual_premium = _ZERO
        total_due = _ZERO
        total_surrender = _ZERO
        total_bonus = _ZERO
        next_premium: Optional[InsurancePortfolioRow] = None
        for member in members:
            for policy in self.list_insurance_policies(member.member_id):
                row = InsurancePortfolioRow(owner_name=member.name, policy=policy)
                rows.append(row)
                if policy.status != "ACTIVE":
                    continue
                total_sum_assured += policy.sum_assured or _ZERO
                total_annual_premium += compute_annual_premium(
                    policy.premium_amount, policy.premium_frequency
                )
                total_due += policy.total_premium_due_to_date
                total_surrender += policy.current_surrender_value or _ZERO
                total_bonus += policy.accrued_bonus or _ZERO
                if policy.next_due_date is not None and (
                    next_premium is None
                    or policy.next_due_date < next_premium.policy.next_due_date
                ):
                    next_premium = row

        return InsurancePortfolio(
            family_id=family_id,
            member_id=member_id,
            rows=tuple(rows),
            total_sum_assured=total_sum_assured,
            total_annual_premium=total_annual_premium,
            total_premium_due_to_date=total_due,
            total_surrender_value=total_surrender,
            total_accrued_bonus=total_bonus,
            next_premium=next_premium,
        )

    def get_due_items(self, family_id: int, *, today: Optional[date] = None) -> List[DueItem]:
        """Every ACTIVE FD still to mature and every ACTIVE policy's next
        premium, from today on, for the reminder emails (see `reminders`)."""
        today = today or date.today()
        items: List[DueItem] = []
        for row in self.get_fd_portfolio(family_id).rows:
            fd = row.fd
            if fd.status != "ACTIVE" or fd.maturity_date < today:
                continue
            items.append(DueItem(
                key=f"FD:{fd.asset_id}", kind=FD_MATURITY, owner_name=row.owner_name,
                title=f"{fd.bank_name} FD", due_date=fd.maturity_date,
                amount=fd.amount_at_maturity or fd.principal,
            ))
        for row in self.get_insurance_portfolio(family_id).rows:
            policy = row.policy
            if policy.status != "ACTIVE":
                continue
            # The next premium on or after `today` — one due today still counts.
            upcoming = premium_due_dates_between(
                start_date=policy.start_date, premium_frequency=policy.premium_frequency,
                maturity_date=self._premiums_end(policy), first=today, last=today + timedelta(days=400),
            )
            if not upcoming:
                continue
            items.append(DueItem(
                key=f"POLICY:{policy.policy_id}", kind=PREMIUM, owner_name=row.owner_name,
                title=f"{policy.insurer} — {policy.plan_name}" if policy.plan_name else
                f"{policy.insurer} {policy.policy_type.title()} policy",
                due_date=upcoming[0], amount=policy.premium_amount,
            ))
        items.sort(key=lambda i: i.due_date)
        return items

    # ------------------------------------------------------------------
    # Personal planning: goals, what each holding is for, preferences
    # (see `financial_engine.planning`)
    # ------------------------------------------------------------------

    def list_goals(self, family_id: int) -> List[Goal]:
        return self._planning.list_goals(family_id)

    def save_goal(
        self, *, family_id: int, name: str, goal_type: str, target_amount: Money, target_date: date,
        goal_id: Optional[int] = None, member_id: Optional[int] = None, priority: int = 2,
        inflation_pct: Optional[Money] = None, status: str = "ACTIVE",
    ) -> Goal:
        """Add (no `goal_id`) or change a goal: an amount in today's money by
        a date, optionally for one member."""
        if self._family.get_family(family_id) is None:
            raise ValueError(f"family {family_id} does not exist")
        name = (name or "").strip()
        if not name:
            raise ValueError("give the goal a name")
        if goal_type not in GOAL_TYPES:
            raise ValueError("choose a goal type")
        amount = _decimal(target_amount)
        if amount <= _ZERO:
            raise ValueError("the amount must be positive")
        if int(priority) not in (1, 2, 3):
            raise ValueError("priority must be high, medium or low")
        if status not in ("ACTIVE", "ACHIEVED", "ABANDONED"):
            raise ValueError("status must be ACTIVE, ACHIEVED or ABANDONED")
        infl = None if inflation_pct in (None, "") else _decimal(inflation_pct)
        if infl is not None and not _ZERO <= infl <= Decimal("20"):
            raise ValueError("inflation must be between 0% and 20%")
        if member_id is not None:
            member = self._family.get_family_member(int(member_id))
            if member is None or member.family_id != family_id:
                raise ValueError("choose a member of this family")
        if goal_id is not None:
            current = self._planning.get_goal(goal_id)
            if current is None or current.family_id != family_id:
                raise ValueError(f"goal {goal_id} does not exist")
        elif target_date <= date.today():
            raise ValueError("the target date must be in the future")
        return self._planning.save_goal(
            goal_id, family_id=family_id, member_id=None if member_id is None else int(member_id), name=name,
            goal_type=goal_type, target_amount=amount, target_date=target_date, priority=int(priority),
            status=status, inflation_pct=infl,
        )

    def delete_goal(self, goal_id: int) -> None:
        """Delete a goal; the holdings put towards it become free again."""
        if not self._planning.delete_goal(goal_id):
            raise ValueError(f"goal {goal_id} does not exist")

    def set_asset_purpose(
        self, *, asset_id: int, purpose: Optional[str] = None, goal_id: Optional[int] = None,
        pct: Money = Decimal("100"),
    ) -> None:
        """Say what a holding is for: towards a goal (`goal_id`, `pct` of it),
        EMERGENCY / INCOME / KEEP (`purpose`), or nothing (neither)."""
        asset = self._assets.get_asset(asset_id)
        if asset is None:
            raise ValueError(f"holding {asset_id} does not exist")
        if goal_id is not None and purpose is not None:
            raise ValueError("a holding is either for a goal or earmarked, not both")
        if purpose is not None and purpose not in PURPOSES:
            raise ValueError("purpose must be EMERGENCY, INCOME or KEEP")
        share = _decimal(pct)
        if goal_id is not None:
            goal = self._planning.get_goal(int(goal_id))
            owner = self._family.get_family_member(asset.member_id)
            if goal is None or owner is None or goal.family_id != owner.family_id:
                raise ValueError("choose one of this family's goals")
            if not _ZERO < share <= Decimal("100"):
                raise ValueError("the share must be between 1% and 100%")
        self._planning.set_asset_purpose(
            asset_id, purpose=purpose, goal_id=None if goal_id is None else int(goal_id), pct=share,
        )

    def get_planning_preferences(self, family_id: int) -> PlanningPreference:
        return self._planning.get_preference(family_id)

    def save_planning_preferences(
        self, *, family_id: int, tax_slab_pct: Money, emergency_months: int, min_fd_amount: Money = 0,
        avoid_direct_stocks: bool = False,
    ) -> PlanningPreference:
        if self._family.get_family(family_id) is None:
            raise ValueError(f"family {family_id} does not exist")
        slab = _decimal(tax_slab_pct)
        if slab not in (Decimal(x) for x in (0, 5, 10, 15, 20, 25, 30)):
            raise ValueError("tax slab must be 0, 5, 10, 15, 20, 25 or 30%")
        months = int(emergency_months)
        if not 1 <= months <= 36:
            raise ValueError("emergency fund must be 1 to 36 months")
        minimum = _decimal(min_fd_amount or 0)
        if minimum < _ZERO:
            raise ValueError("minimum FD amount cannot be negative")
        pref = PlanningPreference(family_id, slab, months, minimum, bool(avoid_direct_stocks))
        self._planning.save_preference(pref)
        return pref

    GOAL_MONEY = ("YES", "IF_NEEDED", "NO")

    def _member_policies(self, family_id: int) -> List["MemberMoneyPolicy"]:
        """Whose money may fund goals that are not their own: the saved
        choice, else the default for the person's stage of life."""
        saved = self._planning.list_member_policies()
        out = []
        for m in self._family.list_family_members(family_id, active_only=True):
            default = "YES" if m.life_stage in (None, "EARNING") else "IF_NEEDED"
            out.append(MemberMoneyPolicy(m.member_id, m.name, saved.get(m.member_id, default),
                                         m.member_id not in saved))
        return out

    def set_member_goal_money(self, *, member_id: int, goal_money: str) -> None:
        """YES / IF_NEEDED / NO — may this person's money fund others' goals."""
        if goal_money not in self.GOAL_MONEY:
            raise ValueError("choose Yes, Only if needed, or No")
        if self._family.get_family_member(member_id) is None:
            raise ValueError(f"family member {member_id} does not exist")
        self._planning.set_member_policy(member_id, goal_money)

    def apply_funding_plan(self, family_id: int) -> int:
        """Assign the holdings the funding plan uses ("from your money") to
        their goals. Backup money is never assigned by this. Returns how many
        holdings were assigned."""
        page = self.get_plan(family_id)
        funding = page.plan.funding
        if funding is None:
            return 0
        count = 0
        with self._db.unit_of_work() as uow:
            for g in funding.goals:
                for use in g.from_holdings:
                    pct = min(max(use.pct, Decimal("1")), Decimal("100"))
                    self._planning.set_asset_purpose(use.asset_id, goal_id=g.goal_id, pct=pct, uow=uow)
                    count += 1
        return count

    def set_move_dismissed(self, *, family_id: int, key: str, dismissed: bool = True) -> None:
        """Hide a suggestion for good ("not for me"), or bring it back."""
        key = (key or "").strip()
        if not key or len(key) > 120:
            raise ValueError("unknown suggestion")
        if self._family.get_family(family_id) is None:
            raise ValueError(f"family {family_id} does not exist")
        self._planning.set_dismissed(family_id, key, dismissed, date.today())

    def _plan_context(self, family_id: int, member_id: Optional[int], today: date):
        """(preferences, active goals, holdings with what each is for, FD
        value that must not be suggested for moving)."""
        pref = self._planning.get_preference(family_id)
        goals = [g for g in self._planning.list_goals(family_id)
                 if g.status == "ACTIVE" and (member_id is None or g.member_id == member_id)]
        holdings = self._plan_holdings(family_id, member_id, today)
        soon = {g.goal_id for g in goals if (g.target_date - today).days < 5 * 365}
        protected = sum((h.value for h in holdings if h.kind == "FD" and (
            h.earmark is not None or (h.goal_id in soon))), _ZERO)
        return pref, goals, holdings, protected

    def _plan_holdings(self, family_id: int, member_id: Optional[int], today: date) -> List[HoldingInput]:
        members = {m.name: m for m in self._portfolio_members(family_id, member_id)}
        allocations = {a.asset_id: a for a in self._planning.list_allocations(family_id)}
        earmarks = self._planning.list_earmarks()

        def who(owner: str):
            m = members.get(owner)
            if m is None:
                return None, None
            age = None
            if m.date_of_birth:
                age = today.year - m.date_of_birth.year - (
                    (today.month, today.day) < (m.date_of_birth.month, m.date_of_birth.day))
            return m.life_stage, age

        def holding(asset_id: int, owner: str, kind: str, label: str, value: Decimal, **extra) -> HoldingInput:
            stage, age = who(owner)
            alloc = allocations.get(asset_id)
            return HoldingInput(
                asset_id=asset_id, owner_name=owner, owner_stage=stage, owner_age=age, kind=kind, label=label,
                value=value, goal_id=alloc.goal_id if alloc else None,
                goal_pct=alloc.allocated_pct if alloc else _ZERO, earmark=earmarks.get(asset_id), **extra,
            )

        out: List[HoldingInput] = []
        for r in self.get_fd_portfolio(family_id, member_id).rows:
            fd = r.fd
            if fd.status != "ACTIVE":
                continue
            out.append(holding(
                fd.asset_id, r.owner_name, "FD", f"{fd.bank_name} FD", fd.current_value,
                fd_rate=fd.interest_rate, fd_payout=fd.payout_type if fd.payout_interest else None,
                fd_payout_interest=fd.payout_interest or _ZERO, maturity_date=fd.maturity_date,
                amount_at_maturity=fd.amount_at_maturity,
            ))
        kinds = {"EQUITY": "EQUITY_FUND", "DEBT": "DEBT_FUND", "HYBRID": "HYBRID_FUND", "GOLD": "GOLD"}
        for r in self.get_mf_portfolio(family_id, member_id).rows:
            mf = r.mf
            if mf.status != "ACTIVE":
                continue
            out.append(holding(
                mf.asset_id, r.owner_name, kinds[fund_kind(mf.scheme_name)], mf.scheme_name, mf.current_value,
                monthly_sip=mf.sip_amount if mf.sip_enabled and mf.sip_amount else _ZERO,
            ))
        for r in self.get_stock_portfolio(family_id, member_id).rows:
            st = r.stock
            if st.status == "ACTIVE" and st.current_value > _ZERO:
                out.append(holding(st.asset_id, r.owner_name, "STOCK", f"{st.symbol} shares", st.current_value))
        for r in self.get_gold_portfolio(family_id, member_id).rows:
            g = r.gold
            if g.status == "ACTIVE":
                out.append(holding(g.asset_id, r.owner_name, "GOLD", g.name, g.current_value))
        for r in self.get_pf_portfolio(family_id, member_id).rows:
            a = r.account
            if a.status == "ACTIVE":
                out.append(holding(a.asset_id, r.owner_name, a.account_type, a.name, a.balance))
        for r in self.get_nps_portfolio(family_id, member_id).rows:
            a = r.account
            if a.status == "ACTIVE":
                out.append(holding(a.asset_id, r.owner_name, "NPS", a.name, a.balance))
        return out

    def get_plan(self, family_id: int, member_id: Optional[int] = None, *, today: Optional[date] = None) -> "PlanPage":
        """The personal Financial Health page: the health check-up (with the
        family's preferences and earmarks respected), area scores, goals and
        smart moves, plus what the plan-settings editor needs."""
        today = today or date.today()
        ctx = self._plan_context(family_id, member_id, today)
        pref, goals, holdings, _ = ctx
        health = self._assess_health(family_id, member_id, today, ctx)
        names = {m.member_id: m.name for m in self._family.list_family_members(family_id)}
        policies = self._member_policies(family_id)
        plan = make_plan(PlanInputs(
            money_policy=tuple((p.name, p.goal_money) for p in policies),
            as_of=today,
            goals=tuple(GoalInput(
                goal_id=g.goal_id, name=g.name, goal_type=g.goal_type, owner_name=names.get(g.member_id),
                target_today=g.target_amount, target_date=g.target_date, priority=g.priority or 2,
                inflation_pct=g.inflation_pct,
            ) for g in goals),
            holdings=tuple(holdings),
            monthly_outgo=health.monthly_outgo,
            monthly_free=health.monthly_saving - health.monthly_investing,
            cash_in_bank=health.cash_in_bank,
            tax_slab_pct=pref.tax_slab_pct, emergency_months=pref.emergency_months,
            min_fd_amount=pref.min_fd_amount, avoid_direct_stocks=pref.avoid_direct_stocks,
            dismissed=frozenset(self._planning.list_dismissed(family_id)),
            checks=health.checks, actions=health.actions, has_loans=health.total_loans > _ZERO,
        ))
        return PlanPage(
            plan=plan, health=health,
            goal_records=tuple(self._planning.list_goals(family_id)),
            holdings=tuple(PlanHolding(
                asset_id=h.asset_id, owner_name=h.owner_name, kind=h.kind, label=h.label, value=h.value,
                goal_id=h.goal_id, goal_pct=h.goal_pct, purpose=h.earmark,
            ) for h in sorted(holdings, key=lambda h: (h.kind != "FD", -h.value))),
            preferences=pref,
            member_policies=tuple(policies),
        )

    # ------------------------------------------------------------------
    # Financial health: the whole picture, judged (see `health`)
    # ------------------------------------------------------------------

    def get_financial_health(
        self, family_id: int, member_id: Optional[int] = None, *, today: Optional[date] = None
    ) -> FinancialHealth:
        """A check-up of the family's (or one member's) finances from what is
        recorded: net worth, asset mix, health checks, a ranked action plan
        and a 20-year projection. See `financial_engine.health`. The family's
        planning preferences and earmarks are respected (see `get_plan`)."""
        today = today or date.today()
        ctx = self._plan_context(family_id, member_id, today)
        return self._assess_health(family_id, member_id, today, ctx)

    def _assess_health(self, family_id: int, member_id: Optional[int], today: date, ctx) -> FinancialHealth:
        pref, _, _, protected_fd = ctx
        members = self._portfolio_members(family_id, member_id)
        ages = tuple(
            today.year - m.date_of_birth.year
            - ((today.month, today.day) < (m.date_of_birth.month, m.date_of_birth.day))
            for m in members if m.date_of_birth
        )
        risk = next((m.risk_tolerance for m in members if m.risk_tolerance), None)

        cf = self.get_monthly_cash_flow(family_id, today.year, today.month, member_id, today=today)
        # The health check judges the regular month: one-time (ad hoc) rows are left out.
        cf = dataclasses.replace(cf, **{
            group: tuple(l for l in getattr(cf, group) if not (l.source == "ITEM" and l.frequency == "ONCE"))
            for group in ("income", "expenses", "investments")
        })
        insurance = self.get_insurance_portfolio(family_id, member_id)
        expenses = sum(
            (l.monthly_amount for l in cf.expenses if l.source not in ("LOAN_EMI", "LOAN_PREPAYMENT", "PREMIUM")),
            _ZERO,
        ) + (insurance.total_annual_premium / Decimal("12")).quantize(_CENT)
        emi = sum((l.monthly_amount for l in cf.expenses if l.source == "LOAN_EMI"), _ZERO)
        investing = sum((l.monthly_amount for l in cf.investments if l.source != "PREMIUM"), _ZERO)
        expense_rows = sum(1 for l in cf.expenses if l.source == "ITEM")

        mf = self.get_mf_portfolio(family_id, member_id)
        funds = tuple(
            FundInput(name=r.mf.scheme_name, value=r.mf.current_value, kind=fund_kind(r.mf.scheme_name),
                      regular_plan=is_regular_plan(r.mf.scheme_name))
            for r in mf.rows if r.mf.status == "ACTIVE"
        )
        stocks = self.get_stock_portfolio(family_id, member_id)
        stocks_cas = sum(
            1 for r in stocks.rows
            if r.stock.status == "ACTIVE" and any(t.is_opening_balance and t.status == "ACTIVE" for t in r.stock.transactions)
        )
        fds = self.get_fd_portfolio(family_id, member_id)
        active_fds = [r.fd for r in fds.rows if r.fd.status == "ACTIVE"]
        principal = sum((f.principal for f in active_fds), _ZERO)
        fd_rate = (
            sum((f.principal * f.interest_rate for f in active_fds), _ZERO) / principal
            if principal > _ZERO else _ZERO
        )
        pf = self.get_pf_portfolio(family_id, member_id)
        nps = self.get_nps_portfolio(family_id, member_id)
        nps_rows = [r.account for r in nps.rows if r.account.status == "ACTIVE"]
        nps_equity = sum((a.balance * a.equity_pct / Decimal("100") for a in nps_rows), _ZERO)
        nps_total = sum((a.balance for a in nps_rows), _ZERO)
        gold = self.get_gold_portfolio(family_id, member_id)
        policies = [r.policy for r in insurance.rows if r.policy.status == "ACTIVE"]

        # The suggested home-loan prepayment: FDs beyond a year of outgoings,
        # up to a quarter of the loan, in whole lakhs — only when the loan
        # costs more than FDs earn after tax.
        outgo = expenses + emi
        spare_fd = fds.total_current_value - outgo * 12
        loans = []
        for row in self.get_loan_portfolio(family_id, member_id, today=today).rows:
            loan = row.loan
            if loan.status != "ACTIVE" or loan.outstanding <= _ZERO:
                continue
            series = tuple(
                next((r.closing for r in loan.schedule if r.month_no == loan.current_month_no + 12 * y),
                     loan.schedule[-1].closing if loan.schedule and loan.current_month_no + 12 * y > len(loan.schedule) else loan.outstanding)
                for y in range(21)
            )
            amount = saved = _ZERO
            months = 0
            if fd_rate * (Decimal("1") - Decimal("0.3")) < loan.interest_rate and spare_fd > _ZERO:
                amount = (min(spare_fd, loan.outstanding / 4) / Decimal("100000")).to_integral_value() * Decimal("100000")
                if amount > _ZERO:
                    what_if = self.loan_what_if(liability_id=loan.liability_id, amount=amount, on_date=today)
                    saved, months = what_if.interest_saved, what_if.months_saved
                    spare_fd -= amount
            loans.append(LoanInput(
                label=self._LOAN_TYPE_LABEL[loan.loan_type].lower(), outstanding=loan.outstanding,
                rate=loan.interest_rate, monthly_payment=loan.this_month_payment,
                is_home_loan=loan.loan_type == "HOME_LOAN", prepay_amount=amount,
                prepay_interest_saved=saved, prepay_months_saved=months, balance_by_year=series,
            ))

        return assess(HealthInputs(
            as_of=today, member_count=len(members), ages=ages, risk_tolerance=risk,
            monthly_income=sum((l.monthly_amount for l in cf.income if l.counted), _ZERO),
            monthly_expenses=expenses, monthly_emi=emi,
            monthly_investing=investing, expense_rows=expense_rows,
            annual_premiums=insurance.total_annual_premium, cash=cf.total_cash_in_bank,
            funds=funds, stocks_value=stocks.total_current_value,
            largest_stock=stocks.analysis.largest_symbol if stocks.analysis else None,
            largest_stock_pct=stocks.analysis.largest_pct if stocks.analysis else _ZERO,
            fd_value=fds.total_current_value, fd_avg_rate=fd_rate.quantize(Decimal("0.01")),
            epf_ppf_value=pf.total_balance, nps_equity=nps_equity, nps_debt=nps_total - nps_equity,
            gold_value=gold.total_current_value,
            policy_savings_value=sum(
                ((p.current_surrender_value or _ZERO) + (p.accrued_bonus or _ZERO)
                 for p in policies if p.is_savings_policy), _ZERO),
            savings_policy_count=sum(1 for p in policies if p.is_savings_policy),
            term_cover=sum((p.sum_assured or _ZERO for p in policies if p.policy_type == "TERM"), _ZERO),
            health_policies=sum(1 for p in policies if p.policy_type == "HEALTH"),
            loans=tuple(loans), funds_needing_details=mf.xirr_excluded_count,
            stocks_at_statement_price=stocks_cas,
            people=tuple(self._person_inputs(family_id, members, cf, today)),
            tax_slab=pref.tax_slab_pct / Decimal("100"), emergency_months=pref.emergency_months,
            protected_fd=protected_fd,
        ))

    def _person_inputs(self, family_id: int, members, cf, today: date) -> List[PersonInput]:
        names = {m.member_id: m.name for m in self._family.list_family_members(family_id)}
        people = []
        for m in members:
            age = None
            if m.date_of_birth:
                age = today.year - m.date_of_birth.year - (
                    (today.month, today.day) < (m.date_of_birth.month, m.date_of_birth.day))
            policies = [p for p in self.list_insurance_policies(m.member_id) if p.status == "ACTIVE"]
            people.append(PersonInput(
                member_id=m.member_id, name=m.name, relationship=m.relationship, age=age,
                life_stage=m.life_stage, heir_name=names.get(m.heir_member_id),
                retirement_age=m.retirement_age,
                assets=self.get_asset_allocation(family_id, m.member_id).total_current_value,
                fd_value=self.get_fd_portfolio(family_id, m.member_id).total_current_value,
                loans=self.get_loan_portfolio(family_id, m.member_id, today=today).total_outstanding,
                monthly_income=sum((l.monthly_amount for l in cf.income if l.member_id == m.member_id), _ZERO),
                policies=len(policies),
                policies_without_nominee=sum(1 for p in policies if not p.nominee),
                has_health_cover=any(p.policy_type == "HEALTH" for p in policies),
                checks_done=tuple(k for k, v in self._succession.list_checks(m.member_id).items() if v),
            ))
        return people

    def get_member_investment_summary(self, member_id: int) -> MemberInvestmentSummary:
        """Sum current value across `member_id`'s holdings, by category.

        Sums the primary holder's assets (`asset.member_id`); a joint asset's
        value is not yet split by `asset_ownership` share here — see the
        assessment.
        """
        member = self._family.get_family_member(member_id)
        if member is None:
            raise ValueError(f"family member {member_id} does not exist")

        today = date.today()
        fds: List[FdSummary] = []
        mfs: List[MfSummary] = []
        total_invested = _ZERO
        total_current_value = _ZERO
        for asset in self._assets.list_assets(member_id):
            if asset.status != "ACTIVE":
                continue
            current_value = self._current_value_for_asset(asset, today)
            total_invested += asset.invested_amount
            total_current_value += current_value
            if asset.category == "FD":
                holding = self._assets.get_fd(asset.asset_id)
                fds.append(self._fd_summary(asset, holding, today))
            elif asset.category == "MF":
                holding = self._assets.get_mutual_fund(asset.asset_id)
                transactions = self._mf_txns.list_transactions(asset.asset_id)
                mfs.append(self._mf_summary(asset, holding, transactions, today))

        return MemberInvestmentSummary(
            member_id=member.member_id,
            name=member.name,
            fds=tuple(fds),
            mfs=tuple(mfs),
            insurance_policies=tuple(self.list_insurance_policies(member_id)),
            total_invested=total_invested,
            total_current_value=total_current_value,
            total_gain_loss=total_current_value - total_invested,
        )

    def get_family_dashboard(self, family_id: int) -> FamilyDashboard:
        """Family-wide totals and a per-member breakdown.

        "Net worth" is not a separate figure yet: with no liabilities tracked
        (Step 7 onward left that subsystem parked — see the assessment),
        total investments and net worth are the same number today. This
        method reports `total_current_value`/`total_invested` honestly rather
        than naming a "net worth" this block cannot yet distinguish.
        """
        family = self._family.get_family(family_id)
        if family is None:
            raise ValueError(f"family {family_id} does not exist")

        members = [
            self.get_member_investment_summary(member.member_id)
            for member in self._family.list_family_members(family_id, active_only=True)
        ]

        by_category: Dict[str, Decimal] = {}
        for member in self._family.list_family_members(family_id, active_only=True):
            for asset in self._assets.list_assets(member.member_id):
                if asset.status != "ACTIVE":
                    continue
                current_value = self._current_value_for_asset(asset, date.today())
                by_category[asset.category] = by_category.get(asset.category, _ZERO) + current_value

        total_invested = sum((m.total_invested for m in members), _ZERO)
        total_current_value = sum((m.total_current_value for m in members), _ZERO)

        return FamilyDashboard(
            family_id=family.family_id,
            family_name=family.family_name,
            base_currency=family.base_currency,
            total_invested=total_invested,
            total_current_value=total_current_value,
            total_gain_loss=total_current_value - total_invested,
            by_category=by_category,
            members=tuple(members),
        )
