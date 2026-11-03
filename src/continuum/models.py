"""Chat models: one client for any OpenAI-compatible chat-completions API (DeepSeek's among them).

A model is named in the settings (`[models.<name>]`) with its address, its model id and where its key is; plugins ask
for a model by that name. The key is read from an environment variable or a file, never from the settings file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("continuum.models")

TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    # Where the key is: an environment variable, or a file holding only the key (or a JSON object with `key_field`).
    api_key_env: str | None = "DEEPSEEK_API_KEY"
    api_key_file: str | None = None
    key_field: str | None = None
    timeout_seconds: float = Field(default=120, gt=0)
    retries: int = Field(default=3, ge=0, le=10)
    # Merged into every request, such as {"thinking": {"type": "disabled"}}.
    extra: dict[str, Any] = {}

    def key(self) -> str | None:
        if self.api_key_file:
            text = Path(self.api_key_file).expanduser().read_text(encoding="utf-8").strip()
            return json.loads(text)[self.key_field] if self.key_field else text
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class ModelError(Exception):
    pass


class ChatModel:
    """One chat model. `complete` returns the assistant message as the API gave it, including `tool_calls` and any
    `reasoning_content`, so a conversation can be sent back unchanged."""

    def __init__(self, name: str, settings: ModelSettings, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            key = self.settings.key()
            if not key:
                raise ModelError(f"model {self.name}: no API key ({self.settings.api_key_env or self.settings.api_key_file})")
            self._client = httpx.AsyncClient(
                base_url=self.settings.base_url.rstrip("/"), timeout=self.settings.timeout_seconds,
                headers={"Authorization": f"Bearer {key}"}, transport=self._transport)
        return self._client

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                       tool_choice: Any = None, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.settings.model, "messages": messages, **self.settings.extra, **extra}
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        delay = 2.0
        for attempt in range(self.settings.retries + 1):
            try:
                response = await self._http().post("/chat/completions", json=body)
            except httpx.TransportError as error:
                if attempt == self.settings.retries:
                    raise ModelError(f"model {self.name}: {type(error).__name__}: {error}") from error
            else:
                if response.status_code == 200:
                    data = response.json()
                    return data["choices"][0]["message"]
                if response.status_code not in TRANSIENT or attempt == self.settings.retries:
                    raise ModelError(f"model {self.name}: HTTP {response.status_code}: {response.text[:500]}")
            log.warning("model %s: attempt %d failed, retrying in %.0fs", self.name, attempt + 1, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        raise ModelError(f"model {self.name}: no answer")  # pragma: no cover

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """An OpenAI-style function tool."""
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or list(properties)}}}


def tool_calls(message: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(id, name, arguments) of each tool call; arguments that are not valid JSON become {"_invalid": text}."""
    out = []
    for call in message.get("tool_calls") or []:
        raw = call.get("function", {}).get("arguments") or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                args = {"_invalid": raw}
        except json.JSONDecodeError:
            args = {"_invalid": raw}
        out.append((call.get("id", ""), call.get("function", {}).get("name", ""), args))
    return out
