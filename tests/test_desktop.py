"""The desktop app, as far as it can be tested without Windows: its API client in Node against a real server, and
its window in a headless browser with a stand-in for the Electron bridge. The Electron shell itself is tested by
the Windows CI workflow."""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from test_api import app_runtime, serve

DESKTOP = Path(__file__).resolve().parent.parent / "desktop"
SHOTS = os.environ.get("CONTINUUM_SHOTS")


@pytest.mark.skipif(shutil.which("node") is None or not (DESKTOP / "node_modules" / "ws").exists(),
                    reason="needs node and `npm ci` in desktop/")
def test_the_desktop_api_client_against_a_real_server(tmp_path):
    async def main():
        rt = app_runtime(tmp_path)
        token = rt.accounts.add("me", "test")
        rt.accounts.set_password("me", "correct horse")
        port, runner = await serve(rt)
        try:
            env = {**os.environ, "CONTINUUM_URL": f"http://127.0.0.1:{port}", "CONTINUUM_TOKEN": token,
                   "CONTINUUM_PASSWORD": "correct horse"}
            node = await asyncio.create_subprocess_exec(
                "node", "--test", str(DESKTOP / "test" / "client.test.js"), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await asyncio.wait_for(node.communicate(), timeout=60)
            assert node.returncode == 0, output.decode()
        finally:
            runner.cancel()
            await rt.close()

    asyncio.run(main())


def _browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_avatars_drawn_from_names():
    import subprocess

    result = subprocess.run(["node", "--test", str(DESKTOP / "test" / "avatar.test.js")], capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(_browser() is None, reason="needs playwright")
def test_the_window_connects_converses_and_lists_what_waits_for_a_person():
    sync_playwright = _browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_context(viewport={"width": 520, "height": 780}, bypass_csp=True).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(path=str(DESKTOP / "test" / "fake-bridge.js"))
        page.goto((DESKTOP / "renderer" / "index.html").as_uri())
        page.wait_for_selector("#app[data-ready]")

        # First run: log in. The server address is filled in.
        assert page.is_visible("#setup") and page.input_value("#server") == "https://continuum.example.com"
        assert page.inner_text("#setup-state") == "未连接"
        page.click("#reveal")
        assert page.get_attribute("#password", "type") == "text"
        page.click("#reveal")
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-setup.png")
        page.fill("#username", "me")
        page.fill("#password", "wrong-password")
        page.click("#connect")
        page.wait_for_selector("#setup-error:not([hidden])")
        assert "用户名或密码不对" in page.inner_text("#setup-error")
        page.fill("#password", "good-password")
        page.click("#connect")
        page.wait_for_selector("#main:not([hidden])")
        assert page.inner_text("#user") == "me"
        assert page.locator(".msg").count() == 2
        assert page.locator("#threads button").all_inner_texts() == ["主对话", "bom"]
        assert page.inner_text("#thread-title") == "主对话" and page.inner_text("#thread-no") == "#001"
        assert page.inner_text("#badge") == "1"  # one ticket waits for an answer

        # Avatars are drawn from the names: the person's and the assistant's.
        assert page.locator(".msg.user .avatar svg").count() == 1 and page.locator(".msg.acknowledgement .avatar svg").count() == 1
        assert page.locator("#me-avatar svg").count() == 1
        # The steps a worker took are kept in the conversation, folded into one line.
        assert "已执行 2 个步骤" in page.inner_text(".work")
        page.click(".work summary")
        assert "python3 compare.py quotes/" in page.inner_text(".work .step.shell")

        # A message: its acknowledgement, the worker thinking and running a command as it happens, then the answer
        # written as it comes, which gives way to the reply itself.
        page.evaluate("window.__fake.hold = true")
        page.fill("#input", "顺便核一下交期")
        page.press("#input", "Enter")
        page.wait_for_selector(".work.running .step.shell.running >> text=ls /work")
        assert "正在运行命令" in page.inner_text(".work.running summary")
        page.evaluate("window.__fake.release()")
        page.wait_for_selector(".work .step.shell.done >> text=ls /work")
        page.wait_for_selector(".msg.streaming")
        page.wait_for_function("document.querySelector('.msg.streaming').textContent.includes('/work/report.md')")
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-streaming.png")
        page.evaluate("window.__fake.hold = false; window.__fake.release()")
        page.wait_for_selector(".msg.reply:not(.streaming)")
        assert page.locator(".msg.streaming").count() == 0 and page.locator(".work.running").count() == 0
        roles = [c.split()[1] for c in page.locator(".msg").evaluate_all("ns => ns.map(n => n.className)")]
        assert roles == ["user", "acknowledgement", "user", "acknowledgement", "reply"]
        assert page.locator(".msg.reply li").count() == 2 and page.input_value("#input") == ""
        # The assistant's words are not boxed; the person's are.
        styles = page.evaluate("""() => ['.msg.reply .bubble', '.msg.user .bubble'].map(s => {
            const c = getComputedStyle(document.querySelector(s)); return [c.borderTopWidth, c.backgroundColor]; })""")
        assert styles[0] == ["0px", "rgba(0, 0, 0, 0)"] and styles[1][0] != "0px"
        if SHOTS:
            page.wait_for_timeout(400)
            page.screenshot(path=f"{SHOTS}/desktop-chat.png")

        # Another channel: replies render a little Markdown, and nothing in them becomes markup of its own.
        page.click("#threads button[data-thread=bom]")
        page.wait_for_selector(".msg.reply.rich table")
        assert page.inner_text("#thread-no") == "#002"
        assert page.locator(".msg.reply td").count() == 6 and page.locator(".msg.reply li").count() == 2
        assert page.locator(".msg.reply b").count() == 0 and "<b>未</b>" in page.inner_text(".msg.reply ul")
        if SHOTS:
            page.wait_for_timeout(400)
            page.screenshot(path=f"{SHOTS}/desktop-rich.png")

        # Tickets: what waits for a person comes first, and a stop asks once more before it acts.
        page.click("#tab-tickets")
        assert page.locator("#tickets .group").all_inner_texts() == ["等你处理 01", "执行中 01", "已结束 01"]
        assert "0402 还是 0603" in page.inner_text(".card.person .question")
        if SHOTS:
            page.wait_for_timeout(400)
            page.screenshot(path=f"{SHOTS}/desktop-tickets.png")
        working = page.locator(".card.working")
        working.get_by_role("button", name="停止").click()
        working.get_by_role("button", name="确认停止？").click()
        page.wait_for_function("document.querySelector('#tickets').textContent.includes('已结束 02')")
        assert "已停止" in page.inner_text("#tickets")

        # "去回答" goes to the conversation the question belongs to.
        page.locator(".card.person").get_by_role("button", name="去回答").click()
        page.wait_for_function("document.querySelector('#thread-title').textContent === '主对话'")
        assert page.is_visible("#view-chat") and page.locator(".msg").count() == 5 and page.locator(".work").count() == 2
        assert errors == []
        browser.close()


@pytest.mark.skipif(_browser() is None, reason="needs playwright")
def test_the_settings_page_adds_tests_and_removes_ssh_servers():
    sync_playwright = _browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        context = browser.new_context(viewport={"width": 520, "height": 780}, bypass_csp=True)
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(path=str(DESKTOP / "test" / "fake-bridge.js"))
        page.goto((DESKTOP / "renderer" / "index.html").as_uri())
        page.wait_for_selector("#app[data-ready]")
        page.fill("#username", "me")
        page.fill("#password", "good-password")
        page.click("#connect")
        page.wait_for_selector("#main:not([hidden])")

        page.click("#settings")
        page.wait_for_selector("#ssh-admin:not([hidden])")
        assert page.inner_text("#ssh-key").startswith("ssh-ed25519 ")
        assert page.inner_text("#ssh-line").startswith('from="13.134.76.110",no-agent-forwarding')
        page.click("#ssh-copy-line")
        assert page.evaluate("navigator.clipboard.readText()").startswith('from="13.134.76.110"')

        # A GCP address straight from AWS: the test says why it cannot connect.
        page.click(".card.host[data-host=andrew] >> text=测试连接")
        page.wait_for_selector(".card.host.bad[data-host=andrew]")
        assert "连接超时" in page.inner_text(".card.host[data-host=andrew]")

        # Add a jump host, then send andrew through it.
        page.click("#ssh-add")
        page.fill("#ssh-name", "Bad Name")
        page.fill("#ssh-address", "34.1.2.3")
        page.fill("#ssh-user", "Yuxia")
        page.click("#ssh-save")
        assert "名字只能用" in page.inner_text("#ssh-error")
        page.fill("#ssh-name", "bastion")
        page.select_option("#ssh-approval", "read")
        page.click("#ssh-save")
        page.wait_for_selector("#ssh-form", state="hidden")
        assert "只读命令直接执行" in page.inner_text(".card.host[data-host=bastion]")
        page.click(".card.host[data-host=andrew] >> text=编辑")
        assert page.get_attribute("#ssh-name", "readonly") is not None
        page.select_option("#ssh-jump", "bastion")
        page.click("#ssh-save")
        page.wait_for_selector("#ssh-form", state="hidden")
        page.click(".card.host[data-host=andrew] >> text=测试连接")
        page.wait_for_selector(".card.host.ok[data-host=andrew]")
        assert "经过跳板" in page.inner_text(".card.host[data-host=andrew]")
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-settings.png", full_page=True)

        # A jump host in use is not removed; once nothing goes through it, it is (after a second click).
        page.click(".card.host[data-host=bastion] >> text=删除")
        page.click(".card.host[data-host=bastion] >> text=确认删除？")
        page.wait_for_selector("#status:not([hidden])")
        assert "还经过它跳转" in page.inner_text("#status")
        page.click(".card.host[data-host=andrew] >> text=删除")
        page.click(".card.host[data-host=andrew] >> text=确认删除？")
        page.wait_for_selector(".card.host[data-host=andrew]", state="detached")

        page.click("#settings")
        assert page.is_visible("#view-chat") and not page.is_visible("#view-settings")
        assert errors == []
        browser.close()
