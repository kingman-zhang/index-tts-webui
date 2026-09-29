# 引擎层：统一接口 + 能力声明（2026-09-29）

## 一句话

上层**不判断引擎名**（不区分云引擎还是本地服务器），只读引擎自己声明的
`capabilities`，通过同一个 `synthesize_segment()` 拿音频字节。

```
任务执行层（切片 · 并发 · 重试 · 拼接）
    │  只读 engine.capabilities
    ▼
选择策略 select_engine()          ← 下一轮换负载均衡只改这里
    ▼
统一接口 synthesize_segment()      ← 全引擎同一签名，返回 bytes
    ▼
四个适配器（鉴权/轮询/转码/音色注册全部内部消化）
    ▼
底层同一件事：IndexTTS-2 合成服务
```

## 为什么改

改造前有四个具体问题，每一个都有代码证据：

| # | 问题 | 证据 | 后果 |
|---|---|---|---|
| 1 | 引擎能力靠**引擎名硬编码**泄漏到上层 | `mono_runner.py` / `podcast_runner.py` 里的 `if engine_name == "indextts_art"` | 302.ai 单次上限 2000 字、SiliconFlow 上限 2048 字，两者超限直接 `raise ValueError("请走 chunker")`，而上层只给 art 切片 ⇒ **同一段长文本，art 成功、302.ai 必失败** |
| 2 | 四个引擎**探活语义各不相同** | `indextts_art.health()` 只 `return bool(self.token)` | art 只要 Token 存在就"健康"。**余额耗尽（HTTP 403）照样被选中**，然后每一段都失败 |
| 3 | **故障切换是死代码** | `base.py` 的 `EngineRegistry.synthesize()` 写着"主引擎失败自动尝试下一个"，但两个 runner 都是 `resolve()` 后直接调 `engine.synthesize_segment()` | 承诺的降级从未发生。已删除该函数 |
| 4 | 每个任务重建注册表 | 两个 runner 各调一次 `build_registry()` | health TTL 与音色解析缓存全是实例级的，一重建就作废 ⇒ 每任务重复探活 |

另有一处结构性问题：同一后端里并存**两套执行路径** —— `mono`/`podcast` 走 backend
适配层，其余 kind 走兜底分支 `POST {TTS_URL}/api/podcast` 交给本地 tts-server。
云端引擎部署下后者必然失败，且绕过了能力声明、探活与熔断。**已删除**，未知 kind
现在直接报错。

## 能力矩阵

| 引擎 | 单次上限 | 并发 | 语速 | 情绪 | 备注 |
|---|---|---|---|---|---|
| `indextts_local`（自建 tts-server） | 不限 | 1 | ✓ | ✓ | 整段交模型侧分段；GPU 实例串行 |
| `indextts_302ai` | 2000 | env | ✗ | ✓ | 平台请求体无 speed 参数，传了无效 |
| `indextts_siliconflow` | 2048 | env | ✓ | 按模型 | CosyVoice2 支持（内联提示词）；MOSS-TTSD 无情绪控制 |
| `indextts_art`（autodl.art） | 2048 | env | ✗ | ✓ | `emo_surprised` 被平台锁死为 `"0"`，该标签降级为跟随音色 |

- **并发** `env` = 未声明，读 `TTS_CONCURRENCY`（默认 3，钳 1-8）。
- **上限口径**：`chunker._char_count` 的计费口径（1 汉字 = 2 字符）。
  302.ai 的实际校验是 `len(text)`（Python 字符数），用 2000 作上限会更保守
  —— 方向安全，只是片数偏多。

代码位置：`app/engines/base.py:EngineCapabilities` + 各适配器的类属性。

## 加一个新引擎

只要动一个文件（新适配器），上层一行不用改：

```python
class IndexttsFooEngine:
    name = "indextts_foo"
    capabilities = EngineCapabilities(
        display_name="Foo 托管 IndexTTS-2",
        max_input_chars=2000,      # None = 不限
        max_concurrency=None,      # None = 读 TTS_CONCURRENCY
        supports_speed=False,
        supports_emotion=True,
    )

    async def health(self) -> bool: ...
    async def synthesize_segment(self, req: SegmentRequest) -> bytes: ...
```

然后在 `app/engines/factory.py:_create_registry()` 里 `registry.register(...)`。

**唯一的硬约定**：`synthesize_segment` 必须返回 **bytes**。各家"提交任务 → 轮询 →
下载 URL"的差异不许泄漏成调用方可见的第二种返回形态（自建 tts-server 的
`/api/synthesize` 返回 JSON `output_filename`、需要二次 GET，这段就由
`IndexttsLocalEngine` 内部消化）。

## 注册、选择与熔断

- **注册表是进程级单例**（`factory.build_registry()`）。理由有二：熔断状态必须
  跨任务存活；health TTL 与音色缓存不能每任务作废。改环境变量后需
  `reset_registry()`（测试里尤其注意）。
- **注册顺序即优先级**：自建 → 302.ai → SiliconFlow → autodl.art，有 Key/Token
  才注册。`TTS_ENGINE_PREFERRED` 把指定引擎提到最前。
- **降级发生在选择阶段**（`resolve()` 逐个探活），不在合成阶段。任务内**刻意锁定
  单一引擎** —— 切片策略与采样率都取决于引擎，中途换引擎会产出参数不一致的音频。
- **熔断**：合成阶段重试后仍失败的段，若「全部失败」或「3 段以上且过半失败」，
  则把该引擎置入 120 秒冷却，后续任务在选择阶段跳过它。
  判据刻意收窄 —— 单段失败更可能是该段文本自身的问题，误伤整个引擎 120 秒
  比漏判更贵。
  全部引擎都在冷却时**忽略冷却**按优先级重试（半开），避免一个引擎故障就让服务
  停止接单。

## 凭据与 `.env`（两个踩过的坑）

- **显式传值优先**：三个带凭据的适配器统一写成
  `self.api_key = api_key if api_key is not None else os.environ.get(...)`。
  原写法 `api_key or os.environ.get(...)` 会让**显式传入的 `""`** 被环境变量顶掉
  ⇒ 无法在测试/调试里关掉 Key（`api_key=""` 仍会发真实请求）。
  语义定为：`None` = 读环境变量，`""` = 明确不要凭据。三个文件：
  `indextts_302ai.py` / `indextts_siliconflow.py` / `indextts_art.py`。
- **`import app.engines` 会连带加载 `.env`**：`engines/__init__.py` →
  `factory` → `selector` → `config`，而 `config` 在 import 时就把 `.env` 灌进
  `os.environ`。所以测试里**必须显式传凭据**（哪怕是 `""`），不能指望"没传就是没 Key"
  —— 开发机 `.env` 里有真 Key 时，行为会跟 CI 不一样。

## 可观测性

`GET /api/version` 的 `engines` 字段与启动日志的 `[startup] 引擎优先级 ...` 一行，
一起回答"这次为什么走了这个引擎"：

```
[startup] 引擎优先级 indextts_art(上限2048/并发env) → indextts_local(上限不限/并发1)
```

`engines.registered[]` 每项含 `name / display_name / max_input_chars /
max_concurrency / supports_speed / supports_emotion / in_cooldown`。
`engines` 字段本身也是版本指纹 —— 出现即证明进程加载的是含能力声明的新代码。

## 下一轮：负载均衡

现在 `TTS_ENGINE_PREFERRED` 的语义是**优先级**（把某个引擎提到最前），不是
**可用性分发**。要改成"哪个可用就调哪个"，只需替换
`app/engines/selector.py:select_engine()` 的排序依据，适配器与上层都不用动。
熔断状态（`registry.cooldown_until`）已经是分发可用的输入之一。

## 遗留：`resume_polling`

`queue_worker.resume_polling()` 仍在轮询**历史遗留**的 tts-server 任务
（`task["tts_task_id"]` 存在时）。删掉兜底提交路径后，新任务不会再产生
`tts_task_id`，所以这段只服务于历史任务（backend 重启后把它们追完）。
等旧任务清空即可下线。

## 测试

```bash
cd webui-backend
python tests/test_engine_layer.py      # 32 项：能力声明、能力驱动并发、选择降级、熔断半开、单例、未知 kind
python tests/test_mono_concurrency.py  # 含「按能力切片」用例
python tests/test_registry_order.py    # 优先级重排
```
