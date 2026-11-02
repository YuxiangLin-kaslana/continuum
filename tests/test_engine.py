"""Domain rules of the Engine, ported from dsh-superagent's core-engine tests."""

from datetime import timedelta

import pytest

from continuum.core import Accept, Ignore, Stop, ThreadRef, TriggerInput, UserError, WorkerReport, followup_owners, \
    ticket_stage

from conftest import BASE, GUEST, Harness, trigger


# --- intake -------------------------------------------------------------------------------------------------------


def test_stores_a_trigger_once_and_records_source_filtering(h: Harness):
    assert h.engine.receive(trigger("1")) == "stored"
    assert h.engine.receive(trigger("1")) == "duplicate"
    assert h.engine.receive(trigger("2", meta={"blocked": True})) == "filtered"
    assert h.repo.triggers.disposition("2").filtered.code == "chat not allowed"
    assert h.repo.threads.get("chat:t1").pending == ["1"]


def test_records_correlation_and_evaluates_only_on_the_primary_thread(h: Harness):
    h.engine.receive(trigger("1", threads=(ThreadRef(source="chat", id="a"), ThreadRef(source="chat", id="b"))))
    assert h.repo.threads.get("chat:a").correlated == ["chat:b"]
    assert h.repo.threads.get("chat:b").correlated == ["chat:a"]
    assert (h.repo.threads.get("chat:a").pending, h.repo.threads.get("chat:b").pending) == (["1"], [])


# --- eval ---------------------------------------------------------------------------------------------------------


def test_ignores_at_the_gate_without_an_assessor_when_nothing_is_admissible(h: Harness):
    h.engine.receive(trigger("1", human=False))
    h.pump()
    assert h.requests == []
    [decision] = h.decisions()
    assert decision.kind == "ignore" and decision.reason[0].code == "not human"


def test_serializes_evals_per_thread_and_batches_triggers_that_arrive_meanwhile(h: Harness):
    h.engine.receive(trigger("1"))
    h.pump()
    h.engine.receive(trigger("2"))
    h.engine.receive(trigger("3"))
    h.pump()
    assert len(h.requests) == 1
    h.decide(h.requests[0], Ignore(reason="chat"))
    h.pump()
    assert [t.id for t in h.requests[1].triggers] == ["2", "3"]
    assert h.released == [h.requests[0].eval_id]


def test_accept_creates_ticket_mandate_subscription_acknowledgement_and_assignment(h: Harness):
    decision = h.accepted()
    ticket = h.ticket(decision.ticket_id)
    assert ticket.mandate.strength == "explicit" and ticket.mandate.policy_version == "v1"
    assert ticket.mandate.scope == "small"
    assert h.repo.threads.get("chat:t1").subscriptions == [ticket.id]
    assert [(m.kind, m.text) for m in h.outbox] == [("acknowledgement", "ok")]
    assert [a.state for a in ticket.assignments] == ["running"]
    assert [e.type for e in h.events if e.type.startswith("ticket")] == ["ticket.created"]


def test_refuses_to_decide_an_eval_that_a_later_eval_replaced(h: Harness):
    h.engine.receive(trigger("1"))
    h.pump()
    first = h.requests[0]
    h.engine.policy_changed("v1", "v1")  # restarts the Eval in progress
    h.tick(1)
    h.pump()
    with pytest.raises(UserError) as error:
        h.decide(first, Ignore(reason="old"))
    assert error.value.code == "evalFinished"
    assert h.decide(h.requests[1], Ignore(reason="new")).kind == "ignore"


def test_an_accept_records_everything_together_or_nothing():
    def broken(ticket, outcome):
        raise RuntimeError("prepare failed")

    h = Harness(prepare_ticket=broken)
    h.engine.receive(trigger("1"))
    h.pump()
    with pytest.raises(RuntimeError):
        h.decide(h.requests[0], Accept(title="Fix", scope="small", reason="requested", acknowledgement="ok"))
    assert (h.repo.tickets.list(), h.decisions(), h.outbox) == ([], [], [])
    assert h.repo.threads.get("chat:t1").evaluating is not None


def test_records_unavailable_and_queues_again_when_the_policy_changed_during_assessment(h: Harness):
    h.engine.receive(trigger("1"))
    h.pump()
    h.policy.version = "v2"
    decision = h.decide(h.requests[0], Accept(title="Fix", scope="small", reason="requested"))
    assert decision.kind == "unavailable" and decision.reason[0].code == "policyChanged"
    assert h.repo.threads.get("chat:t1").pending == ["1"]
    h.pump()
    assert h.requests[1].policy_version == "v2"


def test_counts_the_deadline_from_delivery_queues_again_twice_then_records_unavailable(h: Harness):
    h.engine.receive(trigger("1"))
    h.pump()
    h.tick(600)
    h.pump()  # not delivered yet: it never expires
    assert len(h.requests) == 1
    for _ in range(3):
        h.engine.candidate_delivered(h.requests[-1].eval_id)
        h.tick(61)
        h.pump()
    kinds = [d.kind for d in h.decisions()]
    assert len(h.requests) == 3 and kinds == ["unavailable"]
    assert h.decisions()[0].reason[0].code == "timeout"


# --- subscription fan-out -----------------------------------------------------------------------------------------


def test_a_persons_later_message_goes_to_the_live_ticket_as_context_and_not_to_eval(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.receive(trigger("2", actor=GUEST))
    h.pump()
    ticket = h.ticket(ticket_id)
    assert [(i.kind, getattr(i, "trigger_id", None)) for i in ticket.inputs] == [("trigger", "2")]
    assert ticket.undelivered == [] and h.delivered == [(ticket_id, ticket.inputs[0].id)]
    assert h.repo.triggers.disposition("2").fanout == [ticket_id]
    assert len(h.requests) == 1 and len(h.decisions()) == 1
    # A bot's message there still goes to Eval, which the gate does not admit.
    h.engine.receive(trigger("3", human=False))
    h.pump()
    assert h.decisions()[-1].kind == "ignore" and h.decisions()[-1].trigger_ids == ["3"]


def test_keeps_inputs_undelivered_until_an_assignment_runs(h: Harness):
    h.engine.receive(trigger("1"))
    h.pump()
    ticket_id = h.decide(h.requests[0], Accept(title="Fix", scope="small", reason="requested")).ticket_id
    h.settle(launch=False)
    h.engine.receive(trigger("2"))
    h.engine.evaluate_due()
    h.settle(launch=False)
    ticket = h.ticket(ticket_id)
    assert len(ticket.undelivered) == 1 and h.delivered == []
    assert h.starts == 2  # the pending launch is asked for again
    h.engine.assignment_started(ticket_id, ticket.assignments[0].id)
    h.settle(launch=False)
    assert h.delivered == [(ticket_id, ticket.undelivered[0])]


def test_a_bots_message_does_not_reach_an_ended_ticket(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.report(ticket_id, WorkerReport(state="ready", summary="done"))
    h.engine.receive(trigger("2", human=False))
    h.pump()
    ticket = h.ticket(ticket_id)
    assert ticket.ended and ticket.undelivered == [] and h.starts == 1


# --- resume and stop ----------------------------------------------------------------------------------------------


def test_resumes_an_ended_ticket_on_a_persons_message_without_a_decision(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.report(ticket_id, WorkerReport(state="ready", summary="done"))
    h.engine.receive(trigger("2", actor=GUEST))
    h.pump()
    h.pump()
    ticket = h.ticket(ticket_id)
    assert ticket.ended is None and len(ticket.assignments) == 2
    assert [i.kind for i in ticket.inputs] == ["trigger", "resumed"]
    # The resume note goes ahead of the message.
    assert [i for _, i in h.delivered] == [ticket.inputs[1].id, ticket.inputs[0].id]
    assert len(h.decisions()) == 1


def test_drops_what_waited_when_the_policy_no_longer_holds_the_mandate(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.report(ticket_id, WorkerReport(state="ready", summary="done"))
    h.policy.version = "revoked"
    h.engine.receive(trigger("2"))
    h.pump()
    ticket = h.ticket(ticket_id)
    assert ticket.ended and ticket.undelivered == []
    # It reached no ticket that can take it, so it went to Eval.
    assert h.repo.triggers.disposition("2").fanout == []
    assert h.requests[-1].triggers[0].id == "2"


def test_a_ticket_a_person_stopped_stays_ended_and_receives_nothing(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.stop_ticket(ticket_id, "enough")
    h.engine.receive(trigger("2"))
    h.pump()
    ticket = h.ticket(ticket_id)
    assert ticket.ended.by == "human" and ticket.ended.outcome == "stopped" and ticket.inputs == []
    assert h.requests[-1].triggers[0].id == "2"
    with pytest.raises(UserError) as error:
        h.engine.attach(ticket_id, "2")
    assert error.value.code == "ticketStopped"


def test_stops_a_live_ticket_only_on_an_explicit_request(h: Harness):
    ticket_id = h.accepted().ticket_id
    # A guest's message on the Thread is the ticket's follow-up: its worker decides it, but a stop may be routed.
    h.engine.receive(trigger("2", actor=GUEST))
    decision = h.engine.decide("2", Stop(ticket_id=ticket_id, reason="stop"))
    assert decision.kind == "ignore" and decision.reason[0].code == "stopNotExplicit"
    assert h.ticket(ticket_id).ended is None
    h.engine.receive(trigger("3"))
    decision = h.engine.decide("3", Stop(ticket_id=ticket_id, reason="please stop"))
    assert decision.kind == "stop"
    assert h.ticket(ticket_id).ended.by == "human" and h.stops


# --- follow-ups and hand-backs ------------------------------------------------------------------------------------


def test_a_follow_up_belongs_to_the_workers_and_the_router_may_only_stop(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.receive(trigger("2"))
    assert followup_owners(h.repo.triggers.get("2"), h.repo) == [ticket_id]
    with pytest.raises(UserError) as error:
        h.engine.decide("2", Accept(title="More", scope="x", reason="new"))
    assert error.value.code == "workerDecides"


def test_a_hand_back_queues_the_message_for_a_fresh_eval_once_and_keeps_the_subscription(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.receive(trigger("2"))
    h.pump()
    assert h.engine.handback(ticket_id, "2", "a different feature") is True
    with pytest.raises(UserError) as error:
        h.engine.handback(ticket_id, "2", "again")
    assert error.value.code == "handedBack"
    h.pump()
    assert h.requests[-1].triggers[0].id == "2" and h.requests[-1].strength == "explicit"
    assert h.repo.threads.get("chat:t1").subscriptions == [ticket_id]
    with pytest.raises(UserError) as error:
        h.engine.attach(ticket_id, "2")
    assert error.value.code == "handedBackHere"


def test_a_follow_up_the_worker_never_received_goes_to_eval_when_the_ticket_ends(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.receive(trigger("2"))  # not delivered: no settle
    h.engine.report(ticket_id, WorkerReport(state="ready", summary="done"))
    assert h.repo.threads.get("chat:t1").pending == ["2"]


def test_a_follow_up_read_after_the_last_report_goes_to_eval_unless_acknowledged(h: Harness):
    from continuum.core import ReplyRequest
    ticket_id = h.accepted().ticket_id
    h.engine.receive(trigger("2"))
    h.engine.receive(trigger("3"))
    h.settle()
    h.read(ticket_id)
    h.engine.reply(ReplyRequest(purpose="acknowledgement", ticket_id=ticket_id, trigger_id="2", text="on it"))
    # The read happened after no report: both are unread at the end, but 2 was taken up.
    h.engine.report(ticket_id, WorkerReport(state="ready", summary="done"))
    assert h.repo.threads.get("chat:t1").pending == ["3"]


# --- Threads followed for context, notes, attach ------------------------------------------------------------------


def test_a_thread_followed_for_context_reaches_the_ticket_but_lends_nothing(h: Harness):
    ticket_id = h.accepted().ticket_id
    assert h.engine.follow_for_context(ticket_id, ThreadRef(source="chat", id="other")) is True
    h.engine.receive(trigger("2", thread="other"))
    h.pump()
    [item] = h.ticket(ticket_id).inputs
    assert isinstance(item, TriggerInput) and item.context
    # It is none of the ticket's follow-ups, so it went to Eval too.
    assert followup_owners(h.repo.triggers.get("2"), h.repo) == []
    assert h.requests[-1].triggers[0].id == "2"
    # An ended ticket is not resumed from a Thread it followed for context.
    h.engine.report(ticket_id, WorkerReport(state="ready", summary="done"))
    h.engine.receive(trigger("3", thread="other"))
    h.pump()
    assert h.ticket(ticket_id).ended is not None


def test_a_note_is_held_like_automatic_input_and_refused_for_an_ended_ticket(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.report(ticket_id, WorkerReport(state="blocked", summary="need a key"))
    h.engine.note(ticket_id, "the key is in the vault")
    assert h.ticket(ticket_id).undelivered == []
    h.engine.stop_ticket(ticket_id)
    with pytest.raises(UserError):
        h.engine.note(ticket_id, "late")


def test_attach_routes_a_message_to_a_ticket_and_ends_the_eval_that_held_only_it(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.receive(trigger("2", thread="t2"))
    h.pump()
    ticket = h.engine.attach(ticket_id, "2", note="same bug")
    assert any(getattr(i, "trigger_id", None) == "2" and i.note == "same bug" for i in ticket.inputs)
    assert ticket_id in h.repo.threads.get("chat:t2").subscriptions
    assert h.decisions("chat:t2")[-1].reason[0].code == "routed"
    assert h.repo.threads.get("chat:t2").evaluating is None


# --- direct decisions ---------------------------------------------------------------------------------------------


def test_decides_an_explicit_request_directly_and_accepts_a_trigger_once(h: Harness):
    h.engine.receive(trigger("1", thread="t2"))
    first = h.engine.decide("1", Accept(title="Fix", scope="small", reason="requested"))
    again = h.engine.decide("1", Accept(title="Fix", scope="small", reason="requested"))
    assert first.kind == "accept" and again.id == first.id
    assert len(h.repo.tickets.list()) == 1
    assert h.repo.threads.get("chat:t2").pending == []


def test_accepts_an_inferred_candidate_only_on_rules_the_policy_admits(h: Harness):
    h.accepted()
    h.engine.receive(trigger("2", thread="t1", actor=GUEST))
    h.engine.handback(h.repo.tickets.list()[0].id, "2", "new work")
    h.pump()
    candidate = h.requests[-1]
    assert candidate.strength == "inferred"
    with pytest.raises(UserError) as error:
        h.decide(candidate, Accept(title="New", scope="x", reason="seems useful"))
    assert error.value.code == "notExplicit"
    decision = h.decide(candidate, Accept(title="New", scope="x", reason="copy fix", rules=["copy"]))
    assert decision.kind == "accept" and decision.mandate.basis.rules == ("copy",)


# --- automatic input and reports ----------------------------------------------------------------------------------


def test_holds_back_automatic_input_from_a_blocked_worker_until_a_person_writes(h: Harness):
    ticket_id = h.accepted().ticket_id
    h.engine.report(ticket_id, WorkerReport(state="blocked", summary="need a decision"))
    h.engine.receive(trigger("2", human=False))
    h.settle()
    assert h.ticket(ticket_id).undelivered == [] and h.delivered == []
    assert ticket_stage(h.ticket(ticket_id)).action == "blocked"
    h.tick(1)
    h.engine.receive(trigger("3"))
    h.settle()
    assert [t for t, _ in h.delivered] == [ticket_id]
    assert ticket_stage(h.ticket(ticket_id)).stage == "working"


def test_asks_keeps_replaces_and_clears_the_question_for_a_person(h: Harness):
    ticket_id = h.accepted().ticket_id
    asked = h.engine.report(ticket_id, WorkerReport(state="progress", summary="x", question="A or B?"))
    first = asked.attention.id
    assert h.engine.report(ticket_id, WorkerReport(state="progress", summary="y")).attention.id == first
    replaced = h.engine.report(ticket_id, WorkerReport(state="progress", summary="z", question="C or D?"))
    assert replaced.attention.id != first and len(h.withdrawn) == 1
    cleared = h.engine.report(ticket_id, WorkerReport(state="progress", summary="w", clear_question=True))
    assert cleared.attention is None and len(h.withdrawn) == 2
    with pytest.raises(UserError) as error:
        h.engine.report(ticket_id, WorkerReport(state="ready", summary="done", question="?"))
    assert error.value.code == "questionWithResult"


def test_a_final_report_ends_the_ticket_unless_a_followed_thread_is_open(h: Harness):
    dropped = h.accepted("1", thread="a").ticket_id
    h.engine.report(dropped, WorkerReport(state="dropped", summary="not reproducible"))
    assert h.ticket(dropped).ended.outcome == "dropped"

    ready = h.accepted("2", thread="b").ticket_id
    h.engine.bind_thread(ready, ThreadRef(source="git", id="pr-7"), own=True)
    h.engine.report(ready, WorkerReport(state="ready", summary="PR open"))
    assert h.ticket(ready).ended is None
    assert ticket_stage(h.ticket(ready)).action == "review"
    h.tick(30)
    h.engine.thread_closed(ThreadRef(source="git", id="pr-7"), "merged", BASE + timedelta(seconds=20))
    assert h.ticket(ready).ended.outcome == "merged"

    plain = h.accepted("3", thread="c").ticket_id
    h.engine.report(plain, WorkerReport(state="ready", summary="done"))
    assert h.ticket(plain).ended.outcome == "completed"
    with pytest.raises(UserError) as error:
        h.engine.report(plain, WorkerReport(state="progress", summary="late"))
    assert error.value.code == "ticketEnded"


# --- replies ------------------------------------------------------------------------------------------------------


def test_replies_follow_their_rules(h: Harness):
    from continuum.core import ReplyRequest
    ticket_id = h.accepted().ticket_id
    h.engine.receive(trigger("2"))
    h.settle()
    ack = h.engine.reply(ReplyRequest(purpose="acknowledgement", ticket_id=ticket_id, trigger_id="2", text="ok"))
    again = h.engine.reply(ReplyRequest(purpose="acknowledgement", ticket_id=ticket_id, trigger_id="2", text="ok!"))
    assert ack.id == again.id and again.reused
    with pytest.raises(UserError) as error:
        h.engine.reply(ReplyRequest(purpose="answer", ticket_id=ticket_id, trigger_id="2", text="it is X"))
    assert error.value.code == "noAnswer"
    h.engine.report(ticket_id, WorkerReport(state="progress", summary="answered", answers="2"))
    h.engine.reply(ReplyRequest(purpose="answer", ticket_id=ticket_id, trigger_id="2", text="it is X"))
    with pytest.raises(UserError) as error:
        h.engine.reply(ReplyRequest(purpose="result", ticket_id=ticket_id, text="done"))
    assert error.value.code == "notReady"
    h.engine.report(ticket_id, WorkerReport(state="ready", summary="done"))
    h.engine.reply(ReplyRequest(purpose="result", ticket_id=ticket_id, text="done"))
    assert [m.kind for m in h.outbox] == ["acknowledgement", "acknowledgement", "reply", "reply"]


# --- retry, status, restart, pause, policy change -----------------------------------------------------------------


def test_retries_a_failed_ticket_and_asks_only_a_waiting_worker_for_status(h: Harness):
    ticket_id = h.accepted().ticket_id
    with pytest.raises(UserError) as error:
        h.engine.retry_ticket(ticket_id)
    assert error.value.code == "notRetryable"
    assert h.engine.request_status(ticket_id) is False
    h.engine.report(ticket_id, WorkerReport(state="waiting", summary="idle"))
    assert h.engine.request_status(ticket_id) is True
    h.engine.report(ticket_id, WorkerReport(state="failed", summary="crashed"))
    ticket = h.engine.retry_ticket(ticket_id)
    assert [a.state for a in ticket.assignments] == ["ended", "pending"]
    assert ticket.assignments[0].ended_reason == "retried"


def test_an_interrupted_assignment_starts_again_with_a_recovery_note(h: Harness):
    busy = h.accepted("1", thread="a").ticket_id
    idle = h.accepted("2", thread="b").ticket_id
    h.engine.report(idle, WorkerReport(state="waiting", summary="idle"))
    for ticket_id in (busy, idle):
        h.engine.assignment_interrupted(ticket_id, h.ticket(ticket_id).assignments[-1].id)
    restarted = h.ticket(busy)
    assert len(restarted.assignments) == 2 and restarted.assignments[0].ended_reason == "hostRestarted"
    assert restarted.inputs[-1].kind == "text" and "restarted" in restarted.inputs[-1].text
    assert len(h.ticket(idle).assignments) == 1


def test_no_eval_expires_while_paused_and_its_deadline_moves_on(h: Harness):
    h.engine.receive(trigger("1"))
    h.pump()
    h.engine.candidate_delivered(h.requests[0].eval_id)
    deadline = h.repo.threads.get("chat:t1").evaluating.deadline
    h.policy.paused = True
    h.engine.evaluate_due()
    h.tick(600)
    h.engine.evaluate_due()
    assert h.repo.threads.get("chat:t1").evaluating is not None
    h.policy.paused = False
    h.engine.evaluate_due()
    assert h.repo.threads.get("chat:t1").evaluating.deadline == deadline + timedelta(seconds=600)


def test_a_policy_change_tells_only_the_tickets_it_bears_on_and_restarts_evals(h: Harness):
    from continuum.core import PolicyEffect
    told = h.accepted("1", thread="a").ticket_id
    untold = h.accepted("2", thread="b").ticket_id
    h.engine.receive(trigger("3", thread="c"))
    h.pump()
    h.engine.policy_changed("v1", "v2", {told: PolicyEffect(proactive=False)})
    assert [i.kind for i in h.ticket(told).inputs] == ["policy-changed"]
    assert h.ticket(untold).inputs == []
    assert h.decisions("chat:c")[-1].reason[0].code == "policyChanged"
    assert h.repo.threads.get("chat:c").pending == ["3"]


def test_a_worker_drops_an_attached_thread_but_not_its_own(h: Harness):
    ticket_id = h.accepted().ticket_id
    with pytest.raises(UserError) as error:
        h.engine.mark_unrelated(ticket_id, "chat:t1", "mine")
    assert error.value.code == "sourceThread"
    h.engine.receive(trigger("2", thread="t2"))
    h.pump()
    h.engine.attach(ticket_id, "2")
    h.engine.mark_unrelated(ticket_id, "chat:t2", "different topic")
    assert ticket_id not in h.repo.threads.get("chat:t2").subscriptions
    assert h.repo.triggers.disposition("2").handbacks[0].reason == "different topic"
    assert h.repo.threads.get("chat:t2").pending == ["2"]


def test_dismisses_only_an_unavailable_decision(h: Harness):
    h.engine.receive(trigger("1"))
    h.pump()
    decision = h.decide(h.requests[0], Ignore(reason="chat"))
    with pytest.raises(UserError):
        h.engine.dismiss(decision.id)
