"""Built-in plugins. They register through the same path as any third-party plugin."""

from continuum.plugin import Plugin
from continuum.policy.gate import StandardGate

from .agent_executor import AgentExecutor
from .app_source import AppSource
from .cli_source import CliSource
from .model_router import ModelRouter
from .rule_router import RuleRouter
from .script_executor import ScriptExecutor
from .scripts import echo

BUILTINS = [
    Plugin(name="builtin", sources=[CliSource(), AppSource()], routers=[RuleRouter(), ModelRouter()],
           executors=[ScriptExecutor(), AgentExecutor()],
           gate_rules=[StandardGate()], scripts={"echo": echo}),
]
