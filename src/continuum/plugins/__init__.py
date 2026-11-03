"""Built-in plugins. They register through the same path as any third-party plugin."""

from continuum.plugin import Plugin
from continuum.policy.gate import StandardGate

from .cli_source import CliSource
from .rule_router import RuleRouter
from .script_executor import ScriptExecutor
from .scripts import echo

BUILTINS = [
    Plugin(name="builtin", sources=[CliSource()], routers=[RuleRouter()], executors=[ScriptExecutor()],
           gate_rules=[StandardGate()], scripts={"echo": echo}),
]
