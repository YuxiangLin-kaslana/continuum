"""The HTTP and WebSocket API the desktop app uses, against a real server on a free port."""

import asyncio
import json

import httpx
import pytest
import websockets

from continuum.plugin import Plugin

from test_runtime import runtime


async def serve(rt):
    rt.start()
    port = await rt.serve_api()
    runner = asyncio.create_task(_passes(rt))
    return port, runner


async def _passes(rt):
    while True:
        await rt.pass_once()
        await asyncio.sleep(0.01)


def app_runtime(tmp_path, script="echo", extra=(), allowed=("me",)):
    policy = {"sources": {"app": {"allowed": [{"id": u} for u in allowed]}}}
    return runtime(tmp_path, script, extra, policy=policy, api={"enabled": True, "port": 0},
                   sources={"app": {"kind": "app"}})


async def until(predicate, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.02)


def test_a_desktop_conversation_end_to_end(tmp_path):
    async def main():
        rt = app_runtime(tmp_path)
        token = rt.accounts.add("me", "laptop")
        port, runner = await serve(rt)
        base = f"http://127.0.0.1:{port}"
        auth = {"Authorization": f"Bearer {token}"}
        events = []
        try:
            async with httpx.AsyncClient(base_url=base) as client:
                assert (await client.get("/api/health")).json() == {"ok": True}
                assert (await client.get("/api/v1/me")).status_code == 401
                assert (await client.get("/api/v1/me", headers=auth)).json()["user"] == "me"
                async with websockets.connect(f"ws://127.0.0.1:{port}/api/v1/stream?token={token}") as ws:
                    assert json.loads(await ws.recv())["type"] == "hello"

                    async def listen():
                        async for raw in ws:
                            events.append(json.loads(raw))

                    listener = asyncio.create_task(listen())
                    posted = await client.post("/api/v1/threads/bom/messages", headers=auth,
                                               json={"text": "检查 BOM 报价"})
                    assert posted.status_code == 201 and posted.json()["role"] == "user"
                    await until(lambda: any(e.get("message", {}).get("role") == "reply" for e in events))
                    listener.cancel()
                roles = [e["message"]["role"] for e in events if e["type"] == "message"]
                assert roles == ["user", "acknowledgement", "reply"]
                assert any(e["type"] == "ticket" and e["ticket"]["outcome"] == "completed" for e in events)
                history = (await client.get("/api/v1/threads/bom/messages", headers=auth)).json()
                assert [m["role"] for m in history] == ["user", "acknowledgement", "reply"]
                assert (await client.get("/api/v1/threads", headers=auth)).json()[0]["thread"] == "bom"
                [ticket] = (await client.get("/api/v1/tickets", headers=auth)).json()
                assert ticket["thread"] == "bom" and ticket["outcome"] == "completed"
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_users_see_only_their_own_conversations_and_tickets_and_can_stop_them(tmp_path):
    async def hold(job):
        await asyncio.sleep(3600)

    async def main():
        rt = app_runtime(tmp_path, "hold", [Plugin(name="hold", scripts={"hold": hold})], allowed=("ann", "bob"))
        ann = {"Authorization": f"Bearer {rt.accounts.add('ann')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                await client.post("/api/v1/threads/main/messages", headers=ann, json={"text": "a long job"})
                await until(lambda: bool(rt.executor.sessions))
                [ticket] = (await client.get("/api/v1/tickets?open=true", headers=ann)).json()
                assert ticket["stage"] == "working"
                assert (await client.get("/api/v1/tickets", headers=bob)).json() == []
                assert (await client.get("/api/v1/threads/main/messages", headers=bob)).json() == []
                assert (await client.get(f"/api/v1/tickets/{ticket['id']}", headers=bob)).status_code == 404
                assert (await client.post(f"/api/v1/tickets/{ticket['id']}/stop", headers=bob)).status_code == 404
                stopped = await client.post(f"/api/v1/tickets/{ticket['id']}/stop", headers=ann,
                                            json={"reason": "not needed"})
                assert stopped.json()["outcome"] == "stopped"
                await until(lambda: rt.executor.sessions == {})
                assert (await client.post("/api/v1/threads/bad name/messages", headers=ann,
                                           json={"text": "x"})).status_code == 422
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_a_password_login_gives_the_device_its_own_token_and_too_many_failures_lock_it(tmp_path):
    async def main():
        rt = app_runtime(tmp_path)
        rt.accounts.set_password("me", "correct horse")
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                ok = await client.post("/api/v1/login", json={"username": "me", "password": "correct horse",
                                                               "device": "laptop"})
                assert ok.status_code == 200
                token = ok.json()["token"]
                auth = {"Authorization": f"Bearer {token}"}
                assert (await client.get("/api/v1/me", headers=auth)).json()["user"] == "me"
                assert [d["device"] for d in rt.accounts.list()] == ["laptop"]
                # Wrong passwords and unknown users look the same.
                wrong = await client.post("/api/v1/login", json={"username": "me", "password": "nope"})
                ghost = await client.post("/api/v1/login", json={"username": "ghost", "password": "nope"})
                assert wrong.status_code == ghost.status_code == 401 and wrong.json() == ghost.json()
                for _ in range(4):
                    await client.post("/api/v1/login", json={"username": "me", "password": "nope"})
                locked = await client.post("/api/v1/login", json={"username": "me", "password": "correct horse"})
                assert locked.status_code == 429
                # Logging out gives the token back.
                assert (await client.post("/api/v1/logout", headers=auth)).json() == {"revoked": True}
                assert (await client.get("/api/v1/me", headers=auth)).status_code == 401
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_passwords_are_stored_as_salted_hashes_and_checked(tmp_path):
    from continuum.runtime.accounts import Accounts
    from continuum.runtime.store import SQLiteStore

    store = SQLiteStore(tmp_path / "c.db")
    accounts = Accounts(store)
    accounts.set_password("me", "correct horse")
    accounts.set_password("you", "correct horse")
    stored = [row[0] for row in store.db.execute("SELECT hash FROM passwords ORDER BY user")]
    assert all(h.startswith("scrypt$") and "correct horse" not in h for h in stored) and stored[0] != stored[1]
    assert accounts.check_password("me", "correct horse") and not accounts.check_password("me", "Correct horse")
    with pytest.raises(ValueError):
        accounts.set_password("me", "short")
    store.close()


def test_the_passwd_command_sets_a_password(tmp_path, monkeypatch):
    import io

    from continuum.runtime.accounts import Accounts
    from continuum.runtime.cli import main
    from continuum.runtime.store import SQLiteStore

    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    assert main(["--home", str(tmp_path), "user", "passwd", "me", "--password-stdin"]) == 0
    store = SQLiteStore(tmp_path / "continuum.db")
    assert Accounts(store).check_password("me", "a long enough password")
    store.close()
    # A short password is refused unless the person running the server says they mean it.
    monkeypatch.setattr("sys.stdin", io.StringIO("123456\n"))
    assert main(["--home", str(tmp_path), "user", "passwd", "me", "--password-stdin"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO("123456\n"))
    assert main(["--home", str(tmp_path), "user", "passwd", "me", "--password-stdin", "--allow-short"]) == 0
    store = SQLiteStore(tmp_path / "continuum.db")
    assert Accounts(store).check_password("me", "123456")
    store.close()


def test_a_revoked_token_no_longer_works(tmp_path):
    async def main():
        rt = app_runtime(tmp_path)
        token = rt.accounts.add("me")
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                auth = {"Authorization": f"Bearer {token}"}
                assert (await client.get("/api/v1/me", headers=auth)).status_code == 200
                rt.accounts.revoke("me")
                assert (await client.get("/api/v1/me", headers=auth)).status_code == 401
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_the_workers_page_shows_what_runs_now_and_what_ran(tmp_path):
    async def hold(job):
        job.activity({"type": "step", "key": "s1", "name": "shell", "input": "ls", "status": "done", "output": ""})
        job.activity({"type": "step", "key": "s2", "name": "claude_code", "input": "写", "status": "running",
                      "output": ""})
        await asyncio.sleep(3600)

    async def main():
        rt = app_runtime(tmp_path, "hold", [Plugin(name="hold", scripts={"hold": hold})], allowed=("ann", "bob"))
        ann = {"Authorization": f"Bearer {rt.accounts.add('ann')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                await client.post("/api/v1/threads/main/messages", headers=ann, json={"text": "a long job"})
                await until(lambda: bool(rt.executor.sessions))
                await until(lambda: rt.sources["app"].kind.latest_step(rt.sources["app"], "ann",
                                                                        next(iter(rt.executor.sessions))) is not None)
                page = (await client.get("/api/v1/workers", headers=ann)).json()
                assert page["max_workers"] == rt.settings.executor.max_workers and page["busy_now"] == 1
                [running] = page["running"]
                assert running["busy"] and running["title"] == "a long job" and running["number"] == 1
                assert running["current"]["name"] == "claude_code" and running["steps"] == {"shell": 1, "claude_code": 1}
                assert page["totals"]["assignments"] == 1 and page["tools"] == {"shell": 1, "claude_code": 1}
                # Someone else's workers are not shown.
                other = (await client.get("/api/v1/workers", headers=bob)).json()
                assert other["running"] == [] and other["totals"]["assignments"] == 0 and other["tools"] == {}

                await client.post(f"/api/v1/tickets/{running['ticket_id']}/stop", headers=ann, json={})
                await until(lambda: rt.executor.sessions == {})
                page = (await client.get("/api/v1/workers", headers=ann)).json()
                assert page["running"] == [] and page["busy_now"] == 0
                [done] = page["recent"]
                assert done["state"] == "ended" and done["outcome"] == "stopped" and done["ended_at"]
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_searching_the_conversation_finds_the_persons_own_messages_as_typed(tmp_path):
    async def main():
        rt = app_runtime(tmp_path, allowed=("ann", "bob"))
        ann = {"Authorization": f"Bearer {rt.accounts.add('ann')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                for text in ["检查 BOM 报价", "交期 100% 确认", "交期 100x 确认", "file_name.txt"]:
                    await client.post("/api/v1/threads/main/messages", headers=ann, json={"text": text})
                await client.post("/api/v1/threads/main/messages", headers=bob, json={"text": "bob 的报价"})
                found = lambda q, who=ann: client.get("/api/v1/search", params={"q": q}, headers=who)  # noqa: E731
                assert [m["text"] for m in (await found("报价")).json() if m["role"] == "user"] == ["检查 BOM 报价"]
                # What is typed is looked for as it is: % and _ are not wildcards.
                mine = lambda rows: [m["text"] for m in rows if m["role"] == "user"]  # noqa: E731
                assert mine((await found("100%")).json()) == ["交期 100% 确认"]
                assert mine((await found("e_n")).json()) == ["file_name.txt"]
                assert mine((await found("报价", bob)).json()) == ["bob 的报价"]
                assert (await found("")).status_code == 422
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_a_run_that_finished_its_turn_and_waits_for_no_one_is_closed_after_a_while(tmp_path):
    async def quiet(job):
        return None  # does its turn, reports nothing final

    async def main():
        rt = app_runtime(tmp_path, "quiet", [Plugin(name="quiet", scripts={"quiet": quiet})])
        port, runner = await serve(rt)
        ctx = rt.sources["app"]
        try:
            ctx.kind.post(ctx, "me", "main", "随便看看")
            def turned():
                found = rt.store.tickets.list()
                return bool(found) and bool(found[0].assignments) and found[0].assignments[-1].report is not None \
                    and found[0].assignments[-1].state == "running" and not any(
                        s.busy for s in rt.executor.sessions.values())

            await until(turned)
            [ticket] = rt.store.tickets.list()
            assert ticket.ended is None
            rt._idle_at = 0
            rt._tick_idle()
            assert rt.store.tickets.get(ticket.id).ended is None  # quiet, but not for long yet
            rt.IDLE_CLOSE = __import__("datetime").timedelta(seconds=0)

            def closed():
                rt._idle_at = 0
                rt._tick_idle()
                return rt.store.tickets.get(ticket.id).ended is not None

            await until(closed)
            assert rt.store.tickets.get(ticket.id).ended.outcome == "stopped"
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())
