"""The real model, end to end. Runs only with CONTINUUM_LIVE=1 and DEEPSEEK_API_KEY set; it costs a few cents."""

import asyncio
import os
import time
import tomllib

import pytest

from continuum.plugins import BUILTINS, cli_source, sandbox
from continuum.runtime import Runtime, Settings
from continuum.runtime.settings import DEFAULT_TOML

from test_runtime import drive, replies

pytestmark = pytest.mark.skipif(os.environ.get("CONTINUUM_LIVE") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
                                reason="set CONTINUUM_LIVE=1 and DEEPSEEK_API_KEY to talk to the real model")


def live_runtime(home):
    raw = tomllib.loads(DEFAULT_TOML)
    raw["sources"] = {"cli": {"kind": "cli", "config": {"poll_seconds": 0.05}}}
    raw["tick_seconds"] = 0.05
    raw["router"] = {"kind": "model"}
    raw["executor"] = {"kind": "agent", "config": {"sandbox": {
        "kind": "bwrap" if sandbox.available() else "none", "timeout_seconds": 60}}}
    return Runtime(home, Settings.model_validate(raw), plugins=BUILTINS, entry_points=False)


def test_a_real_request_and_a_follow_up(tmp_path):
    timings = {}

    async def main():
        rt = live_runtime(tmp_path)
        try:
            db = cli_source.db_path(tmp_path)
            start = time.monotonic()
            cli_source.post(db, "me", "main", "在工作目录写一个 Python 脚本 fib.py，打印前 10 个斐波那契数（从 0 开始），"
                                              "运行它，把输出告诉我。")
            await drive(rt, lambda: any(k == "acknowledgement" for k, _ in replies(tmp_path)), timeout=60)
            timings["acknowledged"] = time.monotonic() - start
            await drive(rt, lambda: any(k == "reply" for k, _ in replies(tmp_path)), timeout=180)
            timings["answered"] = time.monotonic() - start
            start = time.monotonic()
            cli_source.post(db, "me", "main", "再改成打印前 15 个", mention=False)
            await drive(rt, lambda: sum(k == "reply" for k, _ in replies(tmp_path)) >= 2, timeout=180)
            timings["follow-up answered"] = time.monotonic() - start
            return rt.store.tickets.list()
        finally:
            await rt.close()

    tickets = asyncio.run(main())
    for kind, text in replies(tmp_path):
        print(f"[{kind}] {text}")
    print({k: f"{v:.1f}s" for k, v in timings.items()})
    assert len(tickets) == 1  # the follow-up went to the same ticket
    answers = [text for kind, text in replies(tmp_path) if kind == "reply"]
    assert "34" in answers[0] and "377" in answers[-1]
