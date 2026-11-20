"""A prompt the person keeps on the server and switches on or off: added at the very end of a worker's context.

The text is the person's own; Continuum never shows it back (not in the app, not in a transcript, not in a step). When
the person's switch is on, each model call of their work gets it as the last message of what is sent, and only there:
it is not kept in the conversation the worker goes on with, so the next call sees it again at the end, once, and
nothing that follows carries it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# As the last message of a request. A system message keeps it apart from the person's words and from tool results.
ROLE = "system"


class Injection:
    def __init__(self, home: Path, store: Any):
        self.folder = home / "prompts"
        self.store = store

    @property
    def path(self) -> Path:
        return self.folder / "inject.md"

    def info(self) -> dict[str, Any]:
        """What the app may know: whether there is one, under what name, and how long. Never the words."""
        try:
            meta = json.loads((self.folder / "inject.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        there = self.path.is_file() and self.path.stat().st_size > 0
        return {"available": there, "name": meta.get("name") or ("提示词" if there else None),
                "chars": meta.get("chars") if there else None}

    def enabled(self, user: str) -> bool:
        return bool((self.store.get(f"inject:{user}") or {}).get("on"))

    def switch(self, user: str, on: bool) -> dict[str, Any]:
        self.store.set(f"inject:{user}", {"on": bool(on)})
        return {**self.info(), "enabled": self.enabled(user)}

    def text_for(self, user: str | None) -> str | None:
        """The words to add for this person's work now: None when their switch is off or there are none."""
        if not user or not self.enabled(user):
            return None
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def add(self, messages: list[dict[str, Any]], user: str | None) -> list[dict[str, Any]]:
        """What to send: the conversation as it is, and the words after everything else (a new list: the
        conversation the worker keeps is not touched)."""
        text = self.text_for(user)
        return [*messages, {"role": ROLE, "content": text}] if text else messages
