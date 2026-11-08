"""The SSH servers page's backend: the list, the ssh_config it writes, connection tests, and who may use it."""

import asyncio
import subprocess
import sys

import httpx

from test_api import app_runtime, serve

FAKE_SSH = r'''
import sys
args = sys.argv[1:]
host = args[args.index("-F") + 2]
if "== services" in args[-1]:
    print("== host\nweb-box\nUbuntu 24.04\n== services\nnginx.service\nerp-api.service\n== listening\n0.0.0.0:443 nginx")
    print("== repos\n-- /home/u/erp  [feature/mrp]\n2026-11-07 MRP: 物料需求计算\n   (3 uncommitted changes)")
    sys.exit(0)
if host == "continuum-down":
    print("ssh: connect to host 10.128.0.20 port 22: Connection timed out")
    sys.exit(255)
if host == "continuum-locked":
    print("Yuxia@1.2.3.4: Permission denied (publickey).")
    sys.exit(255)
print(host.removeprefix("continuum-") + "-box")
'''


def ssh_runtime(tmp_path, admins=("me",)):
    key = tmp_path / "keys" / "continuum_ed25519"
    key.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "continuum@test", "-f", str(key)], check=True)
    fake = tmp_path / "fake-ssh"
    fake.write_text(f"#!{sys.executable}\n{FAKE_SSH}")
    fake.chmod(0o755)
    rt = app_runtime(tmp_path / "home", allowed=("me", "bob"))
    rt.settings.ssh.key = str(key)
    rt.settings.ssh.admins = list(admins)
    rt.settings.ssh.ssh_command = str(fake)
    return rt


def test_admins_add_test_and_remove_servers_and_others_cannot(tmp_path):
    async def main():
        rt = ssh_runtime(tmp_path)
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                page = (await c.get("/api/v1/ssh", headers=me)).json()
                assert page["admin"] and page["public_key"].startswith("ssh-ed25519 ") and page["hosts"] == []
                assert page["fingerprint"].startswith("SHA256:")
                # Someone who is not an admin sees nothing and changes nothing.
                assert (await c.get("/api/v1/ssh", headers=bob)).json() == {
                    "admin": False, "hosts": [], "public_key": None, "fingerprint": None}
                assert (await c.put("/api/v1/ssh/hosts/web1", headers=bob,
                                    json={"address": "1.2.3.4", "user": "Yuxia"})).status_code == 403

                saved = await c.put("/api/v1/ssh/hosts/bastion", headers=me,
                                    json={"address": "34.1.2.3", "user": "Yuxia", "approval": "read"})
                assert saved.status_code == 200 and saved.json()["approval_text"].startswith("只读命令")
                assert (await c.put("/api/v1/ssh/hosts/andrew", headers=me,
                                    json={"address": "10.128.0.20", "port": 22, "user": "Yuxia",
                                          "jump": "bastion"})).status_code == 200
                await c.put("/api/v1/ssh/hosts/down", headers=me, json={"address": "10.128.0.20", "user": "Yuxia"})
                await c.put("/api/v1/ssh/hosts/locked", headers=me, json={"address": "1.2.3.4", "user": "Yuxia"})

                # What a person may not enter.
                for name, body, words in [
                    ("Bad Name", {"address": "1.2.3.4", "user": "u"}, "名字"),
                    ("evil", {"address": "-oProxyCommand=touch /tmp/x", "user": "u"}, "地址"),
                    ("evil", {"address": "1.2.3.4", "user": "u; rm -rf /"}, "登录用户"),
                    ("evil", {"address": "1.2.3.4", "user": "u", "jump": "nowhere"}, "没有叫 nowhere"),
                    ("bastion", {"address": "34.1.2.3", "user": "u", "jump": "andrew"}, "圈"),
                ]:
                    refused = await c.put(f"/api/v1/ssh/hosts/{name}", headers=me, json=body)
                    assert refused.status_code == 422 and words in refused.json()["detail"], (name, refused.text)

                config = (tmp_path / "home" / "ssh" / "config").read_text()
                assert "Host continuum-andrew\n  HostName 10.128.0.20" in config
                assert "  ProxyJump continuum-bastion" in config and "StrictHostKeyChecking accept-new" in config
                assert f"IdentityFile {tmp_path / 'keys' / 'continuum_ed25519'}" in config
                assert "IdentitiesOnly yes" in config and "BatchMode yes" in config

                ok = (await c.post("/api/v1/ssh/hosts/andrew/test", headers=me)).json()
                assert ok["state"]["ok"] is True and ok["state"]["message"] == "andrew-box"
                down = (await c.post("/api/v1/ssh/hosts/down/test", headers=me)).json()
                assert down["state"]["ok"] is False and "连接超时" in down["state"]["message"]
                locked = (await c.post("/api/v1/ssh/hosts/locked/test", headers=me)).json()
                assert "没认这把公钥" in locked["state"]["message"]
                listed = {h["name"]: h["state"].get("ok") for h in (await c.get("/api/v1/ssh", headers=me)).json()["hosts"]}
                assert listed == {"andrew": True, "bastion": None, "down": False, "locked": False}

                # Changing where a host is forgets what its last test found.
                moved = await c.put("/api/v1/ssh/hosts/andrew", headers=me,
                                    json={"address": "10.128.0.99", "user": "Yuxia", "jump": "bastion"})
                assert moved.json()["state"]["ok"] is None

                # A jump host in use stays until nothing goes through it.
                assert (await c.delete("/api/v1/ssh/hosts/bastion", headers=me)).status_code == 409
                assert (await c.delete("/api/v1/ssh/hosts/andrew", headers=me)).json() == {"deleted": "andrew"}
                assert (await c.delete("/api/v1/ssh/hosts/bastion", headers=me)).status_code == 200
                assert (await c.delete("/api/v1/ssh/hosts/bastion", headers=me)).status_code == 404
                assert "continuum-andrew" not in (tmp_path / "home" / "ssh" / "config").read_text()
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_a_worker_runs_commands_on_a_server_and_waits_for_a_person_to_approve_changes(tmp_path):
    from test_agent import ScriptedModel, StreamingModel, call

    from continuum.runtime.ssh import Host

    log = tmp_path / "ssh.log"
    fake = tmp_path / "fake-ssh"
    fake.write_text(f"#!{sys.executable}\nimport sys\nopen({str(log)!r}, 'a').write(sys.argv[-1] + '\\n')\n"
                    "print('ran: ' + sys.argv[-1])\n")
    fake.chmod(0o755)
    tools_seen = []

    class Worker(StreamingModel):
        async def stream(self, messages, tools=None, on_progress=None, **extra):
            tools_seen.append([t["function"]["name"] for t in tools or []])
            return await super().stream(messages, tools, on_progress, **extra)

    router = ScriptedModel(call("decide", decision="accept", title="看看 web", scope="检查并重启 web",
                                acknowledgement="好。", name="web", reason="请求"))
    worker = Worker(
        call("ssh_hosts"),
        call("ssh", host="web", command="systemctl status nginx", reason="看状态"),
        call("ssh", host="web", command="systemctl restart nginx", reason="它挂了，重启一下"),
        call("ssh", host="web", command="rm -rf /var/log/nginx", reason="清日志"),
        lambda messages: call("report", state="ready", summary="重启好了；删日志被你拒绝了。"))

    async def main():
        from test_agent import agent_runtime

        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}]}}},
                           api={"enabled": True, "port": 0})
        rt.settings.ssh.admins = ["me"]
        rt.settings.ssh.ssh_command = str(fake)
        rt.ssh.put(Host(name="web", address="34.1.2.3", user="Yuxia", approval="read"))
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)

        def waiting():
            return [s for s in ctx.kind.steps(ctx, "me", "main") if s["status"] == "approval"]

        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "看看 web，挂了就重启"})
                await _until(lambda: bool(waiting()))
                [first] = waiting()
                assert first["input"] == "web  $ systemctl restart nginx" and first["output"] == "它挂了，重启一下"
                # Only the ticket's own people decide, and only once.
                url = f"/api/v1/approvals/{first['ticket_id']}/{first['key']}"
                assert (await c.post(url, headers=me, json={"approve": True})).json() == {"decided": "approved"}
                assert (await c.post(url, headers=me, json={"approve": True})).status_code == 404
                await _until(lambda: len(waiting()) == 1 and waiting()[0]["key"] != first["key"])
                [second] = waiting()
                url = f"/api/v1/approvals/{second['ticket_id']}/{second['key']}"
                assert (await c.post(url, headers=me, json={"approve": False})).json() == {"decided": "refused"}
                await _until(lambda: any(m["role"] == "reply" for m in ctx.kind.history(ctx, "me", "main")))
                return ctx.kind.steps(ctx, "me", "main"), worker.seen
        finally:
            runner.cancel()
            await rt.close()

    steps, seen = asyncio.run(main())
    # The read-only command ran unasked, the approved one ran, the refused one never did.
    assert log.read_text().splitlines() == ["systemctl status nginx", "systemctl restart nginx"]
    assert [(s["input"].split("$ ")[1], s["status"]) for s in steps if s["name"] == "ssh"] == [
        ("systemctl status nginx", "done"), ("systemctl restart nginx", "done"), ("rm -rf /var/log/nginx", "error")]
    assert "web：Yuxia@34.1.2.3:22" in seen[1][-1]["content"] and "只读命令直接执行" in seen[1][-1]["content"]
    assert "人拒绝了这条命令" in seen[4][-1]["content"]
    assert {"ssh", "ssh_hosts"} <= set(tools_seen[0])


def test_the_ssh_tools_are_offered_only_for_someone_who_may_manage_the_servers(tmp_path):
    from test_agent import ScriptedModel, StreamingModel, agent_runtime, call, post

    from continuum.runtime.ssh import Host

    tools_seen = []

    class Worker(StreamingModel):
        async def stream(self, messages, tools=None, on_progress=None, **extra):
            tools_seen.append({t["function"]["name"] for t in tools or []})
            return await super().stream(messages, tools, on_progress, **extra)

    router = ScriptedModel(call("decide", decision="accept", title="x", scope="x", reason="r"))
    worker = Worker(call("report", state="ready", summary="好了"))

    async def main():
        from test_runtime import drive, replies

        rt = agent_runtime(tmp_path, router, worker)
        rt.settings.ssh.admins = ["someone-else"]
        rt.ssh.put(Host(name="web", address="34.1.2.3", user="Yuxia"))
        try:
            post(tmp_path, "看看服务器")
            await drive(rt, lambda: len(replies(tmp_path)) == 2, timeout=10)
        finally:
            await rt.close()

    asyncio.run(main())
    assert tools_seen and not ({"ssh", "ssh_hosts"} & tools_seen[0])


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.02)



class Summarizer:
    def __init__(self):
        self.asked = []

    async def complete(self, messages, tools=None, tool_choice=None, **extra):
        self.asked.append(messages[-1]["content"])
        return {"role": "assistant", "content": "ERP 的生产机。\n- 服务：nginx（443）、erp-api\n- 项目：/home/u/erp，分支 feature/mrp，在做 MRP"}

    async def close(self):
        pass


def test_a_server_reached_is_looked_over_and_its_profile_kept(tmp_path):
    async def main():
        rt = ssh_runtime(tmp_path)
        summarizer = Summarizer()
        rt.models["default"] = summarizer
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                await c.put("/api/v1/ssh/hosts/web", headers=me, json={"address": "34.1.2.3", "user": "u"})
                await c.put("/api/v1/ssh/hosts/down", headers=me, json={"address": "10.128.0.20", "user": "u"})
                # Reached: looked over in the background, once.
                assert (await c.post("/api/v1/ssh/hosts/web/test", headers=me)).json()["state"]["ok"]
                await _until(lambda: (rt.ssh.get("web").get("profile") or {}).get("text"))
                profile = rt.ssh.get("web")["profile"]
                assert profile["ok"] and profile["text"].startswith("ERP 的生产机。")
                assert "feature/mrp" in profile["raw"] and "erp-api.service" in summarizer.asked[0]
                # Not reached: nothing to look at.
                assert not (await c.post("/api/v1/ssh/hosts/down/test", headers=me)).json()["state"]["ok"]
                await asyncio.sleep(0.2)
                assert "profile" not in rt.ssh.get("down")
                # On request, and kept through an edit that does not move the host.
                again = (await c.post("/api/v1/ssh/hosts/web/profile", headers=me)).json()
                assert again["profile"]["ok"] and len(summarizer.asked) == 2
                await c.put("/api/v1/ssh/hosts/web", headers=me, json={"address": "34.1.2.3", "user": "u", "note": "x"})
                assert rt.ssh.get("web")["profile"]["text"].startswith("ERP")
                moved = (await c.put("/api/v1/ssh/hosts/web", headers=me,
                                     json={"address": "34.9.9.9", "user": "u"})).json()
                assert "profile" not in moved
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())
