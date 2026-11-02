"""Test harness: an Engine over in-memory repositories with fake ports, a manual clock and a simulated Executor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from continuum.core import Accept, ActorRef, Admit, Candidate, ChannelRef, Engine, EngineOptions, MandateBasis, \
    MemoryRepositories, OutboundMessage, Refuse, ScopeResult, ThreadRef, Trigger, TriggerInput, running_assignment

MEMBER = ActorRef(source="chat", id="alice")
GUEST = ActorRef(source="chat", id="guest")
BASE = datetime(2026, 9, 23, tzinfo=UTC)


def trigger(id: str, thread: str = "t1", **overrides: Any) -> Trigger:
    values: dict[str, Any] = dict(
        id=id, source="chat", threads=(ThreadRef(source="chat", id=thread),), actor=MEMBER, human=True,
        at=BASE + timedelta(seconds=int(id)), text=f"message {id}",
        channel=ChannelRef(source="chat", thread=thread, address={"message": id}), meta={})
    values.update(overrides)
    return Trigger(**values)


class FakePolicy:
    """alice is explicit; anyone else is an inferred follow-up on a Thread with tickets; bots are not admitted."""

    def __init__(self) -> None:
        self.version = "v1"
        self.paused = False
        self.eval_timeout = timedelta(seconds=60)

    def gate(self, t: Trigger, thread, tickets):
        if not t.human:
            return Refuse(reason="not human")
        if t.actor.id == "alice":
            return Admit(strength="explicit", basis=MandateBasis(via="addressed"))
        if thread.subscriptions:
            return Admit(strength="inferred", basis=MandateBasis(via="followup"))
        return Refuse(reason="not a member")

    def revalidate(self, mandate) -> bool:
        return self.version != "revoked"

    def admits_inferred(self, rules) -> bool:
        return len(rules) > 0


class FakeScope:
    def scope(self, source: str, meta: dict) -> ScopeResult:
        if meta.get("blocked"):
            return ScopeResult(listened=False, reason="chat not allowed")
        return ScopeResult(listened=True, observed=bool(meta.get("observed")), row=meta.get("row"))


@dataclass
class Harness:
    policy: Any = field(default_factory=FakePolicy)
    prepare_ticket: Callable | None = None
    repo: MemoryRepositories = field(default_factory=MemoryRepositories)
    outbox: list[OutboundMessage] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    requests: list[Candidate] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    launches: list = field(default_factory=list)
    starts: int = 0
    deliveries: dict[str, None] = field(default_factory=dict)
    delivered: list[tuple[str, str]] = field(default_factory=list)
    stops: list[tuple[str, str, str]] = field(default_factory=list)
    events: list = field(default_factory=list)
    clock: int = 0
    scope: Any = field(default_factory=FakeScope)

    def __post_init__(self) -> None:
        harness = self

        class Assessor:
            def request(self, candidate): harness.requests.append(candidate)
            def release(self, eval_id): harness.released.append(eval_id)

        class Executor:
            def start(self, ticket, assignment):
                harness.starts += 1
                harness.launches.append((ticket.id, assignment.id))

            def deliver(self, ticket, assignment): harness.deliveries[ticket.id] = None
            def stop(self, ticket, assignment, reason): harness.stops.append((ticket.id, assignment.id, reason))

        class Outbox:
            def send(self, message):
                if not any(m.id == message.id for m in harness.outbox):
                    harness.outbox.append(message)

            def status(self, message_id):
                return "pending" if any(m.id == message_id for m in harness.outbox) else None

            def withdraw(self, message_id): harness.withdrawn.append(message_id)

        self.engine = Engine(EngineOptions(
            repo=self.repo, policy=lambda: self.policy, scope=self.scope, assessor=Assessor(), executor=Executor(),
            outbox=Outbox(), prepare_ticket=self.prepare_ticket, now=lambda: BASE + timedelta(seconds=self.clock),
            emit=self.events.append))

    def tick(self, seconds: int) -> None:
        self.clock += seconds

    def settle(self, launch: bool = True) -> None:
        """Simulate the Executor starting queued Assignments and delivering waiting Inputs."""
        while True:
            if launch and self.launches:
                ticket_id, assignment_id = self.launches.pop(0)
                self.engine.assignment_started(ticket_id, assignment_id)
                continue
            if not self.deliveries:
                return
            ticket_id = next(iter(self.deliveries))
            del self.deliveries[ticket_id]
            ticket = self.repo.tickets.get(ticket_id)
            running = running_assignment(ticket) if ticket else None
            if ticket is None or ticket.ended or running is None:
                continue
            for input_id in list(ticket.undelivered):
                self.delivered.append((ticket_id, input_id))
                self.engine.inputs_delivered(ticket_id, running.id, [input_id])

    def pump(self) -> None:
        self.engine.evaluate_due()
        self.settle()

    def read(self, ticket_id: str) -> None:
        """Simulate the worker's Session taking its delivered, unread Trigger Inputs into a step."""
        ticket = self.repo.tickets.get(ticket_id)
        assert ticket
        self.engine.inputs_read(ticket_id, [i.id for i in ticket.inputs if isinstance(i, TriggerInput)
                                            and i.delivered_to and i.read_at is None])

    def decide(self, candidate: Candidate, outcome):
        return self.engine.decide(candidate.triggers[0].id, outcome.model_copy(update={"eval_id": candidate.eval_id}))

    def accepted(self, id: str = "1", thread: str = "t1"):
        """Receive a request from alice and accept it."""
        self.engine.receive(trigger(id, thread=thread))
        self.pump()
        decision = self.decide(self.requests[-1], Accept(title="Fix", scope="small", reason="requested",
                                                         acknowledgement="ok"))
        self.settle()
        return decision

    def ticket(self, ticket_id: str):
        ticket = self.repo.tickets.get(ticket_id)
        assert ticket
        return ticket

    def decisions(self, thread: str = "chat:t1"):
        return sorted(self.repo.decisions.by_thread(thread), key=lambda d: d.at)


@pytest.fixture
def h() -> Harness:
    return Harness()
