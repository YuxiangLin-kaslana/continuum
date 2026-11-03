"""The Store: one SQLite database holding the domain, the effect queue, the outbox receipts and a small KV.

It implements the core's `Repositories` port. Rows hold each aggregate as JSON, with the few columns its queries need.
`atomic` opens a transaction, or a savepoint inside one, and `after_commit` runs an action only once the outermost
transaction commits.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from continuum.core.model import Decision, Thread, Ticket, Trigger, TriggerDisposition, latest_assignment

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispositions (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS threads (key TEXT PRIMARY KEY, active INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, open INTEGER NOT NULL, active INTEGER NOT NULL,
                                    body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, thread_key TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS decisions_thread ON decisions (thread_key);
CREATE TABLE IF NOT EXISTS effects (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
  due REAL NOT NULL, version INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, error TEXT,
  UNIQUE (kind, key));
CREATE TABLE IF NOT EXISTS receipts (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, ticket_id TEXT, channel TEXT NOT NULL,
  message TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (token_hash TEXT PRIMARY KEY, user TEXT NOT NULL, device TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
"""


class _Repo:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def one(self, sql: str, *args: Any) -> Any:
        row = self.store.db.execute(sql, args).fetchone()
        return row[0] if row else None

    def all(self, sql: str, *args: Any) -> list[Any]:
        return [row[0] for row in self.store.db.execute(sql, args).fetchall()]


class _Triggers(_Repo):
    def get(self, trigger_id: str) -> Trigger | None:
        body = self.one("SELECT body FROM triggers WHERE id = ?", trigger_id)
        return Trigger.model_validate_json(body) if body else None

    def save(self, trigger: Trigger) -> None:
        self.store.db.execute("INSERT INTO triggers (id, body) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET "
                              "body = excluded.body", (trigger.id, trigger.model_dump_json()))

    def disposition(self, trigger_id: str) -> TriggerDisposition | None:
        body = self.one("SELECT body FROM dispositions WHERE id = ?", trigger_id)
        return TriggerDisposition.model_validate_json(body) if body else None

    def save_disposition(self, trigger_id: str, value: TriggerDisposition) -> None:
        self.store.db.execute("INSERT INTO dispositions (id, body) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET "
                              "body = excluded.body", (trigger_id, value.model_dump_json()))


class _Threads(_Repo):
    def get(self, key: str) -> Thread | None:
        body = self.one("SELECT body FROM threads WHERE key = ?", key)
        return Thread.model_validate_json(body) if body else None

    def save(self, thread: Thread) -> None:
        active = 1 if thread.pending or thread.evaluating else 0
        self.store.db.execute("INSERT INTO threads (key, active, body) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE "
                              "SET active = excluded.active, body = excluded.body",
                              (thread.key, active, thread.model_dump_json()))

    def list(self) -> list[Thread]:
        return [Thread.model_validate_json(b) for b in self.all("SELECT body FROM threads ORDER BY rowid")]

    def active(self) -> list[Thread]:
        return [Thread.model_validate_json(b) for b in self.all("SELECT body FROM threads WHERE active = 1 "
                                                                 "ORDER BY rowid")]


class _Tickets(_Repo):
    def get(self, ticket_id: str) -> Ticket | None:
        body = self.one("SELECT body FROM tickets WHERE id = ?", ticket_id)
        return Ticket.model_validate_json(body) if body else None

    def save(self, ticket: Ticket) -> None:
        latest = latest_assignment(ticket)
        active = 1 if ticket.undelivered or (latest and latest.state in ("pending", "running")) else 0
        self.store.db.execute(
            "INSERT INTO tickets (id, open, active, body) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "open = excluded.open, active = excluded.active, body = excluded.body",
            (ticket.id, 0 if ticket.ended else 1, active, ticket.model_dump_json()))

    def list(self) -> list[Ticket]:
        """Newest first."""
        return [Ticket.model_validate_json(b) for b in self.all("SELECT body FROM tickets ORDER BY rowid DESC")]

    def open(self) -> list[Ticket]:
        return [Ticket.model_validate_json(b) for b in self.all("SELECT body FROM tickets WHERE open = 1 "
                                                                 "ORDER BY rowid DESC")]

    def active(self) -> list[Ticket]:
        return [Ticket.model_validate_json(b) for b in self.all("SELECT body FROM tickets WHERE active = 1 "
                                                                 "ORDER BY rowid DESC")]


class _Decisions(_Repo):
    def get(self, decision_id: str) -> Decision | None:
        body = self.one("SELECT body FROM decisions WHERE id = ?", decision_id)
        return Decision.model_validate_json(body) if body else None

    def save(self, decision: Decision) -> None:
        self.store.db.execute("INSERT INTO decisions (id, thread_key, body) VALUES (?, ?, ?) ON CONFLICT (id) DO "
                              "UPDATE SET body = excluded.body",
                              (decision.id, decision.thread_key, decision.model_dump_json()))

    def by_thread(self, key: str) -> list[Decision]:
        return [Decision.model_validate_json(b) for b in self.all("SELECT body FROM decisions WHERE thread_key = ? "
                                                                   "ORDER BY rowid", key)]


class SQLiteStore:
    def __init__(self, path: Path | str, readonly: bool = False):
        uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
        self.db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA busy_timeout = 10000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = NORMAL")
            self.db.executescript(SCHEMA)
        self.triggers = _Triggers(self)
        self.threads = _Threads(self)
        self.tickets = _Tickets(self)
        self.decisions = _Decisions(self)
        self._depth = 0
        self._after: list[Callable[[], None]] = []

    # --- transactions ---

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def atomic[T](self, work: Callable[[], T]) -> T:
        name = f"sp{self._depth}"
        hooks = len(self._after)
        self.db.execute("BEGIN IMMEDIATE" if self._depth == 0 else f"SAVEPOINT {name}")
        self._depth += 1
        try:
            result = work()
        except BaseException:
            self._depth -= 1
            del self._after[hooks:]
            if self._depth == 0:
                self.db.execute("ROLLBACK")
            else:
                self.db.execute(f"ROLLBACK TO {name}")
                self.db.execute(f"RELEASE {name}")
            raise
        self._depth -= 1
        if self._depth > 0:
            self.db.execute(f"RELEASE {name}")
            return result
        self.db.execute("COMMIT")
        actions, self._after = self._after, []
        for action in actions:
            action()
        return result

    def after_commit(self, action: Callable[[], None]) -> None:
        """Run an action once the outermost transaction commits; at once outside a transaction."""
        if self._depth == 0:
            action()
        else:
            self._after.append(action)

    # --- KV ---

    def get(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                        "value = excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    def close(self) -> None:
        self.db.close()
