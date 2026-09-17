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


SMTP_HINT = (
    "这个报错通常是**网络层有代理/VPN 在拦 SMTP**（例如 Clash 的 TUN 模式会把 "
    "smtp.qq.com 解析成 198.18.x.x 的假 IP，再走代理节点，导致 TLS 握手被中断）。"
    "处理办法：在代理规则里给邮件服务器域名加一条 DIRECT（Clash 示例："
    "`DOMAIN-SUFFIX,qq.com,DIRECT`，并用 Parsers 的 prepend-rules 保证订阅更新后仍在），"
    "或临时关闭 TUN 模式。用 `python scripts/diag_smtp.py` 可一键确认。"
)


class SmtpTransport(Transport):
    style = "smtp"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.smtp = self.section.get("smtp", {}) or {}

    # ------------------------------------------------------------------ #
    def _attempt(self, host: str, port: int, security: str, message: EmailMessage) -> None:
        timeout = float(self.section.get("timeout", 30))
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
        with server:
            username = (self.smtp.get("username") or "").strip()
            password = self.smtp.get("password") or ""
            if username and password:
                server.login(username, password)
            server.send_message(message)

    def _endpoints(self) -> list[tuple[int, str]]:
        """发信端点列表：主用配置，再附上备用端口（465↔587 互备）。

        为什么需要备用：某些网络/代理只放行其中一个端口。
        """
        primary = (int(self.smtp.get("port", 465)), (self.smtp.get("security") or "ssl").lower())
        out = [primary]
        fallback_port = int(self.smtp.get("fallback_port", 587 if primary[0] == 465 else 465))
        fallback_security = (self.smtp.get("fallback_security") or ("starttls" if fallback_port == 587 else "ssl")).lower()
        if (fallback_port, fallback_security) != primary:
            out.append((fallback_port, fallback_security))
        return out

    def send(self, message: EmailMessage) -> dict:
        host = (self.smtp.get("host") or "").strip()
        if not host:
            return {"ok": False, "transport": "smtp", "error": "未配置 mail.smtp.host"}
        retries = max(1, int(self.section.get("retries", 2)))
        errors: list[str] = []
        for port, security in self._endpoints():
            for attempt in range(1, retries + 1):
                try:
                    self._attempt(host, port, security, message)
                except Exception as exc:  # noqa: BLE001
                    detail = f"{host}:{port}/{security} 第{attempt}次 {type(exc).__name__}: {exc}"
                    errors.append(detail)
                    if attempt < retries:
                        import time as _time

                        _time.sleep(1.5 * attempt)  # TLS 被中途掐断常是瞬时的，退避后重试
                    continue
                result = {"ok": True, "transport": "smtp", "to": message["To"], "endpoint": f"{host}:{port}/{security}"}
                if len(errors):
                    result["note"] = "主端点失败后由备用端点成功：" + errors[-1]
                return result
        joined = " | ".join(errors[-3:])
        return {"ok": False, "transport": "smtp", "error": joined, "hint": SMTP_HINT}


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
        if not result.get("ok"):
            # 把"发信失败"在日志层面也说清楚，避免只出现在返回值里没人看见
            print(f"[mailer] 发信失败：{result.get('error')}")
        return result
