"""Files the assistant shows the person: an image it drew, a screenshot, a PDF, a table, a recording.

A worker hands a file over (from its workspace, anywhere on this host when it has the whole machine, or from one of
the person's servers); it is kept here under an unguessable id, and the person's own devices fetch it by that id.
Nobody else may: each file knows whose it is.
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_FILE = 25 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{24}$")
# Kinds browsers know that mimetypes may not.
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/csv", ".csv")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/plain", ".log")


def kind_of(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class Files:
    def __init__(self, folder: Path):
        self.folder = folder

    def keep(self, owner: str, name: str, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_FILE:
            raise ValueError(f"文件太大（{len(data) // 1024 // 1024} MB），最多 {MAX_FILE // 1024 // 1024} MB")
        name = Path(name.replace("\\", "/")).name.strip() or "file"
        ident = secrets.token_hex(12)
        where = self.folder / ident
        where.mkdir(parents=True, mode=0o700)
        (where / "data").write_bytes(data)
        meta = {"id": ident, "name": name, "mime": kind_of(name), "size": len(data), "owner": owner,
                "at": datetime.now(UTC).isoformat()}
        (where / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta

    def get(self, ident: str) -> tuple[Path, dict[str, Any]] | None:
        if not _ID.match(ident):
            return None
        where = self.folder / ident
        try:
            meta = json.loads((where / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return where / "data", meta

    @staticmethod
    def shown(meta: dict[str, Any]) -> dict[str, Any]:
        """What a message carries about a file: never whose it is."""
        return {k: meta[k] for k in ("id", "name", "mime", "size")}
