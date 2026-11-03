"""The whole runtime: a command-line message is accepted, worked and answered; a crash loses nothing."""

import asyncio
import tomllib

import pytest

from continuum.plugin import Plugin
from continuum.plugins import BUILTINS, cli_source
from continuum.runtime import ConfigurationError, HomeLocked, Runtime, Settings
from continuum.runtime.settings import DEFAULT_TOML


def settings(script: str = "echo", **changes) -> Settings:
    raw = tomllib.loads(DEFAULT_TOML)
    raw["executor"]["config"]["script"] = script
    raw["sources"]["cli"]["config"] = {"poll_seconds": 0.01}
    raw["tick_seconds"] = 0.01
    for key, value in changes.items():
        raw[key] = value
    return Settings.model_validate(raw)


def runtime(home, script="echo", extra=(), **changes) -> Runtime:
    return Runtime(home, settings(script, **changes), plugins=[*BUILTINS, *extra], entry_points=False)


async def drive(rt: Runtime, until, timeout: float = 5.0) -> None:
    rt.start()
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not until():
        if loop.time() > end:
            raise AssertionError("timed out")
        await rt.pass_once()
        await asyncio.sleep(0.01)


def replies(home, thread="main"):
    return [(r["kind"], r["text"]) for r in cli_source.replies(cli_source.db_path(home), thread)]


def test_a_request_is_accepted_worked_and_answered(tmp_path):
    async def main():
        rt = runtime(tmp_path)
        try:
            cli_source.post(cli_source.db_path(tmp_path), "me", "main", "检查 BOM 报价")
            await drive(rt, lambda: len(replies(tmp_path)) == 2)
            [ticket] = rt.store.tickets.list()
            assert ticket.ended.outcome == "completed"
            assert (tmp_path / "work" / ticket.execution.name / "result.md").exists()
        finally:
            await rt.close()

    asyncio.run(main())
    assert [k for k, _ in replies(tmp_path)] == ["acknowledgement", "reply"]


def test_a_follow_up_resumes_the_ended_ticket_and_a_stranger_is_turned_away(tmp_path):
    async def main():
        rt = runtime(tmp_path)
        try:
            db = cli_source.db_path(tmp_path)
            cli_source.post(db, "me", "main", "检查 BOM 报价")
            await drive(rt, lambda: len(replies(tmp_path)) == 2)
            cli_source.post(db, "me", "main", "顺便核一下交期", mention=False)
            await drive(rt, lambda: len(replies(tmp_path)) == 4)
            cli_source.post(db, "stranger", "other", "也帮我做一个")
            await drive(rt, lambda: any(d.kind == "ignore" for d in rt.store.decisions.by_thread("cli:other")))
            assert len(rt.store.tickets.list()) == 1
        finally:
            await rt.close()

    asyncio.run(main())
    assert replies(tmp_path)[2:] == [("acknowledgement", "收到补充：顺便核一下交期"),
                                     ("reply", replies(tmp_path)[3][1])]
    assert "顺便核一下交期" in replies(tmp_path)[3][1]


def test_a_crash_mid_work_loses_nothing_and_sends_nothing_twice(tmp_path):
    state = {"runs": 0, "recovered": False}

    async def slow(job):
        state["runs"] += 1
        items = await job.inputs()
        if any(i.kind == "text" and "restarted" in i.text for i in items):
            state["recovered"] = True
            job.report("ready", "done after restart")
            return
        await asyncio.sleep(3600)

    plugin = Plugin(name="slow", scripts={"slow": slow})

    async def first():
        rt = runtime(tmp_path, "slow", [plugin])
        cli_source.post(cli_source.db_path(tmp_path), "me", "main", "a slow job")
        await drive(rt, lambda: state["runs"] == 1)
        # A crash: the process goes away mid-turn. Nothing in the domain is told.
        await rt.close()

    async def second():
        rt = runtime(tmp_path, "slow", [plugin])
        try:
            await drive(rt, lambda: len(replies(tmp_path)) == 2)
            [ticket] = rt.store.tickets.list()
            assert ticket.ended.outcome == "completed"
            assert [a.ended_reason for a in ticket.assignments] == ["hostRestarted", None]
        finally:
            await rt.close()

    asyncio.run(first())
    asyncio.run(second())
    assert state == {"runs": 2, "recovered": True}
    assert [k for k, _ in replies(tmp_path)] == ["acknowledgement", "reply"]


def test_a_worker_that_raises_is_reported_failed_and_waits_for_a_person(tmp_path):
    async def broken(job):
        raise RuntimeError("supplier API down")

    async def main():
        rt = runtime(tmp_path, "broken", [Plugin(name="broken", scripts={"broken": broken})])
        try:
            cli_source.post(cli_source.db_path(tmp_path), "me", "main", "check")
            await drive(rt, lambda: bool(rt.store.tickets.list()) and bool(
                rt.store.tickets.list()[0].assignments[-1].report))
            ticket = rt.store.tickets.list()[0]
            assert ticket.assignments[-1].report.state == "failed"
            assert "supplier API down" in ticket.assignments[-1].report.summary
            assert ticket.ended is None
        finally:
            await rt.close()

    asyncio.run(main())


def test_configuration_problems_are_named(tmp_path):
    with pytest.raises(ConfigurationError, match="router kind"):
        runtime(tmp_path, router={"kind": "oracle"})
    rt = runtime(tmp_path, sources={"cli": {"kind": "cli", "config": {"poll_seconds": -1}},
                                     "mail": {"kind": "imap"}})
    try:
        assert rt.sources == {}
        assert any("mail" in p for p in rt.problems) and any("cli" in p for p in rt.problems)
        with pytest.raises(HomeLocked):
            runtime(tmp_path)
    finally:
        asyncio.run(rt.close())


def test_a_policy_change_between_starts_tells_the_open_tickets_it_bears_on(tmp_path):
    async def hold(job):
        await asyncio.sleep(3600)

    plugin = Plugin(name="hold", scripts={"hold": hold})

    async def first():
        rt = runtime(tmp_path, "hold", [plugin])
        cli_source.post(cli_source.db_path(tmp_path), "me", "main", "keep going")
        await drive(rt, lambda: bool(rt.executor.sessions))
        await rt.close()

    async def second():
        raw = tomllib.loads(DEFAULT_TOML)
        raw["policy"]["sources"]["cli"]["allowed"] = [{"id": "someone-else"}]
        rt = runtime(tmp_path, "hold", [plugin], policy=raw["policy"])
        try:
            rt.start()
            [ticket] = rt.store.tickets.list()
            changed = [i for i in ticket.inputs if i.kind == "policy-changed"]
            assert changed and changed[0].effect.requester_allowed is False
        finally:
            await rt.close()

    asyncio.run(first())
    asyncio.run(second())
