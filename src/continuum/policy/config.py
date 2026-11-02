"""Policy configuration: proactive rules, each Source's part (allowed list, bot list, admission table), accept mode."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from continuum.core.ids import key
from continuum.core.model import AcceptMode, ActorRef

AdmissionCell = Literal["listed_unaddressed", "unlisted_addressed", "unlisted_unaddressed"]
Admission = Literal["explicit", "inferred", "ignore"]
# Unaddressed messages from listed senders are inferred candidates; unlisted senders are ignored.
ADMISSION_DEFAULTS: dict[str, Admission] = {
    "listed_unaddressed": "inferred", "unlisted_addressed": "ignore", "unlisted_unaddressed": "ignore",
}
STRENGTH_ORDER: dict[str, int] = {"ignore": 0, "inferred": 1, "explicit": 2}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Allowed(_Frozen):
    id: str = Field(min_length=1, max_length=200)
    name: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.id


class Rule(_Frozen):
    """A kind of proactive work: what may be taken on without anyone asking."""

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    name: str = Field(min_length=1)
    enabled: bool = True
    instruction: str = Field(min_length=1)


class AdmissionRow(_Frozen):
    """One receiving scope's row of the admission table. An unaddressed message can never be explicit."""

    listed_unaddressed: Literal["inferred", "ignore"] | None = None
    unlisted_addressed: Admission | None = None
    unlisted_unaddressed: Literal["inferred", "ignore"] | None = None


class SourcePolicy(_Frozen):
    """A Source's part of the Policy, in that Source's own identities."""

    allowed: tuple[Allowed, ...] = ()
    bots: tuple[Allowed, ...] = ()
    admission: dict[str, AdmissionRow] = {}


class PolicyConfig(_Frozen):
    rules: tuple[Rule, ...] = ()
    sources: dict[str, SourcePolicy] = {}
    mode: AcceptMode = "observe"
    eval_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    paused: bool = False

    @field_validator("rules")
    @classmethod
    def _unique(cls, rules: tuple[Rule, ...]) -> tuple[Rule, ...]:
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("Duplicate rule IDs")
        return rules

    @property
    def eval_timeout(self) -> timedelta:
        return timedelta(seconds=self.eval_timeout_seconds)

    def enabled_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def rules_on(self, ids: tuple[str, ...] | list[str]) -> bool:
        return all((r := self.rule(i)) is not None and r.enabled for i in ids)

    def allowed_sender(self, actor: ActorRef) -> Allowed | None:
        part = self.sources.get(actor.source)
        return next((a for a in part.allowed if a.id.lower() == actor.id.lower()), None) if part else None

    def listed_bot(self, actor: ActorRef) -> Allowed | None:
        part = self.sources.get(actor.source)
        return next((a for a in part.bots if a.id.lower() == actor.id.lower()), None) if part else None

    def admission(self, source: str, row: str | None, cell: AdmissionCell) -> Admission:
        part = self.sources.get(source)
        value = getattr(part.admission.get(row), cell, None) if part and row is not None and row in part.admission \
            else None
        return value or ADMISSION_DEFAULTS[cell]


POLICY_REVISION = "continuum-mandate-v1"


def policy_version(config: PolicyConfig, gate_rules: list[str]) -> str:
    """A hash of what bears on authorization: rules, accept mode, the Sources' parts and the gate rules that judge.
    Models, concurrency and pause do not invalidate decisions."""
    payload = json.dumps({
        "rules": [r.model_dump() for r in config.rules],
        "mode": config.mode,
        "sources": {k: v.model_dump() for k, v in sorted(config.sources.items())},
        "gate": gate_rules,
    }, sort_keys=True, ensure_ascii=False)
    return key(POLICY_REVISION, payload)
