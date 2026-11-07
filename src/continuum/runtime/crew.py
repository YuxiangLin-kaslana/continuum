"""The crew: workers a project's lead sends out at once, sharing one limit across all projects.

A lead (a ticket's worker) hands parts of its job to sub-workers that run side by side in its workspace; each takes
a place in the crew while it works and gives it back when done. The places are what the settings call max_workers,
so however many leads dispatch, no more than that many sub-workers run at once; the rest wait their turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any


class Crew:
    def __init__(self, limit: int):
        self.limit = limit
        self._places = asyncio.Semaphore(limit)
        # What each sub-worker at work is doing, by its id.
        self.active: dict[str, dict[str, Any]] = {}
        self.waiting = 0

    @contextlib.asynccontextmanager
    async def place(self, ticket_id: str, label: str, task: str) -> AsyncIterator[dict[str, Any]]:
        self.waiting += 1
        try:
            await self._places.acquire()
        finally:
            self.waiting -= 1
        entry = {"id": secrets.token_hex(4), "ticket_id": ticket_id, "label": label, "task": task[:200],
                 "started_at": datetime.now(UTC).isoformat(), "step": None}
        self.active[entry["id"]] = entry
        try:
            yield entry
        finally:
            self.active.pop(entry["id"], None)
            self._places.release()

    def of(self, ticket_ids: set[str]) -> list[dict[str, Any]]:
        return sorted((dict(e) for e in self.active.values() if e["ticket_id"] in ticket_ids),
                      key=lambda e: e["started_at"])
