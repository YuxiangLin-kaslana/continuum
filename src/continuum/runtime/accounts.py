"""Accounts: who may use the API, by token.

A token is shown once when it is made and stored only as its SHA-256 hash. A token names a user; what that user may
ask for is still the Policy's to decide, through the `app` Source's allowed list.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from .store import SQLiteStore

USER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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

    def revoke(self, user: str, device: str | None = None) -> int:
        sql = "UPDATE users SET revoked = 1 WHERE user = ? AND revoked = 0"
        args: tuple[str, ...] = (user,)
        if device is not None:
            sql += " AND device = ?"
            args += (device,)
        return self.store.db.execute(sql, args).rowcount
