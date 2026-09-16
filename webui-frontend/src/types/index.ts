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
  speaker: "A" | "B";
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
  silence: SilenceConfig;
  params: GenerationParams;
  created_at?: string;
  updated_at?: string;
}

export type VoiceSource = "custom" | "preset";

export interface VoiceFile {
  name: string;
  path: string;
  size_kb: number;
  source?: VoiceSource;
  renameable?: boolean;
  deletable?: boolean;
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
  return { within_segment: 200, between_lines: 300, speaker_switch: 500 };
}

export function defaultProject(): PodcastProject {
  return {
    name: "未命名播客",
    voices: {
      A: { name: "主持人A", voice_path: null, voice_name: null, speed: 1.0, emotion: defaultEmotion() },
      B: { name: "主持人B", voice_path: null, voice_name: null, speed: 1.0, emotion: defaultEmotion() },
    },
    lines: [],
    silence: defaultSilence(),
    params: defaultParams(),
  };
}

export function makeLine(speaker: "A" | "B", text = "", emotion?: EmotionConfig): PodcastLine {
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

export interface MonoParsedLine {
  text: string;
  /** null=跟随音色 */
  emotion_label: string | null;
}

/** 把画布文本解析为后端协议的段列表（作用域语义）。
 *
 * 情绪标记【label】是该作用域的起点：从标记到下一个标记或行尾的文字都使用该情绪。
 * 因此一行内可以有多个情绪段（MiniMax 式），提交时按标记拆成多个合成段、段间零停顿。
 * 空行跳过；纯标记段跳过；未知【xx】按普通文本保留。
 */
export function textToMonoLines(text: string): MonoParsedLine[] {
  const out: MonoParsedLine[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    // 找出本行所有已知情绪标记的起点
    const starts: { idx: number; end: number; emotion: string }[] = [];
    for (const m of line.matchAll(/【[^【】]+】/g)) {
      const value = MONO_EMOTION_MARKERS[m[0].slice(1, -1)];
      if (value !== undefined) starts.push({ idx: m.index ?? 0, end: (m.index ?? 0) + m[0].length, emotion: value });
    }
    if (starts.length === 0) {
      out.push({ text: line, emotion_label: null });
      continue;
    }
    // 段：行首→第一个标记（无情绪）；每个标记→下一个标记/行尾
    const push = (body: string, emotion: string | null) => {
      const t = body.trim();
      if (t) out.push({ text: t, emotion_label: emotion });
    };
    push(line.slice(0, starts[0].idx), null);
    for (let i = 0; i < starts.length; i++) {
      const end = i + 1 < starts.length ? starts[i + 1].idx : line.length;
      push(line.slice(starts[i].end, end), starts[i].emotion);
    }
  }
  return out;
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
