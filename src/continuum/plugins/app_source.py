"""The app Source: conversations people have with Continuum through the API, such as from the desktop app.

Each user has their own conversations; a message there is a direct message to the agent. The Source keeps the
conversation (people's messages and the agent's replies) in a SQLite file of its own and pushes new rows to the
user's open streams.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from continuum.core.model import ActorRef, ChannelRef, ThreadRef, Trigger
from continuum.core.ports import OutboundMessage, ScopeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, thread TEXT NOT NULL, role TEXT NOT NULL,
  text TEXT NOT NULL, message_id TEXT UNIQUE, reply_to TEXT, ticket_id TEXT, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (user, thread, id);
"""
COLUMNS = ("id", "user", "thread", "role", "text", "message_id", "reply_to", "ticket_id", "at")


class AppConfig(BaseModel):
    pass


def thread_id(user: str, thread: str) -> str:
    """A user's conversation, as the domain names its Thread."""
    return f"{user}/{thread}"


class AppSource:
    kind = "app"
    config_schema = AppConfig

    def __init__(self) -> None:
        self._streams: dict[str, set[asyncio.Queue]] = {}
        self._dbs: dict[Path, sqlite3.Connection] = {}

    # --- storage ---

    def _db(self, ctx: Any) -> sqlite3.Connection:
        path = ctx.data_dir / "app.db"
        if path not in self._dbs:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            self._dbs[path] = db
        return self._dbs[path]

    def _row(self, ctx: Any, row_id: int) -> dict[str, Any]:
        row = self._db(ctx).execute(f"SELECT {', '.join(COLUMNS)} FROM messages WHERE id = ?", (row_id,)).fetchone()
        return dict(zip(COLUMNS, row, strict=True))

    # --- the SourceKind protocol ---

    def scope(self, config: AppConfig, meta: dict[str, Any]) -> ScopeResult:
        return ScopeResult(listened=True, observed=False, row="direct")

    async def run(self, ctx: Any) -> None:
        # Messages arrive through `post`, called by the API; there is nothing to poll.
        await asyncio.Event().wait()

    async def send(self, ctx: Any, message: OutboundMessage) -> Literal["sent", "gone"]:
        user = message.channel.address.get("user")
        thread = message.channel.address.get("thread")
        if not user or not thread:
            return "gone"
        cursor = self._db(ctx).execute(
            "INSERT OR IGNORE INTO messages (user, thread, role, text, message_id, reply_to, ticket_id, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user, thread, message.kind, message.text, message.id, message.channel.address.get("message"),
             message.ticket_id, datetime.now(UTC).isoformat()))
        if cursor.rowcount:
            self.publish(user, {"type": "message", "message": self._row(ctx, int(cursor.lastrowid or 0))})
        return "sent"

    # --- called by the API ---

    def post(self, ctx: Any, user: str, thread: str, text: str) -> dict[str, Any]:
        """A person's message in their conversation: stored, shown on their other devices, handed to the Engine."""
        at = datetime.now(UTC)
        cursor = self._db(ctx).execute(
            "INSERT INTO messages (user, thread, role, text, at) VALUES (?, ?, 'user', ?, ?)",
            (user, thread, text, at.isoformat()))
        row_id = int(cursor.lastrowid or 0)
        ref = ThreadRef(source=ctx.instance, id=thread_id(user, thread))
        ctx.receive(Trigger(
            id=f"{ctx.instance}:{row_id}", source=ctx.instance, threads=(ref,),
            actor=ActorRef(source=ctx.instance, id=user), human=True, addressed="direct", at=at, text=text,
            channel=ChannelRef(source=ctx.instance, thread=ref.id,
                               address={"user": user, "thread": thread, "message": str(row_id)})))
        row = self._row(ctx, row_id)
        self.publish(user, {"type": "message", "message": row})
        return row

    def history(self, ctx: Any, user: str, thread: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db(ctx).execute(
            f"SELECT {', '.join(COLUMNS)} FROM messages WHERE user = ? AND thread = ? AND id > ? ORDER BY id LIMIT ?",
            (user, thread, after, limit)).fetchall()
        return [dict(zip(COLUMNS, row, strict=True)) for row in rows]

    def threads(self, ctx: Any, user: str) -> list[dict[str, Any]]:
        rows = self._db(ctx).execute(
            "SELECT thread, MAX(id), MAX(at), COUNT(*) FROM messages WHERE user = ? GROUP BY thread "
            "ORDER BY MAX(id) DESC", (user,)).fetchall()
        return [dict(thread=r[0], last_id=r[1], last_at=r[2], count=r[3]) for r in rows]

    # --- live streams ---

    def subscribe(self, user: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._streams.setdefault(user, set()).add(queue)
        return queue

    def unsubscribe(self, user: str, queue: asyncio.Queue) -> None:
        self._streams.get(user, set()).discard(queue)

    def publish(self, user: str, event: dict[str, Any]) -> None:
        for queue in list(self._streams.get(user, ())):
            if queue.full():
                # A stream that stopped reading is dropped; its client reconnects and catches up by `after`.
                self.unsubscribe(user, queue)
                continue
            queue.put_nowait(event)

    def close_streams(self) -> None:
        """Tell every open stream the host is going away, so it closes at once."""
        for queues in self._streams.values():
            for queue in list(queues):
                if not queue.full():
                    queue.put_nowait({"type": "closing"})

    def close(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()
