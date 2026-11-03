"""Effects: the durable side-effect queue. Store first, do afterwards.

A command enqueues work inside its transaction, so the work exists exactly when the domain change does. A pass runs the
due rows oldest first; a failure is tried again with a growing delay, and `Later` defers without counting a failure.
One row per (kind, key): enqueuing a waiting key again replaces its payload and makes it due at once.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .store import SQLiteStore

log = logging.getLogger("continuum.effects")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class Later(Exception):
    """Try again after `delay` seconds without counting a failure, such as when no worker slot is free."""

    def __init__(self, delay: float = 5.0):
        super().__init__(f"later in {delay}s")
        self.delay = delay


@dataclass
class _Kind:
    run: Handler
    base: float
    cap: float


class Effects:
    # A kind reports a problem once one of its rows failed this many times in a row.
    PROBLEM_AFTER = 3

    def __init__(self, store: SQLiteStore, clock: Callable[[], float] = time.time,
                 on_enqueue: Callable[[], None] | None = None):
        self.store = store
        self.clock = clock
        self.on_enqueue = on_enqueue
        self.kinds: dict[str, _Kind] = {}

    def handle(self, kind: str, run: Handler, base: float = 2.0, cap: float = 60.0) -> None:
        self.kinds[kind] = _Kind(run, base, cap)

    def enqueue(self, kind: str, key: str, payload: dict[str, Any], delay: float = 0.0) -> None:
        self.store.db.execute(
            "INSERT INTO effects (kind, key, payload, due) VALUES (?, ?, ?, ?) ON CONFLICT (kind, key) DO UPDATE SET "
            "payload = excluded.payload, due = excluded.due, version = version + 1",
            (kind, key, json.dumps(payload, ensure_ascii=False), self.clock() + delay))
        if self.on_enqueue:
            self.store.after_commit(self.on_enqueue)

    def cancel(self, kind: str, key: str) -> None:
        self.store.db.execute("DELETE FROM effects WHERE kind = ? AND key = ?", (kind, key))

    def pending(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT kind, key, payload, due, failures, error FROM effects"
        rows = self.store.db.execute(sql + (" WHERE kind = ?" if kind else "") + " ORDER BY seq",
                                     (kind,) if kind else ()).fetchall()
        return [dict(kind=r[0], key=r[1], payload=json.loads(r[2]), due=r[3], failures=r[4], error=r[5])
                for r in rows]

    def problems(self) -> list[dict[str, Any]]:
        return [row for row in self.pending() if row["failures"] >= self.PROBLEM_AFTER]

    def next_due(self) -> float | None:
        if not self.kinds:
            return None
        row = self.store.db.execute("SELECT MIN(due) FROM effects WHERE kind IN (%s)"
                                    % ",".join("?" * len(self.kinds)), tuple(self.kinds)).fetchone()
        return row[0] if row else None

    async def run_due(self) -> int:
        """Run every due row once, oldest first. Returns how many ran."""
        if not self.kinds:
            return 0
        rows = self.store.db.execute(
            "SELECT seq, kind, key, payload, version, failures FROM effects WHERE due <= ? AND kind IN (%s) "
            "ORDER BY seq" % ",".join("?" * len(self.kinds)), (self.clock(), *self.kinds)).fetchall()
        for seq, kind, key, payload, version, failures in rows:
            handler = self.kinds[kind]
            try:
                await handler.run(json.loads(payload))
            except Later as later:
                self.store.db.execute("UPDATE effects SET due = ? WHERE seq = ? AND version = ?",
                                      (self.clock() + later.delay, seq, version))
                continue
            except Exception as error:  # noqa: BLE001 - every failure is recorded and retried
                failures += 1
                delay = min(handler.base * 2 ** (failures - 1), handler.cap)
                log.warning("effect %s:%s failed (%d): %s", kind, key, failures, error)
                self.store.db.execute("UPDATE effects SET due = ?, failures = ?, error = ? WHERE seq = ? AND "
                                      "version = ?", (self.clock() + delay, failures, str(error)[:500], seq, version))
                continue
            # A row enqueued again while it ran keeps its newer payload.
            self.store.db.execute("DELETE FROM effects WHERE seq = ? AND version = ?", (seq, version))
        return len(rows)
