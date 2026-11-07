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
