# TTS 段级资源池

## 配置与兼容

`select_engine()` 返回进程级 pool 门面；每段 `synthesize_segment()` 单独取得租约，mono/podcast 跨任务共享并发计数。仅支持当前单 backend 进程，不扩展数据库。

`TTS_RESOURCES` 为非空 JSON 列表；也可通过 `TTS_RESOURCES_FILE` 指向 JSON 文件（前者优先）。Docker 可使用现有数据卷中的 `/app/data/config/tts-resources.json`，路径与账号 key 配在挂载的 backend `.env` 中，JSON 不放密钥。摘要 `pool_schema_version=3` 标识池结构版本（v3 = 语速只在资源侧应用一次，`speed_guaranteed=true`）。

配置错误、空列表、重复 id、缺凭据直接失败，不静默退回其它账号。示例不含密钥：

```json
[
  {"id":"gpu-1","provider":"local","base_url":"http://127.0.0.1:8000","shared_voice_paths":true},
  {"id":"gpu-2","provider":"local","base_url":"http://127.0.0.1:8001","shared_voice_paths":true},
  {"id":"cloud-a","provider":"302ai","api_key_env":"TTS_ACCOUNT_A","max_concurrency":2,"weight":1},
  {"id":"cloud-b","provider":"302ai","api_key_env":"TTS_ACCOUNT_B","max_concurrency":4,"weight":2}
]
```

provider 支持 local/302ai/siliconflow/art；id 仅允许字母数字、下划线、短横线。云端 tier=cloud，本地强制 tier=local、max_concurrency=1。base_url 不允许内嵌凭据或查询参数。art 的 base_url 指服务根地址。

未配置 TTS_RESOURCES 时，TTS_URL、INDEXTTS302_API_KEY、SILICONFLOW_API_KEY、AUTODL_API_TOKEN、原 base URL/model 变量转译为同一个池；无凭据云端不注册。旧 TTS_CONCURRENCY 是每个云资源默认容量（1–8）。TTS_ENGINE_PREFERRED 弃用并告警，不允许优先级破坏本地利用和公平性。

缓存按资源 id + provider/endpoint/凭据指纹隔离，凭据轮换也不复用旧音色 URI；不自动迁移旧共享缓存，首次上传可能产生原有平台上传费用。不要把同一账号或同一物理 GPU 用多个 id 重复声明，否则无法识别隐藏的共享配额。

## 调度、探测和恢复

- 本地有空闲可用槽位优先；满载后云端溢出。
- 云端排序：inflight / capacity / weight；同负载时以累计段数 / weight 打破平局，顺序小任务不会永远命中首账号。
- 普通满载只等待 condition，不计失败、不熔断；池 health 不把忙当离线。
- 本地 health、302.ai 查询任务和 SiliconFlow 音色列表使用现有只读非合成端点。池 TTL 默认 15 秒、单资源探测任务共享，探测超时上限 12 秒。只读探测证明连通/鉴权，不证明余额足够或模型一定可用。
- art 无已确认的免费 probe，health() 返回 False；池将有 token 的资源标为 unverified，允许真实业务请求验证，不伪称健康、不额外付费试音。
- 探测失败或真实请求失败隔离该资源 120 秒；到期后下一次业务请求重新探测。没有后台探活和付费 probe。全部隔离立即报错，不忽略冷却强冲。
- 取消释放客户端租约；HTTP 取消不等于远端任务终止，所以取消中的资源也暂时隔离。远端是否真正停止依赖平台协议，不能保证。
- pool 不接受整体 mark_failed('pool')；故障只反馈到实际取得租约的资源。

## 防重复计费

池对已经进入适配器的所有异常保守包装为 NonRetryableSynthesisError，两个 runner 明确排除自动重试。提交读超时、返回 task_id 后轮询失败、取音频失败、音频转码失败，均不会重新提交另一账号。池不实施合成失败跨账号补发。

302.ai 适配器原有 ConnectError/ConnectTimeout 重试仍保留（连接未建立）；GET 轮询和原下载行为不改。直接绕过池调用适配器的代码不受池异常保护。人工重新提交仍可能重复计费，应先确认平台任务状态。

## 音频与能力

池以所有资源的最小输入上限、能力布尔交集向 runner 声明能力，总并发为槽位之和。输出统一 24000 Hz、mono、PCM16 WAV；普通整数 PCM 使用标准库解码、声道平均和线性重采样（不是高品质抗混叠重采样），其它编码需 ffmpeg。输出写规范 WAV 头，_concat_wavs 的强一致性校验继续保留。

**语速契约（v3，2026-09-29）**：`speed_guaranteed=true` 表示 `synthesize_segment()` 返回的音频已是 `req.speed` 的语速，**调用方不得再变速**。池按实际服务该段的资源决定：`supports_speed=true` 的资源把 speed 原样传给引擎；其余资源（302.ai / autodl.art 的请求体都没有 speed 字段）由 `normalize_pcm(data, speed)` 在同一次 ffmpeg 调用里用 atempo 补齐。混池下两类资源各段恰好变速一次。atempo 链由 `atempo_filters()` 统一生成（单个 atempo 只接受 0.5–2.0，超出自动串联），不再用 `min/max` 静默截断用户语速。

修掉的真 bug：播客链路曾对**原生支持语速**的引擎再套一层 atempo，实际语速 ≈ speed²（调到 1.5 听起来约 2.25 倍）。mono 路径此前对不支持原生变速的资源静默丢掉语速，现由池补齐，并在两者都不成立时打 warning。

统一容器格式不保证不同模型的音色、响度、情绪相同；语速请求值一致，但原生参数与 ffmpeg atempo 的音色细节仍有差异，跨资源混用需听感验收。

## 多本地音色与按需同步

只配置多个 URL 不能让节点共享文件。`/api/synthesize` 接受的是**服务端路径**，不是字节流。多 local 配置必须逐节点显式 `shared_voice_paths=true`，表示部署方保证所有节点上相同绝对路径指向同一份参考音频。未声明则配置失败；声明是部署契约，不是服务端文件存在性证明。

**2026-09-30 起新增兜底：backend 会在提交前按需把缺失的音色补传过去**（`app/engines/voice_sync.py`，见下节）。所以「新上线一台一个音色都没有的服务器」不再必然 400。但 `shared_voice_paths` 的语义**没有放松** —— 共享挂载仍是首选，按需同步只是补丁，且每台节点会各存一份（磁盘按节点数增长）。

**历史决定（2026-09-29，用户确认）**：多本地节点先用**共享挂载**（NFS / 对象存储挂载 / 同一台机器多实例）解决音色文件；不在同一存储域时由用户自行上传同步。

## 参考音频的按需同步（`voice_sync`，2026-09-30）

`/api/synthesize` 只认**服务器本地路径**（`server.py:357` 是 `os.path.exists(req.voice)`），于是「backend 选好的音色」与「服务器上有什么文件」是两份互不知情的状态，任何一边变动就 400。
`engines/voice_sync.py` 在**提交前**确保服务器上有这个文件，把三种路径形态（服务器路径 / backend 绝对路径 / 陈旧相对路径）在引擎层统一掉。

### 方向：为什么是 backend 推，而不是 tts-server 拉

| | 拉（tts-server → backend） | **推（backend → tts-server）** |
| --- | --- | --- |
| 前提 | backend 要有 GPU 机器可达的入口 | 无（`TTS_URL` 早就配好了） |
| 实际 | ❌ backend 常跑在开发机/内网（NAT 后），GPU 机器连不上它 | ✅ 方向本来就是出流量 |
| 新端点 | 需要一个「按名下载音频」的端点 + 鉴权 | **复用已有的 `/api/voices` 与 `POST /api/voices/upload`** |
| 新配置 | tts-server 要知道 backend 地址 | 无 |

（注：部署成 docker 时 `webui-frontend/nginx.conf` 有 `location /api/ → proxy_pass http://backend:3001`，所以 backend 是「经 web 端口可达」的 —— 但那只在 backend 有公网入口时才有意义。）

### 命名规则：只有「用户自上传」这一种需要隔离

| 类别 | 目录 | 归属 | 服务器上的文件名 |
| --- | --- | --- | --- |
| 预设音色 | `data/preset-voices/`（含 `emotions/`） | 全员共享 | **原名** |
| BreezeBlue | `data/breezeblue/audio/` | 全员共享（310 条） | **原名** |
| 用户自上传 | `data/voices/` | **用户独有** | **`{名}__{member_id}{后缀}`** |

共享音色「同名即同内容」，直接用原名；而两个用户可能有名字相同、内容不同的自定义音色 —— 用原名上传会互相覆盖（谁先传谁占坑，后传的静默改掉前者的音色）。`owner_id` 来自任务的 `member_id`，由两个 runner 填进 `VoiceRef.owner_id`。

**未知目录**（不在上面几处，也不在 `VOICE_FALLBACK_DIRS`）按**独有**处理：安全优先，宁可多占磁盘也不要串音。
拿不到 `owner_id`（隔离未开启/未登录）时退化为原名，与旧行为一致。

### 行为要点

- **预检而非兜底**：提交前查 `/api/voices`（TTL 30s 缓存），命中就直接用服务器路径 —— 正常任务零额外请求；只有 miss 才多打一次列表，且**只在首次遇到某个音色时**发生一次上传。
- **同一音色第二个段起走缓存**，不重复查表也不重复上传（端到端实测：第二段 0 额外请求）。
- **409 竞态**（提交上传的那一刻别人刚同名传完）复用服务器现有路径。
- **探不到音色表就降级**：原样返回原路径，把判断交回合成环节。探片刻失败不该在 backend 侧把任务判死（服务器可能其实有那个文件）—— 与「响度归一探测失败保持保守值」同一套思路。
- **两边都没有**时抛 `VoiceUnavailable`，消息里明确写出「服务器上不存在、backend 本地也找不到」，而不是含糊的 400。
- **回退保护**：若期望的隔离名不在、但服务器上有**原名**版本，且 backend 本地也没有文件可传，就复用原名版本 —— 避免「服务器本来跑得通、却因为命名规则变化而回归失败」。

## 两套术语表与文本规则的归属（2026-09-30）

### ⚠️ 引擎自带一份术语表，会静默生效

`index-tts` 自己有一套术语表，**与我们无关，但会被我们踩到**：

| 位置 | 内容 |
|---|---|
| `indextts/utils/front.py:75` | `self.term_glossary = dict()` |
| `front.py:323-343` | 按**词条长度降序**排序后 `re.sub`（`re.IGNORECASE`），支持 `{"term": {"zh": …, "en": …}}` |
| `infer_v2.py:184` | `TextNormalizer(enable_glossary=True)` |
| **`infer_v2.py:191-194`** | **`<model_dir>/glossary.yaml` 存在就自动加载**，打印 `>> Glossary loaded from:` |

tts-server 构造引擎时把 `--model-dir` 当作 `model_dir`，所以只要 GPU 上
`…/checkpoints/glossary.yaml` 存在，**引擎就会自动启用它**。

后果：本地引擎链路上会出现**两套术语表串联**（backend 先 `str.replace`，引擎再
`re.sub`），匹配顺序与规则都不同；backend 完全感知不到，`/api/version` 也看不到。
表现是「同一句话，本地引擎和云引擎读法不一样」，排查时极易误判为「backend 的词条
没生效」。

**处置**：`tts-server/doctor.py` 已显式报告该文件是否存在与条目数（**存在即 WARN**，
让人有据可查）。不需要它就删掉/改名 —— 那是关闭它的唯一开关。

### 文本规则：唯一落点在哪一侧

| 规则 | 落点 | 状态 |
|---|---|---|
| 术语表 | backend（`stores.py` + `routes/glossary.py`） | ✓ 用户可增删 |
| 人名中点归一 | backend `name_punct.py`（10 变体 × 4 目标） | ✓ |
| 年份逐位读 | backend `year_norm.py` | ✓ 2026-09-29 从 tts-server 提过来 |
| **时间 `时:分`** | backend `time_norm.py` | ✓ 2026-09-30 从 tts-server 提过来 |
| 数值读法 / 号码读法 | backend `num_value_norm.py` / `number_norm.py` | ✓ |
| 情感 4 模式、采样参数、token 诊断、模型加载与 health、GPU 串行锁、音色存储 | **tts-server（引擎侧）** | ✓ 这些只能是引擎的 |
| 响度归一 -16 LUFS | 2.0 壳：两侧各有一份等价实现（6 常量逐项相同）；2.5 壳：只有单遍 loudnorm、达不到目标 | ⚠️ 见下「响度归一」 |
| 变速 | 两侧（钳制范围不一致） | ⚠️ 见下「变速」 |

判据：**规则若与「读什么」有关，必须在 backend**（因为云引擎链路不经过 tts-server，
规则留在引擎侧就只有本地链路生效）；**与「怎么合成」有关的（情感、采样、GPU、
音色存储）只能留在引擎侧**。

### 响度归一（`normalizes_loudness`，2026-09-30）

**契约**：`normalizes_loudness=True` 表示「返回的音频**已归一到 -16 LUFS**」——是「达标」，
不只是「做过归一动作」。报 `False` 也不代表「本壳没做归一」，而代表「没达到 -16，请上层兜底」。

`tts-server/`（2.0）的 `/api/synthesize` 内部必经 `_apply_speed`，做的是「ebur128 测量 →
固定增益 → alimiter 限幅 → 24kHz」，目标 -16 LUFS、峰值 ≤ -1.5 dBFS —— 与 backend
`podcast_runner._normalize_segment` 是**两份独立实现、6 个常量逐项相同**，于是 2.0 自述
`True`，backend 跳过自己那一次重复归一（否则白跑一次 ffmpeg + 一次重采样）。

⚠️ **`tts-server-2.5/` 不是同一回事**：它每段会跑一次 `_apply_loudness`，对每段执行**单遍**
`loudnorm=I=-16:TP=-1.5:LRA=11`。单遍 loudnorm 的响度统计带门限（gating），实测只到
**-21.7 LUFS**（同素材，见 2.0 侧 `podcast_engine.py` 的 NORM_* 实测记录）——「动作做了、
目标没到」。所以 2.5 自述 `False`，让 backend 再走一次它那套把响度拉回 -16（这是正确行为，
不是重复劳动）。**不要用 `tier == "local"` 推断「是本壳 = 2.0 壳」。**

修法照 `speed_guaranteed` 的思路加**能力声明**，但取值改为**服务自述**：

- `EngineCapabilities.normalizes_loudness`（默认 `False`）
- `indextts_local` 的类属性保持保守 `False`，**在探活时按 `/api/health` 自述更新**
  （`_apply_capabilities`）；云引擎一律 `False`（我们不知道它做了什么）；
- 池门面 `capabilities` 是 **property**（不再在 `__init__` 里算一次），取池内资源**当前**
  能力的 `all()` —— 混池（本地 + 云端）⇒ `False` ⇒ 照旧归一。
- `podcast_runner` 在「资源已保证」时**跳过本层 ffmpeg**，不再重复归一。

**观测**：`/api/version` 的 `engines.registered[].normalizes_loudness` 报出该能力。因为它是
「服务自述」值，`/api/version` 会**先触发一次池探活**再读快照（`refresh_pool_health()`，
幂等、带 15s TTL、内置引擎探活均免费）—— 否则报的是构造时的保守值，部署自检会得到与自己
相反的结论（本地 2.0 壳明明会归一到 -16，却报 False）。

`tools/deploy_g1_autodl.sh` 只断言该**字段存在**（防旧代码），**不再断言「全 local ⇒ True」**：
本地壳有两代，2.0 自述 True、2.5 自述 False，硬断言必然有一边误报。实际值打印出来，与
tts-server 的 `/api/health` 自述核对。

### 变速（2026-09-30）

`engines/base.py:atempo_filters()` 支持**链式 atempo**（`atempo=2.0,atempo=2.0` ⇒ 到 4.0，
低到 0.25），而 tts-server 的 `_apply_speed` 是 `max(0.5, min(2.0, speed))`
**静默钳制**。因为 `indextts_local` 声明 `supports_speed=True`，池认为「资源原生支持」
⇒ 不在池内补 atempo ⇒ 直接交给 tts-server ⇒ 被钳到 2.0，**同一设置云引擎 3.0 倍、
本地引擎 2.0 倍，且无任何日志**。

已改：tts-server 侧改用同一套链式 atempo，超范围打 warning，不再静默吞语速。

## 在 GPU 服务器上启动 tts-server（实操，2026-09-30）

### 0. 先纠正认知：tts-server 不是独立服务

`tts-server` **没有自己的运行环境**，它是 index-tts 源码的 HTTP 壳。`server.py` 启动只做三件事：

1. `sys.path.insert(0, <--indextts-home>)` → `from indextts.infer_v2 import IndexTTS2`
2. 用 `<--model-dir>/config.yaml` 加载模型权重
3. uvicorn 监听 `<--host>:<--port>`

所以问题不是「要不要另找一台有 index-tts 环境的机器」，而是「**这台机器上的 index-tts 环境是否完整**」。已经能跑 IndexTTS2 的机器直接复用，不要另起一套。

### 1. 四道前置关

| # | 关卡 | 判据 | 缺了会怎样 |
| --- | --- | --- | --- |
| ① | index-tts 源码 + venv | `--indextts-home` 下有 `indextts/infer_v2.py`，且 `.venv/bin/python` 里 `import torch` 成功 | `import indextts` 直接失败 |
| ② | 模型权重 | `--model-dir` 下有 `config.yaml`，且它引用的 checkpoint 文件都在 | **进程照样起来**，`model_loaded=false` |
| ③ | 4 个额外依赖 | `fastapi` / `uvicorn[standard]` / `pydantic` / `python-multipart` 装在**同一个 venv** | 起不来（ImportError） |
| ④ | 参考音频目录 | `--voices-dir` 里有 backend 会用到的音色，**路径与 backend 侧一致** | 合成 400 参考音频不存在（2026-09-30 起 `voice_sync` 会在首次合成时按需补传，但预置仍能省掉首次等待） |

第 ③ 关安装（venv 必须是第 ① 关那个）：

```bash
/root/index-tts/.venv/bin/pip install -r tts-server/requirements.txt
```

### 2. 上机前先体检

`tts-server/doctor.py`（**只读**，不装东西不改文件）把上面四关 + CUDA/ffmpeg/端口/显存/磁盘一次查完，并把结论翻译成下一条命令：

```bash
# 用 index-tts 的 venv 跑（推荐，才能查到 torch/CUDA）
/root/index-tts/.venv/bin/python doctor.py

# 路径自动探测不准时手动指定
/root/index-tts/.venv/bin/python doctor.py \
  --indextts-home /root/index-tts \
  --model-dir /mnt/storage/index-tts-data/checkpoints \
  --voices-dir /mnt/storage/index-tts-data/voices --port 8000
```

退出码 `0` = 可启动，`1` = 有告警，`2` = 有阻断。加 `--deep` 会真正 `import indextts.infer_v2`（慢，但能提前暴露缺包/版本冲突）。

### 3. 启动

```bash
cd <tts-server 目录>
mkdir -p logs
nohup env HF_ENDPOINT=https://hf-mirror.com \
  /root/index-tts/.venv/bin/python server.py \
  --indextts-home /root/index-tts \
  --model-dir /mnt/storage/index-tts-data/checkpoints \
  --voices-dir /mnt/storage/index-tts-data/voices \
  --output-dir /mnt/storage/index-tts-data/outputs \
  --device cuda:0 --fp16 --host 0.0.0.0 --port 8000 \
  > logs/tts-server.log 2>&1 &
```

仓库里也有现成脚本（改开头 4 个路径后 `bash start.sh`；AutoDL 版是 `start_autodl.sh`）。注意 `start_autodl.sh` 里用的是**裸 `python`** —— 没有激活 venv 时它会用系统 python，然后 import torch 失败；用 `doctor.py` 确认解释器那项，或显式改用 venv 绝对路径。

验证（**这一步不能只看端口通**）：

```bash
curl -s http://127.0.0.1:8000/api/health
# 必须看到 "model_loaded": true —— 进程起来 ≠ 能用
```

### 4. 七个真坑（都有代码依据）

1. **模型加载失败不会让进程退出**。`server.py:108-111` 捕获异常后把 `tts` 置 None，服务照常监听、`/api/health` 照常 200，只是 `status="no_model"`。所以「端口通了」是伪验收，必须看 `model_loaded`。
2. **必须用 venv 的解释器**。`python server.py` 若指向系统 python，`import torch` / `import indextts` 会失败。
3. **cwd 决定 HF 缓存位置**。老版 v2.0.0 的 `indextts/infer_v2.py` 头几行硬写 `os.environ['HF_HUB_CACHE'] = './checkpoints/hf_cache'` —— **相对路径 + 直接赋值**：会覆盖你 export 的 `HF_HOME`，且跟着启动时的 cwd 走。所以要么在 tts-server 目录下启动并把 `checkpoints/hf_cache` 软链到数据盘（`start_autodl.sh` 的做法），要么用 `tools/prefetch_aux_models.py --cache <该目录>` 预下载。放着不管会把几 GB 辅助模型下到系统盘。
4. **首次启动要联网拉 4 个辅助模型**（w2v-bert-2.0 ~2.3GB、MaskGCT semantic codec、CAMPPlus、BigVGAN）。国内设 `HF_ENDPOINT=https://hf-mirror.com`；下齐后可 `HF_HUB_OFFLINE=1` 让启动秒过。别把「静默卡住」当成在加载模型。
5. **ffmpeg 是双人播客的硬依赖**。`podcast_engine._apply_speed` 里 `shutil.which("ffmpeg")` 找不到直接 `RuntimeError`，整个 `/api/podcast` 任务失败；单段 `/api/synthesize` 不需要。
6. **端口**：2.0 = 8000（`tts-server/`），2.5 = 8001（`tts-server-2.5/`）。两版同时跑会占两份显存。
7. **服务零鉴权**。见下一节的网络白名单要求 —— 别因为「终于跑起来了」就直接暴露到公网。

### 5. 端到端顺序

1. 服务器上跑 `doctor.py` → 消掉所有 `[ ✗ ]`
2. 启动 tts-server → `curl /api/health` 确认 `model_loaded: true`
3. backend 侧改 `TTS_URL` + `TTS_RESOURCES`（见下一节）→ 重启 backend
4. backend 侧跑 `tools/check_tts_endpoint.py --probe-synth` → 确认两类检查都过

## 接入一台远程 tts-server（实操，2026-09-29）

tts-server 默认 `--host 0.0.0.0`，本身就是 HTTP API —— 所以「公开接口」不缺口子，缺的是网络通道、池配置，以及**音色路径对齐**。

### 1. 网络：不要让 8000 裸奔到公网

tts-server **没有任何鉴权**（无 API Key，CORS `allow_origins=["*"]`），且带破坏性端点：`POST /api/voices/upload`（写文件）、`DELETE /api/voices/{name}`（删音色）、`DELETE /api/task/{id}`（删任务），外加吃 GPU 的 `POST /api/synthesize`。只给自家 backend 用时，**用防火墙白名单把它变成事实上的私网接口**：

- 云安全组：只放行 backend 机器出口 IP → TCP 8000（取出口 IP：backend 机器上 `curl -s https://ifconfig.me`；容器内执行同样走宿主机 NAT）
- 服务器系统防火墙兜底：`ufw allow from <BACKEND_IP> to any port 8000 proto tcp` 后 `ufw deny 8000/tcp`（规则顺序：allow 必须在 deny 之前）
- backend 与 tts-server 同云时优先走内网 IP：免费、更快、完全不暴露

端口：2.0 = 8000（`tts-server/`），2.5 = 8001（`tts-server-2.5/`），HTTP 契约相同，适配器与配置格式都不用变。

### 2. 配置：`TTS_URL` 与 `TTS_RESOURCES` 必须一起改

**只改 TTS_RESOURCES 不够**（容易漏）。两条是独立路径：

| 用途 | 走哪个配置 |
| --- | --- |
| 合成（池调度） | `TTS_RESOURCES` 的 local 条目 |
| 预设音色上传 `/api/preset-voices/upload-to-tts` | `TTS_URL` |
| `/api/tts/health` 探针 | `TTS_URL` |

前端选**预设音色**时，backend 会把文件上传到 `TTS_URL` 那台，并把**服务器返回的路径**存进任务（`SpeakerPanel.tsx:124` / `MonoVoiceCard.tsx:75`）。所以 `TTS_URL` 仍指旧地址时，音色会被传到旧机器，新机器上一个都没有 —— 表现为「网络通了、资源池也进去了，一合成就 400 参考音频不存在」。

```
TTS_URL=http://<TTS_HOST>:8000
TTS_RESOURCES=[{"id":"gpu-1","provider":"local","base_url":"http://<TTS_HOST>:8000","shared_voice_paths":true}]
```

**优先级陷阱（2026-09-30 实际踩到）**：`TTS_URL` 有四个来源，从左到右覆盖 ——
`--tts-url` 命令行 > 真实环境变量 > **`webui-backend/.env`** > 内置默认 `http://localhost:8000`
（`app/config.py:52`）。所以**只要启动器传了 `--tts-url`，`.env` 就彻底不生效**。
当时 `webui-backend/start.sh` 写死了 `--tts-url http://localhost:8000`，症状是
「`.env` 明明改了地址，`/api/tts/health` 却永远回 `无法连接 TTS 服务: http://localhost:8000`、
`/api/version` 里本地资源恒 `unavailable`」（云引擎照常可用，很容易误判成网络问题）。
`start.sh` 已改为不传该参数。**排查口诀：配置改了不生效，先 `pgrep -fl server.py` 看进程 cmdline 有没有被传参覆盖。**

`TTS_RESOURCES` 在进程启动时读取，改完 `docker compose restart backend`。若 backend 所在机器设了 `HTTP_PROXY`，httpx 默认 `trust_env` 会把该地址也丢给代理（探活会返回代理的 502/`upstream connect failed`）—— 给 TTS 地址配 `NO_PROXY`。

### 3. 音色对齐：自检 + 按需同步

`engines/indextts_local.py` 提交前会调 `voice_sync.ensure_voice_on_server()`，把 `VoiceRef` 里的路径换成**服务器自报的绝对路径**（缺文件就按需上传）。所以「任务里存的是什么形态的路径」不再决定成败：

| 来源 | 服务器上不存在时（2026-09-30 前 → 后） |
| --- | --- |
| 预设音色（65 个） | 选中时已自动上传 → 没传过也会由 `voice_sync` 补上 |
| 自上传音色（`data/voices/`） | **合成 400** → 按 `{名}__{member_id}` 上传 |
| BreezeBlue（310 个） | **合成 400** → 按原名上传 |

**仍会失败的情况只有两种**：
1. backend 本地也没有该文件（音色被删）且服务器上也没有 ⇒ 抛 `VoiceUnavailable`，消息里写明两边都缺；
2. `voice_sync` 连不上服务器的 `/api/voices` **且**服务器确实缺文件 ⇒ 降级为旧行为，交给合成报 400。

⚠️ 400 仍发生在**提交之后**，池会把进适配器的异常包成 NonRetryable（防重复计费），两个 runner 都不自动重试 —— **不会改投云端**，用户看到的是整个任务失败。所以别把「按需同步」当成「音色不用管了」。

接入前先跑只读自检（不上传、不改配置）：

```bash
python tools/check_tts_endpoint.py --tts-url http://<TTS_HOST>:8000
python tools/check_tts_endpoint.py --tts-url http://<TTS_HOST>:8000 --probe-synth
```

依次检查 ①连通性与 `model_loaded` ②服务器已有音色 ③本地三类音色里哪些服务器上没有 ④可选端到端合成。退出码 0 = 无阻断项，1 = 有告警，2 = 连不上。有了按需同步后，③ 的缺项会在首次合成时自动补；主动预置（共享挂载 / rsync）仍能省掉首次上传的等待。

## 摘要与日志

engine_summary 的 pool.resources 提供 id/provider/tier/capacity/weight/inflight/in_cooldown/health/health_age_sec；health 为 unknown/reachable/unavailable/unverified。不返回 API key、凭据环境变量内容、endpoint URL 或缓存指纹。探测/隔离日志只标记资源 id 和状态，不打印异常响应正文。

## 离线回归

新增 tests/test_resource_pool.py，覆盖探测合并、TTL、离线本地跳过、恢复、权重公平、跨任务容量、忙态健康、取消、nonretryable、双 runner、异构 PCM、旧 env、缓存隔离、摘要脱敏、多节点契约和空配置；v3 增补语速专项：原生资源不叠加、非原生资源恰好补一次、播客层语速让位给资源层、atempo 链覆盖 0.25×/4×。

测试必须在 import app 前屏蔽 .env 的读取，并使用临时 DATA_DIR 和 fake 凭据；provider 测试用 httpx MockTransport，禁止运行 tools 下 live 测试。配置更新需要重启进程；不得在仍有租约时 reset_registry。

`tests/test_voice_sync.py`（41 项）专门覆盖按需音色同步：目录分类、命名规则（含「已带后缀不叠加」与 owner 净化）、音色表 TTL 缓存、以及预检的六条分支（命中 / 本地有则上传 / 两边都缺则明确报错 / 探不到表则降级 / 409 竞态 / 隔离名缺失回退原名）。它把模块用的音色目录换成临时目录，用假 client，**不触网、不依赖 tts-server**。
