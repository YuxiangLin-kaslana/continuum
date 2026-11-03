"""The sandbox a worker's shell commands run in.

With bubblewrap, a command sees the system's programs read-only, its ticket's workspace as `/work` (read-write), a
private `/tmp`, and nothing else of the host: no home directory, no other tickets, no network unless allowed. It gets
a minimal environment, so no key or token of the host reaches it. A command that runs too long is killed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# Read-only system paths a command may need; those missing on a host are skipped.
SYSTEM_PATHS = ["/usr", "/etc/alternatives", "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d",
                "/etc/ssl", "/etc/pki", "/etc/ca-certificates", "/etc/localtime", "/etc/passwd", "/etc/group",
                "/etc/nsswitch.conf", "/etc/hosts", "/etc/mime.types"]
NETWORK_PATHS = ["/etc/resolv.conf", "/etc/gai.conf"]
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
       "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"}


class SandboxConfig(BaseModel):
    # `bwrap` isolates each command; `none` runs it directly in the workspace and is for trusted setups only.
    kind: Literal["bwrap", "none"] = "bwrap"
    network: bool = False
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_output: int = Field(default=12000, ge=1000)
    # More host paths to show read-only, such as a shared toolchain.
    read_only: list[str] = []


class CommandResult(BaseModel):
    exit_code: int | None
    output: str
    timed_out: bool = False

    def text(self) -> str:
        head = "timed out and was killed" if self.timed_out else f"exit code {self.exit_code}"
        return f"[{head}]\n{self.output}"


def available() -> bool:
    return shutil.which("bwrap") is not None


def bwrap_args(workdir: Path, config: SandboxConfig) -> list[str]:
    args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if config.network:
        args.append("--share-net")
    for path in [*SYSTEM_PATHS, *(NETWORK_PATHS if config.network else []), *config.read_only]:
        args += ["--ro-bind-try", path, path]
    for link, target in (("/bin", "usr/bin"), ("/sbin", "usr/sbin"), ("/lib", "usr/lib"), ("/lib64", "usr/lib64")):
        if os.path.islink(link) or not os.path.exists(link):
            args += ["--symlink", target, link]
        else:
            args += ["--ro-bind", link, link]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--bind", str(workdir.resolve()), "/work", "--chdir", "/work"]
    return args


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters cut] ...\n{text[-half:]}"


async def run(command: str, workdir: Path, config: SandboxConfig) -> CommandResult:
    workdir.mkdir(parents=True, exist_ok=True)
    if config.kind == "bwrap":
        argv = [*bwrap_args(workdir, config), "/bin/sh", "-c", command]
        cwd = None
    else:
        argv = ["/bin/sh", "-c", command]
        cwd = str(workdir)
    env = dict(ENV)
    if config.kind == "none":
        env["HOME"] = str(workdir)
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=config.timeout_seconds)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = await process.communicate()
        return CommandResult(exit_code=None, output=_clip(output.decode(errors="replace"), config.max_output),
                             timed_out=True)
    except asyncio.CancelledError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise
    return CommandResult(exit_code=process.returncode,
                         output=_clip(output.decode(errors="replace"), config.max_output))
