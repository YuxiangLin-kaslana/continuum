"""The gate: deterministic code that decides who may originate work and with what strength.

A Policy runs a chain of gate rules. The first rule that does not abstain decides; a rule may also refuse outright.
The rule that admitted a Trigger is recorded on its Mandate, and that rule alone revalidates the Mandate later.
`StandardGate` ports dsh-superagent's gate: allowed lists, follow-ups, the admission table and listed bots.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from continuum.core.model import Mandate, MandateBasis, Thread, Ticket, Trigger
from continuum.core.ports import Admit, GateResult, Refuse, ScopeResult, SourceScope

from .config import STRENGTH_ORDER, AdmissionCell, PolicyConfig, policy_version


@dataclass(frozen=True)
class GateEnv:
    """What a gate rule reads: the Policy configuration and which Triggers the Sources receive."""

    config: PolicyConfig
    sources: SourceScope

    def listened(self, source: str, meta: dict) -> ScopeResult:
        return self.sources.scope(source, meta)


class GateRule(Protocol):
    name: str

    def gate(self, trigger: Trigger, thread: Thread, tickets: Sequence[Ticket], env: GateEnv) -> GateResult | None:
        """Admit with a strength and basis, refuse with a reason, or abstain with None."""
        ...

    def revalidate(self, mandate: Mandate, env: GateEnv) -> bool:
        """Whether a Mandate this rule admitted still holds."""
        ...


def _participating(thread: Thread, tickets: Sequence[Ticket]) -> bool:
    """Whether the Thread has tickets of its own, not only ones that follow it for context."""
    return any(i not in thread.context for i in [*thread.subscriptions, *(t.id for t in tickets)])


def _cell(allowed: bool, addressed: bool) -> AdmissionCell | None:
    if allowed:
        return None if addressed else "listed_unaddressed"
    return "unlisted_addressed" if addressed else "unlisted_unaddressed"


class StandardGate:
    """Allowed senders addressing the agent are explicit; follow-ups on a Thread with tickets take the sender's
    strength; the admission table decides the rest; listed bots make inferred candidates at most."""

    name = "standard"

    def gate(self, trigger: Trigger, thread: Thread, tickets: Sequence[Ticket], env: GateEnv) -> GateResult:
        config, mode = env.config, env.config.mode
        if not trigger.human:
            return self._machine(trigger, env)
        allowed = config.allowed_sender(trigger.actor)
        scope = env.listened(trigger.source, trigger.meta)
        row = scope.row if scope.listened else None
        recorded = dict(source=trigger.source, scope=trigger.meta, row=row, sender=allowed.label if allowed else None)
        if allowed and trigger.addressed:
            return Admit(strength="explicit",
                         basis=MandateBasis(via="addressed", addressed=trigger.addressed, **recorded))
        cell = _cell(bool(allowed), bool(trigger.addressed))
        assert cell is not None
        # Unaddressed messages use the admission table only where the Source receives every message.
        receivable = bool(trigger.addressed) or (scope.listened and scope.observed)
        admission = config.admission(trigger.source, row, cell) if receivable else "ignore"
        basis = MandateBasis(via="admission", cell=cell, addressed=trigger.addressed, **recorded)
        # An explicit cell wins over a follow-up, so a Thread's tickets never weaken an explicit request.
        if admission == "explicit":
            return Admit(strength="explicit", basis=basis)
        # A direct-message Thread belongs to one sender; an unlisted sender's follow-up needs an accept mode.
        if not (trigger.addressed == "direct" and not allowed) and (allowed or mode != "off") \
                and _participating(thread, tickets):
            return Admit(strength="explicit" if allowed else "inferred",
                         basis=MandateBasis(via="followup", **recorded))
        if admission == "inferred" and mode != "off":
            return Admit(strength="inferred", basis=basis.model_copy(update={"observation": mode}))
        if not allowed:
            return Refuse(reason="not_a_member")
        return Refuse(reason="admission_ignored" if receivable and admission == "ignore" else "no_mention")

    def _machine(self, trigger: Trigger, env: GateEnv) -> GateResult:
        """Bots and CI: only listed bots, as inferred candidates, and senderless events the table admits."""
        config, mode = env.config, env.config.mode
        bot = config.listed_bot(trigger.actor) if trigger.actor.id else None
        if trigger.actor.id and bot is None:
            return Refuse(reason="not_human")
        scope = env.listened(trigger.source, trigger.meta)
        if not scope.listened or not (trigger.addressed or scope.observed):
            return Refuse(reason="not_human")
        recorded = dict(source=trigger.source, scope=trigger.meta, row=scope.row)
        if bot:
            if mode == "off":
                return Refuse(reason="admission_ignored")
            return Admit(strength="inferred", basis=MandateBasis(
                via="bot", sender=bot.label, addressed=trigger.addressed, observation=mode, **recorded))
        cell: AdmissionCell = "unlisted_unaddressed"
        if config.admission(trigger.source, scope.row, cell) != "inferred" or mode == "off":
            return Refuse(reason="admission_ignored")
        return Admit(strength="inferred", basis=MandateBasis(via="admission", cell=cell, observation=mode, **recorded))

    def revalidate(self, mandate: Mandate, env: GateEnv) -> bool:
        config, mode, basis = env.config, env.config.mode, mandate.basis
        current = env.listened(basis.source, basis.scope) if basis.source and basis.scope is not None else None
        if current is not None and not current.listened:
            return False
        # Explicit follow-ups hold; inferred ones need auto mode and their matched rules enabled.
        if basis.via == "followup":
            return mandate.strength == "explicit" or (mode == "auto" and config.rules_on(basis.rules))
        if basis.via == "bot" and config.listed_bot(mandate.actor) is None:
            return False
        if basis.via == "admission":
            listed = config.allowed_sender(mandate.actor) is not None
            # A listed sender who addresses the agent is always explicit.
            if basis.cell == "unlisted_addressed" and listed:
                return True
            # A sender added to the list since moves to the listed cell.
            now = "listed_unaddressed" if basis.cell == "unlisted_unaddressed" and listed else basis.cell
            if now == "listed_unaddressed" and not listed:
                return False
            row = current.row if current is not None and current.listened else None
            admission = config.admission(basis.source or "", row, now)  # type: ignore[arg-type]
            if STRENGTH_ORDER[admission] < STRENGTH_ORDER[mandate.strength]:
                return False
            if mandate.strength == "explicit":
                return True
        elif basis.via != "bot" and config.allowed_sender(mandate.actor) is None:
            return False
        # Observed, inferred Mandates hold only in auto mode with their matched rules enabled.
        if basis.via in ("admission", "bot") and basis.observation:
            return mode == "auto" and config.rules_on(basis.rules)
        return True


class RulePolicy:
    """A Policy made of a configuration and a chain of gate rules; it implements the core's `Policy` port."""

    def __init__(self, config: PolicyConfig, sources: SourceScope, rules: Sequence[GateRule] | None = None):
        self.config = config
        self.env = GateEnv(config, sources)
        self.rules: list[GateRule] = list(rules) if rules is not None else [StandardGate()]
        names = [r.name for r in self.rules]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate gate rule names: {names}")
        self._version = policy_version(config, names)

    @property
    def version(self) -> str:
        return self._version

    @property
    def paused(self) -> bool:
        return self.config.paused

    @property
    def eval_timeout(self) -> timedelta:
        return self.config.eval_timeout

    def gate(self, trigger: Trigger, thread: Thread, tickets: Sequence[Ticket]) -> GateResult:
        for rule in self.rules:
            result = rule.gate(trigger, thread, tickets, self.env)
            if isinstance(result, Admit):
                return result.model_copy(update={"basis": result.basis.model_copy(update={"rule": rule.name})})
            if result is not None:
                return result
        return Refuse(reason="no_gate_rule")

    def revalidate(self, mandate: Mandate) -> bool:
        rule = next((r for r in self.rules if r.name == mandate.basis.rule), None)
        return rule is not None and rule.revalidate(mandate, self.env)

    def admits_inferred(self, rules: Sequence[str]) -> bool:
        return self.config.mode == "auto" and len(rules) > 0 and self.config.rules_on(list(rules))
