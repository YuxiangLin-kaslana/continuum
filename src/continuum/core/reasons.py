"""Why the core recorded a Decision or set a Trigger aside, by code.

Gate rules and Source scopes may add their own literal codes.
"""

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

REASONS: dict[str, str] = {
    "said": "{text}",
    "timeout": "Assessment timed out",
    "policyChanged": "Policy changed during assessment",
    "noAdmissible": "No admissible trigger",
    "startedTicket": "Already started ticket {ticket}",
    "acceptedBefore": "Trigger {trigger} was accepted before",
    "routed": "Routed to ticket {ticket}",
    "humanStop": "Human asked to stop",
    "retried": "Retried by a person",
    "hostRestarted": "Host restarted",
    "stopUnknown": "Stop target is unknown or already ended",
    "stopNotExplicit": "A stop needs an explicit request from a sender on the list",
    "directionUnsettled": "Implementation intent or target direction is not established, or a decision is still required",
    "noRuleMatched": "No enabled admission rule matched",
    "ignoredFromCard": "Ignored from its card, without a rule assessment",
    "observed": "Observe mode: would accept ({text})",
}


class Reason(BaseModel):
    """A reason code with its template values; `trigger_id` names the Trigger of a batch it concerns."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    values: dict[str, str] = {}
    trigger_id: str | None = None


def said(text: str) -> Reason:
    return Reason(code="said", values={"text": text})


def reason_text(reasons: Reason | Iterable[Reason] | None) -> str:
    """English text of reasons, for agents to read."""
    if reasons is None:
        return ""
    items = [reasons] if isinstance(reasons, Reason) else list(reasons)
    out = []
    for reason in items:
        text = REASONS.get(reason.code, reason.code)
        for name, value in reason.values.items():
            text = text.replace("{" + name + "}", value)
        out.append(f"{reason.trigger_id}: {text}" if reason.trigger_id else text)
    return "; ".join(out)
