"""In-memory repositories, for domain tests and ephemeral engines.

Every read and write copies, so a caller's changes reach the store only through `save`, as with a database.
`atomic` rolls every change back when its work raises.
"""

from __future__ import annotations

from collections.abc import Callable

from .model import Decision, Thread, Ticket, Trigger, TriggerDisposition, latest_assignment


def _copy[T](value: T | None) -> T | None:
    return value.model_copy(deep=True) if value is not None else None  # type: ignore[union-attr]


class _Triggers:
    def __init__(self) -> None:
        self.rows: dict[str, Trigger] = {}
        self.dispositions: dict[str, TriggerDisposition] = {}

    def get(self, trigger_id: str) -> Trigger | None:
        return _copy(self.rows.get(trigger_id))

    def save(self, trigger: Trigger) -> None:
        self.rows[trigger.id] = _copy(trigger)  # type: ignore[assignment]

    def disposition(self, trigger_id: str) -> TriggerDisposition | None:
        return _copy(self.dispositions.get(trigger_id))

    def save_disposition(self, trigger_id: str, value: TriggerDisposition) -> None:
        self.dispositions[trigger_id] = _copy(value)  # type: ignore[assignment]


class _Threads:
    def __init__(self) -> None:
        self.rows: dict[str, Thread] = {}

    def get(self, key: str) -> Thread | None:
        return _copy(self.rows.get(key))

    def save(self, thread: Thread) -> None:
        self.rows[thread.key] = _copy(thread)  # type: ignore[assignment]

    def list(self) -> list[Thread]:
        return [_copy(t) for t in self.rows.values()]  # type: ignore[misc]

    def active(self) -> list[Thread]:
        return [t for t in self.list() if t.pending or t.evaluating]


class _Tickets:
    def __init__(self) -> None:
        self.rows: dict[str, Ticket] = {}

    def get(self, ticket_id: str) -> Ticket | None:
        return _copy(self.rows.get(ticket_id))

    def save(self, ticket: Ticket) -> None:
        self.rows[ticket.id] = _copy(ticket)  # type: ignore[assignment]

    def list(self) -> list[Ticket]:
        """Newest first."""
        return [_copy(t) for t in reversed(self.rows.values())]  # type: ignore[misc]

    def open(self) -> list[Ticket]:
        return [t for t in self.list() if t.ended is None]

    def active(self) -> list[Ticket]:
        out = []
        for ticket in self.list():
            latest = latest_assignment(ticket)
            if ticket.undelivered or (latest and latest.state in ("pending", "running")):
                out.append(ticket)
        return out


class _Decisions:
    def __init__(self) -> None:
        self.rows: dict[str, Decision] = {}

    def get(self, decision_id: str) -> Decision | None:
        return _copy(self.rows.get(decision_id))

    def save(self, decision: Decision) -> None:
        self.rows[decision.id] = _copy(decision)  # type: ignore[assignment]

    def by_thread(self, key: str) -> list[Decision]:
        return [_copy(d) for d in self.rows.values() if d.thread_key == key]  # type: ignore[misc]


class MemoryRepositories:
    def __init__(self) -> None:
        self.triggers = _Triggers()
        self.threads = _Threads()
        self.tickets = _Tickets()
        self.decisions = _Decisions()

    def atomic[T](self, work: Callable[[], T]) -> T:
        tables = [self.triggers.rows, self.triggers.dispositions, self.threads.rows, self.tickets.rows,
                  self.decisions.rows]
        snapshot = [dict(table) for table in tables]
        try:
            return work()
        except BaseException:
            for table, saved in zip(tables, snapshot, strict=True):
                table.clear()
                table.update(saved)
            raise
