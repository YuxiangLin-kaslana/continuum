"""The HTTP and WebSocket API, for the desktop app and other clients.

Every route but `/health` needs a bearer token (`continuum user add`); the stream takes it as `?token=`. A user sees
only their own conversations and the tickets started from them.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from continuum.core.errors import UserError
from continuum.core.model import ActorRef, Ticket
from continuum.core.stage import can_retry, ticket_stage

from .ssh import APPROVALS, Host

if TYPE_CHECKING:
    from .app import Runtime

THREAD = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PostMessage(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)
    device: str = Field(default="", max_length=80)


class ApprovalRequest(BaseModel):
    approve: bool


class Throttle:
    """Failed logins per user and per client address: after `limit` failures in `window` seconds, refuse for the
    rest of the window. Kept in memory; a restart clears it."""

    def __init__(self, limit: int = 5, window: float = 600.0):
        self.limit = limit
        self.window = window
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self.failures.get(key, []) if now - t < self.window]
        self.failures[key] = recent
        return recent

    def blocked(self, keys: list[str], now: float) -> bool:
        return any(len(self._recent(k, now)) >= self.limit for k in keys)

    def failed(self, keys: list[str], now: float) -> None:
        for k in keys:
            self._recent(k, now).append(now)

    def succeeded(self, keys: list[str]) -> None:
        for k in keys:
            self.failures.pop(k, None)


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

    throttle = Throttle()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/api/v1/login")
    async def login(body: LoginRequest, request: Request) -> dict[str, Any]:
        # Behind a proxy the client's address is the first hop it names.
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
        address = forwarded.split(",")[0].strip() or (request.client.host if request.client else "")
        keys = [f"user:{body.username.lower()}", f"ip:{address}"]
        now = time.monotonic()
        if throttle.blocked(keys, now):
            raise HTTPException(429, "登录失败次数太多，请过十分钟再试")
        token = rt.accounts.login(body.username, body.password, body.device)
        if token is None:
            throttle.failed(keys, now)
            raise HTTPException(401, "用户名或密码不对")
        throttle.succeeded(keys)
        return {"user": body.username, "token": token}

    @app.post("/api/v1/logout")
    async def logout(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else ""
        return {"revoked": bool(token) and rt.accounts.revoke_token(token)}

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

    @app.get("/api/v1/threads/{thread}/steps")
    async def steps(thread: str, user: str = Depends(user_of)) -> list[dict[str, Any]]:
        kind, ctx = source()
        return kind.steps(ctx, user, thread_name(thread))

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

    # --- SSH servers: only for the users named in [ssh] admins ---

    def ssh_admin(user: str) -> None:
        if user not in rt.settings.ssh.admins:
            raise HTTPException(403, "只有管理员能管理 SSH 服务器（[ssh] admins）")

    def ssh_view(host: dict[str, Any]) -> dict[str, Any]:
        return {**host, "approval_text": APPROVALS.get(host.get("approval", "ask"), "")}

    @app.get("/api/v1/ssh")
    async def ssh_list(user: str = Depends(user_of)) -> dict[str, Any]:
        admin = user in rt.settings.ssh.admins
        if not admin:
            return {"admin": False, "hosts": [], "public_key": None, "fingerprint": None}
        return {"admin": True, **rt.ssh.public_key(), "hosts": [ssh_view(h) for h in rt.ssh.list()],
                "approvals": APPROVALS, "source_address": rt.settings.ssh.source_address}

    @app.put("/api/v1/ssh/hosts/{name}")
    async def ssh_save(name: str, body: dict[str, Any], user: str = Depends(user_of)) -> dict[str, Any]:
        ssh_admin(user)
        try:
            host = Host.model_validate({**body, "name": name})
            return ssh_view(rt.ssh.put(host))
        except ValidationError as error:
            raise HTTPException(422, "；".join(e["msg"].removeprefix("Value error, ") for e in error.errors())) \
                from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.delete("/api/v1/ssh/hosts/{name}")
    async def ssh_delete(name: str, user: str = Depends(user_of)) -> dict[str, Any]:
        ssh_admin(user)
        try:
            if not rt.ssh.delete(name):
                raise HTTPException(404, "没有这台服务器")
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"deleted": name}

    @app.post("/api/v1/ssh/hosts/{name}/test")
    async def ssh_test(name: str, user: str = Depends(user_of)) -> dict[str, Any]:
        ssh_admin(user)
        try:
            return ssh_view(await rt.ssh.test(name))
        except KeyError:
            raise HTTPException(404, "没有这台服务器") from None

    @app.post("/api/v1/approvals/{ticket_id}/{key}")
    async def approve(ticket_id: str, key: str, body: ApprovalRequest, user: str = Depends(user_of)) \
            -> dict[str, Any]:
        """A person's answer to a command a worker wants to run on one of their servers."""
        ticket_of(user, ticket_id)
        ssh_admin(user)
        if not rt.ssh.approvals.decide(ticket_id, key, body.approve):
            raise HTTPException(404, "这条命令已经不在等批准了")
        return {"decided": "approved" if body.approve else "refused"}

    @app.websocket("/api/v1/stream")
    async def stream(socket: WebSocket, token: str | None = None) -> None:
        user = rt.accounts.verify(token)
        if user is None:
            # Accepted first, so the client sees why: 4401, the WebSocket form of 401.
            await socket.accept()
            await socket.close(code=4401, reason="a valid token is required")
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
