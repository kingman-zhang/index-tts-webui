# 术语词汇表（两层结构）使用说明

> 2026-09-28 落地。存储为纯 JSON 文件（`data/glossary.json` + `data/glossary_users/`），
> 零新增依赖，可整体回滚。

## 一、为什么是两层

术语表是**逐字同音替换**机制：把 TTS 容易读错的词换成读音完全相同的字组合，
让合成读出正确读音（例：`游说 → 游睡`，因为「说」在这里读 shuì）。
它不是拼音标注，替换会真实改动送进 TTS 的文本——但**只改送进合成的那一份**
（见第六节「替换不回写任务」），任务详情与存档始终是用户原文。

| 层 | 文件 | 谁维护 | 作用范围 |
|---|---|---|---|
| **全局库** | `data/glossary.json` | 超管（CLI / 管理接口） | 所有用户，开箱即用 |
| **用户库** | `data/glossary_users/<user_id>.json` | 登录用户（前端面板） | 仅本人 |

**同名时用户库优先**：用户无需改动全局库，就能按自己的稿件覆盖或关掉某条内置词。

## 二、合并规则

以**全局库的顺序为骨架**，逐条输出：

1. 用户库有同 `original` 的条目 → 用用户的值（`source: "user"`），**位置不变**；
2. 用户库同名条目 `replacement` 为**空串** → **停用**该词，从生效词表剔除（可恢复）；
3. 全局库其余条目原样保留（`source: "global"`）；
4. 用户库独有的条目 → 追加在末尾（`source: "user"`）。

维持「全局骨架顺序」是有意为之：替换是按序 `str.replace`，顺序会影响结果，
用户覆盖不应打乱全局条目的相对次序。

## 三、接口

| 方法 | 端点 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/glossary` | 可选登录 | 生效词表 `terms`（带 `source`）+ 全局库 `global` + 我的 `mine` |
| POST | `/api/glossary` | **需登录** | 新增/覆盖我的词条；`replacement` 留空 = 停用同名的内置词 |
| PUT | `/api/glossary` | **需登录** | 全量覆盖我的词条 |
| DELETE | `/api/glossary/{original}` | **需登录** | 删除我的词条（若只是覆盖内置，删后内置重新生效） |
| POST | `/api/glossary/apply` | 可选登录 | 按当前用户视角替换文本（预览用） |
| GET | `/api/admin/glossary` | `X-Admin-Token` | 全局库原始列表 |
| POST | `/api/admin/glossary` | `X-Admin-Token` | 新增/覆盖全局条目 |
| PUT | `/api/admin/glossary` | `X-Admin-Token` | 全量覆盖全局库 |
| DELETE | `/api/admin/glossary/{original}` | `X-Admin-Token` | 删除全局条目 |

- 用户端鉴权头：`Authorization: Bearer <token>`（登录后前端自动携带）。
- 超管鉴权头：`X-Admin-Token: $MEMBER_ADMIN_TOKEN`；**未配置该环境变量时管理接口整体禁用（403）**。
- 写操作必须登录：未登录返回 401，前端提示「请先登录后再维护个人词条」。

## 四、超管 CLI（无需后端运行）

```bash
cd webui-backend

python tools/glossary_admin.py list
python tools/glossary_admin.py add --original 游说 --replacement 游睡
python tools/glossary_admin.py remove --original 游说
python tools/glossary_admin.py import --file ../tools/glossary_proposed.json   # 合并导入
python tools/glossary_admin.py import --file xxx.json --replace                # 清空后导入
python tools/glossary_admin.py export --file glossary.backup-$(date +%Y%m%d).json
```

数据目录非默认时：`python tools/glossary_admin.py --data-dir /path/to/data list`
（该参数由 `app.config` 消费）。

### 加词前必须校验读音

```bash
python tools/check_glossary_homophone.py        # 在 podcast-webui/ 下运行
```

它用 pypinyin 逐字比对**声母、韵母、声调**（轻声差异单独标注为可接受）。
不校验就直接加词，可能把正确读音改成错的——历史事故：`重头 → 从头`，
「从头」读 `cóng tóu` 而「重头」应读 `chóng tóu`，声母 c/ch 不符，
应改为 `重头 → 崇头`。

### 换词法的硬边界

目标读音**必须存在常用同音字**，否则无法用换词解决。以下读音目前无解：

`zháo(着)` `nàn(难)` `nìng(宁)` `hèng(横)` `mēng(蒙)` `pǎi(迫)` `zuàn(钻)`
`chuǎi(揣)` `děi(得)` `hàng(巷)` `mú(模)` `fà(发)` `shǎi(色)` `shě(舍)`
`mēn(闷)` `zuō(作)` `nā(南)`

其中 `zháo` 类（着急 / 着火 / 着凉 / 睡着）最高频却最无解，只能靠拼音标注或接受现状。

## 五、前端行为

术语面板（单人与双人页面均挂载）分两区：

- **我的词条**：可增删改；同名条目优先于内置，带「覆盖内置」角标。
- **内置词库**（默认折叠）：只读列表 + 搜索。每条可：
  - **覆盖** — 把内置值预填进上方表单，改完点「添加」；
  - **停用** — 写入同名空值条目，仅对自己生效；
  - 已覆盖 / 已停用的显示「恢复」，删除自己的条目即回到内置值。

未登录时输入框与操作按钮禁用，提示需登录。

## 六、替换不回写任务（2026-09-28 已实现）

术语替换发生在 `queue_worker._execute_task` 里，做法是**为合成单独生成一份文本**：

```python
synth_lines = apply_glossary(task["lines"], terms)   # app/stores.py，返回新列表
await run_mono_task(task, lines=synth_lines)         # 播客同理
```

- `task["lines"]` **保持用户原文**：队列任务详情、`GET /api/queue/{id}`、
  磁盘 `data/queue/*.json` 里都是原文，不会出现「游睡」「说福」。
- 合成层（`mono_runner` / `podcast_runner`）通过 `lines` 参数接收替换后文本，
  两者都不改写 `task["lines"]`。
- 重试扣费按**原文**字数计算（此前就地替换会让扣费按替换后字数走）。
- 同一任务重试会重新读词表再替换，不会出现二次替换叠加。

### 其它已知限制

1. **术语表仍是全局共享资源**：全局库对所有用户生效，用户只能覆盖不能隐藏他人条目。
2. `replacement` 为空在**两层里语义不同**：用户库 = 停用该词；全局库 = 替换为空串（沿用旧行为）。
3. **历史任务文件里的文本可能仍是替换后的**：本次改动前落盘的 `data/queue/*.json`
   已经被就地改过。只影响旧任务的详情显示，新任务不受影响；如需彻底干净可删除
   已完成任务文件。

## 七、数据文件
```
data/
├── glossary.json                    全局库（超管）**已纳入 git 管理**
└── glossary_users/
    └── u_xxxxxxxxxxxx.json          用户库（按 user_id 分文件，原子写入）
```

用户库文件名会过滤非法字符，杜绝路径穿越；损坏的 JSON 会退化为空表而不影响启动。

### 跨环境分发（2026-09-28 确立）

**唯一真源是 `webui-backend/data/glossary.json`**，它已列入 `.gitignore` 白名单
（`data/` 的其余内容——用户数据、产物、大资产——仍整体忽略，见仓库根 `.gitignore` 的注释）。
因此：

| 做了什么事 | 生效范围 |
|---|---|
| 本地改 `data/glossary.json`（或 CLI `add`）→ commit → push | 服务器 `git pull` 后自动生效 |
| 服务器上临时 `glossary_admin.py add` | 只在那台机器生效，且会让 git 工作区变脏 —— **不推荐** |

**约定：词表只在开发机维护，服务器只读**。需要在服务器上试词，改完请回写到开发机再 push。

**为什么**：2026-09-28 的事故——词表快照曾以 `tools/glossary_global.json` 的形式入库，
但它只是导出副本，**代码从不读它**，必须手工 `import` 才生效。用户改了那个「看得见的文件」，
而真源 `data/glossary.json` 缺失在服务器上，于是「词条配了却不生效」且毫无提示。
现在快照文件已删除，只保留一个真源，并在启动时自检：

```
[startup] 全局术语表 130 条：...          # 正常
[startup] 全局术语表缺失或为空：...        # warning，术语替换不会生效
```

排查单条词为什么不生效，用 `tools/diagnose_text.py`（在**仓库根目录**执行，
见该脚本头部说明）—— 它会打出原文的**逐字符码位**、命中了哪些词条、
以及最终送进引擎的文本。

## 八、词条 key 的码位等价展开（2026-09-28）

最常见的失败原因是**码位不匹配**：词条里的 `・`(U+30FB) 与输入里的 `·`(U+00B7)
肉眼完全一样，`str.replace` 却是精确匹配。中译外国人名的间隔号一共有
**10 个 Unicode 变体**（清单见 `app/name_punct.py:NAME_SEPARATORS`），
用户不可能每次都对上。

所以合成前会把**含分隔号的词条自动展开成全部写法**：

```
data/glossary.json 里写的一条：        9・11 → 九幺幺        （U+30FB）
合成时实际生效的 10 条：              9·11 / 9・11 / 9‧11 / 9･11 / 9•11 / …
                                       全部 → 九幺幺
```

**两个视角，别混用：**

| 函数 | 用途 | 是否展开 |
|---|---|---|
| `stores.load_glossary()` | 管理界面、前端「我的词条」、超管后台 | ❌ 逐条如实返回 |
| `stores.load_glossary_for_synthesis()` | **合成**（`queue_worker`）、**预览**（`POST /api/glossary/apply`）、`diagnose_text.py` | ✅ 展开 |

展开只在**内存**里做，不落盘、不改真源 —— 用户在界面上看到的仍是他自己写的那一条。

**边界（都是刻意的）：**

1. 只展开 `NAME_SEPARATORS` 里那 10 个**中点类**字符。半角 `-` **不展开**：
   `2020-2025` 这类范围写法比人名分隔号常见得多，自动展开风险大于收益。
2. 单条词条的变体数上限 `GLOSSARY_SEP_VARIANTS_MAX`（默认 64）。n 个中点 → 10^n，
   `A・B・C` 会算出 100 条，超限就跳过该条并打 warning（原文需与词条码位一致）。
3. **已被显式定义的 key 优先**：词表里若同时手写了 `9・11` 和 `9·11`，
   展开不会覆盖后者 —— 手写的那条说了算。
4. 用户库覆盖全局库时，**变体跟着用户的值走**（先合并、后展开）。
5. 空 `replacement`（全局库语义 = 替换为空串/删除）**不展开** ——
   否则 `9・11` 的变体会把文中所有 `9·11` 整段删掉。

**验证**（在**仓库根目录**执行；诊断脚本按设计不跟随 cwd）：

```bash
python3 tools/diagnose_text.py --env                     # 「合成生效条数」应大于「全局词条数」
python3 tools/diagnose_text.py --audit                   # 中点类条目会显示「自动覆盖 N 种写法」
cd webui-backend && python3 tests/test_glossary_sep_variants.py   # 23 项
```

## 九、测试

```bash
cd webui-backend
python tests/test_glossary_layers.py        # 存储层/合并规则/合成副本（23 项）
python tests/test_glossary_api.py           # HTTP 层鉴权与视图（30 项）
python tests/test_glossary_sep_variants.py  # 词条 key 中点变体展开（23 项）
```

两者都在临时数据目录中运行，不触碰真实 `data/`。
