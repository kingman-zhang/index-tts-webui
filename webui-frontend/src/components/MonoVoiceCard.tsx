/**
 * 配音模式左栏音色卡：选择 / 上传 / 试听 / 语速。
 * 视觉：白底大圆角卡片 + 翡翠绿点缀，与画布格调一致。
 */
import { useEffect, useRef, useState } from "react";
import { MicVocal, Play, Square, Library, Upload } from "lucide-react";
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
  const [presetVoices, setPresetVoices] = useState<PresetVoices>({ female: [], male: [], emotion: [] });
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    api.listPresetVoices().then(r => setPresetVoices(r.categories || { female: [], male: [], emotion: [] })).catch(() => {});
  }, [voiceFiles]);

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
        {/* 音色主体 */}
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-full bg-emerald-50 border border-emerald-100 flex items-center justify-center shrink-0">
            {voice.voice_name ? (
              <span className="text-base font-semibold text-emerald-700">
                {voice.voice_name.replace(/\.[^.]+$/, "").slice(0, 1)}
              </span>
            ) : (
              <MicVocal className="w-5 h-5 text-emerald-500" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className={cn("text-sm font-medium truncate", voice.voice_name ? "text-gray-800" : "text-gray-400")}>
              {voice.voice_name ? voice.voice_name.replace(/\.[^.]+$/, "") : "未选择音色"}
            </p>
            <p className="text-[11px] text-gray-400 mt-0.5">
              {voice.voice_name ? "试听确认效果后再生成" : "从预设库选择，或上传参考音频"}
            </p>
          </div>
        </div>

        {/* 操作三键 */}
        <div className="grid grid-cols-3 gap-2">
          <Button variant="outline" size="sm" icon={Library} onClick={() => setShowVoicePicker(true)}>
            选择
          </Button>
          <Button variant="outline" size="sm" icon={Upload} onClick={() => fileRef.current?.click()} disabled={uploading}>
            {uploading ? "上传中" : "上传"}
          </Button>
          <Button variant="outline" size="sm" icon={playing ? Square : Play} onClick={() => preview()} disabled={!voice.voice_name}>
            {playing ? "停止" : "试听"}
          </Button>
        </div>

        {/* 语速 */}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs font-medium text-gray-600">语速</span>
            <span className="text-xs font-medium text-gray-600 tabular-nums">{speed.toFixed(2)}x</span>
          </div>
          <input
            type="range" min={0.5} max={2} step={0.05} value={speed}
            onChange={e => onChange({ speed: Number(e.target.value) })}
            className="w-full accent-emerald-600"
          />
          <div className="flex justify-between text-[10px] text-gray-300 mt-1">
            <span>0.5x</span><span>1x</span><span>2x</span>
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
