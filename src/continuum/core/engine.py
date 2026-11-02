"""The Engine: the single entry point of the core, and the role views its callers receive.

Every command runs in one repository transaction. Ports only queue external work inside it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .assignments import Assignments, WorkerReport
from .errors import UserError
from .evals import Evals
from .intake import Intake, ReceiveResult
from .model import Decision, PolicyEffect, ThreadRef, Ticket, Trigger, live_ticket
from .part import EngineOptions
from .ports import Candidate, Outcome
from .replies import Replies, Reply, ReplyRequest
from .tickets import Tickets


class Engine:
    def __init__(self, options: EngineOptions):
        self.options = options
        self.repo = options.repo
        self._replies = Replies(options)
        self._tickets = Tickets(options, self._replies)
        self._assignments = Assignments(options, self._tickets, self._replies)
        self._evals = Evals(options, self._tickets)
        self._intake = Intake(options, self._tickets)

    # --- Sources ---

    def receive(self, trigger: Trigger) -> ReceiveResult:
        """Store a Trigger once, fan it out to the tickets subscribed to its Threads, and queue it for Eval on its
        primary Thread unless a ticket's worker decides it."""
        return self.repo.atomic(lambda: self._intake.receive(trigger))

    def bind_thread(self, ticket_id: str, thread: ThreadRef, own: bool) -> Ticket:
        """Make an open ticket follow one of the Source's Threads, `own` when the ticket opened it."""
        return self.repo.atomic(lambda: self._tickets.bind(ticket_id, thread, own))

    def thread_closed(self, thread: ThreadRef, outcome: str, at: datetime) -> None:
        self.repo.atomic(lambda: self._tickets.thread_closed(thread, outcome, at))

    # --- the clock ---

    def evaluate_due(self) -> None:
        """Expire overdue Evals, resume ended tickets a person's message reached during a pause, and start the Evals of
        Threads whose queued Triggers wait. Each Eval and each ticket takes one transaction."""
        policy = self.options.policy()
        for thread in self.repo.threads.active():
            if thread.evaluating:
                self.repo.atomic(lambda thread=thread: self._evals.due(thread, policy))
        if policy.paused:
            return
        for ticket in self.repo.tickets.active():
            if not live_ticket(ticket) and ticket.undelivered:
                self.repo.atomic(lambda ticket=ticket: self._tickets.wake(ticket.id))
        for thread in self.repo.threads.active():
            if thread.pending and not thread.evaluating:
                self.repo.atomic(lambda thread=thread: self._evals.start(thread.key, policy))

    # --- the router ---

    def candidate(self, trigger_id: str) -> Candidate | None:
        return self._evals.candidate(trigger_id)

    def candidate_delivered(self, eval_id: str) -> None:
        self.repo.atomic(lambda: self._evals.delivered(eval_id, self.options.policy()))

    def decide(self, trigger_id: str, outcome: Outcome) -> Decision:
        return self.repo.atomic(lambda: self._evals.decide(trigger_id, outcome))

    def attach(self, ticket_id: str, trigger_id: str, note: str | None = None) -> Ticket:
        """Route a Trigger to a ticket it points at; the ticket's worker then decides it."""
        def work() -> Ticket:
            disposition = self.repo.triggers.disposition(trigger_id)
            if disposition and any(h.ticket_id == ticket_id for h in disposition.handbacks):
                raise UserError("handedBackHere")
            trigger = self._tickets.attachable(ticket_id, trigger_id)
            self._evals.withdraw(trigger)
            self._tickets.attach(ticket_id, trigger, note)
            self._evals.routed(trigger_id, ticket_id)
            self._tickets.wake(ticket_id)
            ticket = self.repo.tickets.get(ticket_id)
            assert ticket is not None
            return ticket
        return self.repo.atomic(work)

    def request_status(self, ticket_id: str) -> bool:
        return self.repo.atomic(lambda: self._tickets.request_status(ticket_id))

    def note(self, ticket_id: str, text: str) -> Ticket:
        return self.repo.atomic(lambda: self._tickets.note(ticket_id, text))

    def reply(self, request: ReplyRequest) -> Reply:
        return self.repo.atomic(lambda: self._replies.reply(request))

    # --- people ---

    def reevaluate(self, trigger_id: str) -> bool:
        return self.repo.atomic(lambda: self._evals.reevaluate(trigger_id))

    def dismiss(self, decision_id: str) -> Decision:
        return self.repo.atomic(lambda: self._evals.dismiss(decision_id))

    def unsubscribe(self, thread: str) -> bool:
        return self.repo.atomic(lambda: self._tickets.drop_subscriptions(thread, "person"))

    def stop_ticket(self, ticket_id: str, reason: str | None = None) -> Ticket:
        """A person's stop, which withdraws the Mandate."""
        return self.repo.atomic(lambda: self._tickets.stop(ticket_id, reason))

    def retry_ticket(self, ticket_id: str) -> Ticket:
        return self.repo.atomic(lambda: self._tickets.retry(ticket_id))

    # --- the Executor ---

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self._assignments.started(ticket_id, assignment_id))

    def inputs_delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        self.repo.atomic(lambda: self._assignments.delivered(ticket_id, assignment_id, input_ids))

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None:
        self.repo.atomic(lambda: self._assignments.read(ticket_id, input_ids))

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self._assignments.turn_ended(ticket_id, assignment_id))

    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None:
        self.repo.atomic(lambda: self._assignments.delivery_failed(ticket_id, assignment_id, error))

    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self._assignments.interrupted(ticket_id, assignment_id))

    def report(self, ticket_id: str, report: WorkerReport) -> Ticket:
        return self.repo.atomic(lambda: self._assignments.report(ticket_id, report))

    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None:
        self.repo.atomic(lambda: self._tickets.mark_unrelated(ticket_id, thread, reason))

    def follow_for_context(self, ticket_id: str, thread: ThreadRef) -> bool:
        return self.repo.atomic(lambda: self._tickets.follow_for_context(ticket_id, thread))

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        return self.repo.atomic(lambda: self._evals.handback(ticket_id, trigger_id, reason))

    # --- settings ---

    def policy_changed(self, from_version: str, to_version: str,
                       effects: dict[str, PolicyEffect] | None = None) -> None:
        def work() -> None:
            self._tickets.policy_changed(from_version, to_version, effects or {})
            self._evals.policy_changed(from_version, to_version)
        self.repo.atomic(work)


# --- role views: each caller sees only what its role may ask or tell ----------------------------------------------


class EngineForSources(Protocol):
    def receive(self, trigger: Trigger) -> ReceiveResult: ...
    def bind_thread(self, ticket_id: str, thread: ThreadRef, own: bool) -> Ticket: ...
    def thread_closed(self, thread: ThreadRef, outcome: str, at: datetime) -> None: ...


class EngineForRouter(Protocol):
    def candidate(self, trigger_id: str) -> Candidate | None: ...
    def candidate_delivered(self, eval_id: str) -> None: ...
    def decide(self, trigger_id: str, outcome: Outcome) -> Decision: ...
    def attach(self, ticket_id: str, trigger_id: str, note: str | None = None) -> Ticket: ...
    def reply(self, request: ReplyRequest) -> Reply: ...
    def request_status(self, ticket_id: str) -> bool: ...
    def note(self, ticket_id: str, text: str) -> Ticket: ...


class EngineForPeople(Protocol):
    def stop_ticket(self, ticket_id: str, reason: str | None = None) -> Ticket: ...
    def retry_ticket(self, ticket_id: str) -> Ticket: ...
    def reevaluate(self, trigger_id: str) -> bool: ...
    def unsubscribe(self, thread: str) -> bool: ...
    def dismiss(self, decision_id: str) -> Decision: ...


class EngineForSettings(Protocol):
    def policy_changed(self, from_version: str, to_version: str,
                       effects: dict[str, PolicyEffect] | None = None) -> None: ...


class EngineForExecutor(Protocol):
    def assignment_started(self, ticket_id: str, assignment_id: str) -> None: ...
    def inputs_delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None: ...
    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None: ...
    def turn_ended(self, ticket_id: str, assignment_id: str) -> None: ...
    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None: ...
    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None: ...
    def report(self, ticket_id: str, report: WorkerReport) -> Ticket: ...
    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None: ...
    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool: ...
    def follow_for_context(self, ticket_id: str, thread: ThreadRef) -> bool: ...
    def reply(self, request: ReplyRequest) -> Reply: ...
