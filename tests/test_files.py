"""Files the assistant shows the person: put in their conversation, fetched by their own devices only."""

import asyncio
import base64
import sys

import httpx

from continuum.runtime.files import Files
from continuum.runtime.ssh import Host

from test_agent import ScriptedModel, StreamingModel, agent_runtime, call
from test_api import serve, until

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_a_worker_shows_a_file_from_here_and_from_a_server_and_only_its_person_may_fetch_them(tmp_path):
    server = tmp_path / "server-home"
    (server / "reports").mkdir(parents=True)
    (server / "reports" / "week.csv").write_text("零件,数量\nR12,40\n", encoding="utf-8")
    # Stands in for ssh: runs the command it is given in a shell, as the server's user would.
    stand_in = tmp_path / "fake-ssh"
    stand_in.write_text(f"#!{sys.executable}\nimport subprocess, sys\n"
                        f"sys.exit(subprocess.run(['/bin/sh', '-c', sys.argv[-1]], env={{'HOME': {str(server)!r}, "
                        "'PATH': '/usr/bin:/bin'}).returncode)\n")
    stand_in.chmod(0o755)
    router = ScriptedModel(call("decide", decision="accept", title="画图", scope="画图", acknowledgement="好。", reason="r"))
    worker = StreamingModel(
        lambda messages: call("shell", command=f"printf '%s' '{base64.b64encode(PNG).decode()}' | base64 -d > plot.png"),
        call("show_file", path="plot.png", caption="**本周**的走势"),
        call("show_file", path="~/reports/week.csv", host="crm"),
        call("show_file", path="nothing.png"),
        lambda messages: call("report", state="ready", summary=f"图发了。{messages[-1]['content'][:20]}"))

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}, {"id": "bob"}]}}},
                           api={"enabled": True, "port": 0})
        rt.settings.ssh.admins = ["me"]
        rt.settings.ssh.ssh_command = str(stand_in)
        rt.ssh.put(Host(name="crm", address="34.1.2.3", user="u", approval="auto"))
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "画个图给我看"})
                await until(lambda: any(m["role"] == "reply" for m in ctx.kind.everything(ctx, "me")), timeout=15)
                shown = (await c.get("/api/v1/messages", headers=me)).json()
                files = [m for m in shown if m["role"] == "attachment"]
                got = [(await c.get(f"/api/v1/files/{m['meta']['files'][0]['id']}", headers=me)) for m in files]
                theirs = await c.get(f"/api/v1/files/{files[0]['meta']['files'][0]['id']}", headers=bob)
                return files, got, theirs, shown[-1]["text"], ctx.kind.steps(ctx, "me", None)
        finally:
            runner.cancel()
            await rt.close()

    files, got, theirs, said, steps = asyncio.run(main())
    assert [m["meta"]["files"][0]["name"] for m in files] == ["plot.png", "week.csv"]
    assert files[0]["text"] == "**本周**的走势" and files[0]["meta"]["files"][0]["mime"] == "image/png"
    assert files[0]["project"] and files[0]["project"] == files[1]["project"]  # in the project's own conversation
    assert got[0].content == PNG and got[0].headers["content-type"] == "image/png"
    assert got[1].text == "零件,数量\nR12,40\n" and got[1].headers["content-type"].startswith("text/csv")
    assert "owner" not in files[0]["meta"]["files"][0]
    assert theirs.status_code == 404  # someone else's file is as missing as one that never was
    assert "没有这个文件" in said
    assert [(s["input"], s["status"]) for s in steps if s["name"] == "show_file"] == [
        ("plot.png", "done"), ("crm  $ ~/reports/week.csv", "done"), ("nothing.png", "error")]


def test_files_are_kept_by_an_unguessable_id_and_stay_within_their_size(tmp_path):
    files = Files(tmp_path)
    meta = files.keep("me", "../../etc/passwd", b"x")
    assert meta["name"] == "passwd" and len(meta["id"]) == 24 and files.get(meta["id"])[0].read_bytes() == b"x"
    assert files.get("../" + meta["id"]) is None and files.get("0" * 24) is None
    try:
        files.keep("me", "big.bin", b"0" * (25 * 1024 * 1024 + 1))
        raise AssertionError("kept a file over the limit")
    except ValueError as error:
        assert "太大" in str(error)


def test_a_file_in_the_persons_own_work_opens_by_its_path_and_nothing_else_on_the_host_does(tmp_path):
    router = ScriptedModel(*[call("decide", decision="accept", title="清单", scope="写清单", acknowledgement="好。",
                                  reason="r") for _ in range(2)])
    worker = ScriptedModel(
        call("write_file", path="清单.md", content="# 进度\n| 项 | 状态 |\n|---|---|\n| 发票 | 已改 |\n"),
        lambda messages: call("report", state="ready", summary="写好了"),
        lambda messages: call("report", state="ready", summary="bob 的"))

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}, {"id": "bob"}]}}},
                           api={"enabled": True, "port": 0})
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "写个清单"})
                await until(lambda: any(m["role"] == "reply" for m in ctx.kind.everything(ctx, "me")), timeout=15)
                [ticket] = rt.store.tickets.list()
                path = f"{ticket.execution.directory}/清单.md"
                (tmp_path / "home" / "secret.txt").write_text("no")
                get = lambda p, who=me: c.get("/api/v1/workfile", params={"path": p}, headers=who)  # noqa: E731
                return (await get(path), await get(path, bob), await get(str(tmp_path / "home" / "secret.txt")),
                        await get(f"{ticket.execution.directory}/../../secret.txt"), await get("/etc/passwd"))
        finally:
            runner.cancel()
            await rt.close()

    mine, theirs, outside, climbed, system = asyncio.run(main())
    assert mine.status_code == 200 and "| 发票 | 已改 |" in mine.text and mine.headers["content-type"].startswith("text/markdown")
    assert [r.status_code for r in (theirs, outside, climbed, system)] == [404, 404, 404, 404]
