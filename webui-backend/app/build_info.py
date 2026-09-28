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

容器 / 裸进程部署都适用。`tools/deploy_g1_autodl.sh` 在重启后会自动比对
`git rev-parse` 与这里的 `git_head`：不一致就直接报错，避免「部署脚本跑完了、
其实什么都没变」。
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

# 必须先导入 config —— 它会加载 .env；而 name_punct / number_norm 在**模块级**
# 读 os.environ。顺序反了会让开关显示成默认值（真源是 .env）。这是本仓库已记录
# 过的坑，见 app/stores.py 顶部注释。
from . import config as _config  # noqa: F401  仅为了触发 .env 加载，保持首位

logger = _config.logger

# 进程启动时刻：模块被 import 的那一刻，即进程读走源码的时刻。
PROCESS_STARTED_AT = datetime.now().isoformat(timespec="seconds")

# 仓库根：webui-backend/app/build_info.py → parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]

# 这些文件决定「文本送出去长什么样」，是排错时最需要核对的一组。
# 只取 mtime，不做 import，成本可忽略。
WATCHED = (
    "app/name_punct.py",
    "app/number_norm.py",
    "app/stores.py",
    "app/queue_worker.py",
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
    """被监视源文件的 mtime（ISO 秒）；文件不存在记 None。"""
    out: dict[str, str | None] = {}
    for rel in WATCHED:
        path = REPO_ROOT / "webui-backend" / rel
        try:
            out[rel] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            out[rel] = None
    return out


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
        from . import name_punct, number_norm, stores

        terms = stores.load_global_glossary()
        synth = stores.load_glossary_for_synthesis(None)
        return {
            "name_punct_enabled": name_punct.ENABLED,
            "name_punct_target": name_punct.TARGET,
            "number_norm_enabled": number_norm.ENABLED,
            "glossary_sep_variants": stores.GLOSSARY_SEP_VARIANTS,
            "glossary_sep_variants_max": stores.MAX_SEP_VARIANTS,
            "glossary_terms": len(terms),
            "glossary_terms_for_synthesis": len(synth),
        }
    except Exception as e:  # 自检本身不能把进程带崩
        logger.warning("[build_info] switches 读取失败: %s", e)
        return {"error": str(e)}


def snapshot() -> dict:
    """给 /api/version 的完整快照。"""
    mtimes = source_mtimes()
    stale = [
        rel for rel, ts in mtimes.items()
        if ts and ts > PROCESS_STARTED_AT
    ]
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
        "source_mtimes": mtimes,
        # 非空 ⇒ 这些文件在进程启动之后被改过，进程里跑的是旧代码
        "stale_sources": stale,
        "text_pipeline": _text_switches(),
    }
