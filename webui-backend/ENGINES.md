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

## 多本地音色协议限制

只配置多个 URL 不能让节点共享文件。当前 /api/synthesize 接受服务端路径，不新增上传协议。多 local 配置必须逐节点显式 shared_voice_paths=true，表示部署方保证所有节点上相同绝对路径指向同一份参考音频。未声明则配置失败；声明是部署契约，不是服务端文件存在性证明。需要独立文件系统节点时先同步/挂载音色目录，不要仅复制 URL 配置。

**当前决定（2026-09-29，用户确认）**：多本地节点先用**共享挂载**（NFS / 对象存储挂载 / 同一台机器多实例）解决音色文件；不在同一存储域时由用户自行上传同步。**音色上传接口列为后续待办**，实施前不要假设「只填 URL 就能多机共用音色」。

## 摘要与日志

engine_summary 的 pool.resources 提供 id/provider/tier/capacity/weight/inflight/in_cooldown/health/health_age_sec；health 为 unknown/reachable/unavailable/unverified。不返回 API key、凭据环境变量内容、endpoint URL 或缓存指纹。探测/隔离日志只标记资源 id 和状态，不打印异常响应正文。

## 离线回归

新增 tests/test_resource_pool.py，覆盖探测合并、TTL、离线本地跳过、恢复、权重公平、跨任务容量、忙态健康、取消、nonretryable、双 runner、异构 PCM、旧 env、缓存隔离、摘要脱敏、多节点契约和空配置；v3 增补语速专项：原生资源不叠加、非原生资源恰好补一次、播客层语速让位给资源层、atempo 链覆盖 0.25×/4×。

测试必须在 import app 前屏蔽 .env 的读取，并使用临时 DATA_DIR 和 fake 凭据；provider 测试用 httpx MockTransport，禁止运行 tools 下 live 测试。配置更新需要重启进程；不得在仍有租约时 reset_registry。
