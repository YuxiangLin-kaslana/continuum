"""The router adapter: the core's `Assessor` port, backed by a RouterKind plugin.

`request` queues an `assessment` effect; its handler tells the Engine the candidate was delivered (its deadline counts
from then), asks the router, and records the outcome through `Engine.decide`. A router that returns nothing lets the
Eval expire, which queues it again. Worker results and questions reach the router through `relay` effects.
"""

from __future__ import annotations

import logging
from typing import Any

from continuum.core.engine import Engine
from continuum.core.errors import UserError
from continuum.core.ports import Candidate
from continuum.plugin import Relay, RouterContext, RouterKind

from .effects import Effects
from .store import SQLiteStore

log = logging.getLogger("continuum.router")


class RouterAssessor:
    def __init__(self, store: SQLiteStore, effects: Effects, kind: RouterKind, config: Any):
        self.store = store
        self.effects = effects
        self.kind = kind
        self.config = config
        self._engine: Engine | None = None
        effects.handle("assessment", self._assess, base=2.0, cap=60.0)
        effects.handle("relay", self._relay, base=2.0, cap=60.0)

    def bind(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def context(self) -> RouterContext:
        assert self._engine is not None
        return RouterContext(engine=self._engine, config=self.config)

    # --- the port ---

    def request(self, candidate: Candidate) -> None:
        self.effects.enqueue("assessment", candidate.eval_id,
                             {"eval": candidate.eval_id, "trigger": candidate.triggers[0].id})

    def release(self, eval_id: str) -> None:
        self.effects.cancel("assessment", eval_id)

    # --- relays from the Executor ---

    def relay(self, relay: Relay) -> None:
        self.effects.enqueue("relay", f"{relay.ticket_id}:{relay.state}:{relay.summary}:{relay.question}",
                             relay.model_dump())

    # --- effect handlers ---

    async def _assess(self, payload: dict[str, Any]) -> None:
        ctx = self.context
        candidate = ctx.engine.candidate(payload["trigger"])
        if candidate is None or candidate.eval_id != payload["eval"]:
            return
        ctx.engine.candidate_delivered(candidate.eval_id)
        outcome = await self.kind.assess(ctx, candidate)
        if outcome is None:
            return
        try:
            decision = ctx.engine.decide(payload["trigger"], outcome.model_copy(update={"eval_id": candidate.eval_id}))
        except UserError as error:
            # A refused outcome leaves the Eval to expire and be judged again.
            log.warning("router outcome refused for %s: %s", payload["trigger"], error)
            return
        log.info("decided %s: %s", payload["trigger"], decision.kind)

    async def _relay(self, payload: dict[str, Any]) -> None:
        relay = Relay.model_validate(payload)
        ticket = self.store.tickets.get(relay.ticket_id)
        if ticket is None:
            return
        await self.kind.on_report(self.context, ticket, relay)
