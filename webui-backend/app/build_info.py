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
    "app/number_norm.py",
    "app/stores.py",
    "app/queue_worker.py",
    # 引擎层（2026-09-29 拆出）：协议/能力声明、注册表、选择策略
    "app/engines/base.py",
    "app/engines/factory.py",
    "app/engines/selector.py",
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
        from . import name_punct, number_norm, stores, year_norm

        terms = stores.load_global_glossary()
        synth = stores.load_glossary_for_synthesis(None)
        return {
            "name_punct_enabled": name_punct.ENABLED,
            "name_punct_target": name_punct.TARGET,
            # 年份读法（2026-09-29 新增）：这条规则以前只在 tts-server 里，
            # 云引擎链路绕过它 ⇒「以前修好了、现在又坏了」。出现该字段即说明
            # 进程加载的是含 year_norm 的新代码。
            "year_norm_enabled": year_norm.ENABLED,
            "number_norm_enabled": number_norm.ENABLED,
            "glossary_sep_variants": stores.GLOSSARY_SEP_VARIANTS,
            "glossary_sep_variants_max": stores.MAX_SEP_VARIANTS,
            "glossary_terms": len(terms),
            "glossary_terms_for_synthesis": len(synth),
        }
    except Exception as e:  # 自检本身不能把进程带崩
        logger.warning("[build_info] switches 读取失败: %s", e)
        return {"error": str(e)}


def _engines() -> dict:
    """注册了哪些引擎、各自能力、是否在熔断冷却中。

    这几项回答的是「**这次为什么走了这个引擎**」—— 此前完全不可见，
    只能靠猜（`TTS_ENGINE_PREFERRED` 是否生效、有没有 Key、探活结果如何）。
    出现 `engines` 字段本身也说明进程加载的是含能力声明的新代码。
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
        "engines": _engines(),
    }
