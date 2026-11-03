"""Settings: the home's `continuum.toml`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from continuum.policy.config import PolicyConfig


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceInstance(_Strict):
    kind: str
    enabled: bool = True
    config: dict[str, Any] = {}


class RouterSettings(_Strict):
    kind: str = "rules"
    config: dict[str, Any] = {}


class ExecutorSettings(_Strict):
    kind: str = "script"
    # How many worker turns may run at once.
    max_workers: int = Field(default=3, ge=1, le=64)
    config: dict[str, Any] = {}


class Settings(_Strict):
    policy: PolicyConfig = PolicyConfig()
    # The gate rules, in the order they are asked; `standard` is dsh-superagent's gate.
    gate: list[str] = ["standard"]
    router: RouterSettings = RouterSettings()
    executor: ExecutorSettings = ExecutorSettings()
    sources: dict[str, SourceInstance] = {"cli": SourceInstance(kind="cli")}
    # Seconds between passes when nothing pokes the scheduler.
    tick_seconds: float = Field(default=1.0, gt=0, le=60)


DEFAULT_TOML = """\
# Continuum settings. Changes take effect on the next start.

gate = ["standard"]

[policy]
# off: only explicit requests. observe: proactive candidates are judged and only recorded. auto: they are accepted.
mode = "observe"
eval_timeout_seconds = 120

# Who may ask for work through the command-line Source, in that Source's own identities.
[policy.sources.cli]
allowed = [{ id = "me" }]

[router]
kind = "rules"

[executor]
kind = "script"
max_workers = 3

[executor.config]
script = "echo"

[sources.cli]
kind = "cli"
"""


def settings_path(home: Path) -> Path:
    return home / "continuum.toml"


def load_settings(home: Path) -> Settings:
    path = settings_path(home)
    if not path.exists():
        return Settings.model_validate(tomllib.loads(DEFAULT_TOML))
    return Settings.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def init_home(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "plugins").mkdir(exist_ok=True)
    path = settings_path(home)
    if not path.exists():
        path.write_text(DEFAULT_TOML, encoding="utf-8")
    return path
