"""F2c: the chat model client, the sandbox, the model router and the agent executor.

The router and the agent are driven by scripted models, so the tests are exact and need no network; the real model
is exercised by test_live.py on request.
"""

import asyncio
import json
import tomllib

import httpx
import pytest

from continuum.models import ChatModel, ModelError, ModelSettings, tool_calls
from continuum.plugins import BUILTINS, cli_source, sandbox
from continuum.plugins.agent_executor import transcript_path
from continuum.plugins.sandbox import SandboxConfig
from continuum.runtime import Runtime, Settings
from continuum.runtime.settings import DEFAULT_TOML

from test_runtime import drive, replies

# --- the model client ---------------------------------------------------------------------------------------------


def test_the_model_client_retries_a_rate_limit_and_parses_tool_calls(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": None, "reasoning_content": "thinking",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "shell", "arguments": '{"command": "ls"}'}}]}}]})

    monkeypatch.setenv("TEST_KEY", "k")
    model = ChatModel("t", ModelSettings(api_key_env="TEST_KEY", extra={"thinking": {"type": "disabled"}}),
                      transport=httpx.MockTransport(handler))
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    async def main():
        message = await model.complete([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        await model.close()
        return message

    message = asyncio.run(main())
    assert len(calls) == 2 and calls[1]["thinking"] == {"type": "disabled"} and calls[1]["model"] == "deepseek-flash"
    assert tool_calls(message) == [("c1", "shell", {"command": "ls"})]
    assert message["reasoning_content"] == "thinking"  # kept, so it can be sent back


async def _no_sleep(_):
    return None


def test_the_model_client_names_a_missing_key_and_a_hard_error(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    model = ChatModel("t", ModelSettings(api_key_env="NO_SUCH_KEY"))
    with pytest.raises(ModelError, match="no API key"):
        asyncio.run(model.complete([]))
    monkeypatch.setenv("TEST_KEY", "k")
    bad = ChatModel("t", ModelSettings(api_key_env="TEST_KEY"),
                    transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad request")))
    with pytest.raises(ModelError, match="HTTP 400"):
        asyncio.run(bad.complete([]))


# --- the sandbox --------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not sandbox.available(), reason="needs bubblewrap")
def test_the_sandbox_sees_only_its_workspace_has_no_network_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    work = tmp_path / "work"
    config = SandboxConfig(timeout_seconds=2)

    async def main():
        wrote = await sandbox.run("echo made > out.txt && pwd", work, config)
        home = await sandbox.run(f"ls {tmp_path} 2>&1; ls /home 2>&1", work, config)
        env = await sandbox.run("env", work, config)
        net = await sandbox.run("python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 53), 2)' "
                                "&& echo NET || echo NONET", work, config)
        slow = await sandbox.run("sleep 30", work, config)
        return wrote, home, env, net, slow

    wrote, home, env, net, slow = asyncio.run(main())
    assert wrote.exit_code == 0 and "/work" in wrote.output and (work / "out.txt").read_text() == "made\n"
    assert "No such file" in home.output
    assert "must-not-leak" not in env.output
    assert "NONET" in net.output
    assert slow.timed_out


# --- the router and the agent, scripted ---------------------------------------------------------------------------


def call(tool_name, /, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call-{tool_name}", "type": "function",
         "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class ScriptedModel:
    """Answers with the next step of its script; a step may be a function of the messages so far."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    async def complete(self, messages, tools=None, tool_choice=None, **extra):
        self.seen.append([dict(m) for m in messages])
        if not self.steps:
            raise AssertionError("the script ran out")
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step

    async def close(self):
        pass


def agent_runtime(home, router, worker, **changes):
    raw = tomllib.loads(DEFAULT_TOML)
    raw["sources"] = {"cli": {"kind": "cli", "config": {"poll_seconds": 0.01}}}
    raw["tick_seconds"] = 0.01
    raw["router"] = {"kind": "model", "config": {"model": "router"}}
    raw["executor"] = {"kind": "agent", "config": {"model": "worker", "sandbox": {
        "kind": "bwrap" if sandbox.available() else "none", "timeout_seconds": 10}}}
    raw.update(changes)
    return Runtime(home, Settings.model_validate(raw), plugins=BUILTINS, entry_points=False,
                   models={"router": router, "worker": worker})


def post(home, text, mention=True):
    cli_source.post(cli_source.db_path(home), "me", "main", text, mention=mention)


def test_the_model_router_accepts_and_the_agent_works_in_the_sandbox_and_delivers(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="写个问候脚本", scope="写 hello.py 并运行",
                                acknowledgement="好，我来写 hello.py 并跑一遍。", name="Hello Script", reason="明确请求"))
    worker = ScriptedModel(
        call("write_file", path="hello.py", content="print('hello from continuum')\n"),
        call("shell", command="python3 hello.py"),
        lambda messages: call("report", state="ready",
                              summary=f"hello.py 已写好，运行输出：{messages[-1]['content'].splitlines()[-1]}"))

    async def main():
        rt = agent_runtime(tmp_path, router, worker)
        try:
            post(tmp_path, "写一个 hello.py 并运行，告诉我输出")
            await drive(rt, lambda: len(replies(tmp_path)) == 2, timeout=15)
            [ticket] = rt.store.tickets.list()
            assert ticket.ended.outcome == "completed" and ticket.execution.name.startswith("hello-script-")
            assert (tmp_path / "work" / ticket.execution.name / "hello.py").exists()
            return ticket
        finally:
            await rt.close()

    ticket = asyncio.run(main())
    assert replies(tmp_path) == [("acknowledgement", "好，我来写 hello.py 并跑一遍。"),
                                 ("reply", "hello.py 已写好，运行输出：hello from continuum")]
    # The router saw the message as data; the worker's first message carries the assignment.
    assert "写一个 hello.py" in router.seen[0][1]["content"]
    assert "Request from me: 写一个 hello.py" in worker.seen[0][1]["content"]
    saved = json.loads(transcript_path(tmp_path / "work" / ticket.execution.name).read_text())
    assert [m["role"] for m in saved][-2:] == ["assistant", "tool"]


def test_the_agent_asks_a_question_and_goes_on_with_the_answer(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="选封装", scope="定 R12 封装", reason="请求",
                                acknowledgement="好，我来看 R12。"))

    def answered(messages):
        answer = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        assert "0603" in answer and "【消息】" in answer
        trigger_id = answer.split("trigger_id=")[1].split()[0]
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "ack", "arguments": json.dumps(
                {"trigger_id": trigger_id, "text": "好，用 0603。"}, ensure_ascii=False)}},
            {"id": "b", "type": "function", "function": {"name": "report", "arguments": json.dumps(
                {"state": "ready", "summary": "R12 改成 0603，已更新 BOM。"}, ensure_ascii=False)}}]}

    worker = ScriptedModel(call("ask", question="R12 用 0402 还是 0603？", summary="比价完成"), answered)

    async def main():
        rt = agent_runtime(tmp_path, router, worker)
        try:
            post(tmp_path, "帮我定一下 R12 的封装")
            await drive(rt, lambda: len(replies(tmp_path)) == 2, timeout=15)
            assert replies(tmp_path)[1] == ("reply", "R12 用 0402 还是 0603？")
            assert rt.store.tickets.list()[0].attention is not None
            post(tmp_path, "用 0603 吧", mention=False)
            await drive(rt, lambda: len(replies(tmp_path)) == 4, timeout=15)
            [ticket] = rt.store.tickets.list()
            assert ticket.ended.outcome == "completed" and ticket.attention is None
        finally:
            await rt.close()

    asyncio.run(main())
    assert replies(tmp_path)[2:] == [("acknowledgement", "好，用 0603。"), ("reply", "R12 改成 0603，已更新 BOM。")]
    # The second turn went on from the first: the question it asked is in what it read.
    assert any(m.get("tool_calls") and "ask" in json.dumps(m["tool_calls"]) for m in worker.seen[1])


def test_a_worker_that_is_stuck_tells_the_person(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="部署", scope="部署", reason="请求"))
    worker = ScriptedModel(call("report", state="blocked", summary="需要生产环境的账号"))

    async def main():
        rt = agent_runtime(tmp_path, router, worker)
        try:
            post(tmp_path, "部署一下")
            await drive(rt, lambda: any(k == "reply" for k, _ in replies(tmp_path)), timeout=15)
        finally:
            await rt.close()

    asyncio.run(main())
    assert ("reply", "卡住了：需要生产环境的账号") in replies(tmp_path)


def test_the_model_router_ignores_chat_and_inferred_candidates_without_rules(tmp_path):
    router = ScriptedModel(call("decide", decision="ignore", reason="只是打招呼"))

    async def main():
        rt = agent_runtime(tmp_path, router, ScriptedModel())
        try:
            post(tmp_path, "早上好")
            await drive(rt, lambda: bool(rt.store.decisions.by_thread("cli:main")), timeout=10)
            [decision] = rt.store.decisions.by_thread("cli:main")
            assert decision.kind == "ignore" and decision.audit["router"]["reason"] == "只是打招呼"
            post(tmp_path, "随口一说", mention=False)  # inferred: no rule is enabled, so no model call
            await drive(rt, lambda: len(rt.store.decisions.by_thread("cli:main")) == 2, timeout=10)
            assert len(router.seen) == 1 and rt.store.tickets.list() == []
        finally:
            await rt.close()

    asyncio.run(main())


def test_the_agent_cannot_write_outside_its_workspace(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="写文件", scope="写", reason="请求"))
    worker = ScriptedModel(
        call("write_file", path="../escape.txt", content="x"),
        lambda messages: call("report", state="dropped", summary=messages[-1]["content"]))

    async def main():
        rt = agent_runtime(tmp_path, router, worker)
        try:
            post(tmp_path, "写个文件")
            await drive(rt, lambda: bool(rt.store.tickets.list()) and rt.store.tickets.list()[0].ended is not None,
                        timeout=15)
            return rt.store.tickets.list()[0]
        finally:
            await rt.close()

    ticket = asyncio.run(main())
    assert "outside /work" in ticket.assignments[-1].report.summary
    assert not (tmp_path / "work" / "escape.txt").exists()


