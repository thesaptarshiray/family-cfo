"""
API / UI layer — the first package above the Financial Engine.

This package may import only `financial_engine` (and the standard library):
it never imports `repository` or `sqlite3`. That is not just a convention
here — the architecture-boundary tests police it automatically for any
package other than `repository`/`financial_engine`, the moment one exists.
This is that package, and it is expected to stay the only door into the
application from outside: everything it does goes through one
`FinancialEngine` instance.

`server.run_server` starts a small local HTTP server (stdlib only, no new
dependency) serving a JSON API and the static dashboard page. It is meant to
be run on your own machine, over `localhost`, for your own family's data —
see `run_dashboard.py` at the project root.
"""

from .auth import AuthStore
from .server import create_server, run_server

__all__ = ["create_server", "run_server", "AuthStore"]
