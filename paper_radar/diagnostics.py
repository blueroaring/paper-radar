"""链路诊断：把"发不出邮件"这类问题定位到具体一层。

放在包内（而不是 scripts/ 里）是为了让 CLI 能可靠调用：
`python -m paper_radar diag-mail`。
"""

from __future__ import annotations

import socket
import smtplib
import ssl


def _fake_ip(ips: list[str]) -> bool:
    return any(ip.startswith(("198.18.", "198.19.")) for ip in ips)


def diagnose_smtp(cfg) -> int:
    """依次检查 DNS → 465 隐式 SSL → 587 STARTTLS → 真实登录，并给出结论。"""
    smtp = cfg.get("mail.smtp", {}) or {}
    host = smtp.get("host", "smtp.qq.com")
    port = int(smtp.get("port", 465))
    user = smtp.get("username", "")
    password = smtp.get("password", "")

    print("--- 1) DNS 解析 ---")
    fake = False
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
        print(f"  {host} -> {', '.join(ips)}")
        fake = _fake_ip(ips)
        if fake:
            print("  ⚠ 解析到 198.18/198.19 段：这是代理软件（Clash 等）的 fake-IP 段，流量被 TUN 接管了")
    except Exception as exc:  # noqa: BLE001
        print(f"  解析失败: {type(exc).__name__}: {exc}")

    print("--- 2) 465 隐式 SSL ---")
    ok465 = False
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 465), timeout=12) as raw:
            print(f"  TCP 已连接: {raw.getpeername()}")
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                print(f"  ✓ TLS 成功: {tls.version()} / {tls.cipher()[0]}")
                print(f"    欢迎语: {tls.recv(120).decode('utf-8', 'replace').strip()}")
                ok465 = True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 失败: {type(exc).__name__}: {exc}")

    print("--- 3) 587 STARTTLS ---")
    ok587 = False
    try:
        with smtplib.SMTP(host, 587, timeout=12) as s:
            print(f"  TCP 已连接: {s.sock.getpeername()}")
            s.starttls(context=ssl.create_default_context())
            print(f"  ✓ STARTTLS 成功: {s.sock.version()}")
            ok587 = True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 失败: {type(exc).__name__}: {exc}")

    print(f"--- 4) 按配置登录（{host}:{port}）---")
    ok_login = False
    if not (user and password):
        print("  跳过（未配置 mail.smtp.username / password）")
    else:
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(host, port, timeout=20)
                if (smtp.get("security") or "ssl").lower() == "starttls":
                    server.starttls(context=ssl.create_default_context())
            with server:
                server.login(user, password)
                print("  ✓ 登录成功 —— 当前链路可以发信")
                ok_login = True
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 失败: {type(exc).__name__}: {exc}")

    print("--- 结论 ---")
    if ok_login:
        print("  链路正常。若仍收不到邮件，检查垃圾箱/收件规则。")
        return 0
    if fake and not (ok465 or ok587):
        print("  所有 SMTP 端口都不通，且域名被解析到 fake-IP —— 代理软件的 TUN 模式拦截了 SMTP。")
        print("  处理：")
        print("    ① 在代理规则里给邮件域名加 DIRECT，并用 Parsers 的 prepend-rules 固化")
        print("       （Clash 示例：DOMAIN-SUFFIX,qq.com,DIRECT）；")
        print("    ② 或者临时关闭 TUN 模式（改用系统代理）后重试；")
        print("    ③ 改完再跑一次本诊断确认，然后 `python -m paper_radar requeue-mail` 把没发出去的退回重发。")
    elif not (ok465 or ok587):
        print("  两个端口都不通，但域名解析正常 —— 可能是网络/防火墙拦了 SMTP 端口。")
        print("  试试换端口：把 mail.smtp.port 改成 587、security 改成 starttls。")
    else:
        print("  SMTP 端口能通但登录失败 —— 检查授权码是否过期、账号是否填写正确。")
    return 1
