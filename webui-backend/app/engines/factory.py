"""进程级资源池工厂；旧环境变量仅转译配置，不保留旧优先级算法。

## 配置：三类服务，一种写法（2026-09-30 收敛；2026-10-08 切 YAML）

自建 tts-server、302.ai、SiliconFlow、autodl.art 在池里都是**同一条资源**，
所以只有一种写法 —— 一个列表，`provider` 决定它是什么。增减一台服务器
就是增删一条，不需要记第二套变量名、不需要改代码。

配置是 **YAML**：支持注释 ⇒ 「先停用一台、要用了再放开」把整条用 `#` 注释掉即可。
JSON 写法照样能读（JSON 是 YAML 子集），但**文件扩展名必须是 `.yaml`/`.yml`**。

三种来源（`config.resources_source()`，优先级即此顺序）：

| 形态 | 怎么配 | 加减一台服务器 |
| --- | --- | --- |
| `TTS_RESOURCES` | `.env` 里一行内联 | 改完**要重启** |
| `TTS_RESOURCES_FILE` | 指向一个 `.yaml` 文件 | **改文件即生效**（热加载，见 `build_registry`） |
| 旧式分散变量 | `TTS_URL` + `INDEXTTS302_API_KEY` 等 | 要重启 |

推荐 `TTS_RESOURCES_FILE`：模板见 `webui-backend/tts-resources.example.yaml`，
字段说明见 `ENGINES.md`。密钥用 `api_key_env` 引用环境变量名，**不写进配置**。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from dataclasses import fields
from urllib.parse import urlsplit

import yaml

from ..config import (DATA_DIR, TTS_URL, http_client, read_resources_text,
                      resource_file_path, resources_source)
from .base import EngineCapabilities, EnginePoolFacade, EngineRegistry, ResourceConfig, effective_concurrency
from .indextts_302ai import Indextts302aiEngine, DEFAULT_BASE_URL as AI302_URL
from .indextts_art import IndexttsArtEngine
from .indextts_local import IndexttsLocalEngine
from .indextts_siliconflow import IndexttsSiliconflowEngine, DEFAULT_BASE_URL as SF_URL

logger = logging.getLogger(__name__)
_REGISTRY = None
_SOURCE_SIG = None
_LOCK = threading.Lock()

_ALLOWED_FIELDS = frozenset(f.name for f in fields(ResourceConfig))

# 已删除的字段。留着它们只会让人以为「共享挂载必须先在这里声明」。
# 报错时把原因直接写在字段名后面 —— 用户看到的是一句话，不是一次翻文档。
_REMOVED_FIELDS = {
    "shared_voice_paths": "已删除（2026-09-30）：各台服务器缺哪个音色，由 voice_sync 在提交前"
                          "按需补传；共享挂载只是「省掉每台首次上传」的优化手段，不需要声明",
}

# 各 provider 惯用的凭据变量名。只用于**报错时给一个能照抄的例子**。
_DEFAULT_KEY_ENV = {
    "302ai": "INDEXTTS302_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "art": "AUTODL_API_TOKEN",
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _diagnose_config_failure(raw: str, exc: yaml.YAMLError) -> str:
    """把 YAML 的解析失败翻译成「你实际写错了什么」。

    `yaml.safe_load` 的报错格式是

        while parsing a block mapping
          in "<unicode string>", line 3, column 5
        expected <block end>, but found '-'

    —— 位置有了，但「该改成什么」没有。所以这里补上最常犯的两种错，并**点名到行**：
    C 风格注释（YAML 的注释符是 `#`）与 Tab 缩进（YAML 只允许空格）。

    诊断只看「行首是不是 `//` / `/*`」与「**缩进里**有没有 Tab」：手写注释几乎总是
    整行，而 URL（`https://…`）永远不会出现在行首 ⇒ **零误报**；Tab 只查缩进部分，
    避免把引号里合法的 Tab 也报成缩进错误。
    """
    mark = getattr(exc, "problem_mark", None)
    where = f"（第 {mark.line + 1} 行第 {mark.column + 1} 列）" if mark is not None else ""
    parts = [x for x in (getattr(exc, "context", None), getattr(exc, "problem", None)) if x]
    detail = f"：{'；'.join(parts)}" if parts else f"：{exc}"
    head = f"资源池配置不是合法 YAML{where}{detail}"
    for lineno, line in enumerate(raw.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("//", "/*")):
            return (f"{head}。第 {lineno} 行是 C 风格注释 —— YAML 的注释符是 `#`"
                    f"（想停用某台服务器，把那一整条前面逐行加 `#` 即可）")
        if "\t" in line[:len(line) - len(stripped)]:
            return (f"{head}。第 {lineno} 行的**缩进**里用了 Tab —— YAML 只允许空格缩进"
                    f"（把 Tab 换成等宽空格）")
    return (f"{head}。常见原因：缩进不一致（Tab 与空格混用、同级条目没对齐）、"
            f"`键: 值` 的冒号后少了空格、列表项的 `-` 与上一级缩进对不上")


def _looks_like_a_secret(value: str) -> bool:
    """启发式：这串东西更像**密钥本身**，而不是环境变量名。

    2026-09-30 实际踩到：`"api_key_env": "j7cyWVxW..."`（43 位随机串）。
    名称几乎总是全大写加下划线，密钥则常是长串混合大小写 —— 但对
    `AUTODL_API_TOKEN`（全大写、合法名）必须判 False，对 `my_token`
    （合法但短）也别误报。**只用于提示，不做拒绝**：环境变量名技术上允许小写。
    """
    return len(value) >= 16 and not (value.isupper() and _ENV_NAME_RE.fullmatch(value))


def _credential_hint(spec: ResourceConfig) -> str:
    """把「凭据读不到」翻译成一句能直接照着改的话。

    原来的措辞是「缺少 api_key_env（<值>）指定的凭据」—— 用户看到自己填的密钥
    出现在括号里，很容易读成「这个密钥不对」，而真正的问题是**这里根本不该填密钥**。
    """
    suggested = _DEFAULT_KEY_ENV.get(spec.provider, "")
    name = spec.api_key_env
    if not name:
        head = (f"资源 {spec.id}（provider={spec.provider}）没有 api_key_env："
                f"它要填**环境变量名**（如 {suggested or 'MY_API_KEY'}），密钥的值写在 .env 里")
    elif _looks_like_a_secret(name):
        # 只给前 4 位与长度：足够让用户认出是哪一个，又不把密钥写进日志。
        head = (f"资源 {spec.id}（provider={spec.provider}）的 api_key_env 填的是 {name[:4]}…"
                f"（{len(name)} 字符）—— 它看起来是**密钥本身**，而这里要填的是**变量名**。"
                f"例如写成 \"{suggested or 'MY_API_KEY'}\"，密钥留在 .env")
    else:
        head = (f"资源 {spec.id}（provider={spec.provider}）的 api_key_env={name} "
                f"在环境里读不到（未设置或为空）—— 在 .env 里补一行 {name}=<密钥>")
    if suggested and suggested != name and os.environ.get(suggested):
        head += f"；注：环境里已存在 {suggested}，若密钥就在那里，把 api_key_env 改成它即可"
    return head


def _make_spec(item) -> ResourceConfig:
    """把一条配置变成 ResourceConfig；错误消息必须能直接指向要改的那一行。"""
    if not isinstance(item, dict):
        raise ValueError(f"资源列表的每一项都必须是映射（`- id: ...` 或 `- {{id: ...}}`），"
                         f"收到 {type(item).__name__}")
    unknown = sorted(set(item) - _ALLOWED_FIELDS)
    if unknown:
        detail = "；".join(f"{k} —— {_REMOVED_FIELDS[k]}" if k in _REMOVED_FIELDS else k
                           for k in unknown)
        raise ValueError(
            f"资源含未知字段：{detail}。可用字段：{', '.join(sorted(_ALLOWED_FIELDS))}"
        )
    try:
        return ResourceConfig(**item)
    except TypeError as exc:   # 缺 id，或字段名对但类型不对
        raise ValueError(f"资源字段不合法：{exc}") from None


def _resource_specs() -> list[ResourceConfig]:
    raw = read_resources_text()   # 文件形态下文件缺失/扩展名不对会在这里明确报错
    if raw is not None:
        try:
            items = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError(_diagnose_config_failure(raw, exc)) from None
        if items is None:
            raise ValueError(
                "资源池配置是空的（只有注释或什么都没有）—— 至少要留一条资源。"
                "全部注释掉等于没有引擎可用，所以这里直接失败而不是给你一个空池。")
        if isinstance(items, dict):
            raise ValueError(
                f"资源池配置的顶层必须是**列表**（每行以 `- ` 开头），收到的是映射"
                f"（键：{', '.join(map(str, items)) or '无'}）。"
                f"如果它套了一层 `resources:` 这样的包装键，把那一行删掉、"
                f"让 `- id: ...` 直接顶格即可")
        if not isinstance(items, list) or not items:
            got = "空列表" if isinstance(items, list) else type(items).__name__
            raise ValueError(f"资源列表必须是非空列表（顶层每行以 `- ` 开头），收到{got}")
        specs = [_make_spec(item) for item in items]
        if len({s.id for s in specs}) != len(specs):
            raise ValueError("资源 id 重复")
        return specs

    # legacy：把旧式分散环境变量转译成同一个池（老 .env 继续可用）
    concurrency = effective_concurrency(EngineCapabilities("旧配置"))
    specs = [ResourceConfig("indextts_local", "local", base_url=TTS_URL)]
    for provider, env, url in [
        ("302ai", "INDEXTTS302_API_KEY", os.environ.get("INDEXTTS302_BASE_URL") or AI302_URL),
        ("siliconflow", "SILICONFLOW_API_KEY", os.environ.get("SILICONFLOW_BASE_URL") or SF_URL),
        ("art", "AUTODL_API_TOKEN", None),
    ]:
        if os.environ.get(env):
            specs.append(ResourceConfig("indextts_" + provider, provider, env, url, concurrency))
    if os.environ.get("TTS_ENGINE_PREFERRED"):
        logger.warning("TTS_ENGINE_PREFERRED 已失效（2026-09-29）；资源池始终本地优先、云端公平分配，该行可直接删除")
    return specs


def _create_provider(spec):
    key = os.environ.get(spec.api_key_env, "") if spec.api_key_env else ""
    if spec.base_url:
        parsed = urlsplit(spec.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 必须是无内嵌凭据、查询参数的 HTTP 地址")
    if spec.provider == "local":
        return IndexttsLocalEngine(spec.base_url or TTS_URL, http_client)
    if not key:
        raise ValueError(_credential_hint(spec))
    # 凭据轮换和 endpoint 更换也不可复用旧账号音色 URI；摘要不输出该指纹。
    digest = hashlib.sha256((spec.provider + "\0" + (spec.base_url or "") + "\0" + key).encode()).hexdigest()
    cache = DATA_DIR / "engine-cache" / spec.id / digest / "voices.json"
    if spec.provider == "302ai":
        return Indextts302aiEngine(api_key=key, base_url=spec.base_url or AI302_URL, cache_path=cache, client=http_client)
    if spec.provider == "siliconflow":
        return IndexttsSiliconflowEngine(api_key=key, base_url=spec.base_url or SF_URL, cache_path=cache, client=http_client)
    if spec.provider == "art":
        kwargs = {}
        if spec.base_url:
            root = spec.base_url.rstrip("/")
            kwargs = {"submit_url": root + "/api/v1/comfyui/comfyui_workflow/indextts2-v1",
                      "result_url": root + "/api/v1/comfyui/comfyui_workflow/result/{task_id}"}
        return IndexttsArtEngine(token=key, client=http_client, **kwargs)
    raise ValueError(f"资源 {spec.id} 的 provider 未知：{spec.provider}（可用：local/302ai/siliconflow/art）")


def _create_registry():
    specs = _resource_specs()
    # 凭据问题**一次性列全**：配了三家云、只报第一家，等于让人来回重启三次
    # （2026-09-30 实际发生）。这里不静默跳过缺凭据的资源 —— 与「配置错误直接
    # 失败，不悄悄换一套账号去合成」的原则一致。
    problems = [_credential_hint(s) for s in specs
                if s.provider != "local"
                and not (os.environ.get(s.api_key_env, "") if s.api_key_env else "")]
    if problems:
        raise ValueError("资源池凭据不完整：\n  - " + "\n  - ".join(problems))
    registry = EngineRegistry()
    registry.register_pool([(s, _create_provider(s)) for s in specs])
    return registry


def _source_signature():
    """配置源指纹。只有 `TTS_RESOURCES_FILE` 会在运行期变化，其余（环境变量）是常量。"""
    kind, _ = resources_source()
    if kind != "file":
        return (kind,)
    path = resource_file_path()
    try:
        st = path.stat()
    except OSError:
        return ("file", str(path), None, None)   # 文件被删/不可读：也算一次事件，交给重建去报错
    return ("file", str(path), st.st_mtime_ns, st.st_size)


def build_registry(force: bool = False):
    """取进程级资源池；**配置文件变了就自动重建**（热加载）。

    热加载只对 `TTS_RESOURCES_FILE` 生效：环境变量在进程里改不了，`.env` 也一样
    （启动时读一次进 `os.environ` 就固定了）。所以「上线/下线一台 tts-server 不用
    重启 backend」这件事的配法，就是把资源写进 `.yaml` 文件、用
    `TTS_RESOURCES_FILE` 指过去 —— 加一条、注释掉一条，下一次任务即可用。

    **重建失败不推翻现有池**：手滑写坏一个字符不该让整条合成链路停摆 ——
    沿用上一次的配置并打 ERROR（记下这个坏版本，避免每个请求都重试并刷日志），
    把文件改回可用内容即自动恢复。首次构建失败仍然抛出（配置错就是起不来）。
    """
    global _REGISTRY, _SOURCE_SIG
    with _LOCK:
        signature = _source_signature()
        if _REGISTRY is not None and not force and signature == _SOURCE_SIG:
            return _REGISTRY
        try:
            registry = _create_registry()
        except Exception as exc:  # noqa: BLE001 - 首次必须失败；热加载则保留旧池
            if _REGISTRY is None or force:
                raise
            _SOURCE_SIG = signature
            logger.error(
                "[engine] 资源池配置已变更但重建失败，继续沿用上一次的配置"
                "（改回可用内容即自动重试）：%s", exc)
            return _REGISTRY
        reloaded = _REGISTRY is not None
        _REGISTRY = registry
        _SOURCE_SIG = signature
        if reloaded:
            pool = registry.engines[0] if registry.engines else None
            current = ([cfg.id for cfg, _ in pool.resources]
                       if isinstance(pool, EnginePoolFacade) else [])
            logger.info("[engine] 资源池配置已热加载，当前资源：%s", current)
        return _REGISTRY


def reset_registry():
    global _REGISTRY, _SOURCE_SIG
    with _LOCK:
        _REGISTRY = None
        _SOURCE_SIG = None


def config_source() -> str:
    """当前资源池配置来自哪里（给 `/api/version` 与排障用）。"""
    kind, _ = resources_source()
    if kind == "file":
        return f"TTS_RESOURCES_FILE={resource_file_path()}（支持热加载，改文件即生效）"
    return {
        "env": "TTS_RESOURCES（内联 YAML，改动需重启）",
        "legacy": "旧式环境变量（TTS_URL + 各平台 API Key，改动需重启）",
    }[kind]


def engine_summary():
    source = config_source()
    return [{"name": e.name, "pool_schema_version": 3, "config_source": source,
             "resources": e.resource_snapshot(),
             "display_name": e.capabilities.display_name,
             "max_input_chars": e.capabilities.max_input_chars,
             "max_concurrency": e.capabilities.max_concurrency,
             "supports_speed": e.capabilities.supports_speed,
             # speed_guaranteed=True ⇒ 上层不得再变速（v3 起语速只在资源侧应用一次）
             "speed_guaranteed": e.capabilities.speed_guaranteed,
             # normalizes_loudness=True ⇒ 返回音频已归一到 -16 LUFS，上层不得再归一
             # （2026-09-30）。本地引擎 True、云引擎 False；池门面取 all()。
             "normalizes_loudness": e.capabilities.normalizes_loudness,
             "supports_emotion": e.capabilities.supports_emotion,
             "in_cooldown": False} for e in build_registry().engines]


async def refresh_pool_health() -> None:
    """触发一次池探活，让「**服务自述**能力」刷新到当前值（2026-09-30）。

    为什么需要：`normalizes_loudness` 这类能力由**资源自述**（tts-server 的
    `/api/health`）决定，适配器初始是保守值（`IndexttsLocalEngine` 默认 False），
    只有探活过才会翻成真值（见 engines/indextts_local.py:_apply_capabilities）。
    而 `engine_summary()` 是**同步**读能力快照的 —— 若在此调用前从没探过活，
    `/api/version` 报出来的是**构造时的保守值**：本地 2.0 壳明明会把响度归到
    -16（自述 True），却报 False。部署自检据此判断就会得到与事实相反的结论。

    幂等且便宜：池门面的 `_refresh_health` 自带 TTL（15s）与冷却，重复调用不会
    重复探测；内置引擎的探活都不产生合成费用（local 打 `/api/health`、302.ai
    查一个不存在的 task_id、siliconflow 列 voice 列表、autodl.art 只看 Token
    是否存在）。**任何探活失败都不影响调用方** —— 能力保持保守值即可。
    """
    for engine in build_registry().engines:
        try:
            await engine.health()
        except Exception:  # noqa: BLE001 - 探活失败只意味着能力停在保守值
            logger.debug("[engine] %s 探活失败，能力保持保守值", engine.name)
