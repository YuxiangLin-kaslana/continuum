"""Assignments: what the Executor and its workers report about the Assignments that run tickets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from .errors import UserError
from .ids import key
from .model import Attention, AssignmentReport, DeliveryFailure, ReportState, Ticket, TriggerInput, latest_assignment, live_ticket, \
    running_assignment
from .part import EngineOptions, EnginePart
from .tickets import automatic

if TYPE_CHECKING:
    from .replies import Replies
    from .tickets import Tickets

RECOVERY_NOTE = ("The host restarted during this ticket. Inspect the current workspace and outside state before "
                 "continuing. Do not repeat an already completed external operation. Finish the original assignment "
                 "and report its status.")


class WorkerReport(BaseModel):
    """A worker's report, or its Executor's own (`waiting` after an idle turn, `failed` after an error).

    `question` asks a person one concrete question; `None` clears an open one; leaving it unset keeps the current one.
    """

    model_config = ConfigDict(extra="forbid")
    state: ReportState
    summary: str
    question: str | None = None
    clear_question: bool = False
    # The person's message whose request raised the question; the question is asked where it was written.
    asked_by: str | None = None
    # The person's question this report answers.
    answers: str | None = None


class Assignments(EnginePart):
    def __init__(self, options: EngineOptions, tickets: Tickets, replies: Replies):
        super().__init__(options)
        self.tickets = tickets
        self.replies = replies

    def started(self, ticket_id: str, assignment_id: str) -> None:
        ticket = self.require(ticket_id)
        assignment = next((a for a in ticket.assignments if a.id == assignment_id), None)
        if assignment is None or assignment.state == "ended":
            return
        assignment.state = "running"
        assignment.started_at = assignment.started_at or self.now()
        ticket.updated_at = self.now()
        self.repo.tickets.save(ticket)
        if live_ticket(ticket) and ticket.undelivered:
            self.options.executor.deliver(ticket, assignment)
        self.emit("assignment.started", ticket_id=ticket_id, assignment_id=assignment_id)

    def delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if ticket is None:
            return
        ticket.undelivered = [i for i in ticket.undelivered if i not in input_ids]
        ticket.updated_at = self.now()
        running = running_assignment(ticket)
        assert running
        running.delivered_at = ticket.updated_at
        for item in ticket.inputs:
            if isinstance(item, TriggerInput) and item.id in input_ids:
                item.delivered_to = assignment_id
        ticket.delivery = None
        self.repo.tickets.save(ticket)

    def read(self, ticket_id: str, input_ids: list[str]) -> None:
        """The worker's Session took these Inputs into a step, from which its model reads them."""
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            return
        for item in ticket.inputs:
            if isinstance(item, TriggerInput) and item.id in input_ids and item.read_at is None:
                item.read_at = self.now()
        self.repo.tickets.save(ticket)

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if ticket is None:
            return
        running = running_assignment(ticket)
        assert running
        running.idle_at = self.now()
        self.repo.tickets.save(ticket)

    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if ticket is None:
            return
        ticket.delivery = DeliveryFailure(error=error[:300], at=self.now())
        self.repo.tickets.save(ticket)

    def interrupted(self, ticket_id: str, assignment_id: str) -> None:
        """An Assignment was running when the host stopped: start it again with a recovery note, unless it had
        reported waiting, ready, blocked or failed and received nothing since."""
        ticket = self._running(ticket_id, assignment_id)
        running = running_assignment(ticket) if ticket else None
        if ticket is None or running is None:
            return
        report = running.report
        busy = running.delivered_at is not None and report is not None and running.delivered_at >= report.at
        settled = report is not None and report.state in ("waiting", "ready", "blocked", "failed") and not busy
        if not settled:
            self.tickets.restart(ticket, running, "hostRestarted", RECOVERY_NOTE)
        elif ticket.undelivered:
            self.options.executor.deliver(ticket, running)

    def report(self, ticket_id: str, report: WorkerReport) -> Ticket:
        """Record a report on an open ticket's current Assignment. `dropped` ends the ticket; `ready` ends it as
        completed, or, when it follows a Source's Threads, once it is done with them."""
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            raise UserError("ticketEnded")
        if report.question and (report.state == "ready" or report.answers):
            raise UserError("questionWithResult")
        if report.asked_by and not report.question:
            raise UserError("askerWithoutQuestion")

        def person(trigger_id: str, channel: bool) -> bool:
            trigger = self.repo.triggers.get(trigger_id)
            return bool(trigger and trigger.human and (not channel or trigger.channel)
                        and self.reaches(ticket, trigger, False))

        if report.asked_by and not person(report.asked_by, True):
            raise UserError("notAsker")
        if report.answers and not person(report.answers, False):
            raise UserError("notAnswerable")
        assignment = running_assignment(ticket) or latest_assignment(ticket)
        if assignment is None:
            raise UserError("unknownAssignment")
        if assignment.state == "ended":
            raise UserError("assignmentEnded")
        at = self.now()
        previous = ticket.attention
        if report.question and (previous is None or report.question != previous.question):
            asked = Attention(id=key("attention", ticket_id, report.question, at), question=report.question,
                              trigger_id=report.asked_by)
        else:
            asked = previous
        attention = None if report.clear_question or report.state == "ready" else asked
        ticket.attention = attention
        # A replaced or cleared question withdraws its reply that has not gone out.
        if previous and (attention is None or previous.id != attention.id):
            self.replies.withdraw_attention(previous.id)
        assignment.report = AssignmentReport(state=report.state, summary=report.summary, at=at)
        ticket.updated_at = at
        if report.answers and report.answers not in ticket.answers:
            ticket.answers.append(report.answers)
        if report.answers:
            self.take_up(ticket_id, report.answers)
        # A worker that waits on a person gets no automatic Input until a person writes.
        if report.state in ("blocked", "failed"):
            ticket.undelivered = [i for i in ticket.undelivered if not automatic(ticket.input(i))]
        self.repo.tickets.save(ticket)
        done = self.tickets.follows_done(ticket)
        ready = "completed" if not ticket.follows else (done.outcome if done else None)
        if report.state == "dropped":
            outcome = done.outcome if done else "dropped"
        elif report.state == "ready":
            outcome = ready
        else:
            outcome = None
        if outcome:
            self.tickets.end(ticket_id, "executor", outcome, at)
        return self.require(ticket_id)

    def _running(self, ticket_id: str, assignment_id: str) -> Ticket | None:
        ticket = self.require(ticket_id)
        running = running_assignment(ticket)
        return ticket if live_ticket(ticket) and running and running.id == assignment_id else None
