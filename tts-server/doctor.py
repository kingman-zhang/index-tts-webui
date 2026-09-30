#!/usr/bin/env python3
"""tts-server 启动前体检（**只读**，不改任何文件、不装任何东西）。

为什么需要这个脚本
    tts-server 不是独立服务，它是 index-tts 源码的 HTTP 壳。启动时它只做三件事：

      ① sys.path.insert(0, <indextts-home>)  →  from indextts.infer_v2 import IndexTTS2
      ② 用 <model-dir>/config.yaml 加载模型权重
      ③ uvicorn 监听 <host>:<port>

    坑在于 **第 ② 步失败不会让进程退出** —— server.py 把异常吞掉、把 tts 置为 None，
    服务照常监听，/api/health 也照常返回 200，只是 status="no_model"。
    于是「进程起来了 / 端口通了」很容易被误判成「部署成功」，直到第一次合成才炸。

    本脚本把上机要点一次查完，并把结论直接翻译成「下一步敲哪条命令」。

用法
    # 用系统 python 跑（能查文件、端口、依赖；查不到 torch 会明确提示）
    python3 doctor.py

    # 用 index-tts 的 venv 跑（**推荐**：能真正查 torch / CUDA / 缺少的包）
    /root/index-tts/.venv/bin/python doctor.py

    # 自动探测不准时手动指定
    /root/index-tts/.venv/bin/python doctor.py \\
        --indextts-home /root/index-tts \\
        --model-dir /mnt/storage/index-tts-data/checkpoints \\
        --voices-dir /mnt/storage/index-tts-data/voices \\
        --port 8000

退出码
    0 = 无阻断   1 = 有告警（能启动但功能可能不全）   2 = 有阻断（现在启动一定失败）
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_ORDER = {OK: 0, WARN: 1, FAIL: 2}
_ICON = {OK: "✓", WARN: "!", FAIL: "✗"}

# 权重文件后缀：用于从 config.yaml 里挑出「必须存在的文件」
WEIGHT_EXT = (".pt", ".pth", ".bin", ".safetensors", ".npy", ".model", ".vocab", ".yaml", ".yml", ".json")

# server.py 之外的必需依赖（index-tts 自身 venv 已含 torch 等）
EXTRA_DEPS = (
    ("fastapi", "fastapi", "fastapi>=0.110.0"),
    ("uvicorn", "uvicorn", "uvicorn[standard]>=0.29.0"),
    ("pydantic", "pydantic", "pydantic>=2.6.0"),
    ("python-multipart", "multipart", "python-multipart>=0.0.9"),
)

_START = time.time()
ROWS: list[tuple[str, str, str, str, str]] = []  # level, group, title, detail, fix


def add(level: str, group: str, title: str, detail: str = "", fix: str = "") -> None:
    ROWS.append((level, group, title, detail, fix))


# ─── 目录探测 ───────────────────────────────────────────────

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages",
              ".mypy_cache", ".pytest_cache", ".cache", "hf_cache"}


def _scan(start: Path, max_depth: int, want: str, limit: int = 4000):
    """在 start 下按最大深度找指定相对路径（如 indextts/infer_v2.py）。"""
    if not start.is_dir():
        return
    start_depth = len(start.parts)
    stack = [start]
    seen = 0
    while stack:
        d = stack.pop()
        seen += 1
        if seen > limit:
            return
        if (d / want).is_file():
            yield d
            continue
        if len(d.parts) - start_depth >= max_depth:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False) and e.name not in _SKIP_DIRS \
                            and not e.name.startswith("."):
                        stack.append(Path(e.path))
        except OSError:
            continue


def find_indextts_home(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    cands = [Path(p) for p in (
        "/root/index-tts", "/root/index-tts-2.5", "/root/index-tts-2", "/opt/index-tts",
        "/mnt/storage/index-tts", "/mnt/storage/index-tts-data/index-tts",
        "/mnt/storage/index-tts-data/server-src/index-tts", "/data/index-tts")]
    here = Path(__file__).resolve()
    for base in here.parents[:4]:
        for name in ("index-tts", "index-tts-main", "index-tts-2.5", "index-tts2"):
            cands.append(base / name)
    for p in cands:
        if (p / "indextts" / "infer_v2.py").is_file():
            return p
    for root in ("/root", "/opt", "/mnt", "/mnt/storage", "/data", "/home"):
        for hit in _scan(Path(root), 3, "indextts/infer_v2.py"):
            return hit
    return None


def find_model_dir(explicit: str | None, indextts_home: Path | None) -> Path | None:
    if explicit:
        return Path(explicit)
    cands: list[Path] = []
    if indextts_home:
        cands.append(indextts_home / "checkpoints")
    cands += [Path(p) for p in (
        "/mnt/storage/index-tts-data/checkpoints", "/mnt/storage/index-tts-data/checkpoints_25",
        "/root/index-tts/checkpoints", "/data/index-tts-data/checkpoints")]
    here = Path(__file__).resolve()
    for base in here.parents[:3]:
        cands.append(base / "checkpoints")
    for p in cands:
        if (p / "config.yaml").is_file():
            return p
    for root in ("/mnt/storage", "/root", "/data", "/opt"):
        hits = [d for d in _scan(Path(root), 3, "config.yaml")
                if any(f.suffix in (".pt", ".pth", ".bin", ".safetensors") for f in d.iterdir() if f.is_file())]
        if hits:
            return hits[0]
    return None


def find_voices_dir(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    cands = [Path(p) for p in (
        "/mnt/storage/index-tts-data/voices", "/root/index-tts/voices",
        "/data/index-tts-data/voices")]
    here = Path(__file__).resolve()
    cands += [here.parent / "voices", here.parent / "data" / "preset-voices"]
    audio = (".wav", ".mp3", ".flac", ".ogg", ".webm")
    for p in cands:
        if p.is_dir() and any(f.suffix.lower() in audio for f in p.iterdir() if f.is_file()):
            return p
    for root in ("/mnt/storage/index-tts-data", "/root"):
        base = Path(root)
        if base.is_dir():
            for e in base.iterdir():
                if e.is_dir() and "voice" in e.name.lower():
                    return e
    return None


# ─── config.yaml 解析（不依赖 omegaconf） ────────────────────

_KV = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*:\s*(.+?)\s*$")


def parse_config_refs(cfg_path: Path) -> list[tuple[str, str]]:
    """从 config.yaml 里抽出所有形如 `key: xxx.pt` 的引用（兼容嵌套缩进）。"""
    try:
        text = cfg_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    refs: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _KV.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).split("#")[0].strip().strip("'\"")
        if not val or val.lower() in ("null", "none", "true", "false"):
            continue
        if val.endswith(WEIGHT_EXT) or "/" in val:
            refs.append((key, val))
    return refs


EXTRA_WEIGHT_FILES = ("pinyin.vocab", "glossary.yaml")

# 顶层键 = 行首无缩进、非注释、含冒号（`"C++":` 或 `"C++": {...}`）。
# 缩进行是某个词条的 en/zh 取值，不算新词条。
_TOP_KEY = re.compile(r"^([^\s#][^:]*?)\s*:(?:\s|$)")


def count_glossary_entries(path: Path) -> int:
    """粗略数 `glossary.yaml` 的**顶层词条数**（刻意不依赖 pyyaml）。

    这只是个提示性数字：数不出来就返回 0，不值得为它报错。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    n = 0
    for line in text.splitlines():
        if not line.strip() or line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        if _TOP_KEY.match(line):
            n += 1
    return n


# ─── 各项检查 ───────────────────────────────────────────────

def check_interpreter() -> None:
    exe = Path(sys.executable)
    ver = "%d.%d.%d" % sys.version_info[:3]
    in_venv = sys.prefix != sys.base_prefix or "VIRTUAL_ENV" in os.environ
    if in_venv:
        add(OK, "解释器", "运行在虚拟环境中", f"{exe}  (Python {ver})")
    else:
        add(WARN, "解释器", "当前不是虚拟环境", f"{exe}  (Python {ver})",
            "tts-server 必须用 index-tts 的 venv 解释器启动，例如 "
            "/root/index-tts/.venv/bin/python server.py；否则 import torch / indextts 会失败")
    if sys.version_info < (3, 10):
        add(FAIL, "解释器", "Python 版本过低", ver, "IndexTTS2 需要 Python >= 3.10")


def check_torch(deep: bool) -> bool:
    if importlib.util.find_spec("torch") is None:
        add(FAIL, "运行时", "当前解释器里没有 torch",
            "说明用的不是 index-tts 的 venv",
            "改用 /root/index-tts/.venv/bin/python 运行；若确认 venv 里也没有，"
            "在 index-tts 目录里执行 uv sync --all-extras")
        return False
    try:
        import torch  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        add(FAIL, "运行时", "import torch 失败", repr(exc)[:200],
            "常见原因：venv 与系统 CUDA 不匹配，或 torch 安装损坏")
        return False
    has_cuda = torch.cuda.is_available()
    if not has_cuda:
        add(FAIL, "运行时", "torch 装好了，但 CUDA 不可用",
            f"torch {torch.__version__}  cuda_build={torch.version.cuda}",
            "检查 nvidia-smi 是否有输出、驱动是否正常；CPU 上跑 IndexTTS2 不现实")
        return False
    n = torch.cuda.device_count()
    name = torch.cuda.get_device_name(0)
    free = total = 0
    try:
        free_b, total_b = torch.cuda.mem_get_info(0)
        free, total = free_b / 2 ** 30, total_b / 2 ** 30
    except Exception:
        pass
    detail = f"torch {torch.__version__} | CUDA {torch.version.cuda} | {n} 卡 | 0 号: {name}"
    if total:
        detail += f" | 显存 空闲 {free:.1f}G / 共 {total:.1f}G"
    add(OK, "运行时", "torch + CUDA 可用", detail)
    if total and free < 6:
        add(WARN, "运行时", "0 号卡可用显存偏低", f"空闲 {free:.1f}G",
            "IndexTTS2 fp16 通常需 6~8G 以上；先确认没有别的进程占着卡（nvidia-smi）")
    return True


def check_indextts(indextts_home: Path | None, deep: bool) -> None:
    if indextts_home is None or not (indextts_home / "indextts" / "infer_v2.py").is_file():
        add(FAIL, "index-tts 源码", "找不到 index-tts 源码目录",
            f"探测结果: {indextts_home}",
            "server.py 靠 --indextts-home 把这个目录塞进 sys.path；"
            "用 --indextts-home /root/index-tts 显式指定（该目录下应有 indextts/infer_v2.py）")
        return
    v25 = (indextts_home / "indextts" / "infer_v2_5.py").is_file()
    add(OK, "index-tts 源码", "源码目录就位",
        f"{indextts_home}  (infer_v2.py 有 / infer_v2_5.py {'有' if v25 else '无'})")

    venv_py = indextts_home / ".venv" / "bin" / "python"
    if venv_py.is_file():
        add(OK, "index-tts 源码", "同目录下有 venv", str(venv_py),
            f"启动就用它：{venv_py} server.py ...")
    else:
        add(WARN, "index-tts 源码", "源码目录下没有 .venv",
            f"期望 {venv_py}",
            "如果环境在别处（conda env / 另一个路径），启动时用那个 python 即可")

    if not deep:
        return
    if sys.path[0] != str(indextts_home):
        sys.path.insert(0, str(indextts_home))
    try:
        mod = __import__("indextts.infer_v2", fromlist=["IndexTTS2"])
        cls = getattr(mod, "IndexTTS2", None)
        add(OK if cls else FAIL, "index-tts 源码",
            "from indextts.infer_v2 import IndexTTS2 成功" if cls else "模块里没有 IndexTTS2",
            f"模块文件: {mod.__file__}")
    except Exception as exc:
        add(FAIL, "index-tts 源码", "import indextts.infer_v2 失败", repr(exc)[:300],
            "按报错补依赖：缺包就在 venv 里装；若是 'No module named indextts' "
            "说明 --indextts-home 指错了目录")


def check_model_dir(model_dir: Path | None) -> None:
    if model_dir is None:
        add(FAIL, "模型权重", "找不到模型目录（没有含 config.yaml 且在权重文件的目录）",
            "", "用 --model-dir 指定；官方权重: modelscope download --model IndexTeam/IndexTTS-2 "
            "--local_dir /mnt/storage/index-tts-data/checkpoints")
        return
    cfg = model_dir / "config.yaml"
    if not cfg.is_file():
        add(FAIL, "模型权重", "模型目录里没有 config.yaml", str(model_dir),
            "IndexTTS2 构造函数第一个参数就是 <model-dir>/config.yaml，缺它必然启动失败")
        return

    refs = parse_config_refs(cfg)
    missing: list[tuple[str, str]] = []
    okn = 0
    for key, val in refs:
        p = Path(val)
        target = p if p.is_absolute() else model_dir / val
        if target.is_file():
            okn += 1
        else:
            missing.append((key, val))

    for extra in EXTRA_WEIGHT_FILES:
        t = model_dir / extra
        if t.is_file():
            okn += 1

    if missing:
        hard = [(k, v) for k, v in missing if "qwen" not in k.lower() and "emo_text" not in k.lower()]
        soft = [(k, v) for k, v in missing if (k, v) not in hard]
        if hard:
            add(FAIL, "模型权重", f"config.yaml 引用的权重缺 {len(hard)} 个",
                "; ".join(f"{k}={v}" for k, v in hard[:6]),
                "权重没下全：重新 modelscope download 到同一目录；"
                "或这些文件其实在别处（PATH 写错），可用 --model-dir 指到真正的那份")
        if soft:
            add(WARN, "模型权重", f"可选模型缺失 {len(soft)} 个（情感文本模式才用）",
                "; ".join(f"{k}={v}" for k, v in soft[:4]), "不用情感文本模式可以先不管")
    else:
        add(OK, "模型权重", f"config.yaml 引用全部就位（{okn} 项）", str(model_dir))

    weights = [f for f in model_dir.iterdir()
               if f.is_file() and f.suffix in (".pt", ".pth", ".bin", ".safetensors")]
    size_gb = sum(f.stat().st_size for f in weights) / 2 ** 30
    add(OK if weights else WARN, "模型权重", "主权重文件与体积",
        f"{len(weights)} 个权重文件，共 {size_gb:.2f} GB",
        "" if weights else "目录里没有 .pt/.pth/.bin/.safetensors，权重很可能没下")

    # 引擎内置术语表（2026-09-30 新增）。**这一项必须显式报**：
    # index-tts 自带另一套术语表（front.py 的 TextNormalizer.term_glossary），
    # infer_v2.py 在 <model_dir>/glossary.yaml 存在时**自动加载**（启动日志会打
    # ">> Glossary loaded from:"）。它与 backend 的那套是两套、规则也不同：
    #   backend：str.replace 精确匹配、中点变体展开、用户可增删（data/glossary.json）
    #   引擎  ：按词条长度降序 + re.IGNORECASE 的 re.sub，支持 {zh,en} 双语读法
    # 两者会**串联**（backend 先替换，引擎再替换），backend 完全感知不到、
    # /api/version 也看不到。表现是「同一句话，本地引擎与云引擎读法不一样」，
    # 排查时极易误判成「backend 的词条没生效」。所以这里报出来，让人有据可查。
    gl = model_dir / "glossary.yaml"
    if gl.is_file():
        add(WARN, "模型权重", f"存在引擎内置术语表 glossary.yaml（{count_glossary_entries(gl)} 条）",
            str(gl),
            "它会与 backend 的术语表**串联**（backend 先替换、引擎再替换），规则与 backend "
            "不同，且 /api/version 看不到。若不是有意为之，删掉/改名即关闭；"
            "详见 webui-backend/ENGINES.md 的「两套术语表」")
    else:
        add(OK, "模型权重", "没有引擎内置术语表（glossary.yaml 不存在）",
            "引擎不会额外做术语替换；backend 的词条照常生效")

    hf_cache = model_dir / "hf_cache"
    if hf_cache.is_dir() and any(hf_cache.iterdir()):
        add(OK, "模型权重", "辅助模型缓存已在模型目录内", str(hf_cache),
            "新版源码（index-tts-main）读 {model_dir}/hf_cache")
    else:
        add(WARN, "模型权重", "模型目录内没有辅助模型缓存",
            f"期望 {hf_cache}",
            "首次启动会联网拉 4 个辅助模型（w2v-bert-2.0 等，约 2.3GB+）。"
            "国内先设 HF_ENDPOINT=https://hf-mirror.com；"
            "老版 v2.0.0 读的是 <启动目录>/checkpoints/hf_cache，用 "
            "tools/prefetch_aux_models.py --cache <DIR> 预下载可避免静默卡住")


def check_deps() -> None:
    absent: list[str] = []
    present: list[str] = []
    for dist, mod, spec in EXTRA_DEPS:
        has = importlib.util.find_spec(mod) is not None
        if not has:
            absent.append(spec)
            continue
        try:
            ver = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            ver = "?"
        present.append(f"{dist} {ver}")
    if absent:
        venv = sys.prefix
        add(FAIL, "额外依赖", f"缺 {len(absent)} 个包", ", ".join(a.split(">=")[0] for a in absent),
            f"{venv}/bin/pip install " + " ".join(f'"{a}"' for a in absent))
    if present:
        add(OK, "额外依赖", "server.py 需要的 4 个包齐全", ", ".join(present))


def _find_ffmpeg() -> str | None:
    """PATH 优先，再兜底常见安装路径。

    注意：不能只信 shutil.which —— 本机（macOS homebrew）ffmpeg 装在
    /opt/homebrew/bin 而不在 PATH 里，只信 which 会误报「没装」。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(cand).is_file():
            return cand
    return None


def check_ffmpeg() -> None:
    p = _find_ffmpeg()
    if p:
        add(OK, "音频工具", "ffmpeg 就位", p,
            "双人播客（/api/podcast）的变速与响度归一依赖它；单段 /api/synthesize 不需要")
        return
    add(WARN, "音频工具", "没有找到 ffmpeg",
        "podcast_engine._apply_speed 里 shutil.which('ffmpeg') 找不到会直接 RuntimeError",
        "双人播客会整段失败。装：apt-get install -y ffmpeg（或 conda install -c conda-forge ffmpeg）")


def check_voices(voices_dir: Path | None) -> None:
    if voices_dir is None:
        add(WARN, "音色目录", "没探测到音色目录", "",
            "server.py 起不来不是问题（会自动 mkdir），但 backend 传过来的参考音频路径"
            "必须落在 --voices-dir 里，否则合成报 400 参考音频不存在")
        return
    audio = (".wav", ".mp3", ".flac", ".ogg", ".webm")
    n = sum(1 for f in voices_dir.iterdir() if f.is_file() and f.suffix.lower() in audio)
    if n:
        add(OK, "音色目录", f"{n} 个参考音频", str(voices_dir))
    else:
        add(WARN, "音色目录", "目录里没有参考音频", str(voices_dir),
            "把 backend 的 data/preset-voices/ 同步过来（backend 不会上传参考音频，只传路径）")


def check_port(port: int) -> None:
    already = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            already = True
    except OSError:
        already = False

    health = probe_health(port)
    if health is not None:
        status = health.get("status")
        loaded = health.get("model_loaded")
        ok = (status == "ok" and loaded is True)
        add(OK if ok else WARN, "端口与服务",
            f"{port} 端口上已经有一个 tts-server 在跑",
            f"status={status} model_loaded={loaded} device={health.get('device')}",
            "这是正常的" if ok else
            "**但它是「起来了没法用」状态**：model 没加载成功。看它的启动日志找 "
            "model load failed 那行，别被端口通了骗过去")
        return
    if already:
        add(FAIL, "端口与服务", f"{port} 端口被别的进程占用，但不是 tts-server",
            "127.0.0.1:%d 可连接但 /api/health 不是预期响应" % port,
            f"换端口（--port 8001）或先腾出来：ss -ltnp | grep :{port}")
    else:
        add(OK, "端口与服务", f"{port} 端口空闲", "可以直接启动")


def probe_health(port: int):
    """带短超时的 /api/health 探测；显式绕过代理（服务器上通常有 HTTP_PROXY）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2.0) as r:
            import json
            return json.loads(r.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def check_disk(paths: list[Path]) -> None:
    checked = set()
    for p in paths:
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if probe in checked:
            continue
        checked.add(probe)
        try:
            u = shutil.disk_usage(probe)
        except OSError:
            continue
        free_gb = u.free / 2 ** 30
        lvl = OK if free_gb >= 10 else WARN
        add(lvl, "磁盘", f"{probe} 剩余 {free_gb:.1f} GB",
            f"总 {u.total / 2 ** 30:.0f} GB",
            "" if free_gb >= 10 else "输出音频会持续占用，建议清一下或换到数据盘")


# ─── 输出 ───────────────────────────────────────────────────

def render(indextts_home, model_dir, voices_dir, port, tts_dir) -> int:
    worst = max((_ORDER[r[0]] for r in ROWS), default=0)
    groups: list[str] = []
    for row in ROWS:
        if row[1] not in groups:
            groups.append(row[1])

    width = 78
    print("=" * width)
    print(" tts-server 启动前体检")
    print("=" * width)
    for g in groups:
        print(f"\n── {g} " + "─" * max(0, width - len(g) - 5))
        for level, grp, title, detail, fix in ROWS:
            if grp != g:
                continue
            print(f"  [{_ICON[level]}] {title}")
            if detail:
                print(f"      {detail}")
            if fix and level != OK:
                print(f"      → {fix}")

    print()
    print("=" * width)
    if worst == 0:
        print(" 结论: 体检通过 —— 可以启动")
    elif worst == 1:
        print(" 结论: 有告警 —— 能启动，但上面 [ ! ] 的项会让部分功能不可用")
    else:
        print(" 结论: 有阻断项 —— 现在启动 tts-server 一定失败（见上面 [ ✗ ]）")

    venv_py = (indextts_home / ".venv" / "bin" / "python") if indextts_home else None
    py = str(venv_py) if venv_py and venv_py.is_file() else sys.executable
    if worst != 2:
        print("\n 启动命令（在 tts-server 目录下执行）:")
        print(f"   cd {tts_dir}")
        print("   nohup env HF_ENDPOINT=https://hf-mirror.com \\")
        print(f"     {py} server.py \\")
        print(f"     --indextts-home {indextts_home or '<index-tts 源码目录>'} \\")
        print(f"     --model-dir {model_dir or '<模型目录>'} \\")
        print(f"     --voices-dir {voices_dir or '<音色目录>'} \\")
        print(f"     --output-dir {tts_dir}/outputs \\")
        print(f"     --device cuda:0 --fp16 --host 0.0.0.0 --port {port} \\")
        print("     > logs/tts-server.log 2>&1 &")
        print(f"\n   验证: curl -s http://127.0.0.1:{port}/api/health")
        print("         必须看到 \"model_loaded\": true（进程起来 ≠ 能用）")
    print(f"\n 耗时 {time.time() - _START:.1f}s")
    print("=" * width)
    return worst


def main() -> int:
    ap = argparse.ArgumentParser(description="tts-server 启动前体检（只读）")
    ap.add_argument("--indextts-home", default=None, help="index-tts 源码目录（含 indextts/infer_v2.py）")
    ap.add_argument("--model-dir", default=None, help="模型权重目录（含 config.yaml）")
    ap.add_argument("--voices-dir", default=None, help="参考音频目录")
    ap.add_argument("--output-dir", default=None, help="输出目录")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--deep", action="store_true",
                    help="额外真正 import indextts.infer_v2（较慢，能提前暴露缺包/版本冲突）")
    args = ap.parse_args()

    tts_dir = Path(__file__).resolve().parent
    indextts_home = find_indextts_home(args.indextts_home)
    model_dir = find_model_dir(args.model_dir, indextts_home)
    voices_dir = find_voices_dir(args.voices_dir)
    output_dir = Path(args.output_dir) if args.output_dir else tts_dir / "outputs"

    check_interpreter()
    torch_ok = check_torch(deep=args.deep)
    check_indextts(indextts_home, deep=args.deep and torch_ok)
    check_model_dir(model_dir)
    check_deps()
    check_ffmpeg()
    check_voices(voices_dir)
    check_port(args.port)
    check_disk([p for p in (model_dir, voices_dir, output_dir, tts_dir) if p])

    return render(indextts_home, model_dir, voices_dir, args.port, tts_dir)


if __name__ == "__main__":
    sys.exit(main())
