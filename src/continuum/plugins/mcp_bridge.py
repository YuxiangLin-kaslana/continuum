"""Continuum's own tools, lent to Claude Code while it works a turn.

Claude Code runs its own loop with its own tools (shell, files). The tools that are Continuum's (a person's servers
and their approvals, coding agents on servers, sub-workers, files shown to the person, memory, connected services)
live in the Continuum process, with the job they act for. While a turn runs, the process listens on a local socket
for that turn's calls under a one-time token; Claude Code starts this module as an MCP server (stdio), which tells it
the tools and passes each call to the socket. The call is run there as the DeepSeek worker would run it: the person
sees the same steps, approvals and results.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

Handler = Callable[[str, dict[str, Any]], Awaitable[str]]
PROTOCOL = "2025-06-18"


class Bridge:
    """The listening end, in the Continuum process: one socket, a handler per running turn."""

    def __init__(self) -> None:
        self.turns: dict[str, Handler] = {}
        self.server: asyncio.AbstractServer | None = None
        self.path = Path(tempfile.gettempdir()) / f"continuum-bridge-{os.getuid()}-{os.getpid()}.sock"

    async def start(self) -> None:
        if self.server is not None:
            return
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        os.chmod(self.path, 0o600)

    def open(self, handler: Handler) -> str:
        token = secrets.token_hex(16)
        self.turns[token] = handler
        return token

    def close(self, token: str) -> None:
        self.turns.pop(token, None)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            ask = json.loads(line)
            handler = self.turns.get(str(ask.get("token")))
            if handler is None:
                answer = {"text": "错误：这一轮已经结束了", "error": True}
            else:
                text = await handler(str(ask.get("name", "")), dict(ask.get("args") or {}))
                answer = {"text": text, "error": text.startswith(("错误：", "被拒绝："))}
        except Exception as error:  # noqa: BLE001 - the agent hears what went wrong
            answer = {"text": f"错误：{type(error).__name__}: {error}", "error": True}
        try:
            writer.write((json.dumps(answer, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()


_bridge: Bridge | None = None


async def bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    await _bridge.start()
    return _bridge


def mcp_config(path: Path, token: str, tools_file: Path) -> dict[str, Any]:
    """What Claude Code is given (--mcp-config) to start this module for one turn."""
    return {"mcpServers": {"continuum": {
        "command": sys.executable, "args": ["-m", "continuum.plugins.mcp_bridge"],
        "env": {"CONTINUUM_BRIDGE": str(path), "CONTINUUM_TOKEN": token, "CONTINUUM_TOOLS": str(tools_file)}}}}


def as_mcp(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI-style tool definitions, as MCP lists them."""
    out = []
    for t in tools:
        f = t["function"]
        out.append({"name": f["name"], "description": f.get("description", ""),
                    "inputSchema": f.get("parameters") or {"type": "object", "properties": {}}})
    return out


# --- the MCP server, started by Claude Code (stdio, one JSON-RPC message a line) ---

async def _call(path: str, token: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps({"token": token, "name": name, "args": args}, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line) if line else {"text": "错误：Continuum 没有回答", "error": True}


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def serve() -> None:
    path, token = os.environ["CONTINUUM_BRIDGE"], os.environ["CONTINUUM_TOKEN"]
    tools = json.loads(Path(os.environ["CONTINUUM_TOOLS"]).read_text(encoding="utf-8"))
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 24)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    pending: set[asyncio.Task] = set()

    async def answer(message: dict[str, Any]) -> None:
        ident, method, params = message.get("id"), message.get("method"), message.get("params") or {}
        if method == "initialize":
            result: Any = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                           "capabilities": {"tools": {}}, "serverInfo": {"name": "continuum", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            got = await _call(path, token, str(params.get("name", "")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": got.get("text", "")}], "isError": bool(got.get("error"))}
        elif method == "ping":
            result = {}
        elif ident is None:
            return  # a notification
        else:
            _send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"no method {method}"}})
            return
        if ident is not None:
            _send({"jsonrpc": "2.0", "id": ident, "result": result})

    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            message = json.loads(line)
        except ValueError:
            continue
        # Calls run side by side: a long one (a sub-worker, a render) does not hold up the next.
        task = asyncio.create_task(answer(message))
        pending.add(task)
        task.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(serve())
