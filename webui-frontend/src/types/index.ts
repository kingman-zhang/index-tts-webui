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
