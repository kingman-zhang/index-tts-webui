"""举报与反馈端点（`POST /api/feedback`）的离线验收。

覆盖的是**契约**，不是 Mongo：
  ① 必须登录（未登录 401）—— 没有 user_id 的反馈无法跟进；
  ② 入参校验（类型非法 / 太短 / 超长 / 联系方式超长都 400，且报错说清原因）；
  ③ **库不可用时回 503**，不假装成功（反馈里带联系方式，静默丢件最伤用户信任）；
  ④ 成功路径落库的字段形状（打桩 `_col()`，不需要真 Mongo）。

为什么第 ④ 步用打桩而不是真库：这个文件要能在**离线回归**里跑
（`outputs/run-offline-tests.py` 屏蔽网络），真 Mongo 会把它变成"环境依赖"。
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="wb-feedback-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_STORE"] = "file"
os.environ["MEMBER_CHECKIN_BONUS"] = "0"
os.environ["MEMBER_REG_BONUS"] = "0"      # 本文件不关心注册礼包，别让它干扰余额断言
os.environ.pop("MONGO_URL", None)          # 先造出"库不可用"的环境（第 ③ 步要用）
os.environ.pop("MEMBER_ADMIN_TOKEN", None)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

_path_exists = Path.exists
with patch.object(Path, "exists", lambda p: False if p.name == ".env" else _path_exists(p)):
    from app.main import app  # noqa: E402
from app.membership import service  # noqa: E402
from app.routes import feedback as fb_routes  # noqa: E402

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


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


r = service.register("fbuser", "secret123")
TOKEN = r["token"]
USER_ID = r["user"]["user_id"]

print("[1] 鉴权：必须登录")
resp = client.post("/api/feedback", json={"category": "bug", "content": "点了没反应"})
check("未登录 → 401", resp.status_code == 401, f"{resp.status_code} {resp.text[:120]}")

print("[2] 入参校验")
resp = client.post("/api/feedback", json={"category": "nope", "content": "点了没反应"},
                   headers=auth(TOKEN))
check("类型非法 → 400", resp.status_code == 400, f"{resp.status_code} {resp.text[:120]}")
check("报错里列出可选类型（能自查）", "bug" in resp.json().get("detail", ""), resp.text[:160])

resp = client.post("/api/feedback", json={"category": "bug", "content": "  太短  "},
                   headers=auth(TOKEN))
check("描述太短 → 400", resp.status_code == 400, f"{resp.status_code} {resp.text[:120]}")

resp = client.post("/api/feedback", json={"category": "bug", "content": "嗯" * 1001},
                   headers=auth(TOKEN))
check("描述超长 → 400", resp.status_code == 400, f"{resp.status_code} {resp.text[:120]}")

resp = client.post("/api/feedback",
                   json={"category": "bug", "content": "点了没反应", "contact": "x" * 101},
                   headers=auth(TOKEN))
check("联系方式超长 → 400", resp.status_code == 400, f"{resp.status_code} {resp.text[:120]}")

print("[3] 库不可用：503 + 说清原因（绝不假装成功）")
resp = client.post("/api/feedback", json={"category": "bug", "content": "点了没反应"},
                   headers=auth(TOKEN))
check("MONGO_URL 缺失 → 503", resp.status_code == 503, f"{resp.status_code} {resp.text[:160]}")
check("503 文案提到「数据库」或「暂时不可用」",
      any(k in resp.json().get("detail", "") for k in ("数据库", "暂时不可用")),
      resp.text[:160])

print("[4] 成功路径：落库字段形状（打桩 _col）")


class _FakeCollection:
    """只实现 insert_one，用来观察"到底写了什么"。"""

    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(doc)

        class _Result:
            inserted_id = doc["_id"]
        return _Result()


fake = _FakeCollection()
original_col = fb_routes._col
fb_routes._col = lambda: fake
try:
    resp = client.post(
        "/api/feedback",
        json={
            "category": "voice",
            "content": "  第 3 段音色发闷，像隔着被子  ",
            "contact": "me@example.com",
            "page_url": "https://voice.lizigege.com/dubbing",
            "user_agent": "Mozilla/5.0 (test)",
        },
        headers={**auth(TOKEN), "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
    )
finally:
    fb_routes._col = original_col

check("成功 → 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:200]}")
body = resp.json()
check("回执带 id（用户可凭它追问）", body.get("ok") is True and body.get("id", "").startswith("fb_"),
      str(body))
check("只写了一条", len(fake.docs) == 1, str(len(fake.docs)))

if fake.docs:
    doc = fake.docs[0]
    check("_id == 回执 id", doc["_id"] == body["id"], f"{doc.get('_id')} vs {body.get('id')}")
    check("带上 user_id（能定位到人）", doc.get("user_id") == USER_ID, str(doc.get("user_id")))
    check("带上 username / nickname", bool(doc.get("username")), str(doc.get("username")))
    check("类型落英文 key + 中文 label",
          doc.get("category") == "voice" and doc.get("category_label") == "音色与音质",
          str((doc.get("category"), doc.get("category_label"))))
    check("描述已 strip（首尾空格不进库）",
          doc.get("content") == "第 3 段音色发闷，像隔着被子", repr(doc.get("content")))
    check("页面地址 / UA 一并记下（复现线索）",
          doc.get("page_url", "").endswith("/dubbing") and "Mozilla" in doc.get("user_agent", ""),
          str((doc.get("page_url"), doc.get("user_agent"))))
    check("IP 取 X-Forwarded-For 第一段", doc.get("ip") == "203.0.113.9", str(doc.get("ip")))
    check("初值 status=new", doc.get("status") == "new", str(doc.get("status")))
    check("created_at 是本地时间字面量（与本项目其它时间戳一致）",
          isinstance(doc.get("created_at"), str) and "T" in doc["created_at"],
          str(doc.get("created_at")))
    check("created_ts 是 BSON Date（可做时间聚合）",
          doc.get("created_ts").__class__.__name__ == "datetime", str(type(doc.get("created_ts"))))

print(f"\n=== 举报与反馈：{PASS} 通过 / {FAIL} 失败 ===")
sys.exit(1 if FAIL else 0)
