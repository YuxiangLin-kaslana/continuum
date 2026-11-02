"""Stable content-derived identifiers: equal inputs always give the same id."""

import hashlib
import json
from datetime import datetime


def key(*values: str | datetime) -> str:
    text = json.dumps([v.isoformat() if isinstance(v, datetime) else v for v in values], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:32]
