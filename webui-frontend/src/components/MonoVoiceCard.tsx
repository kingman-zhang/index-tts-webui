/**
 * 配音模式左栏音色卡：选择 / 上传 / 试听 / 语速。
 * 视觉：白底大圆角卡片 + 翡翠绿点缀，与画布格调一致。
 */
import { useEffect, useRef, useState } from "react";
import { MicVocal, Play, Square, AudioLines, Upload } from "lucide-react";
import { Card, CardContent, Button } from "./ui";
import { VoicePicker } from "./VoicePicker";
import { api } from "@/api/client";
import type { VoiceFile } from "@/types";
import { cn } from "@/lib/utils";

interface PresetVoices {
  female: VoiceFile[];
  male: VoiceFile[];
  emotion: VoiceFile[];
}

export interface MonoVoice {
  voice_path: string | null;
  voice_name: string | null;
}

interface MonoVoiceCardProps {
  voice: MonoVoice;
  speed: number;
  onChange: (patch: Partial<MonoVoice & { speed: number }>) => void;
  voiceFiles: VoiceFile[];
  onUpload: (file: File, customName?: string) => Promise<{ name: string; path: string } | null>;
}

export function MonoVoiceCard({ voice, speed, onChange, voiceFiles, onUpload }: MonoVoiceCardProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [playingName, setPlayingName] = useState<string | null>(null);
  const [showVoicePicker, setShowVoicePicker] = useState(false);
  const [speedText, setSpeedText] = useState(speed.toFixed(2));
  const [presetVoices, setPresetVoices] = useState<PresetVoices>({ female: [], male: [], emotion: [] });
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    api.listPresetVoices().then(r => setPresetVoices(r.categories || { female: [], male: [], emotion: [] })).catch(() => {});
  }, [voiceFiles]);

  // 语速输入框跟随外部状态（如草稿加载）
  useEffect(() => { setSpeedText(speed.toFixed(2)); }, [speed]);

  // 对数刻度：0.5–2 关于 1.0 对称（log2 空间），1.0x 恰在滑条正中
  const sliderPos = Math.log2(speed / 0.5) / 2;
  const fillPct = Math.round(sliderPos * 1000) / 10;

  const commitSpeed = () => {
    const v = Number.parseFloat(speedText);
    if (Number.isNaN(v)) { setSpeedText(speed.toFixed(2)); return; }
    const clamped = Math.min(2, Math.max(0.5, Math.round(v * 100) / 100));
    onChange({ speed: clamped });
    setSpeedText(clamped.toFixed(2));
  };

  const preview = (name?: string) => {
    const target = name || voice.voice_name;
    if (!target) return;
    if (playing && playingName === target) {
      audioRef.current?.pause(); setPlaying(false); setPlayingName(null); return;
    }
    if (!audioRef.current) audioRef.current = new Audio();
    audioRef.current.src = `/api/audio/${target}`;
    audioRef.current.play(); setPlaying(true); setPlayingName(target);
    audioRef.current.onended = () => { setPlaying(false); setPlayingName(null); };
  };

  const selectPreset = async (name: string) => {
    try { const r = await api.uploadPresetToTTS(name); onChange({ voice_path: r.path, voice_name: r.name }); }
    catch (e) { alert("加载预设音色失败: " + e); }
  };

  const handleVoiceSelect = (path: string, name: string) => {
    const isPreset = presetVoices.female.some(f => f.path === path) ||
      presetVoices.male.some(f => f.path === path) ||
      presetVoices.emotion.some(f => f.path === path);
    if (isPreset) selectPreset(name);
    else onChange({ voice_path: path, voice_name: name });
  };

  const handleFileSelect = (file: File) => {
    const name = window.prompt("音频名称", file.name.replace(/\.[^.]+$/, ""));
    if (!name?.trim()) return;
    setUploading(true);
    onUpload(file, name.trim())
      .then(result => { if (result) onChange({ voice_path: result.path, voice_name: result.name }); })
      .catch((e: any) => alert("上传失败: " + e.message))
      .finally(() => setUploading(false));
  };

  return (
    <Card className="rounded-2xl border-gray-200 shadow-sm">
      <CardContent className="p-5 space-y-4">
        {/* 音色主体：头像悬停即试听/停止 */}
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => preview()}
            disabled={!voice.voice_name}
            title={playing ? "停止试听" : "试听"}
            className="group relative w-11 h-11 rounded-full shrink-0 disabled:cursor-not-allowed"
          >
            <div className="w-11 h-11 rounded-full bg-emerald-50 border border-emerald-100 flex items-center justify-center">
              {voice.voice_name ? (
                <span className="text-base font-semibold text-emerald-700">
                  {voice.voice_name.replace(/\.[^.]+$/, "").slice(0, 1)}
                </span>
              ) : (
                <MicVocal className="w-5 h-5 text-emerald-500" />
              )}
            </div>
            {voice.voice_name && (
              <div className="absolute inset-0 rounded-full bg-emerald-900/45 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                {playing ? (
                  <Square className="w-4 h-4 text-white fill-white" />
                ) : (
                  <Play className="w-4 h-4 text-white fill-white translate-x-[1px]" />
                )}
              </div>
            )}
          </button>
          <div className="min-w-0 flex-1">
            <p className={cn("text-sm font-medium truncate", voice.voice_name ? "text-gray-800" : "text-gray-400")}>
              {voice.voice_name ? voice.voice_name.replace(/\.[^.]+$/, "") : "未选择音色"}
            </p>
            <p className="text-[0.75rem] text-gray-400 mt-0.5">
              {voice.voice_name ? "悬停头像可试听" : "从预设库选择，或上传参考音频"}
            </p>
          </div>
        </div>

        {/* 操作两键 */}
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" size="sm" icon={AudioLines} onClick={() => setShowVoicePicker(true)}>
            选择
          </Button>
          <Button variant="outline" size="sm" icon={Upload} onClick={() => fileRef.current?.click()} disabled={uploading}>
            {uploading ? "上传中" : "上传"}
          </Button>
        </div>

        {/* 语速：对数刻度滑条（1.0x 居中）+ 数值输入 */}
        <div>
          <span className="text-xs font-medium text-gray-600">语速</span>
          <div className="flex items-center gap-2.5 mt-1.5">
            <input
              type="range" min={0} max={1} step={0.005} value={sliderPos}
              onChange={e => {
                const v = 0.5 * Math.pow(2, Number(e.target.value) * 2);
                onChange({ speed: Math.round(v * 20) / 20 }); // 吸附到 0.05 步进
              }}
              className="flex-1 min-w-0 speed-range"
              style={{
                background: `linear-gradient(to right, #10b981 0%, #34d399 ${fillPct}%, #e5e7eb ${fillPct}%, #e5e7eb 100%)`,
              }}
            />
            <input
              value={speedText}
              onChange={e => setSpeedText(e.target.value)}
              onBlur={commitSpeed}
              onKeyDown={e => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
              inputMode="decimal"
              className="w-[4.5rem] h-8 text-center text-xs font-medium tabular-nums rounded-lg border border-gray-200 outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100 transition-colors shrink-0"
            />
          </div>
        </div>
      </CardContent>

      <VoicePicker open={showVoicePicker} onClose={() => setShowVoicePicker(false)}
        currentPath={voice.voice_path} voiceFiles={voiceFiles} presetVoices={presetVoices}
        onSelect={handleVoiceSelect} onPreview={preview} playingName={playingName} />

      <input ref={fileRef} type="file" accept="audio/*" className="hidden"
        onChange={e => { const f = e.target.files?.[0]; if (f) handleFileSelect(f); e.target.value = ""; }} />
    </Card>
  );
}
