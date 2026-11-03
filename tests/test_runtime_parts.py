"""The Store, the effect queue, the outbox and the plugin registry."""

import asyncio
from datetime import UTC, datetime

import pytest

from continuum.core import ChannelRef, OutboundMessage
from continuum.plugin import API_VERSION, Plugin
from continuum.runtime.effects import Effects, Later
from continuum.runtime.outbox import StoreOutbox
from continuum.runtime.registry import Registry
from continuum.runtime.store import SQLiteStore

from conftest import trigger


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "c.db")
    yield s
    s.close()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- Store --------------------------------------------------------------------------------------------------------


def test_the_store_keeps_data_across_reopening(tmp_path):
    first = SQLiteStore(tmp_path / "c.db")
    first.atomic(lambda: first.triggers.save(trigger("1")))
    first.set("policy", {"version": "v1"})
    first.close()
    again = SQLiteStore(tmp_path / "c.db")
    assert again.triggers.get("1").text == "message 1" and again.get("policy") == {"version": "v1"}
    again.close()


def test_a_nested_failure_rolls_back_only_its_savepoint_and_hooks_wait_for_the_commit(store):
    ran = []

    def inner():
        store.triggers.save(trigger("2"))
        store.after_commit(lambda: ran.append("inner"))
        raise ValueError("inner fails")

    def outer():
        store.triggers.save(trigger("1"))
        store.after_commit(lambda: ran.append("outer"))
        with pytest.raises(ValueError):
            store.atomic(inner)
        assert ran == []

    store.atomic(outer)
    assert (store.triggers.get("1") is not None, store.triggers.get("2")) == (True, None)
    assert ran == ["outer"]


def test_a_failed_transaction_leaves_nothing(store):
    def work():
        store.triggers.save(trigger("1"))
        raise RuntimeError

    with pytest.raises(RuntimeError):
        store.atomic(work)
    assert store.triggers.get("1") is None


# --- Effects ------------------------------------------------------------------------------------------------------


def test_an_effect_exists_exactly_when_its_transaction_commits(store):
    effects = Effects(store)

    def work():
        effects.enqueue("x", "k", {})
        raise RuntimeError

    with pytest.raises(RuntimeError):
        store.atomic(work)
    assert effects.pending() == []
    store.atomic(lambda: effects.enqueue("x", "k", {"n": 1}))
    store.atomic(lambda: effects.enqueue("x", "k", {"n": 2}))  # replaces the payload, keeps one row
    assert [row["payload"] for row in effects.pending()] == [{"n": 2}]


def test_a_failing_effect_backs_off_and_later_defers_without_counting(store):
    clock = Clock()
    effects = Effects(store, clock=clock)
    calls = []

    async def flaky(payload):
        calls.append(payload)
        if payload["mode"] == "later":
            raise Later(10)
        if len(calls) < 3:
            raise ConnectionError("down")

    effects.handle("x", flaky, base=2, cap=60)
    effects.enqueue("x", "a", {"mode": "fail"})
    asyncio.run(effects.run_due())
    [row] = effects.pending()
    assert (row["failures"], row["due"]) == (1, 1002)
    clock.now = 1002
    asyncio.run(effects.run_due())
    assert effects.pending()[0]["due"] == 1006  # 2, then 4 seconds
    clock.now = 1006
    asyncio.run(effects.run_due())
    assert effects.pending() == []
    effects.enqueue("x", "b", {"mode": "later"})
    asyncio.run(effects.run_due())
    assert (effects.pending()[0]["failures"], effects.pending()[0]["due"]) == (0, 1016)


def test_a_tickets_messages_to_one_conversation_go_out_in_order(store):
    sent = []

    async def transport(message):
        if message.text == "first" and len(sent) == 0 and not getattr(transport, "recovered", False):
            transport.recovered = True
            raise ConnectionError("down")
        sent.append(message.text)
        return "sent"

    clock = Clock()
    effects = Effects(store, clock=clock)
    outbox = StoreOutbox(store, effects, transport)
    channel = ChannelRef(source="cli", thread="main")
    store.atomic(lambda: outbox.send(OutboundMessage(id="1", channel=channel, text="first", kind="acknowledgement",
                                                     ticket_id="t")))
    store.atomic(lambda: outbox.send(OutboundMessage(id="2", channel=channel, text="second", kind="reply",
                                                     ticket_id="t")))
    asyncio.run(effects.run_due())
    assert sent == [] and outbox.status("2") == "pending"  # the second waits behind the failed first
    clock.now += 60
    asyncio.run(effects.run_due())
    clock.now += 60
    asyncio.run(effects.run_due())
    assert sent == ["first", "second"]
    assert (outbox.status("1"), outbox.status("2")) == ("sent", "sent")


def test_a_withdrawn_message_never_goes_out(store):
    sent = []

    async def transport(message):
        sent.append(message.id)
        return "sent"

    effects = Effects(store)
    outbox = StoreOutbox(store, effects, transport)
    message = OutboundMessage(id="q", channel=ChannelRef(source="cli", thread="main"), text="?", kind="reply")
    outbox.send(message)
    outbox.withdraw("q")
    asyncio.run(effects.run_due())
    assert sent == [] and outbox.status("q") == "dropped"


# --- Registry -----------------------------------------------------------------------------------------------------


def test_the_registry_refuses_a_wrong_version_and_a_taken_name_and_keeps_the_rest(tmp_path):
    registry = Registry()

    def noop(job):
        return None

    assert registry.add(Plugin(name="a", scripts={"run": noop}))
    assert not registry.add(Plugin(name="old", api_version="0", scripts={"other": noop}))
    assert not registry.add(Plugin(name="b", scripts={"run": noop}))  # the script name is taken
    assert not registry.add(Plugin(name="a"))
    assert list(registry.scripts) == ["run"] and len(registry.problems) == 3
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "good.py").write_text("from continuum.plugin import Plugin\n"
                                    "plugin = Plugin(name='good', scripts={'good': lambda job: None})\n")
    (folder / "broken.py").write_text("raise ImportError('missing dependency')\n")
    (folder / "empty.py").write_text("x = 1\n")
    registry.load(directory=folder, entry_points=False)
    assert "good" in registry.scripts
    assert any("broken.py" in p for p in registry.problems) and any("empty.py" in p for p in registry.problems)
    assert API_VERSION == "1"


def test_trigger_times_survive_the_store(store):
    store.atomic(lambda: store.triggers.save(trigger("7")))
    assert store.triggers.get("7").at.tzinfo is not None
    assert store.triggers.get("7").at > datetime(2026, 1, 1, tzinfo=UTC)
