"""The person's own prompt: at the very end of each model call of their work while switched on, kept nowhere else."""

import asyncio
import json

import httpx

from continuum.plugins.agent_executor import transcript_path

from test_agent import ScriptedModel, agent_runtime, call
from test_api import serve, until

SECRET = "这是人自己的提示词，不该在任何别的地方出现。"


def install(home):
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "inject.md").write_text(SECRET, encoding="utf-8")
    (home / "prompts" / "inject.json").write_text(json.dumps({"name": "Dsh.md", "chars": len(SECRET)}), encoding="utf-8")


def test_switched_on_it_ends_every_call_and_stays_in_none_and_switched_off_it_is_gone(tmp_path):
    home = tmp_path / "home"
    install(home)
    router = ScriptedModel(*[call("decide", decision="accept", title="查", scope="查", acknowledgement="好。", reason="r")
                             for _ in range(2)])
    worker = ScriptedModel(
        call("shell", command="echo 1"), call("shell", command="echo 2"), call("report", state="ready", summary="好了"),
        call("report", state="ready", summary="又好了"))

    async def main():
        rt = agent_runtime(home, router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}, {"id": "bob"}]}}},
                           api={"enabled": True, "port": 0})
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)
        replies = lambda: [m for m in ctx.kind.everything(ctx, "me") if m["role"] == "reply"]  # noqa: E731
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                shown = (await c.get("/api/v1/me/inject", headers=me)).json()
                switched = (await c.put("/api/v1/me/inject", headers=me, json={"enabled": True})).json()
                theirs = (await c.get("/api/v1/me/inject", headers=bob)).json()
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "查一下"})
                await until(lambda: len(replies()) == 1, timeout=15)
                await c.put("/api/v1/me/inject", headers=me, json={"enabled": False})
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "再查一下"})
                await until(lambda: len(replies()) == 2, timeout=15)
                return shown, switched, theirs, rt.store.tickets.list()
        finally:
            runner.cancel()
            await rt.close()

    shown, switched, theirs, tickets = asyncio.run(main())
    # The app learns that there is one, and its name; never its words. Each person has their own switch.
    assert shown == {"available": True, "name": "Dsh.md", "chars": len(SECRET), "enabled": False}
    assert switched["enabled"] is True and SECRET not in json.dumps(switched)
    assert theirs["enabled"] is False
    on, off = worker.seen[:3], worker.seen[3:]
    for sent in on:
        # The very end of every call, and only there.
        assert sent[-1] == {"role": "system", "content": SECRET}
        assert sum(1 for m in sent if m.get("content") == SECRET) == 1
    assert len(on[2]) > len(on[0])  # the conversation went on, and still it is only at the end
    for sent in off:
        assert all(m.get("content") != SECRET for m in sent)
    # Kept nowhere: not in a transcript, not in the conversation, not in the router's view.
    for ticket in tickets:
        saved = transcript_path(home / "work" / ticket.execution.name).read_text(encoding="utf-8")
        assert SECRET not in saved
    assert all(SECRET not in json.dumps(m, ensure_ascii=False) for seen in router.seen for m in seen)


def test_it_cannot_be_switched_on_without_a_prompt_on_the_server(tmp_path):
    router, worker = ScriptedModel(), ScriptedModel()

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}]}}}, api={"enabled": True, "port": 0})
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                return ((await c.get("/api/v1/me/inject", headers=me)).json(),
                        (await c.put("/api/v1/me/inject", headers=me, json={"enabled": True})).status_code)
        finally:
            runner.cancel()
            await rt.close()

    info, refused = asyncio.run(main())
    assert info == {"available": False, "name": None, "chars": None, "enabled": False} and refused == 409
