"""The plugin registry: discovery, version checks and registration by extension point.

Plugins come from the built-ins, from installed packages' `continuum.plugins` entry points, and from the `plugin`
attribute of each file in the home's `plugins/` directory. A plugin with the wrong API version or a name another
plugin already registered is refused and recorded as a problem; the others still load.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from continuum.plugin import API_VERSION, ExecutorKind, Plugin, RouterKind, Script, SourceKind
from continuum.policy.gate import GateRule

log = logging.getLogger("continuum.plugins")

ENTRY_POINT_GROUP = "continuum.plugins"


class Registry:
    def __init__(self) -> None:
        self.plugins: list[Plugin] = []
        self.problems: list[str] = []
        self.sources: dict[str, SourceKind] = {}
        self.routers: dict[str, RouterKind] = {}
        self.executors: dict[str, ExecutorKind] = {}
        self.gate_rules: dict[str, GateRule] = {}
        self.scripts: dict[str, Script] = {}

    def add(self, plugin: Any, origin: str = "") -> bool:
        """Register a plugin's contributions, all or none; false and a problem when it is refused."""
        where = f" ({origin})" if origin else ""
        if not isinstance(plugin, Plugin):
            return self._refuse(f"{origin or plugin!r}: not a continuum.plugin.Plugin")
        if plugin.api_version != API_VERSION:
            return self._refuse(f"plugin {plugin.name}{where}: API version {plugin.api_version}, "
                                f"this host speaks {API_VERSION}")
        if any(p.name == plugin.name for p in self.plugins):
            return self._refuse(f"plugin {plugin.name}{where}: a plugin with that name is already loaded")
        groups: list[tuple[dict[str, Any], dict[str, Any], str]] = [
            (self.sources, {k.kind: k for k in plugin.sources}, "source kind"),
            (self.routers, {k.kind: k for k in plugin.routers}, "router kind"),
            (self.executors, {k.kind: k for k in plugin.executors}, "executor kind"),
            (self.gate_rules, {r.name: r for r in plugin.gate_rules}, "gate rule"),
            (self.scripts, dict(plugin.scripts), "script"),
        ]
        for table, items, label in groups:
            taken = sorted(set(items) & set(table))
            if taken:
                return self._refuse(f"plugin {plugin.name}{where}: {label} {', '.join(taken)} already registered")
        for table, items, _ in groups:
            table.update(items)
        self.plugins.append(plugin)
        log.info("loaded plugin %s%s", plugin.name, where)
        return True

    def load(self, builtins: Iterable[Plugin] = (), entry_points: bool = True, directory: Path | None = None) -> None:
        for plugin in builtins:
            self.add(plugin, "built-in")
        if entry_points:
            for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
                try:
                    self.add(entry.load(), f"entry point {entry.value}")
                except Exception as error:  # noqa: BLE001
                    self._refuse(f"entry point {entry.value}: {error}")
        if directory and directory.is_dir():
            for path in sorted(directory.glob("*.py")):
                self.add_file(path)

    def add_file(self, path: Path) -> bool:
        try:
            spec = importlib.util.spec_from_file_location(f"continuum_local_plugins.{path.stem}", path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            # Registered before it runs, as an import would, so dataclasses and the like can find it.
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        except Exception as error:  # noqa: BLE001
            sys.modules.pop(f"continuum_local_plugins.{path.stem}", None)
            return self._refuse(f"{path.name}: {type(error).__name__}: {error}")
        if not hasattr(module, "plugin"):
            return self._refuse(f"{path.name}: defines no `plugin`")
        return self.add(module.plugin, path.name)

    def _refuse(self, problem: str) -> bool:
        log.warning("plugin refused: %s", problem)
        self.problems.append(problem)
        return False
