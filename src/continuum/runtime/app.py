"""The Runtime: the composition root. It builds the parts, starts them and stops them, and holds no domain rules.

One process owns a home directory at a time (a lock file). A pass, every `tick_seconds` or as soon as something is
queued, runs the Engine's clock (`evaluate_due`) and then the due effects.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
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
from .ssh import SshHosts
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
