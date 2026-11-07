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
