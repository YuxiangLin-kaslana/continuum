"""Replies: the Engine's messages to Channels, each by the rule that allows it, under an id that keeps it from going
out twice."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .errors import UserError
from .ids import key
from .model import ChannelRef, Ticket, Trigger, latest_assignment, live_ticket, ticket_outcome
from .part import EnginePart
from .ports import OutboundMessage, ReplyStatus


class ReplyRequest(BaseModel):
    """A message by purpose: an acknowledgement of a person's request the worker took up, the answer to a person's
    question once the worker reported it, a finished ticket's result, or the ticket's current question for a person."""

    model_config = ConfigDict(extra="forbid")
    purpose: Literal["acknowledgement", "answer", "result", "attention"]
    text: str
    ticket_id: str | None = None
    trigger_id: str | None = None
    question: str | None = None


class Reply(BaseModel):
    id: str
    status: ReplyStatus
    reused: bool


class Replies(EnginePart):
    def reply(self, request: ReplyRequest) -> Reply:
        return self._acknowledge(request) if request.purpose == "acknowledgement" else self._answer(request)

    def acknowledge_accepted(self, ticket_id: str, trigger: Trigger, text: str) -> None:
        """The acknowledgement an accept carries, where the person's message that started the ticket was written."""
        if trigger.channel and trigger.human:
            self.options.outbox.send(OutboundMessage(id=key("ack", trigger.id), channel=trigger.channel, text=text,
                                                     kind="acknowledgement", ticket_id=ticket_id))

    def withdraw_attention(self, attention_id: str) -> None:
        self.options.outbox.withdraw(key("attention-reply", attention_id))

    def _acknowledge(self, request: ReplyRequest) -> Reply:
        if not request.ticket_id or not request.trigger_id:
            raise UserError("ackNoChannel")
        ticket = self.require(request.ticket_id)
        if not live_ticket(ticket):
            raise UserError("ackEnded")
        trigger = self.repo.triggers.get(request.trigger_id)
        if trigger is None:
            raise UserError("unknownTrigger")
        # Threads followed for context are read-only.
        if not self.reaches(ticket, trigger, False):
            raise UserError("notReached")
        if trigger.channel is None:
            raise UserError("ackNoChannel")
        message_id = key("ack", trigger.id)
        existing = self.options.outbox.status(message_id)
        if existing is None and not trigger.human:
            raise UserError("ackNotPerson")
        if existing is None:
            self.options.outbox.send(OutboundMessage(id=message_id, channel=trigger.channel, text=request.text,
                                                     kind="acknowledgement", ticket_id=ticket.id))
        self.take_up(ticket.id, trigger.id)
        return Reply(id=message_id, status=self.options.outbox.status(message_id) or "pending",
                     reused=existing is not None)

    def _answer(self, request: ReplyRequest) -> Reply:
        trigger = self.repo.triggers.get(request.trigger_id) if request.trigger_id else None
        if request.trigger_id and trigger is None:
            raise UserError("unknownTrigger")
        if request.ticket_id:
            ticket: Ticket | None = self.require(request.ticket_id)
        elif trigger:
            ticket = next((t for t in self.repo.tickets.list() if self.reaches(t, trigger)), None)
        else:
            ticket = None
        if request.purpose == "result":
            outcome = ticket_outcome(ticket) if ticket else None
            latest = latest_assignment(ticket) if ticket else None
            reported = ticket is not None and live_ticket(ticket) and latest is not None \
                and latest.report is not None and latest.report.state == "ready"
            if ticket is None or not (outcome in ("completed", "merged", "dropped") or reported):
                raise UserError("notReady")
        attention = ticket.attention if ticket and request.purpose == "attention" else None
        if request.purpose == "attention" and (attention is None or attention.question != request.question):
            raise UserError("notAttention")
        if request.purpose == "answer" and not (trigger and trigger.human):
            raise UserError("notQuestion")
        if attention:
            message_id = key("attention-reply", attention.id)
        else:
            message_id = key("reply", (trigger.id if trigger else ticket.id if ticket else ""), request.text)
        existing = self.options.outbox.status(message_id)
        answered = ticket is None or trigger is None or trigger.id in ticket.answers
        if request.purpose == "answer" and not answered and existing is None:
            raise UserError("noAnswer")
        asker = self.repo.triggers.get(attention.trigger_id) if attention and attention.trigger_id else None
        channel: ChannelRef | None
        if request.purpose == "answer":
            channel = trigger.channel if trigger else None
        else:
            channel = (asker.channel if asker else None) or (self._source_channel(ticket) if ticket else None)
        if channel is None:
            raise UserError("noChannel")
        self.options.outbox.send(OutboundMessage(id=message_id, channel=channel, text=request.text, kind="reply",
                                                 ticket_id=ticket.id if ticket else None))
        if request.purpose == "answer" and ticket and trigger and trigger.id in ticket.answers:
            ticket.answers = [i for i in ticket.answers if i != trigger.id]
            self.repo.tickets.save(ticket)
        return Reply(id=message_id, status=self.options.outbox.status(message_id) or "pending",
                     reused=existing is not None)

    def _source_channel(self, ticket: Ticket) -> ChannelRef | None:
        source = self.source(ticket)
        return (source.channel if source else None) or (ticket.channels[0] if ticket.channels else None)
