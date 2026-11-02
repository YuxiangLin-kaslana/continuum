"""Tickets: their lifecycle from an accept to their end, their Subscriptions and their Inputs."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from .errors import UserError
from .ids import key
from .model import Assignment, ChannelRef, Evaluation, Follow, FollowClose, Mandate, NoteInput, Origin, \
    PolicyChangedInput, PolicyEffect, ResumedInput, TextInput, ThreadRef, Ticket, TicketEnd, TicketEndBy, \
    TicketInput, Trigger, TriggerInput, latest_assignment, live_ticket, read_until, running_assignment, settled, \
    thread_key, waits_for_person
from .part import EngineOptions, EnginePart
from .ports import Accept
from .stage import can_retry

if TYPE_CHECKING:
    from .replies import Replies

STATUS_REQUEST = ("Report the current ticket status. This is a status-only request: do not resume implementation or "
                  "perform external actions.")


def automatic(item: TicketInput | None) -> bool:
    """A message no person wrote, such as CI's, a bot's, or the router's note."""
    return isinstance(item, NoteInput) or (isinstance(item, TriggerInput) and not item.human)


def read_before(item: TriggerInput, until: datetime) -> bool:
    """Whether the worker's Session took the message into a step before `until`.

    Unlike dsh-superagent, a message never delivered to the worker counts as unread, even if it arrived before
    `until`: the worker cannot have read it through its Session."""
    return item.read_at is not None and item.read_at < until


class Tickets(EnginePart):
    def __init__(self, options: EngineOptions, replies: Replies):
        super().__init__(options)
        self.replies = replies

    def create(self, ticket_id: str, decision_id: str, mandate: Mandate, evaluation: Evaluation, outcome: Accept,
               at: datetime) -> None:
        """Create the ticket an accept decided, with the Channels and Threads of its Triggers, its first Assignment and
        the acknowledgement the accept carries, and queue its launch."""
        trigger_ids = evaluation.trigger_ids
        primary = self.repo.triggers.get(evaluation.primary or trigger_ids[-1])
        origin = Origin(trigger_id=primary.id if primary else trigger_ids[0], trigger_ids=list(trigger_ids))
        channels: list[ChannelRef] = []
        for trigger_id in trigger_ids:
            trigger = self.repo.triggers.get(trigger_id)
            if trigger and trigger.channel and trigger.channel not in channels:
                channels.append(trigger.channel)
        ticket = Ticket(id=ticket_id, title=outcome.title, decision_id=decision_id, mandate=mandate, channels=channels,
                        created_at=at, updated_at=at, origin=origin)
        if self.options.prepare_ticket:
            ticket = self.options.prepare_ticket(ticket, outcome)
        assignment = self._new_assignment(ticket)
        self.repo.tickets.save(ticket)
        for trigger_id in trigger_ids:
            trigger = self.repo.triggers.get(trigger_id)
            for ref in trigger.threads if trigger else ():
                self._subscribe(ticket_id, ref)
        if outcome.acknowledgement and primary:
            self.replies.acknowledge_accepted(ticket_id, primary, outcome.acknowledgement)
        self.options.executor.start(self.require(ticket_id), assignment)
        self.emit("ticket.created", ticket_id=ticket_id, decision_id=decision_id, strength=mandate.strength)

    # --- Threads a ticket follows ---

    def bind(self, ticket_id: str, ref: ThreadRef, own: bool) -> Ticket:
        """Subscribe an open ticket to a Source's Thread it follows, such as its pull request's."""
        if not live_ticket(self.require(ticket_id)):
            raise UserError("ticketEnded")
        self._subscribe(ticket_id, ref)
        ticket = self.require(ticket_id)
        followed = thread_key(ref)
        follow = next((f for f in ticket.follows if f.thread == followed), None)
        if follow:
            follow.own = follow.own or own
            follow.closed = None
        else:
            ticket.follows.append(Follow(thread=followed, own=own))
        ticket.updated_at = self.now()
        self.repo.tickets.save(ticket)
        return ticket

    def thread_closed(self, ref: ThreadRef, outcome: str, at: datetime) -> None:
        """A Thread open tickets follow closed: each ticket done with its Threads ends, unless its work has not
        finished, which ends it once it reports ready."""
        closed = thread_key(ref)
        for ticket in self.repo.tickets.open():
            follow = next((f for f in ticket.follows if f.thread == closed and f.closed is None), None)
            if follow is None:
                continue
            follow.closed = FollowClose(at=at, outcome=outcome)
            self.repo.tickets.save(ticket)
            done = self.follows_done(ticket)
            if done is None or self._unfinished(ticket, at):
                continue
            latest = latest_assignment(ticket)
            resumed = latest is not None and latest.created_at > at
            self.end(ticket.id, "executor", done.outcome, self.now() if resumed else done.at)

    def follows_done(self, ticket: Ticket) -> FollowClose | None:
        """How a ticket is done with the Threads it follows: once no Thread it opened is open, or, when it opened none,
        once every one it follows closed; the latest close among them."""
        own = [f for f in ticket.follows if f.own]
        counted = own or ticket.follows
        if not counted or any(f.closed is None for f in counted):
            return None
        # The latest close; among equal times, the last one listed.
        return sorted((f.closed for f in counted if f.closed), key=lambda c: c.at)[-1]

    # --- routing to a ticket ---

    def attachable(self, ticket_id: str, trigger_id: str) -> Trigger:
        trigger = self.repo.triggers.get(trigger_id)
        if trigger is None:
            raise UserError("unknownTrigger")
        self.left_to_workers(trigger)
        current = self.require(ticket_id)
        if current.ended and current.ended.by == "human":
            raise UserError("ticketStopped")
        if not live_ticket(current) and not self.policy().revalidate(current.mandate):
            raise UserError("mandateRevoked")
        return trigger

    def attach(self, ticket_id: str, trigger: Trigger, note: str | None = None) -> None:
        """Subscribe a ticket to an attached Trigger's Threads and add the Trigger as an Input, with its note."""
        for ref in trigger.threads:
            self._subscribe(ticket_id, ref)
        current = self.disposition(trigger.id)
        was_context, was_requeued = list(current.context), current.requeued
        was_fanout = list(current.fanout)
        current.context = [i for i in current.context if i != ticket_id]
        current.requeued = False
        current.fanout = list(dict.fromkeys([*current.fanout, ticket_id]))
        self.repo.triggers.save_disposition(trigger.id, current)
        ticket = self.require(ticket_id)
        input_id = key("input", ticket_id, "trigger", trigger.id)
        existing = ticket.input(input_id)
        at = self.now()

        def made(new_id: str) -> TriggerInput:
            return TriggerInput(id=new_id, trigger_id=trigger.id, at=at, note=note, human=trigger.human)

        if not isinstance(existing, TriggerInput):
            self.add_input(ticket, made(input_id))
        elif note and input_id in ticket.undelivered:
            existing.note = note
            ticket.updated_at = at
            self.repo.tickets.save(ticket)
        elif was_requeued or ticket_id not in was_fanout or ticket_id in was_context or not live_ticket(ticket):
            # A person sent it to Eval, or the ticket let it go, read it for context only, or ended after its worker
            # had it: the worker gets it again.
            self.add_input(ticket, made(key("input", ticket_id, "trigger", trigger.id, "attached", at)))
        elif note:
            self.add_input(ticket, made(key("input", ticket_id, "trigger", trigger.id, "note", note)))

    def request_status(self, ticket_id: str) -> bool:
        """Ask a running worker whose latest report is `waiting` for its status."""
        ticket = self.require(ticket_id)
        running = running_assignment(ticket)
        if not live_ticket(ticket) or running is None or running.report is None or running.report.state != "waiting":
            return False
        self.add_input(ticket, TextInput(id=f"status:{ticket_id}:{ticket.updated_at.isoformat()}",
                                         text=STATUS_REQUEST, at=self.now()))
        return True

    def stop(self, ticket_id: str, reason: str | None = None) -> Ticket:
        self.end(ticket_id, "human", "stopped", self.now(), reason)
        return self.require(ticket_id)

    def retry(self, ticket_id: str) -> Ticket:
        """End the stalled Assignment of a retryable ticket and start a new one under the same Mandate."""
        ticket = self.require(ticket_id)
        latest = latest_assignment(ticket)
        if not can_retry(ticket) or latest is None:
            raise UserError("notRetryable")
        self._end_assignment(ticket, latest, "retried")
        self._requeue_untaken(ticket, latest)
        assignment = self._new_assignment(ticket)
        ticket.updated_at = self.now()
        self.repo.tickets.save(ticket)
        self.options.executor.stop(ticket, latest, "retried")
        self.options.executor.start(ticket, assignment)
        return ticket

    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None:
        """Drop a ticket's Subscription to a Thread its worker found unrelated, and hand back the person's messages
        there that the ticket decides."""
        ticket = self.require(ticket_id)
        source = self.source(ticket)
        if source and any(thread_key(ref) == thread for ref in source.threads):
            raise UserError("sourceThread")
        if any(f.thread == thread for f in ticket.follows):
            raise UserError("followedThread")
        current = self.repo.threads.get(thread)
        if current is None or ticket_id not in current.subscriptions:
            raise UserError("notFollowing")
        self.drop_subscriptions(thread, "executor", ticket_id, reason)
        for trigger in self._triggers_of(ticket):
            if any(thread_key(ref) == thread for ref in trigger.threads) and ticket_id in self.owners(trigger):
                self.hand_back(ticket_id, trigger, reason)

    def policy_changed(self, from_version: str, to_version: str, effects: dict[str, PolicyEffect]) -> None:
        """Tell each open ticket a Policy change bears on how it does, oldest first."""
        at = self.now()
        for ticket in sorted(self.repo.tickets.open(), key=lambda t: t.created_at):
            effect = effects.get(ticket.id)
            if effect:
                self.add_input(ticket, PolicyChangedInput(
                    id=key("input", ticket.id, "policy", from_version, to_version, at), from_version=from_version,
                    to_version=to_version, at=at, effect=effect))

    def drop_subscriptions(self, thread: str, by: str, ticket_id: str | None = None,
                           reason: str | None = None) -> bool:
        """Drop a Thread's Subscriptions, every ticket's or only `ticket_id`'s; false when none dropped."""
        current = self.repo.threads.get(thread)
        dropped = [i for i in (current.subscriptions if current else []) if ticket_id is None or i == ticket_id]
        if current is None or not dropped:
            return False
        current.subscriptions = [i for i in current.subscriptions if i not in dropped]
        self.repo.threads.save(current)
        for i in dropped:
            ticket = self.repo.tickets.get(i)
            if ticket and any(f.thread == thread for f in ticket.follows):
                ticket.follows = [f for f in ticket.follows if f.thread != thread]
                self.repo.tickets.save(ticket)
        for i in dropped:
            self.emit("thread.unsubscribed", thread_key=current.key, ticket_id=i, by=by, reason=reason)
        return True

    # --- Inputs ---

    def add_input(self, ticket: Ticket, item: TicketInput) -> None:
        """Add an Input once and ask the Executor to deliver it, or to launch again a pending Assignment whose launch
        failed once no person is waited for. Automatic Inputs are held while the worker waits on a person."""
        if any(existing.id == item.id for existing in ticket.inputs):
            return
        ticket.inputs.append(item)
        ticket.updated_at = self.now()
        held = live_ticket(ticket) and automatic(item) and waits_for_person(ticket)
        if not held:
            ticket.undelivered.append(item.id)
        self.repo.tickets.save(ticket)
        if not live_ticket(ticket) or held:
            return
        running, latest = running_assignment(ticket), latest_assignment(ticket)
        if running:
            self.options.executor.deliver(ticket, running)
        elif latest and latest.state == "pending" and not waits_for_person(ticket):
            self.options.executor.start(ticket, latest)

    def wake(self, ticket_id: str) -> None:
        """Resume an ended ticket under its unchanged Mandate when a person's message on one of its Threads waits for
        it: no Eval and no Decision. One whose Mandate the Policy no longer holds stays ended and takes nothing."""
        ticket = self.repo.tickets.get(ticket_id)
        if ticket is None or live_ticket(ticket) or (ticket.ended and ticket.ended.by == "human") \
                or not ticket.undelivered:
            return
        policy = self.policy()
        if policy.paused:
            return
        if not policy.revalidate(ticket.mandate):
            self._refuse(ticket)
            return
        first = next((i for i in (ticket.input(u) for u in ticket.undelivered)
                      if isinstance(i, TriggerInput) and i.human), None)
        if first is None:
            return
        at = self.now()
        ticket.ended = None
        ticket.updated_at = at
        assignment = self._new_assignment(ticket)
        resumed = ResumedInput(id=key("input", ticket.id, "resumed", first.trigger_id), trigger_id=first.trigger_id,
                               at=at)
        ticket.inputs.append(resumed)
        ticket.undelivered.insert(0, resumed.id)
        self.repo.tickets.save(ticket)
        self.options.executor.start(ticket, assignment)
        self.emit("ticket.resumed", ticket_id=ticket_id, trigger_id=first.trigger_id)

    def note(self, ticket_id: str, text: str) -> Ticket:
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            raise UserError("ticketEnded")
        at = self.now()
        self.add_input(ticket, NoteInput(id=key("input", ticket_id, "note", at, text), text=text, at=at))
        return self.require(ticket_id)

    def follow_for_context(self, ticket_id: str, ref: ThreadRef) -> bool:
        if not live_ticket(self.require(ticket_id)):
            raise UserError("ticketEnded")
        if ticket_id in self.thread(ref).subscriptions:
            return False
        self._subscribe(ticket_id, ref)
        thread = self.thread(ref)
        thread.context.append(ticket_id)
        self.repo.threads.save(thread)
        return True

    # --- ending ---

    def end(self, ticket_id: str, by: TicketEndBy, outcome: str, at: datetime | None = None,
            reason: str | None = None) -> None:
        """End a ticket. It lets go of the messages its worker never received, and of the person's messages it decides
        that its worker neither read before its latest report or turn end, nor took up or handed back."""
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            return
        at = at or self.now()
        stopping = [a for a in ticket.assignments if a.state != "ended"]
        until = read_until(ticket)
        waiting = self._triggers_of(ticket, ticket.undelivered)
        left = self._untaken(ticket, [
            i.id for i in ticket.inputs if isinstance(i, TriggerInput)
            and (not read_before(i, until) or i.id in ticket.undelivered)])
        ticket.ended = TicketEnd(at=at, by=by, outcome=outcome, reason=reason if reason != outcome else None)
        ticket.undelivered = []
        ticket.updated_at = self.now()
        ticket.attention = None
        ticket.delivery = None
        for assignment in stopping:
            assignment.state = "ended"
            assignment.ended_at = at
        self.repo.tickets.save(ticket)
        self._let_go(ticket_id, waiting, left)
        self.emit("ticket.ended", ticket_id=ticket_id, by=by, outcome=outcome, reason=ticket.ended.reason)
        for assignment in stopping:
            self.options.executor.stop(ticket, assignment, reason or outcome)

    def restart(self, ticket: Ticket, running: Assignment, reason: str, note: str) -> None:
        """Replace a running Assignment with a new one that receives `note` first."""
        self._end_assignment(ticket, running, reason)
        self._requeue_untaken(ticket, running)
        assignment = self._new_assignment(ticket)
        at = self.now()
        item = TextInput(id=key("input", ticket.id, "restart", assignment.id), text=note, at=at)
        ticket.inputs.append(item)
        ticket.undelivered.append(item.id)
        ticket.updated_at = at
        self.repo.tickets.save(ticket)
        self.options.executor.start(ticket, assignment)

    # --- internals ---

    def _triggers_of(self, ticket: Ticket, ids: list[str] | None = None) -> list[Trigger]:
        out = []
        for item in ticket.inputs:
            if isinstance(item, TriggerInput) and (ids is None or item.id in ids):
                trigger = self.repo.triggers.get(item.trigger_id)
                if trigger:
                    out.append(trigger)
        return out

    def _taken_up(self, ticket: Ticket, trigger: Trigger) -> bool:
        disposition = self.repo.triggers.disposition(trigger.id)
        return bool(disposition) and ticket.id in disposition.acked

    def _untaken(self, ticket: Ticket, ids: list[str]) -> list[Trigger]:
        """The person's messages among Inputs `ids` that the ticket decides and its worker did not take up."""
        return [t for t in self._triggers_of(ticket, ids)
                if ticket.id in self.owners(t) and not self._taken_up(ticket, t)]

    def _let_go(self, ticket_id: str, waiting: list[Trigger], left: list[Trigger]) -> None:
        """The ticket no longer counts as having reached these messages; each of `left` no other ticket decides goes
        to Eval, unless a ticket took it up, a Decision settled it, or it is the message a stop ended this ticket on."""
        for trigger in [*waiting, *left]:
            disposition = self.repo.triggers.disposition(trigger.id)
            if disposition and ticket_id in disposition.fanout:
                disposition.fanout = [i for i in disposition.fanout if i != ticket_id]
                self.repo.triggers.save_disposition(trigger.id, disposition)
        for trigger in left:
            disposition = self.repo.triggers.disposition(trigger.id)
            decision = self.repo.decisions.get(disposition.decision_id) \
                if disposition and disposition.decision_id else None
            stopped = decision is not None and decision.kind == "stop" and decision.ticket_id == ticket_id \
                and not (disposition and disposition.handbacks)
            decided = decision is not None and (settled(decision) or stopped)
            if not self.owners(trigger) and not (disposition and disposition.acked) and not decided:
                self.queue_for_eval(trigger)

    def _refuse(self, ticket: Ticket) -> None:
        waiting = self._triggers_of(ticket, ticket.undelivered)
        left = self._untaken(ticket, ticket.undelivered)
        ticket.undelivered = []
        self.repo.tickets.save(ticket)
        self._let_go(ticket.id, waiting, left)

    def _requeue_untaken(self, ticket: Ticket, replaced: Assignment) -> None:
        """Put back, ahead of what waits, the Trigger Inputs delivered to `replaced` that its worker did not take."""
        untaken = [i.id for i in ticket.inputs if isinstance(i, TriggerInput) and i.delivered_to == replaced.id
                   and i.read_at is None and i.id not in ticket.undelivered]
        ticket.undelivered[:0] = untaken

    def _unfinished(self, ticket: Ticket, at: datetime) -> bool:
        """Whether a ticket's work has not finished at a close at `at`."""
        until = read_until(ticket, progress=False)
        unread = [i.id for i in ticket.inputs if isinstance(i, TriggerInput) and not read_before(i, until)]
        taken = any(ticket.id in self.owners(t) and self._taken_up(ticket, t) for t in self._triggers_of(ticket, unread))
        latest = latest_assignment(ticket)
        if taken or latest is None or not latest.created_at > at:
            return taken
        report = latest.report
        later = any(i.kind != "policy-changed" and (report is None or i.at > report.at) for i in ticket.inputs)
        return report is None or report.state != "ready" or bool(ticket.undelivered) or later

    def _new_assignment(self, ticket: Ticket) -> Assignment:
        assignment = Assignment(id=key("assignment", ticket.id, str(len(ticket.assignments))), created_at=self.now())
        ticket.assignments.append(assignment)
        ticket.delivery = None
        return assignment

    def _end_assignment(self, ticket: Ticket, assignment: Assignment, reason: str) -> None:
        assignment.state = "ended"
        assignment.ended_at = self.now()
        assignment.ended_reason = reason
        self.emit("assignment.ended", ticket_id=ticket.id, assignment_id=assignment.id, reason=reason)

    def _subscribe(self, ticket_id: str, ref: ThreadRef) -> None:
        thread = self.thread(ref)
        if ticket_id not in thread.subscriptions:
            thread.subscriptions.append(ticket_id)
            self.emit("thread.subscribed", thread_key=thread.key, ticket_id=ticket_id)
        thread.context = [i for i in thread.context if i != ticket_id]
        self.repo.threads.save(thread)
