"""
Repository layer — Block 1: foundation.

This package is the ONLY part of the application permitted to hold a SQLite
connection or execute SQL. Every other layer (Financial Engine, and later
the UI/AI layer) talks to the database exclusively through this package.

Block 1: connection/transaction management and schema initialization.
Block 2A: family and family-member CRUD (`FamilyRepository`).

It intentionally does NOT yet contain:

  - business operations (create_asset, create_transaction, etc.)
  - the Financial Engine's atomic operations (create_transfer,
    create_redemption, execute_reversal, ...)
  - any financial calculation or validation rule

Those arrive in later blocks, built on top of what's exposed here.

Public surface:
"""

from .database import Database
from .bank_repository import BankAccount, BankRepository
from .asset_repository import (
  Asset,
  AssetRepository,
  FdHolding,
  MutualFundHolding,
  StockHolding,
)
from .asset_valuation_repository import AssetValuation, AssetValuationRepository
from .derived_cache import DerivedCacheRepository
from .errors import RepositoryError, RepositoryIntegrityError
from .family_repository import Family, FamilyMember, FamilyRepository
from .insurance_repository import InsurancePolicy, InsurancePolicyRepository
from .mf_transaction_repository import MfTransaction, MfTransactionRepository
from .schema import initialize_schema, get_table_names
from .transaction_repository import Transaction, TransactionRepository
from .unit_of_work import UnitOfWork

__all__ = [
    "Database",
    "UnitOfWork",
    "TransactionRepository",
    "Transaction",
    "DerivedCacheRepository",
    "BankRepository",
    "BankAccount",
    "AssetRepository",
    "Asset",
    "MutualFundHolding",
    "StockHolding",
    "FdHolding",
    "initialize_schema",
    "get_table_names",
    "FamilyRepository",
    "Family",
    "FamilyMember",
    "InsurancePolicyRepository",
    "InsurancePolicy",
    "MfTransactionRepository",
    "MfTransaction",
    "AssetValuationRepository",
    "AssetValuation",
    "RepositoryError",
    "RepositoryIntegrityError",
]
