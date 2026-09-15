"""邮件推送：通道可插拔（smtp / file / 以后可加 sendgrid、webhook、企业微信…）。

新增通道 = 继承 Transport 并在 TRANSPORTS 里注册一行；
配置里用 mail.transport 选择。默认还有 file 通道：不发信，把邮件落到 data/outbox/*.eml，
方便在没有邮箱授权码时先看效果、也方便做自动化测试。
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any


class Transport:
    style = ""

    def __init__(self, cfg):
        self.cfg = cfg
        self.section = cfg.get("mail", {}) or {}

    def send(self, message: EmailMessage) -> dict:  # pragma: no cover - 抽象
        raise NotImplementedError


class SmtpTransport(Transport):
    style = "smtp"

    def send(self, message: EmailMessage) -> dict:
        smtp = self.section.get("smtp", {}) or {}
        host = (smtp.get("host") or "").strip()
        if not host:
            return {"ok": False, "transport": "smtp", "error": "未配置 mail.smtp.host"}
        port = int(smtp.get("port", 465))
        security = (smtp.get("security") or "ssl").lower()
        username = (smtp.get("username") or "").strip()
        password = smtp.get("password") or ""
        timeout = float(self.section.get("timeout", 30))
        try:
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(host, port, timeout=timeout)
                server.ehlo()
                if security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            with server:
                if username and password:
                    server.login(username, password)
                server.send_message(message)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "transport": "smtp", "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "transport": "smtp", "to": message["To"]}


class FileTransport(Transport):
    """把邮件写成 .eml（Outlook / Foxmail 可直接打开），用于预览与测试。"""

    style = "file"

    def __init__(self, cfg):
        super().__init__(cfg)
        base = Path(cfg.get("app.data_dir", "data"))
        if not base.is_absolute():
            base = Path(cfg.root) / base
        self.outbox = base / "outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)

    def send(self, message: EmailMessage) -> dict:
        subject = str(message.get("Subject", "mail"))
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in subject)[:60]
        # Windows 文件名不能含 ':'，所以时间戳整段做一次白名单过滤
        stamp = formatdate(localtime=True)
        stamp = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stamp)[:40]
        path = self.outbox / f"{stamp}-{safe}.eml"
        path.write_bytes(message.as_bytes())
        return {"ok": True, "transport": "file", "path": str(path), "to": message["To"]}


TRANSPORTS: dict[str, type[Transport]] = {
    "smtp": SmtpTransport,
    "file": FileTransport,
}


class Mailer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.section = cfg.get("mail", {}) or {}
        style = self.section.get("transport", "smtp")
        cls = TRANSPORTS.get(style, SmtpTransport)
        self.transport = cls(cfg)

    def enabled(self) -> bool:
        if not self.section.get("enabled", False):
            return False
        if self.section.get("transport", "smtp") == "file":
            return True
        return bool((self.section.get("smtp", {}) or {}).get("host")) and bool(self.recipients())

    def recipients(self) -> list[str]:
        raw = self.section.get("to", []) or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace(";", ",").split(",")]
        return [x for x in (str(x).strip() for x in raw) if x]

    # ------------------------------------------------------------------ #
    def build_message(
        self,
        *,
        subject: str,
        html: str,
        text: str = "",
        to: list[str] | None = None,
        cc: list[str] | None = None,
    ) -> EmailMessage:
        smtp = self.section.get("smtp", {}) or {}
        sender = (smtp.get("username") or smtp.get("from") or "").strip()
        from_name = smtp.get("from_name") or "Paper Radar"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((from_name, sender)) if sender else from_name
        msg["To"] = ", ".join(to or self.recipients())
        cc_list = cc if cc is not None else [str(x) for x in (self.section.get("cc") or [])]
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(text or "本邮件需要支持 HTML 的客户端查看。")
        msg.add_alternative(html, subtype="html")
        return msg

    def send(
        self,
        *,
        subject: str,
        html: str,
        text: str = "",
        to: list[str] | None = None,
        cc: list[str] | None = None,
        force: bool = False,
    ) -> dict:
        if not self.enabled() and not force:
            return {"ok": False, "transport": self.transport.style, "error": "邮件未启用或未配置收件人"}
        recipients = to or self.recipients()
        if not recipients:
            return {"ok": False, "transport": self.transport.style, "error": "没有收件人"}
        if self.section.get("dry_run"):
            return {
                "ok": True,
                "transport": "dry-run",
                "to": recipients,
                "note": "mail.dry_run = true，未真正发信",
            }
        message = self.build_message(subject=subject, html=html, text=text, to=recipients, cc=cc)
        result: dict[str, Any] = self.transport.send(message)
        result.setdefault("to", ", ".join(recipients))
        return result
