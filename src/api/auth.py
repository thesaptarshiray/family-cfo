"""
A single shared password gating access to the dashboard, plus in-memory
sessions.

This is your family's app, not a multi-tenant service — one password, not
per-member accounts, matching the single-family simplification the rest of
this block already makes.

Deliberately kept out of the financial SQLite database and out of the
`repository` package: authentication is not a financial fact, and the V6
schema (and the architecture-boundary rule that only `repository` may touch
`sqlite3`) is about the family-finance domain, not this crosscutting
concern. The credential is instead one small JSON file next to the database
(`family_finance_auth.json` by default) holding a salted password hash —
never the password itself. Sessions are not persisted at all: they live only
in this process's memory, so restarting the server requires logging in
again. That is a deliberate simplification, not an oversight — it avoids
needing session expiry/rotation logic for a first pass.

Hashing uses `hashlib.pbkdf2_hmac` (stdlib, no new dependency) with a random
salt and a high iteration count. Session tokens use `secrets.token_urlsafe`.
Neither is bcrypt/argon2-grade, but this runs on your own machine, for your
own data, not as an internet-facing service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Optional

_ITERATIONS = 200_000
_SALT_BYTES = 16
_SESSION_COOKIE = "session"


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)


class AuthStore:
    """The password credential, persisted as one small JSON file.

    Bootstrap-only: once a password is set, this store has no method to
    change or reset it (a "change password" flow is a natural fast-follow,
    left out to keep this first pass small). If the file is lost, editing it
    is the escape hatch for now.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def is_password_set(self) -> bool:
        return self._path.exists()

    def set_password(self, password: str) -> None:
        if self.is_password_set():
            raise ValueError("a password is already set")
        if not password or len(password) < 4:
            raise ValueError("password must be at least 4 characters")
        salt = secrets.token_bytes(_SALT_BYTES)
        digest = _hash(password, salt)
        self._path.write_text(
            json.dumps({"salt": salt.hex(), "hash": digest.hex()}), encoding="utf-8"
        )

    def verify_password(self, password: str) -> bool:
        if not self.is_password_set():
            return False
        data = json.loads(self._path.read_text(encoding="utf-8"))
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["hash"])
        return hmac.compare_digest(_hash(password, salt), expected)


class SessionManager:
    """In-memory set of valid session tokens (see the module docstring for
    why these are not persisted)."""

    def __init__(self) -> None:
        self._tokens: set[str] = set()

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def is_valid(self, token: Optional[str]) -> bool:
        return token is not None and token in self._tokens

    def invalidate(self, token: Optional[str]) -> None:
        self._tokens.discard(token)


def session_cookie_header(token: str) -> str:
    """The `Set-Cookie` header value for a newly created session.

    No `Secure` flag: this serves plain HTTP on localhost, and `Secure`
    would prevent the browser from ever sending the cookie back there.
    """
    return f"{_SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax"


def clear_session_cookie_header() -> str:
    return f"{_SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"


def read_session_token(cookie_header: Optional[str]) -> Optional[str]:
    """Pull the session token out of a raw `Cookie` request header."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == _SESSION_COOKIE:
            return value
    return None
