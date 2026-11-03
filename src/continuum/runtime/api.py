"""The HTTP and WebSocket API, for the desktop app and other clients.

Every route but `/health` needs a bearer token (`continuum user add`); the stream takes it as `?token=`. A user sees
only their own conversations and the tickets started from them.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from continuum.core.errors import UserError
from continuum.core.model import ActorRef, Ticket
from continuum.core.stage import can_retry, ticket_stage

if TYPE_CHECKING:
    from .app import Runtime

THREAD = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PostMessage(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class StopRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def ticket_view(ticket: Ticket) -> dict[str, Any]:
    stage = ticket_stage(ticket)
    report = ticket.assignments[-1].report if ticket.assignments else None
    thread = next((c.address.get("thread") for c in ticket.channels if c.address.get("thread")), None)
    return {
        "id": ticket.id, "title": ticket.title, "thread": thread,
        "stage": stage.stage, "action": stage.action,
        "outcome": ticket.ended.outcome if ticket.ended else None,
        "question": ticket.attention.question if ticket.attention else None,
        "report": report.model_dump(mode="json") if report else None,
        "can_retry": can_retry(ticket),
        "created_at": ticket.created_at.isoformat(), "updated_at": ticket.updated_at.isoformat(),
    }


def create_app(rt: Runtime) -> FastAPI:
    # Every route is `async def`, so it runs on the host's event loop: the Engine and the Store are used from that
    # loop only, never from FastAPI's thread pool.
    app = FastAPI(title="Continuum", version="0.0.1", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def source() -> tuple[Any, Any]:
        ctx = next((c for c in rt.sources.values() if c.kind.kind == "app"), None)
        if ctx is None:
            raise HTTPException(503, "the app Source is not running")
        return ctx.kind, ctx

    async def user_of(authorization: str | None = Header(default=None)) -> str:
        token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
        user = rt.accounts.verify(token)
        if user is None:
            raise HTTPException(401, "a valid bearer token is required")
        return user

    def owns(user: str, ticket: Ticket) -> bool:
        ctx = next((c for c in rt.sources.values() if c.kind.kind == "app"), None)
        instance = ctx.instance if ctx else "app"
        return ticket.mandate.actor == ActorRef(source=instance, id=user) or any(
            c.source == instance and c.address.get("user") == user for c in ticket.channels)

    def ticket_of(user: str, ticket_id: str) -> Ticket:
        ticket = rt.store.tickets.get(ticket_id)
        if ticket is None or not owns(user, ticket):
            raise HTTPException(404, "no such ticket")
        return ticket

    def thread_name(thread: str) -> str:
        if not THREAD.match(thread):
            raise HTTPException(422, "a thread name is 1-64 letters, digits, '_' or '-'")
        return thread

    def refused(error: UserError) -> HTTPException:
        return HTTPException(404 if error.missing else 409, {"code": error.code, "message": str(error)})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/v1/me")
    async def me(user: str = Depends(user_of)) -> dict[str, Any]:
        return {"user": user, "problems": rt.problems}

    @app.get("/api/v1/threads")
    async def threads(user: str = Depends(user_of)) -> list[dict[str, Any]]:
        kind, ctx = source()
        return kind.threads(ctx, user)

    @app.get("/api/v1/threads/{thread}/messages")
    async def history(thread: str, after: int = 0, user: str = Depends(user_of)) -> list[dict[str, Any]]:
        kind, ctx = source()
        return kind.history(ctx, user, thread_name(thread), after)

    @app.post("/api/v1/threads/{thread}/messages", status_code=201)
    async def post(thread: str, body: PostMessage, user: str = Depends(user_of)) -> dict[str, Any]:
        kind, ctx = source()
        return kind.post(ctx, user, thread_name(thread), body.text)

    @app.get("/api/v1/tickets")
    async def tickets(open_only: bool = Query(default=False, alias="open"), user: str = Depends(user_of)) \
            -> list[dict[str, Any]]:
        listed = rt.store.tickets.open() if open_only else rt.store.tickets.list()
        return [ticket_view(t) for t in listed if owns(user, t)][:200]

    @app.get("/api/v1/tickets/{ticket_id}")
    async def ticket(ticket_id: str, user: str = Depends(user_of)) -> dict[str, Any]:
        return ticket_view(ticket_of(user, ticket_id))

    @app.post("/api/v1/tickets/{ticket_id}/stop")
    async def stop(ticket_id: str, body: StopRequest | None = None, user: str = Depends(user_of)) -> dict[str, Any]:
        ticket_of(user, ticket_id)
        try:
            return ticket_view(rt.engine.stop_ticket(ticket_id, body.reason if body else None))
        except UserError as error:
            raise refused(error) from None

    @app.post("/api/v1/tickets/{ticket_id}/retry")
    async def retry(ticket_id: str, user: str = Depends(user_of)) -> dict[str, Any]:
        ticket_of(user, ticket_id)
        try:
            return ticket_view(rt.engine.retry_ticket(ticket_id))
        except UserError as error:
            raise refused(error) from None

    @app.websocket("/api/v1/stream")
    async def stream(socket: WebSocket, token: str | None = None) -> None:
        user = rt.accounts.verify(token)
        if user is None:
            await socket.close(code=4401)
            return
        kind, _ = source()
        await socket.accept()
        queue = kind.subscribe(user)
        try:
            await socket.send_json({"type": "hello", "user": user})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                except TimeoutError:
                    event = {"type": "ping"}
                if event.get("type") == "closing":
                    await socket.close(code=1001)
                    return
                if event.get("type") == "ticket":
                    current = rt.store.tickets.get(event["id"])
                    if current is None or not owns(user, current):
                        continue
                    event = {"type": "ticket", "ticket": ticket_view(current)}
                await socket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            kind.unsubscribe(user, queue)

    return app
