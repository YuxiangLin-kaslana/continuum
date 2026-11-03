"""The Outbox: the core's `Outbox` port, and the only writer of the receipts.

`send` keeps a message's receipt and queues its `reply` effect in the same transaction. A ticket's messages to one
conversation go out in order: a message waits while an earlier one of the same ticket to the same conversation has not
gone out, until that one has failed three times. A message is dropped only when its conversation can never receive it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from continuum.core.ports import OutboundMessage, ReplyStatus

from .effects import Effects, Later
from .store import SQLiteStore

SendResult = Literal["sent", "gone", "waiting"]


def channel_key(message: OutboundMessage) -> str:
    return json.dumps(message.channel.model_dump(), sort_keys=True)


class StoreOutbox:
    def __init__(self, store: SQLiteStore, effects: Effects,
                 transport: Callable[[OutboundMessage], Awaitable[SendResult]]):
        self.store = store
        self.effects = effects
        self.transport = transport
        effects.handle("reply", self._deliver, base=5.0, cap=300.0)

    # --- the port ---

    def send(self, message: OutboundMessage) -> None:
        if self.status(message.id) is not None:
            return
        self.store.db.execute(
            "INSERT INTO receipts (id, ticket_id, channel, message, status, at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (message.id, message.ticket_id, channel_key(message), message.model_dump_json(),
             datetime.now(UTC).isoformat()))
        self.effects.enqueue("reply", message.id, {"id": message.id})

    def status(self, message_id: str) -> ReplyStatus | None:
        row = self.store.db.execute("SELECT status FROM receipts WHERE id = ?", (message_id,)).fetchone()
        return row[0] if row else None

    def withdraw(self, message_id: str) -> None:
        if self.status(message_id) == "pending":
            self._settle(message_id, "dropped", "withdrawn")
            self.effects.cancel("reply", message_id)

    # --- reads ---

    def receipts(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, ticket_id, message, status, attempts, error FROM receipts"
        rows = self.store.db.execute(sql + (" WHERE status = ?" if status else "") + " ORDER BY seq",
                                     (status,) if status else ()).fetchall()
        return [dict(id=r[0], ticket_id=r[1], message=OutboundMessage.model_validate_json(r[2]), status=r[3],
                     attempts=r[4], error=r[5]) for r in rows]

    # --- sending ---

    def _behind(self, message_id: str) -> bool:
        row = self.store.db.execute("SELECT seq, ticket_id, channel FROM receipts WHERE id = ?",
                                    (message_id,)).fetchone()
        if row is None or row[1] is None:
            return False
        seq, ticket_id, channel = row
        earlier = self.store.db.execute(
            "SELECT 1 FROM receipts WHERE ticket_id = ? AND channel = ? AND seq < ? AND status = 'pending' AND "
            "attempts < 3 LIMIT 1", (ticket_id, channel, seq)).fetchone()
        return earlier is not None

    def _settle(self, message_id: str, status: str, error: str | None = None) -> None:
        self.store.db.execute("UPDATE receipts SET status = ?, error = ? WHERE id = ?", (status, error, message_id))

    async def _deliver(self, payload: dict[str, Any]) -> None:
        message_id = payload["id"]
        row = self.store.db.execute("SELECT message, status FROM receipts WHERE id = ?", (message_id,)).fetchone()
        if row is None or row[1] != "pending":
            return
        if self._behind(message_id):
            raise Later(5.0)
        try:
            result = await self.transport(OutboundMessage.model_validate_json(row[0]))
        except Exception as error:
            self.store.db.execute("UPDATE receipts SET attempts = attempts + 1, error = ? WHERE id = ?",
                                  (str(error)[:500], message_id))
            raise
        if result == "waiting":
            raise Later(5.0)
        self._settle(message_id, "sent" if result == "sent" else "dropped",
                     None if result == "sent" else "conversation gone")
