"""Claude Code and Codex as workers: their command lines, read from their JSON event streams.

The agents are stand-in scripts that print the events the real ones print (recorded from claude 2.1 and codex 0.162)
and log how they were called, so the tests need no account and no network.
"""

import asyncio
import json
import sys
import tomllib
from pathlib import Path

import pytest

from continuum.plugin import ExecutorContext
from continuum.plugins import BUILTINS
from continuum.plugins.cli_agents import (ClaudeCodeConfig, ClaudeCodeExecutor, CodexConfig, CodexExecutor,
                                          parse_answer, state_path)
from continuum.runtime import Runtime, Settings
from continuum.runtime.settings import DEFAULT_TOML

from test_runtime import drive

CLAUDE = r'''
import json, sys, os, time
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": os.path.exists(os.path.expanduser("~/.claude/.credentials.json"))}, indent=2))
    sys.exit(0)
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
prompt = sys.argv[sys.argv.index("-p") + 1]
sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else sys.argv[sys.argv.index("--session-id") + 1]
def out(e):
    print(json.dumps(e, ensure_ascii=False), flush=True)
if "SANDBOX-OFF" in prompt:
    print("⚠ Sandbox disabled: sandbox is enabled but dependencies are missing", file=sys.stderr, flush=True)
    time.sleep(30)
if "CRASH" in prompt:
    print("something broke", file=sys.stderr)
    sys.exit(3)
out({"type": "system", "subtype": "init", "session_id": sid, "tools": ["Bash"]})
if "ASK" in prompt:
    answer = "【问题】R12 用 0402 还是 0603？"
else:
    out({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "先跑一下"}]}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "我先看看。"}]}})
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo hi", "description": "x"}}]}})
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi",
                                                  "is_error": False}]}})
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_2", "name": "Write", "input": {"file_path": "note.txt", "content": "ok"}}]}})
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_2",
                                                  "content": [{"type": "text", "text": "File created"}]}]}})
    answer = "跑完了，输出 hi，写了 note.txt。" if "第二轮" not in prompt else "好的，按你说的改完了。"
out({"type": "stream_event", "event": {"type": "message_start", "message": {}}})
for i in range(0, len(answer), 3):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": answer[i:i + 3]}}})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": sid})
'''

CODEX = r'''
import json, sys, os
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
def out(e):
    print(json.dumps(e, ensure_ascii=False), flush=True)
print("Reading additional input from stdin...", file=sys.stderr)
out({"type": "thread.started", "thread_id": "01a1b38b-7adc-7240-8892-df920dd24921"})
out({"type": "turn.started"})
out({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "我会运行命令并写入文件。\n"}})
out({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "/bin/bash -lc 'echo hi'",
                                      "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
out({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
                                        "command": "/bin/bash -lc 'echo hi'", "aggregated_output": "hi\n",
                                        "exit_code": 0, "status": "completed"}})
cwd = os.getcwd()
out({"type": "item.started", "item": {"id": "item_2", "type": "file_change",
                                      "changes": [{"path": cwd + "/note.txt", "kind": "add"}], "status": "in_progress"}})
out({"type": "item.completed", "item": {"id": "item_2", "type": "file_change",
                                        "changes": [{"path": cwd + "/note.txt", "kind": "add"}], "status": "completed"}})
out({"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "已运行 echo hi，写了 note.txt。"}})
out({"type": "turn.completed", "usage": {"input_tokens": 1}})
'''


def fake(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / f"fake-{name}"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class Item:
    def __init__(self, text, kind="trigger"):
        self.kind, self.text, self.sender, self.context = kind, text, "me", False


class FakeJob:
    def __init__(self, workdir: Path, inputs=()):
        self.workdir = workdir
        self.assignment = "Ticket: 跑一下\nRequest from me: 跑一下 echo hi"
        self.pending = list(inputs)
        self.events: list[dict] = []
        self.reports: list[tuple] = []

    async def inputs(self):
        items, self.pending = self.pending, []
        return items

    def activity(self, event):
        self.events.append(event)

    def report(self, state, summary, *, question=None, **_):
        self.reports.append((state, summary, question))


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_answers_open_with_a_marker_to_ask_or_to_say_they_are_stuck():
    assert parse_answer("做完了。") == ("ready", "做完了。")
    assert parse_answer("【问题】用哪个？") == ("ask", "用哪个？")
    assert parse_answer(" 【卡住】需要密码") == ("blocked", "需要密码")
    assert parse_answer("【无需处理】") == ("dropped", "【无需处理】")


def test_claude_code_runs_a_turn_shows_its_steps_and_goes_on_in_its_session(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    config = ClaudeCodeConfig(command=fake(tmp_path, "claude", CLAUDE), isolation="none",
                              env={"FAKE_LOG": str(tmp_path / "log")}, allowed_domains=["pypi.org"])
    ctx = ExecutorContext(config=config, scripts={})
    work = tmp_path / "work" / "run-1"
    job = FakeJob(work, [Item("跑一下 echo hi")])
    asyncio.run(ClaudeCodeExecutor().turn(ctx, job))

    assert job.reports == [("ready", "跑完了，输出 hi，写了 note.txt。", None)]
    steps = [e for e in job.events if e["type"] == "step"]
    assert [(s["name"], s["status"]) for s in steps] == [
        ("think", "done"), ("note", "done"), ("shell", "running"), ("shell", "done"), ("write_file", "running"),
        ("write_file", "done")]
    assert steps[3]["input"] == "echo hi" and steps[3]["output"] == "hi" and steps[5]["output"] == "File created"
    written = [e["text"] for e in job.events if e["type"] == "stream"]
    assert written[-1] == "跑完了，输出 hi，写了 note.txt。" and all(written[-1].startswith(t) for t in written)
    assert job.events[-1] == {"type": "idle"}

    [first] = calls(tmp_path / "log")
    session = first[first.index("--session-id") + 1]
    assert first[first.index("--permission-mode") + 1] == "acceptEdits"
    settings = json.loads(first[first.index("--settings") + 1])
    assert settings["sandbox"]["enabled"] and settings["sandbox"]["failIfUnavailable"]
    assert not settings["sandbox"]["allowUnsandboxedCommands"]
    assert settings["sandbox"]["network"] == {"allowedDomains": ["pypi.org"]}
    assert "【问题】" in first[first.index("--append-system-prompt") + 1]
    assert "Request from me: 跑一下 echo hi" in first[first.index("-p") + 1]
    assert json.loads(state_path(work).read_text())["session"] == session

    # A person's reply: the next turn resumes the same session with only what is new.
    job2 = FakeJob(work, [Item("第二轮：改一下")])
    asyncio.run(ClaudeCodeExecutor().turn(ctx, job2))
    second = calls(tmp_path / "log")[1]
    assert second[second.index("--resume") + 1] == session and "--session-id" not in second
    assert "第二轮" in second[second.index("-p") + 1] and "Request from me" not in second[second.index("-p") + 1]
    assert job2.reports == [("ready", "好的，按你说的改完了。", None)]


def test_claude_code_asks_fails_and_refuses_to_run_unsandboxed(tmp_path, monkeypatch):
    log = tmp_path / "log"
    base = dict(command=fake(tmp_path, "claude", CLAUDE), isolation="none", env={"FAKE_LOG": str(log)})
    ctx = ExecutorContext(config=ClaudeCodeConfig(**base), scripts={})

    asking = FakeJob(tmp_path / "work" / "a", [Item("ASK")])
    asyncio.run(ClaudeCodeExecutor().turn(ctx, asking))
    assert asking.reports == [("progress", "等待回答", "R12 用 0402 还是 0603？")]
    # The marker never shows while the question is being written.
    assert all(not e["text"].startswith("【") for e in asking.events if e["type"] == "stream")

    crashing = FakeJob(tmp_path / "work" / "b", [Item("CRASH")])
    asyncio.run(ClaudeCodeExecutor().turn(ctx, crashing))
    [(state, summary, question)] = crashing.reports
    assert state == "failed" and "退出码 3" in summary and "something broke" in summary and question

    unsafe = FakeJob(tmp_path / "work" / "c", [Item("SANDBOX-OFF")])
    started = asyncio.run(_timed(ClaudeCodeExecutor().turn(ctx, unsafe)))
    [(state, summary, _)] = unsafe.reports
    assert state == "failed" and "沙箱" in summary and started < 10


async def _timed(coro):
    loop = asyncio.get_running_loop()
    start = loop.time()
    await coro
    return loop.time() - start


def test_codex_runs_a_turn_shows_its_steps_and_resumes_its_thread(tmp_path):
    log = tmp_path / "log"
    config = CodexConfig(command=fake(tmp_path, "codex", CODEX), isolation="none", env={"FAKE_LOG": str(log)})
    ctx = ExecutorContext(config=config, scripts={})
    work = tmp_path / "work" / "run-1"
    job = FakeJob(work, [Item("跑一下 echo hi")])
    asyncio.run(CodexExecutor().turn(ctx, job))

    assert job.reports == [("ready", "已运行 echo hi，写了 note.txt。", None)]
    steps = [(e["name"], e["status"], e["input"]) for e in job.events if e["type"] == "step"]
    assert steps == [("note", "done", ""), ("shell", "running", "echo hi"), ("shell", "done", "echo hi"),
                     ("write_file", "running", "note.txt"), ("write_file", "done", "note.txt")]
    [first] = calls(log)
    assert first[:2] == ["exec", "--json"] and 'sandbox_mode="workspace-write"' in first
    assert "sandbox_workspace_write.network_access=false" in first
    # Codex has no separate system prompt: the rules lead its first prompt.
    assert first[-1].startswith("你是 Continuum 的 Worker") and "跑一下 echo hi" in first[-1]
    assert json.loads(state_path(work).read_text())["session"] == "01a1b38b-7adc-7240-8892-df920dd24921"

    asyncio.run(CodexExecutor().turn(ctx, FakeJob(work, [Item("再来")])))
    second = calls(log)[1]
    assert second[:3] == ["exec", "resume", "01a1b38b-7adc-7240-8892-df920dd24921"] and "Worker" not in second[-1]


def test_the_agent_process_is_isolated_from_the_hosts_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for path in (".ssh", ".config", ".claude", ".codex"):
        (home / path).mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    (home / ".netrc").write_text("machine x")
    workdir = home / "continuum-home" / "work" / "t-1"
    workdir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    job = FakeJob(workdir)

    claude = ClaudeCodeExecutor()._isolated(ClaudeCodeConfig(), job, ["claude", "-p", "x"])
    pairs = list(zip(claude, claude[1:], strict=False))
    assert ("--ro-bind", "/") in pairs and ("--tmpfs", str(home / ".ssh")) in pairs
    assert ("--tmpfs", str(home / ".config")) in pairs and ("--tmpfs", str(home / ".codex")) in pairs
    assert ("--ro-bind", "/dev/null") in pairs and str(home / ".netrc") in claude
    assert ("--tmpfs", str(home / "continuum-home")) in pairs
    assert ("--bind", str(home / ".claude")) in pairs and ("--bind", str(workdir)) in pairs
    # The workspace is shown again after its home is hidden.
    assert claude.index(str(home / "continuum-home")) < claude.index(str(workdir))
    assert claude[-3:] == ["claude", "-p", "x"]

    codex = CodexExecutor()._isolated(CodexConfig(), job, ["codex"])
    pairs = list(zip(codex, codex[1:], strict=False))
    assert ("--tmpfs", str(home / ".claude")) in pairs and ("--bind", str(home / ".codex")) in pairs


def test_with_the_whole_machine_the_agents_run_unisolated_unsandboxed_and_unasked(tmp_path):
    job = FakeJob(tmp_path)
    full = {"full_access": True}
    assert ClaudeCodeExecutor()._isolated(ClaudeCodeConfig(**full), job, ["claude", "-p", "x"]) == ["claude", "-p", "x"]
    claude = ClaudeCodeExecutor().command(ClaudeCodeConfig(**full), job, "task", None, "s-1", "rules")
    assert claude[claude.index("--permission-mode") + 1] == "bypassPermissions"
    assert json.loads(claude[claude.index("--settings") + 1])["sandbox"]["enabled"] is False
    assert "完全权限" in claude[claude.index("--append-system-prompt") + 1]
    # Without it, the sandbox stays on and a run that says it is off is stopped.
    kept = ClaudeCodeExecutor().command(ClaudeCodeConfig(), job, "task", None, "s-1", "rules")
    assert json.loads(kept[kept.index("--settings") + 1])["sandbox"]["failIfUnavailable"] is True
    codex = CodexExecutor().command(CodexConfig(**full), job, "task", None, "s-1", "rules")
    assert "--dangerously-bypass-approvals-and-sandbox" in codex and not any("workspace-write" in a for a in codex)
    assert "完全权限" in codex[-1]


@pytest.mark.parametrize("kind", ["claude_code", "codex"])
def test_the_runtime_takes_either_agent_as_its_executor(tmp_path, kind):
    raw = tomllib.loads(DEFAULT_TOML)
    raw["executor"] = {"kind": kind, "config": {"isolation": "none", "command": "/bin/true"}}
    raw["sources"]["cli"]["config"] = {"poll_seconds": 0.01}

    async def main():
        rt = Runtime(tmp_path, Settings.model_validate(raw), plugins=BUILTINS, entry_points=False)
        try:
            await drive(rt, lambda: True)
            return type(rt.executor.context.config).__name__
        finally:
            await rt.close()

    assert asyncio.run(main()) == {"claude_code": "ClaudeCodeConfig", "codex": "CodexConfig"}[kind]


def test_the_deepseek_worker_hands_a_task_to_claude_code_and_reports_its_result(tmp_path):
    from test_agent import ScriptedModel, StreamingModel, agent_runtime, call

    log = tmp_path / "log"
    claude = fake(tmp_path, "claude", CLAUDE)
    router = ScriptedModel(call("decide", decision="accept", title="写个脚本", scope="写并运行脚本",
                                acknowledgement="好，我来安排。", name="Script", reason="明确请求"))
    worker = StreamingModel(
        call("claude_code", task="写一个 note.txt，内容 ok，并运行 echo hi"),
        lambda messages: call("claude_code", task="第二轮：再检查一遍"),
        lambda messages: call("report", state="ready", summary=f"Claude Code 说：{messages[-1]['content']}"))

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}]}}})
        rt.settings.executor.config["claude_code"] = {"command": claude, "isolation": "none",
                                                      "env": {"FAKE_LOG": str(log)}}
        rt.executor.context.config = type(rt.executor.context.config).model_validate(rt.settings.executor.config)
        ctx = rt.sources["app"]
        try:
            rt.start()
            ctx.kind.post(ctx, "me", "main", "写个脚本跑一下")
            done = lambda: any(r["role"] == "reply" for r in ctx.kind.history(ctx, "me", "main"))  # noqa: E731
            await drive(rt, done, timeout=15)
            return ctx.kind.history(ctx, "me", "main"), ctx.kind.steps(ctx, "me", "main"), worker.seen
        finally:
            await rt.close()

    history, steps, seen = asyncio.run(main())
    assert history[-1]["text"] == "Claude Code 说：好的，按你说的改完了。"
    # The worker was told it has Claude Code, and was offered the tool.
    assert "claude_code" in seen[0][0]["content"]
    names = [(s["name"], s["status"]) for s in steps]
    assert names[0] == ("claude_code", "done") and ("shell", "done") in names and ("write_file", "done") in names
    assert steps[0]["input"].startswith("写一个 note.txt") and "跑完了" in steps[0]["output"]
    # Its first call started a session with the tool's rules; the second went on in it.
    first, second = calls(log)
    assert "交回给 Worker" in first[first.index("--append-system-prompt") + 1]
    assert second[second.index("--resume") + 1] == first[first.index("--session-id") + 1]


def test_an_agent_installed_for_the_user_is_found_off_the_services_path(tmp_path, monkeypatch):
    from continuum.plugins import cli_agents

    home = tmp_path / "home"
    (home / ".npm-global" / "bin").mkdir(parents=True)
    tool = home / ".npm-global" / "bin" / "claude"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli_agents.resolve("claude") == str(tool)
    assert cli_agents.resolve("no-such-agent") is None

    job = FakeJob(tmp_path / "work" / "t")
    config = ClaudeCodeConfig(command="no-such-agent", isolation="none")
    asyncio.run(ClaudeCodeExecutor().turn(ExecutorContext(config=config, scripts={}), job))
    [(state, summary, _)] = job.reports
    assert state == "failed" and "找不到命令 no-such-agent" in summary


def test_a_job_on_one_server_goes_to_the_agent_on_that_server_with_full_access(tmp_path):
    """The dispatcher hands a job confined to one server to the coding agent there, over ssh."""
    from continuum.plugins import cli_agents
    from continuum.runtime import ssh as ssh_rules

    server = tmp_path / "server-home"
    (server / ".local" / "bin").mkdir(parents=True)
    (server / "app").mkdir()
    (server / ".local" / "bin" / "claude").symlink_to(fake(tmp_path, "claude", CLAUDE))
    log = tmp_path / "log"
    # Stands in for ssh: runs the command it is given in a shell, as the server's user would.
    stand_in = fake(tmp_path, "ssh", "import os, subprocess, sys\n"
                    f"env = {{'HOME': {str(server)!r}, 'PATH': '/usr/bin:/bin', 'FAKE_LOG': {str(log)!r}}}\n"
                    "sys.exit(subprocess.run(['/bin/sh', '-c', sys.argv[-1]], env=env, cwd='/').returncode)\n")

    class Hosts:
        def ssh_argv(self, name):
            return [stand_in, name]

    job = FakeJob(tmp_path / "work")

    async def main():
        first = await cli_agents.remote_delegate("claude_code", ClaudeCodeConfig(command="/somewhere/claude"), job,
                                                 Hosts(), "crm", "看一下日志", cwd="~/app")
        second = await cli_agents.remote_delegate("claude_code", ClaudeCodeConfig(), job, Hosts(), "crm", "第二轮：接着")
        nowhere = await cli_agents.remote_delegate("claude_code", ClaudeCodeConfig(), job, Hosts(), "crm", "x",
                                                   cwd="/no/such/dir", fresh=True)
        missing = await cli_agents.remote_delegate("codex", CodexConfig(), job, Hosts(), "crm", "x")
        return first, second, nowhere, missing

    first, second, nowhere, missing = asyncio.run(main())
    assert first == "跑完了，输出 hi，写了 note.txt。" and second == "好的，按你说的改完了。"
    assert nowhere == "错误：crm 上的 Claude Code没有完成：服务器上没有目录 /no/such/dir"
    assert missing == "错误：crm 上的 Codex没有完成：这台服务器上没有装 codex"
    one, two = calls(log)
    assert one[one.index("--permission-mode") + 1] == "bypassPermissions"
    assert "服务器 crm" in one[one.index("--append-system-prompt") + 1]
    # Each (agent, server) goes on in its own session.
    assert two[two.index("--resume") + 1] == one[one.index("--session-id") + 1]
    # Its steps show in the ticket.
    assert any(e.get("name") == "shell" for e in job.events)

    class Probed:
        def __init__(self):
            self.kept = {"name": "crm"}

        async def run(self, name, command, timeout=60):
            return 0, "agent:claude_code\nagent-out:codex\n"

        def get(self, name):
            return dict(self.kept)

        def keep(self, name, host):
            self.kept = host

    hosts = Probed()
    assert asyncio.run(ssh_rules.probe_agents(hosts, "crm")) == ["claude_code"] and hosts.kept["agents"] == ["claude_code"]
    assert hosts.kept["agents_out"] == ["codex"]  # installed, signed out
    # A run that finds the sign-in gone takes the agent off the list.
    ssh_rules.signed_out(hosts, "crm", "claude_code")
    assert hosts.kept["agents"] == [] and hosts.kept["agents_out"] == ["claude_code", "codex"]
