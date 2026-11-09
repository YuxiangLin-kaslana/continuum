"""Tools beyond the sandbox: GitHub, email, Slack and Google, each with the person's own credentials.

An admin connects a tool on the settings page. Its credentials are kept only in a file of their own in Continuum's
home (mode 600), never in the database and never sent back to a client, which sees a masked summary. A worker uses a
connected tool through the host, so the credentials never enter its sandbox:

- github: the `gh` command line with the person's token;
- email: IMAP to search and read, SMTP to send;
- slack: the Slack Web API with a bot token;
- google: `gcloud` with a service account, and the Google APIs (Drive, Docs, Sheets, Cloud) with its token;
- cloudflare: the Cloudflare API (zones, DNS records) with an API token;
- godaddy: the GoDaddy API (domains, DNS records) with an API key and secret;
- aws: the `aws` command line with an access key, never the host's own instance role.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import functools
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import shlex
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx

KINDS = ("github", "email", "slack", "google", "cloudflare", "godaddy", "aws")
NAMES = {"github": "GitHub", "email": "邮箱", "slack": "Slack", "google": "Google（Cloud / Drive / Docs）",
         "cloudflare": "Cloudflare", "godaddy": "GoDaddy", "aws": "AWS"}
# The HTTP APIs a worker calls by path, each with its base address.
API_BASES = {"cloudflare": "https://api.cloudflare.com/client/v4", "godaddy": "https://api.godaddy.com"}
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/drive",
                 "https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/spreadsheets"]
EMAIL_PRESETS = {
    "gmail": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 465},
    "outlook": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com",
                "smtp_port": 587},
    "qq": {"imap_host": "imap.qq.com", "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465},
    "netease": {"imap_host": "imap.163.com", "imap_port": 993, "smtp_host": "smtp.163.com", "smtp_port": 465},
    "icloud": {"imap_host": "imap.mail.me.com", "imap_port": 993, "smtp_host": "smtp.mail.me.com", "smtp_port": 587},
}
PRESET_NAMES = {"gmail": "Gmail", "outlook": "Outlook", "qq": "QQ 邮箱", "netease": "网易邮箱", "icloud": "iCloud"}
# The provider a well-known address belongs to, by its domain.
EMAIL_DOMAINS = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook", "msn.com": "outlook",
    "outlook.cn": "outlook", "live.cn": "outlook",
    "qq.com": "qq", "foxmail.com": "qq", "vip.qq.com": "qq",
    "163.com": "netease", "126.com": "netease", "yeah.net": "netease",
    "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud",
}
# Microsoft's own mailboxes, which stopped taking passwords over IMAP in September 2024.
MICROSOFT_PERSONAL = {"outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.cn", "live.cn"}
# Why a login was refused, and what to do about it, by provider.
LOGIN_HINTS = {
    "gmail": "Gmail 要先开两步验证，再到 myaccount.google.com/apppasswords 生成 16 位应用专用密码填进来（不是登录密码）。",
    "outlook": "公司或学校的 Microsoft 365 邮箱要管理员允许 IMAP 和应用密码。",
    "qq": "QQ 邮箱要在网页版 设置 → 账户 里开启 IMAP/SMTP 服务，用它给的授权码当密码（不是 QQ 密码）。",
    "netease": "网易邮箱要在网页版 设置 → POP3/SMTP/IMAP 里开启 IMAP，用它给的授权码当密码。",
    "icloud": "iCloud 要在 appleid.apple.com → 登录与安全 → App 专用密码 里生成一个填进来。",
}


def email_provider(address: str) -> str | None:
    return EMAIL_DOMAINS.get(address.rpartition("@")[2].strip().lower())


def login_refused(settings: dict[str, Any], error: Exception) -> str:
    """Say in words why the mailbox would not let us in."""
    text = error.args[0] if error.args else str(error)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    domain = str(settings.get("address") or "").rpartition("@")[2].lower()
    if domain in MICROSOFT_PERSONAL:
        hint = ("微软从 2024 年 9 月起不再让 outlook.com / hotmail / live 个人邮箱用密码（包括应用密码）登录 IMAP，"
                "只能用微软账号授权（OAuth）登录。")
    else:
        hint = LOGIN_HINTS.get(str(settings.get("preset") or ""), "请确认填的是邮箱的应用专用密码或授权码，并且已开启 IMAP。")
    return f"邮箱拒绝登录（{text}）。{hint}"
OUTPUT_LIMIT = 15000


class IntegrationError(Exception):
    pass


def _clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[后面截断]"


# Which account each tool uses in the call under way (set by _scoped).
_SELECTED: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("integration_accounts", default=None)


def _scoped(kind: str | None = None) -> Any:
    """A tool call made with one account: `account=` names it (by id or name), or the first is used."""
    def wrap(fn: Any) -> Any:
        @functools.wraps(fn)
        async def scoped(self: Integrations, *args: Any, account: str | None = None, **kwargs: Any) -> Any:
            which = kind or args[0]
            token = _SELECTED.set({**(_SELECTED.get() or {}), which: self._resolve(which, account)})
            try:
                return await fn(self, *args, **kwargs)
            finally:
                _SELECTED.reset(token)
        return scoped
    return wrap


def _slug(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or "account"
    ident, n = base, 2
    while ident in taken:
        ident, n = f"{base}-{n}", n + 1
    return ident


class Integrations:
    def __init__(self, home: Path, transport: httpx.AsyncBaseTransport | None = None):
        self.home = home
        self.folder = home / "integrations"
        self._transport = transport
        # Google access tokens, by account, while they last.
        self._google_token: dict[str, tuple[str, float]] = {}
        # The commands, so tests can stand in for them.
        self.gh = "gh"
        self.gcloud = "gcloud"
        self.aws_cli = "aws"

    # --- what is kept: per tool, a list of accounts, each with its own name, credentials and last test ---

    def _path(self, kind: str) -> Path:
        if kind not in KINDS:
            raise IntegrationError(f"没有叫 {kind} 的工具")
        return self.folder / f"{kind}.json"

    def accounts(self, kind: str) -> list[dict[str, Any]]:
        path = self._path(kind)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if "accounts" in data:
            return data["accounts"]
        # One tool, one account, as first kept: it becomes the default account.
        return [{"id": "default", "label": "默认", **data}] if data.get("secrets") else []

    def _store(self, kind: str, accounts: list[dict[str, Any]]) -> None:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(kind)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"accounts": accounts}, handle, ensure_ascii=False)
        tmp.replace(path)

    def _resolve(self, kind: str, which: str | None) -> str:
        """The id of the account to use: the one named (by id or name), or the first."""
        found = [a for a in self.accounts(kind) if a.get("secrets")]
        if not found:
            raise IntegrationError(f"{NAMES[kind]} 还没有连接")
        if not which:
            return found[0]["id"]
        for a in found:
            if which in (a["id"], a.get("label")) or which.casefold() == str(a.get("label", "")).casefold():
                return a["id"]
        raise IntegrationError(f"{NAMES[kind]} 没有叫“{which}”的账号；有：{'、'.join(a.get('label', a['id']) for a in found)}")

    def get(self, kind: str, which: str | None = None) -> dict[str, Any] | None:
        """An account: the one this call is scoped to, the one named, or the first."""
        chosen = which or (_SELECTED.get() or {}).get(kind)
        found = self.accounts(kind)
        for a in found:
            if chosen in (a["id"], a.get("label")):
                return a
        return found[0] if found else None

    def _write(self, kind: str, body: dict[str, Any]) -> None:
        found = self.accounts(kind)
        for i, a in enumerate(found):
            if a["id"] == body["id"]:
                found[i] = body
                break
        else:
            found.append(body)
        self._store(kind, found)

    def scrub(self, text: str) -> str:
        """Any kept secret that shows up in what a tool said is masked, whatever tool said it."""
        for kind in KINDS:
            for account in self.accounts(kind):
                for value in _secret_values(account.get("secrets") or {}):
                    text = text.replace(value, "***")
        return text

    # --- switches for the coding agents a worker may hand work to (Claude Code, Codex): on unless turned off ---

    def agent_enabled(self, kind: str) -> bool:
        path = self.folder / "agents.json"
        return bool(json.loads(path.read_text(encoding="utf-8")).get(kind, True)) if path.exists() else True

    def set_agent_enabled(self, kind: str, enabled: bool) -> None:
        path = self.folder / "agents.json"
        found = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        found[kind] = bool(enabled)
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(found), encoding="utf-8")

    def connected(self) -> set[str]:
        return {k for k in KINDS if any(a.get("secrets") for a in self.accounts(k))}

    def view(self) -> list[dict[str, Any]]:
        """What a client may see: each tool's accounts, with whom each connects and how its last test went."""
        out = []
        for kind in KINDS:
            accounts = [{"id": a["id"], "label": a.get("label", a["id"]), "account": a.get("account"),
                         "settings": a.get("settings", {}), "test": a.get("test")}
                        for a in self.accounts(kind) if a.get("secrets")]
            first = accounts[0] if accounts else {}
            out.append({"kind": kind, "connected": bool(accounts), "accounts": accounts,
                        "account": first.get("account"), "settings": first.get("settings", {}),
                        "test": first.get("test")})
        return out

    def save(self, kind: str, fields: dict[str, Any], account_id: str | None = None) -> str:
        """Keep an account's credentials and settings (a new account when `account_id` is None); a secret left empty
        keeps the one already kept. Returns the account's id."""
        found = self.accounts(kind)
        old = next((a for a in found if a["id"] == account_id), None) if account_id else None
        if account_id and old is None:
            raise IntegrationError("没有这个账号")
        old = old or {}
        secrets_, settings = dict(old.get("secrets") or {}), dict(old.get("settings") or {})
        if kind in ("github", "cloudflare"):
            if fields.get("token"):
                secrets_["token"] = str(fields["token"]).strip()
        elif kind == "aws":
            for key in ("access_key_id", "secret_access_key", "session_token"):
                if fields.get(key):
                    secrets_[key] = str(fields[key]).strip()
            if fields.get("region") or not settings.get("region"):
                settings["region"] = str(fields.get("region") or "us-east-1").strip()
            if "access_key_id" in secrets_ and "secret_access_key" not in secrets_:
                raise IntegrationError("AWS 要填 Access Key ID 和 Secret Access Key")
        elif kind == "godaddy":
            for key in ("key", "secret"):
                if fields.get(key):
                    secrets_[key] = str(fields[key]).strip()
            if fields.get("key") and not fields.get("secret") and "secret" not in secrets_:
                raise IntegrationError("GoDaddy 要填 API Key 和 Secret")
        elif kind == "slack":
            if fields.get("token"):
                secrets_["token"] = str(fields["token"]).strip()
        elif kind == "email":
            address = str(fields.get("address") or settings.get("address") or "")
            belongs = email_provider(address)
            if not fields.get("preset") and belongs:
                fields = {**fields, "preset": belongs}
            chosen = str(fields.get("preset") or "")
            if belongs and chosen in EMAIL_PRESETS and chosen != belongs:
                raise IntegrationError(f"{address} 是 {PRESET_NAMES[belongs]} 的地址，类型却选了 {PRESET_NAMES[chosen]}，"
                                       f"请把类型改成 {PRESET_NAMES[belongs]}")
            preset = EMAIL_PRESETS.get(chosen, {})
            for key in ("address", "imap_host", "smtp_host"):
                if fields.get(key) or preset.get(key):
                    settings[key] = str(fields.get(key) or preset.get(key)).strip()
            for key in ("imap_port", "smtp_port"):
                if fields.get(key) or preset.get(key):
                    settings[key] = int(fields.get(key) or preset.get(key))
            if fields.get("preset"):
                settings["preset"] = str(fields["preset"])
            if fields.get("password"):
                secrets_["password"] = str(fields["password"])
            if not settings.get("address") or not settings.get("imap_host") or not settings.get("smtp_host"):
                raise IntegrationError("邮箱要填地址，以及收信（IMAP）和发信（SMTP）服务器")
        elif kind == "google":
            if fields.get("key_json"):
                try:
                    key = json.loads(str(fields["key_json"]))
                except json.JSONDecodeError:
                    raise IntegrationError("服务账号密钥不是合法的 JSON") from None
                if key.get("type") != "service_account" or not key.get("private_key") or not key.get("client_email"):
                    raise IntegrationError("这不是服务账号的密钥（要 type=service_account 的 JSON）")
                secrets_["key"] = key
                settings["client_email"] = key["client_email"]
                settings.setdefault("project", key.get("project_id"))
                self._google_token.pop(old.get("id", ""), None)
            if fields.get("project"):
                settings["project"] = str(fields["project"]).strip()
        if not secrets_:
            raise IntegrationError("还没有填凭据")
        label = str(fields.get("label") or old.get("label") or "").strip()[:40]
        if not label:
            taken = {a.get("label") for a in found}
            label = next(n for n in (["默认"] + [f"账号 {i}" for i in range(2, 100)]) if n not in taken)
        if any(a.get("label") == label and a["id"] != old.get("id") for a in found):
            raise IntegrationError(f"已经有一个叫“{label}”的账号了")
        ident = old.get("id") or _slug(label, {a["id"] for a in found})
        self._write(kind, {"id": ident, "label": label, "secrets": secrets_, "settings": settings,
                           "account": old.get("account"), "test": None, "saved_at": datetime.now(UTC).isoformat()})
        return ident

    def remove(self, kind: str, account_id: str | None = None) -> bool:
        found = self.accounts(kind)
        if account_id is None and len(found) == 1:
            account_id = found[0]["id"]
        kept = [a for a in found if a["id"] != account_id]
        if len(kept) == len(found):
            return False
        if kind == "google":
            shutil.rmtree(self.folder / f"gcloud-{account_id}", ignore_errors=True)
            self._google_token.pop(account_id or "", None)
        if kept:
            self._store(kind, kept)
        else:
            self._path(kind).unlink(missing_ok=True)
        return True

    async def test(self, kind: str, account_id: str | None = None) -> dict[str, Any]:
        """Try an account with what is kept; remember who it connects as."""
        ident = self._resolve(kind, account_id)
        token = _SELECTED.set({**(_SELECTED.get() or {}), kind: ident})
        try:
            return await self._test_scoped(kind)
        finally:
            _SELECTED.reset(token)

    async def _test_scoped(self, kind: str) -> dict[str, Any]:
        body = self.get(kind)
        if not body or not body.get("secrets"):
            raise IntegrationError("还没有连接")
        try:
            account = self.scrub(str(await getattr(self, f"_test_{kind}")()))[:200]
            result = {"ok": True, "message": f"已连接：{account}", "at": datetime.now(UTC).isoformat()}
            body["account"] = account
        except Exception as error:  # noqa: BLE001 - the person sees what went wrong
            result = {"ok": False, "message": self.scrub(str(error))[:300] or type(error).__name__,
                      "at": datetime.now(UTC).isoformat()}
        body["test"] = result
        self._write(kind, body)
        return result

    # --- running commands on the host ---

    def _env(self, extra: dict[str, str]) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        return {**env, "HOME": str(self.folder), "NO_COLOR": "1", **extra}

    async def _command(self, argv: list[str], env: dict[str, str], timeout: float = 120) -> tuple[int | None, str]:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.folder), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except FileNotFoundError:
            raise IntegrationError(f"服务器上没有装 {argv[0]}") from None
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return None, f"超过 {timeout:.0f} 秒，已停止"
        return process.returncode, output.decode("utf-8", "replace")

    # --- GitHub ---

    def _gh_env(self) -> dict[str, str]:
        token = ((self.get("github") or {}).get("secrets") or {}).get("token")
        if not token:
            raise IntegrationError("GitHub 还没有连接")
        return self._env({"GH_TOKEN": token, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
                          "GH_CONFIG_DIR": str(self.folder / "gh")})

    @_scoped("github")
    async def github(self, args: str) -> str:
        argv = shlex.split(args)
        if argv and argv[0] == "gh":
            argv = argv[1:]
        if not argv:
            raise IntegrationError("要给 gh 的参数，比如 \"pr list --repo owner/name\"")
        code, output = await self._command([self.gh, *argv], self._gh_env())
        return _clip(f"[exit code {code}]\n{output}")

    async def _test_github(self) -> str:
        code, output = await self._command([self.gh, "api", "user", "--jq", ".login"], self._gh_env(), timeout=30)
        if code != 0:
            raise IntegrationError(output.strip()[-200:] or "令牌不对")
        return output.strip()

    # --- AWS ---

    def _aws_env(self) -> dict[str, str]:
        body = self.get("aws") or {}
        found, settings = body.get("secrets") or {}, body.get("settings") or {}
        if not found.get("access_key_id") or not found.get("secret_access_key"):
            raise IntegrationError("AWS 还没有连接")
        env = self._env({
            "AWS_ACCESS_KEY_ID": found["access_key_id"], "AWS_SECRET_ACCESS_KEY": found["secret_access_key"],
            "AWS_DEFAULT_REGION": settings.get("region") or "us-east-1", "AWS_PAGER": "",
            # Only the person's key: never the host's own role, nor any profile on the host.
            "AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": str(self.folder / "aws-config-none"),
            "AWS_SHARED_CREDENTIALS_FILE": str(self.folder / "aws-credentials-none")})
        if found.get("session_token"):
            env["AWS_SESSION_TOKEN"] = found["session_token"]
        return env

    @_scoped("aws")
    async def aws(self, args: str) -> str:
        argv = shlex.split(args)
        if argv and argv[0] == "aws":
            argv = argv[1:]
        if not argv:
            raise IntegrationError("要给 aws 的参数，比如 \"ec2 describe-instances\"")
        code, output = await self._command([self.aws_cli, *argv], self._aws_env(), timeout=180)
        return _clip(f"[exit code {code}]\n{output}")

    async def _test_aws(self) -> str:
        code, output = await self._command([self.aws_cli, "sts", "get-caller-identity", "--output", "json"],
                                           self._aws_env(), timeout=30)
        if code != 0:
            raise IntegrationError(output.strip()[-200:] or "密钥不对")
        return json.loads(output).get("Arn", "?")

    # --- HTTP APIs: Cloudflare and GoDaddy ---

    def _api_headers(self, kind: str) -> dict[str, str]:
        found = (self.get(kind) or {}).get("secrets") or {}
        if kind == "cloudflare" and found.get("token"):
            return {"Authorization": f"Bearer {found['token']}"}
        if kind == "godaddy" and found.get("key") and found.get("secret"):
            return {"Authorization": f"sso-key {found['key']}:{found['secret']}", "Accept": "application/json"}
        raise IntegrationError(f"{kind} 还没有连接")

    @_scoped()
    async def api(self, kind: str, method: str, path: str, body: Any = None) -> str:
        """Call a connected HTTP API by its path (under its base address)."""
        if kind not in API_BASES:
            raise IntegrationError(f"{kind} 不是按路径调用的接口")
        method = (method or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise IntegrationError("method 只能是 GET、POST、PUT、PATCH、DELETE")
        if not path or not path.startswith("/") or "://" in path or ".." in path:
            raise IntegrationError("path 要以 / 开头，比如 /zones 或 /v1/domains")
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.request(method, API_BASES[kind] + path, headers=self._api_headers(kind),
                                            json=body if body not in (None, "") else None)
        try:
            text = json.dumps(response.json(), ensure_ascii=False, indent=1)
        except ValueError:
            text = response.text
        return _clip(f"[HTTP {response.status_code}]\n{text}")

    async def _test_cloudflare(self) -> str:
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.get(API_BASES["cloudflare"] + "/user/tokens/verify",
                                        headers=self._api_headers("cloudflare"))
            zones = await client.get(API_BASES["cloudflare"] + "/zones?per_page=50",
                                     headers=self._api_headers("cloudflare"))
        answer = response.json()
        if not answer.get("success"):
            raise IntegrationError(f"Cloudflare 拒绝了：{(answer.get('errors') or [{}])[0].get('message', '令牌不对')}")
        names = [z.get("name") for z in (zones.json().get("result") or [])]
        return f"令牌有效，可管理 {len(names)} 个域名" + (f"（{', '.join(names[:5])}{'…' if len(names) > 5 else ''}）"
                                                          if names else "")

    async def _test_godaddy(self) -> str:
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.get(API_BASES["godaddy"] + "/v1/domains?limit=50",
                                        headers=self._api_headers("godaddy"))
        if response.status_code != 200:
            try:
                why = response.json().get("message")
            except ValueError:
                why = response.text[:200]
            raise IntegrationError(f"GoDaddy 拒绝了（HTTP {response.status_code}）：{why}")
        names = [d.get("domain") for d in response.json()]
        return f"{len(names)} 个域名" + (f"（{', '.join(names[:5])}{'…' if len(names) > 5 else ''}）" if names else "")

    # --- Slack ---

    @_scoped("slack")
    async def slack(self, method: str, params: dict[str, Any] | None = None) -> str:
        if not re.fullmatch(r"[a-zA-Z]+(\.[a-zA-Z]+)+", method or ""):
            raise IntegrationError("method 要是 Slack Web API 的方法名，比如 chat.postMessage")
        token = ((self.get("slack") or {}).get("secrets") or {}).get("token")
        if not token:
            raise IntegrationError("Slack 还没有连接")
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.post(f"https://slack.com/api/{method}", json=params or {},
                                         headers={"Authorization": f"Bearer {token}"})
        return _clip(json.dumps(response.json(), ensure_ascii=False, indent=1))

    async def _test_slack(self) -> str:
        answer = json.loads((await self.slack("auth.test")).split("\n…[")[0])
        if not answer.get("ok"):
            raise IntegrationError(f"Slack 拒绝了：{answer.get('error')}")
        return f"{answer.get('user')} @ {answer.get('team')}"

    # --- email ---

    def _email(self) -> tuple[dict[str, Any], str]:
        body = self.get("email") or {}
        password = (body.get("secrets") or {}).get("password")
        if not password:
            raise IntegrationError("邮箱还没有连接")
        return body.get("settings") or {}, password

    def _imap(self) -> imaplib.IMAP4:
        settings, password = self._email()
        imap = imaplib.IMAP4_SSL(settings["imap_host"], int(settings.get("imap_port") or 993),
                                 ssl_context=ssl.create_default_context(), timeout=30)
        try:
            imap.login(settings["address"], password)
        except imaplib.IMAP4.error as error:
            imap.shutdown()
            raise IntegrationError(login_refused(settings, error)) from None
        if settings.get("preset") == "netease":
            # NetEase turns away clients that do not say who they are ("Unsafe Login").
            imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
            imap._simple_command("ID", '("name" "continuum" "version" "1.0")')
        return imap

    @_scoped("email")
    async def email_search(self, query: str, folder: str = "INBOX", limit: int = 20) -> str:
        def work() -> str:
            imap = self._imap()
            try:
                imap.select(_quote(folder or "INBOX"), readonly=True)
                criteria = query.strip() or "ALL"
                if not re.match(r"^(ALL|FROM|TO|SUBJECT|TEXT|BODY|SINCE|BEFORE|ON|UNSEEN|SEEN|OR|NOT|\()", criteria,
                                re.I):
                    criteria = f"TEXT {_quote(criteria)}"
                status, data = imap.uid("SEARCH", "CHARSET", "UTF-8", criteria.encode("utf-8"))
                if status != "OK":
                    status, data = imap.uid("SEARCH", None, criteria)
                uids = (data[0] or b"").split()[-max(1, min(limit, 50)):]
                rows = []
                for uid in reversed(uids):
                    _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                    header = email.message_from_bytes(parts[0][1]) if parts and parts[0] else {}
                    rows.append(f"- uid={uid.decode()} | {_decode(header.get('Date', ''))} | "
                                f"{_decode(header.get('From', ''))} | {_decode(header.get('Subject', ''))}")
                return "\n".join(rows) or "（没有找到）"
            finally:
                imap.logout()

        return await asyncio.to_thread(work)

    @_scoped("email")
    async def email_read(self, uid: str, folder: str = "INBOX") -> str:
        def work() -> str:
            imap = self._imap()
            try:
                imap.select(_quote(folder or "INBOX"), readonly=True)
                _, parts = imap.uid("FETCH", str(uid).encode(), "(BODY.PEEK[])")
                if not parts or not parts[0]:
                    return "（没有这封邮件）"
                message = email.message_from_bytes(parts[0][1])
                head = "\n".join(f"{k}: {_decode(message.get(k, ''))}" for k in ("From", "To", "Cc", "Date", "Subject"))
                return _clip(f"{head}\n\n{_body_text(message)}")
            finally:
                imap.logout()

        return await asyncio.to_thread(work)

    @_scoped("email")
    async def email_send(self, to: str, subject: str, body: str, cc: str = "") -> str:
        settings, password = self._email()

        def work() -> str:
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = settings["address"], to, subject
            if cc:
                message["Cc"] = cc
            message["Date"] = email.utils.formatdate(localtime=True)
            message.set_content(body)
            port = int(settings.get("smtp_port") or 465)
            context = ssl.create_default_context()
            if port == 465:
                server: smtplib.SMTP = smtplib.SMTP_SSL(settings["smtp_host"], port, context=context, timeout=30)
            else:
                server = smtplib.SMTP(settings["smtp_host"], port, timeout=30)
                server.starttls(context=context)
            try:
                server.login(settings["address"], password)
                server.send_message(message)
            finally:
                server.quit()
            return f"已发出：给 {to}{'，抄送 ' + cc if cc else ''}，主题「{subject}」"

        return await asyncio.to_thread(work)

    async def _test_email(self) -> str:
        def work() -> str:
            imap = self._imap()
            try:
                status, data = imap.select("INBOX", readonly=True)
                return f"{self._email()[0]['address']}（收件箱 {data[0].decode() if data and data[0] else '?'} 封）"
            finally:
                imap.logout()

        return await asyncio.to_thread(work)

    # --- Google ---

    def _google_key(self) -> dict[str, Any]:
        key = ((self.get("google") or {}).get("secrets") or {}).get("key")
        if not key:
            raise IntegrationError("Google 还没有连接")
        return key

    async def google_token(self) -> str:
        """An access token for the service account, signed with its key (by openssl, kept briefly)."""
        ident = (self.get("google") or {}).get("id", "")
        kept = self._google_token.get(ident)
        if kept and kept[1] > time.time() + 60:
            return kept[0]
        key = self._google_key()
        now = int(time.time())
        segments = [_b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()), _b64(json.dumps({
            "iss": key["client_email"], "scope": " ".join(GOOGLE_SCOPES), "aud": key.get("token_uri") or
            "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}).encode())]
        unsigned = ".".join(segments).encode()
        signature = await asyncio.to_thread(_sign, key["private_key"], unsigned)
        assertion = unsigned.decode() + "." + _b64(signature)
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            response = await client.post(key.get("token_uri") or "https://oauth2.googleapis.com/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        answer = response.json()
        if "access_token" not in answer:
            raise IntegrationError(f"Google 拒绝了：{answer.get('error_description') or answer.get('error')}")
        self._google_token[ident] = (answer["access_token"], time.time() + int(answer.get("expires_in", 3600)))
        return answer["access_token"]

    @_scoped("google")
    async def google_api(self, method: str, url: str, body: Any = None) -> str:
        if not re.match(r"^https://([a-z0-9-]+\.)*googleapis\.com/", url or ""):
            raise IntegrationError("url 要是 https://…googleapis.com/ 下的地址")
        method = (method or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise IntegrationError("method 只能是 GET、POST、PUT、PATCH、DELETE")
        token = await self.google_token()
        async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
            response = await client.request(method, url, json=body if body not in (None, "") else None,
                                            headers={"Authorization": f"Bearer {token}"})
        text = response.text
        try:
            text = json.dumps(response.json(), ensure_ascii=False, indent=1)
        except ValueError:
            pass
        return _clip(f"[HTTP {response.status_code}]\n{text}")

    @_scoped("google")
    async def gcloud(self, args: str) -> str:
        argv = shlex.split(args)
        if argv and argv[0] == "gcloud":
            argv = argv[1:]
        if not argv:
            raise IntegrationError("要给 gcloud 的参数，比如 \"compute instances list\"")
        env = await self._gcloud_env()
        code, output = await self._command([self.gcloud, *argv, "--quiet"], env, timeout=180)
        return _clip(f"[exit code {code}]\n{output}")

    async def _gcloud_env(self) -> dict[str, str]:
        key = self._google_key()
        config = self.folder / f"gcloud-{(self.get('google') or {}).get('id', 'default')}"
        env = self._env({"CLOUDSDK_CONFIG": str(config), "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
        project = ((self.get("google") or {}).get("settings") or {}).get("project")
        if project:
            env["CLOUDSDK_CORE_PROJECT"] = str(project)
        marker = config / ".activated"
        if not marker.exists() or marker.read_text() != key["client_email"]:
            config.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=self.folder, suffix=".json", delete=False) as handle:
                os.chmod(handle.name, 0o600)
                json.dump(key, handle)
            try:
                code, output = await self._command(
                    [self.gcloud, "auth", "activate-service-account", f"--key-file={handle.name}", "--quiet"], env,
                    timeout=60)
            finally:
                os.unlink(handle.name)
            if code != 0:
                raise IntegrationError(f"gcloud 没能用这个服务账号登录：{output.strip()[-200:]}")
            marker.write_text(key["client_email"])
        return env

    async def _test_google(self) -> str:
        await self.google_token()
        return self._google_key()["client_email"]


def _secret_values(found: Any) -> list[str]:
    """The strings worth masking among kept secrets (and inside a key file), longest first."""
    values: list[str] = []
    if isinstance(found, dict):
        for key, value in found.items():
            if isinstance(value, dict | list):
                values += _secret_values(value)
            elif isinstance(value, str) and len(value) >= 8 and key not in ("type", "client_email", "token_uri",
                                                                             "auth_uri", "project_id", "universe_domain"):
                values.append(value)
    elif isinstance(found, list):
        for value in found:
            values += _secret_values(value)
    return sorted(set(values), key=len, reverse=True)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode(value: Any) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(str(value))))
    except Exception:  # noqa: BLE001 - a header that cannot be decoded is shown as it is
        return str(value)


def _body_text(message: email.message.Message) -> str:
    plain, html = [], []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        (plain if part.get_content_type() == "text/plain" else html).append(text)
    if plain:
        return "\n".join(plain).strip()
    stripped = re.sub(r"<[^>]+>", " ", "\n".join(html))
    return re.sub(r"[ \t]+", " ", stripped).strip()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(private_key: str, data: bytes) -> bytes:
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
        os.chmod(handle.name, 0o600)
        handle.write(private_key)
    try:
        result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", handle.name], input=data, capture_output=True,
                                timeout=30)
    finally:
        os.unlink(handle.name)
    if result.returncode != 0:
        raise IntegrationError(f"没能用服务账号的私钥签名：{result.stderr.decode()[-200:]}")
    return result.stdout
