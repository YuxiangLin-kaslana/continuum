"""Effects: the durable side-effect queue. Store first, do afterwards.

A command enqueues work inside its transaction, so the work exists exactly when the domain change does. A pass runs the
due rows oldest first; a failure is tried again with a growing delay, and `Later` defers without counting a failure.
One row per (kind, key): enqueuing a waiting key again replaces its payload and makes it due at once.
Each kind runs up to `concurrency` rows at once, oldest first; a kind with concurrency 1 keeps its rows in order.
"""

from __future__ import annotations

import asyncio
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
    concurrency: int = 1


class Effects:
    # A kind reports a problem once one of its rows failed this many times in a row.
    PROBLEM_AFTER = 3

    def __init__(self, store: SQLiteStore, clock: Callable[[], float] = time.time,
                 on_enqueue: Callable[[], None] | None = None):
        self.store = store
        self.clock = clock
        self.on_enqueue = on_enqueue
        self.kinds: dict[str, _Kind] = {}
        # Rows whose handler is running, by seq, with their kind.
        self.running: dict[int, tuple[str, asyncio.Task]] = {}

    def handle(self, kind: str, run: Handler, base: float = 2.0, cap: float = 60.0, concurrency: int = 1) -> None:
        self.kinds[kind] = _Kind(run, base, cap, concurrency)

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
        """When the earliest row that is not running is due."""
        if not self.kinds:
            return None
        rows = self.store.db.execute("SELECT seq, due FROM effects WHERE kind IN (%s) ORDER BY due"
                                     % ",".join("?" * len(self.kinds)), tuple(self.kinds)).fetchall()
        return next((due for seq, due in rows if seq not in self.running), None)

    async def run_due(self, wait: bool = True) -> int:
        """Start every due row its kind has room for, oldest first. With `wait`, return once they finished;
        without, leave them running (the host's loop). Returns how many started."""
        if not self.kinds:
            return 0
        rows = self.store.db.execute(
            "SELECT seq, kind, key, payload, version, failures FROM effects WHERE due <= ? AND kind IN (%s) "
            "ORDER BY seq" % ",".join("?" * len(self.kinds)), (self.clock(), *self.kinds)).fetchall()
        started = []
        for seq, kind, key, payload, version, failures in rows:
            if seq in self.running:
                continue
            busy = sum(1 for k, _ in self.running.values() if k == kind)
            if busy >= self.kinds[kind].concurrency:
                continue
            task = asyncio.create_task(self._run(seq, kind, key, payload, version, failures),
                                       name=f"effect-{kind}")
            self.running[seq] = (kind, task)
            task.add_done_callback(lambda _, seq=seq: self.running.pop(seq, None))
            started.append(task)
        if wait and started:
            await asyncio.gather(*started)
        return len(started)

    async def _run(self, seq: int, kind: str, key: str, payload: str, version: int, failures: int) -> None:
        handler = self.kinds[kind]
        try:
            await handler.run(json.loads(payload))
        except Later as later:
            self.store.db.execute("UPDATE effects SET due = ? WHERE seq = ? AND version = ?",
                                  (self.clock() + later.delay, seq, version))
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - every failure is recorded and retried
            failures += 1
            delay = min(handler.base * 2 ** (failures - 1), handler.cap)
            log.warning("effect %s:%s failed (%d): %s", kind, key, failures, error)
            self.store.db.execute("UPDATE effects SET due = ?, failures = ?, error = ? WHERE seq = ? AND "
                                  "version = ?", (self.clock() + delay, failures, str(error)[:500], seq, version))
            return
        # A row enqueued again while it ran keeps its newer payload.
        self.store.db.execute("DELETE FROM effects WHERE seq = ? AND version = ?", (seq, version))
        if self.on_enqueue:
            self.on_enqueue()

    async def shutdown(self) -> None:
        """Cancel running handlers; their rows stay and run again after a restart."""
        tasks = [task for _, task in self.running.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
