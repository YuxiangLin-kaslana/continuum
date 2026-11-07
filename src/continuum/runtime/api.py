"""The HTTP and WebSocket API, for the desktop app and other clients.

Every route but `/health` needs a bearer token (`continuum user add`); the stream takes it as `?token=`. A user sees
only their own conversations and the tickets started from them.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from continuum.core.errors import UserError
from continuum.core.model import ActorRef, Ticket
from continuum.core.stage import can_retry, ticket_stage

from .projects import project_of_thread
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


class TimezoneRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)


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
        project_id = project_of_thread(thread)
        meta = None
        if project_id:
            # Talking to a project: still the one conversation, addressed to that project.
            project = own_project(user, project_id)
            if project.state not in ("active", "paused"):
                raise HTTPException(409, "这个项目已经结束了")
            meta = {"project": project.id, "project_name": project.name}
        row = kind.post(ctx, user, thread_name(thread), body.text, meta=meta)
        return {**row, "project": project_id}

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

    # --- the one conversation, and the projects it is about ---

    def project_index(user: str) -> tuple[dict[str, Any], dict[str, str]]:
        """The person's long-running projects by id, and each ticket's project (its own id for a one-off)."""
        ongoing = {p.id: p for p in rt.projects.list(user)}
        by_ticket: dict[str, str] = {}
        for ticket in rt.store.tickets.list():
            if not owns(user, ticket):
                continue
            threads = [c.address.get("thread") for c in ticket.channels]
            project = next((project_of_thread(t) for t in threads if project_of_thread(t) in ongoing), None)
            by_ticket[ticket.id] = project or ticket.id
        return ongoing, by_ticket

    def tag(message: dict[str, Any], by_ticket: dict[str, str]) -> dict[str, Any]:
        meta = message.get("meta") or {}
        project = meta.get("project") or project_of_thread(message.get("thread")) or (
            by_ticket.get(message["ticket_id"]) if message.get("ticket_id") else None)
        return {**message, "project": project}

    @app.get("/api/v1/messages")
    async def everything(after: int = 0, user: str = Depends(user_of)) -> list[dict[str, Any]]:
        kind, ctx = source()
        _, by_ticket = project_index(user)
        return [tag(m, by_ticket) for m in kind.everything(ctx, user, after)]

    @app.get("/api/v1/search")
    async def search(q: str = Query(min_length=1, max_length=200), user: str = Depends(user_of)) \
            -> list[dict[str, Any]]:
        kind, ctx = source()
        _, by_ticket = project_index(user)
        return [tag(m, by_ticket) for m in kind.search(ctx, user, q.strip() or q)]

    @app.get("/api/v1/steps")
    async def all_steps(user: str = Depends(user_of)) -> list[dict[str, Any]]:
        kind, ctx = source()
        _, by_ticket = project_index(user)
        return [{**s, "project": by_ticket.get(s["ticket_id"], s["ticket_id"])} for s in kind.steps(ctx, user, None,
                                                                                                    limit=600)]

    @app.put("/api/v1/me/timezone")
    async def set_timezone(body: TimezoneRequest, user: str = Depends(user_of)) -> dict[str, Any]:
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "不认识这个时区") from None
        rt.store.set(f"timezone:{user}", body.timezone)
        return {"timezone": body.timezone}

    @app.get("/api/v1/projects")
    async def projects(user: str = Depends(user_of)) -> list[dict[str, Any]]:
        ongoing, by_ticket = project_index(user)
        tickets = {t.id: t for t in rt.store.tickets.list() if t.id in by_ticket}
        out = []
        for project in ongoing.values():
            if project.state == "refused":
                continue
            runs = [tickets[t] for t, p in by_ticket.items() if p == project.id and t in tickets]
            busy = sum(1 for t in runs if (s := rt.executor.sessions.get(t.id)) is not None and s.busy)
            waiting = [t for t in runs if ticket_stage(t).stage == "person"]
            nexts = [s.next_at for s in project.schedules if s.next_at]
            out.append({**rt.projects.view(project), "runs": len(runs), "open_runs": sum(1 for t in runs if t.ended is None),
                        "busy": busy, "question": waiting[0].attention.question if waiting and waiting[0].attention else None,
                        "next_at": min(nexts).isoformat() if nexts and project.state == "active" else None,
                        "last_at": max((t.updated_at for t in runs), default=project.updated_at).isoformat()})
        for ticket_id, project_id in by_ticket.items():
            if project_id != ticket_id:
                continue
            ticket = tickets[ticket_id]
            view = ticket_view(ticket)
            session = rt.executor.sessions.get(ticket.id)
            state = "ended" if ticket.ended else ("waiting" if view["stage"] == "person" else "active")
            out.append({"id": ticket.id, "kind": "once", "name": ticket.title, "goal": ticket.mandate.scope,
                        "state": state, "outcome": view["outcome"], "question": view["question"],
                        "thread": view["thread"], "runs": len(ticket.assignments), "open_runs": int(ticket.ended is None),
                        "busy": int(bool(session and session.busy)), "created_at": view["created_at"],
                        "updated_at": view["updated_at"], "last_at": view["updated_at"], "next_at": None,
                        "can_retry": view["can_retry"], "stage": view["stage"], "action": view["action"]})
        out.sort(key=lambda p: p["last_at"], reverse=True)
        return out

    def own_project(user: str, project_id: str) -> Any:
        project = rt.projects.get(project_id)
        if project is None or project.owner != user:
            raise HTTPException(404, "没有这个项目")
        return project

    @app.post("/api/v1/projects/{project_id}/{action}")
    async def project_action(project_id: str, action: str, user: str = Depends(user_of)) -> dict[str, Any]:
        own_project(user, project_id)
        try:
            if action in ("confirm", "refuse"):
                project = rt.projects.decide(project_id, action == "confirm")
                rt.announce_decision(project)
            elif action in ("pause", "resume", "end"):
                project = rt.projects.set_state(project_id, {"pause": "paused", "resume": "active", "end": "ended"}[action])
            else:
                raise HTTPException(404, "没有这个操作")
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return rt.projects.view(project)

    # --- workers: how many run, on what, and what they have used ---

    @app.get("/api/v1/workers")
    async def workers(user: str = Depends(user_of)) -> dict[str, Any]:
        kind, ctx = source()
        counts = kind.step_counts(ctx, user)
        tickets = [t for t in rt.store.tickets.list() if owns(user, t)]
        rows = []
        for ticket in tickets:
            thread = ticket_view(ticket)["thread"]
            for number, a in enumerate(ticket.assignments, 1):
                rows.append({"ticket_id": ticket.id, "title": ticket.title, "thread": thread, "assignment_id": a.id,
                             "number": number, "state": a.state,
                             "outcome": ticket.ended.outcome if ticket.ended and a is ticket.assignments[-1] else None,
                             "report": a.report.state if a.report else None,
                             "created_at": a.created_at.isoformat(),
                             "started_at": a.started_at.isoformat() if a.started_at else None,
                             "ended_at": a.ended_at.isoformat() if a.ended_at else None})
        rows.sort(key=lambda r: r["started_at"] or r["created_at"], reverse=True)
        running = []
        for row in rows:
            if row["state"] != "running":
                continue
            session = rt.executor.sessions.get(row["ticket_id"])
            busy = bool(session and session.assignment_id == row["assignment_id"] and session.busy)
            running.append({**row, "busy": busy, "steps": counts.get(row["ticket_id"], {}),
                            "current": kind.latest_step(ctx, user, row["ticket_id"]) if busy else None})
        tools: dict[str, int] = {}
        for per_ticket in counts.values():
            for name, n in per_ticket.items():
                tools[name] = tools.get(name, 0) + n
        ended = [{**r, "steps": counts.get(r["ticket_id"], {})} for r in rows if r["state"] == "ended"][:20]
        # The sub-workers the leads have sent out, with what each is on now.
        titles = {t.id: t.title for t in tickets}
        crew = [{**c, "title": titles.get(c["ticket_id"], "")} for c in rt.crew.of(set(titles))]
        for row in running:
            row["crew"] = sum(1 for c in crew if c["ticket_id"] == row["ticket_id"])
        return {
            "executor": rt.settings.executor.kind, "max_workers": rt.settings.executor.max_workers,
            "busy_now": sum(1 for r in running if r["busy"]) + len(crew), "crew": crew,
            "crew_waiting": rt.crew.waiting,
            "totals": {"assignments": len(rows), "tickets": len(tickets), "running": len(running),
                       "waiting": sum(1 for r in running if not r["busy"]), "pending": sum(
                           1 for r in rows if r["state"] == "pending")},
            "tools": tools, "running": running, "recent": ended,
        }

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
                if event.get("type") in ("stream", "thinking", "idle") and event.get("ticket_id"):
                    _, by_ticket = project_index(user)
                    event = {**event, "project": by_ticket.get(event["ticket_id"], event["ticket_id"])}
                if event.get("type") in ("message", "message-updated", "step"):
                    _, by_ticket = project_index(user)
                    if event["type"] == "step":
                        step = event["step"]
                        event = {**event, "step": {**step, "project": by_ticket.get(step["ticket_id"], step["ticket_id"])}}
                    else:
                        event = {**event, "message": tag(event["message"], by_ticket)}
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
