"""Evals: the Thread-level judgements of queued Triggers, and the Decisions they end with."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import UserError
from .ids import key
from .model import ActorRef, Decision, DecisionKind, Evaluation, Mandate, MandateBasis, Strength, Thread, Trigger, \
    live_ticket, thread_key
from .part import EngineOptions, EnginePart
from .ports import Accept, Admit, Candidate, Ignore, Outcome, Policy, Stop, Unavailable
from .reasons import Reason, said

if TYPE_CHECKING:
    from .tickets import Tickets

# How many times in a row an expired Eval queues its Triggers again before it is recorded unavailable.
REQUEUES = 2


def _reasons(reason: str | Reason) -> list[Reason]:
    return [said(reason) if isinstance(reason, str) else reason]


def _stronger(a: Strength, b: Strength) -> Strength:
    return "explicit" if "explicit" in (a, b) else "inferred"


class Evals(EnginePart):
    """Batches a Thread's queued Triggers into one Eval, takes its strength from the gate, asks the assessor, keeps its
    deadline through a pause, and records the Decision it ends with."""

    def __init__(self, options: EngineOptions, tickets: Tickets):
        super().__init__(options)
        self.tickets = tickets

    # --- the clock ---

    def due(self, thread: Thread, policy: Policy) -> None:
        """Expire a Thread's Eval past its deadline, unless processing is paused: then the Eval notes when the pause
        began, and its deadline moves on by the pause's length once processing resumes. An Eval whose candidate has
        not reached the assessor does not expire."""
        evaluation = thread.evaluating
        if evaluation is None or (policy.paused and evaluation.paused_at):
            return
        now = self.now()
        if policy.paused:
            evaluation.paused_at = now
        elif evaluation.paused_at:
            evaluation.deadline = evaluation.deadline + (now - evaluation.paused_at)
            evaluation.paused_at = None
        elif evaluation.delivered and evaluation.deadline <= now:
            self._expire(thread)
            return
        else:
            return
        self.repo.threads.save(thread)

    def delivered(self, eval_id: str, policy: Policy) -> None:
        """An Eval's candidate reached the assessor: its deadline counts from now."""
        thread = next((t for t in self.repo.threads.active() if t.evaluating and t.evaluating.id == eval_id), None)
        if thread is None or thread.evaluating is None or thread.evaluating.delivered:
            return
        now = self.now()
        thread.evaluating.delivered = now
        thread.evaluating.deadline = now + policy.eval_timeout
        self.repo.threads.save(thread)

    def _expire(self, thread: Thread) -> None:
        expired = thread.expired + 1
        assert thread.evaluating
        if expired > REQUEUES:
            self._finish(thread, thread.evaluating, "unavailable", [Reason(code="timeout")])
            return
        eval_id, trigger_ids = thread.evaluating.id, thread.evaluating.trigger_ids
        thread.evaluating = None
        thread.expired = expired
        thread.pending = [*trigger_ids, *(i for i in thread.pending if i not in trigger_ids)]
        self.repo.threads.save(thread)
        self.options.assessor.release(eval_id)

    def start(self, key_: str, policy: Policy) -> None:
        """Start one Eval of the Triggers queued on a Thread, judged by the gate, and ask the assessor."""
        thread = self.repo.threads.get(key_)
        assert thread is not None
        at = self.now()
        # Triggers that already started a ticket cannot lend authority to a new Eval or start a duplicate.
        accepted = self._accepted(thread.key)
        started = [i for i in thread.pending if i in accepted]
        if started:
            self._ignore(thread, started, [Reason(code="startedTicket", trigger_id=i,
                                                  values={"ticket": accepted[i].ticket_id or ""}) for i in started],
                         policy, at)
        triggers = [t for i in thread.pending if (t := self.repo.triggers.get(i)) is not None]
        tickets = self.tickets_on_thread(thread)
        reasons: list[Reason] = []
        strongest: tuple[Trigger, Admit] | None = None
        for trigger in triggers:
            gate = policy.gate(trigger, thread, tickets)
            if not isinstance(gate, Admit):
                reasons.append(Reason(code=gate.reason, trigger_id=trigger.id))
                continue
            if strongest is None:
                strongest = (trigger, gate)
                continue
            later = gate.strength == strongest[1].strength and trigger.at >= strongest[0].at
            weaker = _stronger(strongest[1].strength, gate.strength) != strongest[1].strength
            if weaker or later:
                strongest = (trigger, gate)
        if strongest is None:
            if thread.pending:
                self._ignore(thread, list(thread.pending), reasons or [Reason(code="noAdmissible")], policy, at)
            self.repo.threads.save(thread)
            return
        primary, gate = strongest
        eval_id = key("eval", thread.key, policy.version, at, *thread.pending)
        thread.evaluating = Evaluation(
            id=eval_id, thread_key=thread.key, trigger_ids=list(thread.pending), primary=primary.id,
            strength=gate.strength, actor=primary.actor, basis=gate.basis, policy_version=policy.version,
            started_at=at, deadline=at + policy.eval_timeout)
        thread.pending = []
        self.repo.threads.save(thread)
        for trigger in triggers:
            disposition = self.disposition(trigger.id)
            disposition.eval_id = eval_id
            self.repo.triggers.save_disposition(trigger.id, disposition)
        self.emit("eval.started", eval_id=eval_id, thread_key=thread.key,
                  trigger_ids=thread.evaluating.trigger_ids, strength=gate.strength)
        self.options.assessor.request(self._candidate_of(thread))

    # --- deciding ---

    def candidate(self, trigger_id: str) -> Candidate | None:
        thread = self._holding(trigger_id)
        return self._candidate_of(thread) if thread else None

    def decide(self, trigger_id: str, outcome: Outcome) -> Decision:
        """Decide a Trigger: the Eval that holds it, or a direct Eval of it alone when none does."""
        trigger = self.repo.triggers.get(trigger_id)
        if trigger is None:
            raise UserError("unknownTrigger")
        held = self._holding(trigger_id)
        if outcome.eval_id and (held is None or held.evaluating is None or held.evaluating.id != outcome.eval_id):
            raise UserError("evalFinished", {"eval": outcome.eval_id})
        # A replayed Trigger that already started a ticket does not start a second one.
        earlier = self._accepted(thread_key(trigger.threads[0])).get(trigger_id) if isinstance(outcome, Accept) \
            else None
        if earlier:
            if held and held.evaluating:
                others = [i for i in held.evaluating.trigger_ids if i != trigger_id]
                if others:
                    self._reassess(held, Reason(code="acceptedBefore", values={"trigger": trigger_id}), [trigger_id])
                else:
                    self._finish(held, held.evaluating, "ignore", [Reason(
                        code="startedTicket", trigger_id=trigger_id, values={"ticket": earlier.ticket_id or ""})])
            return earlier
        # A follow-up that reached a ticket is its worker's to judge: the router may only stop the work on it.
        if not isinstance(outcome, Stop):
            self.left_to_workers(trigger)
        thread = held or self.thread(trigger.threads[0])
        evaluation = held.evaluating if held and held.evaluating else self._direct_eval(
            thread, trigger, outcome.kind, outcome.ticket_id if isinstance(outcome, Stop) else None)
        # An inferred candidate is accepted only on enabled rules its assessment matched, in auto mode.
        if isinstance(outcome, Accept) and evaluation.strength != "explicit" \
                and not self.policy().admits_inferred(outcome.rules or []):
            raise UserError("notExplicit")
        return self._decide_eval(thread, evaluation, outcome)

    def reevaluate(self, trigger_id: str) -> bool:
        """Queue a stored Trigger for a fresh Eval, such as a person's replay; false while an Eval holds it."""
        trigger = self.repo.triggers.get(trigger_id)
        if trigger is None:
            raise UserError("unknownTrigger")
        if self._holding(trigger_id):
            return False
        self.queue_for_eval(trigger)
        disposition = self.disposition(trigger_id)
        disposition.requeued = True
        self.repo.triggers.save_disposition(trigger_id, disposition)
        self.emit("trigger.requeued", trigger_id=trigger_id)
        return True

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        """A ticket's worker hands back a Trigger that reached it as beyond the ticket. Returns whether it was queued."""
        ticket = self.require(ticket_id)
        if not any(getattr(i, "trigger_id", None) == trigger_id and i.kind == "trigger" for i in ticket.inputs):
            raise UserError("notReached")
        disposition = self.repo.triggers.disposition(trigger_id)
        if disposition and any(h.ticket_id == ticket_id for h in disposition.handbacks):
            raise UserError("handedBack")
        trigger = self.repo.triggers.get(trigger_id)
        assert trigger is not None
        return self.hand_back(ticket_id, trigger, reason)

    def dismiss(self, decision_id: str) -> Decision:
        decision = self.repo.decisions.get(decision_id)
        if decision is None or decision.kind != "unavailable":
            raise UserError("unknownProblem")
        decision.dismissed = True
        self.repo.decisions.save(decision)
        return decision

    def withdraw(self, trigger: Trigger) -> None:
        """Take a Trigger out of its Thread's queue because it was routed by hand."""
        thread = self.repo.threads.get(thread_key(trigger.threads[0]))
        if thread is None or trigger.id not in thread.pending:
            return
        thread.pending = [i for i in thread.pending if i != trigger.id]
        self.repo.threads.save(thread)

    def routed(self, trigger_id: str, ticket_id: str) -> None:
        """End the Eval that holds a Trigger routed to a ticket; its other Triggers wait for a fresh Eval."""
        held = self._holding(trigger_id)
        if held is None or held.evaluating is None:
            return
        reason = Reason(code="routed", values={"ticket": ticket_id})
        if len(held.evaluating.trigger_ids) == 1:
            self._finish(held, held.evaluating, "ignore", [reason])
        else:
            self._reassess(held, reason.model_copy(update={"trigger_id": trigger_id}), [trigger_id])

    def policy_changed(self, from_version: str, to_version: str) -> None:
        for thread in self.repo.threads.active():
            if thread.evaluating:
                self._reassess(thread, Reason(code="policyChanged"))
        self.emit("policy.changed", from_version=from_version, to_version=to_version)

    # --- internals ---

    def _reassess(self, thread: Thread, reason: Reason, without: list[str] | None = None) -> Decision:
        assert thread.evaluating
        ids = [i for i in thread.evaluating.trigger_ids if i not in (without or [])]
        decision = self._finish(thread, thread.evaluating, "unavailable", [reason])
        current = self.repo.threads.get(thread.key)
        assert current is not None
        current.pending = [*ids, *(i for i in current.pending if i not in ids)]
        self.repo.threads.save(current)
        return decision

    def _holding(self, trigger_id: str) -> Thread | None:
        trigger = self.repo.triggers.get(trigger_id)
        thread = self.repo.threads.get(thread_key(trigger.threads[0])) if trigger else None
        if thread and thread.evaluating and trigger_id in thread.evaluating.trigger_ids:
            return thread
        return None

    def _candidate_of(self, thread: Thread) -> Candidate:
        evaluation = thread.evaluating
        assert evaluation
        triggers = [t for i in evaluation.trigger_ids if (t := self.repo.triggers.get(i)) is not None]
        return Candidate(eval_id=evaluation.id, thread=thread, triggers=triggers, strength=evaluation.strength,
                         policy_version=evaluation.policy_version, tickets=self.tickets_on_thread(thread))

    def _ignore(self, thread: Thread, ids: list[str], reasons: list[Reason], policy: Policy, at) -> None:
        """Record that queued Triggers start no Eval, and take them out of the queue."""
        evaluation = Evaluation(
            id=key("eval", thread.key, "ignored", policy.version, at, *ids), thread_key=thread.key, trigger_ids=ids,
            strength="inferred", actor=ActorRef(source=thread.ref.source, id=""), basis=MandateBasis(via="none"),
            policy_version=policy.version, started_at=at, deadline=at)
        self._record(thread, evaluation, "ignore", reasons)
        thread.pending = [i for i in thread.pending if i not in ids]
        thread.expired = 0

    def _accepted(self, key_: str) -> dict[str, Decision]:
        """The accept Decisions of the Triggers whose primary Thread is `key_`, by Trigger."""
        accepted: dict[str, Decision] = {}
        for decision in self.repo.decisions.by_thread(key_):
            if decision.kind != "accept" or not decision.ticket_id:
                continue
            for trigger_id in decision.trigger_ids:
                accepted.setdefault(trigger_id, decision)
        return accepted

    def _direct_eval(self, thread: Thread, trigger: Trigger, kind: str, ticket_id: str | None) -> Evaluation:
        """A gate-only Eval of one Trigger, decided at once beside an Eval the Thread may have in progress."""
        policy = self.policy()
        gate = policy.gate(trigger, thread, self.tickets_on_thread(thread))
        if not isinstance(gate, Admit):
            raise UserError("notAdmissible", {"reason": gate.reason})
        at = self.now()
        if trigger.id in thread.pending:
            thread.pending = [i for i in thread.pending if i != trigger.id]
            self.repo.threads.save(thread)
        eval_id = key("eval", thread.key, "direct", trigger.id, kind, *([ticket_id] if ticket_id else []), at)
        return Evaluation(id=eval_id, thread_key=thread.key, trigger_ids=[trigger.id], primary=trigger.id,
                          strength=gate.strength, actor=trigger.actor, basis=gate.basis,
                          policy_version=policy.version, started_at=at, deadline=at)

    def _decide_eval(self, thread: Thread, evaluation: Evaluation, outcome: Outcome) -> Decision:
        audit = outcome.audit
        # Decided under an older Policy, such as across a restart: judge it again.
        if self.policy().version != evaluation.policy_version:
            if thread.evaluating and thread.evaluating.id == evaluation.id:
                return self._reassess(thread, Reason(code="policyChanged"))
            return self._finish(thread, evaluation, "unavailable", [Reason(code="policyChanged")], audit=audit)
        if isinstance(outcome, (Ignore, Unavailable)):
            unassessed = [Reason(code="ignoredFromCard")] if isinstance(outcome, Ignore) \
                and evaluation.strength != "explicit" and audit is None else []
            return self._finish(thread, evaluation, outcome.kind, [*unassessed, *_reasons(outcome.reason)],
                                audit=audit)
        if isinstance(outcome, Stop):
            ticket = self.repo.tickets.get(outcome.ticket_id)
            if ticket is None or not live_ticket(ticket):
                return self._finish(thread, evaluation, "ignore", [Reason(code="stopUnknown")], audit=audit)
            if evaluation.strength != "explicit":
                return self._finish(thread, evaluation, "ignore", [Reason(code="stopNotExplicit")], audit=audit)
            decision = self._finish(thread, evaluation, "stop", _reasons(outcome.reason), audit=audit,
                                    ticket_id=ticket.id)
            self.tickets.end(ticket.id, "human", "stopped", self.now(),
                             outcome.reason if isinstance(outcome.reason, str) else None)
            return decision
        return self._accept(thread, evaluation, outcome)

    def _accept(self, thread: Thread, evaluation: Evaluation, outcome: Accept) -> Decision:
        at = self.now()
        basis = evaluation.basis.model_copy(update={"rules": tuple(outcome.rules)}) if outcome.rules \
            else evaluation.basis
        mandate = Mandate(strength=evaluation.strength, policy_version=evaluation.policy_version, basis=basis,
                          actor=evaluation.actor, scope=outcome.scope)
        ticket_id = key("ticket", evaluation.id)
        decision = self._finish(thread, evaluation, "accept", _reasons(outcome.reason), audit=outcome.audit,
                                mandate=mandate, ticket_id=ticket_id)
        self.tickets.create(ticket_id, decision.id, mandate, evaluation, outcome, at)
        return decision

    def _finish(self, thread: Thread, evaluation: Evaluation, kind: DecisionKind, reasons: list[Reason], *,
                audit=None, ticket_id: str | None = None, mandate: Mandate | None = None) -> Decision:
        """Record an Eval's Decision; the Thread's own Eval ends with it, a gate-only one leaves it going."""
        decision = self._record(thread, evaluation, kind, reasons, audit=audit, ticket_id=ticket_id, mandate=mandate)
        if thread.evaluating is None or thread.evaluating.id != evaluation.id:
            return decision
        thread.evaluating = None
        thread.expired = 0
        self.repo.threads.save(thread)
        self.options.assessor.release(evaluation.id)
        return decision

    def _record(self, thread: Thread, evaluation: Evaluation, kind: DecisionKind, reasons: list[Reason], *,
                audit=None, ticket_id: str | None = None, mandate: Mandate | None = None) -> Decision:
        at = self.now()
        decision = Decision(id=key("decision", evaluation.id), eval_id=evaluation.id, thread_key=thread.key,
                            trigger_ids=list(evaluation.trigger_ids), policy_version=evaluation.policy_version, at=at,
                            kind=kind, reason=reasons, strength=evaluation.strength, ticket_id=ticket_id,
                            mandate=mandate, audit=audit)
        self.repo.decisions.save(decision)
        for trigger_id in evaluation.trigger_ids:
            disposition = self.disposition(trigger_id)
            disposition.eval_id = evaluation.id
            disposition.decision_id = decision.id
            self.repo.triggers.save_disposition(trigger_id, disposition)
        self.emit("decision.recorded", decision_id=decision.id, kind=kind, thread_key=thread.key)
        return decision
