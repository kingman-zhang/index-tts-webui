/**
 * 落地页的**对外口径与内容**单一数据源。
 *
 * 为什么单独成文件
 * --------------
 * 落地页上的每个数字（积分单价、注册赠送、音色条数、套餐价格）都会同时出现在
 * 三处：页面正文、`<head>` 的 title/description、以及 JSON-LD 结构化数据。
 * 结构化数据与可见正文不一致属于「误导性标记」，是会被搜索引擎判罚的 ——
 * 所以它们必须来自同一份常量，而不是三处手抄。
 *
 * ⚠️ 事实来源（改口径时按这个清单逐条核对，不要凭印象改）
 *   - 计费/赠送/签到   → `webui-backend/app/membership/service.py`
 *                        （线上 `.env` 配 `MEMBER_POINTS_PER_1000_CHARS=50`）
 *   - 套餐与价格       → `webui-backend/app/membership/packs.py` 的 `PACKS`
 *   - 音色条数         → `webui-backend/data/preset-voices/manifest.json` 的 `count`(65)
 *                        + `webui-backend/data/breezeblue/voices.json` 条数(310)
 *   - 情绪种类         → `webui-frontend/src/types/index.ts` 的 `MONO_EMOTION_META`
 *   - 单章上限/导入格式 → `webui-backend/app/book_split.py`、`app/routes/mono.py`
 *   - 内置纠音词条数   → `webui-backend/data/glossary.json` 条数(130)
 *   - 时长折算语速     → `src/lib/format.ts` 的 `TTS_CHARS_PER_SECOND`
 *
 * ## 关键词地图（落地页承担的主词与长尾词）
 *
 * | 词 | 意图 | 落地位置 |
 * |---|---|---|
 * | AI 配音 / AI 配音工具 / 在线 AI 配音 | 商业调查 | H1、title、FAQ §1 |
 * | 文字转语音 / 文字转语音在线生成 | 信息+工具 | H1 副标题、能力区、title |
 * | 配音软件 / 配音工具有哪些 | 商业调查 | 「适合谁用」、FAQ §1 |
 * | 有声书制作 / 小说配音怎么弄 | 信息 | 书稿模式卡、最佳实践案例 C |
 * | 播客生成 / AI 播客 / 双人播客怎么录 | 信息 | 双人播客卡、案例 B |
 * | 声音克隆 / 用我自己的声音配音 | 商业调查 | 音色能力卡、FAQ §5 |
 * | 公众号音频怎么做 | 信息（长尾） | 案例 A |
 * | 多音字读错怎么办 | 信息（长尾） | 术语表卡、FAQ §6 |
 * | 免费 AI 配音额度 | 交易 | 定价区、FAQ §3 |
 *
 * ⚠️ 只做白帽：不堆砌、不做隐藏文字、不承诺无法兑现的量（如「永久免费」「全网第一」）。
 *    搜索量级需要接百度指数 / 5118 / Ahrefs 才能量化，本文件不写臆测的搜索量数字。
 */
import { SITE_URL } from "@/lib/site";
import { fmtDuration } from "@/lib/format";
import { billableChars, estimatePoints, textToMonoLines, textToPodcastSegments } from "@/types";

// ─── 事实常量（全部可溯源，见文件头清单）─────────────────────

/** 合成单价：每千字扣多少积分（线上 `MEMBER_POINTS_PER_1000_CHARS=50`）。 */
export const POINTS_PER_1000_CHARS = 50;
/** 单次合成最低收费（积分）。 */
export const MIN_CHARGE_POINTS = 5;
/** 1 积分 = ¥0.01（与 `packs.py` 的定价锚点一致）。 */
export const YUAN_PER_POINT = 0.01;
/**
 * 注册赠送积分。
 * ⚠️ 改这个数必须同步三处：后端 `MEMBER_REG_BONUS`（`app/membership/service.py` 默认值
 * + `docker-compose.yml` 透传值 + 服务器 `.env`）。前端这一份只负责**文案口径**，
 * 它不参与任何入账 —— 数字与后端不一致时，用户会看到「说送 200、实际到账 500」这类
 * 自相矛盾的页面，所以 `outputs/verify-landing-numbers.js` 会拿它跟文案交叉校验。
 */
export const REG_BONUS_POINTS = 200;
/** 注册赠送能免费合成的字数（按单价折算；文案里出现的"约 N 字"都由它推出来）。 */
export const REG_BONUS_CHARS = Math.round(REG_BONUS_POINTS / POINTS_PER_1000_CHARS * 1000);
/** 每日签到。 */
export const CHECKIN_BONUS_POINTS = 20;
/** 音色库规模。 */
export const VOICE_PRESET_COUNT = 65;
export const VOICE_BREEZEBLUE_COUNT = 310;
export const VOICE_TOTAL_COUNT = VOICE_PRESET_COUNT + VOICE_BREEZEBLUE_COUNT;
/** 书稿模式单章上限默认值（字）。 */
export const CHAPTER_MAX_CHARS = 20000;
/** 内置纠音词条数。 */
export const GLOSSARY_BUILTIN_COUNT = 130;
/** 9 种情绪（顺序与 `MONO_EMOTION_META` 一致）。 */
export const EMOTION_LABELS = ["喜悦", "愤怒", "悲伤", "低落", "恐惧", "厌恶", "惊喜", "平静", "中性"] as const;

/** 单次合成的积分花费（与后端 `estimate_task_cost` 同一公式）。
 *  真正的计算复用 `@/types` 里编辑器在用的那两个函数，这里只是包一层默认参数。 */
export function estimateCostPoints(chars: number): number {
  return estimatePoints(chars, POINTS_PER_1000_CHARS, MIN_CHARGE_POINTS);
}

/** 多少积分折合多少元（用于「约 ¥x」的展示）。 */
export function pointsToYuan(points: number): string {
  const y = points * YUAN_PER_POINT;
  return `¥${Number(y.toFixed(2))}`;
}

/** 积分商城套餐（与 `packs.py` 的 `PACKS` 逐项对齐；`bonus` 仅供展示「多送 N」）。 */
export interface PointsPack { id: string; name: string; priceFen: number; points: number; bonus: number; note: string }
export const POINTS_PACKS: PointsPack[] = [
  { id: "p1000",  name: "体验包", priceFen: 1000,  points: 1000,  bonus: 0,    note: "先试一条完整音频" },
  { id: "p3200",  name: "标准包", priceFen: 3000,  points: 3200,  bonus: 200,  note: "做一档每周更新的栏目" },
  { id: "p5750",  name: "超值包", priceFen: 5000,  points: 5750,  bonus: 750,  note: "有声明书连载的起步量" },
  { id: "p12500", name: "尊享包", priceFen: 10000, points: 12500, bonus: 2500, note: "整本书 + 长期连载" },
];

// ─── 结构化正文 ─────────────────────────────────────────────

export interface VoiceSample { file: string; label: string; desc: string; scene: string }

/** 试听片段（取自平台自有预设音色的**参考音频**，截前 18 秒、单声道 48kbps）。 */
export const VOICE_SAMPLES: VoiceSample[] = [
  { file: "host-a.mp3",          label: "温暖女声 · 播客主持", desc: "语速平稳、亲和，适合对谈类节目", scene: "双人播客" },
  { file: "host-b.mp3",          label: "稳重男声 · 播客主持", desc: "中低音区，适合与女声搭档一问一答", scene: "双人播客" },
  { file: "narration-male.mp3",  label: "醇厚男声 · 知识讲述", desc: "低音厚实，适合读书分享与课程讲解", scene: "单人配音" },
  { file: "narration-female.mp3",label: "甜美女声 · 情感朗读", desc: "轻柔悦耳，适合情感与生活类内容", scene: "单人配音" },
];

export interface PracticeCase {
  id: string;
  tag: string;
  title: string;
  goal: string;
  workspace: string;
  deliverable: string;
  /** 文稿属于哪套语法：影响用哪个解析器算字数（与编辑器完全同一条路） */
  kind: "mono" | "podcast";
  /** 关键参数表 */
  params: [string, string][];
  steps: string[];
  /** 可直接复制进画布的文稿/脚本模板 */
  script?: string;
  scriptLabel?: string;
  /** 花费按多少字估算。缺省＝按 `script` 的实际计费字数现算。
   *  书稿案例只有一个「每 1 万字」的单价锚点，没有整篇文稿，所以显式给出基准。 */
  costBasisChars?: number;
  /** 花费基准的说明（有 `costBasisChars` 时必填，避免读者误以为「这一篇只要这点积分」） */
  costBasisNote?: string;
  /** 常见坑（来自产品文档里已被验证的边界） */
  pitfalls: string[];
}

/** 一条案例的**实际**计费字数（走编辑器同款解析器：情绪标记已剥离、[pause:N] 不计费）。 */
export function caseChars(c: PracticeCase): number {
  if (c.costBasisChars) return c.costBasisChars;
  if (!c.script) return 0;
  const texts = (c.kind === "podcast"
    ? textToPodcastSegments(c.script)
    : textToMonoLines(c.script)
  ).map(s => s.text);
  return billableChars(texts);
}

/** 一条案例的花费展示（字数 / 积分 / 折合金额 / 约合时长）。 */
export function caseCost(c: PracticeCase) {
  const chars = caseChars(c);
  const points = estimateCostPoints(chars);
  return { chars, points, yuan: pointsToYuan(points), duration: fmtDuration(chars) };
}

/**
 * 三个「照着做」案例。
 *
 * 定位：这不是客户案例（本站没有虚构用户证言），而是**新手作业**——
 * 每个案例都给出可直接复制的文稿与确定的参数，跟着做完就能拿到一条成品音频。
 * 三段文稿都是原创示例文本，用户可以原样跑通，也可以只替换内容。
 */
export const PRACTICE_CASES: PracticeCase[] = [
  {
    id: "case-reading",
    tag: "案例 A · 最简单",
    title: "读书分享音频（约 1 分钟）",
    goal: "把一段读书笔记做成能直接发公众号的音频，全程只用「单人配音」一个工作台。",
    workspace: "单人配音",
    deliverable: "一条约 1 分钟的成品音频 + 一份可复用的栏目模板",
    kind: "mono",
    params: [
      // ⚠️ 这个字数是**实测值**（与下面自动算出的成本行一致），不是估的：
      //    改文稿后请重跑核对，否则正文里会出现「250 字」却算出别的积分。
      ["文稿", "9 段，约 250 字（每段控制在 60 字内）"],
      ["音色", "音色库 → 筛「女 / 青年」→ 选一条温暖女声"],
      ["语速", "1.00"],
      ["情绪", "整体【平静】，开头与结尾各一处【喜悦】，金句前加【惊喜】"],
      ["停顿", "两处 [pause:0.5]、一处 [pause:0.8]"],
    ],
    script: `【平静】大家好，欢迎来到「小柯读书」，我是小柯。
【喜悦】今天想和你聊一本书，叫《认知觉醒》。[pause:0.5]
【平静】很多人以为，只要足够努力，就一定能变好。
【平静】但作者周岭说，大多数人的努力，只是低水平的重复。
【惊喜】真正拉开差距的，是认知。你看到的世界，决定了你能走多远。
【平静】书里有三个概念让我印象最深：大脑的三重结构、舒适区边缘，还有元认知。[pause:0.8]
【平静】简单说，就是别在舒适区里重复，也别一步跨进困难区，要站在「有点难但够得着」的地方练。
【喜悦】如果你也常常焦虑、拖延、想改变却没方向，这本书值得你认真读一遍。[pause:0.5]
【喜悦】好了，今天就聊到这里。喜欢的话，记得点个关注，我们下期再见。`,
    scriptLabel: "文稿模板（可直接复制进画布）",
    steps: [
      "先把三件事想清楚再动笔：给谁听、听完记住哪一句、最后希望他做什么。",
      "注册登录（邮箱 + 验证码），进入「单人配音」，把上面的文稿整段粘进画布。",
      "点工具条的「情绪」，逐个检查【】标签；点停顿标签可改时长（0.05~5 秒）。",
      "点「选择音色」→ 筛「女 / 青年」→ 逐个试听 → 选中一个关掉弹窗。",
      "语速保持 1.00；画布右上角会实时显示「字数 · 段数 · 约需积分」。",
      "点「生成配音」，任务进入右侧队列：排队中 → 合成中 → 完成。",
      "点 ▶ 试听三件事：有没有读错字、语气平不平、句间赶不赶；满意后点 ⤓ 下载。",
    ],
    pitfalls: [
      "一行别写太长：超过 60~120 字，合成时间会明显变长，句间停顿也会变生硬。",
      "试听按钮放的是音色的**参考音频**，不是你这次的合成结果 —— 判断像不像用它，判断效果要听成品。",
      "想让某半句单独带情绪，在结束处插一个【/】终止符。",
    ],
  },
  {
    id: "case-podcast",
    tag: "案例 B · 两个人说话",
    title: "双人播客对谈（约 1 分钟）",
    goal: "用「双人播客」做一期一问一答的对谈音频，两位主持人各自独立音色与语速。",
    workspace: "双人播客",
    deliverable: "一条双人对谈音频（A/B 两个音色自动交替、行间停顿由服务端统一处理）",
    kind: "podcast",
    params: [
      ["主持人 A", "温暖女声，语速 1.00"],
      ["主持人 B", "稳重男声，语速 0.98（略慢一点，更像真实聊天）"],
      ["默认情绪", "平静（没有单独标注情绪的行都用它）"],
      ["文稿规则", "每行开头必须标明谁在说，否则提交前会被拦下并提示行号"],
    ],
    script: `【A】哈喽大家好，欢迎收听《下班聊两句》，我是小柯。
【B】我是阿磊。今天我们聊一个很现实的话题：为什么你越努力，反而越焦虑？[pause:0.5]
【A】我先说个观察。很多人从早忙到晚，睡前回想一下，好像什么也没留下。
【B】这就是典型的「低水平勤奋」。
【A】那问题出在哪？
【B】出在方向。努力是油门，认知是方向盘。方向错了，油门踩得越狠，偏得越远。[pause:0.8]
【A】听起来有点扎心。
【B】所以书里给了三个抓手：看清大脑的三重结构、守住舒适区边缘、用元认知监督自己。
【A】我最喜欢「舒适区边缘」这个说法，别太难，也别太容易。[pause:0.5]
【B】对，难度停在「踮一踮够得着」的位置，进步最快。
【A】好，今天就聊到这儿。如果你也在这种状态里，欢迎在评论区说说你的做法。
【B】我们下期再见。`,
    scriptLabel: "对谈脚本模板（可直接复制，导入时会自动识别 A/B）",
    steps: [
      "进入「双人播客」，左栏给主持人 A、B 各选一个音色 —— 两人必须不同，否则听不出在对谈。",
      "把上面的脚本整段粘进画布，或点「导入」用 A:/B: 开头的纯文本批量贴入（没标前缀的行会 A/B 交替分配）。",
      "检查每一行是否都带了【A】或【B】：漏标的会被前后端拦截并告诉你第几行。",
      "给 A、B 分别设语速；A 略快、B 略慢，比两人同速更自然。",
      "提交前看一眼余额够不够；不足会直接标红并禁用按钮。",
      "点「生成配音」，在队列里等完成，试听确认换人节奏舒不舒服。",
    ],
    pitfalls: [
      "换人说话之间的间隔由服务端统一控制，不用手动加停顿；只想在某处停久一点时再插 [pause:1]。",
      "两位主持人的音色不要选同一个；同音色对谈会听不出谁在说话。",
      "脚本里的书名号、英文缩写如果读错，去左栏「术语词汇表」加一条词条即可（不改原文）。",
    ],
  },
  {
    id: "case-book",
    tag: "案例 C · 一整本书",
    title: "整本有声书按章连载",
    goal: "把一整本 5~10 万字的书稿交给「书稿模式」，一章一个任务，做完一章可以马上发布。",
    workspace: "单人配音 · 书稿模式",
    deliverable: "一条条按章拆好的音频（读错的那一章只重做那一章，前面不白做）",
    kind: "mono",
    costBasisChars: 10000,
    costBasisNote: "按「每 1 万字」折算，不对应某一本具体的书；整本的花费＝各章之和",
    params: [
      ["导入格式", ".doc / .docx / .pdf / .txt / .md，单文件 ≤ 20MB"],
      ["切章规则", "Markdown # 标题、第 X 章 / 第 X 回、Chapter N 都会自动识别"],
      ["单章上限", `${CHAPTER_MAX_CHARS / 10000} 万字（可调；上限只是粒度旋钮，不是容量限制）`],
      ["先做什么", "只勾第 1 章试水，听满意了再放开整本"],
    ],
    script: `第一章 大脑的三重结构
第二章 焦虑的根源
第三章 舒适区边缘
第四章 元认知：让自己看见自己
第五章 从知道到做到`,
    scriptLabel: "章节标题示例（导入文件里这样写，就会被自动切成 5 章）",
    steps: [
      "点「导入」选书稿文件（.docx / .pdf / .txt 都行）；解析完成后会自动弹出「确认分段」。",
      "这一步不能跳：核对切章结果、改错的标题、把被误切开的短章用 ⧉ 合并回上一章。",
      "调「单章上限」档位（超过上限的章会被切碎），改完点「重新切分」。",
      "不想做的章取消勾选 —— 不勾就不会花钱。先只留第 1 章，点「导入 N 章到书稿」。",
      "选一个讲述类音色（醇厚低音类），语速 0.95，比默认慢一点更适合长听。",
      "点某一章行尾的 ⚡ 单独生成这一章；确认听感 OK 后，再回目录勾选全部、批量生成。",
      "合成的产物按章下载；换章的间隙可以继续在画布上改后面的章节，互不影响。",
    ],
    pitfalls: [
      "批量提交是按顺序一章一章提交的；中途余额不足会在那一章停下并提示，已提交的章不会白做。",
      "自动切章不可能永远切对，而切错了等合成完才发现代价很高（一章几十分钟、几百积分）—— 所以确认分段这一步值得花两分钟。",
      "整本连载建议把项目存成「项目存档」：下一期只换文稿，音色、语速、情绪都不用重设。",
    ],
  },
];

/** FAQ：**页面可见正文**与 JSON-LD `FAQPage` 共用这一份（内容必须一致，否则属误导性标记）。 */
export const FAQ_ITEMS: { q: string; a: string }[] = [
  {
    q: "播客工坊是什么？和普通配音软件有什么区别？",
    a: "它是一个在浏览器里跑的 AI 配音工作台：把文稿粘进去、选一个音色、点生成，就能拿到可以直接下载的音频，不用录音、不用请人配音、不用装软件。区别主要有三点：一是按字数计费（50 积分/千字）而不是按会员月费；二是支持上传 10 秒参考音频用你自己的声音配音；三是内置「书稿模式」，能一次处理一整本书并按章交付。",
  },
  {
    q: "完全不懂技术，能用吗？",
    a: "能。整个流程只有三步：粘文稿、选音色、点生成。文稿画布只有三条规则 —— 换行代表分段、【喜悦】这类方括号标签代表情绪、[pause:0.5] 代表停顿，不写这些标签也能正常生成。另有完整的使用文档，里面有逐屏截图。",
  },
  {
    q: "要花钱吗？免费能做出多少？",
    a: `注册即送 ${REG_BONUS_POINTS} 积分，按 ${POINTS_PER_1000_CHARS} 积分/千字的单价，够免费生成约 ${REG_BONUS_CHARS} 字（约 13 分钟音频），把整条流程完整跑通。之后可以每天签到领 ${CHECKIN_BONUS_POINTS} 积分、用兑换码，或者按需购买积分包（¥10 起）。合成失败或被取消，预扣的积分会自动退回。`,
  },
  {
    q: "一条音频多久能生成出来？",
    a: "和字数、以及排在前面等待的任务都有关。一段 300 字左右的稿子通常在十几秒到一分钟之间完成；任务在队列里会显示排队中、合成中与实时进度。整本书请用书稿模式按章分批做，一次只提交你需要的几章。",
  },
  {
    q: "能用我自己的声音吗？",
    a: "可以。录一段 10 秒到 1 分钟的参考音频上传（安静环境、只有你一个人在说话、不要背景音乐），系统就能用它来合成。参考音频越干净，像的程度越高。使用他人声音做配音前，请先获得对方同意。",
  },
  {
    q: "机器读错字，多音字或专有名词读错怎么办？",
    a: "用左栏的「术语词汇表」加一条词条：填「原词」和「替换为」（写成读音正确的同音字），合成时就会临时替换。它只作用于合成副本，不会改动你的原文，画布上看到的还是你写的那句。常用多音字与译名平台已经内置好了，你不用维护；只有读错了才需要自己加一条，同名时你的替换优先。",
  },
  {
    q: "能一次处理一整本书或几万字的报告吗？",
    a: "能。导入长文后会自动进入「书稿模式」：按章节切开，一章一个任务、各扣各的积分，某一章读错只重做那一章。单章上限默认 2 万字，可以按需调档。确认分段这一步会把切章结果先摊开给你核对，确认后才开始生成。",
  },
  {
    q: "双人播客和单人配音有什么区别？我该用哪个？",
    a: "看内容里有几个人说话。双人播客负责两位主持人一问一答的对谈（A/B 各自独立音色与语速）；单人配音是一个音色从头讲到尾。读书分享、课程讲解、公众号音频、有声书用单人配音；聊天式节目、访谈、双主播播客用双人播客。两个工作区的内容互相独立，不会覆盖。",
  },
  {
    q: "生成失败或者不满意，积分会退吗？",
    a: "合成失败或被你取消，这一单预扣的积分会自动退回，可以在个人中心的「积分记录」里看到一条「退款」。注意：点 ↻ 重做属于一次新任务，会重新扣费。",
  },
  {
    q: "生成的音频存在哪里？能下载到本地吗？",
    a: "任务完成后在队列卡片上点 ⤓ 就能下载到本地，文件名就是你的项目名。音频存放在服务器上、按你的项目归属；删掉任务记录会连同磁盘上的音频一起删除，所以请及时下载留档。",
  },
];

/** 页面级 SEO 文案（构建期静态产物与运行时 head 都用这一份）。 */
export const LANDING_TITLE = "AI 配音工具 - 文字转语音在线生成，375 条音色｜播客工坊";
export const LANDING_DESCRIPTION =
  `浏览器里的 AI 配音工具：粘贴文稿、挑选音色、一键生成可下载音频。375 条音色、9 种情绪、整本书按章连载，双人播客与单人配音两套工作台。注册即送 ${REG_BONUS_POINTS} 积分，可免费生成约 ${REG_BONUS_CHARS} 字。`;
export const LANDING_KEYWORDS = [
  "AI配音", "AI配音工具", "文字转语音", "文字转语音在线", "在线配音", "配音软件",
  "有声书制作", "小说配音", "播客生成器", "AI播客", "双人播客", "声音克隆",
  "公众号音频", "读书分享配音", "TTS", "IndexTTS",
];
export const LANDING_OG_IMAGE = `${SITE_URL}/landing-assets/og-cover.jpg`;
export const LANDING_CANONICAL_PATH = "/";
