"""邮件发送层：QQ/163 等个人邮箱 SMTP 直发（标准库 smtplib，零新依赖）。

配置（环境变量或 .env）：
  SMTP_HOST   SMTP 服务器，如 smtp.qq.com；未配置 = 邮箱功能整体禁用
  SMTP_PORT   端口，默认 465（SSL）；587 则走 STARTTLS
  SMTP_USER   登录账号（通常就是发件邮箱）
  SMTP_PASS   授权码（注意：QQ/163 邮箱是「授权码」不是登录密码）
  SMTP_FROM   发件人显示地址，缺省用 SMTP_USER
  SMTP_FROM_NAME  发件人显示名，默认「播客工坊」

个人开发者量级（每天几十封）完全够用；日后切阿里云邮件推送等通道时
只需替换本文件的 send_email 实现，业务层不动。
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "播客工坊")
# TLS 模式：ssl（465 默认）/ starttls（587 默认）/ none（本地调试，明文）
SMTP_TLS = (os.environ.get("SMTP_TLS", "").strip().lower()
            or ("ssl" if SMTP_PORT == 465 else "starttls"))


def configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def send_email(to: str, subject: str, html: str) -> None:
    """发送一封 HTML 邮件。失败抛 smtplib.SMTPException / OSError。"""
    if not configured():
        raise RuntimeError("SMTP 未配置")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))

    if SMTP_TLS == "none":
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as client:
            client.ehlo()
            if SMTP_USER:
                client.login(SMTP_USER, SMTP_PASS)
            client.sendmail(SMTP_FROM, [to], msg.as_string())
    elif SMTP_TLS == "ssl" or SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15,
                              context=ssl.create_default_context()) as client:
            client.login(SMTP_USER, SMTP_PASS)
            client.sendmail(SMTP_FROM, [to], msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as client:
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.login(SMTP_USER, SMTP_PASS)
            client.sendmail(SMTP_FROM, [to], msg.as_string())


def send_verification_code(to: str, code: str, ttl_minutes: int) -> None:
    """发送注册验证码邮件。"""
    html = f"""\
<div style="max-width:480px;margin:0 auto;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#333;">
  <h2 style="font-size:18px;margin:0 0 16px;">播客工坊 · 注册验证码</h2>
  <p style="font-size:14px;line-height:1.6;margin:0 0 16px;">您好！您正在注册播客工坊账号，验证码为：</p>
  <p style="font-size:28px;font-weight:600;letter-spacing:6px;margin:0 0 16px;color:#534AB7;">{code}</p>
  <p style="font-size:13px;line-height:1.6;color:#888;margin:0 0 8px;">验证码 {ttl_minutes} 分钟内有效，请勿泄露给他人。</p>
  <p style="font-size:13px;color:#888;margin:0;">如果这不是您本人的操作，请忽略本邮件。</p>
</div>"""
    send_email(to, f"注册验证码：{code}", html)
