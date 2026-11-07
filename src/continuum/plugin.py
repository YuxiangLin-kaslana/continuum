"""The plugin API: what a plugin provides and what it is given.

A plugin is a `Plugin` value, found in the built-ins, in an installed package's `continuum.plugins` entry point, or as
the `plugin` attribute of a file in the home's `plugins/` directory. Built-in and third-party plugins take the same
path. A plugin never sees the Store and never changes a ticket; it asks the Engine through the narrow context it is
given, and the Engine may refuse.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel

from continuum.core.model import Ticket, Trigger
from continuum.core.ports import Candidate, OutboundMessage, Outcome, ScopeResult
from continuum.policy.gate import GateRule

if TYPE_CHECKING:
    from continuum.core.engine import EngineForRouter
    from continuum.models import ChatModel
    from continuum.runtime.executor import Job

API_VERSION = "1"


# --- Sources ------------------------------------------------------------------------------------------------------


class SourceContext(Protocol):
    """What a Source instance is given."""

    instance: str
    config: Any
    # A directory of its own for anything it keeps, such as its read position.
    data_dir: Path

    def receive(self, trigger: Trigger) -> str:
        """Hand a Trigger to the Engine: `stored`, `duplicate` or `filtered`."""
        ...


class SourceKind(Protocol):
    """A stream of events, and the write side of its Threads."""

    kind: str
    config_schema: type[BaseModel]

    def scope(self, config: Any, meta: dict[str, Any]) -> ScopeResult:
        """Whether this instance receives a Trigger with this metadata."""
        ...

    async def run(self, ctx: SourceContext) -> None:
        """Read events until cancelled."""
        ...

    async def send(self, ctx: SourceContext, message: OutboundMessage) -> Literal["sent", "gone"]:
        """Write a message to its Channel; `gone` when the conversation can never receive it."""
        ...


# --- Router -------------------------------------------------------------------------------------------------------


class Relay(BaseModel):
    """A worker report the router hears about: a result, a question, or a stop."""

    ticket_id: str
    state: str
    summary: str
    question: str | None = None


@dataclass
class RouterContext:
    engine: EngineForRouter
    config: Any
    # The chat models the settings name, by name.
    models: Mapping[str, ChatModel] = field(default_factory=dict)
    # The Policy's configuration, for rules and accept mode.
    policy: Any = None


class RouterKind(Protocol):
    """The router judges candidates and passes worker results on. F2 adds one that asks a model."""

    kind: str
    config_schema: type[BaseModel]

    async def assess(self, ctx: RouterContext, candidate: Candidate) -> Outcome | None:
        """Decide a candidate, or return None to let it expire and be queued again."""
        ...

    async def on_report(self, ctx: RouterContext, ticket: Ticket, relay: Relay) -> None: ...


# --- Executors ----------------------------------------------------------------------------------------------------

Script = Callable[["Job"], Awaitable[None] | None]


@dataclass
class ExecutorContext:
    config: Any
    scripts: Mapping[str, Script]
    models: Mapping[str, ChatModel] = field(default_factory=dict)
    # The SSH servers a worker may reach through the host, with their approvals (runtime.ssh.SshHosts).
    ssh: Any = None
    # The person's projects, for a worker that proposes one (runtime.app.ProjectDesk).
    projects: Any = None


class ExecutorKind(Protocol):
    """How one turn of a ticket's worker runs. The runtime keeps one session per ticket, delivers its Inputs, ends a
    turn that reports nothing final as `waiting`, and starts a new turn when Inputs wait."""

    kind: str
    config_schema: type[BaseModel]

    async def turn(self, ctx: ExecutorContext, job: Job) -> None: ...


# --- the plugin value ---------------------------------------------------------------------------------------------


@dataclass
class Plugin:
    name: str
    api_version: str = API_VERSION
    sources: Sequence[SourceKind] = ()
    routers: Sequence[RouterKind] = ()
    executors: Sequence[ExecutorKind] = ()
    gate_rules: Sequence[GateRule] = ()
    # Named worker functions for the script executor.
    scripts: Mapping[str, Script] = field(default_factory=dict)
