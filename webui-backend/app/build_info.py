"""运行实例的身份：**这个进程到底在执行哪一份代码**。

## 为什么需要它（2026-09-28）

同一个下午反复出现同一种误判：「本地已修好、服务器 `git log` 也是最新 commit，
但生成出来的音频没变化」。

原因很简单也很隐蔽：
- `git log` 看的是**磁盘上的仓库**；
- 真正干活的是**早就启动、还留在内存里的那个 uvicorn 进程**。

Python 进程在启动时把模块读进内存，之后 `git pull` 改磁盘文件**对它毫无影响**
（除非开了 `--reload`，本项目没有）。所以「代码在磁盘上是最新的」与
「跑的是最新代码」是两件事，而界面上、命令行里都看不出来。

本模块把这两件事变成**可比对的数字**：把进程启动时刻、它实际加载的源文件
mtime、以及 git HEAD 一起吐出来。判据只有一条：

    进程启动时刻 < 源文件 mtime   ⇒   这个进程跑的是旧代码，必须重启

## 暴露方式

    GET /api/version      # 见 routes/system.py

容器 / 裸进程部署都适用。`tools/deploy_g1_autodl.sh` 在重启后会自动核对：
裸进程路径比 `git rev-parse` 与这里的 `git_head`（不一致直接报错）；Docker 路径
容器内没有 `.git`（`git_head` 恒 null），改按**镜像构建输入的最后改动时间**判，
见 `tools/lib_deploy_checks.sh:check_image_fresh`。
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

# 必须先导入 config —— 它会加载 .env；而 name_punct / number_norm 在**模块级**
# 读 os.environ。顺序反了会让开关显示成默认值（真源是 .env）。这是本仓库已记录
# 过的坑，见 app/stores.py 顶部注释。
from . import config as _config  # noqa: F401  仅为了触发 .env 加载，保持首位

logger = _config.logger

# 进程启动时刻：模块被 import 的那一刻，即进程读走源码的时刻。
# 两份表示：浮点秒用于**精确比较**，ISO 字符串只用于展示。
# 不要拿 ISO(timespec="seconds") 去比 mtime —— 秒级截断会把「同一秒内先改文件再启动」
# 判成相等（漏报），也会让读者误以为存在误报。比较必须用浮点。
PROCESS_STARTED_TS = time.time()
PROCESS_STARTED_AT = datetime.fromtimestamp(PROCESS_STARTED_TS).isoformat(timespec="seconds")

# 后端根 = webui-backend/（本文件在 webui-backend/app/build_info.py）。
# 受监视源文件的相对路径是**相对后端根**记的（"app/name_punct.py"），所以解析
# 必须挂在 BACKEND_ROOT 上。
#
# 2026-09-29 修：原来写成 `REPO_ROOT / "webui-backend" / rel`，而 REPO_ROOT 取
# `parents[2]` —— 在容器里 app/ 是 COPY 到 /app 的，本文件在 /app/app/build_info.py，
# parents[2] = `/`，于是去找 `/webui-backend/app/...`，永远不存在 ⇒
# `source_mtimes` 全为 null、`stale_sources` 恒空。后果是**「进程跑的是旧代码」
# 这个探测器在 Docker 部署下彻底失效**——恰恰是最需要它的场景（见模块 docstring）。
BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 仓库根：裸进程部署时用于定位 .git；容器内 /app 无 .git ⇒ git_head 恒为 null 属正常。
REPO_ROOT = BACKEND_ROOT.parent

# 这些文件决定「文本送出去长什么样」与「走哪个引擎」，是排错时最需要核对的一组。
# 只取 mtime，不做 import，成本可忽略。
WATCHED = (
    "app/name_punct.py",
    "app/year_norm.py",
    "app/time_norm.py",
    "app/num_value_norm.py",
    "app/number_norm.py",
    "app/stores.py",
    "app/queue_worker.py",
    # 引擎层（2026-09-29 拆出）：协议/能力声明、注册表、选择策略
    "app/engines/base.py",
    "app/engines/factory.py",
    "app/engines/selector.py",
    # 资源池配置源（2026-09-30）：TTS_URL 派生、资源列表三态来源都在这里
    "app/config.py",
    # 参考音频按需同步（2026-09-30 新增）：**这个文件在 source_mtimes 里出现本身
    # 就证明新代码已上线** —— 旧版本没有它，字段会是 null。这是「版本可观测」
    # 最省事的落点，不必再额外造一个开关字段。
    "app/engines/voice_sync.py",
    "app/engines/indextts_local.py",
)


def _git_head() -> tuple[str | None, str | None]:
    """返回 (短 sha, 提交标题)；git 不可用/非仓库时返回 (None, None)。

    只调用一次（在模块 import 时），所以不进请求热路径。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%h%x00%s"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if out.returncode != 0:
        return None, None
    sha, _, subject = out.stdout.strip().partition("\x00")
    return (sha or None), (subject or None)


GIT_HEAD, GIT_SUBJECT = _git_head()


def source_mtimes() -> dict[str, str | None]:
    """被监视源文件的 mtime（ISO 秒，仅展示用）；文件不存在记 None。

    别名 `source_mtimes_ts()` 返回同样的 map 但值是浮点秒，供精确比较使用。
    """
    out: dict[str, str | None] = {}
    for rel, ts in source_mtimes_ts().items():
        out[rel] = None if ts is None else datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    return out


def source_mtimes_ts() -> dict[str, float | None]:
    """被监视源文件的 mtime（浮点秒）；文件不存在记 None。"""
    out: dict[str, float | None] = {}
    for rel in WATCHED:
        try:
            out[rel] = (BACKEND_ROOT / rel).stat().st_mtime
        except OSError:
            out[rel] = None
    return out


def stale_sources() -> list[str]:
    """在**进程启动之后**才被写过的源文件 —— 即进程内存里仍是旧版本的那些。

    用浮点 mtime 与 PROCESS_STARTED_TS 精确比较，不留秒级模糊地带。
    """
    return [
        rel for rel, ts in source_mtimes_ts().items()
        if ts is not None and ts > PROCESS_STARTED_TS
    ]


def _data_dir() -> dict:
    """数据目录与词表真源的位置。指错目录 = 词条全部静默失效，必须能一眼核对。"""
    from .config import GLOSSARY_PATH

    return {
        "data_dir": str(_config.DATA_DIR),
        "data_dir_exists": _config.DATA_DIR.exists(),
        "glossary_path": str(GLOSSARY_PATH),
        "glossary_exists": GLOSSARY_PATH.exists(),
    }


def _text_switches() -> dict:
    """文本预处理各道关的开关与词表规模（延迟 import，保证 .env 已加载）。"""
    try:
        from . import name_punct, num_value_norm, number_norm, stores, time_norm, year_norm

        terms = stores.load_global_glossary()
        synth = stores.load_glossary_for_synthesis(None)
        return {
            "name_punct_enabled": name_punct.ENABLED,
            "name_punct_target": name_punct.TARGET,
            # 年份读法（2026-09-29 新增）：这条规则以前只在 tts-server 里，
            # 云引擎链路绕过它 ⇒「以前修好了、现在又坏了」。出现该字段即说明
            # 进程加载的是含 year_norm 的新代码。
            "year_norm_enabled": year_norm.ENABLED,
            # 逐位读的**最少位数**。这个数字存在的理由：只有 year_norm_enabled
            # 时，「四位版」与「三位版」的取值都是 true，**分辨不出来** ——
            # 2026-09-29 就撞上了：服务器跑四位版，现象是「2011 读对、850 读错」，
            # 而 /api/version 看不出差异，只能去读 git 史。
            # 看到 3 = 三位规则在跑；看到 4 = 还是老的四位版，必须重新部署。
            "year_norm_min_digits": year_norm.MIN_DIGITS,
            # 时间读法（2026-09-30 新增）：`12:30` → 十二点三十分。这条规则以前
            # 只在 tts-server 里，云引擎链路绕过它 ⇒ 带空格的 `12 : 30` 与小时为 0
            # 的 `0:30` 会被 TN 读成「比」。出现该字段即说明进程加载的是含
            # time_norm 的新代码。
            "time_norm_enabled": time_norm.ENABLED,
            # 受理的「段数」：2 = 只做 时:分。将来做 时:分:秒 会变成 3。
            # 报取值域而非只报开关的理由同 year_norm_min_digits：只有布尔开关时，
            # 「只做时分」与「做时分秒」两版都是 true，端点上分辨不出来。
            "time_norm_max_parts": time_norm.MAX_PARTS,
            # 数值读法（2026-09-29 新增）：单位/幅度词旁的阿拉伯数字换汉字。
            # 同上，报取值域而非只报开关：num_value_max_digits 是这一版规则的
            # 受理位数上限，部署自检断言它 >= 8。
            "num_value_normalize": num_value_norm.ENABLED,
            "num_value_max_digits": num_value_norm.MAX_DIGITS,
            # 「两/二」的语境表（2026-09-30 新增）。初版把「单个 2 一律读两」收得太宽，
            # 用户报 `第2章` 被读成「第两章」。**同样是取值域**：这张表里有没有「第」，
            # 直接说明进程加载的是修过的那一版还是初版 —— 开关两者都是 true，看不出。
            "num_value_ordinal_markers": list(num_value_norm.ORDINAL_MARKERS),
            "number_norm_enabled": number_norm.ENABLED,
            "glossary_sep_variants": stores.GLOSSARY_SEP_VARIANTS,
            "glossary_sep_variants_max": stores.MAX_SEP_VARIANTS,
            "glossary_terms": len(terms),
            "glossary_terms_for_synthesis": len(synth),
        }
    except Exception as e:  # 自检本身不能把进程带崩
        logger.warning("[build_info] switches 读取失败: %s", e)
        return {"error": str(e)}


def _voice_management() -> dict:
    """音色管理的结构版本（2026-10-02 重构）。

    为什么要有这个字段：这次改的是**音色的存储与分发方式**，而改动前后的
    `/api/voices` 都能正常返回列表、上传也能成功 —— 光看接口通不通分辨不出
    进程跑的是哪一版：

    - 旧版：上传只转发给 `TTS_URL` 那一台；服务器命名 `{名}__{owner}`；
      列表里 tts-server 的音色和 backend 的音色混在一起（「我的音色」不纯净）。
    - 新版（`schema_version = 2`）：上传落 `data/voices/<user_id>/<voice_id>.ext`
      + 广播到池内所有 local；服务器命名 `{user_id}_{voice_id}{ext}`（与显示名无关
      ⇒ 改名不用碰 tts-server）；列表用 `scope` 区分「我的」与「共享池」。

    所以报**结构版本**这个取值域，部署自检断言它 >= 2。
    """
    try:
        from .voice_store import USER_VOICES_ROOT

        return {
            "schema_version": 2,
            "layout": "data/voices/<user_id>/<voice_id><ext>",
            "server_naming": "{user_id}_{voice_id}{ext}",
            "store_root": str(USER_VOICES_ROOT),
            "legacy_flat_files_supported": True,   # 老结构不迁移，继续可读可删
        }
    except Exception as e:  # 自检本身不能把进程带崩
        logger.warning("[build_info] voice_management 读取失败: %s", e)
        return {"schema_version": 0, "error": str(e)}


def _membership() -> dict:
    """会员/积分模块的结构版本与关键取值（**取值域**，供部署自检）。

    为什么报这些：2026-10-05 加了「注册礼包 100→500」与「积分购买链路」。
    只报开关证明不了版本 —— 注册礼包必须报**金额**（500 就是新、100 就是旧），
    购买链路必须报 `schema_version`（1=无购买流程，2=有）与 `packs` 清单
    （列表非空即说明套餐表已随代码上线）。`mock_pay_enabled` 是**安全项**：
    接了真实支付却忘了关它 ⇒ 任何登录用户都能白拿积分，必须能一眼看到。
    """
    try:
        from .membership import service as msvc

        return {
            "schema_version": msvc.MEMBERSHIP_SCHEMA_VERSION,
            "signup_bonus": msvc.REG_BONUS,
            "checkin_bonus": msvc.CHECKIN_BONUS,
            "points_per_1000_chars": msvc.POINTS_PER_1000_CHARS,
            "min_charge": msvc.MIN_CHARGE,
            "enforce": msvc.ENFORCE,
            "require_login": msvc.REQUIRE_LOGIN,
            "mock_pay_enabled": msvc.MOCK_PAY,
            "pay_notify_enabled": bool(msvc.PAY_NOTIFY_SECRET),
            "packs": [p["id"] for p in msvc._packs.sorted_packs()],
        }
    except Exception as e:  # 自检本身不能把进程带崩
        logger.warning("[build_info] membership 读取失败: %s", e)
        return {"schema_version": 0, "error": str(e)}


def _engines() -> dict:
    """注册了哪些引擎、各自能力、是否在熔断冷却中。

    这几项回答的是「**这次为什么走了这个引擎**」—— 此前完全不可见，
    只能靠猜（池里注册了谁、有没有 Key、探活结果如何）。
    出现 `engines` 字段本身也说明进程加载的是含能力声明的新代码。
    `config_source` 进一步说明池是按哪份配置起的（内联 / 文件 / 旧式变量），
    排「配了却不生效」时先看它。
    """
    try:
        from .engines.factory import engine_summary

        return {"registered": engine_summary(), "error": None}
    except Exception as e:  # 自检不能把进程带崩
        logger.warning("[build_info] engines 读取失败: %s", e)
        return {"registered": [], "error": str(e)}


def snapshot() -> dict:
    """给 /api/version 与启动日志的完整快照。"""
    return {
        "git_head": GIT_HEAD,
        "git_subject": GIT_SUBJECT,
        "repo_root": str(REPO_ROOT),
        # 数据目录也必须报出来：词表/存档/队列全在这里，而它的缺省值曾是
        # cwd 相对的 "./data" —— 从错误的目录启动 backend 会静默读另一份
        # （通常是空的）data，表现为「词条配了不生效」。2026-09-28 起缺省
        # 改为相对 backend 根，这里打印实际值便于核对。
        **_data_dir(),
        "process_started_at": PROCESS_STARTED_AT,
        "process_started_ts": PROCESS_STARTED_TS,
        "source_mtimes": source_mtimes(),
        # 非空 ⇒ 这些文件在进程启动之后被改过，进程里跑的是旧代码
        "stale_sources": stale_sources(),
        "text_pipeline": _text_switches(),
        "voice_management": _voice_management(),
        "membership": _membership(),
        "engines": _engines(),
    }
