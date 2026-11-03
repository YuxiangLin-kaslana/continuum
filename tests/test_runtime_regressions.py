"""Regressions from the F1 review: each test pins one bug that was found and fixed."""

import asyncio

from continuum.plugin import Plugin
from continuum.plugins import cli_source
from continuum.runtime.registry import Registry

from test_runtime import drive, replies, runtime


def post(home, text, sender="me", mention=True):
    cli_source.post(cli_source.db_path(home), sender, "main", text, mention=mention)


def test_a_follow_up_that_arrives_during_a_turn_is_not_lost_when_the_host_stops(tmp_path):
    state = {"turns": 0}

    async def busy(job):
        state["turns"] += 1
        await job.inputs()
        if state["turns"] == 1:
            await state["release"].wait()  # turn 1 runs until the follow-up is in its inbox
            return
        await asyncio.sleep(3600)  # turn 2 is cut off by the host stopping

    plugin = Plugin(name="busy", scripts={"busy": busy})

    async def first():
        state["release"] = asyncio.Event()
        rt = runtime(tmp_path, "busy", [plugin])
        post(tmp_path, "a job")
        await drive(rt, lambda: state["turns"] == 1)
        post(tmp_path, "and one more thing", mention=False)
        await drive(rt, lambda: any(s.inbox for s in rt.executor.sessions.values()))
        state["release"].set()
        await drive(rt, lambda: state["turns"] == 2)
        await rt.close()

    async def second():
        rt = runtime(tmp_path, "busy", [plugin])
        try:
            rt.start()
            [ticket] = rt.store.tickets.list()
            # The worker was busy with the follow-up, so it starts again with a recovery note.
            assert ticket.assignments[0].ended_reason == "hostRestarted"
        finally:
            await rt.close()

    asyncio.run(first())
    asyncio.run(second())


def test_a_worker_that_never_reads_its_inputs_does_not_loop(tmp_path):
    turns = []

    def deaf(job):
        turns.append(1)

    async def main():
        rt = runtime(tmp_path, "deaf", [Plugin(name="deaf", scripts={"deaf": deaf})])
        try:
            post(tmp_path, "a job")
            await drive(rt, lambda: len(turns) == 1)
            post(tmp_path, "are you there?", mention=False)
            await drive(rt, lambda: len(turns) >= 2)
            for _ in range(20):
                await rt.pass_once()
                await asyncio.sleep(0.01)
            [ticket] = rt.store.tickets.list()
            assert len(turns) == 2 and ticket.assignments[-1].report.state == "waiting"
        finally:
            await rt.close()

    asyncio.run(main())


def test_a_persons_message_during_a_failed_turn_gets_another_turn(tmp_path):
    seen = []

    async def flaky(job):
        items = await job.inputs()
        seen.extend(i.text for i in items)
        if not seen:
            await asyncio.sleep(0.05)  # long enough for the follow-up to arrive
            raise RuntimeError("first try fails")
        job.report("ready", "done")

    async def main():
        rt = runtime(tmp_path, "flaky", [Plugin(name="flaky", scripts={"flaky": flaky})])
        try:
            post(tmp_path, "a job")
            await drive(rt, lambda: bool(rt.executor.sessions))
            post(tmp_path, "try again please", mention=False)
            await drive(rt, lambda: rt.store.tickets.list()[0].ended is not None)
            assert "try again please" in seen
        finally:
            await rt.close()

    asyncio.run(main())


class ExplodingGate:
    name = "explodes"

    def gate(self, trigger, thread, tickets, env):
        raise KeyError("bad plugin")

    def revalidate(self, mandate, env):
        return True


def test_a_gate_rule_that_raises_does_not_stop_the_host(tmp_path):
    async def main():
        rt = runtime(tmp_path, extra=[Plugin(name="bad", gate_rules=[ExplodingGate()])], gate=["explodes", "standard"])
        try:
            post(tmp_path, "a job")
            await drive(rt, lambda: any("evaluate" in p for p in rt.problems))
            await rt.pass_once()  # still running
        finally:
            await rt.close()

    asyncio.run(main())


def test_a_local_plugin_file_may_use_dataclasses(tmp_path):
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "typed.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from continuum.plugin import Plugin\n"
        "@dataclass\n"
        "class Options:\n"
        "    loud: bool = False\n"
        "plugin = Plugin(name='typed', scripts={'typed': lambda job: None})\n")
    registry = Registry()
    registry.load(directory=folder, entry_points=False)
    assert "typed" in registry.scripts, registry.problems


def test_nothing_launches_or_goes_out_while_paused(tmp_path):
    async def main():
        rt = runtime(tmp_path, policy={"paused": True, "sources": {"cli": {"allowed": [{"id": "me"}]}}})
        try:
            post(tmp_path, "a job")
            for _ in range(30):
                await rt.pass_once()
                await asyncio.sleep(0.01)
            assert rt.executor.sessions == {} and replies(tmp_path) == []
        finally:
            await rt.close()

    asyncio.run(main())
