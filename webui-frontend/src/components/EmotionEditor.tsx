import { ChevronDown, Sparkles, Wand2 } from "lucide-react";
import { Select, Slider, Switch, Label, Button, Badge } from "./ui";
import { EMO_LABELS, EMO_PRESETS, type EmotionConfig, type VoiceFile } from "@/types";
import { cn } from "@/lib/utils";

interface EmotionEditorProps {
  emotion: EmotionConfig;
  onChange: (e: EmotionConfig) => void;
  voiceFiles: VoiceFile[];
  collapsed: boolean;
  onToggle: () => void;
}

const MODE_DESC = [
  "跟随音色参考音频的情感",
  "用独立情感参考音频控制",
  "用 8 维情感向量精确控制",
  "用文字描述情感（实验性）",
];

function matchesPreset(emotion: EmotionConfig, preset: { vector: number[]; weight: number }) {
  const vector = emotion.vector || [];
  return vector.length === preset.vector.length &&
    vector.every((value, index) => Math.abs((value || 0) - preset.vector[index]) < 0.001) &&
    Math.abs((emotion.weight || 0) - preset.weight) < 0.001;
}

export function EmotionEditor({ emotion, onChange, voiceFiles, collapsed, onToggle }: EmotionEditorProps) {
  const update = (patch: Partial<EmotionConfig>) => onChange({ ...emotion, ...patch });

  return (
    <div className="rounded-lg bg-gray-50 border border-gray-100">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-xs"
      >
        <span className="flex items-center gap-1.5 font-medium text-gray-600">
          <Sparkles className="w-3.5 h-3.5 text-indigo-500" />
          情感控制
          <Badge color="indigo">{["跟随音色", "参考音频", "情感向量", "文本描述"][emotion.mode]}</Badge>
        </span>
        <ChevronDown className={cn("w-3.5 h-3.5 text-gray-400 transition-transform", !collapsed && "rotate-180")} />
      </button>

      {!collapsed && (
        <div className="px-3 pb-3 space-y-3">
          {/* 模式选择 */}
          <div className="grid grid-cols-2 gap-1.5">
            {["跟随音色", "参考音频", "情感向量", "文本描述"].map((label, i) => (
              <button
                key={i}
                onClick={() => update({ mode: i as 0 | 1 | 2 | 3 })}
                className={cn(
                  "h-8 rounded-md text-xs font-medium border transition-colors",
                  emotion.mode === i
                    ? "bg-indigo-600 text-white border-indigo-600"
                    : "bg-white text-gray-600 border-gray-200 hover:border-indigo-300"
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-gray-400 -mt-1">{MODE_DESC[emotion.mode]}</p>

          {/* mode 1: 情感参考音频 */}
          {emotion.mode === 1 && (
            <div>
              <Label>情感参考音频</Label>
              <Select
                value={emotion.audio_path || ""}
                onChange={e => update({ audio_path: e.target.value || null })}
              >
                <option value="">— 选择情感参考音频 —</option>
                {voiceFiles.map(f => (
                  <option key={f.path} value={f.path}>{f.name}</option>
                ))}
              </Select>
            </div>
          )}

          {/* mode 2: 情感向量 */}
          {emotion.mode === 2 && (
            <div className="space-y-2">
              <div className="flex items-center gap-1.5 mb-1">
                <Wand2 className="w-3 h-3 text-indigo-500" />
                <span className="text-xs font-medium text-gray-600">情感预设</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {EMO_PRESETS.map(preset => (
                  <button
                    key={preset.name}
                    onClick={() => update({ vector: [...preset.vector], weight: preset.weight })}
                    className={cn(
                      "px-2 py-1 rounded-md text-[11px] border transition-colors",
                      matchesPreset(emotion, preset)
                        ? "bg-indigo-600 border-indigo-600 text-white"
                        : "bg-white border-gray-200 text-gray-600 hover:border-indigo-300 hover:text-indigo-600"
                    )}
                  >
                    {preset.name}
                  </button>
                ))}
              </div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 pt-1">
                {EMO_LABELS.map((label, i) => (
                  <Slider
                    key={label}
                    label={label}
                    min={0} max={1} step={0.05}
                    value={emotion.vector[i] || 0}
                    onChange={v => {
                      const vec = [...emotion.vector];
                      while (vec.length < 8) vec.push(0);
                      vec[i] = v;
                      update({ vector: vec });
                    }}
                  />
                ))}
              </div>
            </div>
          )}

          {/* mode 3: 情感文本 */}
          {emotion.mode === 3 && (
            <div>
              <Label>情感描述文本</Label>
              <textarea
                value={emotion.text || ""}
                onChange={e => update({ text: e.target.value || null })}
                placeholder="例如：用兴奋、愉快的语气说话"
                className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 resize-y min-h-[60px]"
              />
            </div>
          )}

          {/* 通用：权重 + 随机 */}
          {emotion.mode !== 0 && (
            <div className="flex items-center gap-4 pt-1 border-t border-gray-200">
              <Slider
                label="情感权重"
                min={0} max={1} step={0.05}
                value={emotion.weight}
                onChange={v => update({ weight: v })}
                className="flex-1"
              />
              <Switch
                checked={emotion.random}
                onChange={v => update({ random: v })}
                label="随机采样"
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
