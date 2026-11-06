"""Chat models: one client for any OpenAI-compatible chat-completions API (DeepSeek's among them).

A model is named in the settings (`[models.<name>]`) with its address, its model id and where its key is; plugins ask
for a model by that name. The key is read from an environment variable or a file, never from the settings file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
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

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                     on_progress: Callable[[dict[str, Any]], None] | None = None, **extra: Any) -> dict[str, Any]:
        """`complete`, streamed: `on_progress` sees the assistant message as it grows (content, reasoning and tool
        calls with their arguments so far), and the whole message is returned the same way `complete` returns it. A
        retry starts the message over, so `on_progress` may see it shrink."""
        body: dict[str, Any] = {"model": self.settings.model, "messages": messages, **self.settings.extra, **extra,
                                "stream": True}
        if tools:
            body["tools"] = tools
        delay = 2.0
        for attempt in range(self.settings.retries + 1):
            try:
                async with self._http().stream("POST", "/chat/completions", json=body) as response:
                    if response.status_code == 200:
                        return await _gather(response, on_progress)
                    text = (await response.aread()).decode("utf-8", "replace")
                    if response.status_code not in TRANSIENT or attempt == self.settings.retries:
                        raise ModelError(f"model {self.name}: HTTP {response.status_code}: {text[:500]}")
            except (httpx.TransportError, json.JSONDecodeError) as error:
                if attempt == self.settings.retries:
                    raise ModelError(f"model {self.name}: {type(error).__name__}: {error}") from error
            log.warning("model %s: attempt %d failed, retrying in %.0fs", self.name, attempt + 1, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        raise ModelError(f"model {self.name}: no answer")  # pragma: no cover

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


async def _gather(response: httpx.Response, on_progress: Callable[[dict[str, Any]], None] | None) -> dict[str, Any]:
    """Put a streamed answer (server-sent events of deltas) back together into one assistant message."""
    message: dict[str, Any] = {"role": "assistant", "content": ""}
    calls: dict[int, dict[str, Any]] = {}
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        choices = json.loads(data).get("choices") or []
        delta = (choices[0].get("delta") or {}) if choices else {}
        if not delta:
            continue
        if delta.get("content"):
            message["content"] += delta["content"]
        if delta.get("reasoning_content"):
            message["reasoning_content"] = message.get("reasoning_content", "") + delta["reasoning_content"]
        for part in delta.get("tool_calls") or []:
            call = calls.setdefault(part.get("index", 0),
                                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            call["id"] = part.get("id") or call["id"]
            function = part.get("function") or {}
            call["function"]["name"] += function.get("name") or ""
            call["function"]["arguments"] += function.get("arguments") or ""
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        if on_progress:
            on_progress(message)
    return message


def partial_string(raw: str, field: str) -> str | None:
    """The value of a string field in a JSON object that is still arriving, as far as it has come; None before the
    field starts. Only the first such field is read, wherever it is."""
    match = re.search(r'"%s"\s*:\s*"' % re.escape(field), raw)
    if match is None:
        return None
    out: list[str] = []
    i = match.end()
    while i < len(raw):
        c = raw[i]
        if c == '"':
            break
        if c == "\\":
            if i + 1 >= len(raw):
                break
            escaped = raw[i + 1]
            if escaped == "u":
                try:
                    out.append(chr(int(raw[i + 2:i + 6], 16)) if i + 6 <= len(raw) else "")
                except ValueError:
                    break
                if i + 6 > len(raw):
                    break
                i += 6
                continue
            out.append(ESCAPES.get(escaped, escaped))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


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
