"""The rule router: a router without a model, for F1.

It accepts explicit requests and ignores inferred candidates; it sends a finished ticket's result and a worker's
question back to the conversation. F2 replaces it with a router that asks a model.
"""

from __future__ import annotations

from pydantic import BaseModel

from continuum.core.errors import UserError
from continuum.core.model import Ticket
from continuum.core.ports import Accept, Candidate, Ignore, Outcome
from continuum.core.replies import ReplyRequest
from continuum.plugin import Relay, RouterContext


class RuleRouterConfig(BaseModel):
    acknowledgement: str = "收到，开始处理：{title}"
    title_length: int = 40


class RuleRouter:
    kind = "rules"
    config_schema = RuleRouterConfig

    async def assess(self, ctx: RouterContext, candidate: Candidate) -> Outcome | None:
        if candidate.strength != "explicit":
            return Ignore(reason="The rule router accepts only explicit requests")
        request = next((t for t in reversed(candidate.triggers) if t.human), candidate.triggers[-1])
        text = request.text.strip()
        config: RuleRouterConfig = ctx.config
        title = text.splitlines()[0][:config.title_length] if text else "request"
        return Accept(title=title, scope=text or title, reason="explicit request",
                      acknowledgement=config.acknowledgement.format(title=title))

    async def on_report(self, ctx: RouterContext, ticket: Ticket, relay: Relay) -> None:
        try:
            if relay.state == "ready":
                ctx.engine.reply(ReplyRequest(purpose="result", ticket_id=ticket.id, text=relay.summary))
            elif relay.question and ticket.attention and ticket.attention.question == relay.question:
                ctx.engine.reply(ReplyRequest(purpose="attention", ticket_id=ticket.id, question=relay.question,
                                              text=relay.question))
        except UserError:
            # The ticket moved on since the report, such as a question already replaced; nothing to send.
            return

