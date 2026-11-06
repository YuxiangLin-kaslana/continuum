"""Accounts: who may use the API, by token, and how a person gets one with a password.

A token is shown once when it is made and stored only as its SHA-256 hash. A token names a user; what that user may
ask for is still the Policy's to decide, through the `app` Source's allowed list.

A password is stored only as a salted scrypt hash. Logging in with one gives the device its own token, so a lost
device is revoked alone and the password never needs to be kept on it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from .store import SQLiteStore

USER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# scrypt with OWASP's recommended minimum cost; the parameters are stored with each hash so they can be raised later.
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSWORD = 8


def hash_password(password: str, salt: bytes | None = None, n: int = SCRYPT["n"], r: int = SCRYPT["r"],
                  p: int = SCRYPT["p"]) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = hash_password(password, base64.b64decode(salt), int(n), int(r), int(p)).split("$")[-1]
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, digest)


class Accounts:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def add(self, user: str, device: str = "") -> str:
        if not USER_NAME.match(user):
            raise ValueError("a user name is 1-64 letters, digits, '.', '_' or '-'")
        token = "ctm_" + secrets.token_urlsafe(32)
        self.store.db.execute("INSERT INTO users (token_hash, user, device, created_at) VALUES (?, ?, ?, ?)",
                              (_hash(token), user, device, datetime.now(UTC).isoformat()))
        return token

    def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        row = self.store.db.execute("SELECT user FROM users WHERE token_hash = ? AND revoked = 0",
                                    (_hash(token),)).fetchone()
        return row[0] if row else None

    def list(self) -> list[dict[str, Any]]:
        rows = self.store.db.execute("SELECT user, device, created_at, revoked FROM users ORDER BY rowid").fetchall()
        return [dict(user=r[0], device=r[1], created_at=r[2], revoked=bool(r[3])) for r in rows]

    # --- passwords ---

    def set_password(self, user: str, password: str, min_length: int = MIN_PASSWORD) -> None:
        """Set a user's password; `min_length` may be lowered only by the person running the server, on purpose."""
        if not USER_NAME.match(user):
            raise ValueError("a user name is 1-64 letters, digits, '.', '_' or '-'")
        if len(password) < max(1, min_length):
            raise ValueError(f"a password has at least {max(1, min_length)} characters")
        self.store.db.execute(
            "INSERT INTO passwords (user, hash, updated_at) VALUES (?, ?, ?) ON CONFLICT (user) DO UPDATE SET "
            "hash = excluded.hash, updated_at = excluded.updated_at",
            (user, hash_password(password), datetime.now(UTC).isoformat()))

    def has_password(self, user: str) -> bool:
        return self.store.db.execute("SELECT 1 FROM passwords WHERE user = ?", (user,)).fetchone() is not None

    def check_password(self, user: str, password: str) -> bool:
        row = self.store.db.execute("SELECT hash FROM passwords WHERE user = ?", (user,)).fetchone()
        if row is None:
            # Spend the same time as a real check, so a missing user cannot be told from a wrong password.
            password_matches(password, hash_password("not a password", b"0" * 16))
            return False
        return password_matches(password, row[0])

    def login(self, user: str, password: str, device: str = "") -> str | None:
        """A new token for this device when the password is right; None otherwise."""
        if not USER_NAME.match(user or "") or not self.check_password(user, password):
            return None
        return self.add(user, device[:80])

    def revoke_token(self, token: str) -> bool:
        return self.store.db.execute("UPDATE users SET revoked = 1 WHERE token_hash = ? AND revoked = 0",
                                     (_hash(token),)).rowcount > 0

    def revoke(self, user: str, device: str | None = None) -> int:
        sql = "UPDATE users SET revoked = 1 WHERE user = ? AND revoked = 0"
        args: tuple[str, ...] = (user,)
        if device is not None:
            sql += " AND device = ?"
            args += (device,)
        return self.store.db.execute(sql, args).rowcount
