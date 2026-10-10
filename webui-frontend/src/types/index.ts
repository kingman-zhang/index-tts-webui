/** 双人播客 WebUI 类型定义，与后端 API 对齐。 */

export interface EmotionConfig {
  mode: 0 | 1 | 2 | 3; // 0=跟随音色 1=参考音频 2=向量 3=文本
  audio_path: string | null;
  vector: number[]; // 8 维 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
  weight: number; // 0-1
  text: string | null;
  random: boolean;
}

export interface SilenceConfig {
  within_segment: number; // 段内分句间静音 ms
  between_lines: number; // 同说话人行间静音 ms
  speaker_switch: number; // 说话人切换静音 ms
}

export interface GenerationParams {
  speed: number; // 合成后音频速度倍率，1.0 为正常速度
  max_text_tokens_per_segment: number;
  do_sample: boolean;
  top_p: number;
  top_k: number;
  temperature: number;
  length_penalty: number;
  num_beams: number;
  repetition_penalty: number;
  max_mel_tokens: number;
  infer_concurrency: number; // GPU 模型串行推理，固定为 1
}

export interface SpeakerConfig {
  name: string;
  voice_path: string | null;
  voice_name: string | null;
  speed: number; // 角色独立语速倍率，1.0 为正常速度
  emotion: EmotionConfig; // 角色默认情感参数，新增行会继承
}

export interface PodcastLine {
  id: string;
  /** 主持人：A / B；null = 未标注（导入无前缀行或新建空行），提交前必须标注 */
  speaker: "A" | "B" | null;
  text: string;
  emotion: EmotionConfig;
  /** 可选：JSONL 行级尾部静音，未设置时使用全局静音规则。 */
  silence_after_ms?: number;
  /**
   * 标记该行情感是否来自 JSONL 代码中显式写入的值。
   * - true 表示 JSONL 中有 emotion / emotion_text 等字段，序列化时原样写回。
   * - false / undefined 表示情感继承自角色默认值，序列化时省略，除非用户在可视化中修改过。
   */
  emotion_from_code?: boolean;
  /**
   * 标记该行尾部静音是否来自 JSONL 代码中显式写入的值。
   * - true 表示 JSONL 中有 silence_after_ms 字段，序列化时原样写回。
   * - false / undefined 表示使用全局静音规则，序列化时省略。
   */
  silence_from_code?: boolean;
}

export interface PodcastProject {
  id?: string;
  name: string;
  voices: {
    A: SpeakerConfig;
    B: SpeakerConfig;
  };
  lines: PodcastLine[];
  /** 画布标记文本（新模型唯一真源）；旧项目无此字段时由 lines 迁移 */
  script?: string;
  silence: SilenceConfig;
  params: GenerationParams;
  created_at?: string;
  updated_at?: string;
}

export type VoiceSource = "custom" | "preset";

/**
 * 音色归属：
 * - `user`    = backend 本地属于当前用户的音色（`/api/voices` 里带这个标记的才是「我的音色」）
 * - `library` = tts-server 共享池里的（预置音色、BreezeBlue、历史转发上传的音色）
 *
 * 2026-10-02 起「我的音色」只认 `user`：以前把 tts-server 的列表原样并进来，
 * 于是别人的音色、共享音色也会出现在「我的音色」里。
 */
export type VoiceScope = "user" | "library";

export interface VoiceFile {
  name: string;
  path: string;
  size_kb: number;
  source?: VoiceSource;
  renameable?: boolean;
  deletable?: boolean;
  /** 归属（见 VoiceScope）；老接口不返回时视为 undefined，按「不在我的音色里」处理 */
  scope?: VoiceScope;
  /**
   * 音色 id（`voc_` + 12 位 hex），**新结构用户音色才有**。
   * 改名/删除用它当 key（文件名与显示名已解耦）；老结构音色没有，退回用 `name`。
   */
  voice_id?: string;
  /** 试听时实际请求的文件名；缺省用 name（BreezeBlue 音色的 name 是显示名，与文件名不同） */
  preview_name?: string;
  /** 预设音色补充元数据（来自 manifest.json） */
  description?: string;
  /** child / young / middle_aged / old */
  age?: string;
  /** 中文风格标签 */
  tones_zh?: string[];
  /** 真实分类 code（narration/roleplay/podcast…，与音色库一致） */
  voice_category?: string;
}

/** BreezeBlue 音色库条目（data/breezeblue/voices.json，经 /api/breezeblue/voices 返回） */
export interface BreezeblueVoice {
  id: string;
  name: string;
  description: string;
  category: string;
  category_zh: string;
  gender: "female" | "male" | "";
  gender_zh: string;
  age: "child" | "young" | "middle_aged" | "old" | "";
  age_zh: string;
  tones: string[];
  tones_zh: string[];
  audio: string;
  duration_s: number | null;
  source: "breezeblue";
  imported_at: string;
  /** 服务端音频绝对路径（作 voice_path 用） */
  path: string;
  /** 服务端音频文件名（voc_xxx.wav，作试听 key 用） */
  filename: string;
}

export interface TaskInfo {
  task_id: string;
  kind: string;
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  current_line: number;
  total_lines: number;
  message: string;
  output_filename: string | null;
  duration_sec: number;
  error: string | null;
  created_at: string;
  completed_at: string | null;
}

export const EMO_LABELS = [
  "喜", "怒", "哀", "惧", "厌", "低落", "惊喜", "平静",
] as const;

export const EMO_PRESETS: { name: string; vector: number[]; weight: number }[] = [
  { name: "平静", vector: [0, 0, 0, 0, 0, 0, 0, 0.4], weight: 0.65 },
  { name: "轻松", vector: [0.3, 0, 0, 0, 0, 0, 0.1, 0.2], weight: 0.65 },
  { name: "兴奋", vector: [0.6, 0, 0, 0, 0, 0, 0.4, 0], weight: 0.7 },
  { name: "严肃", vector: [0, 0.1, 0, 0, 0, 0.1, 0, 0.3], weight: 0.6 },
  { name: "悲伤", vector: [0, 0, 0.5, 0, 0, 0.3, 0, 0.1], weight: 0.7 },
  { name: "惊讶", vector: [0.2, 0, 0, 0, 0, 0, 0.6, 0], weight: 0.7 },
];

export function defaultEmotion(): EmotionConfig {
  return { mode: 0, audio_path: null, vector: Array(8).fill(0), weight: 0.65, text: null, random: false };
}

export function defaultParams(): GenerationParams {
  return {
    speed: 1.0,
    max_text_tokens_per_segment: 120,
    do_sample: true,
    top_p: 0.75,
    top_k: 20,
    temperature: 0.6,
    length_penalty: 0.0,
    num_beams: 2,
    repetition_penalty: 5.0,
    max_mel_tokens: 1500,
    infer_concurrency: 1,
  };
}

export function defaultSilence(): SilenceConfig {
  // 与后端 .env（PODCAST_SILENCE_*）默认保持一致；仅作前端兜底，
  // 实际生效值以后端提交时的默认（env 可调）为准。
  return { within_segment: 200, between_lines: 250, speaker_switch: 250 };
}

/** 默认项目名：未命名_YYYYMMDD（本地日期） */
export function defaultProjectName(): string {
  const d = new Date();
  const ymd =
    `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  return `未命名_${ymd}`;
}

export function defaultProject(): PodcastProject {
  return {
    name: defaultProjectName(),
    voices: {
      A: { name: "主持人A", voice_path: null, voice_name: null, speed: 1.0, emotion: defaultEmotion() },
      B: { name: "主持人B", voice_path: null, voice_name: null, speed: 1.0, emotion: defaultEmotion() },
    },
    lines: [],
    silence: defaultSilence(),
    params: defaultParams(),
  };
}

export function makeLine(speaker: "A" | "B" | null, text = "", emotion?: EmotionConfig): PodcastLine {
  return {
    id: Math.random().toString(36).slice(2, 10),
    speaker,
    text,
    emotion: emotion ? { ...emotion, vector: [...emotion.vector] } : defaultEmotion(),
  };
}

// ─── 配音模式（单音色，标记文本模型） ───────────────────────
//
// 文稿是唯一真源：一块画布文本，情绪用行首【标记】、停顿用行内 [pause:秒]。
// 提交时由 textToMonoLines 解析为后端协议的行列表。

/** 旧版逐段模型（仅用于 v1 草稿迁移） */
export interface MonoLine {
  id: string;
  text: string;
  /** null=跟随音色；统一 8 标签 + neutral（与后端引擎适配层对齐） */
  emotion_label: string | null;
  /** 段后停顿毫秒（旧模型，已由行内 [pause:N] 取代） */
  silence_after_ms?: number;
}

/** 情绪元数据：中文标签 + 预览芯片配色（value 与后端引擎适配层 8 标签对齐） */
export const MONO_EMOTION_META: Record<string, { label: string; chip: string; dot: string; scope: string }> = {
  happy:       { label: "喜悦", chip: "bg-amber-50 text-amber-700 border-amber-200", dot: "bg-amber-400", scope: "bg-amber-100/70" },
  angry:       { label: "愤怒", chip: "bg-red-50 text-red-700 border-red-200", dot: "bg-red-400", scope: "bg-red-100/70" },
  sad:         { label: "悲伤", chip: "bg-sky-50 text-sky-700 border-sky-200", dot: "bg-sky-400", scope: "bg-sky-100/70" },
  melancholic: { label: "低落", chip: "bg-indigo-50 text-indigo-700 border-indigo-200", dot: "bg-indigo-400", scope: "bg-indigo-100/70" },
  afraid:      { label: "恐惧", chip: "bg-violet-50 text-violet-700 border-violet-200", dot: "bg-violet-400", scope: "bg-violet-100/70" },
  disgusted:   { label: "厌恶", chip: "bg-lime-50 text-lime-700 border-lime-200", dot: "bg-lime-400", scope: "bg-lime-100/70" },
  surprised:   { label: "惊喜", chip: "bg-orange-50 text-orange-700 border-orange-200", dot: "bg-orange-400", scope: "bg-orange-100/70" },
  calm:        { label: "平静", chip: "bg-teal-50 text-teal-700 border-teal-200", dot: "bg-teal-400", scope: "bg-teal-100/70" },
  neutral:     { label: "中性", chip: "bg-gray-100 text-gray-600 border-gray-200", dot: "bg-gray-400", scope: "bg-gray-100" },
};

/** 行首【中文】标记 → 情绪 value */
export const MONO_EMOTION_MARKERS: Record<string, string> = Object.fromEntries(
  Object.entries(MONO_EMOTION_META).map(([value, m]) => [m.label, value])
);

/**
 * 情绪作用域终止符：标记选区作用域的结束位置。
 * 【恐惧】用来充实我们的大脑【/】，比如读书、看电影；
 * = 「用来充实我们的大脑」是恐惧，「，比如读书、看电影；」跟随音色。
 * 没有【/】时作用域延伸到下一个情绪标记或行尾（兼容旧格式）。
 */
export const MONO_SCOPE_END = "【/】";

/** 剥掉行内停顿标记 [pause:N]。 */
export function stripPauseTokens(text: string): string {
  return text.replace(/\[pause:\s*[\d.]+\s*\]/g, "");
}

/** 计费字数：剥离 [pause:N] 后的字符数。
 *
 * 停顿标记只控制合成时插多长静音、不是要念的正文，因此不计费——与后端
 * `membership/service.py: count_billable_chars()` 同一口径，改动需两边同步。
 * 情绪标记【label】/【/】已在 textToMonoLines / textToPodcastSegments 阶段
 * 剥离，不进 text，故这里无需再处理。
 */
export function billableChars(texts: string[]): number {
  return texts.reduce((n, t) => n + stripPauseTokens(t).trim().length, 0);
}

/** 预估一次合成的积分扣费，与后端 `estimate_task_cost()` 同式：
 *  `max(最低收费, ceil(字数 × 每千字单价 / 1000))`。
 *  per1000 为 0（未启用按量计费）时返回 0，表示不显示预估。 */
export function estimatePoints(chars: number, per1000: number, minCharge: number): number {
  if (per1000 <= 0 || chars <= 0) return 0;
  return Math.max(minCharge, Math.ceil((chars * per1000) / 1000));
}

// ─── 导入自动分章（与后端 app/book_split.py 同一约定）─────────
//
// 后端只在**行边界**切分，返回 text.split("\n") 的半开区间下标 [start_line, end_line)。
// 前端拿同一份 text 用同样的切法还原章节正文，因此预览与真正送进合成的文本
// 逐字符一致 —— 不要在这里引入任何「行内切分」，否则会截断行内 [pause:N]
// 与【情绪】作用域。

/** 单章字数上限默认值 —— **必须与后端 `app/book_split.py: DEFAULT_CHAPTER_MAX_CHARS` 一致**。
 *
 *  只有两个用途，都是「后端返回值到达之前」或「没有后端可问」的兜底：
 *    - 导入弹窗打开时 `chapterMax` 的初值；
 *    - 帮助文案里的「约 N 万字」。
 *  导入/重切之后一律以后端返回的 `chapter_max_chars` 为准（后端认环境变量
 *  `MONO_CHAPTER_MAX_CHARS`，前端不重复判）。此前这两处各自写死 10000 / 「1 万字」，
 *  改后端它们不会跟着变 —— 收进一个常量就是为了消掉这个重复。
 */
export const DEFAULT_CHAPTER_MAX_CHARS = 20_000;

/** 章节元数据。 */
export interface ChapterMeta {
  index: number;
  title: string;
  /** text.split("\n") 的起始下标（含） */
  start_line: number;
  /** text.split("\n") 的结束下标（不含） */
  end_line: number;
  /** 不计空白的字数（与后端 count_chars 同口径） */
  chars: number;
}

/** 书稿模式里的一章：切分确认后落成的「可编辑章节」。
 *
 *  与 ChapterMeta 的区别：ChapterMeta 是切分结果的**元数据**（行号区间，只在分段确认页用）；
 *  MonoChapter 是**内容实体**（自带正文），是书稿编辑与逐章生成的基本单位。
 *  画布文本恒等于「当前章」的 text，编辑画布即改写它。 */
export interface MonoChapter {
  /** 稳定 id（前端生成）：切章、勾选、队列对应都用它 */
  id: string;
  title: string;
  /** 该章正文（保留 [pause:N] 与【情绪】标记，与画布同格式） */
  text: string;
}

/** 生成章节 id。带随机后缀：批量导入时同一毫秒会连造几十个，纯时间戳会撞。 */
export function newChapterId(): string {
  return `ch_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

/** 把文本切成与后端一致的行数组（统一换行符）。 */
export function splitTextLines(text: string): string[] {
  return text.replace(/\r\n?/g, "\n").split("\n");
}

/** 按行号还原章节正文。 */
export function chapterTextOf(
  lines: string[],
  ch: Pick<ChapterMeta, "start_line" | "end_line">
): string {
  return lines.slice(ch.start_line, ch.end_line).join("\n");
}

/** 中文 TTS 语速粗估（字/秒）：只用于确认页显示「约 N 分钟」，不参与任何计费。 */
export const TTS_CHARS_PER_SEC = 4.5;

/** 字数 → 预估音频时长（分钟，浮点）。 */
export function estimateMinutes(chars: number): number {
  return chars / TTS_CHARS_PER_SEC / 60;
}

/** 把分钟格式化成「N 分钟」/「N 小时 M 分」。 */
export function formatMinutes(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return "0 分钟";
  if (minutes < 60) return `${Math.max(1, Math.round(minutes))} 分钟`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes - h * 60);
  return m > 0 ? `${h} 小时 ${m} 分` : `${h} 小时`;
}

/** 把字数格式化成「N 字」/「N.N 万字」（与 AccountPage 的 fmtChars 同风格）。 */
export function formatChars(n: number): string {
  if (!n || n <= 0) return "0 字";
  if (n < 10000) return `${n} 字`;
  const wan = n / 10000;
  return `${Number.isInteger(wan) ? wan : wan.toFixed(1)} 万字`;
}

export interface MonoParsedLine {
  text: string;
  /** null=跟随音色 */
  emotion_label: string | null;
}

/** 把画布文本解析为后端协议的段列表（作用域语义）。
 *
 * 情绪标记【label】是作用域起点；作用域终点 = 【/】终止符、下一个情绪标记、
 * 或行尾三者中最早出现的（无【/】的旧格式按原语义：到下一个标记/行尾）。
 * 因此一行内可以有多个情绪段（MiniMax 式），提交时按标记拆成多个合成段、段间零停顿。
 * 空行跳过；纯标记段跳过；未知【xx】按普通文本保留；【/】只作结构不进正文。
 */
export function textToMonoLines(text: string): MonoParsedLine[] {
  const out: MonoParsedLine[] = [];
  const stripEnds = (s: string) => s.split(MONO_SCOPE_END).join("");
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    // 收集结构 token：已知情绪标记（起点）与【/】（作用域终点），按位置排序
    const toks: { idx: number; end: number; kind: "emo" | "end"; emotion?: string }[] = [];
    for (const m of line.matchAll(/【[^【】]+】/g)) {
      const idx = m.index ?? 0;
      if (m[0] === MONO_SCOPE_END) {
        toks.push({ idx, end: idx + m[0].length, kind: "end" });
        continue;
      }
      const value = MONO_EMOTION_MARKERS[m[0].slice(1, -1)];
      if (value !== undefined) toks.push({ idx, end: idx + m[0].length, kind: "emo", emotion: value });
    }
    if (!toks.some(t => t.kind === "emo")) {
      const t = stripEnds(line).trim();
      if (t) out.push({ text: t, emotion_label: null });
      continue;
    }
    // 按 token 切段：token 前的文字属于当前作用域状态（无/上一个情绪）
    const push = (body: string, emotion: string | null) => {
      const t = stripEnds(body).trim();
      if (t) out.push({ text: t, emotion_label: emotion });
    };
    let curEmotion: string | null = null;
    let segStart = 0;
    const flushTo = (stop: number) => {
      push(line.slice(segStart, stop), curEmotion);
      segStart = stop;
    };
    for (const t of toks) {
      flushTo(t.idx);
      curEmotion = t.kind === "emo" ? t.emotion! : null;
      segStart = t.end;
    }
    flushTo(line.length);
  }
  return out;
}

// ─── 双人播客（标记文本模型：单人配音画布 + 主持人标识块） ─────────
//
// 画布文本是唯一真源，每行一位主持人的发言：
//   【A】今天聊点什么呢？【喜悦】我很期待 [pause:0.5]
// - 行首【A】/【B】= 主持人标识块（点击切换，不可删除，每行至多一个）
// - 【喜悦】等情绪标记与 [pause:秒] 停顿与单人配音画布同语义；
//   行内 [pause:N] 由播客引擎原生支持（子段拼接插静音），提交时原样保留在 text 里。
// 提交时由 textToPodcastSegments 解析为后端协议的行列表。

export interface PodcastParsedSegment {
  /** null = 未标注（提交前校验会拦截） */
  speaker: "A" | "B" | null;
  /** 保留行内 [pause:秒] 标记 */
  text: string;
  /** null = 跟随音色 */
  emotion_label: string | null;
  /** 同一视觉行内被情绪切分出的非末段为 0（段间零静音，覆盖全局 between_lines） */
  silence_after_ms?: number;
}

/** 情绪 value → 8 维向量下标（与后端 EMO_VECTOR_ORDER 对齐） */
const EMO_VALUE_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"];

/** 情绪标签 → 行级 EmotionConfig（mode=2 one-hot 向量；权重与 mono 引擎一致取 1.0） */
export function emotionFromLabel(label: string): EmotionConfig {
  const vector = Array(8).fill(0);
  const i = EMO_VALUE_ORDER.indexOf(label);
  if (i >= 0) vector[i] = 1.0;
  return { mode: 2, audio_path: null, vector, weight: 1.0, text: null, random: false };
}

function speakerMarkerKey(marker: string): "A" | "B" | null {
  const m = marker.match(/^【([AB])】$/);
  return m ? (m[1] as "A" | "B") : null;
}

/** 视觉行 → 段列表（作用域切分逻辑与 textToMonoLines 一致，另加主持人前缀） */
function podcastLineToSegments(raw: string): PodcastParsedSegment[] {
  const line = raw.trim();
  if (!line) return [];
  // 行首主持人标识
  let speaker: "A" | "B" | null = null;
  let body = line;
  const sp = line.match(/^【([AB])】\s*/);
  if (sp) {
    speaker = sp[1] as "A" | "B";
    body = line.slice(sp[0].length);
  }
  // 收集结构 token：情绪标记（起点）与【/】（作用域终点）；未知【xx】按普通文本
  const toks: { idx: number; end: number; kind: "emo" | "end"; emotion?: string }[] = [];
  for (const m of body.matchAll(/【[^【】]+】/g)) {
    const idx = m.index ?? 0;
    if (m[0] === MONO_SCOPE_END) {
      toks.push({ idx, end: idx + m[0].length, kind: "end" });
      continue;
    }
    const value = MONO_EMOTION_MARKERS[m[0].slice(1, -1)];
    if (value !== undefined) toks.push({ idx, end: idx + m[0].length, kind: "emo", emotion: value });
  }
  const stripEnds = (s: string) => s.split(MONO_SCOPE_END).join("");
  const segs: { text: string; emotion_label: string | null }[] = [];
  if (!toks.some(t => t.kind === "emo")) {
    const t = stripEnds(body).trim();
    if (t) segs.push({ text: t, emotion_label: null });
  } else {
    const push = (s: string, emotion: string | null) => {
      const t = stripEnds(s).trim();
      if (t) segs.push({ text: t, emotion_label: emotion });
    };
    let curEmotion: string | null = null;
    let segStart = 0;
    for (const t of toks) {
      push(body.slice(segStart, t.idx), curEmotion);
      curEmotion = t.kind === "emo" ? t.emotion! : null;
      segStart = t.end;
    }
    push(body.slice(segStart), curEmotion);
  }
  // 同一视觉行内切出的非末段：显式 0 静音（播客引擎行级 silence_after_ms 优先级最高）
  return segs.map((s, i) => ({
    speaker,
    text: s.text,
    emotion_label: s.emotion_label,
    ...(i < segs.length - 1 ? { silence_after_ms: 0 as const } : {}),
  }));
}

/** 画布文本 → 播客段列表（空行跳过；空段跳过） */
export function textToPodcastSegments(text: string): PodcastParsedSegment[] {
  const out: PodcastParsedSegment[] = [];
  for (const raw of text.split(/\r?\n/)) {
    out.push(...podcastLineToSegments(raw));
  }
  return out;
}

/** 提交前校验：返回未标注主持人的非空视觉行号（1 起） */
export function podcastScriptIssues(text: string): number[] {
  const issues: number[] = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    if (!/^【[AB]】/.test(line)) issues.push(i + 1);
  }
  return issues;
}

/** 旧版逐行项目（可视化行模型）→ 画布文本（迁移用） */
export function podcastLinesToScript(lines: PodcastLine[]): string {
  return (lines || [])
    .map(l => {
      const speaker = l.speaker ? `【${l.speaker}】` : "";
      let emo = "";
      if (l.emotion?.mode === 2) {
        const hits = (l.emotion.vector || [])
          .map((v, i) => ({ v, i }))
          .filter(x => x.v >= 0.5);
        if (hits.length === 1) {
          const value = EMO_VALUE_ORDER[hits[0].i];
          const label = Object.entries(MONO_EMOTION_META).find(([k]) => k === value)?.[1].label;
          if (label) emo = `【${label}】`;
        }
      }
      const pause =
        l.silence_after_ms && l.silence_after_ms > 0
          ? ` [pause:${String(Math.round((l.silence_after_ms / 1000) * 10) / 10)}]`
          : "";
      return speaker + emo + l.text + pause;
    })
    .join("\n");
}

/** 导入纯文本 → 画布文本：识别行首 A: / B:（含全角冒号）转主持人标识，
 *  无前缀行按对话节奏自动交替（有前缀行会重置交替状态） */
export function dialogTextToScript(raw: string): string {
  let last: "A" | "B" | null = null;
  return raw
    .split(/\r?\n/)
    .map(line => {
      const t = line.trim();
      if (!t) return line;
      const m = t.match(/^([AB])[:：]\s*/);
      if (m) {
        const k = m[1] as "A" | "B";
        last = k;
        return `【${k}】` + t.slice(m[0].length);
      }
      const k: "A" | "B" = last ? (last === "A" ? "B" : "A") : "A";
      last = k;
      return `【${k}】` + t;
    })
    .join("\n");
}

/** 旧版逐段模型 → 画布文本（v1 草稿迁移用） */
export function monoLinesToText(lines: MonoLine[]): string {
  return lines
    .map(l => {
      const meta = l.emotion_label ? MONO_EMOTION_META[l.emotion_label] : null;
      const prefix = meta ? `【${meta.label}】` : "";
      const tail =
        l.silence_after_ms && l.silence_after_ms > 0
          ? ` [pause:${String(Math.round((l.silence_after_ms / 1000) * 10) / 10)}]`
          : "";
      return prefix + l.text + tail;
    })
    .join("\n");
}
