"""Domain model: the concepts and their invariants, independent of any Source, Policy or Executor.

Ported from dsh-superagent's `src/core/model.ts`. Times are timezone-aware datetimes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .reasons import Reason

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Strength = Literal["explicit", "inferred"]
AcceptMode = Literal["off", "observe", "auto"]
Addressed = Literal["mention", "direct"]


class ActorRef(Frozen):
    """An identity in its Source's own vocabulary; identities are never mapped across Sources."""

    source: str
    id: str


class ThreadRef(Frozen):
    source: str
    id: str


class ChannelRef(Frozen):
    """The write side of a Thread; the address format belongs to the Source."""

    source: str
    thread: str
    address: dict[str, str] = {}


def thread_key(ref: ThreadRef) -> str:
    return f"{ref.source}:{ref.id}"


class Trigger(Frozen):
    """One immutable event from a Source. `text` and `meta` are untrusted external data."""

    id: str
    source: str
    # Every Thread it belongs to; the first is the primary Thread that hosts its Eval.
    threads: tuple[ThreadRef, ...] = Field(min_length=1)
    actor: ActorRef
    # Source-declared: a person wrote it, rather than a bot, CI or this agent.
    human: bool
    # Source-declared: the sender addressed the agent.
    addressed: Addressed | None = None
    at: datetime
    text: str = ""
    channel: ChannelRef | None = None
    url: str | None = None
    meta: dict[str, Any] = {}


class Handback(Model):
    ticket_id: str
    reason: str
    at: datetime


class TriggerDisposition(Model):
    """Where a Trigger went after intake. It grants nothing, but decides who decides a person's follow-up."""

    filtered: Reason | None = None
    eval_id: str | None = None
    decision_id: str | None = None
    # The tickets it reached, by fan-out or attachment.
    fanout: list[str] = []
    # Those of `fanout` it reached only on a Thread they follow for context.
    context: list[str] = []
    # A person, or the router for a message no ticket decides, sent it to Eval again.
    requeued: bool = False
    handbacks: list[Handback] = []
    # Tickets whose worker acknowledged or answered it.
    acked: list[str] = []
    at: datetime


class MandateBasis(Frozen):
    """How the gate admitted the Trigger a Mandate came from, which `revalidate` checks again.

    `rule` names the gate rule that admitted it and later revalidates it; `via` is that rule's own word for how.
    The standard gate uses `addressed`, `followup`, `admission` and `bot`.
    """

    rule: str = "standard"
    via: str
    source: str | None = None
    scope: dict[str, Any] | None = None
    row: str | None = None
    sender: str | None = None
    rules: tuple[str, ...] = ()
    addressed: Addressed | None = None
    cell: str | None = None
    # Set when it made an inferred candidate under an accept mode.
    observation: AcceptMode | None = None
    extra: dict[str, Any] = {}


class Mandate(Frozen):
    """Immutable authorization produced by an accept; execution is bounded by it."""

    strength: Strength
    policy_version: str
    basis: MandateBasis
    actor: ActorRef
    scope: str


class Evaluation(Model):
    """The Eval in progress on a Thread; at most one per Thread."""

    id: str
    thread_key: str
    trigger_ids: list[str]
    primary: str | None = None
    strength: Strength
    actor: ActorRef
    basis: MandateBasis
    policy_version: str
    started_at: datetime
    delivered: datetime | None = None
    deadline: datetime
    paused_at: datetime | None = None


class Thread(Model):
    """Thread aggregate: the consistency boundary of Eval, holder of Subscriptions and Decision history."""

    key: str
    ref: ThreadRef
    created_at: datetime
    last_at: datetime | None = None
    subscriptions: list[str] = []
    # Subscribed tickets that follow the Thread only for read-only context.
    context: list[str] = []
    correlated: list[str] = []
    # Triggers waiting for the next Eval.
    pending: list[str] = []
    evaluating: Evaluation | None = None
    # How many Evals of its queued Triggers expired in a row.
    expired: int = 0
    meta: dict[str, Any] = {}


DecisionKind = Literal["ignore", "accept", "stop", "unavailable"]


class Decision(Model):
    id: str
    eval_id: str
    thread_key: str
    trigger_ids: list[str]
    policy_version: str
    at: datetime
    kind: DecisionKind
    reason: list[Reason]
    strength: Strength
    ticket_id: str | None = None
    mandate: Mandate | None = None
    audit: dict[str, Any] | None = None
    dismissed: bool = False


AssignmentState = Literal["pending", "running", "ended"]
ReportState = Literal["progress", "waiting", "ready", "blocked", "failed", "dropped"]


class AssignmentReport(Model):
    state: ReportState
    summary: str
    at: datetime


class Assignment(Model):
    """The binding of a ticket to one Executor instance."""

    id: str
    state: AssignmentState = "pending"
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    ended_reason: str | None = None
    report: AssignmentReport | None = None
    # When Inputs last reached the worker.
    delivered_at: datetime | None = None
    # When a turn of its worker last ended with nothing left to run.
    idle_at: datetime | None = None


class RuleEffect(Model):
    id: str
    name: str
    change: Literal["changed", "disabled"]
    instruction: str | None = None


class PolicyEffect(Model):
    """How a Policy change bears on a ticket."""

    rules: list[RuleEffect] = []
    proactive: bool | None = None
    requester_allowed: bool | None = None
    requester: ActorRef | None = None

    def empty(self) -> bool:
        return not self.rules and self.proactive is None and self.requester_allowed is None


class TriggerInput(Model):
    """A Trigger on one of the ticket's Threads, or attached to it."""

    kind: Literal["trigger"] = "trigger"
    id: str
    trigger_id: str
    at: datetime
    note: str | None = None
    human: bool = False
    # It reached the ticket only on a Thread followed for context.
    context: bool = False
    # The Assignment it was last delivered to, and when that worker's Session took it into a step.
    delivered_to: str | None = None
    read_at: datetime | None = None


class PolicyChangedInput(Model):
    kind: Literal["policy-changed"] = "policy-changed"
    id: str
    from_version: str
    to_version: str
    at: datetime
    effect: PolicyEffect


class ResumedInput(Model):
    """A person's message on one of its Threads resumed the ended ticket under its unchanged Mandate."""

    kind: Literal["resumed"] = "resumed"
    id: str
    trigger_id: str
    at: datetime


class TextInput(Model):
    kind: Literal["text"] = "text"
    id: str
    text: str
    at: datetime


class NoteInput(Model):
    """A fact the router gave the ticket from other work: context, which asks for nothing and grants nothing."""

    kind: Literal["note"] = "note"
    id: str
    text: str
    at: datetime


TicketInput = Annotated[
    TriggerInput | PolicyChangedInput | ResumedInput | TextInput | NoteInput, Field(discriminator="kind")
]

TicketEndBy = Literal["executor", "human"]
# completed, dropped and stopped are the core's; a Source's Thread close adds its own, such as merged or closed.
TicketOutcome = str


class TicketEnd(Model):
    at: datetime
    by: TicketEndBy
    outcome: TicketOutcome
    reason: str | None = None


class Attention(Model):
    """A question for a person; `trigger_id` is the person's message that raised it."""

    id: str
    question: str
    trigger_id: str | None = None


class FollowClose(Model):
    at: datetime
    outcome: str


class Follow(Model):
    """A Thread a Source made the ticket follow, such as its pull request's; `own` when the ticket opened it."""

    thread: str
    own: bool
    closed: FollowClose | None = None


class Execution(Model):
    name: str
    directory: str
    assignment: str


class Origin(Model):
    trigger_id: str
    trigger_ids: list[str]


class DeliveryFailure(Model):
    error: str
    at: datetime


class Ticket(Model):
    """Ticket aggregate: a unit of authorized work."""

    id: str
    title: str
    decision_id: str
    mandate: Mandate
    channels: list[ChannelRef] = []
    ended: TicketEnd | None = None
    attention: Attention | None = None
    assignments: list[Assignment] = []
    inputs: list[TicketInput] = []
    undelivered: list[str] = []
    follows: list[Follow] = []
    # People's questions the worker answered, whose answers the router may send.
    answers: list[str] = []
    created_at: datetime
    updated_at: datetime
    origin: Origin
    execution: Execution | None = None
    delivery: DeliveryFailure | None = None

    def input(self, input_id: str) -> TicketInput | None:
        return next((item for item in self.inputs if item.id == input_id), None)


# --- predicates ---------------------------------------------------------------------------------------------------


def live_ticket(ticket: Ticket) -> bool:
    return ticket.ended is None


def ticket_outcome(ticket: Ticket) -> str | None:
    return ticket.ended.outcome if ticket.ended else None


def running_assignment(ticket: Ticket) -> Assignment | None:
    return next((a for a in ticket.assignments if a.state == "running"), None)


def latest_assignment(ticket: Ticket) -> Assignment | None:
    return ticket.assignments[-1] if ticket.assignments else None


def latest_report(ticket: Ticket) -> AssignmentReport | None:
    for assignment in reversed(ticket.assignments):
        if assignment.report:
            return assignment.report
    return None


def read_until(ticket: Ticket, progress: bool = True) -> datetime:
    """Until when the worker had a turn to read what reached it: its latest report (but not a progress one when
    `progress` is false) or the end of its latest turn, whichever is later."""
    report = None
    for assignment in reversed(ticket.assignments):
        if assignment.report and (progress or assignment.report.state != "progress"):
            report = assignment.report
            break
    times = [report.at if report else EPOCH] + [a.idle_at or EPOCH for a in ticket.assignments]
    return max(times)


def waits_for_person(ticket: Ticket) -> bool:
    """Its latest Assignment reported blocked or failed, and no person's message reached it since."""
    latest = latest_assignment(ticket)
    report = latest.report if latest else None
    if not report or report.state not in ("blocked", "failed"):
        return False
    return not any(
        isinstance(item, TriggerInput) and item.human and not item.context and item.at > report.at
        for item in ticket.inputs
    )


class _Getter[T](Protocol):
    def get(self, key: str, /) -> T | None: ...


class FollowupRecords(Protocol):
    @property
    def tickets(self) -> _Getter[Ticket]: ...

    @property
    def threads(self) -> _Getter[Thread]: ...

    @property
    def triggers(self) -> Any: ...


def followup_owners(trigger: Trigger, records: FollowupRecords) -> list[str]:
    """The tickets whose worker decides a person's follow-up: those it reached on their own Threads that can still
    decide it and did not hand it back. While a ticket owns it, the message goes to no Eval, and the router may only
    stop the work on it."""
    if not trigger.human:
        return []
    disposition: TriggerDisposition | None = records.triggers.disposition(trigger.id)
    if disposition is None or disposition.requeued:
        return []
    returned = {item.ticket_id for item in disposition.handbacks}
    reached_ids = [i for i in disposition.fanout if i not in returned and i not in disposition.context]
    if not reached_ids:
        return []
    primary = records.threads.get(thread_key(trigger.threads[0]))
    if primary and primary.evaluating and trigger.id in primary.evaluating.trigger_ids:
        return []
    owners = []
    for ticket_id in reached_ids:
        ticket = records.tickets.get(ticket_id)
        if ticket is None:
            continue
        waiting = any(
            isinstance(item, TriggerInput) and item.trigger_id == trigger.id and item.id in ticket.undelivered
            for item in ticket.inputs
        )
        if live_ticket(ticket) or (ticket.ended and ticket.ended.by != "human" and waiting):
            owners.append(ticket_id)
    return owners


def settled(decision: Decision) -> bool:
    """Whether a Decision settled what a person's message asks for."""
    if decision.kind in ("stop", "unavailable"):
        return False
    return not any(r.code in ("stopNotExplicit", "stopUnknown", "routed") for r in decision.reason)


def reached(ticket: Ticket, trigger: Trigger, thread: Callable[[str], Thread | None], context: bool = True) -> bool:
    """Whether a Trigger reached a ticket: it started the ticket, came as an Input, or is on a subscribed Thread.
    With `context` false, not on a Thread the ticket follows only for context."""
    if ticket.origin.trigger_id == trigger.id:
        return True
    as_input = any(isinstance(i, TriggerInput) and i.trigger_id == trigger.id for i in ticket.inputs)
    for ref in trigger.threads:
        current = thread(thread_key(ref))
        if current and ticket.id in current.subscriptions and (context or ticket.id not in current.context):
            return True
    if not as_input:
        return False
    if context:
        return True
    return not all(
        (t := thread(thread_key(ref))) is not None and ticket.id in t.context for ref in trigger.threads
    )
