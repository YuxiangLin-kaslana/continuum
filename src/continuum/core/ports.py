"""Ports the core depends on. Sources, policies, assessors, executors and channels are plugins."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .model import ChannelRef, Decision, Frozen, MandateBasis, Mandate, Model, Strength, Thread, Ticket, Trigger, \
    TriggerDisposition
from .reasons import Reason

# --- Policy and Source scope ---------------------------------------------------------------------------------------


class Admit(Frozen):
    admissible: Literal[True] = True
    strength: Strength
    basis: MandateBasis


class Refuse(Frozen):
    admissible: Literal[False] = False
    reason: str


GateResult = Admit | Refuse


class ScopeResult(Frozen):
    """Whether a Source receives a Trigger; `observed` when it also receives messages not addressed to the agent,
    and `row` the receiving scope the admission table names."""

    listened: bool
    observed: bool = False
    row: str | None = None
    reason: str | None = None


class SourceScope(Protocol):
    def scope(self, source: str, meta: dict[str, Any]) -> ScopeResult: ...


class Policy(Protocol):
    """Versioned rules; `gate` decides who may originate work."""

    @property
    def version(self) -> str: ...

    @property
    def paused(self) -> bool: ...

    @property
    def eval_timeout(self) -> timedelta: ...

    def gate(self, trigger: Trigger, thread: Thread, tickets: Sequence[Ticket]) -> GateResult: ...

    def revalidate(self, mandate: Mandate) -> bool:
        """Check a Mandate's gate basis again under this Policy, such as before an ended ticket resumes."""
        ...

    def admits_inferred(self, rules: Sequence[str]) -> bool:
        """Whether an inferred candidate may be accepted on the rules its assessment matched."""
        ...


# --- Assessor -----------------------------------------------------------------------------------------------------


class Candidate(Model):
    """What the assessor receives: bounded facts, never authority."""

    eval_id: str
    thread: Thread
    triggers: list[Trigger]
    strength: Strength
    policy_version: str
    # Every ticket on the Thread, ended ones too, for duplicate judgement.
    tickets: list[Ticket]


class Ignore(Model):
    kind: Literal["ignore"] = "ignore"
    reason: str | Reason
    audit: dict[str, Any] | None = None
    eval_id: str | None = None


class Unavailable(Model):
    kind: Literal["unavailable"] = "unavailable"
    reason: str | Reason
    audit: dict[str, Any] | None = None
    eval_id: str | None = None


class Accept(Model):
    kind: Literal["accept"] = "accept"
    title: str
    scope: str
    reason: str | Reason
    acknowledgement: str | None = None
    # The rules the assessment matched; required for an inferred candidate.
    rules: list[str] | None = None
    audit: dict[str, Any] | None = None
    # A requested short name for the ticket's workspace, and a routing note for its worker.
    name: str | None = None
    note: str | None = None
    eval_id: str | None = None


class Stop(Model):
    kind: Literal["stop"] = "stop"
    ticket_id: str
    reason: str | Reason
    audit: dict[str, Any] | None = None
    eval_id: str | None = None


Outcome = Ignore | Unavailable | Accept | Stop


class Assessor(Protocol):
    """Semantic judgement. `request` queues the assessment, idempotently per eval id; its outcome comes back
    through `Engine.decide`."""

    def request(self, candidate: Candidate) -> None: ...

    def release(self, eval_id: str) -> None:
        """Called whenever an Eval ends, decided or not."""
        ...


# --- Executor and Outbox ------------------------------------------------------------------------------------------


class Executor(Protocol):
    """Runs Assignments. The Engine calls it inside its commands, so each method only queues work, which reports
    back through the Engine."""

    def start(self, ticket: Ticket, assignment: Any) -> None: ...

    def deliver(self, ticket: Ticket, assignment: Any) -> None: ...

    def stop(self, ticket: Ticket, assignment: Any, reason: str) -> None: ...


ReplyStatus = Literal["pending", "sent", "dropped"]


class OutboundMessage(Model):
    id: str
    channel: ChannelRef
    text: str
    kind: Literal["acknowledgement", "reply", "notice"]
    ticket_id: str | None = None


class Outbox(Protocol):
    def send(self, message: OutboundMessage) -> None:
        """Queue a message idempotently; an id queued before keeps its first message."""
        ...

    def status(self, message_id: str) -> ReplyStatus | None: ...

    def withdraw(self, message_id: str) -> None: ...


# --- Domain events ------------------------------------------------------------------------------------------------

EVENT_TYPES = (
    "trigger.received", "eval.started", "decision.recorded", "ticket.created", "ticket.resumed", "ticket.ended",
    "assignment.started", "assignment.ended", "thread.subscribed", "thread.unsubscribed", "trigger.requeued",
    "policy.changed",
)


class DomainEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: str
    at: datetime
    data: dict[str, Any]


# --- Repositories -------------------------------------------------------------------------------------------------


class TriggerRepository(Protocol):
    def get(self, trigger_id: str) -> Trigger | None: ...
    def save(self, trigger: Trigger) -> None: ...
    def disposition(self, trigger_id: str) -> TriggerDisposition | None: ...
    def save_disposition(self, trigger_id: str, value: TriggerDisposition) -> None: ...


class ThreadRepository(Protocol):
    def get(self, key: str) -> Thread | None: ...
    def save(self, thread: Thread) -> None: ...
    def list(self) -> list[Thread]: ...
    def active(self) -> list[Thread]:
        """Threads with Triggers queued for Eval or an Eval in progress."""
        ...


class TicketRepository(Protocol):
    def get(self, ticket_id: str) -> Ticket | None: ...
    def save(self, ticket: Ticket) -> None: ...
    def list(self) -> list[Ticket]: ...
    def open(self) -> list[Ticket]: ...
    def active(self) -> list[Ticket]:
        """Tickets with undelivered Inputs or a pending or running latest Assignment."""
        ...


class DecisionRepository(Protocol):
    def get(self, decision_id: str) -> Decision | None: ...
    def save(self, decision: Decision) -> None: ...
    def by_thread(self, key: str) -> list[Decision]: ...


class Repositories(Protocol):
    """Durable state. Every method is synchronous so `atomic` can wrap a unit of work."""

    triggers: TriggerRepository
    threads: ThreadRepository
    tickets: TicketRepository
    decisions: DecisionRepository

    def atomic[T](self, work: Callable[[], T]) -> T: ...
