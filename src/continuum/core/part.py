"""Shared base and configuration of the Engine's parts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .errors import UserError
from .model import Handback, Thread, ThreadRef, Ticket, Trigger, TriggerDisposition, followup_owners, reached, thread_key
from .ports import Accept, Assessor, DomainEvent, Executor, Outbox, Policy, Repositories, SourceScope


@dataclass
class EngineOptions:
    repo: Repositories
    policy: Callable[[], Policy]
    # Whether a Source listens to a Trigger; one it does not is stored with a `filtered` disposition.
    scope: SourceScope
    assessor: Assessor
    executor: Executor
    outbox: Outbox
    prepare_ticket: Callable[[Ticket, Accept], Ticket] | None = None
    now: Callable[[], datetime] | None = None
    emit: Callable[[DomainEvent], None] | None = None


class EnginePart:
    """Base of Intake, Evals, Tickets, Assignments and Replies: shared reads, the clock and events. Each part changes its
    own aggregates and asks a sibling for the others, inside the transaction the Engine opened."""

    def __init__(self, options: EngineOptions):
        self.options = options
        self.repo = options.repo

    def now(self) -> datetime:
        return self.options.now() if self.options.now else datetime.now(UTC)

    def policy(self) -> Policy:
        return self.options.policy()

    def owners(self, trigger: Trigger) -> list[str]:
        """The tickets whose worker decides a person's follow-up: see `followup_owners`."""
        return followup_owners(trigger, self.repo)

    def left_to_workers(self, trigger: Trigger) -> None:
        """Refuse a router decision on a follow-up a ticket's worker still decides."""
        owners = self.owners(trigger)
        if owners:
            raise UserError("workerDecides", {"tickets": ", ".join(owners)})

    def disposition(self, trigger_id: str) -> TriggerDisposition:
        return self.repo.triggers.disposition(trigger_id) or TriggerDisposition(at=self.now())

    def hand_back(self, ticket_id: str, trigger: Trigger, reason: str) -> bool:
        """Record that a ticket's worker hands back a Trigger, and queue it for Eval when the ticket decided it and no
        other ticket still does. Returns whether it was queued."""
        decided = ticket_id in self.owners(trigger)
        disposition = self.disposition(trigger.id)
        disposition.handbacks.append(Handback(ticket_id=ticket_id, reason=reason, at=self.now()))
        self.repo.triggers.save_disposition(trigger.id, disposition)
        return decided and not self.owners(trigger) and self.queue_for_eval(trigger)

    def take_up(self, ticket_id: str, trigger_id: str) -> None:
        """Record that a ticket's worker took a person's message up, by acknowledging or answering it."""
        disposition = self.repo.triggers.disposition(trigger_id)
        if disposition and ticket_id not in disposition.acked:
            disposition.acked.append(ticket_id)
            self.repo.triggers.save_disposition(trigger_id, disposition)

    def queue_for_eval(self, trigger: Trigger) -> bool:
        """Queue a Trigger on its primary Thread for the next Eval, unless it waits there already or an Eval holds it."""
        primary = self.thread(trigger.threads[0])
        if trigger.id in primary.pending or (primary.evaluating and trigger.id in primary.evaluating.trigger_ids):
            return False
        primary.pending.append(trigger.id)
        self.repo.threads.save(primary)
        return True

    def emit(self, type_: str, **data: Any) -> None:
        if self.options.emit:
            self.options.emit(DomainEvent(type=type_, at=self.now(), data=data))

    def require(self, ticket_id: str) -> Ticket:
        ticket = self.repo.tickets.get(ticket_id)
        if ticket is None:
            raise UserError("unknownTicket")
        return ticket

    def thread(self, ref: ThreadRef) -> Thread:
        """The stored Thread, or an unsaved empty one."""
        k = thread_key(ref)
        return self.repo.threads.get(k) or Thread(key=k, ref=ref, created_at=self.now())

    def tickets_on_thread(self, thread: Thread) -> list[Ticket]:
        return [t for i in thread.subscriptions if (t := self.repo.tickets.get(i)) is not None]

    def source(self, ticket: Ticket) -> Trigger | None:
        return self.repo.triggers.get(ticket.origin.trigger_id)

    def reaches(self, ticket: Ticket, trigger: Trigger, context: bool = True) -> bool:
        return reached(ticket, trigger, self.repo.threads.get, context)
