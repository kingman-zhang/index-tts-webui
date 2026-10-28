#!/usr/bin/env python3
"""邮箱找回密码单测（自跑脚本）。内嵌 mini SMTP 服务器验证完整发信链路。

覆盖的安全边界（这几条比「能改密码」本身更重要）：
  1. 未注册邮箱不发码（404），已注册才发（200）
  2. **注册码不能当重置码用**（purpose 校验）—— 否则「刚注册时收到的码」
     就能去改同邮箱账号的密码
  3. 重置成功后**旧会话全部失效**（忘记密码的前提常是密码已泄露）
  4. 验证码一次性、有错误次数上限、会过期、新密码长度校验
  5. 限频与注册码共用计数器

用法：/Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_password_reset.py
隔离：临时 DATA_DIR；SMTP 走 127.0.0.1 明文（SMTP_TLS=none），不发真实邮件。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-pwreset-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_REG_BONUS"] = "0"   # 本测试不关心积分，去掉噪声
# ⚠️ 必须把存储钉在本地的 file 后端：真实环境变量优先于 .env，本机 .env 配的是
#    MEMBER_STORE=mongo / SESSION_STORE=redis，不钉住就会把测试账号和非真实会话
#    写进**开发库**（2026-10-28 实际踩到：跑一次测试就往 mongo users 塞进几个
#    example.com 账号，第二次再跑直接「邮箱已注册」）。DATA_DIR 只对 file 后端有效。
os.environ["MEMBER_STORE"] = "file"
os.environ["SESSION_STORE"] = "file"

# ─── mini SMTP 服务器（明文，SMTP_TLS=none 配套） ──────────

SMTP_PORT = 35326
smtp_messages: list[dict] = []  # {from, to, data}
STOP = threading.Event()


def _mini_smtp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", SMTP_PORT))
    srv.listen(4)
    srv.settimeout(0.5)
    while not STOP.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        try:
            conn.sendall(b"220 mini smtp\r\n")
            buffer: list[str] = []
            in_data = False
            sender = rcpt = ""
            file = conn.makefile("rb")
            while True:
                line = file.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if in_data:
                    if text == ".":
                        smtp_messages.append({"from": sender, "to": rcpt, "data": "\n".join(buffer)})
                        buffer = []
                        in_data = False
                        conn.sendall(b"250 ok\r\n")
                    else:
                        buffer.append(text)
                    continue
                low = text.lower()
                if low.startswith("ehlo") or low.startswith("helo"):
                    conn.sendall(b"250-mini\r\n250-AUTH PLAIN\r\n250 HELP\r\n")
                elif low.startswith("auth"):
                    conn.sendall(b"235 ok\r\n")
                elif low.startswith("mail from:"):
                    sender = text.split(":", 1)[1].strip()
                    conn.sendall(b"250 ok\r\n")
                elif low.startswith("rcpt to:"):
                    rcpt = text.split(":", 1)[1].strip()
                    conn.sendall(b"250 ok\r\n")
                elif low == "data":
                    in_data = True
                    conn.sendall(b"354 go\r\n")
                elif low == "quit":
                    conn.sendall(b"221 bye\r\n")
                    break
                else:
                    conn.sendall(b"250 ok\r\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
    srv.close()


threading.Thread(target=_mini_smtp_server, daemon=True).start()
time.sleep(0.2)

os.environ["SMTP_HOST"] = "127.0.0.1"
os.environ["SMTP_PORT"] = str(SMTP_PORT)
os.environ["SMTP_USER"] = "noreply@test.local"
os.environ["SMTP_PASS"] = "dummy"
os.environ["SMTP_TLS"] = "none"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.membership import service, store  # noqa: E402
from app.membership.service import (  # noqa: E402
    CODE_PURPOSE_REGISTER, CODE_PURPOSE_RESET,
)

client = TestClient(app)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def set_code_state(email: str, **patch) -> None:
    """直改验证码存储，用于模拟等待/过期等时序场景。"""
    codes = store.load_email_codes()
    codes.setdefault(email, {}).update(patch)
    store.save_email_codes(codes)


def get_code(email: str) -> str:
    return store.load_email_codes().get(email, {}).get("code", "")


def get_entry(email: str) -> dict:
    return store.load_email_codes().get(email, {})


def decode_html(msg: dict) -> str:
    import email as email_lib
    from email import policy
    parsed = email_lib.message_from_string(msg["data"], policy=policy.default)
    for part in parsed.walk():
        if part.get_content_type() == "text/html":
            return part.get_content()
    return ""


EMAIL = "reset1@example.com"
PWD_OLD = "oldpass123"
PWD_NEW = "newpass456"


def main():
    # ─── 前置：先注册一个账号 ─────────────────────────────
    print("── 前置：注册账号 ──")
    r = client.post("/api/auth/email-code", json={"email": EMAIL})
    check("注册码发送成功", r.status_code == 200, r.text)
    code = get_code(EMAIL)
    r = client.post("/api/auth/register-email",
                    json={"email": EMAIL, "code": code, "password": PWD_OLD})
    check("注册成功", r.status_code == 200, r.text)
    user_id = r.json()["user"]["user_id"]
    old_token = r.json()["token"]

    # ─── 发码：存在性判断 ────────────────────────────────
    print("── 发码：存在性判断 ──")
    r = client.post("/api/auth/password-code", json={"email": "nobody@example.com"})
    check("未注册邮箱 404", r.status_code == 404, r.text)
    check("未注册提示可行动", "注册" in r.json().get("detail", ""), r.text)

    r = client.post("/api/auth/password-code", json={"email": "not-an-email"})
    check("非法邮箱 400", r.status_code == 400, r.text)

    # 注册成功时验证码条目已被删除 ⇒ 此刻无 60s 频控，先发一次重置码
    before = len(smtp_messages)
    r = client.post("/api/auth/password-code", json={"email": EMAIL})
    check("已注册邮箱发码成功", r.status_code == 200, r.text)
    check("返回有效期", r.json().get("ttl_minutes") == 10, r.text)
    # 接下来 60s 内再发一次必须被挡（同邮箱一条 entry、一个重发窗口）
    r = client.post("/api/auth/password-code", json={"email": EMAIL})
    check("60s 内重发 429", r.status_code == 429, r.text)
    time.sleep(0.3)
    check("mini SMTP 收到 1 封", len(smtp_messages) == before + 1, str(len(smtp_messages)))
    html = decode_html(smtp_messages[-1])
    check("邮件标题是重置密码", "重置密码验证码" in html, html[:200])
    check("邮件不再出现「正在注册」", "正在注册" not in html, html[:200])
    code_reset = get_code(EMAIL)
    m = re.search(r"\b(\d{6})\b", html)
    check("存储验证码与邮件一致", bool(m) and m.group(1) == code_reset, f"{m and m.group(1)} vs {code_reset}")
    check("存储标记 purpose=reset", get_entry(EMAIL).get("purpose") == CODE_PURPOSE_RESET,
          str(get_entry(EMAIL)))

    # ─── 用途隔离（关键）─────────────────────────────────
    print("── 用途隔离 ──")
    # 手工把 entry 改回「注册码」，模拟用户在注册流程里拿到的码
    set_code_state(EMAIL, purpose=CODE_PURPOSE_REGISTER)
    r = client.post("/api/auth/reset-password",
                    json={"email": EMAIL, "code": code_reset, "new_password": PWD_NEW})
    check("注册码不能重置密码 400", r.status_code == 400, r.text)
    check("提示用途不匹配", "用途" in r.json().get("detail", ""), r.text)
    check("用途不匹配不计错误次数", get_entry(EMAIL).get("attempts", 0) == 0,
          str(get_entry(EMAIL)))

    # 反向：重置码也不能拿去注册
    set_code_state(EMAIL, purpose=CODE_PURPOSE_RESET)
    r = client.post("/api/auth/register-email",
                    json={"email": EMAIL, "code": code_reset, "password": PWD_NEW})
    check("重置码不能注册 400", r.status_code == 400, r.text)

    # ─── 错误码 / 过期 ───────────────────────────────────
    print("── 错误码 / 过期 ──")
    r = client.post("/api/auth/reset-password",
                    json={"email": EMAIL, "code": "000000", "new_password": PWD_NEW})
    check("错误验证码 400", r.status_code == 400, r.text)
    check("错误计数 +1", get_entry(EMAIL).get("attempts", 0) == 1, str(get_entry(EMAIL)))

    set_code_state(EMAIL, expires_ts=time.time() - 1)
    r = client.post("/api/auth/reset-password",
                    json={"email": EMAIL, "code": code_reset, "new_password": PWD_NEW})
    check("过期验证码 400", r.status_code == 400, r.text)
    set_code_state(EMAIL, expires_ts=time.time() + 600)

    r = client.post("/api/auth/reset-password",
                    json={"email": EMAIL, "code": code_reset, "new_password": "123"})
    check("新密码过短 400", r.status_code == 400, r.text)
    check("密码过短不消耗验证码", EMAIL in store.load_email_codes())

    # ─── 重置成功 ────────────────────────────────────────
    print("── 重置成功 ──")
    r = client.post("/api/auth/reset-password",
                    json={"email": EMAIL, "code": code_reset, "new_password": PWD_NEW})
    check("重置成功", r.status_code == 200, r.text)
    check("提示用新密码登录", "新密码" in r.json().get("message", ""), r.text)
    check("验证码一次性（已作废）", EMAIL not in store.load_email_codes(),
          str(store.load_email_codes()))

    r = client.post("/api/auth/login", json={"username": EMAIL, "password": PWD_OLD})
    check("旧密码登录 401", r.status_code == 401, r.text)
    r = client.post("/api/auth/login", json={"username": EMAIL, "password": PWD_NEW})
    check("新密码登录成功", r.status_code == 200, r.text)

    # 重置前签发的会话必须失效（旧 token 已在上面拿到）
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    check("重置前的会话被吊销 401", r.status_code == 401, r.text)

    # 大小写归一：用大写邮箱也能走通整条链路
    set_code_state(EMAIL, sent_ts=time.time() - 120)
    r = client.post("/api/auth/password-code", json={"email": "Reset1@Example.com"})
    check("大写邮箱发码成功", r.status_code == 200, r.text)
    code2 = get_code(EMAIL)
    r = client.post("/api/auth/reset-password",
                    json={"email": "Reset1@Example.com", "code": code2, "new_password": PWD_OLD})
    check("大写邮箱重置成功", r.status_code == 200, r.text)
    r = client.post("/api/auth/login", json={"username": EMAIL, "password": PWD_OLD})
    check("改回旧密码可登录", r.status_code == 200, r.text)

    # ─── 禁用账号 / SMTP 不可用 ──────────────────────────
    print("── 边界 ──")
    set_code_state(EMAIL, sent_ts=time.time() - 120)
    client.post("/api/auth/password-code", json={"email": EMAIL})
    code3 = get_code(EMAIL)
    # 直接改存储的 disabled 字段，不依赖管理接口的具体形态
    store.update_user_fields(user_id, {"disabled": True})
    r = client.post("/api/auth/reset-password",
                    json={"email": EMAIL, "code": code3, "new_password": PWD_NEW})
    check("禁用账号不能重置 403", r.status_code == 403, r.text)
    store.update_user_fields(user_id, {"disabled": False})

    orig = service.mailer.configured
    service.mailer.configured = lambda: False
    try:
        set_code_state(EMAIL, sent_ts=time.time() - 120)
        r = client.post("/api/auth/password-code", json={"email": EMAIL})
        check("未配置 SMTP 503", r.status_code == 503, r.text)
    finally:
        service.mailer.configured = orig

    # ─── 用户名注册的老账号（无 email 字段）────────────────
    print("── 无 email 字段的老账号 ──")
    try:
        service.register("legacyuser", "pass123")
        legacy_ok = True
    except Exception:
        legacy_ok = False
    check("预置用户名注册账号", legacy_ok)
    r = client.post("/api/auth/password-code", json={"email": "legacyuser"})
    check("用户名当邮箱发码 400", r.status_code == 400, r.text)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    STOP.set()
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
