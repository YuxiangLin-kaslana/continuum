"""Projects: what the person sees of their work.

A one-off request is a project of one run: it is the ticket the router opened, shown as a project, and it is archived
when the ticket ends. A long-running project is a standing authorization the person confirmed: a goal, a charter of
what it may do, and schedules that remind or start a run. Its runs are tickets opened on its own thread, so they share
its workspace and its notes, and several can run at once.

The person talks to one assistant. A project's messages, reminders among them, are part of that one conversation and
carry the project they belong to; opening a project only narrows the view.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from .store import SQLiteStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL, body TEXT NOT NULL);
"""
THREAD_PREFIX = "p-"
State = Literal["proposed", "active", "paused", "ended", "refused"]
WEEKDAYS = "一二三四五六日"


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


class Schedule(BaseModel):
    """When a project acts on its own: reminds the person (`remind`) or starts a run (`run`)."""

    id: str = Field(default_factory=lambda: secrets.token_hex(4))
    action: Literal["remind", "run"] = "remind"
    # What the reminder says, or what the run is asked to do.
    text: str = Field(min_length=1, max_length=2000)
    every: Literal["once", "minutes", "hours", "day", "week", "month"] = "day"
    interval: int = Field(default=1, ge=1, le=1000)
    # "HH:MM" in the project's time zone, for day, week and month.
    at: str | None = None
    # 1 (Monday) to 7, for week; 1 to 28, for month.
    weekday: int | None = Field(default=None, ge=1, le=7)
    day: int | None = Field(default=None, ge=1, le=28)
    # For once: when, in the project's time zone ("2026-11-08 09:00").
    once_at: str | None = None
    next_at: datetime | None = None
    last_at: datetime | None = None

    @field_validator("at")
    @classmethod
    def _at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        hours, _, minutes = value.strip().partition(":")
        h, m = int(hours), int(minutes or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("at 要是 HH:MM")
        return f"{h:02d}:{m:02d}"

    @model_validator(mode="after")
    def _complete(self) -> Schedule:
        if self.every == "once" and not self.once_at:
            raise ValueError("一次性的安排要写 once_at")
        if self.every == "minutes" and self.interval < 5:
            raise ValueError("最短每 5 分钟一次")
        return self

    def describe(self, tz: str) -> str:
        at = f" {self.at}" if self.at else ""
        if self.every == "once":
            when = f"{self.once_at}（一次）"
        elif self.every == "minutes":
            when = f"每 {self.interval} 分钟"
        elif self.every == "hours":
            when = f"每 {self.interval} 小时" if self.interval > 1 else "每小时"
        elif self.every == "day":
            when = (f"每 {self.interval} 天" if self.interval > 1 else "每天") + at
        elif self.every == "week":
            when = f"每周{WEEKDAYS[(self.weekday or 1) - 1]}{at}"
        else:
            when = f"每月 {self.day or 1} 日{at}"
        return f"{when}（{tz}）"

    def following(self, after: datetime, tz: str) -> datetime | None:
        """The first time after `after` this schedule is due; None when a one-time schedule has passed."""
        local_zone = zone(tz)
        local = after.astimezone(local_zone)
        h, m = (int(x) for x in (self.at or "09:00").split(":"))
        if self.every == "once":
            when = datetime.fromisoformat(self.once_at.replace("T", " ")) if self.once_at else None
            if when is None:
                return None
            when = when.replace(tzinfo=local_zone) if when.tzinfo is None else when
            return when.astimezone(UTC) if when > after else None
        if self.every in ("minutes", "hours"):
            step = timedelta(minutes=self.interval) if self.every == "minutes" else timedelta(hours=self.interval)
            base = self.last_at or after
            nxt = base + step
            while nxt <= after:
                nxt += step
            return nxt.astimezone(UTC)
        if self.every == "day":
            candidate = local.replace(hour=h, minute=m, second=0, microsecond=0)
            while candidate <= local:
                candidate += timedelta(days=1)
            if self.interval > 1 and self.last_at:
                earliest = self.last_at.astimezone(local_zone) + timedelta(days=self.interval)
                while candidate < earliest.replace(hour=h, minute=m, second=0, microsecond=0):
                    candidate += timedelta(days=1)
            return candidate.astimezone(UTC)
        if self.every == "week":
            target = (self.weekday or 1) - 1
            candidate = local.replace(hour=h, minute=m, second=0, microsecond=0)
            candidate += timedelta(days=(target - candidate.weekday()) % 7)
            if candidate <= local:
                candidate += timedelta(days=7)
            return candidate.astimezone(UTC)
        day = self.day or 1
        candidate = local.replace(day=day, hour=h, minute=m, second=0, microsecond=0)
        while candidate <= local:
            month = candidate.month % 12 + 1
            candidate = candidate.replace(year=candidate.year + (candidate.month == 12), month=month)
        return candidate.astimezone(UTC)


class Project(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(6))
    owner: str
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=2000)
    # What it may do on its own, in the person's words: the standing authorization they confirm.
    charter: str = Field(default="", max_length=4000)
    state: State = "proposed"
    timezone: str = "UTC"
    schedules: list[Schedule] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ends_at: datetime | None = None
    # The run that proposed it, if a worker did.
    proposed_by: str | None = None

    @property
    def thread(self) -> str:
        return THREAD_PREFIX + self.id


def project_of_thread(thread: str | None) -> str | None:
    return thread[len(THREAD_PREFIX):] if thread and thread.startswith(THREAD_PREFIX) else None


class Projects:
    def __init__(self, store: SQLiteStore, home: Path):
        self.store = store
        self.home = home
        self.store.db.executescript(SCHEMA)

    def _save(self, project: Project) -> Project:
        project.updated_at = datetime.now(UTC)
        self.store.db.execute(
            "INSERT INTO projects (id, owner, body) VALUES (?, ?, ?) ON CONFLICT (id) DO UPDATE SET body = excluded.body",
            (project.id, project.owner, project.model_dump_json()))
        return project

    def get(self, project_id: str) -> Project | None:
        row = self.store.db.execute("SELECT body FROM projects WHERE id = ?", (project_id,)).fetchone()
        return Project.model_validate_json(row[0]) if row else None

    def list(self, owner: str | None = None) -> list[Project]:
        if owner is None:
            rows = self.store.db.execute("SELECT body FROM projects").fetchall()
        else:
            rows = self.store.db.execute("SELECT body FROM projects WHERE owner = ?", (owner,)).fetchall()
        return sorted((Project.model_validate_json(r[0]) for r in rows), key=lambda p: p.created_at, reverse=True)

    def propose(self, owner: str, name: str, goal: str, charter: str, schedules: list[dict[str, Any]],
                timezone: str, proposed_by: str | None = None) -> Project:
        project = Project(owner=owner, name=name, goal=goal, charter=charter, timezone=timezone,
                          schedules=[Schedule.model_validate(s) for s in schedules], proposed_by=proposed_by)
        return self._save(project)

    def decide(self, project_id: str, confirm: bool) -> Project:
        project = self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        if project.state != "proposed":
            raise ValueError("这个项目已经确认或拒绝过了")
        project.state = "active" if confirm else "refused"
        if confirm:
            self._plan(project, datetime.now(UTC))
            self.workspace(project).mkdir(parents=True, exist_ok=True)
        return self._save(project)

    def set_state(self, project_id: str, state: State) -> Project:
        project = self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        allowed = {"paused": {"active"}, "active": {"paused"}, "ended": {"active", "paused"}}
        if project.state not in allowed.get(state, set()):
            raise ValueError(f"项目现在是 {project.state}，不能改成 {state}")
        project.state = state
        if state == "active":
            self._plan(project, datetime.now(UTC))
        return self._save(project)

    def _plan(self, project: Project, now: datetime) -> None:
        for schedule in project.schedules:
            schedule.next_at = schedule.following(now, project.timezone)

    # --- the clock ---

    def due(self, now: datetime) -> list[tuple[Project, Schedule]]:
        """What is due now, each marked as done and planned again (so a crash does not fire it twice)."""
        fired = []
        for project in self.list():
            if project.state != "active":
                continue
            if project.ends_at and now >= project.ends_at:
                project.state = "ended"
                self._save(project)
                continue
            changed = False
            for schedule in project.schedules:
                if schedule.next_at and schedule.next_at <= now:
                    schedule.last_at = now
                    schedule.next_at = schedule.following(now, project.timezone)
                    fired.append((project, schedule))
                    changed = True
            if changed:
                self._save(project)
        return fired

    # --- its place on disk ---

    def workspace(self, project: Project) -> Path:
        return self.home / "projects" / project.id

    def notes(self, project: Project) -> str:
        path = self.workspace(project) / "NOTES.md"
        try:
            return path.read_text(encoding="utf-8")[-6000:]
        except OSError:
            return ""

    def view(self, project: Project) -> dict[str, Any]:
        return {**json.loads(project.model_dump_json()), "kind": "ongoing", "thread": project.thread,
                "schedules": [{**json.loads(s.model_dump_json()), "text_when": s.describe(project.timezone)}
                              for s in project.schedules]}
