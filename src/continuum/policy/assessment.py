"""Turning a model's assessment of a proactive candidate into an Engine outcome.

The model judges; this code checks that the judgement is complete and holds it to the Policy: every enabled rule
judged once, a settled direction, at least one matched rule, evidence of intent, and auto mode for an inferred accept.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from continuum.core.model import Strength
from continuum.core.ports import Accept, Ignore, Outcome, Unavailable
from continuum.core.reasons import Reason

from .config import PolicyConfig


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleAssessment(_Strict):
    rule_id: str
    matched: bool
    reason: str = Field(min_length=1, max_length=500)


class Direction(_Strict):
    implementation_requested: bool
    target_basis: Literal["specified", "existing-standard", "undetermined"]
    requires_design_decision: bool
    evidence: str = Field(min_length=1, max_length=700)


class Assessment(_Strict):
    decision: Literal["accept", "ignore", "unavailable"]
    rule_assessments: list[RuleAssessment]
    # Free-form on purpose: a malformed direction is an unsettled one, not an error.
    direction: Any = None
    title: str = ""
    scope: str = ""
    reason: str
    intent_evidence: str | None = None
    acknowledgement: str | None = None
    name: str | None = None


class AssessmentError(ValueError):
    pass


def direction_allowed(value: Any) -> bool:
    """Implementation was requested, the target is specified or an existing standard, and no decision remains."""
    try:
        direction = Direction.model_validate(value)
    except ValidationError:
        return False
    return direction.implementation_requested and direction.target_basis != "undetermined" \
        and not direction.requires_design_decision


def assessment_outcome(assessment: Assessment, config: PolicyConfig, strength: Strength) -> Outcome:
    enabled = config.enabled_rules()
    judged = assessment.rule_assessments
    ids = {item.rule_id for item in judged}
    if len(judged) != len(enabled) or len(ids) != len(judged) or any(r.id not in ids for r in enabled):
        raise AssessmentError("Exactly one assessment for every enabled rule is required")
    matches = [{"rule_id": i.rule_id, "reason": i.reason} for i in judged if i.matched]
    audit: dict = {"matches": matches, "rule_assessments": [i.model_dump() for i in judged],
                   "direction": assessment.direction}
    if assessment.intent_evidence:
        audit["intent_evidence"] = assessment.intent_evidence
    if assessment.decision == "ignore":
        return Ignore(reason=assessment.reason, audit=audit)
    if assessment.decision == "unavailable":
        return Unavailable(reason=assessment.reason, audit=audit)
    if not direction_allowed(assessment.direction):
        return Ignore(reason=Reason(code="directionUnsettled"), audit=audit)
    if not matches:
        return Ignore(reason=Reason(code="noRuleMatched"), audit=audit)
    if not assessment.intent_evidence:
        raise AssessmentError("Acceptance requires intent_evidence")
    if strength == "inferred" and config.mode != "auto":
        proposal = {"title": assessment.title, "scope": assessment.scope, "name": assessment.name}
        return Ignore(reason=Reason(code="observed", values={"text": assessment.reason}),
                      audit={**audit, "would_accept": True, "proposal": proposal})
    return Accept(title=assessment.title, scope=assessment.scope, reason=assessment.reason,
                  acknowledgement=assessment.acknowledgement, rules=[m["rule_id"] for m in matches], audit=audit,
                  name=assessment.name)
