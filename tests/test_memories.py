"""What the person asks the assistant to remember: kept per person, told to every worker of theirs, shown and taken back."""

import asyncio

import httpx

from test_agent import ScriptedModel, agent_runtime, call
from test_api import serve, until


def test_a_preference_is_remembered_not_made_a_project_and_every_later_worker_is_told(tmp_path):
    router = ScriptedModel(*[call("decide", decision="accept", title=t, scope=t, acknowledgement="好。", reason="r")
                             for t in ("记住", "干活")])
    worker = ScriptedModel(
        call("remember", text="尽量用 Claude Code，不行再用 Codex"),
        lambda messages: call("report", state="ready", summary=messages[-1]["content"]),
        lambda messages: call("report", state="ready", summary="知道" if "尽量用 Claude Code" in messages[0]["content"]
                              else "不知道"))

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}, {"id": "bob"}]}}},
                           api={"enabled": True, "port": 0})
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)
        replies = lambda: [m["text"] for m in ctx.kind.everything(ctx, "me") if m["role"] == "reply"]  # noqa: E731
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "记住，以后尽量用 claude"})
                await until(lambda: len(replies()) == 1, timeout=15)
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "帮我看看 crm"})
                await until(lambda: len(replies()) == 2, timeout=15)
                kept = (await c.get("/api/v1/memories", headers=me)).json()
                theirs = (await c.get("/api/v1/memories", headers=bob)).json()
                added = (await c.post("/api/v1/memories", headers=me, json={"text": "报告用表格"})).json()
                gone = await c.delete(f"/api/v1/memories/{kept[0]['id']}", headers=me)
                missing = await c.delete(f"/api/v1/memories/{kept[0]['id']}", headers=me)
                left = (await c.get("/api/v1/memories", headers=me)).json()
                return replies(), kept, theirs, added, gone.status_code, missing.status_code, left, rt.projects.list("me")
        finally:
            runner.cancel()
            await rt.close()

    said, kept, theirs, added, gone, missing, left, projects = asyncio.run(main())
    assert said[0].startswith("已记下 [") and said[1] == "知道"
    assert [m["text"] for m in kept] == ["尽量用 Claude Code，不行再用 Codex"] and theirs == []
    assert projects == []  # a preference is not a project
    assert (gone, missing) == (200, 404) and [m["text"] for m in left] == [added["text"]] == ["报告用表格"]
    # The worker was told what a long-running project is for, and what to remember instead.
    rules = worker.seen[0][0]["content"]
    assert "用 remember 记下来" in rules and "不要为它提议长期项目" in rules
