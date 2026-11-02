"""The Policy: the standard gate, a gate rule chain, assessment checks and change effects."""

from datetime import UTC, datetime

import pytest

from continuum.core import Accept, ActorRef, Admit, Ignore, Mandate, MandateBasis, Refuse, Thread, ThreadRef, Ticket
from continuum.policy import Allowed, AdmissionRow, Assessment, AssessmentError, PolicyConfig, Rule, RulePolicy, \
    SourcePolicy, StandardGate, assessment_outcome, policy_effects

from conftest import GUEST, MEMBER, FakeScope, Harness, trigger

AT = datetime(2026, 9, 23, tzinfo=UTC)
COPY = Rule(id="copy", name="Copy fixes", instruction="Fix typos when asked.")


def config(**overrides) -> PolicyConfig:
    values = dict(rules=(COPY,), sources={"chat": SourcePolicy(allowed=(Allowed(id="Alice"),),
                                                               bots=(Allowed(id="ci-bot"),))})
    values.update(overrides)
    return PolicyConfig(**values)


def policy(**overrides) -> RulePolicy:
    return RulePolicy(config(**overrides), FakeScope())


def thread(subscriptions=()) -> Thread:
    return Thread(key="chat:t1", ref=ThreadRef(source="chat", id="t1"), created_at=AT, subscriptions=list(subscriptions))


# --- the standard gate --------------------------------------------------------------------------------------------


def test_an_allowed_sender_addressing_the_agent_is_explicit_and_names_case_insensitively():
    result = policy().gate(trigger("1", addressed="mention"), thread(), [])
    assert isinstance(result, Admit) and result.strength == "explicit"
    assert (result.basis.via, result.basis.rule, result.basis.sender) == ("addressed", "standard", "Alice")


def test_unlisted_senders_follow_the_admission_table():
    request = trigger("1", actor=GUEST, addressed="mention", meta={"row": "support"})
    assert policy().gate(request, thread(), []) == Refuse(reason="not_a_member")
    table = {"support": AdmissionRow(unlisted_addressed="explicit")}
    opened = policy(sources={"chat": SourcePolicy(admission=table)})
    assert opened.gate(request, thread(), []).strength == "explicit"


def test_unaddressed_messages_need_a_scope_that_receives_every_message_and_an_accept_mode():
    quiet = trigger("1")
    assert policy().gate(quiet, thread(), []) == Refuse(reason="no_mention")
    observed = trigger("1", meta={"observed": True})
    result = policy().gate(observed, thread(), [])
    assert result.strength == "inferred" and result.basis.observation == "observe"
    assert isinstance(policy(mode="off").gate(observed, thread(), []), Refuse)


def test_a_follow_up_takes_the_senders_strength_except_in_a_direct_message():
    busy = thread(subscriptions=["ticket-1"])
    assert policy().gate(trigger("1"), busy, []).basis.via == "followup"
    assert policy().gate(trigger("1"), busy, []).strength == "explicit"
    assert policy().gate(trigger("1", actor=GUEST), busy, []).strength == "inferred"
    assert isinstance(policy(mode="off").gate(trigger("1", actor=GUEST), busy, []), Refuse)
    assert isinstance(policy().gate(trigger("1", actor=GUEST, addressed="direct"), busy, []), Refuse)


def test_bots_make_inferred_candidates_at_most_and_only_when_listed():
    stranger = trigger("1", human=False, actor=ActorRef(source="chat", id="spam-bot"), addressed="mention")
    assert policy().gate(stranger, thread(), []) == Refuse(reason="not_human")
    listed = trigger("1", human=False, actor=ActorRef(source="chat", id="CI-bot"), addressed="mention")
    result = policy().gate(listed, thread(), [])
    assert result.strength == "inferred" and result.basis.via == "bot"
    assert isinstance(policy(mode="off").gate(listed, thread(), []), Refuse)


def test_revalidation_follows_the_basis():
    p = policy()
    addressed = Mandate(strength="explicit", policy_version=p.version, actor=MEMBER, scope="x",
                        basis=MandateBasis(via="addressed", source="chat", scope={}))
    assert p.revalidate(addressed)
    assert not policy(sources={}).revalidate(addressed)  # the sender left the list
    assert not p.revalidate(addressed.model_copy(update={"basis": addressed.basis.model_copy(
        update={"scope": {"blocked": True}})}))  # the Source no longer listens there
    bot = Mandate(strength="inferred", policy_version=p.version, actor=ActorRef(source="chat", id="ci-bot"), scope="x",
                  basis=MandateBasis(via="bot", source="chat", scope={}, observation="auto", rules=("copy",)))
    assert not p.revalidate(bot)  # observe mode
    assert policy(mode="auto").revalidate(bot)
    assert not policy(mode="auto", rules=(COPY.model_copy(update={"enabled": False}),)).revalidate(bot)


def test_admits_an_inferred_accept_only_in_auto_mode_on_enabled_rules():
    assert not policy().admits_inferred(["copy"])
    assert policy(mode="auto").admits_inferred(["copy"])
    assert not policy(mode="auto").admits_inferred([])
    assert not policy(mode="auto").admits_inferred(["unknown"])


# --- the gate rule chain ------------------------------------------------------------------------------------------


class VipGate:
    """A plugin rule: VIPs are explicit even without addressing the agent."""

    name = "vip"

    def gate(self, t, thread, tickets, env):
        if t.human and t.actor.id == "boss":
            return Admit(strength="explicit", basis=MandateBasis(via="vip", source=t.source))
        return None

    def revalidate(self, mandate, env):
        return mandate.actor.id == "boss"


class QuietHours:
    """A plugin rule that refuses everything flagged as after hours."""

    name = "quiet-hours"

    def gate(self, t, thread, tickets, env):
        return Refuse(reason="quiet_hours") if t.meta.get("after_hours") else None

    def revalidate(self, mandate, env):
        return True


def test_the_first_rule_that_does_not_abstain_decides_and_later_revalidates():
    chain = RulePolicy(config(), FakeScope(), [QuietHours(), VipGate(), StandardGate()])
    boss = trigger("1", actor=ActorRef(source="chat", id="boss"))
    admitted = chain.gate(boss, thread(), [])
    assert admitted.strength == "explicit" and admitted.basis.rule == "vip"
    assert chain.gate(trigger("1", addressed="mention", meta={"after_hours": True}), thread(), []) \
        == Refuse(reason="quiet_hours")
    assert chain.gate(trigger("1", addressed="mention"), thread(), []).basis.rule == "standard"
    mandate = Mandate(strength="explicit", policy_version=chain.version, actor=boss.actor, scope="x",
                      basis=admitted.basis)
    assert chain.revalidate(mandate)
    # A Mandate whose rule left the chain no longer holds.
    assert not RulePolicy(config(), FakeScope()).revalidate(mandate)
    assert chain.version != RulePolicy(config(), FakeScope()).version


def test_duplicate_gate_rule_names_are_refused():
    with pytest.raises(ValueError):
        RulePolicy(config(), FakeScope(), [StandardGate(), StandardGate()])


# --- assessment ---------------------------------------------------------------------------------------------------


def assessment(**overrides) -> Assessment:
    values = dict(decision="accept", reason="a typo fix was asked for", title="Fix typo", scope="one word",
                  intent_evidence="'just say Save'", rule_assessments=[dict(rule_id="copy", matched=True,
                                                                            reason="a wording fix")],
                  direction=dict(implementation_requested=True, target_basis="specified",
                                 requires_design_decision=False, evidence="'Save'"))
    values.update(overrides)
    return Assessment.model_validate(values)


def test_an_assessment_must_judge_every_enabled_rule_once():
    with pytest.raises(AssessmentError):
        assessment_outcome(assessment(rule_assessments=[]), config(mode="auto"), "inferred")


def test_an_unsettled_direction_or_no_matched_rule_is_ignored():
    unsettled = assessment(direction=dict(implementation_requested=True, target_basis="undetermined",
                                          requires_design_decision=False, evidence="x"))
    assert assessment_outcome(unsettled, config(mode="auto"), "inferred").reason.code == "directionUnsettled"
    unmatched = assessment(rule_assessments=[dict(rule_id="copy", matched=False, reason="not wording")])
    assert assessment_outcome(unmatched, config(mode="auto"), "inferred").reason.code == "noRuleMatched"


def test_observe_mode_records_what_it_would_accept_and_auto_mode_accepts():
    observed = assessment_outcome(assessment(), config(), "inferred")
    assert isinstance(observed, Ignore) and observed.audit["would_accept"] is True
    accepted = assessment_outcome(assessment(), config(mode="auto"), "inferred")
    assert isinstance(accepted, Accept) and accepted.rules == ["copy"]
    # An explicit request is not held back by observe mode.
    assert isinstance(assessment_outcome(assessment(), config(), "explicit"), Accept)


# --- policy change effects ----------------------------------------------------------------------------------------


def ticket(mandate: Mandate) -> Ticket:
    from continuum.core import Origin
    return Ticket(id="t", title="x", decision_id="d", mandate=mandate, created_at=AT, updated_at=AT,
                  origin=Origin(trigger_id="1", trigger_ids=["1"]))


def test_policy_effects_name_how_a_change_bears_on_each_ticket():
    scope = lambda c: FakeScope()  # noqa: E731
    old = config(mode="auto")
    inferred = ticket(Mandate(strength="inferred", policy_version="v", actor=MEMBER, scope="x",
                              basis=MandateBasis(via="admission", cell="listed_unaddressed", source="chat",
                                                 scope={"observed": True}, observation="auto", rules=("copy",))))
    changed = config(mode="auto", rules=(COPY.model_copy(update={"instruction": "Fix typos and grammar."}),))
    [rule] = policy_effects([inferred], old, changed, scope)["t"].rules
    assert (rule.change, rule.instruction) == ("changed", "Fix typos and grammar.")
    assert policy_effects([inferred], old, config(), scope)["t"].proactive is False
    removed = policy_effects([inferred], old, config(mode="auto", sources={}), scope)["t"]
    assert removed.requester_allowed is False and removed.requester == MEMBER
    assert policy_effects([inferred], old, old, scope) == {}


# --- the real Policy driving the Engine ---------------------------------------------------------------------------


def test_a_proactive_candidate_is_only_observed_until_auto_mode():
    h = Harness(policy=policy())
    h.engine.receive(trigger("1", meta={"observed": True}, text="the button says Sve, just say Save"))
    h.pump()
    [candidate] = h.requests
    assert candidate.strength == "inferred"
    outcome = assessment_outcome(assessment(), h.policy.config, candidate.strength)
    decision = h.decide(candidate, outcome)
    assert decision.kind == "ignore" and decision.audit["would_accept"] and h.repo.tickets.list() == []

    old = h.policy.version
    h.policy = policy(mode="auto")
    h.engine.policy_changed(old, h.policy.version)
    h.engine.reevaluate("1")
    h.pump()
    candidate = h.requests[-1]
    decision = h.decide(candidate, assessment_outcome(assessment(), h.policy.config, candidate.strength))
    assert decision.kind == "accept"
    assert decision.mandate.basis.rules == ("copy",) and decision.mandate.basis.observation == "auto"


def test_a_malformed_direction_is_unsettled_not_an_error():
    odd = assessment(direction="settled, trust me")
    assert assessment_outcome(odd, config(mode="auto"), "inferred").reason.code == "directionUnsettled"
