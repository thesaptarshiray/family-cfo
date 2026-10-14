"""
Run the Family CFO dashboard locally.

    python run_dashboard.py            (opens the browser by itself)
    python run_dashboard.py --no-browser

It opens http://127.0.0.1:8000/ in the browser; if it is already running,
it just opens the browser. Ctrl+C (or closing its window) stops it. The
daily price sync runs in the background, so pages never wait for it.

Your data lives in `family_finance.db` next to this file (created on first
run); your login password's hash lives in `family_finance_auth.json` next to
it (created the first time you set a password, in the browser); and when MF
NAVs were last synced from AMFI lives in `family_finance_sync_state.json`.
None of these is committed to git (see `.gitignore`), and none leaves your
machine — the server only listens on localhost. The outbound requests the
app makes are the daily downloads of AMFI's public NAV file and the
NSE / BSE end-of-day price files (for stock holdings); when stock prices
were last synced lives in `family_finance_stock_sync_state.json`. Two more,
only once set up in Settings: a question asked in "Ask Family CFO" goes to
Anthropic's API with the figures it needs (no PAN or account numbers), and
due-date reminder emails go out through Gmail. Their settings (API key,
Gmail app password) live in `family_finance_settings.json`, and what was
emailed in `family_finance_reminders.json` — also never committed.

Mirrors `conftest.py`'s `sys.path` setup so this can run directly from the
source tree without an install step.
"""

import shutil
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from repository.database import Database  # noqa: E402
from repository.schema import (  # noqa: E402
    get_schema_version, initialize_schema, migrate_schema, pending_migrations,
)
from financial_engine import FinancialEngine  # noqa: E402
from api import AuthStore, run_server  # noqa: E402
from api.amfi_sync import SyncStateStore  # noqa: E402
from api.notifications import ReminderLog, SettingsStore  # noqa: E402

DB_PATH = ROOT / "family_finance.db"
AUTH_PATH = ROOT / "family_finance_auth.json"
SYNC_STATE_PATH = ROOT / "family_finance_sync_state.json"
STOCK_SYNC_STATE_PATH = ROOT / "family_finance_stock_sync_state.json"
SETTINGS_PATH = ROOT / "family_finance_settings.json"
REMINDER_LOG_PATH = ROOT / "family_finance_reminders.json"
HOST = "127.0.0.1"
PORT = 8000


URL = f"http://{HOST}:{PORT}/"


def _already_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((HOST, PORT)) == 0


def main() -> None:
    open_browser = "--no-browser" not in sys.argv
    if _already_running():
        # Started twice (e.g. the desktop shortcut clicked again): just show it.
        print(f"Family CFO is already running at {URL}")
        if open_browser:
            webbrowser.open(URL)
        return
    is_new = not DB_PATH.exists()
    database = Database(DB_PATH)
    if is_new:
        initialize_schema(database)
        print(f"Created a new database at {DB_PATH}")
    elif pending_migrations(database):
        # Keep a copy of the database as it was before any schema change.
        backup = DB_PATH.with_name(
            f"{DB_PATH.name}.backup-v{get_schema_version(database)}"
        )
        shutil.copy2(DB_PATH, backup)
        applied = migrate_schema(database)
        print(f"Upgraded the database ({', '.join(applied)}); backup at {backup}")

    engine = FinancialEngine(database)
    # One holding per person, symbol and broker: fold any duplicates entered
    # before that rule existed (a no-op once there are none).
    for line in engine.merge_duplicate_stock_holdings():
        print(f"Stocks: {line}")
    auth_store = AuthStore(AUTH_PATH)
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=(URL,)).start()
    run_server(
        engine, auth_store, host=HOST, port=PORT,
        sync_state=SyncStateStore(SYNC_STATE_PATH),
        stock_sync_state=SyncStateStore(STOCK_SYNC_STATE_PATH),
        settings=SettingsStore(SETTINGS_PATH),
        reminder_log=ReminderLog(REMINDER_LOG_PATH),
    )


if __name__ == "__main__":
    main()
