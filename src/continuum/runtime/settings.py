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


class ApiSettings(_Strict):
    enabled: bool = False
    # Keep it on loopback and put a TLS proxy (such as Caddy) in front of it for remote clients.
    host: str = "127.0.0.1"
    port: int = Field(default=8170, ge=0, le=65535)


class Settings(_Strict):
    policy: PolicyConfig = PolicyConfig()
    # The gate rules, in the order they are asked; `standard` is dsh-superagent's gate.
    gate: list[str] = ["standard"]
    router: RouterSettings = RouterSettings()
    executor: ExecutorSettings = ExecutorSettings()
    sources: dict[str, SourceInstance] = {"cli": SourceInstance(kind="cli")}
    api: ApiSettings = ApiSettings()
    # Seconds between passes when nothing pokes the scheduler.
    tick_seconds: float = Field(default=1.0, gt=0, le=60)


DEFAULT_TOML = """\
# Continuum settings. Changes take effect on the next start.

gate = ["standard"]

[policy]
# off: only explicit requests. observe: proactive candidates are judged and only recorded. auto: they are accepted.
mode = "observe"
eval_timeout_seconds = 120

# Who may ask for work, per Source, in that Source's own identities.
[policy.sources.cli]
allowed = [{ id = "me" }]

# Users of the desktop app and other API clients; `continuum user add <name>` gives each a token.
[policy.sources.app]
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

[sources.app]
kind = "app"

# The API the desktop app connects to. On a server, keep it on loopback behind an HTTPS proxy.
[api]
enabled = true
host = "127.0.0.1"
port = 8170
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
