"""The Runtime: the composition root. It builds the parts, starts them and stops them, and holds no domain rules.

One process owns a home directory at a time (a lock file). A pass, every `tick_seconds` or as soon as something is
queued, runs the Engine's clock (`evaluate_due`) and then the due effects.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from continuum.core.engine import Engine
from continuum.models import ChatModel
from continuum.core.model import Trigger
from continuum.core.part import EngineOptions
from continuum.core.ports import DomainEvent, OutboundMessage, ScopeResult
from continuum.plugin import Plugin, SourceKind
from continuum.policy.config import PolicyConfig
from continuum.policy.effects import policy_effects
from continuum.policy.gate import RulePolicy

from .accounts import Accounts
from .crew import Crew
from .integrations import NAMES, Integrations
from .projects import Projects, project_of_thread, zone
from .ssh import SshHosts, survey
from .effects import Effects
from .executor import InProcessExecutor
from .outbox import StoreOutbox
from .registry import Registry
from .router import RouterAssessor
from .settings import Settings, load_settings
from .store import SQLiteStore

log = logging.getLogger("continuum")


class ConfigurationError(Exception):
    pass


class HomeLocked(Exception):
    pass


class SourceInstanceContext:
    """What one Source instance is given: its configuration, a directory of its own, and `receive`."""

    def __init__(self, runtime: Runtime, instance: str, kind: SourceKind, config: Any):
        self.runtime = runtime
        self.instance = instance
        self.kind = kind
        self.config = config
        self.data_dir = runtime.home / "data" / instance
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def receive(self, trigger: Trigger) -> str:
        result = self.runtime.engine.receive(trigger)
        self.runtime.poke()
        return result


class Runtime:
    def __init__(self, home: Path, settings: Settings | None = None, *, plugins: Iterable[Plugin] | None = None,
                 entry_points: bool = True, models: dict[str, Any] | None = None):
        from continuum.plugins import BUILTINS

        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self._lock = self._acquire_lock()
        try:
            self.settings = settings or load_settings(self.home)
            self.registry = Registry()
            self.registry.load(BUILTINS if plugins is None else plugins, entry_points, self.home / "plugins")
            self.problems: list[str] = list(self.registry.problems)
            self.store = SQLiteStore(self.home / "continuum.db")
            self.accounts = Accounts(self.store)
            self.ssh = SshHosts(self.store, self.home, self.settings.ssh)
            self.projects = Projects(self.store, self.home)
            # When each periodic check last ran (time.monotonic(), which counts from boot): None until the first,
            # so a host started minutes ago does not wait out an interval it never began.
            self._clock_at: float | None = None
            self._profiles_at: float | None = None
            self._idle_at: float | None = None
            self._surveying: set[str] = set()
            self._wake = asyncio.Event()
            self.effects = Effects(self.store, on_enqueue=self.poke)
            self.sources = self._sources()
            gate_rules = self._gate_rules()
            # `models` replaces the configured ones, such as with scripted models in tests.
            self.models = models if models is not None else {
                name: ChatModel(name, spec) for name, spec in self.settings.models.items()}
            self.policy = RulePolicy(self.settings.policy, self, gate_rules)
            router_kind = self._kind(self.registry.routers, self.settings.router.kind, "router")
            executor_kind = self._kind(self.registry.executors, self.settings.executor.kind, "executor")
            self.router = RouterAssessor(self.store, self.effects, router_kind,
                                         self._config(router_kind, self.settings.router.config, "router"),
                                         self.models, lambda: self.settings.policy)
            self.executor = InProcessExecutor(
                self.store, self.effects, self.home, executor_kind,
                self._config(executor_kind, self.settings.executor.config, "executor"), self.registry.scripts,
                self.settings.executor.max_workers, self.router.relay, paused=lambda: self.policy.paused,
                on_report=self.notify_ticket, models=self.models,
                on_activity=self.notify_activity, ssh=self.ssh)
            self.outbox = StoreOutbox(self.store, self.effects, self._send)
            self.engine = Engine(EngineOptions(
                repo=self.store, policy=lambda: self.policy, scope=self, assessor=self.router,
                executor=self.executor, outbox=self.outbox, prepare_ticket=self.executor.prepare, emit=self._emit))
            self.executor.project_for = self.project_for_ticket
            self.executor.projects = self.projects
            self.executor.context.projects = ProjectDesk(self)
            self.crew = Crew(self.settings.executor.max_workers)
            self.executor.context.crew = self.crew
            self.integrations = Integrations(self.home)
            self.executor.context.integrations = self.integrations
            self.router.projects = self.executor.context.projects
            self.router.bind(self.engine)
            self.executor.bind(self.engine)
        except BaseException:
            self._release_lock()
            raise
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._server: Any = None
        self._server_task: asyncio.Task | None = None

    # --- construction helpers ---

    def _acquire_lock(self) -> Any:
        handle = open(self.home / ".lock", "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise HomeLocked(f"another Continuum process is running in {self.home}") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle

    def _release_lock(self) -> None:
        if self._lock:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def _kind(self, table: dict[str, Any], name: str, label: str) -> Any:
        if name not in table:
            raise ConfigurationError(f"no {label} kind {name!r}; installed: {', '.join(sorted(table)) or 'none'}")
        return table[name]

    def _config(self, kind: Any, raw: dict[str, Any], label: str) -> Any:
        try:
            return kind.config_schema.model_validate(raw)
        except ValidationError as error:
            raise ConfigurationError(f"{label} {kind.kind}: {error}") from None

    def _gate_rules(self) -> list[Any]:
        missing = [name for name in self.settings.gate if name not in self.registry.gate_rules]
        if missing:
            raise ConfigurationError(f"unknown gate rules {missing}; installed: {sorted(self.registry.gate_rules)}")
        return [self.registry.gate_rules[name] for name in self.settings.gate]

    def _sources(self) -> dict[str, SourceInstanceContext]:
        """Each enabled Source instance whose kind is installed and whose configuration is valid; any other is
        recorded as a problem and stays off."""
        out: dict[str, SourceInstanceContext] = {}
        for instance, spec in self.settings.sources.items():
            if not spec.enabled:
                continue
            kind = self.registry.sources.get(spec.kind)
            if kind is None:
                self.problems.append(f"source {instance}: kind {spec.kind!r} is not installed")
                continue
            try:
                config = kind.config_schema.model_validate(spec.config)
            except ValidationError as error:
                self.problems.append(f"source {instance}: invalid configuration: {error}")
                continue
            out[instance] = SourceInstanceContext(self, instance, kind, config)
        return out

    # --- the Engine's SourceScope port ---

    def scope(self, source: str, meta: dict[str, Any]) -> ScopeResult:
        ctx = self.sources.get(source)
        if ctx is None:
            return ScopeResult(listened=False, reason="source_off")
        return ctx.kind.scope(ctx.config, meta)

    async def _send(self, message: OutboundMessage) -> Literal["sent", "gone", "waiting"]:
        if self.policy.paused:
            return "waiting"
        ctx = self.sources.get(message.channel.source)
        if ctx is None:
            spec = self.settings.sources.get(message.channel.source)
            # A Source that is configured but off keeps the message waiting; one that is gone drops it.
            return "waiting" if spec is not None else "gone"
        return await ctx.kind.send(ctx, message)

    def _emit(self, event: DomainEvent) -> None:
        if event.type in ("ticket.created", "ticket.ended", "ticket.resumed", "decision.recorded"):
            log.info("%s %s", event.type, {k: v for k, v in event.data.items() if v is not None})
        if event.type.startswith("ticket."):
            ticket_id = event.data["ticket_id"]
            self.store.after_commit(lambda: self.notify_ticket(ticket_id))

    def notify_ticket(self, ticket_id: str) -> None:
        """Tell the API streams of the people a ticket belongs to that it changed."""
        ticket = self.store.tickets.get(ticket_id)
        if ticket is None:
            return
        for ctx in self.sources.values():
            if ctx.kind.kind != "app":
                continue
            users = {c.address["user"] for c in ticket.channels if c.source == ctx.instance and "user" in c.address}
            if ticket.mandate.actor.source == ctx.instance:
                users.add(ticket.mandate.actor.id)
            for user in users:
                ctx.kind.publish(user, {"type": "ticket", "id": ticket_id})

    def notify_activity(self, ticket_id: str, event: dict[str, Any]) -> None:
        """Pass what a worker turn is doing to the conversations its ticket answers in: steps are kept there, the rest
        is only shown to whoever is watching."""
        ticket = self.store.tickets.get(ticket_id)
        if ticket is None:
            return
        for ctx in self.sources.values():
            if ctx.kind.kind != "app":
                continue
            places = {(c.address["user"], c.address["thread"]) for c in ticket.channels
                      if c.source == ctx.instance and "user" in c.address and "thread" in c.address}
            for user, thread in places:
                ctx.kind.activity(ctx, user, thread, ticket_id, event)

    # --- running ---

    def poke(self) -> None:
        self._wake.set()

    def start(self) -> None:
        """Bring the domain in line with this start's Policy, resume interrupted work, and start the Sources."""
        if self._started:
            return
        self._started = True
        self._check_policy()
        self.executor.resume()
        for instance, ctx in self.sources.items():
            self._tasks.append(asyncio.create_task(self._run_source(instance, ctx), name=f"source-{instance}"))
        for problem in self.problems:
            log.warning("problem: %s", problem)

    def _check_policy(self) -> None:
        """A Policy that changed since the last start restarts Evals in progress and tells the tickets it bears on."""
        previous = self.store.get("policy")
        current = {"version": self.policy.version, "config": self.settings.policy.model_dump(mode="json"),
                   "gate": self.settings.gate}
        def work() -> None:
            if previous and previous["version"] != self.policy.version:
                old = PolicyConfig.model_validate(previous["config"])
                # The old Policy is judged with the gate rules it ran, as far as they are still installed.
                old_rules = [self.registry.gate_rules[n] for n in previous.get("gate", []) if n in self.registry.gate_rules]
                effects = policy_effects(self.store.tickets.open(), old, self.settings.policy, lambda _: self,
                                         lambda: self.policy.rules, lambda: old_rules)
                self.engine.policy_changed(previous["version"], self.policy.version, effects)
                log.info("policy changed: %d ticket(s) told", len(effects))
            self.store.set("policy", current)
        self.store.atomic(work)

    async def _run_source(self, instance: str, ctx: SourceInstanceContext) -> None:
        delay = 5.0
        while True:
            try:
                await ctx.kind.run(ctx)
                log.warning("source %s stopped by itself", instance)
                self._problem(f"source {instance}: stopped")
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a Source that fails is started again
                log.exception("source %s failed; restarting in %.0fs", instance, delay)
                self._problem(f"source {instance}: {type(error).__name__}: {error}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300.0)

    def _problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    async def pass_once(self) -> None:
        try:
            self.engine.evaluate_due()
        except Exception as error:  # noqa: BLE001 - a plugin that raises, such as a gate rule, must not stop the host
            log.exception("evaluating failed")
            self._problem(f"evaluate: {type(error).__name__}: {error}")
        await self.effects.run_due(wait=False)
        self._tick_projects()
        self._tick_profiles()
        self._tick_idle()

    # --- what runs on each server ---

    def survey_soon(self, name: str) -> None:
        """Look over a server in the background (once at a time per server)."""
        if name in self._surveying:
            return
        self._surveying.add(name)

        async def work() -> None:
            try:
                model = self.models.get(str(self.settings.router.config.get("model", "default")))
                await survey(self.ssh, name, model)
                log.info("ssh: surveyed %s", name)
            except Exception:  # noqa: BLE001 - a server that cannot be surveyed now is tried again later
                log.exception("ssh: surveying %s failed", name)
            finally:
                self._surveying.discard(name)

        task = asyncio.create_task(work(), name=f"survey-{name}")
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    IDLE_CLOSE = timedelta(hours=2)

    def _tick_idle(self) -> None:
        """A run that finished its turn without ending, waits for no one and has been quiet for a while is done:
        it is closed, so it does not show as running for days. A later message still finds it, and opens it again."""
        now = time.monotonic()
        if self._idle_at is not None and now - self._idle_at < 600:
            return
        self._idle_at = now
        quiet = datetime.now(UTC) - self.IDLE_CLOSE
        for ticket in self.store.tickets.open():
            session = self.executor.sessions.get(ticket.id)
            if ticket.attention is not None or (session is not None and session.busy) or ticket.updated_at > quiet:
                continue
            # One about to start a worker is not idle; one whose assignment the engine already wound up is.
            if not ticket.assignments or any(a.state == "pending" for a in ticket.assignments):
                continue
            try:
                self.engine.stop_ticket(ticket.id, "做完一轮后空闲超过 2 小时，自动归档")
                log.info("closed idle ticket %s", ticket.id[:8])
            except Exception:  # noqa: BLE001 - one that cannot be closed now is tried again later
                log.exception("closing idle ticket %s failed", ticket.id[:8])

    def _tick_profiles(self) -> None:
        """Once an hour, look again at reachable servers whose profile is more than a day old (a few at a time)."""
        now = time.monotonic()
        if self._profiles_at is not None and now - self._profiles_at < 3600:
            return
        self._profiles_at = now
        stale = datetime.now(UTC) - timedelta(days=1)
        due = [h["name"] for h in self.ssh.list() if (h.get("state") or {}).get("ok")
               and datetime.fromisoformat((h.get("profile") or {}).get("updated_at") or "2000-01-01T00:00:00+00:00")
               < stale]
        for name in due[:4]:
            self.survey_soon(name)

    # --- projects ---

    def timezone_of(self, user: str) -> str:
        return self.store.get(f"timezone:{user}") or "UTC"

    def app_source(self) -> Any:
        return next((c for c in self.sources.values() if c.kind.kind == "app"), None)

    def _tick_projects(self) -> None:
        """What the projects' schedules make due: a reminder in the person's conversation, or a run of the project."""
        now = time.monotonic()
        if self._clock_at is not None and now - self._clock_at < 5:
            return
        self._clock_at = now
        ctx = self.app_source()
        if ctx is None:
            return
        try:
            due = self.projects.due(datetime.now(UTC))
        except Exception as error:  # noqa: BLE001 - a bad project must not stop the host
            log.exception("projects: schedules failed")
            self._problem(f"projects: {type(error).__name__}: {error}")
            return
        for project, schedule in due:
            meta = {"project": project.id, "project_name": project.name, "schedule": schedule.id}
            if schedule.action == "remind":
                ctx.kind.say(ctx, project.owner, "main", "reminder", schedule.text, meta)
            else:
                ctx.kind.post(ctx, project.owner, project.thread, f"【定时任务】{schedule.text}", role="scheduled",
                              meta=meta)
            log.info("project %s: %s %s", project.id, schedule.action, schedule.id)

    def announce_decision(self, project: Any) -> None:
        """Show the person's answer on the proposal in their conversation, and say what happens next."""
        ctx = self.app_source()
        if ctx is None:
            return
        for message in ctx.kind.everything(ctx, project.owner, limit=2000):
            meta = message.get("meta") or {}
            if message["role"] == "proposal" and meta.get("project") == project.id:
                ctx.kind.update_meta(ctx, message["id"], {**meta, "state": project.state})
        if project.state == "active":
            nexts = [s.next_at for s in project.schedules if s.next_at]
            when = f"，下一次在 {min(nexts).astimezone(zone(project.timezone)):%m-%d %H:%M}" if nexts else ""
            ctx.kind.say(ctx, project.owner, "main", "notice", f"项目「{project.name}」开始了{when}。",
                         {"project": project.id, "project_name": project.name})

    def project_for_ticket(self, ticket: Any) -> Any:
        """The long-running project a run belongs to, if any: the project whose thread the run answers on."""
        for channel in ticket.channels:
            project_id = project_of_thread(channel.address.get("thread"))
            if project_id:
                return self.projects.get(project_id)
        origin = self.store.triggers.get(ticket.origin.trigger_id)
        project_id = project_of_thread(origin.channel.address.get("thread")) if origin and origin.channel else None
        return self.projects.get(project_id) if project_id else None

    async def serve_api(self) -> int:
        """Start the HTTP API on the configured address; returns the port it listens on."""
        import uvicorn

        from .api import create_app

        class Server(uvicorn.Server):
            def capture_signals(self):  # the host handles signals itself
                import contextlib
                return contextlib.nullcontext()

        config = uvicorn.Config(create_app(self), host=self.settings.api.host, port=self.settings.api.port,
                                log_level="warning", lifespan="off", ws_ping_interval=None)
        self._server = Server(config)
        self._server_task = asyncio.create_task(self._server.serve(), name="api")
        while not self._server.started:
            if self._server_task.done():
                self._server_task.result()
                raise RuntimeError("the API server stopped while starting")
            await asyncio.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        log.info("API listening on %s:%d", self.settings.api.host, port)
        return port

    async def run(self, stop: asyncio.Event | None = None) -> None:
        self.start()
        if self.settings.api.enabled and self._server is None:
            await self.serve_api()
        stop = stop or asyncio.Event()
        watcher = asyncio.create_task(stop.wait())
        watcher.add_done_callback(lambda _: self.poke())
        try:
            while not stop.is_set():
                self._wake.clear()
                await self.pass_once()
                timeout = self.settings.tick_seconds
                due = self.effects.next_due()
                if due is not None:
                    timeout = max(0.0, min(timeout, due - self.effects.clock()))
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except TimeoutError:
                    pass
        finally:
            watcher.cancel()

    async def close(self) -> None:
        if self._server is not None and self._server_task is not None:
            for ctx in self.sources.values():
                closer = getattr(ctx.kind, "close_streams", None)
                if closer:
                    closer()
            self._server.should_exit = True
            await asyncio.gather(self._server_task, return_exceptions=True)
        for ctx in self.sources.values():
            closer = getattr(ctx.kind, "close", None)
            if closer:
                closer()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.effects.shutdown()
        for model in self.models.values():
            await model.close()
        await self.executor.shutdown()
        self.store.close()
        self._release_lock()


class ProjectDesk:
    """What a worker may do with the person's projects: propose one, which the person then confirms or refuses."""

    def __init__(self, rt: Runtime):
        self.rt = rt

    def timezone_of(self, user: str) -> str:
        return self.rt.timezone_of(user)

    # --- for a worker: asking the person to connect a tool ---

    def ask_for_tool(self, job: Any, kind: str, reason: str) -> None:
        """A card in the person's conversation where they give a tool's credentials, straight to the server."""
        owner = getattr(job, "requester", None)
        ctx = self.rt.app_source()
        if not owner or ctx is None:
            raise ValueError("只有通过对话提的请求才能请人连接工具")
        ctx.kind.say(ctx, owner, "main", "credential", f"需要你连接 {NAMES.get(kind, kind)}",
                     {"kind": kind, "reason": reason[:300], "state": "waiting"}, ticket_id=job.ticket_id)

    def tool_connected(self, user: str, kind: str, account: str | None) -> None:
        """Cards that asked for this tool say it is done."""
        ctx = self.rt.app_source()
        if ctx is None:
            return
        for message in ctx.kind.everything(ctx, user, limit=500):
            meta = message.get("meta") or {}
            if message["role"] == "credential" and meta.get("kind") == kind and meta.get("state") == "waiting":
                ctx.kind.update_meta(ctx, message["id"], {**meta, "state": "connected", "account": account})

    # --- for the router: a message may continue a project instead of starting one ---

    def open_for(self, user: str) -> list[dict[str, Any]]:
        """The person's projects a new message might continue: the long-running ones, and the one-off ones that are
        open or ended in the last day (newest first, a handful)."""
        ctx = self.rt.app_source()
        if ctx is None:
            return []
        out: list[dict[str, Any]] = []
        for project in self.rt.projects.list(user):
            if project.state in ("active", "paused"):
                out.append({"id": project.id, "name": project.name, "kind": "长期项目",
                            "state": "进行中" if project.state == "active" else "已暂停"})
        recent = datetime.now(UTC) - timedelta(days=1)
        once = []
        for ticket in self.rt.store.tickets.list():
            if ticket.mandate.actor.id != user or ticket.mandate.actor.source != ctx.instance:
                continue
            if self.rt.project_for_ticket(ticket) is not None:
                continue
            if ticket.ended is not None and ticket.updated_at < recent:
                continue
            once.append(ticket)
        once.sort(key=lambda t: t.updated_at, reverse=True)
        for ticket in once[:8]:
            out.append({"id": ticket.id, "name": ticket.title, "kind": "一次性",
                        "state": "已结束" if ticket.ended else "进行中"})
        return out

    def recent_for(self, user: str, before: int, count: int = 8) -> list[dict[str, Any]]:
        """The last few messages of the person's conversation before `before`, each with the project it belongs to."""
        ctx = self.rt.app_source()
        if ctx is None:
            return []
        by_ticket = {}
        for ticket in self.rt.store.tickets.list():
            if ticket.mandate.actor.id == user:
                project = self.rt.project_for_ticket(ticket)
                by_ticket[ticket.id] = project.id if project else ticket.id
        rows = [m for m in ctx.kind.everything(ctx, user, limit=60) if not before or m["id"] < before]
        out = []
        for m in rows[-count:]:
            meta = m.get("meta") or {}
            project = meta.get("project") or project_of_thread(m.get("thread")) or by_ticket.get(m.get("ticket_id") or "")
            who = {"user": "人", "reply": "助理", "acknowledgement": "助理"}.get(m["role"], m["role"])
            text = " ".join(m["text"].split())
            out.append({"project": project, "who": who, "text": text if len(text) <= 120 else text[:120] + "…"})
        return out

    def forward(self, trigger: Any, project_id: str) -> str | None:
        """Take a person's message to the project it continues; the project's name, or None if it cannot go there."""
        ctx = self.rt.app_source()
        address = trigger.channel.address if trigger.channel else {}
        user, row = address.get("user"), address.get("message")
        if ctx is None or not user or not row or trigger.source != ctx.instance:
            return None
        project = self.rt.projects.get(project_id)
        if project is not None:
            if project.owner != user or project.state not in ("active", "paused"):
                return None
            # A new run of the project: a Thread of its own on the project's conversation.
            ok = ctx.kind.forward(ctx, user, int(row), f"{user}/{project.thread}/{row}", project.thread,
                                  {"project": project.id, "project_name": project.name})
            return project.name if ok else None
        ticket = self.rt.store.tickets.get(project_id)
        if ticket is None or ticket.mandate.actor.id != user:
            return None
        origin = self.rt.store.triggers.get(ticket.origin.trigger_id)
        if origin is None or not origin.threads:
            return None
        # The one-off project's own Thread: its ticket takes the message up (and opens again if it had ended).
        display = (origin.channel.address.get("thread") if origin.channel else None) or "main"
        ok = ctx.kind.forward(ctx, user, int(row), origin.threads[0].id, display,
                              {"project": ticket.id, "project_name": ticket.title})
        return ticket.title if ok else None

    def propose_from(self, job: Any, args: dict[str, Any]) -> Any:
        owner = getattr(job, "requester", None)
        ctx = self.rt.app_source()
        if not owner or ctx is None:
            raise ValueError("只有通过对话提的请求才能建项目")
        timezone = self.rt.timezone_of(owner)
        try:
            project = self.rt.projects.propose(
                owner, str(args.get("name", ""))[:80], str(args.get("goal", "")), str(args.get("charter", "")),
                list(args.get("schedules") or []), timezone, proposed_by=job.ticket_id)
        except ValidationError as error:
            raise ValueError("；".join(e["msg"].removeprefix("Value error, ") for e in error.errors())) from None
        if args.get("ends_at"):
            try:
                end = datetime.fromisoformat(str(args["ends_at"])).replace(tzinfo=zone(timezone))
                project.ends_at = end.astimezone(UTC)
                self.rt.projects._save(project)
            except ValueError:
                pass
        ctx.kind.say(ctx, owner, "main", "proposal", f"提议一个长期项目：{project.name}",
                     {"project": project.id, "project_name": project.name, "state": "proposed"},
                     ticket_id=job.ticket_id)
        return project
