"""The person's other tools: kept privately, never shown back, called with the right credentials, used by workers."""

import asyncio
import base64
import json
import stat
import subprocess
import sys

import httpx
import pytest

from continuum.runtime.integrations import IntegrationError, Integrations, _body_text

from test_agent import ScriptedModel, StreamingModel, agent_runtime, call
from test_api import serve


def fake_gh(tmp_path):
    script = tmp_path / "fake-gh"
    script.write_text(f"#!{sys.executable}\nimport os, sys, json\n"
                      "print(json.dumps({'argv': sys.argv[1:], 'token': os.environ.get('GH_TOKEN')}))\n"
                      "sys.exit(0)\n")
    script.chmod(0o755)
    return str(script)


def test_credentials_are_kept_in_private_files_and_never_shown_back(tmp_path):
    tools = Integrations(tmp_path)
    tools.save("github", {"token": "ghp_secret"})
    path = tmp_path / "integrations" / "github.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "ghp_secret" not in json.dumps(tools.view())
    # A secret left empty keeps the one already kept.
    tools.save("github", {"token": ""}, tools.accounts("github")[0]["id"])
    assert tools.get("github")["secrets"]["token"] == "ghp_secret"
    with pytest.raises(IntegrationError):
        tools.save("slack", {})
    tools.save("email", {"preset": "gmail", "address": "me@example.com", "password": "app-pass"})
    assert tools.get("email")["settings"]["imap_host"] == "imap.gmail.com"
    assert tools.connected() == {"github", "email"}
    assert tools.remove("github") and tools.connected() == {"email"}


def test_github_runs_gh_with_the_persons_token(tmp_path):
    tools = Integrations(tmp_path)
    tools.gh = fake_gh(tmp_path)
    tools.save("github", {"token": "ghp_secret"})
    out = json.loads(asyncio.run(tools.github("gh pr list --repo 'o/r name'")).split("\n", 1)[1])
    assert out == {"argv": ["pr", "list", "--repo", "o/r name"], "token": "ghp_secret"}
    assert asyncio.run(tools.test("github"))["ok"]


def test_http_tools_send_their_own_credentials_and_only_to_their_own_api(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization")))
        if "slack.com" in str(request.url):
            return httpx.Response(200, json={"ok": True, "user": "bot", "team": "o2"})
        if request.url.path.endswith("/tokens/verify"):
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/zones"):
            return httpx.Response(200, json={"result": [{"name": "yuxianglin.xyz"}]})
        return httpx.Response(200, json=[{"domain": "o2tech.ai"}])

    tools = Integrations(tmp_path, transport=httpx.MockTransport(handler))
    tools.save("slack", {"token": "xoxb-1"})
    tools.save("cloudflare", {"token": "cf-1"})
    tools.save("godaddy", {"key": "k", "secret": "s"})
    assert asyncio.run(tools.test("slack"))["message"] == "已连接：bot @ o2"
    assert "yuxianglin.xyz" in asyncio.run(tools.test("cloudflare"))["message"]
    assert "o2tech.ai" in asyncio.run(tools.test("godaddy"))["message"]
    asyncio.run(tools.api("godaddy", "GET", "/v1/domains/o2tech.ai/records"))
    assert seen[-1] == ("GET", "https://api.godaddy.com/v1/domains/o2tech.ai/records", "sso-key k:s")
    assert ("POST", "https://slack.com/api/auth.test", "Bearer xoxb-1") in seen
    assert any(u.startswith("https://api.cloudflare.com/client/v4/zones") and a == "Bearer cf-1" for _, u, a in seen)
    for bad in ["https://evil.example/x", "zones", "/../x"]:
        with pytest.raises(IntegrationError):
            asyncio.run(tools.api("cloudflare", "GET", bad))
    with pytest.raises(IntegrationError):
        asyncio.run(tools.slack("https://evil"))


def test_google_signs_for_a_token_with_the_service_accounts_key(tmp_path):
    key = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
                         capture_output=True, text=True, check=True).stdout
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            asked.append(form["assertion"])
            return httpx.Response(200, json={"access_token": "ya29.t", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer ya29.t"
        return httpx.Response(200, json={"files": [{"name": "MRP 计划"}]})

    tools = Integrations(tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(IntegrationError):
        tools.save("google", {"key_json": "{}"})
    tools.save("google", {"key_json": json.dumps({"type": "service_account", "client_email": "bot@p.iam.gserviceaccount.com",
                                                  "private_key": key, "project_id": "p"})})
    assert asyncio.run(tools.test("google"))["message"] == "已连接：bot@p.iam.gserviceaccount.com"
    header, claims, signature = asked[0].split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(header))["alg"] == "RS256" and len(pad(signature)) == 256
    assert json.loads(pad(claims))["iss"] == "bot@p.iam.gserviceaccount.com"
    assert "drive" in json.loads(pad(claims))["scope"]
    assert "MRP 计划" in asyncio.run(tools.google_api("GET", "https://www.googleapis.com/drive/v3/files"))
    assert len(asked) == 1  # the token is kept while it lasts
    with pytest.raises(IntegrationError):
        asyncio.run(tools.google_api("GET", "https://evil.example/drive"))


def test_an_email_body_reads_as_text():
    import email

    message = email.message_from_bytes(
        "Content-Type: multipart/alternative; boundary=b\n\n--b\nContent-Type: text/html; charset=utf-8\n\n"
        "<p>你好 <b>MRP</b></p>\n--b--\n".encode())
    assert _body_text(message) == "你好 MRP"


def test_a_secret_a_tool_prints_is_masked_before_anyone_sees_it(tmp_path):
    tools = Integrations(tmp_path)
    tools.gh = fake_gh(tmp_path)  # prints the token it was given
    tools.save("github", {"token": "ghp_secret"})
    assert "ghp_secret" not in tools.scrub(asyncio.run(tools.github("auth status")))
    assert "ghp_secret" not in json.dumps(asyncio.run(tools.test("github")))


def test_the_settings_api_connects_tools_for_admins_only_and_never_returns_secrets(tmp_path):
    from test_ssh import ssh_runtime

    async def main():
        rt = ssh_runtime(tmp_path)
        rt.integrations.gh = fake_gh(tmp_path)
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        bob = {"Authorization": f"Bearer {rt.accounts.add('bob')}"}
        port, runner = await serve(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                assert (await c.get("/api/v1/integrations", headers=bob)).json() == {"admin": False, "tools": []}
                assert (await c.put("/api/v1/integrations/github", headers=bob, json={"token": "x"})).status_code == 403
                saved = await c.put("/api/v1/integrations/github", headers=me, json={"token": "ghp_secret"})
                assert saved.status_code == 200 and saved.json()["connected"] and saved.json()["test"]["ok"]
                page = await c.get("/api/v1/integrations", headers=me)
                assert "ghp_secret" not in page.text and len(page.json()["tools"]) == 7
                assert (await c.put("/api/v1/integrations/nothing", headers=me, json={})).status_code == 422
                # A second account; an older app's call (no account named) still updates the first.
                second = (await c.put("/api/v1/integrations/github", headers=me,
                                      json={"token": "ghp_home", "label": "个人", "new": True})).json()
                assert [x["label"] for x in second["accounts"]] == ["默认", "个人"] and second["saved"] != "default"
                await c.put("/api/v1/integrations/github", headers=me, json={"token": "ghp_new"})
                assert rt.integrations.get("github", "default")["secrets"]["token"] == "ghp_new"
                tested = await c.post("/api/v1/integrations/github/test", headers=me, params={"account": "个人"})
                assert tested.status_code == 200
                gone = await c.delete("/api/v1/integrations/github", headers=me, params={"account": second["saved"]})
                assert gone.json() == {"removed": True} and len(rt.integrations.accounts("github")) == 1
                assert (await c.delete("/api/v1/integrations/github", headers=me)).json() == {"removed": True}
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def test_a_worker_uses_a_connected_tool_through_the_host(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="看 PR", scope="看", reason="r"))
    worker = StreamingModel(call("github", args="pr list --repo o/r"),
                            lambda m: call("report", state="ready", summary=m[-1]["content"][:40]))
    async def main():
        from test_runtime import drive, replies

        rt = agent_runtime(tmp_path, router, worker)
        rt.settings.ssh.admins = ["me"]
        rt.integrations.gh = fake_gh(tmp_path)
        rt.integrations.save("github", {"token": "ghp_secret"})
        try:
            from test_agent import post

            post(tmp_path, "看看 o/r 的 PR")
            await drive(rt, lambda: len(replies(tmp_path)) == 2, timeout=10)
            return replies(tmp_path), [m for m in worker.seen[0] if m["role"] == "system"][0]["content"]
        finally:
            await rt.close()

    answers, system = asyncio.run(main())
    assert answers[-1][1].startswith("[exit code 0]") and "ghp_secret" not in answers[-1][1]
    assert "已连接：GitHub" in system


def test_aws_uses_the_persons_key_and_never_the_hosts_own_role(tmp_path):
    script = tmp_path / "fake-aws"
    script.write_text(f"#!{sys.executable}\nimport os, sys, json\n"
                      "e = os.environ\n"
                      "if sys.argv[1:3] == ['sts', 'get-caller-identity']:\n"
                      "    print(json.dumps({'Arn': 'arn:aws:iam::1:user/c'})); sys.exit(0)\n"
                      "print(json.dumps({'argv': sys.argv[1:], 'key': e.get('AWS_ACCESS_KEY_ID'), "
                      "'imds': e.get('AWS_EC2_METADATA_DISABLED'), 'region': e.get('AWS_DEFAULT_REGION'), "
                      "'profile': e.get('AWS_SHARED_CREDENTIALS_FILE')}))\n")
    script.chmod(0o755)
    tools = Integrations(tmp_path)
    tools.aws_cli = str(script)
    with pytest.raises(IntegrationError):
        tools.save("aws", {"access_key_id": "AKIA1"})
    tools.save("aws", {"access_key_id": "AKIAEXAMPLEKEY", "secret_access_key": "secretsecret", "region": "eu-west-2"})
    assert asyncio.run(tools.test("aws"))["message"] == "已连接：arn:aws:iam::1:user/c"
    out = json.loads(asyncio.run(tools.aws("aws ec2 describe-instances")).split("\n", 1)[1])
    assert out["argv"] == ["ec2", "describe-instances"] and out["key"] == "AKIAEXAMPLEKEY"
    assert out["imds"] == "true" and out["region"] == "eu-west-2" and "none" in out["profile"]


def test_a_worker_asks_for_a_tool_the_person_connects_it_on_the_card_and_the_worker_goes_on(tmp_path):
    router = ScriptedModel(call("decide", decision="accept", title="看 PR", scope="看", acknowledgement="好。",
                                reason="r"))
    offered = []

    class Worker(StreamingModel):
        async def stream(self, messages, tools=None, on_progress=None, **extra):
            offered.append({t["function"]["name"] for t in tools or []})
            return await super().stream(messages, tools, on_progress, **extra)

    worker = Worker(call("request_tool", kind="github", reason="读 o/r 的 PR"),
                    lambda m: call("github", args="pr list --repo o/r"),
                    lambda m: call("report", state="ready", summary="看完了"))

    async def main():
        rt = agent_runtime(tmp_path / "home", router, worker, sources={"app": {"kind": "app"}},
                           policy={"sources": {"app": {"allowed": [{"id": "me"}]}}}, api={"enabled": True, "port": 0})
        rt.settings.ssh.admins = ["me"]
        rt.integrations.gh = fake_gh(tmp_path)
        me = {"Authorization": f"Bearer {rt.accounts.add('me')}"}
        ctx = rt.sources["app"]
        port, runner = await serve(rt)
        said = lambda role: [m for m in ctx.kind.everything(ctx, "me") if m["role"] == role]  # noqa: E731
        try:
            from test_ssh import _until

            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                await c.post("/api/v1/threads/main/messages", headers=me, json={"text": "看看 o/r 的 PR"})
                await _until(lambda: bool(said("credential")), timeout=10)
                [card] = said("credential")
                assert card["meta"] == {"kind": "github", "reason": "读 o/r 的 PR", "state": "waiting"}
                assert "github" not in offered[0] and "request_tool" in offered[0]
                # The person fills the card: straight to the server, not into the conversation.
                saved = await c.put("/api/v1/integrations/github", headers=me, json={"token": "ghp_secret"})
                assert saved.json()["test"]["ok"]
                await _until(lambda: bool(said("reply")), timeout=10)
                assert said("credential")[0]["meta"]["state"] == "connected"
                assert all("ghp_secret" not in m["text"] for m in ctx.kind.everything(ctx, "me"))
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())
    assert "github" in offered[1]  # offered as soon as it was connected, in the same turn



def test_a_tool_holds_several_accounts_and_each_call_uses_the_one_named(tmp_path):
    tools = Integrations(tmp_path)
    tools.gh = fake_gh(tmp_path)
    work = tools.save("github", {"token": "ghp_work", "label": "公司"})
    home = tools.save("github", {"token": "ghp_home", "label": "个人"})
    assert work != home and [a["label"] for a in tools.view()[0]["accounts"]] == ["公司", "个人"]
    with pytest.raises(IntegrationError):
        tools.save("github", {"token": "x", "label": "公司"})  # names are unique
    token = lambda **kw: json.loads(asyncio.run(tools.github("api user", **kw)).split("\n", 1)[1])["token"]  # noqa: E731
    assert token() == "ghp_work"  # the first, unless one is named
    assert token(account="个人") == "ghp_home" and token(account=home) == "ghp_home"
    with pytest.raises(IntegrationError) as missing:
        asyncio.run(tools.github("api user", account="没有"))
    assert "公司" in str(missing.value) and "个人" in str(missing.value)
    assert asyncio.run(tools.test("github", home))["ok"] and tools.get("github", home)["test"]["ok"]
    assert tools.get("github", work)["test"] is None
    # Secrets of every account are masked.
    assert tools.scrub("ghp_work ghp_home") == "*** ***"
    assert tools.remove("github", work) and [a["label"] for a in tools.accounts("github")] == ["个人"]


def test_a_tool_kept_before_accounts_becomes_the_default_account(tmp_path):
    (tmp_path / "integrations").mkdir()
    (tmp_path / "integrations" / "slack.json").write_text(json.dumps({"secrets": {"token": "xoxb-old"},
                                                                      "settings": {}, "account": "bot"}))
    tools = Integrations(tmp_path)
    [only] = tools.accounts("slack")
    assert only["id"] == "default" and only["label"] == "默认" and tools.connected() == {"slack"}


def test_a_mailbox_type_follows_its_address_and_a_refused_login_says_why(tmp_path):
    import imaplib

    from continuum.runtime.integrations import login_refused

    tools = Integrations(tmp_path)
    # Nothing chosen: a well-known address picks its provider.
    tools.save("email", {"address": "me@qq.com", "password": "code"})
    assert tools.get("email")["settings"]["imap_host"] == "imap.qq.com"
    # The wrong one chosen is turned away with what to pick instead.
    with pytest.raises(IntegrationError, match="Outlook"):
        tools.save("email", {"preset": "gmail", "address": "me@outlook.com", "password": "x"})
    refused = imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    words = login_refused({"address": "me@hotmail.com", "preset": "outlook"}, refused)
    assert "b'" not in words and "OAuth" in words
    assert "apppasswords" in login_refused({"address": "me@example.com", "preset": "gmail"}, refused)
