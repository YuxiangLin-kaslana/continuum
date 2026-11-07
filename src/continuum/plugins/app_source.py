"""The app Source: conversations people have with Continuum through the API, such as from the desktop app.

Each user has their own conversations; a message there is a direct message to the agent. The Source keeps the
conversation (people's messages and the agent's replies) in a SQLite file of its own and pushes new rows to the
user's open streams.
"""

from __future__ import annotations

import asyncio
import json
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
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, thread TEXT NOT NULL, ticket_id TEXT NOT NULL,
  key TEXT NOT NULL, name TEXT NOT NULL, input TEXT NOT NULL, output TEXT NOT NULL, status TEXT NOT NULL,
  at TEXT NOT NULL, ended_at TEXT, UNIQUE (user, ticket_id, key));
CREATE INDEX IF NOT EXISTS steps_thread ON steps (user, thread, id);
"""
STEP_COLUMNS = ("id", "user", "thread", "ticket_id", "key", "name", "input", "output", "status", "at", "ended_at")
COLUMNS = ("id", "user", "thread", "role", "text", "message_id", "reply_to", "ticket_id", "at", "meta")


def _message(row: Any) -> dict[str, Any]:
    message = dict(zip(COLUMNS, row, strict=True))
    message["meta"] = json.loads(message["meta"]) if message.get("meta") else None
    return message


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
            # Added after the first release: what a message is besides its text, such as the project it belongs to.
            if "meta" not in {r[1] for r in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN meta TEXT")
            self._dbs[path] = db
        return self._dbs[path]

    def _row(self, ctx: Any, row_id: int) -> dict[str, Any]:
        row = self._db(ctx).execute(f"SELECT {', '.join(COLUMNS)} FROM messages WHERE id = ?", (row_id,)).fetchone()
        return _message(row) if row else None

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

    def post(self, ctx: Any, user: str, thread: str, text: str, role: str = "user",
             meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """A person's message in their conversation: stored, shown on their other devices, handed to the Engine.
        A project's scheduled run arrives the same way, as `scheduled`: the person asked for it when they confirmed
        the project."""
        at = datetime.now(UTC)
        cursor = self._db(ctx).execute(
            "INSERT INTO messages (user, thread, role, text, at, meta) VALUES (?, ?, ?, ?, ?, ?)",
            (user, thread, role, text, at.isoformat(), json.dumps(meta, ensure_ascii=False) if meta else None))
        row_id = int(cursor.lastrowid or 0)
        # Every message is a Thread of its own: it reaches no earlier work by itself. The router decides whether it
        # is something new or continues a project, and `forward` takes it there.
        ref = ThreadRef(source=ctx.instance, id=thread_id(user, f"{thread}/{row_id}"))
        ctx.receive(Trigger(
            id=f"{ctx.instance}:{row_id}", source=ctx.instance, threads=(ref,),
            actor=ActorRef(source=ctx.instance, id=user), human=True, addressed="direct", at=at, text=text,
            channel=ChannelRef(source=ctx.instance, thread=ref.id,
                               address={"user": user, "thread": thread, "message": str(row_id)})))
        row = self._row(ctx, row_id)
        self.publish(user, {"type": "message", "message": row})
        return row

    def forward(self, ctx: Any, user: str, row_id: int, to_thread: str, display_thread: str,
                meta: dict[str, Any]) -> bool:
        """Hand a person's message to the work it continues: the same words, as a Trigger on that work's Thread (whose
        tickets take it up), answered in `display_thread`. The message is marked as belonging there."""
        row = self._row(ctx, row_id)
        if row is None or row["user"] != user:
            return False
        at = datetime.fromisoformat(row["at"])
        ref = ThreadRef(source=ctx.instance, id=to_thread)
        ctx.receive(Trigger(
            id=f"{ctx.instance}:{row_id}:to:{to_thread}", source=ctx.instance, threads=(ref,),
            actor=ActorRef(source=ctx.instance, id=user), human=True, addressed="direct", at=at, text=row["text"],
            channel=ChannelRef(source=ctx.instance, thread=ref.id,
                               address={"user": user, "thread": display_thread, "message": str(row_id)})))
        self.update_meta(ctx, row_id, {**(row.get("meta") or {}), **meta})
        return True

    def history(self, ctx: Any, user: str, thread: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db(ctx).execute(
            f"SELECT {', '.join(COLUMNS)} FROM messages WHERE user = ? AND thread = ? AND id > ? ORDER BY id LIMIT ?",
            (user, thread, after, limit)).fetchall()
        return [_message(row) for row in rows]

    def everything(self, ctx: Any, user: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """The person's one conversation: every message of theirs, whatever it belongs to, oldest first."""
        if after:
            rows = self._db(ctx).execute(
                f"SELECT {', '.join(COLUMNS)} FROM messages WHERE user = ? AND id > ? ORDER BY id LIMIT ?",
                (user, after, limit)).fetchall()
        else:
            rows = list(reversed(self._db(ctx).execute(
                f"SELECT {', '.join(COLUMNS)} FROM messages WHERE user = ? ORDER BY id DESC LIMIT ?",
                (user, limit)).fetchall()))
        return [_message(row) for row in rows]

    def search(self, ctx: Any, user: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """The person's messages that contain `query` (as typed, not a pattern), newest first."""
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._db(ctx).execute(
            f"SELECT {', '.join(COLUMNS)} FROM messages WHERE user = ? AND text LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT ?", (user, pattern, limit)).fetchall()
        return [_message(row) for row in rows]

    def say(self, ctx: Any, user: str, thread: str, role: str, text: str, meta: dict[str, Any] | None = None,
            ticket_id: str | None = None) -> dict[str, Any]:
        """Something the assistant says on its own, such as a reminder or a project to confirm: kept and shown."""
        cursor = self._db(ctx).execute(
            "INSERT INTO messages (user, thread, role, text, ticket_id, at, meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user, thread, role, text, ticket_id, datetime.now(UTC).isoformat(),
             json.dumps(meta, ensure_ascii=False) if meta else None))
        row = self._row(ctx, int(cursor.lastrowid or 0))
        self.publish(user, {"type": "message", "message": row})
        return row

    def update_meta(self, ctx: Any, row_id: int, meta: dict[str, Any]) -> dict[str, Any]:
        self._db(ctx).execute("UPDATE messages SET meta = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False),
                                                                           row_id))
        row = self._row(ctx, row_id)
        self.publish(row["user"], {"type": "message-updated", "message": row})
        return row

    def steps(self, ctx: Any, user: str, thread: str | None, limit: int = 300) -> list[dict[str, Any]]:
        """The latest steps the workers took in a conversation (or in all of the person's), oldest first."""
        if thread is None:
            rows = self._db(ctx).execute(
                f"SELECT {', '.join(STEP_COLUMNS)} FROM steps WHERE user = ? ORDER BY id DESC LIMIT ?",
                (user, limit)).fetchall()
        else:
            rows = self._db(ctx).execute(
                f"SELECT {', '.join(STEP_COLUMNS)} FROM steps WHERE user = ? AND thread = ? ORDER BY id DESC LIMIT ?",
                (user, thread, limit)).fetchall()
        return [dict(zip(STEP_COLUMNS, row, strict=True)) for row in reversed(rows)]

    def step_counts(self, ctx: Any, user: str) -> dict[str, dict[str, int]]:
        """How many steps of each kind each of the user's tickets took: {ticket_id: {name: count}}."""
        rows = self._db(ctx).execute(
            "SELECT ticket_id, name, COUNT(*) FROM steps WHERE user = ? AND name NOT IN ('note', 'think') "
            "GROUP BY ticket_id, name", (user,)).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for ticket_id, name, n in rows:
            counts.setdefault(ticket_id, {})[name] = n
        return counts

    def latest_step(self, ctx: Any, user: str, ticket_id: str) -> dict[str, Any] | None:
        row = self._db(ctx).execute(
            f"SELECT {', '.join(STEP_COLUMNS)} FROM steps WHERE user = ? AND ticket_id = ? "
            "AND name NOT IN ('note', 'think') ORDER BY id DESC LIMIT 1", (user, ticket_id)).fetchone()
        return dict(zip(STEP_COLUMNS, row, strict=True)) if row else None

    def activity(self, ctx: Any, user: str, thread: str, ticket_id: str, event: dict[str, Any]) -> None:
        """What a worker is doing for this conversation: a step is kept (a running one is updated when it ends);
        the answer as it is written, and the model thinking, are only passed to the open streams."""
        if event.get("type") != "step":
            self.publish(user, {**event, "ticket_id": ticket_id, "thread": thread})
            return
        now = datetime.now(UTC).isoformat()
        ended = None if event["status"] == "running" else now
        db = self._db(ctx)
        db.execute(
            "INSERT INTO steps (user, thread, ticket_id, key, name, input, output, status, at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user, ticket_id, key) DO UPDATE SET "
            "output = excluded.output, status = excluded.status, ended_at = excluded.ended_at",
            (user, thread, ticket_id, str(event["key"]), str(event["name"]), str(event.get("input", "")),
             str(event.get("output", "")), str(event["status"]), now, ended))
        row = db.execute(f"SELECT {', '.join(STEP_COLUMNS)} FROM steps WHERE user = ? AND ticket_id = ? AND key = ?",
                         (user, ticket_id, str(event["key"]))).fetchone()
        self.publish(user, {"type": "step", "step": dict(zip(STEP_COLUMNS, row, strict=True))})

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
