"""F2c: the chat model client, the sandbox, the model router and the agent executor.

The router and the agent are driven by scripted models, so the tests are exact and need no network; the real model
is exercised by test_live.py on request.
"""

import asyncio
import json
import tomllib

import httpx
import pytest

from continuum.models import ChatModel, ModelError, ModelSettings, partial_string, tool_calls
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


def test_a_router_answer_without_a_decision_still_takes_the_request(tmp_path):
    # Seen from DeepSeek: every field of an accept but the decision itself. Only an explicit ignore drops a message.
    router = ScriptedModel(call("decide", title="写并运行 fizzbuzz 脚本", scope="写 fizzbuzz.py 并运行",
                                acknowledgement="好的，我来写。", name="fizzbuzz-script", reason="明确的任务"))

    async def main():
        rt = agent_runtime(tmp_path, router, ScriptedModel(call("report", state="ready", summary="写好了")))
        try:
            post(tmp_path, "写一个 fizzbuzz.py")
            await drive(rt, lambda: len(replies(tmp_path)) == 2, timeout=10)
            [ticket] = rt.store.tickets.list()
            assert ticket.title == "写并运行 fizzbuzz 脚本"
        finally:
            await rt.close()

    asyncio.run(main())
    assert replies(tmp_path) == [("acknowledgement", "好的，我来写。"), ("reply", "写好了")]


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




# --- streaming: what a person sees while the worker works ---------------------------------------------------------


def _sse(*chunks):
    lines = [f"data: {json.dumps({'choices': [{'delta': c}]}, ensure_ascii=False)}\n\n" for c in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def test_the_model_client_streams_and_puts_the_message_back_together(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(
            {"role": "assistant", "reasoning_content": "想"}, {"reasoning_content": "一想"},
            {"content": "先看看"},
            {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "report", "arguments": '{"state": "ready", '}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"summary": "做完\\n了'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}))

    monkeypatch.setenv("TEST_KEY", "k")
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    model = ChatModel("t", ModelSettings(api_key_env="TEST_KEY"), transport=httpx.MockTransport(handler))
    seen = []

    async def main():
        message = await model.stream([{"role": "user", "content": "hi"}], tools=[{"type": "function"}],
                                     on_progress=lambda m: seen.append(json.loads(json.dumps(m))))
        await model.close()
        return message

    message = asyncio.run(main())
    assert calls[-1]["stream"] is True and len(calls) == 2
    assert message["content"] == "先看看" and message["reasoning_content"] == "想一想"
    assert tool_calls(message) == [("c1", "report", {"state": "ready", "summary": "做完\n了"})]
    assert [partial_string(m["tool_calls"][0]["function"]["arguments"], "summary") for m in seen
            if m.get("tool_calls")] == [None, "做完\n了", "做完\n了"]


def test_partial_strings_read_as_far_as_they_have_come():
    assert partial_string('{"state": "rea', "state") == "rea"
    assert partial_string('{"summary": "a\\"b\\u4e2', "summary") == 'a"b'
    assert partial_string('{"summary": "a\\', "summary") == "a"
    assert partial_string('{"state": "ready"}', "summary") is None


class StreamingModel(ScriptedModel):
    """A scripted model that also streams: each answer's arguments arrive a few characters at a time."""

    async def stream(self, messages, tools=None, on_progress=None, **extra):
        message = await self.complete(messages, tools)
        if message.get("tool_calls") and on_progress:
            raw = message["tool_calls"][0]["function"]["arguments"]
            for end in range(1, len(raw) + 1, 3):
                partial = json.loads(json.dumps(message))
                partial["tool_calls"][0]["function"]["arguments"] = raw[:end]
                on_progress(partial)
        return message


def test_a_desktop_conversation_shows_the_steps_and_the_answer_as_it_is_written(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="跑一下", scope="运行 echo", acknowledgement="好。",
                                name="Run", reason="明确请求"))
    worker = StreamingModel(
        {"role": "assistant", "content": "我先跑一下。", "reasoning_content": "要用 shell",
         "tool_calls": [{"id": "s1", "type": "function",
                         "function": {"name": "shell", "arguments": json.dumps({"command": "echo hi"})}}]},
        call("report", state="ready", summary="跑完了，输出是 hi。"))

    async def main():
        rt = agent_runtime(tmp_path, router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}]}}})
        ctx = rt.sources["app"]
        events = []
        queue = ctx.kind.subscribe("me")
        try:
            rt.start()
            ctx.kind.post(ctx, "me", "main", "跑一下 echo hi")
            done = lambda: any(r["role"] == "reply" for r in ctx.kind.history(ctx, "me", "main"))  # noqa: E731
            await drive(rt, done, timeout=15)
            while not queue.empty():
                events.append(queue.get_nowait())
            return events, ctx.kind.steps(ctx, "me", "main"), rt.store.tickets.list()[0]
        finally:
            await rt.close()

    events, steps, ticket = asyncio.run(main())
    assert [(s["name"], s["status"]) for s in steps] == [("think", "done"), ("note", "done"), ("shell", "done")]
    assert steps[2]["input"] == "echo hi" and "hi" in steps[2]["output"] and steps[2]["ticket_id"] == ticket.id
    # The shell step was shown while it ran, then again when it ended.
    assert [e["step"]["status"] for e in events if e["type"] == "step" and e["step"]["name"] == "shell"] == \
        ["running", "done"]
    written = [e["text"] for e in events if e["type"] == "stream"]
    assert written and written[-1] == "跑完了，输出是 hi。" and all("跑完了，输出是 hi。".startswith(t) for t in written)
    assert {e["thread"] for e in events if e["type"] in ("stream", "thinking", "idle")} == {"main"}
    assert any(e["type"] == "thinking" for e in events) and events[-1]["type"] != "thinking"


def test_a_model_that_fails_leaves_the_ticket_waiting_for_the_person(tmp_path):
    class Broken(ScriptedModel):
        async def complete(self, messages, tools=None, tool_choice=None, **extra):
            raise ModelError("HTTP 400: bad request")

    router = ScriptedModel(call("decide", decision="accept", title="x", scope="x", reason="r"))

    async def main():
        rt = agent_runtime(tmp_path, router, Broken())
        try:
            post(tmp_path, "做点事")
            await drive(rt, lambda: bool(rt.store.tickets.list()) and rt.store.tickets.list()[0].attention is not None,
                        timeout=10)
            [ticket] = rt.store.tickets.list()
            assert "回复一句可以让我再试" in ticket.attention.question
        finally:
            await rt.close()

    asyncio.run(main())


def test_an_answer_written_out_instead_of_reported_still_reaches_the_person(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="看 GitHub", scope="看", acknowledgement="好。",
                                reason="r"))
    worker = ScriptedModel({"role": "assistant", "content": "能访问。你的账号是 YuxiangLin-kaslana。"})

    async def main():
        rt = agent_runtime(tmp_path, router, worker)
        try:
            post(tmp_path, "你能看到我的 GitHub 吗")
            await drive(rt, lambda: len(replies(tmp_path)) == 2, timeout=10)
            [ticket] = rt.store.tickets.list()
            assert ticket.ended is not None and ticket.ended.outcome == "completed"
        finally:
            await rt.close()

    asyncio.run(main())
    assert replies(tmp_path)[-1] == ("reply", "能访问。你的账号是 YuxiangLin-kaslana。")


def test_a_transcript_cut_off_while_tools_ran_is_mended_before_it_goes_back_to_the_model():
    from continuum.plugins.agent_executor import _mend

    cut = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
           {"role": "assistant", "content": "", "tool_calls": [
               {"id": "a", "type": "function", "function": {"name": "ssh", "arguments": "{}"}},
               {"id": "b", "type": "function", "function": {"name": "ssh", "arguments": "{}"}},
               {"id": "c", "type": "function", "function": {"name": "ssh", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "a", "content": "ok"},
           {"role": "user", "content": "【系统】The host restarted"}]
    mended = _mend(cut)
    assert [(m["role"], m.get("tool_call_id")) for m in mended] == [
        ("system", None), ("user", None), ("assistant", None), ("tool", "a"), ("tool", "b"), ("tool", "c"),
        ("user", None)]
    assert "被中断了" in mended[4]["content"]
    assert _mend(mended) == mended  # whole transcripts are left as they are
