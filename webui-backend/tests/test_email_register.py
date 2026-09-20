#!/usr/bin/env python3
"""邮箱注册/验证码单测（自跑脚本）。内嵌 mini SMTP 服务器验证完整发信链路。

用法：/Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_email_register.py
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

TMP = tempfile.mkdtemp(prefix="wb-email-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_REG_BONUS"] = "100"

# ─── mini SMTP 服务器（明文，SMTP_TLS=none 配套） ──────────

SMTP_PORT = 35325
smtp_messages: list[dict] = []  # {from, to, data}


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


STOP = threading.Event()
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
from app.membership.service import MemberError  # noqa: E402

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


def main():
    print("── 请求验证码 ──")
    r = client.post("/api/auth/email-code", json={"email": "not-an-email"})
    check("非法邮箱 400", r.status_code == 400, r.text)

    r = client.post("/api/auth/email-code", json={"email": "user1@example.com"})
    check("发送成功", r.status_code == 200, r.text)
    time.sleep(0.3)
    check("mini SMTP 收到 1 封", len(smtp_messages) == 1, str(len(smtp_messages)))
    check("收件人正确", smtp_messages and "user1@example.com" in smtp_messages[0]["to"], str(smtp_messages))
    import email as email_lib
    from email import policy
    decoded = ""
    if smtp_messages:
        parsed = email_lib.message_from_string(smtp_messages[0]["data"], policy=policy.default)
        for part in parsed.walk():
            if part.get_content_type() == "text/html":
                decoded = part.get_content()
                break
    m = re.search(r"\b(\d{6})\b", decoded)
    check("邮件含 6 位验证码", bool(m))
    code1 = get_code("user1@example.com")
    check("存储验证码与邮件一致", bool(m) and m.group(1) == code1, f"{m and m.group(1)} vs {code1}")

    r = client.post("/api/auth/email-code", json={"email": "user1@example.com"})
    check("60s 内重发 429", r.status_code == 429, r.text)

    # 未配置 SMTP → 503
    orig = service.mailer.configured
    service.mailer.configured = lambda: False
    try:
        set_code_state("user1@example.com", sent_ts=time.time() - 120)  # 解除频控
        r = client.post("/api/auth/email-code", json={"email": "user1@example.com"})
        check("未配置 SMTP 503", r.status_code == 503, r.text)
    finally:
        service.mailer.configured = orig
    set_code_state("user1@example.com", sent_ts=time.time() - 120)

    print("── 邮箱注册 ──")
    r = client.post("/api/auth/register-email", json={"email": "user1@example.com", "code": "000000", "password": "pass123"})
    check("错误验证码 400", r.status_code == 400, r.text)
    check("错误计数 +1", store.load_email_codes()["user1@example.com"]["attempts"] == 1)

    set_code_state("user1@example.com", expires_ts=time.time() - 1)
    r = client.post("/api/auth/register-email", json={"email": "user1@example.com", "code": code1, "password": "pass123"})
    check("过期验证码 400", r.status_code == 400, r.text)
    set_code_state("user1@example.com", expires_ts=time.time() + 600)

    r = client.post("/api/auth/register-email", json={"email": "user1@example.com", "code": code1, "password": "pass123", "nickname": "邮哥"})
    check("注册成功", r.status_code == 200, r.text)
    body = r.json()
    check("赠送 100 分", body["user"]["points"] == 100, str(body["user"]))
    check("email 字段回显", body["user"]["email"] == "user1@example.com")
    check("用户名派生自前缀", body["user"]["username"] == "user1", body["user"]["username"])
    check("昵称生效", body["user"]["nickname"] == "邮哥")
    check("验证码一次性（已作废）", "user1@example.com" not in store.load_email_codes())

    r = client.post("/api/auth/email-code", json={"email": "USER1@example.com"})
    check("重复邮箱（大小写归一）409", r.status_code == 409, r.text)

    print("── 邮箱登录 / 用户名兼容 ──")
    r = client.post("/api/auth/login", json={"username": "user1@example.com", "password": "pass123"})
    check("邮箱登录成功", r.status_code == 200, r.text)
    r = client.post("/api/auth/login", json={"username": "user1", "password": "pass123"})
    check("派生用户名也能登录", r.status_code == 200, r.text)
    r = client.post("/api/auth/login", json={"username": "user1@example.com", "password": "wrong"})
    check("密码错 401", r.status_code == 401, r.text)

    print("── 用户名冲突派生 ──")
    try:
        service.register("zhangsan", "pass123")
        ok = True
    except MemberError:
        ok = False
    check("预置同名用户", ok)
    r = client.post("/api/auth/email-code", json={"email": "zhangsan@qq.com"})
    check("发码成功", r.status_code == 200, r.text)
    code2 = get_code("zhangsan@qq.com")
    r = client.post("/api/auth/register-email", json={"email": "zhangsan@qq.com", "code": code2, "password": "pass123"})
    check("冲突时派生带后缀用户名", r.status_code == 200 and r.json()["user"]["username"] != "zhangsan" and r.json()["user"]["username"].startswith("zhangsan"), r.text)

    print("── 特殊前缀清洗 ──")
    r = client.post("/api/auth/email-code", json={"email": "a.b+c@dev.io"})
    check("发码成功", r.status_code == 200, r.text)
    code3 = get_code("a.b+c@dev.io")
    r = client.post("/api/auth/register-email", json={"email": "a.b+c@dev.io", "code": code3, "password": "pass123"})
    check("点/加号被清洗", r.status_code == 200 and r.json()["user"]["username"] == "abc", r.text)

    print("── 错误次数上限 ──")
    client.post("/api/auth/email-code", json={"email": "brute@x.com"})
    set_code_state("brute@x.com", sent_ts=time.time() - 120, attempts=3)
    r = client.post("/api/auth/register-email", json={"email": "brute@x.com", "code": "111111", "password": "pass123"})
    check("第 4 次错误提示剩余 1 次", r.status_code == 400 and "剩余 1" in r.json()["detail"], r.text)
    set_code_state("brute@x.com", attempts=5)
    r = client.post("/api/auth/register-email", json={"email": "brute@x.com", "code": "111111", "password": "pass123"})
    check("超限作废 400", r.status_code == 400 and "重新获取" in r.json()["detail"], r.text)

    print("── 发送失败回滚 ──")
    r = client.post("/api/auth/email-code", json={"email": "user1@example.com"})  # 已注册 409
    check("已注册邮箱不发码", r.status_code == 409, r.text)

    # 模拟 SMTP 挂掉：请求一个新邮箱但服务器已停止接收（端口占用不存在的服务）
    os.environ["SMTP_PORT"] = "1"  # 不可达
    import importlib
    from app.membership import mailer as mailer_mod
    mailer_mod.SMTP_PORT = 1
    r = client.post("/api/auth/email-code", json={"email": "dead@x.com"})
    check("SMTP 不可达 502", r.status_code == 502, r.text)
    check("失败后验证码作废", "dead@x.com" not in store.load_email_codes())

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    STOP.set()
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
