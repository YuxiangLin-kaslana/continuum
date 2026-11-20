"""Claude Code as the brain: the router's decision and a lead's turn by `claude -p`, Continuum's tools lent over MCP."""

import asyncio
import json
import sys

from test_agent import ScriptedModel, call
from test_runtime import drive

# Stands in for `claude -p`: as a router, answers the schema; as a worker, starts the MCP server it is given, lists
# the tools, calls one, and answers in Claude Code's stream.
FAKE_CLAUDE = r'''
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
if "--json-schema" in argv:
    card = argv[argv.index("-p") + 1]
    if "BROKEN" in card:
        print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached"}))
    else:
        print(json.dumps({"type": "result", "is_error": False, "structured_output": {
            "decision": "accept", "title": "Claude 定的", "scope": "做", "acknowledgement": "好，Claude 接了。",
            "reason": "请求"}}))
    sys.exit(0)
prompt = argv[argv.index("-p") + 1]
def out(e):
    print(json.dumps(e, ensure_ascii=False), flush=True)
if "QUOTA" in prompt:
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "usage limit reached"})
    sys.exit(1)
server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["continuum"]
mcp = subprocess.Popen([server["command"], *server["args"]], env={**os.environ, **server["env"]},
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def rpc(ident, method, params):
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
    mcp.stdin.flush()
    return json.loads(mcp.stdout.readline())
rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
names = [t["name"] for t in rpc(2, "tools/list", {})["result"]["tools"]]
out({"type": "system", "subtype": "init", "session_id": "s-1"})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__continuum__remember",
                                                    "input": {"text": "尽量用 Claude"}}]}})
got = rpc(3, "tools/call", {"name": "remember", "arguments": {"text": "尽量用 Claude"}})["result"]
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": got["content"]}]}})
mcp.stdin.close()
answer = "Claude 做完了：" + got["content"][0]["text"] + "｜工具：" + ",".join(sorted(names))
out({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": "s-1"})
'''


def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


def brain_runtime(home, router, worker):
    import tomllib

    from continuum.plugins import BUILTINS
    from continuum.runtime import Runtime, Settings
    from continuum.runtime.settings import DEFAULT_TOML

    raw = tomllib.loads(DEFAULT_TOML)
    raw.update(sources={"app": {"kind": "app"}}, tick_seconds=0.01,
               policy={"sources": {"app": {"allowed": [{"id": "me"}]}}},
               router={"kind": "model", "config": {"model": "router", "engine": "claude"}},
               executor={"kind": "agent", "config": {"model": "worker", "brain": "claude",
                                                     "sandbox": {"kind": "host", "timeout_seconds": 10}}})
    return Runtime(home, Settings.model_validate(raw), plugins=BUILTINS, entry_points=False,
                   models={"router": router, "worker": worker})


def test_claude_decides_and_works_the_turn_with_continuums_tools_and_deepseek_is_not_asked(tmp_path, monkeypatch):
    log = fake_claude(tmp_path, monkeypatch)
    router, worker = ScriptedModel(), ScriptedModel()

    async def main():
        rt = brain_runtime(tmp_path / "home", router, worker)
        ctx = rt.sources["app"]
        try:
            rt.start()
            ctx.kind.post(ctx, "me", "main", "记一下：尽量用 Claude")
            done = lambda: any(r["role"] == "reply" for r in ctx.kind.history(ctx, "me", "main"))  # noqa: E731
            await drive(rt, done, timeout=20)
            return ctx.kind.history(ctx, "me", "main"), ctx.kind.steps(ctx, "me", None), rt.memories.list("me")
        finally:
            await rt.close()

    history, steps, memories = asyncio.run(main())
    said = {m["role"]: m["text"] for m in history}
    assert said["acknowledgement"] == "好，Claude 接了。"
    assert said["reply"].startswith("Claude 做完了：已记下 [")
    # Continuum's tools were lent (not the ones Claude has of its own), and the call ran as Continuum's.
    lent = said["reply"].split("｜工具：")[1].split(",")
    assert "remember" in lent and "show_file" in lent and "shell" not in lent and "report" not in lent
    assert [m["text"] for m in memories] == ["尽量用 Claude"]
    assert [(s["name"], s["status"]) for s in steps if s["name"] == "remember"] == [("remember", "done")]
    assert not any(s["name"].startswith("mcp__") for s in steps)  # shown once, as Continuum's own step
    assert router.seen == [] and worker.seen == []
    worker_call = next(json.loads(line) for line in log.read_text().splitlines() if "--mcp-config" in line)
    assert worker_call[worker_call.index("--permission-mode") + 1] == "bypassPermissions"
    assert "mcp__continuum" in worker_call and "【你的工具】" in worker_call[worker_call.index("--append-system-prompt") + 1]


def test_when_claude_cannot_deepseek_decides_and_works(tmp_path, monkeypatch):
    fake_claude(tmp_path, monkeypatch)
    router = ScriptedModel(call("decide", decision="accept", title="DeepSeek 定的", scope="做", acknowledgement="好。",
                                reason="r"))
    worker = ScriptedModel(lambda messages: call("report", state="ready", summary="DeepSeek 接手：" + (
        "知道" if any("没能完成" in str(m.get("content")) for m in messages) else "不知道")))

    async def main():
        rt = brain_runtime(tmp_path / "home", router, worker)
        ctx = rt.sources["app"]
        try:
            rt.start()
            ctx.kind.post(ctx, "me", "main", "BROKEN QUOTA 做点事")
            done = lambda: any(r["role"] == "reply" for r in ctx.kind.history(ctx, "me", "main"))  # noqa: E731
            await drive(rt, done, timeout=20)
            return ctx.kind.history(ctx, "me", "main")
        finally:
            await rt.close()

    history = asyncio.run(main())
    said = {m["role"]: m["text"] for m in history}
    assert said["acknowledgement"] == "好。" and said["reply"] == "DeepSeek 接手：知道"
