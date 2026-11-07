"""The in-process Executor: the core's `Executor` port, one session and one workspace directory per ticket.

The port's methods only queue `launch` and `delivery` effects; their handlers start sessions and hand over Inputs.
A session runs turns of its ExecutorKind: a turn that ends without a final report reports `waiting`, and Inputs that
arrive while it runs start another turn. A worker's facts go back to the Engine (`assignment_started`,
`inputs_delivered`, `inputs_read`, `turn_ended`, `report`).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from continuum.core.assignments import WorkerReport
from continuum.core.engine import Engine
from continuum.core.errors import UserError
from continuum.core.model import Assignment, Execution, NoteInput, PolicyChangedInput, ReportState, ResumedInput, \
    TextInput, ThreadRef, Ticket, TriggerInput, live_ticket, running_assignment
from continuum.core.ports import Accept
from continuum.core.replies import ReplyRequest
from continuum.plugin import ExecutorContext, ExecutorKind, Relay

from .effects import Effects, Later
from .store import SQLiteStore

if TYPE_CHECKING:
    from continuum.plugin import Script

log = logging.getLogger("continuum.executor")

FINAL = ("ready", "dropped")
HOLDING = ("blocked", "failed")


class InputView(BaseModel):
    """An Input as a worker reads it."""

    id: str
    kind: str
    text: str
    trigger_id: str | None = None
    sender: str | None = None
    human: bool = False
    context: bool = False
    note: str | None = None


class Job:
    """What a worker turn is given: its ticket, its workspace, its Inputs, and the calls it may make."""

    def __init__(self, session: Session, ticket: Ticket):
        self._session = session
        self.ticket_id = ticket.id
        self.title = ticket.title
        self.scope = ticket.mandate.scope
        # Who the work is for: what the worker may reach on their behalf depends on it.
        self.requester = ticket.mandate.actor.id
        self.assignment = ticket.execution.assignment if ticket.execution else ticket.title
        self.workdir = Path(ticket.execution.directory) if ticket.execution else Path(".")
        self.reports: list[ReportState] = []
        self.took_inputs = False

    async def inputs(self) -> list[InputView]:
        """The Inputs delivered since the last call; reading them tells the Engine the worker has them."""
        self.took_inputs = True
        return self._session.take_inbox()

    def ack(self, trigger_id: str, text: str) -> None:
        """Confirm a person's request once, under their message; it marks the message as taken up."""
        self._session.host.engine.reply(ReplyRequest(purpose="acknowledgement", ticket_id=self.ticket_id,
                                                     trigger_id=trigger_id, text=text))

    def report(self, state: ReportState, summary: str, *, question: str | None = None, clear_question: bool = False,
               asked_by: str | None = None, answers: str | None = None) -> None:
        self._session.host.report(self.ticket_id, WorkerReport(
            state=state, summary=summary, question=question, clear_question=clear_question, asked_by=asked_by,
            answers=answers))
        self.reports.append(state)

    def activity(self, event: dict[str, Any]) -> None:
        """Show the people the ticket belongs to what the turn is doing now: a step (a tool call, kept with their
        conversation), the answer as it is being written, or that the model is thinking. Nothing in the domain
        depends on it."""
        if self._session.host.on_activity and not self._session.stopped:
            self._session.host.on_activity(self.ticket_id, event)

    def handback(self, trigger_id: str, reason: str) -> bool:
        """Return a person's message that asks for more than this ticket, for a fresh judgement."""
        return self._session.host.engine.handback(self.ticket_id, trigger_id, reason)

    def follow(self, source: str, thread: str) -> bool:
        """Follow a related Thread for context only."""
        return self._session.host.engine.follow_for_context(self.ticket_id, ThreadRef(source=source, id=thread))


class Session:
    """A ticket's worker: its inbox and the task that runs its turns."""

    def __init__(self, host: InProcessExecutor, ticket_id: str, assignment_id: str):
        self.host = host
        self.ticket_id = ticket_id
        self.assignment_id = assignment_id
        self.inbox: list[InputView] = []
        self.task: asyncio.Task | None = None
        self.stopped = False

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def take_inbox(self) -> list[InputView]:
        items, self.inbox = self.inbox, []
        trigger_inputs = [i.id for i in items if i.kind == "trigger"]
        if trigger_inputs and not self.stopped:
            self.host.engine.inputs_read(self.ticket_id, trigger_inputs)
        return items

    def start(self) -> None:
        if not self.busy and not self.stopped:
            self.task = asyncio.create_task(self._run(), name=f"worker-{self.ticket_id[:8]}")
            self.host.turns.add(self.task)
            self.task.add_done_callback(self.host.turns.discard)

    def _current(self) -> Ticket | None:
        ticket = self.host.store.tickets.get(self.ticket_id)
        running = running_assignment(ticket) if ticket else None
        if self.stopped or ticket is None or not live_ticket(ticket) or running is None \
                or running.id != self.assignment_id:
            return None
        return ticket

    async def _run(self) -> None:
        while (ticket := self._current()) is not None:
            job = Job(self, ticket)
            failure: Exception | None = None
            try:
                await self.host.kind.turn(self.host.context, job)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a failed turn is reported, not raised
                log.exception("worker %s failed", self.ticket_id[:8])
                failure = error
            # What a turn was given counts as read whether or not it looked, so a turn that ignores its Inputs
            # cannot loop on them; only what arrives during a turn starts another.
            if not job.took_inputs:
                self.take_inbox()
            if self._current() is None:
                return
            if failure is not None:
                self.host.report(self.ticket_id, WorkerReport(
                    state="failed", summary=f"{type(failure).__name__}: {failure}"[:500]))
                # A person's message that arrived during the failed turn lifts the hold: go on with it.
                if not any(i.kind == "trigger" and i.human and not i.context for i in self.inbox):
                    return
            elif not self.inbox:
                current = self._current()
                if current is not None:
                    self._end_turn(current, job)
                return
            # Inputs arrived during the turn: go on at once, without reporting waiting, so the Engine still sees a
            # busy worker if the host stops now.
            await asyncio.sleep(0)

    def _end_turn(self, ticket: Ticket, job: Job) -> None:
        """A turn that reported nothing final, asked nothing and is not held reports `waiting`."""
        latest = ticket.assignments[-1].report
        settled = bool(job.reports and job.reports[-1] in (*FINAL, *HOLDING)) or ticket.attention is not None
        if not settled:
            summary = latest.summary if latest else "turn ended"
            self.host.report(self.ticket_id, WorkerReport(state="waiting", summary=summary))
        if self._current():
            self.host.engine.turn_ended(self.ticket_id, self.assignment_id)


def _render(ticket: Ticket, store: SQLiteStore, input_id: str) -> InputView | None:
    item = ticket.input(input_id)
    if isinstance(item, TriggerInput):
        trigger = store.triggers.get(item.trigger_id)
        return InputView(id=item.id, kind="trigger", text=trigger.text if trigger else "", trigger_id=item.trigger_id,
                         sender=trigger.actor.id if trigger else None, human=item.human, context=item.context,
                         note=item.note)
    if isinstance(item, ResumedInput):
        return InputView(id=item.id, kind="resumed", trigger_id=item.trigger_id,
                         text="A person's message resumed this ticket under its original authorization.")
    if isinstance(item, PolicyChangedInput):
        return InputView(id=item.id, kind="policy-changed", text=item.effect.model_dump_json(exclude_none=True))
    if isinstance(item, (TextInput, NoteInput)):
        return InputView(id=item.id, kind=item.kind, text=item.text)
    return None


class InProcessExecutor:
    def __init__(self, store: SQLiteStore, effects: Effects, home: Path, kind: ExecutorKind, config: Any,
                 scripts: dict[str, Script], max_workers: int, relay: Callable[[Relay], None],
                 paused: Callable[[], bool] = lambda: False, on_report: Callable[[str], None] | None = None,
                 models: Any = None, on_activity: Callable[[str, dict[str, Any]], None] | None = None,
                 ssh: Any = None):
        self.store = store
        self.effects = effects
        self.home = home
        self.kind = kind
        self.context = ExecutorContext(config=config, scripts=scripts, models=models or {}, ssh=ssh)
        self.max_workers = max_workers
        self.relay = relay
        self.paused = paused
        self.on_report = on_report
        self.on_activity = on_activity
        self.sessions: dict[str, Session] = {}
        # Every running turn, including one whose session was stopped and is still finishing: each holds a slot.
        self.turns: set[asyncio.Task] = set()
        self._engine: Engine | None = None
        effects.handle("launch", self._launch, base=5.0, cap=300.0, concurrency=2)
        effects.handle("delivery", self._deliver, base=2.0, cap=60.0, concurrency=4)

    @property
    def engine(self) -> Engine:
        assert self._engine is not None
        return self._engine

    def bind(self, engine: Engine) -> None:
        self._engine = engine

    # --- the port, called inside Engine transactions ---

    def start(self, ticket: Ticket, assignment: Assignment) -> None:
        self.effects.enqueue("launch", assignment.id, {"ticket": ticket.id, "assignment": assignment.id})

    def deliver(self, ticket: Ticket, assignment: Assignment) -> None:
        self.effects.enqueue("delivery", ticket.id, {"ticket": ticket.id})

    def stop(self, ticket: Ticket, assignment: Assignment, reason: str) -> None:
        self.store.after_commit(lambda: self._stop(ticket.id, assignment.id))

    # --- the Engine's hook that names a new ticket's workspace and assignment text ---

    def prepare(self, ticket: Ticket, outcome: Accept) -> Ticket:
        slug = re.sub(r"[^a-z0-9-]+", "-", (outcome.name or "").lower()).strip("-")[:40]
        name = f"{slug or 't'}-{ticket.id[:8]}"
        lines = [f"Ticket: {ticket.title}", f"Scope: {ticket.mandate.scope}",
                 f"Authorization: {ticket.mandate.strength}"]
        if ticket.mandate.basis.rules:
            lines.append(f"Matched rules: {', '.join(ticket.mandate.basis.rules)}")
        if outcome.note:
            lines.append(f"Routing note: {outcome.note}")
        origin = self.store.triggers.get(ticket.origin.trigger_id)
        if origin:
            lines.append(f"Request from {origin.actor.id}: {origin.text}")
        return ticket.model_copy(update={"execution": Execution(
            name=name, directory=str(self.home / "work" / name), assignment="\n".join(lines))})

    # --- reports ---

    def report(self, ticket_id: str, report: WorkerReport) -> None:
        """Record a worker's report; a result, a question or a hold is passed on to the router afterwards."""
        def work() -> None:
            self.engine.report(ticket_id, report)
            if report.state in (*FINAL, *HOLDING) or report.question:
                relay = Relay(ticket_id=ticket_id, state=report.state, summary=report.summary,
                              question=report.question)
                self.relay(relay)
            if self.on_report:
                on_report = self.on_report
                self.store.after_commit(lambda: on_report(ticket_id))
        self.store.atomic(work)

    # --- effect handlers ---

    def _busy(self) -> int:
        return len(self.turns)

    async def _launch(self, payload: dict[str, Any]) -> None:
        ticket = self.store.tickets.get(payload["ticket"])
        assignment = next((a for a in ticket.assignments if a.id == payload["assignment"]), None) if ticket else None
        if ticket is None or not live_ticket(ticket) or assignment is None or assignment.state != "pending":
            return
        if self.paused() or self._busy() >= self.max_workers:
            raise Later(1.0)
        try:
            if ticket.execution:
                Path(ticket.execution.directory).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            # A workspace that cannot be prepared fails the ticket; a person's message or a retry tries again.
            self.report(ticket.id, WorkerReport(state="failed", summary=f"workspace: {error}"[:500]))
            return
        old = self.sessions.pop(ticket.id, None)
        if old:
            old.stopped = True
            if old.task and not old.task.done():
                old.task.cancel()
        session = self.sessions[ticket.id] = Session(self, ticket.id, assignment.id)
        self.engine.assignment_started(ticket.id, assignment.id)
        # The Inputs that waited for this Assignment, such as a resume or a recovery note, reach the worker before
        # its first turn, so it never works without them.
        self._hand_over(ticket.id, session)
        session.start()
        log.info("worker %s started", ticket.id[:8])

    async def _deliver(self, payload: dict[str, Any]) -> None:
        ticket = self.store.tickets.get(payload["ticket"])
        running = running_assignment(ticket) if ticket else None
        if ticket is None or not live_ticket(ticket) or running is None or not ticket.undelivered:
            return
        session = self.sessions.get(ticket.id)
        if session is None or session.assignment_id != running.id or session.stopped:
            # The host restarted since this Assignment started; its worker goes on in a new session.
            session = self.sessions[ticket.id] = Session(self, ticket.id, running.id)
        if self.paused() or (not session.busy and self._busy() >= self.max_workers):
            raise Later(1.0)
        self._hand_over(ticket.id, session)
        session.start()

    def _hand_over(self, ticket_id: str, session: Session) -> None:
        """Put the ticket's undelivered Inputs in the session's inbox and tell the Engine they reached the worker."""
        ticket = self.store.tickets.get(ticket_id)
        if ticket is None or not ticket.undelivered:
            return
        ids = list(ticket.undelivered)
        session.inbox.extend(v for i in ids if (v := _render(ticket, self.store, i)) is not None)
        self.engine.inputs_delivered(ticket_id, session.assignment_id, ids)

    def _stop(self, ticket_id: str, assignment_id: str) -> None:
        session = self.sessions.get(ticket_id)
        if session is None or session.assignment_id != assignment_id:
            return
        session.stopped = True
        del self.sessions[ticket_id]
        # A turn that ended its own ticket finishes by itself; any other is cancelled.
        if session.task and not session.task.done() and session.task is not asyncio.current_task():
            session.task.cancel()

    # --- start and stop ---

    def resume(self) -> None:
        """At start: every Assignment that was running when the host stopped starts again with a recovery note, or,
        when it had settled, gets the Inputs that wait for it."""
        for ticket in sorted(self.store.tickets.open(), key=lambda t: t.created_at):
            running = running_assignment(ticket)
            if running:
                try:
                    self.engine.assignment_interrupted(ticket.id, running.id)
                except UserError as error:
                    log.warning("could not resume %s: %s", ticket.id[:8], error)

    async def shutdown(self) -> None:
        tasks = [t for t in self.turns if not t.done()]
        for session in self.sessions.values():
            session.stopped = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.clear()


async def call_script(script: Script, job: Job) -> None:
    result = script(job)
    if inspect.isawaitable(result):
        await result

