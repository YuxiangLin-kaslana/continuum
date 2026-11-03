"""The command-line Source: a conversation kept in a small SQLite file of its own.

`continuum send` appends a message to the inbox; the running Source reads new rows as Triggers. Replies go to the
outbox table, where `continuum send --wait` and `continuum log` read them. A message id is unique, so a reply sent
twice after a crash lands once.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from continuum.core.model import ActorRef, ChannelRef, ThreadRef, Trigger
from continuum.core.ports import OutboundMessage, ScopeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,
  sender TEXT NOT NULL, text TEXT NOT NULL, mention INTEGER NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,
  conversation TEXT NOT NULL, reply_to TEXT, kind TEXT NOT NULL, text TEXT NOT NULL, ticket_id TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS position (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def db_path(home: Path, instance: str = "cli") -> Path:
    return home / "data" / instance / "cli.db"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None, timeout=10)
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    return db


def post(path: Path, sender: str, conversation: str, text: str, mention: bool = True) -> int:
    db = connect(path)
    try:
        cursor = db.execute("INSERT INTO inbox (conversation, sender, text, mention, at) VALUES (?, ?, ?, ?, ?)",
                            (conversation, sender, text, int(mention), datetime.now(UTC).isoformat()))
        return int(cursor.lastrowid or 0)
    finally:
        db.close()


def replies(path: Path, conversation: str | None = None, after: int = 0) -> list[dict[str, Any]]:
    db = connect(path)
    try:
        sql = "SELECT id, conversation, reply_to, kind, text, ticket_id, at FROM outbox WHERE id > ?"
        args: tuple[Any, ...] = (after,)
        if conversation is not None:
            sql += " AND conversation = ?"
            args += (conversation,)
        rows = db.execute(sql + " ORDER BY id", args).fetchall()
    finally:
        db.close()
    keys = ("id", "conversation", "reply_to", "kind", "text", "ticket_id", "at")
    return [dict(zip(keys, row, strict=True)) for row in rows]


class CliConfig(BaseModel):
    poll_seconds: float = Field(default=0.3, gt=0)


class CliSource:
    kind = "cli"
    config_schema = CliConfig

    def scope(self, config: CliConfig, meta: dict[str, Any]) -> ScopeResult:
        # A command-line conversation receives every message, addressed or not.
        return ScopeResult(listened=True, observed=True, row="conversation")

    async def run(self, ctx: Any) -> None:
        db = connect(ctx.data_dir / "cli.db")
        try:
            while True:
                row = db.execute("SELECT value FROM position WHERE name = 'read'").fetchone()
                position = row[0] if row else 0
                for id_, conversation, sender, text, mention, at in db.execute(
                        "SELECT id, conversation, sender, text, mention, at FROM inbox WHERE id > ? ORDER BY id",
                        (position,)).fetchall():
                    ctx.receive(Trigger(
                        id=f"{ctx.instance}:{id_}", source=ctx.instance,
                        threads=(ThreadRef(source=ctx.instance, id=conversation),),
                        actor=ActorRef(source=ctx.instance, id=sender), human=True,
                        addressed="mention" if mention else None, at=datetime.fromisoformat(at), text=text,
                        channel=ChannelRef(source=ctx.instance, thread=conversation, address={"message": str(id_)})))
                    # The Engine stores a Trigger once, so moving the position after it is safe across a crash.
                    db.execute("INSERT INTO position (name, value) VALUES ('read', ?) ON CONFLICT (name) DO UPDATE "
                               "SET value = excluded.value", (id_,))
                await asyncio.sleep(ctx.config.poll_seconds)
        finally:
            db.close()

    async def send(self, ctx: Any, message: OutboundMessage) -> Literal["sent", "gone"]:
        db = connect(ctx.data_dir / "cli.db")
        try:
            db.execute("INSERT OR IGNORE INTO outbox (message_id, conversation, reply_to, kind, text, ticket_id, at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (message.id, message.channel.thread, message.channel.address.get("message"), message.kind,
                        message.text, message.ticket_id, datetime.now(UTC).isoformat()))
        finally:
            db.close()
        return "sent"
