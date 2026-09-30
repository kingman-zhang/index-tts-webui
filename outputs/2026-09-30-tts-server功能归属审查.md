# tts-server 功能归属审查（2026-09-30）

审查对象：`podcast-webui/tts-server/`（IndexTTS 2.0 HTTP 壳，端口 8000）
对照对象：`webui-backend/`（引擎适配层 + 队列 + 前处理）
分支：`feat-balance` @ `939ef1c`（未合并 main、未部署）

---

## 一、先回答「术语表是不是去掉了」

**准确说法：tts-server 这一层从来没有实现过术语表，不是「去掉了」。**

证据（三条独立）：
1. `git log -S "glossary" -- tts-server/` 只命中 `939ef1c`——那是我 2026-09-30 新加的 `doctor.py` 里的权重文件名检查，不是术语表代码。
2. `git log --diff-filter=D --name-only -- tts-server/` 无输出 ⇒ tts-server 目录**没有任何文件被删除过**。
3. 当前 `tts-server/` 全目录搜 `glossary` / `术语`：零命中（不含 `__pycache__`）。

术语表一直只在 backend：`app/stores.py`（`load_glossary_for_synthesis` / `apply_glossary`）+ `app/routes/glossary.py`（API）+ `queue_worker._execute_task` 里串进前处理链。
`重构评估与商业化MVP方案.md:20` 记录的历史链路也印证这一点：即便当年合成是在 tts-server 做的（`POST {TTS}/api/podcast`），术语替换仍然发生在 backend 的 `_process_queue` 里。

### ⚠️ 但存在第二套术语表，且会静默生效

真正需要警惕的是**引擎内置**的那一套：

| 位置 | 内容 |
|---|---|
| `index-tts-main/indextts/utils/front.py:75` | `self.term_glossary = dict()` |
| `front.py:323-343` | 按 **词条长度降序** 排序后做 `re.sub`（`re.IGNORECASE`），支持 `{"term": {"zh": ..., "en": ...}}` 双语读法 |
| `infer_v2.py:184` | `TextNormalizer(enable_glossary=True)` |
| `infer_v2.py:191-194` | **`<model_dir>/glossary.yaml` 存在就自动加载**，打 `>> Glossary loaded from:` |
| `index-tts-main/webui.py:221-227, 1112` | 官方 WebUI 有术语表管理界面 |

**这条链与我们无关，但会被我们踩到**：tts-server 创建 `IndexTTS2(cfg_path=<model_dir>/config.yaml, model_dir=<model_dir>)`，
所以只要 GPU 上 `/mnt/storage/index-tts-data/checkpoints/glossary.yaml` 存在，**引擎就会自动启用它**。

后果：
- 本地引擎链路上会有**两套术语表串联**（backend 先 `str.replace`，引擎再 `re.sub`）且顺序/规则都不同；
- backend 完全感知不到，`/api/version` 也看不到；
- 表现是「同一句话，本地引擎和云引擎读法不一样」，排查时极易误判为 backend 术语表没生效。

**处置建议**：`doctor.py` 应显式报告该文件是否存在及条目数（当前它只把它当「可选文件」计数，缺失也不报错），并把「引擎内置术语表 vs backend 术语表」写进 `ENGINES.md`。

---

## 二、tts-server 端点：活 / 死清单

backend 侧调用点已逐个核对（`grep -n "TTS_URL" webui-backend/`）。

| tts-server 端点 | backend 实际调用点 | 判定 |
|---|---|---|
| `GET /api/health` | `engines/indextts_local.py:41`（探活）、`routes/system.py:34/77`、`doctor.py` | **活（核心）** |
| `POST /api/synthesize` | `engines/indextts_local.py:74`（**合成主路径**）、`routes/voices.py:280`（音色试听） | **活（核心）** |
| `GET /api/audio/{f}` | `engines/indextts_local.py:81`（二次取回音频）、`routes/voices.py:301` | **活（核心）** |
| `GET /api/voices` | `routes/presets.py:70`、`routes/voices.py:99` | 活（音色存储） |
| `POST /api/voices/upload` | `routes/presets.py:84`、`routes/voices.py:173` | 活（音色存储） |
| `POST /api/voices/rename` | `routes/voices.py:222` | 活（音色存储） |
| `DELETE /api/voices/{f}` | `routes/voices.py:256` | 活（音色存储） |
| `POST /api/podcast` | `routes/podcast.py:24` ← `/api/podcast/generate` ← 前端 `client.ts:111 generatePodcast` → **无任何调用者**（`OutputPanel` 也没有被 import） | **死** |
| `GET /api/task/{id}` | `queue_worker.resume_polling:76`（仅历史任务恢复）、`routes/podcast.py:45/83`（死路径） | 半死 |
| `GET /api/task/{id}/audio` | `routes/podcast.py:111`（本地无产物时的历史任务回退） | 半死 |
| `GET /api/tasks` | `main.py:113`、`routes/podcast.py:136`（诊断用） | 半死 |
| `DELETE /api/task/{id}` | 无 | **死** |

结论：**tts-server 真正被依赖的只有 7 个端点**——健康、单段合成、音频取回，加上 4 个音色文件操作。
整个「播客异步任务」体系（`/api/podcast` + task 管理）在 backend 迁移到引擎适配层之后已经没有活调用方。

---

## 三、`podcast_engine.py` 功能：活 / 死 + 归属

`/api/synthesize` 内部只调用了其中一小部分（`EmotionConfig`、`GenerationParams`、`_sanitize_text`、`log_text_tokens`、`synthesize_line_with_pauses`）。

| 功能 | 实现位置 | backend 对应实现 | 判定 |
|---|---|---|---|
| 情感 4 模式 → `infer` kwargs（含 `normalize_emo_vec(apply_bias=True)`） | `EmotionConfig` | 无（backend 只传 8 维向量/标签） | **引擎侧必须留** |
| 采样参数 → `infer` kwargs | `GenerationParams` | 无 | **引擎侧必须留** |
| token 级诊断（需 `tts.tokenizer`） | `log_text_tokens` | 无 | **引擎侧必须留** |
| 模型加载 / `model_loaded` 健康位 | `server.py:95-111` | 无 | **引擎侧必须留** |
| GPU 串行推理锁 | `_model_lock` | 无（由池 `max_concurrency=1` 表达） | **引擎侧必须留** |
| 音色文件上传/改名/删除/列出 | `/api/voices*` | `routes/voices.py`、`presets.py` 代理转发 | 引擎侧**存储层**，职责清晰 |
| **年份逐位读** | `_normalize_reading_text` / `_YEAR_*` | `year_norm.py`（158 项测试，含三位数否决） | **重复，backend 版更完善** |
| **人名中点归一** | `_sanitize_text`（仅 `・`/`･`→`、`） | `name_punct.py`（10 个变体 × 4 目标） | **重复，backend 版更完善** |
| **时间 `时:分` → 中文读法** | `_replace_time` / `_TIME_PATTERN` | **无** | **引擎侧独有 → 云引擎缺这功能** |
| **变速** | `_apply_speed`（`atempo`） | `podcast_runner._normalize_segment` + 池 `atempo_filters` | **重复且行为不一致**（见四.2） |
| **响度归一 -16 LUFS** | `_apply_speed`（ebur128→固定增益→alimiter） | `podcast_runner._normalize_segment`（**6 个常量逐项相同**） | **完全重复** |
| 行内停顿 `[pause:N]` / `<#>` | `split_pauses` + `synthesize_line_with_pauses` | `mono_runner.split_by_pauses` | **重复**；backend 传的文本已切好 ⇒ tts-server 侧对 backend 路径是空转 |
| WAV 拼接 + 格式强校验 | `_concatenate_wav_segments` | `mono_runner._concat_wavs` | **重复** |
| 静音三类规则（行间/切换/行尾） | `synthesize_podcast` 组装段 | `podcast_runner._flatten_podcast_segments` | **重复**（规则已同源） |
| 播客编排（逐行→拼接→落盘） | `synthesize_podcast` | `podcast_runner.run_podcast_task` | **重复**，tts-server 侧已无活调用方 |
| 时长测量 | `get_wav_duration` | `_concat_wavs` 返回 duration | 微小重复，无害 |
| 任务管理（`TaskInfo`/`_tasks`） | `server.py:115-161,510-574` | `queue_state`（backend 自己的队列） | **两套并存** |

常量对照（`tts-server/podcast_engine.py:332-341` vs `webui-backend/app/podcast_runner.py:64-68`）：

```
NORM_TARGET_LUFS      -16.0   ←→  -16.0
NORM_CEILING_DBFS     -1.5    ←→  -1.5
NORM_LIMITER_MARGIN_DB 0.5    ←→  0.5
NORM_MAX_GAIN_DB      24.0    ←→  24.0
NORM_ABNORMAL_GAIN_DB 12.0    ←→  12.0
采样率                 24000   ←→  24000
```

这两份实现是**独立演化**的（一份操作文件路径、一份操作 bytes），已经出现分叉风险。

---

## 四、审查中发现的三个真实缺陷

### 4.1 时间读法缺失（**唯一的功能缺口，影响云引擎**）

- `12:30` → 中文读法只存在于 tts-server 的 `_replace_time`（`podcast_engine.py:181-195`，输出「十二点三十分」，冒号不会被读成「比」）。
- backend 全目录搜 `TIME_PATTERN` / `点整` / `时:分`：**零命中**。
- 前处理链（`queue_worker._execute_task:193-218`）只有 glossary → name_punct → year_norm → num_value_norm → number_norm。
- 而云引擎（302.ai / SiliconFlow / autodl.art）**不经过 tts-server**，且「不能假定云引擎 TN 会转数字」。

⇒ 走云引擎时，文本里的 `12:30` 会以阿拉伯形态进模型，读法不可控（与阿拉伯数字在 BPE 词表里是 unk 是同一类问题）。
**这条必须提到 backend**，否则它是「配了却不生效」的下一颗雷。

### 4.2 语速钳制两边不一致（本地引擎的极端语速会丢）

| 层 | 实现对超出 0.5–2.0 的语速 | 代码 |
|---|---|---|
| backend 池 | **链式 atempo**（`atempo=2.0,atempo=2.0` 支持到 4.0），不吞语速 | `engines/base.py:236-257` |
| tts-server | `speed = max(0.5, min(2.0, float(speed)))` **静默钳制** | `podcast_engine.py:384` |

因为 `indextts_local` 声明 `supports_speed=True` ⇒ 池认为「资源原生支持」⇒ **不在池内补 atempo** ⇒ 直接交给 tts-server ⇒ 被静默钳到 2.0。
同一设置：云引擎做出 3.0 倍，本地引擎只有 2.0 倍，且**没有任何日志**。

### 4.3 `_sanitize_text` 在 `/api/synthesize` 里被调用两次

`server.py:377` 先对整行算一次（结果只喂给 `log_text_tokens`），`synthesize_line_with_pauses:504` 又对子段算一次（真正喂模型）。
功能上无害（第一次仅用于日志），但**多子段时日志打印的 token 与实际进模型的不一致**，排查时会误导。
另外 tts-server 的中点替换把 `・`→`、`（顿号，会产生停顿），而 backend 默认 `NAME_PUNCT_TARGET=drop`（删除）—— 若 backend 关掉 `name_punct`，两者会给出不同韵律。

---

## 五、整改建议（按优先级）

| 优先级 | 事项 | 动作 | 风险 |
|---|---|---|---|
| **P0** | 时间 `时:分` 读法 | 在 backend 新增时间规则（建议并入 `year_norm.py` 或新建 `time_norm.py`，与现有 `MIN_DIGITS`/开关风格一致），补测试后接进 `queue_worker` 前处理链（位置：年份之后） | 低。新增功能，不改既有路径 |
| **P0** | `glossary.yaml` 隐蔽生效 | `doctor.py` 显式报告该文件是否存在 + 条目数；`ENGINES.md` 记录两套术语表的区别 | 极低，纯诊断 |
| **P1** | 响度归一重复 | 收敛到一处。**建议 backend 侧保留**（它是唯一同时管本地与云的地方），tts-server 侧保留（音色试听等直连调用仍需要）但由能力声明避免二次执行——照 `speed_guaranteed` 的思路新增 `normalizes_loudness`，播客/单人在「资源已保证」时跳过本层 ffmpeg | 中。会改音频处理路径，需真听感验收 |
| **P1** | 语速钳制不一致 | tts-server 改用与 backend 相同的链式 atempo（或至少对钳制记 warning，别静默） | 低，但改的是引擎侧代码，需重启服务 |
| **P2** | tts-server 死代码 | 删 `/api/podcast`、`/api/task*`、`DELETE /api/task/{id}`、`synthesize_podcast`、`_concatenate_wav_segments`、`split_pauses`、年份/中点规则（保留 `_replace_time` 直到 P0 落地） | 中。需先确认历史任务不依赖；建议**先加弃用日志观察一段**再删 |
| **P3** | 两套任务系统 | tts-server 只留「无状态合成」，任务状态全部归 backend | 与 P2 同步做 |

### 关于 `_sanitize_text` 的收敛方向

推荐把 tts-server 侧 `_sanitize_text` 退化为**只做引擎必需的清洗**（当前实际上什么都不必做，因为 backend 已覆盖），理由是：
- 它是**本地引擎独有**的，规则再完善也只覆盖一条链路；
- 两套规则并存会持续产生「本地对、云端错」或反之的差异。

前提是 P0 落地（时间规则提到 backend），否则删掉 tts-server 侧会丢时间读法。

---

## 六、证据索引

| 结论 | 证据位置 |
|---|---|
| tts-server 无术语表代码，也无删除记录 | `git log -S glossary -- tts-server/`；`git log --diff-filter=D --name-only -- tts-server/`（空） |
| 引擎内置术语表会静默加载 | `index-tts-main/indextts/infer_v2.py:184-194`、`indextts/utils/front.py:75,323-343,361-384` |
| backend 术语表实现 | `webui-backend/app/stores.py:157-294`、`app/routes/glossary.py`、`app/queue_worker.py:193-197` |
| 前处理链顺序 | `webui-backend/app/queue_worker.py:193-218` |
| 时间规则只在 tts-server | `tts-server/podcast_engine.py:151,172-195,198-212`；backend 搜 `TIME_PATTERN` 零命中 |
| 响度归一常量重复 | `tts-server/podcast_engine.py:332-341` vs `webui-backend/app/podcast_runner.py:64-68` |
| 语速钳制差异 | `tts-server/podcast_engine.py:384` vs `webui-backend/app/engines/base.py:236-257` |
| 播客端点已无活调用方 | 前端 `client.ts:111 generatePodcast` 无调用者；`OutputPanel` 无 import |
| tts-server 活端点 | `engines/indextts_local.py:41,74,81`、`routes/voices.py:99,173,222,256,280,301`、`routes/presets.py:70,84`、`routes/system.py:34,77` |

---

## 七、状态

- 本次为**只读审查**，未修改任何代码。
- 分支 `feat-balance`，未合并 main、未部署。
- 待用户决定是否执行 P0/P1/P2。
