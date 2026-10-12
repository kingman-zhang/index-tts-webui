"""会话存储后端之二：**Redis**（带 TTL，Gate 3）。

> 这是 `session_store.py` 门面背后的一个实现，不要直接 import 它 —— 业务层一律用
> `from . import session_store`。接口契约见 `session_store.py` 的 `_REQUIRED_API`。

key 设计（全部经 `db.redis_key` 强制带 `REDIS_KEY_PREFIX`，与同实例上别的项目隔离）
```
podcast:session:{token}          -> JSON，TTL = 会话剩余寿命
podcast:user_sessions:{user_id}  -> SET（该用户全部 token），TTL 同步续期
```

为什么要那个反向索引
--------------------
`delete_user_sessions`（改密 / 禁用账号时踢下线）要「按 user_id 找 token」，
而 Redis 没有二级索引。**`KEYS podcast:session:*` 绝不能用** —— 它一次性扫全库、
阻塞整个实例；所以维护一个 user → tokens 的集合。

TTL 与过期的分工
----------------
过期**完全交给 Redis 的 TTL**（不像 file 后端那样自查 `expires_ts`）—— 因此本后端
**不写 `expires_ts` 字段**，也不做惰性清理。`get_session` 一次 GET 就是 O(1)，
这正是把会话放 Redis 的收益所在。

故障语义
--------
连不上 / 命令失败一律翻译成 `db.DBUnavailable`（**不吞、不回落 file**）—— 由
service 层决定怎么呈现：读路径降级为「未登录」，登录路径报 503。详见 service.py。
⚠️ `get_redis()` 自己抛的 `DBUnavailable`（连接串没配 / 没装 redis 包）**原样透出**：
   那是配置错误，不该被伪装成「服务暂时不可用」。
"""

from __future__ import annotations

import json
from typing import Optional

from redis.exceptions import RedisError

from ..db import DBUnavailable, get_redis, redis_key


def _run(desc: str, fn, *args, **kwargs):
    """执行一次 Redis 调用，把命令/连接异常统一翻译成 `DBUnavailable`。"""
    try:
        return fn(*args, **kwargs)
    except DBUnavailable:
        raise  # 配置问题（未配置 / 未安装）：原样透出
    except RedisError as e:
        raise DBUnavailable(f"Redis 会话操作失败（{desc}）：{type(e).__name__}: {e}") from e


# ─── 契约接口 ───────────────────────────────────────────────

def put_session(token: str, data: dict, ttl_seconds: int) -> None:
    r = get_redis()
    ttl = max(1, int(ttl_seconds))          # SETEX 不接受 0 / 负值
    uid = (data or {}).get("user_id")
    pipe = r.pipeline()
    pipe.setex(redis_key("session", token), ttl, json.dumps(data, ensure_ascii=False))
    if uid:
        idx = redis_key("user_sessions", uid)
        pipe.sadd(idx, token)
        # 索引的 TTL 必须**不小于**它指向的会话 TTL，否则索引先消失 → 踢下线漏删：
        #   nx：索引还没有 TTL 时设上（注意 `EXPIRE ... GT` 对「无 TTL 的键」不生效，
        #       会被当成无限 TTL 而直接跳过，所以不能只写 gt）；
        #   gt：已有 TTL 但比本次短时才更新（别把长 TTL 缩短了）。
        pipe.expire(idx, ttl, nx=True)
        pipe.expire(idx, ttl, gt=True)
    _run("put_session", pipe.execute)


def get_session(token: str) -> Optional[dict]:
    raw = _run("get_session", get_redis().get, redis_key("session", token))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 读不出来的脏数据永远读不出来，留着只是垃圾 —— 删掉，让调用方走「重新登录」
        _run("get_session(del corrupt)", get_redis().delete, redis_key("session", token))
        return None


def delete_session(token: str) -> bool:
    """返回是否真的删掉了（幂等：不存在也返回 False）。

    ⚠️ 反向索引里的同名 token 不在这里清 —— 那需要先 GET 拿 user_id（多一次往返），
       而残留 token 由 `delete_user_sessions` 与索引自身的 TTL 兜底，代价为零。
    """
    return bool(_run("delete_session", get_redis().delete, redis_key("session", token)))


def delete_user_sessions(user_id: str) -> int:
    """删掉某用户的全部会话，返回删除条数（改密 / 禁用账号时踢下线用）。"""
    r = get_redis()
    idx = redis_key("user_sessions", user_id)
    tokens = _run("delete_user_sessions(smembers)", r.smembers, idx) or set()
    removed = 0
    if tokens:
        keys = [redis_key("session", t) for t in tokens]
        removed = int(_run("delete_user_sessions(del)", r.delete, *keys) or 0)
    # 索引整体删掉（里面剩下的都是已过期的 token，没必要逐个 SREM）
    _run("delete_user_sessions(del idx)", r.delete, idx)
    return removed


def count_sessions() -> int:
    """当前有效的会话数（自检 / 测试用）。

    ⚠️ 用 `SCAN` 而不是 `KEYS` —— `KEYS` 会阻塞整个 Redis 实例。
    """
    r = get_redis()
    pattern = redis_key("session", "*")

    def _scan():
        return list(r.scan_iter(match=pattern, count=500))

    return len(_run("count_sessions", _scan))
