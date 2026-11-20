"""What the person asked the assistant to remember: preferences and agreements that hold for all their work.

"尽量用 Claude", "报告用中文表格", "生产机先问我": a standing word, not a job. Each is a short line kept per person;
every worker of theirs is told all of them when it starts. The person sees them in the app and may take any back.
A long-running project is something else: work to do again on a schedule.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

MAX_MEMORIES = 60
MAX_TEXT = 300


class Memories:
    def __init__(self, store: Any):
        self.store = store

    def list(self, user: str) -> list[dict[str, Any]]:
        return list(self.store.get(f"memories:{user}") or [])

    def add(self, user: str, text: str, by: str = "person") -> dict[str, Any]:
        text = " ".join(str(text).split())[:MAX_TEXT]
        if not text:
            raise ValueError("要记的内容是空的")
        kept = self.list(user)
        same = next((m for m in kept if m["text"] == text), None)
        if same is not None:
            return same
        if len(kept) >= MAX_MEMORIES:
            raise ValueError(f"已经记了 {MAX_MEMORIES} 条，先删掉一些不再需要的")
        memory = {"id": secrets.token_hex(4), "text": text, "at": datetime.now(UTC).isoformat(), "by": by}
        self.store.set(f"memories:{user}", [*kept, memory])
        return memory

    def remove(self, user: str, ident: str) -> bool:
        kept = self.list(user)
        left = [m for m in kept if m["id"] != ident]
        if len(left) == len(kept):
            return False
        self.store.set(f"memories:{user}", left)
        return True

    def brief(self, user: str | None) -> str:
        """The lines a worker is told, or nothing."""
        kept = self.list(user) if user else []
        if not kept:
            return ""
        lines = "\n".join(f"- [{m['id']}] {m['text']}" for m in kept)
        return f"\n【人要你记住的事】（一直有效，干活时照做；和本次请求冲突时以本次请求为准）\n{lines}\n"
