"""Intake: a Source's Trigger stored once, recorded on its Threads, fanned out to subscribed tickets, and queued for
Eval unless a ticket's worker decides it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .ids import key
from .model import Ticket, Trigger, TriggerDisposition, TriggerInput, live_ticket, thread_key
from .part import EngineOptions, EnginePart
from .reasons import Reason

if TYPE_CHECKING:
    from .tickets import Tickets

ReceiveResult = Literal["stored", "duplicate", "filtered"]


class Intake(EnginePart):
    def __init__(self, options: EngineOptions, tickets: Tickets):
        super().__init__(options)
        self.tickets = tickets

    def receive(self, trigger: Trigger) -> ReceiveResult:
        if self.repo.triggers.get(trigger.id):
            return "duplicate"
        at = self.now()
        scope = self.options.scope.scope(trigger.source, trigger.meta)
        if not scope.listened:
            self.repo.triggers.save(trigger)
            self.repo.triggers.save_disposition(trigger.id, TriggerDisposition(
                filtered=Reason(code=scope.reason or "filtered"), at=at))
            return "filtered"
        self.repo.triggers.save(trigger)
        keys = [thread_key(ref) for ref in trigger.threads]
        # ticket id -> (ticket, reached only on Threads followed for context)
        reached: dict[str, tuple[Ticket, bool]] = {}
        for ref in trigger.threads:
            thread = self.thread(ref)
            if thread.last_at is None or trigger.at > thread.last_at:
                thread.last_at = trigger.at
            thread.correlated = list(dict.fromkeys([*thread.correlated, *(k for k in keys if k != thread.key)]))
            for ticket_id in thread.subscriptions:
                ticket = self.repo.tickets.get(ticket_id)
                # Only a person's message on a Thread of its own resumes an ended ticket; bots and CI do not, since
                # its worker reads its Threads again when it resumes. A ticket a person stopped receives nothing.
                followed = ticket_id in thread.context
                resumes = trigger.human and not followed and ticket is not None and (
                    ticket.ended is None or ticket.ended.by != "human")
                if ticket is None or (not live_ticket(ticket) and not resumes):
                    continue
                before = reached.get(ticket_id)
                reached[ticket_id] = (ticket, followed and (before[1] if before else True))
            self.repo.threads.save(thread)
        for ticket_id, (ticket, followed) in reached.items():
            self.tickets.add_input(ticket, TriggerInput(
                id=key("input", ticket_id, "trigger", trigger.id), trigger_id=trigger.id, at=at,
                human=trigger.human, context=followed))
        fanout = list(reached)
        context = [i for i in fanout if reached[i][1]]
        self.repo.triggers.save_disposition(trigger.id, TriggerDisposition(fanout=fanout, context=context, at=at))
        # A person's message no ticket decides, and every bot's, goes to Eval.
        if not self.owners(trigger):
            self.queue_for_eval(trigger)
        for ticket_id in fanout:
            self.tickets.wake(ticket_id)
        self.emit("trigger.received", trigger_id=trigger.id, threads=keys)
        return "stored"
