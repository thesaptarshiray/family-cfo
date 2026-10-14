"""Financial Engine layer.

This package is intentionally separate from the Repository layer. The
Repository owns SQLite access and row-level persistence; the Financial Engine
coordinates higher-level financial operations such as transfers, reversals,
and derived cache recomputation.

The current implementation is the scaffold for the next layer: the engine
exposes the transaction-oriented entry points implied by the V6 DDL and the
companion design audit, while keeping repository access behind the database
boundary.
"""

from .engine import FinancialEngine

__all__ = ["FinancialEngine"]
