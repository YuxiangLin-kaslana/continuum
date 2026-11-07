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
        page.wait_for_selector("#badge:not([hidden])")
        assert page.inner_text("#user") == "me" and page.evaluate("window.__fake.timezone")
        # The conversation tab is only the conversation: everything said, nothing about projects around it.
        assert page.locator(".msg").count() == 9
        assert page.locator(".channels, .project-chip, #threads").count() == 0
        assert page.inner_text("#badge") == "1"  # one project waits for an answer

        # Avatars are drawn from the names: the person's and the assistant's.
        assert page.locator(".msg.user .avatar svg").count() == 3 and page.locator(".msg.acknowledgement .avatar svg").count() == 1
        assert page.locator("#me-avatar svg").count() == 1
        # The steps a worker took are kept in the conversation, folded into one line.
        assert "已执行 2 个步骤" in page.locator(".work").first.inner_text()
        page.locator(".work summary").first.click()
        assert "python3 compare.py quotes/" in page.locator(".work .step.shell").first.inner_text()

        # A reminder from a project arrives in the one conversation.
        assert "记得看巡检结果" in page.inner_text(".msg.reminder")

        # Projects live on their own tab; opening one shows its own part of the conversation, and talking there
        # addresses it (the whole conversation shows that too).
        page.click("#tab-projects")
        page.locator(".card.project[data-project=ops1]").get_by_role("button", name="打开").click()
        page.wait_for_selector("#project-detail:not([hidden])")
        assert "服务器巡检" in page.inner_text("#project-card") and "检查磁盘" in page.inner_text("#project-card")
        assert [c.split()[1] for c in page.eval_on_selector_all("#project-messages .msg", "ns => ns.map(n => n.className)")] == [
            "reminder", "scheduled", "reply"]
        assert page.locator("#messages .msg").count() == 0  # one place shows the conversation at a time
        assert "仍在主对话里" in page.get_attribute("#project-input", "placeholder")
        page.fill("#project-input", "阈值改成 90%")
        page.press("#project-input", "Enter")
        page.wait_for_selector("#project-messages .msg.user >> text=阈值改成 90%")
        page.wait_for_selector("#project-messages .msg.reply >> text=已处理「阈值改成 90%」")
        assert page.evaluate("window.__fake.messages.find(m => m.text === '阈值改成 90%').thread") == "p-ops1"
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-project.png")
        page.click("#project-back")
        assert page.is_visible("#projects-overview")
        page.click("#tab-chat")
        assert page.locator("#messages .msg.user >> text=阈值改成 90%").count() == 1

        # A message: its acknowledgement, the worker thinking and running a command as it happens, then the answer
        # written as it comes, which gives way to the reply itself. The request becomes a project of its own.
        page.evaluate("window.__fake.hold = true")
        page.fill("#input", "顺便核一下交期")
        page.press("#input", "Enter")
        page.wait_for_selector(".work.running .step.shell.running >> text=ls /work")
        assert "正在运行命令" in page.inner_text(".work.running summary")
        page.evaluate("window.__fake.release()")
        page.wait_for_selector(".work .step.shell.done >> text=ls /work")
        try:
            page.wait_for_selector(".msg.streaming")
        except Exception:
            # Seen only on CI, never here: say what the page and the stand-in had, for the next look.
            print("streaming not shown:", page.evaluate("""() => JSON.stringify({
                view: document.querySelector('#view-chat').hidden ? 'not chat' : 'chat',
                streaming: document.querySelectorAll('.msg.streaming').length,
                running: document.querySelectorAll('.work.running').length,
                last: window.__fake.messages.slice(-3).map((m) => `${m.role}:${m.ticket_id}:${m.text.slice(0, 16)}`),
                steps: window.__fake.steps.slice(-2).map((s) => `${s.ticket_id}:${s.key}:${s.status}`)})"""))
            raise
        page.wait_for_function("document.querySelector('.msg.streaming').textContent.includes('/work/report.md')")
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-streaming.png")
        page.evaluate("window.__fake.hold = false; window.__fake.release()")
        page.wait_for_selector(".msg.reply >> text=已处理「顺便核一下交期」")
        page.wait_for_function("!document.querySelector('.msg.streaming') && !document.querySelector('.work.running')")
        roles = [c.split()[1] for c in page.eval_on_selector_all(".msg", "ns => ns.map(n => n.className)")]
        assert roles[-3:] == ["user", "acknowledgement", "reply"] and page.input_value("#input") == ""
        # The assistant's words are not boxed; the person's are.
        styles = page.evaluate("""() => ['.msg.reply .bubble', '.msg.user .bubble'].map(s => {
            const c = getComputedStyle(document.querySelector(s)); return [c.borderTopWidth, c.backgroundColor]; })""")
        assert styles[0] == ["0px", "rgba(0, 0, 0, 0)"] and styles[1][0] != "0px"
        if SHOTS:
            page.wait_for_timeout(400)
            page.screenshot(path=f"{SHOTS}/desktop-chat.png")

        # An archived project: replies render a little Markdown, and nothing in them becomes markup of its own.
        page.click("#tab-projects")
        page.locator(".card.project[data-project=t-4]").get_by_role("button", name="打开").click()
        page.wait_for_selector("#project-messages .msg.reply.rich table")
        assert "已完成" in page.inner_text("#project-card")
        reply = page.locator("#project-messages .msg.reply")
        assert reply.locator("td").count() == 6 and reply.locator("li").count() == 2
        assert reply.locator("b").count() == 0 and "<b>未</b>" in reply.locator("ul").inner_text()
        if SHOTS:
            page.wait_for_timeout(400)
            page.screenshot(path=f"{SHOTS}/desktop-rich.png")
        page.click("#project-back")

        # Something to do every day: the assistant proposes a long-running project, and the person confirms it.
        page.click("#tab-chat")
        page.fill("#input", "以后每天提醒我看交期")
        page.press("#input", "Enter")
        card = page.locator(".proposal-card")
        card.wait_for()
        page.wait_for_selector(".proposal-card >> text=只提醒，不改任何东西")
        assert "每日交期提醒" in card.inner_text() and "只提醒，不改任何东西" in card.inner_text()
        assert "每天 09:00（Asia/Shanghai）" in card.inner_text()
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-proposal.png")
        card.get_by_role("button", name="确认开始").click()
        page.wait_for_selector(".proposal-card >> text=已确认 · 进行中")
        page.wait_for_selector(".msg.notice >> text=项目「每日交期提醒」开始了")

        # The projects page: what waits for the person first; a stop and an end ask once more before they act.
        page.click("#tab-projects")
        page.wait_for_selector(".card.project[data-project=new1]")
        groups = page.locator("#projects .group").all_inner_texts()
        assert groups[0] == "等你处理 01" and groups[1] == "长期项目 02" and groups[-1].startswith("已归档")
        assert "0402 还是 0603" in page.inner_text(".card.project.person .question")
        assert "每天 08:30（Asia/Shanghai） · 开工：检查磁盘" in page.inner_text(".card.project[data-project=ops1]")
        if SHOTS:
            page.wait_for_timeout(400)
            page.screenshot(path=f"{SHOTS}/desktop-projects.png")
        ops = page.locator(".card.project[data-project=ops1]")
        ops.get_by_role("button", name="暂停").click()
        page.wait_for_selector(".card.project[data-project=ops1] >> text=已暂停")
        page.locator(".card.project[data-project=ops1]").get_by_role("button", name="继续").click()
        page.wait_for_selector(".card.project[data-project=ops1] >> text=进行中")
        working = page.locator(".card.project[data-project=t-2]")
        working.get_by_role("button", name="停止").click()
        working.get_by_role("button", name="确认停止？").click()
        page.wait_for_selector(".card.project.ended[data-project=t-2] >> text=已停止")

        # "去回答" opens the project the question belongs to.
        page.locator(".card.project.person").get_by_role("button", name="去回答").click()
        assert page.is_visible("#project-detail") and "核对 Laser" in page.inner_text("#project-card")
        assert page.locator("#project-messages .msg").count() == 1 and page.locator("#project-messages .work").count() == 1
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

        # Logging in asked for updates once; the settings page asks again on request.
        assert page.evaluate("window.__fake.checks") == 1
        page.click("#check-update")
        page.wait_for_selector("#update-result:not([hidden])")
        assert page.inner_text("#update-result") == "已经是最新版本（0.0.6）。"
        page.evaluate("window.__fake.latest = '0.0.7'")
        page.click("#check-update")
        page.wait_for_function("document.querySelector('#update-result').textContent.includes('发现新版本 0.0.7')")

        # The version, and a downloaded update offered with a restart.
        assert page.inner_text("#version").startswith("当前版本 0.0.6")
        assert not page.is_visible("#update")
        page.evaluate("""window.__fake.update = { version: '0.0.7', ready: true };
                         window.__fake.fire('update', window.__fake.update)""")
        page.wait_for_selector("#update:not([hidden])")
        assert "0.0.7 已下载好" in page.inner_text("#update") and "0.0.7 已下载好" in page.inner_text("#version")
        page.click("#update-install")
        assert page.evaluate("window.__fake.installed") is True

        page.click("#settings")
        assert page.is_visible("#view-chat") and not page.is_visible("#view-settings")
        assert errors == []
        browser.close()


@pytest.mark.skipif(_browser() is None, reason="needs playwright")
@pytest.mark.parametrize("width", [480, 520, 900])
def test_nothing_in_a_conversation_scrolls_sideways_and_markdown_renders(width):
    sync_playwright = _browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_context(viewport={"width": width, "height": 780}, bypass_csp=True).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: errors.append("dialog: " + d.message))
        page.add_init_script(path=str(DESKTOP / "test" / "fake-bridge.js"))
        page.goto((DESKTOP / "renderer" / "index.html").as_uri())
        page.wait_for_selector("#app[data-ready]")
        page.fill("#username", "me")
        page.fill("#password", "good-password")
        page.click("#connect")
        page.wait_for_selector("#main:not([hidden])")
        # The one conversation holds a reply made to be too wide.
        page.wait_for_selector(".msg.reply.rich table")
        # Open every step, the widest the conversation gets (this test is about layout, not about clicking).
        page.evaluate("document.querySelectorAll('#messages details').forEach((d) => { d.open = true; })")
        page.wait_for_selector(".step-command")
        assert page.locator(".step-command").count() == 2

        # Markdown: what it reads, and what it leaves as text.
        reply = page.locator(".msg.reply", has_text="我这边看不到任何外部服务器").locator(".bubble")
        assert reply.locator("h4").inner_text() == "结果" and reply.locator("strong").count() == 1
        assert reply.locator("em").inner_text() == "AWS EC2"
        assert reply.locator("a").count() == 2 and reply.locator("a").nth(1).get_attribute("target") == "_blank"
        assert reply.locator("pre code").count() == 1 and reply.locator("ol li").count() == 2
        assert reply.locator("blockquote").count() == 1 and reply.locator("hr").count() == 1
        assert reply.locator("script").count() == 0 and "<script>" in reply.locator("blockquote").inner_text()

        # Nothing is wider than the room it has: no sideways scrolling anywhere.
        wide = page.evaluate("""() => {
            const bad = [];
            for (const box of [document.scrollingElement, document.querySelector('#messages')]) {
                if (box.scrollWidth > box.clientWidth + 1) bad.push(`${box.id || box.tagName} ${box.scrollWidth}>${box.clientWidth}`);
            }
            const right = document.querySelector('#messages').getBoundingClientRect().right;
            for (const node of document.querySelectorAll('#messages *')) {
                const r = node.getBoundingClientRect();
                if (r.width && r.right > right + 1) bad.push(`${node.tagName}.${node.className} ${Math.round(r.right)}>${Math.round(right)}`);
                if (node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflowX !== 'hidden'
                    && ['auto', 'scroll'].includes(getComputedStyle(node).overflowX)) bad.push(`scrolls: ${node.tagName}.${node.className}`);
            }
            return bad;
        }""")
        assert wide == [], wide[:8]
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-wide-{width}.png")
        assert errors == []
        browser.close()


@pytest.mark.skipif(_browser() is None, reason="needs playwright")
def test_a_command_on_a_server_waits_in_the_conversation_for_the_persons_approval():
    sync_playwright = _browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_context(viewport={"width": 520, "height": 780}, bypass_csp=True).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(path=str(DESKTOP / "test" / "fake-bridge.js"))
        page.goto((DESKTOP / "renderer" / "index.html").as_uri())
        page.wait_for_selector("#app[data-ready]")
        page.fill("#username", "me")
        page.fill("#password", "good-password")
        page.click("#connect")
        page.wait_for_selector("#main:not([hidden])")

        page.fill("#input", "web 上的 nginx 挂了，帮我重启")
        page.press("#input", "Enter")
        card = page.locator(".approval-box")
        card.wait_for()
        assert "等你批准一条远程命令" in page.inner_text(".work.running summary")
        assert "服务器 web" in card.inner_text() and "systemctl restart nginx" in card.inner_text()
        assert "理由：nginx 没在跑" in card.inner_text()
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-approval.png")
        card.get_by_role("button", name="批准执行").click()
        page.wait_for_selector(".step.ssh.done")
        page.wait_for_selector(".msg.reply >> text=已重启 nginx。")
        assert page.locator(".approval-box").count() == 0 and page.locator(".work.running").count() == 0

        page.fill("#input", "再重启一次")
        page.press("#input", "Enter")
        page.locator(".approval-box").get_by_role("button", name="拒绝").click()
        page.wait_for_selector(".msg.reply >> text=你拒绝了，没有重启。")
        assert "人拒绝了这条命令" in page.locator(".step.ssh.error").last.text_content()
        assert errors == []
        browser.close()


@pytest.mark.skipif(_browser() is None, reason="needs playwright")
def test_the_workers_page_counts_what_runs_and_what_ran():
    sync_playwright = _browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_context(viewport={"width": 520, "height": 780}, bypass_csp=True).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(path=str(DESKTOP / "test" / "fake-bridge.js"))
        page.goto((DESKTOP / "renderer" / "index.html").as_uri())
        page.wait_for_selector("#app[data-ready]")
        page.fill("#username", "me")
        page.fill("#password", "good-password")
        page.click("#connect")
        page.wait_for_selector("#main:not([hidden])")
        page.click("#tab-projects")
        page.click("#seg-workers")
        page.wait_for_selector("#busy:not([hidden])")
        assert page.inner_text("#busy") == "1"
        page.wait_for_selector(".card.worker.busy")
        stats = page.locator("#worker-stats .stat").all_inner_texts()
        assert stats[0].split() == ["1/3", "正在干活", "/", "上限"] and "累计调用 Worker" in stats[2]
        tools = dict(t.split("\n")[::-1] for t in page.locator("#worker-tools .stat").all_inner_texts())
        assert tools == {"Claude Code": "2", "Codex": "1", "远程命令": "4", "本地命令": "12", "文件操作": "5"}
        busy = page.inner_text(".card.worker.busy")
        assert "干活中" in busy and "交给 Claude Code" in busy and "已运行" in busy and "3 分" in busy
        assert "等待中" in page.inner_text(".card.worker.idle")
        ended = page.inner_text(".card.worker.ended")
        assert "已完成" in ended and "用时" in ended and "6 分 0 秒" in ended and "远程命令 4" in ended
        assert page.locator("#workers .group").all_inner_texts() == ["运行中 02", "最近 01"]
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-workers.png")
        page.locator(".card.worker.busy").get_by_role("button", name="查看对话").click()
        assert page.is_visible("#project-detail") and "整理本周交期变化" in page.inner_text("#project-card")
        assert errors == []
        browser.close()


@pytest.mark.skipif(_browser() is None, reason="needs playwright")
def test_searching_the_conversation_finds_and_goes_to_a_message():
    sync_playwright = _browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_context(viewport={"width": 520, "height": 780}, bypass_csp=True).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(path=str(DESKTOP / "test" / "fake-bridge.js"))
        page.goto((DESKTOP / "renderer" / "index.html").as_uri())
        page.wait_for_selector("#app[data-ready]")
        page.fill("#username", "me")
        page.fill("#password", "good-password")
        page.click("#connect")
        page.wait_for_selector("#main:not([hidden])")

        page.fill("#search", "报价")
        page.wait_for_selector("#search-results:not([hidden]) .search-hit")
        hits = page.locator(".search-hit")
        assert page.inner_text(".search-count") == f"找到 {hits.count()} 条" and hits.count() == 4
        assert hits.first.locator("mark").first.inner_text() == "报价"
        # What a message says is shown as text, never as markup.
        page.fill("#search", "<b>")
        page.wait_for_selector(".search-hit mark >> text=<b>")
        assert page.locator(".search-hit b:not(:first-child)").count() == 0
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/desktop-search.png")

        # Going to a result scrolls the conversation to it and marks it for a moment.
        page.fill("#search", "Laser")
        page.wait_for_selector(".search-hit >> text=帮我核对一下")
        page.locator(".search-hit", has_text="帮我核对一下").click()
        assert page.is_hidden("#search-results")
        page.wait_for_selector("#messages .msg.flash >> text=帮我核对一下 Laser Control Board 的供应商报价")

        page.fill("#search", "没有这句话")
        page.wait_for_selector(".search-none")
        assert page.inner_text(".search-none") == "没有找到「没有这句话」"
        page.press("#search", "Escape")
        assert page.input_value("#search") == "" and page.is_hidden("#search-results")
        assert errors == []
        browser.close()
