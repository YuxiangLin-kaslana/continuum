"""The `continuum` command: init a home, run it, send it a message, see its state."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import signal
import sys
import time
from pathlib import Path

from continuum.core.stage import ticket_stage
from continuum.plugins import cli_source

from .settings import init_home, settings_path


def _home(value: str | None) -> Path:
    return Path(value or ".continuum").expanduser().resolve()


def cmd_init(args: argparse.Namespace) -> int:
    path = init_home(_home(args.home))
    print(f"Settings: {path}")
    print("Edit [policy.sources.cli].allowed to name who may ask for work; `continuum send --as <id>` uses it.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .app import ConfigurationError, HomeLocked, Runtime

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    home = _home(args.home)
    if not settings_path(home).exists():
        init_home(home)

    async def main() -> int:
        try:
            runtime = Runtime(home)
        except (ConfigurationError, HomeLocked) as error:
            print(f"continuum: {error}", file=sys.stderr)
            return 1
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        logging.getLogger("continuum").info("running in %s", home)
        try:
            await runtime.run(stop)
        finally:
            await runtime.close()
        return 0

    return asyncio.run(main())


def cmd_send(args: argparse.Namespace) -> int:
    home = _home(args.home)
    path = cli_source.db_path(home, args.source)
    before = max((r["id"] for r in cli_source.replies(path, args.thread)), default=0)
    message_id = cli_source.post(path, args.sender, args.thread, args.text, mention=not args.no_mention)
    print(f"sent #{message_id} to {args.thread} as {args.sender}")
    deadline = time.monotonic() + args.wait
    seen = before
    while time.monotonic() < deadline:
        for reply in cli_source.replies(path, args.thread, seen):
            seen = reply["id"]
            label = "确认" if reply["kind"] == "acknowledgement" else "回复"
            print(f"[{label}] {reply['text']}")
            if reply["kind"] == "reply":
                return 0
        time.sleep(0.2)
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    path = cli_source.db_path(_home(args.home), args.source)
    for reply in cli_source.replies(path, args.thread):
        print(f"{reply['at'][:19]} {reply['conversation']} [{reply['kind']}] {reply['text']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .store import SQLiteStore

    path = _home(args.home) / "continuum.db"
    if not path.exists():
        print("nothing here yet: run `continuum run` first")
        return 1
    store = SQLiteStore(path, readonly=True)
    try:
        tickets = store.tickets.list()
        print(f"Tickets: {sum(1 for t in tickets if not t.ended)} open, {sum(1 for t in tickets if t.ended)} ended")
        for ticket in tickets[: args.limit]:
            stage = ticket_stage(ticket)
            state = f"{stage.stage}/{stage.action}" if stage.action else stage.stage
            if ticket.ended:
                state = f"ended:{ticket.ended.outcome}"
            report = ticket.assignments[-1].report if ticket.assignments else None
            print(f"  {ticket.id[:8]}  {state:<18} {ticket.title}")
            if report:
                print(f"            last report: {report.state} - {report.summary}")
            if ticket.attention:
                print(f"            question: {ticket.attention.question}")
        rows = store.db.execute("SELECT kind, key, failures, error FROM effects WHERE failures >= 3").fetchall()
        for kind, key, failures, error in rows:
            print(f"Problem: {kind} {key[:12]} failed {failures} times: {error}")
    finally:
        store.close()
    return 0


def cmd_user(args: argparse.Namespace) -> int:
    from .accounts import MIN_PASSWORD, Accounts
    from .store import SQLiteStore

    home = _home(args.home)
    if not settings_path(home).exists():
        init_home(home)
    store = SQLiteStore(home / "continuum.db")
    try:
        accounts = Accounts(store)
        if args.action == "add":
            token = accounts.add(args.name, args.device or "")
            print(f"Token for {args.name}{' (' + args.device + ')' if args.device else ''}, shown once:")
            print(token)
            print(f"To let {args.name} ask for work, list them under [policy.sources.app].allowed in "
                  f"{settings_path(home)}.")
        elif args.action == "passwd":
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\n")
            else:
                password = getpass.getpass(f"New password for {args.name}: ")
                if getpass.getpass("Again: ") != password:
                    print("The two passwords differ; nothing changed.", file=sys.stderr)
                    return 1
            try:
                accounts.set_password(args.name, password, min_length=1 if args.allow_short else MIN_PASSWORD)
            except ValueError as error:
                print(f"continuum: {error}", file=sys.stderr)
                return 1
            print(f"Password set for {args.name}. The desktop app logs in with it.")
        elif args.action == "list":
            for row in accounts.list():
                state = "revoked" if row["revoked"] else "active"
                print(f"{row['user']:<20} {row['device'] or '-':<16} {state:<8} {row['created_at'][:19]}")
        else:
            count = accounts.revoke(args.name, args.device)
            print(f"revoked {count} token(s) of {args.name}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="continuum")
    parser.add_argument("--home", help="the home directory (default: ./.continuum)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create a home with default settings").set_defaults(run=cmd_init)
    sub.add_parser("run", help="run until interrupted").set_defaults(run=cmd_run)
    send = sub.add_parser("send", help="say something in a command-line conversation")
    send.add_argument("text")
    send.add_argument("--as", dest="sender", default="me")
    send.add_argument("--thread", default="main")
    send.add_argument("--source", default="cli")
    send.add_argument("--no-mention", action="store_true", help="say it without addressing the agent")
    send.add_argument("--wait", type=float, default=10.0, help="seconds to wait for replies")
    send.set_defaults(run=cmd_send)
    log = sub.add_parser("log", help="print the replies of command-line conversations")
    log.add_argument("--thread")
    log.add_argument("--source", default="cli")
    log.set_defaults(run=cmd_log)
    user = sub.add_parser("user", help="manage API users and their tokens")
    user.add_argument("action", choices=["add", "passwd", "list", "revoke"])
    user.add_argument("name", nargs="?")
    user.add_argument("--device", help="a label for the device the token is for")
    user.add_argument("--password-stdin", action="store_true", help="passwd: read the password from standard input")
    user.add_argument("--allow-short", action="store_true",
                      help="passwd: accept a password shorter than 8 characters (easier to guess; failed logins are still throttled)")
    user.set_defaults(run=cmd_user)
    status = sub.add_parser("status", help="print tickets and problems")
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(run=cmd_status)
    args = parser.parse_args(argv)
    if getattr(args, "action", None) in ("add", "passwd", "revoke") and not args.name:
        parser.error(f"user {args.action} needs a name")
    with contextlib.suppress(KeyboardInterrupt):
        return args.run(args)
    return 130


if __name__ == "__main__":
    sys.exit(main())
