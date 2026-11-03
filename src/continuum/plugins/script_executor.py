"""The script executor: a worker turn is a call to a named Python function."""

from __future__ import annotations

from pydantic import BaseModel

from continuum.plugin import ExecutorContext


class ScriptConfig(BaseModel):
    # The name of a script a plugin registered.
    script: str = "echo"


class ScriptExecutor:
    kind = "script"
    config_schema = ScriptConfig

    async def turn(self, ctx: ExecutorContext, job) -> None:
        from continuum.runtime.executor import call_script

        config: ScriptConfig = ctx.config
        script = ctx.scripts.get(config.script)
        if script is None:
            raise LookupError(f"no script named {config.script!r}; known: {', '.join(sorted(ctx.scripts))}")
        await call_script(script, job)
