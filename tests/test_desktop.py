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

        # A message, then its acknowledgement and reply arrive on the stream.
        page.fill("#input", "顺便核一下交期")
        page.press("#input", "Enter")
        page.wait_for_selector(".msg.reply")
        roles = [page.locator(".msg").nth(i).get_attribute("class").split()[1] for i in range(5)]
        assert roles == ["user", "acknowledgement", "user", "acknowledgement", "reply"]
        assert page.input_value("#input") == ""
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
        assert page.locator(".group").all_inner_texts() == ["等你处理 01", "执行中 01", "已结束 01"]
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
        assert page.is_visible("#view-chat") and page.locator(".msg").count() == 5
        assert errors == []
        browser.close()
