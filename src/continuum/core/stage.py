"""Ticket stages: what an open ticket waits for, by the first matching row."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .model import Ticket, latest_assignment, waits_for_person


class TicketStage(BaseModel):
    stage: Literal["person", "working", "idle", "ended"]
    action: Literal["delivery", "answer", "review", "blocked", "failed", "queued"] | None = None


def ticket_stage(ticket: Ticket) -> TicketStage:
    if ticket.ended:
        return TicketStage(stage="ended")
    if ticket.delivery:
        return TicketStage(stage="person", action="delivery")
    if ticket.attention:
        return TicketStage(stage="person", action="answer")
    assignment = latest_assignment(ticket)
    report = assignment.report.state if assignment and assignment.report else None
    if report == "ready":
        return TicketStage(stage="person", action="review")
    # A person's message after a blocked or failed report returns the ticket to its worker.
    if report in ("blocked", "failed") and waits_for_person(ticket):
        return TicketStage(stage="person", action=report)
    if assignment is None or assignment.state == "pending":
        return TicketStage(stage="working", action="queued")
    if report == "waiting" or assignment.state == "ended":
        return TicketStage(stage="idle")
    return TicketStage(stage="working")


def can_retry(ticket: Ticket) -> bool:
    """An open ticket stalled on a blocked or failed report with no person's message since, or with a failed delivery."""
    return ticket.ended is None and (waits_for_person(ticket) or ticket.delivery is not None)
