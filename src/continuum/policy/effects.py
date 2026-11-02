"""How a Policy change bears on each open ticket, so only the affected ones are told."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from continuum.core.model import PolicyEffect, RuleEffect, Ticket
from continuum.core.ports import SourceScope

from .config import PolicyConfig
from .gate import GateRule, RulePolicy


def policy_effects(tickets: Sequence[Ticket], old: PolicyConfig, new: PolicyConfig,
                   scope: Callable[[PolicyConfig], SourceScope],
                   gate_rules: Callable[[], Sequence[GateRule]] | None = None) -> dict[str, PolicyEffect]:
    def policy(config: PolicyConfig) -> RulePolicy:
        return RulePolicy(config, scope(config), gate_rules() if gate_rules else None)

    before, after = policy(old), policy(new)
    effects: dict[str, PolicyEffect] = {}
    for ticket in tickets:
        rules: list[RuleEffect] = []
        for rule_id in ticket.mandate.basis.rules:
            was, now = old.rule(rule_id), new.rule(rule_id)
            if now is None or not now.enabled:
                if was is not None and was.enabled:
                    rules.append(RuleEffect(id=rule_id, name=was.name, change="disabled"))
            elif was != now:
                rules.append(RuleEffect(id=rule_id, name=now.name, change="changed", instruction=now.instruction))
        effect = PolicyEffect(rules=rules)
        if before.revalidate(ticket.mandate) and not after.revalidate(ticket.mandate):
            # With the old mode and rules, the Mandate holds unless the sender or receiving scope lost authorization.
            as_before = new.model_copy(update={"mode": old.mode, "rules": old.rules})
            if not policy(as_before).revalidate(ticket.mandate):
                effect.requester_allowed = False
                effect.requester = ticket.mandate.actor
            elif ticket.mandate.strength == "inferred" and old.mode == "auto" and new.mode != "auto":
                effect.proactive = False
        if not effect.empty():
            effects[ticket.id] = effect
    return effects
