"""Projects: one-off requests shown as projects, long-running ones proposed by a worker and confirmed by the person,
their schedules (a reminder in the conversation, or a run in the project's workspace), all in one conversation."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx

from continuum.runtime.projects import Schedule

from test_agent import ScriptedModel, StreamingModel, agent_runtime, call
from test_api import serve
from test_ssh import _until


def test_schedules_fall_on_the_persons_clock():
    now = datetime(2026, 11, 7, 5, 0, tzinfo=UTC)  # 13:00 in Shanghai, a Saturday
    local = lambda s: s.following(now, "Asia/Shanghai").astimezone(  # noqa: E731
        __import__("zoneinfo").ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M %a")
    assert local(Schedule(text="x", every="day", at="09:00")) == "11-08 09:00 Sun"
    assert local(Schedule(text="x", every="day", at="14:00")) == "11-07 14:00 Sat"
    assert local(Schedule(text="x", every="week", weekday=5, at="17:00")) == "11-13 17:00 Fri"
    assert local(Schedule(text="x", every="month", day=1, at="8:00")) == "12-01 08:00 Tue"
    assert local(Schedule(text="x", every="once", once_at="2026-11-08 09:30")) == "11-08 09:30 Sun"
    assert Schedule(text="x", every="once", once_at="2026-11-01 09:30").following(now, "Asia/Shanghai") is None
    assert Schedule(text="x", every="hours", interval=2).following(now, "UTC") == now + timedelta(hours=2)
    assert Schedule(text="x", every="day", at="09:00").describe("Asia/Shanghai") == "每天 09:00（Asia/Shanghai）"


def test_a_worker_proposes_a_project_the_person_confirms_and_it_reminds_and_runs_on_its_own(tmp_path):
    router = ScriptedModel(
        call("decide", decision="accept", title="每天巡检", scope="每天检查磁盘", acknowledgement="好。", name="check",
             reason="请求"),
        call("decide", decision="accept", title="巡检一次", scope="按项目巡检", acknowledgement="开始巡检。",
             name="run", reason="定时任务"))
    worker = StreamingModel(
        call("propose_project", name="服务器巡检", goal="每天检查服务器磁盘", charter="只读检查，超过 80% 提醒我",
             schedules=[{"action": "remind", "text": "记得看巡检结果", "every": "day", "at": "09:00"},
                        {"action": "run", "text": "检查磁盘", "every": "day", "at": "08:30"}]),
        call("report", state="ready", summary="提议了「服务器巡检」，请确认。"),
        # The scheduled run: in the project's workspace, with its goal, leaving notes for the next run.
        call("write_file", path="NOTES.md", content="11-07 磁盘 41%\n"),
        call("report", state="ready", summary="磁盘 41%，正常。"))

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}, {"id": "bob"}]}}},
                           api={"enabled": True, "port": 0})
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)

        def said(role):
            return [m for m in ctx.kind.everything(ctx, "me") if m["role"] == role]

        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                assert (await c.put("/api/v1/me/timezone", headers=me, json={"timezone": "Mars/Base"})).status_code == 422
                await c.put("/api/v1/me/timezone", headers=me, json={"timezone": "Asia/Shanghai"})
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "以后每天帮我巡检服务器磁盘"})
                await _until(lambda: bool(said("proposal")) and bool(said("reply")))
                [proposal] = said("proposal")
                project_id = proposal["meta"]["project"]
                listed = (await c.get("/api/v1/projects", headers=me)).json()
                ongoing = next(p for p in listed if p["id"] == project_id)
                assert ongoing["state"] == "proposed" and ongoing["kind"] == "ongoing" and ongoing["next_at"] is None
                assert [s["text_when"] for s in ongoing["schedules"]] == [
                    "每天 09:00（Asia/Shanghai）", "每天 08:30（Asia/Shanghai）"]
                # The request that proposed it is a one-off project of its own.
                assert any(p["kind"] == "once" and p["name"] == "每天巡检" for p in listed)
                # Only its person decides; a proposal is answered once.
                assert (await c.post(f"/api/v1/projects/{project_id}/confirm", headers=bob)).status_code == 404
                assert (await c.post(f"/api/v1/projects/{project_id}/confirm", headers=me)).json()["state"] == "active"
                assert (await c.post(f"/api/v1/projects/{project_id}/confirm", headers=me)).status_code == 409
                assert said("proposal")[0]["meta"]["state"] == "active"
                [started] = said("notice")
                assert started["text"].startswith("项目「服务器巡检」开始了，下一次在")

                # Both schedules come due: a reminder in the main conversation, and a run in the project.
                project = rt.projects.get(project_id)
                for schedule in project.schedules:
                    schedule.next_at = datetime.now(UTC) - timedelta(seconds=1)
                rt.projects._save(project)
                rt._clock_at = 0
                rt._tick_projects()
                [reminder] = said("reminder")
                assert reminder["thread"] == "main" and reminder["text"] == "记得看巡检结果"
                assert reminder["meta"]["project"] == project_id
                await _until(lambda: len(said("reply")) == 2)
                [scheduled] = said("scheduled")
                assert scheduled["thread"] == f"p-{project_id}" and scheduled["text"] == "【定时任务】检查磁盘"
                workspace = tmp_path / "home" / "projects" / project_id
                assert (workspace / "NOTES.md").read_text() == "11-07 磁盘 41%\n"
                assert "Goal: 每天检查服务器磁盘" in worker.seen[2][1]["content"]
                assert "只读检查，超过 80% 提醒我" in worker.seen[2][1]["content"]
                # Planned again for tomorrow, not fired twice.
                assert all(s.next_at > datetime.now(UTC) for s in rt.projects.get(project_id).schedules)
                rt._clock_at = 0
                rt._tick_projects()
                assert len(said("reminder")) == 1

                # One conversation: every message, each tagged with the project it belongs to.
                everything = (await c.get("/api/v1/messages", headers=me)).json()
                tags = {m["role"]: m["project"] for m in everything if m["role"] in ("reminder", "scheduled", "proposal")}
                assert tags == {"reminder": project_id, "scheduled": project_id, "proposal": project_id}
                run_reply = [m for m in everything if m["role"] == "reply"][-1]
                assert run_reply["text"] == "磁盘 41%，正常。" and run_reply["project"] == project_id
                ongoing = next(p for p in (await c.get("/api/v1/projects", headers=me)).json() if p["id"] == project_id)
                assert ongoing["runs"] == 1 and ongoing["next_at"] is not None

                # Talking to the project; pausing and ending it.
                said_to = await c.post(f"/api/v1/threads/p-{project_id}/messages", headers=me, json={"text": "阈值改成 90%"})
                assert said_to.json()["project"] == project_id and said_to.json()["meta"]["project_name"] == "服务器巡检"
                assert (await c.post(f"/api/v1/threads/p-{project_id}/messages", headers=bob,
                                     json={"text": "x"})).status_code == 404
                assert (await c.post(f"/api/v1/projects/{project_id}/pause", headers=me)).json()["state"] == "paused"
                assert (await c.post(f"/api/v1/projects/{project_id}/end", headers=me)).json()["state"] == "ended"
                assert (await c.post(f"/api/v1/threads/p-{project_id}/messages", headers=me,
                                     json={"text": "x"})).status_code == 409
                assert rt.projects.due(datetime.now(UTC) + timedelta(days=2)) == []
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())
    saved = json.loads((tmp_path / "home" / "work").glob("*.agent.json").__next__().read_text())
    assert saved[0]["role"] == "system"


def test_a_message_reaches_no_earlier_work_by_itself_and_goes_where_the_router_sends_it(tmp_path):
    """Seen live: one message in the conversation reached every earlier ticket there, and two unrelated ones each
    took it up. Now a message is a Thread of its own; the router opens a project for it, or forwards it."""
    seen_cards = []

    class Router(ScriptedModel):
        async def complete(self, messages, tools=None, tool_choice=None, **extra):
            seen_cards.append(messages[1]["content"])
            return await super().complete(messages, tools, tool_choice, **extra)

    router = Router(
        call("decide", decision="accept", title="解释 LLM", scope="解释", acknowledgement="好。", reason="请求"),
        call("decide", decision="accept", title="列封装", scope="列", acknowledgement="好。", reason="请求"),
        # Something new: a project of its own, not a turn of the two earlier ones.
        call("decide", decision="accept", title="看邮件提醒", scope="提醒", acknowledgement="好。", reason="请求"),
        # A follow-up of the first: forwarded to it.
        lambda messages: call("decide", decision="forward", project=next(
            line.split()[1].removeprefix("id=") for line in messages[1]["content"].splitlines()
            if "解释 LLM" in line), reason="是 LLM 那件事的后续"))
    worker = StreamingModel(
        call("report", state="ready", summary="LLM 是大语言模型。"),
        call("report", state="ready", summary="0402、0603、0805。"),
        call("report", state="ready", summary="好的，会提醒。"),
        lambda messages: call("report", state="ready",
                              summary=f"补充：{'再说说 LLM 的上下文长度' in messages[-1]['content']}"))

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}]}}},
                           api={"enabled": True, "port": 0})
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)
        replies = lambda: [m for m in ctx.kind.everything(ctx, "me") if m["role"] == "reply"]  # noqa: E731
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                for n, text in enumerate(["什么是 LLM", "常见贴片电阻封装", "以后提醒我看邮件"], 1):
                    await c.post("/api/v1/threads/main/messages", headers=me, json={"text": text})
                    await _until(lambda n=n: len(replies()) == n)
                tickets = rt.store.tickets.list()
                assert len(tickets) == 3
                # The earlier work never saw the later messages.
                for ticket in tickets:
                    assert [i.kind for i in ticket.inputs if i.kind in ("trigger", "resumed")] == []
                llm = next(t for t in tickets if t.title == "解释 LLM")
                assert f"id={llm.id} 解释 LLM（一次性，已结束）" in seen_cards[2]

                posted = (await c.post("/api/v1/threads/main/messages", headers=me,
                                       json={"text": "再说说 LLM 的上下文长度"})).json()
                await _until(lambda: len(replies()) == 4)
                assert len(rt.store.tickets.list()) == 3  # no new project for a follow-up
                # The router saw the recent conversation, each message with the project it belongs to.
                assert f"[{llm.id}] 助理：LLM 是大语言模型。" in seen_cards[3] and "最近的对话" in seen_cards[3]
                assert replies()[-1]["ticket_id"] == llm.id and replies()[-1]["text"] == "补充：True"
                forwarded = next(m for m in ctx.kind.everything(ctx, "me") if m["id"] == posted["id"])
                assert forwarded["meta"] == {"project": llm.id, "project_name": "解释 LLM"}
                tagged = {m["id"]: m["project"] for m in (await c.get("/api/v1/messages", headers=me)).json()}
                assert tagged[posted["id"]] == llm.id
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


class LeadAndCrew:
    """A model for a lead and its sub-workers, answering by what it is asked (they run at once, in any order)."""

    def __init__(self):
        self.seen = []

    async def complete(self, messages, tools=None, tool_choice=None, **extra):
        self.seen.append([dict(m) for m in messages])
        system = messages[0]["content"]
        if system.startswith("你是 Continuum 的一个子 Worker"):
            name = messages[1]["content"].split("你的子任务（")[1].split("）")[0]
            if messages[-1]["role"] == "tool":
                return call("finish", summary=f"{name}：{messages[-1]['content'].splitlines()[-1]}")
            return call("shell", command=f"sleep 1; echo {name.split('·')[1]} ok")
        if messages[-1]["role"] == "tool" and "【1·" in messages[-1]["content"]:
            parts = [p for p in messages[-1]["content"].split("\n") if p.startswith("【")]
            return call("report", state="ready", summary=f"三台都查完了：{' / '.join(parts)}")
        assert any(t["function"]["name"] == "dispatch" for t in tools)
        return call("dispatch", tasks=[{"name": n, "task": f"检查 {n} 的磁盘"} for n in ("andrew", "bestek", "crm")])

    async def close(self):
        pass


def test_a_lead_sends_workers_out_side_by_side_within_the_crews_limit(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="查三台服务器", scope="查磁盘",
                                acknowledgement="好，分头去查。", reason="请求"))
    worker = LeadAndCrew()

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}]}}},
                           api={"enabled": True, "port": 0})
        rt.crew = type(rt.crew)(2)  # two places: the third waits its turn
        rt.executor.context.crew = rt.crew
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)
        most = 0
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "查一下 andrew、bestek、crm 的磁盘"})
                await _until(lambda: bool(rt.crew.active), timeout=10)
                page = (await c.get("/api/v1/workers", headers=me)).json()
                assert page["crew"] and page["crew"][0]["title"] == "查三台服务器"
                assert page["busy_now"] == 1 + len(page["crew"]) and page["running"][0]["crew"] == len(page["crew"])
                started = asyncio.get_running_loop().time()
                while not any(m["role"] == "reply" for m in ctx.kind.everything(ctx, "me")):
                    most = max(most, len(rt.crew.active))
                    assert asyncio.get_running_loop().time() - started < 15, "timed out"
                    await asyncio.sleep(0.02)
                return ctx.kind.everything(ctx, "me"), ctx.kind.steps(ctx, "me", None), most
        finally:
            runner.cancel()
            await rt.close()

    messages, steps, most = asyncio.run(main())
    reply = [m for m in messages if m["role"] == "reply"][-1]["text"]
    assert reply.startswith("三台都查完了：") and all(f"·{n}：{n} ok" in reply for n in ("andrew", "bestek", "crm"))
    assert most == 2  # never more at once than the crew has places
    dispatch = next(s for s in steps if s["name"] == "dispatch")
    assert dispatch["status"] == "done" and dispatch["input"] == "派出 3 个 Worker：andrew、bestek、crm"
    shells = sorted((s["worker"], s["status"]) for s in steps if s["name"] == "shell")
    assert shells == [("1·andrew", "done"), ("2·bestek", "done"), ("3·crm", "done")]
    # Each sub-worker got the project's background and only its own task, and could not talk to the person.
    subs = [m for m in worker.seen if m[0]["content"].startswith("你是 Continuum 的一个子 Worker")]
    assert all("项目背景：" in m[1]["content"] for m in subs)
