/**
 * 双人播客左栏角色卡：与单人配音 MonoVoiceCard 同风格——
 * 头像悬停试听 + 选择音色/上传本地音色两键 + 对数刻度语速滑条。
 * 角色默认情感不再在此配置（新行默认"跟随音色"，行级可在脚本芯片单独调整）。
 */
import { useEffect, useRef, useState } from "react";
import { Upload, Play, Square, AudioLines, Mic, FolderOpen, Save, Pencil, Trash2 } from "lucide-react";
import { Card, CardContent, Button, Badge, Label } from "./ui";
import { VoicePicker } from "./VoicePicker";
import { api } from "@/api/client";
import type { SpeakerConfig, VoiceFile } from "@/types";
import { cn } from "@/lib/utils";

interface PresetVoices {
  female: VoiceFile[];
  male: VoiceFile[];
  emotion: VoiceFile[];
}

interface SpeakerPanelProps {
  speakers: { A: SpeakerConfig; B: SpeakerConfig };
  onChange: (key: "A" | "B", config: Partial<SpeakerConfig>) => void;
  voiceFiles: VoiceFile[];
  onUpload: (file: File, customName?: string) => Promise<{ name: string; path: string } | null>;
  onRenameVoice: (oldName: string, newName: string) => Promise<void>;
  onDeleteVoice: (name: string) => Promise<void>;
}

const COLORS = {
  A: {
    ring: "border-indigo-200 bg-white",
    dot: "bg-indigo-500",
    text: "text-indigo-700",
    badge: "indigo" as const,
    avatar: "bg-indigo-50 border-indigo-100 text-indigo-700",
    overlay: "bg-indigo-900/45",
  },
  B: {
    ring: "border-teal-200 bg-white",
    dot: "bg-teal-500",
    text: "text-teal-700",
    badge: "blue" as const,
    avatar: "bg-teal-50 border-teal-100 text-teal-700",
    overlay: "bg-teal-900/45",
  },
};

function SpeakerCard({ speakerKey, config, onChange, voiceFiles, onUpload, onRenameVoice, onDeleteVoice, presetVoices }: {
  speakerKey: "A" | "B";
  config: SpeakerConfig;
  onChange: (c: Partial<SpeakerConfig>) => void;
  voiceFiles: VoiceFile[];
  onUpload: (file: File, customName?: string) => Promise<{ name: string; path: string } | null>;
  onRenameVoice: (oldName: string, newName: string) => Promise<void>;
  onDeleteVoice: (name: string) => Promise<void>;
  presetVoices: PresetVoices;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [playingName, setPlayingName] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [recordSecs, setRecordSecs] = useState(0);
  const [showVoicePicker, setShowVoicePicker] = useState(false);
  const [showSavePreset, setShowSavePreset] = useState(false);
  const [presetName, setPresetName] = useState("");
  const [savedPresets, setSavedPresets] = useState<any[]>([]);
  const [showPresetList, setShowPresetList] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [uploadDialog, setUploadDialog] = useState<{ file: File; mode: "upload" | "record" } | null>(null);
  const [speedText, setSpeedText] = useState((config.speed ?? 1.0).toFixed(2));
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const recordChunksRef = useRef<Blob[]>([]);
  const recordTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const c = COLORS[speakerKey];

  useEffect(() => {
    api.listVoicePresets().then(r => setSavedPresets(r.presets)).catch(() => {});
  }, []);

  // 语速输入框跟随外部状态（如项目加载）
  useEffect(() => { setSpeedText((config.speed ?? 1.0).toFixed(2)); }, [config.speed]);

  // 对数刻度：0.5–2 关于 1.0 对称（log2 空间），1.0x 恰在滑条正中
  const speed = config.speed ?? 1.0;
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
    const target = name || config.voice_name;
    if (!target) return;
    if (playing && playingName === target) {
      audioRef.current?.pause(); setPlaying(false); setPlayingName(null); return;
    }
    if (!audioRef.current) audioRef.current = new Audio();
    audioRef.current.src = `/api/audio/${target}`;
    audioRef.current.play(); setPlaying(true); setPlayingName(target);
    audioRef.current.onended = () => { setPlaying(false); setPlayingName(null); };
  };

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      recordChunksRef.current = [];
      recorder.ondataavailable = e => { if (e.data.size > 0) recordChunksRef.current.push(e.data); };
      recorder.onstop = () => {
        const blob = new Blob(recordChunksRef.current, { type: "audio/webm" });
        const file = new File([blob], `recording_${speakerKey}_${Date.now()}.webm`, { type: "audio/webm" });
        setRenameValue(`${speakerKey === "A" ? "主持A" : "主持B"}_录制_${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`);
        setUploadDialog({ file, mode: "record" });
        stream.getTracks().forEach(t => t.stop());
      };
      recorder.start();
      mediaRecorderRef.current = recorder;
      setRecording(true); setRecordSecs(0);
      recordTimerRef.current = setInterval(() => setRecordSecs(s => s + 1), 1000);
    } catch (e) { alert("无法访问麦克风: " + e); }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && recording) {
      mediaRecorderRef.current.stop(); setRecording(false);
      if (recordTimerRef.current) clearInterval(recordTimerRef.current);
    }
  };

  const handleFileSelect = (file: File) => {
    setRenameValue(file.name.replace(/\.[^.]+$/, ""));
    setUploadDialog({ file, mode: "upload" });
  };

  const confirmUpload = async () => {
    if (!uploadDialog || !renameValue.trim()) return;
    setUploading(true);
    try {
      const result = await onUpload(uploadDialog.file, renameValue.trim());
      if (result) onChange({ voice_path: result.path, voice_name: result.name });
      setUploadDialog(null);
    } catch (e: any) { alert("保存失败: " + e.message); }
    finally { setUploading(false); }
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

  const savePreset = async () => {
    if (!presetName.trim() || !config.voice_path) return;
    try {
      await api.saveVoicePreset({
        name: presetName.trim(),
        role_speed: speed,
        voice_path: config.voice_path,
        voice_name: config.voice_name,
      });
      const r = await api.listVoicePresets(); setSavedPresets(r.presets);
      setShowSavePreset(false); setPresetName("");
    } catch (e) { alert("保存失败: " + e); }
  };

  const loadPreset = async (id: string) => {
    try {
      const p = await api.getVoicePreset(id);
      onChange({ voice_path: p.voice_path, voice_name: p.voice_name, speed: p.role_speed ?? p.speed ?? 1.0 });
      setShowPresetList(false);
    }
    catch (e) { alert("加载失败: " + e); }
  };

  const deletePreset = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm("确定删除这个角色预设吗？")) return;
    await api.deleteVoicePreset(id);
    const r = await api.listVoicePresets(); setSavedPresets(r.presets);
  };

  const renamePreset = async (preset: any, e: React.MouseEvent) => {
    e.stopPropagation();
    const next = window.prompt("请输入新的角色预设名称", preset.name);
    if (!next?.trim()) return;
    await api.renameVoicePreset(preset.id, next.trim());
    const r = await api.listVoicePresets(); setSavedPresets(r.presets);
  };

  return (
    <Card className={cn("rounded-2xl border-2 shadow-sm", c.ring)}>
      <CardContent className="p-5 space-y-4">
        {/* 角色名称 + 预设小按钮 */}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <Label>角色名称</Label>
            <div className="flex items-center gap-1">
              {savedPresets.length > 0 && (
                <button onClick={() => setShowPresetList(!showPresetList)} className="p-1.5 rounded text-gray-400 hover:text-indigo-600 hover:bg-indigo-50" title="打开角色预设列表">
                  <FolderOpen className="w-3.5 h-3.5" />
                </button>
              )}
              {config.voice_path && (
                <button onClick={() => setShowSavePreset(!showSavePreset)} className="p-1.5 rounded text-gray-400 hover:text-green-600 hover:bg-green-50" title="保存角色预设">
                  <Save className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          </div>
          <input type="text" value={config.name} onChange={e => onChange({ name: e.target.value })}
            className="h-9 w-full rounded-lg border border-gray-300 bg-white px-3 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            placeholder={`主持人${speakerKey}的名字`} />
          <p className="mt-1 text-[0.6875rem] text-gray-400">用于脚本中的说话人标识，并会随当前项目保存。</p>
        </div>

        {showPresetList && savedPresets.length > 0 && (
          <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-2 space-y-1">
            <p className="text-[0.75rem] font-medium text-indigo-700 px-1">已保存的角色预设</p>
            {savedPresets.map(p => (
              <div key={p.id} onClick={() => loadPreset(p.id)} className="flex items-center justify-between px-2 py-1.5 rounded bg-white cursor-pointer hover:bg-indigo-100 group">
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-gray-700 truncate">{p.name}</p>
                  <p className="text-[0.6875rem] text-gray-400 truncate">{p.voice_name}</p>
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100">
                  <button onClick={e => renamePreset(p, e)} className="p-1 text-gray-300 hover:text-indigo-600" title="重命名"><Pencil className="w-3 h-3" /></button>
                  <button onClick={e => deletePreset(p.id, e)} className="p-1 text-gray-300 hover:text-red-500" title="删除"><Trash2 className="w-3 h-3" /></button>
                </div>
              </div>
            ))}
          </div>
        )}

        {showSavePreset && (
          <div className="rounded-lg border border-green-200 bg-green-50 p-2 space-y-2">
            <Label className="text-[0.75rem] text-green-700">保存当前角色配置（音色+语速）</Label>
            <div className="flex gap-2">
              <input type="text" value={presetName} onChange={e => setPresetName(e.target.value)}
                className="h-8 flex-1 rounded border border-green-300 bg-white px-2 text-xs" placeholder="如 温柔女声" autoFocus />
              <Button size="sm" onClick={savePreset} disabled={!presetName.trim()}>保存</Button>
            </div>
          </div>
        )}

        {/* 音色主体：头像悬停即试听/停止 */}
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => preview()}
            disabled={!config.voice_name}
            title={playing ? "停止试听" : "试听"}
            className="group relative w-11 h-11 rounded-full shrink-0 disabled:cursor-not-allowed"
          >
            <div className={cn("w-11 h-11 rounded-full border flex items-center justify-center", c.avatar)}>
              {config.voice_name ? (
                <span className="text-base font-semibold">
                  {config.name?.trim()?.slice(0, 1) || speakerKey}
                </span>
              ) : (
                <AudioLines className="w-5 h-5 opacity-50" />
              )}
            </div>
            {config.voice_name && (
              <div className={cn("absolute inset-0 rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity", c.overlay)}>
                {playing ? (
                  <Square className="w-4 h-4 text-white fill-white" />
                ) : (
                  <Play className="w-4 h-4 text-white fill-white translate-x-[1px]" />
                )}
              </div>
            )}
          </button>
          <div className="min-w-0 flex-1">
            <p className={cn("text-sm font-medium truncate", config.voice_name ? "text-gray-800" : "text-gray-400")}>
              {config.voice_name ? config.voice_name.replace(/\.[^.]+$/, "") : "未选择音色"}
            </p>
            <p className="text-[0.75rem] text-gray-400 mt-0.5">
              {config.voice_name ? "悬停头像可试听" : "从预设库选择，或上传参考音频"}
            </p>
          </div>
          <Badge color={c.badge}>{speakerKey}</Badge>
        </div>

        {/* 操作两键 + 录制 */}
        <div className="grid grid-cols-[1fr_1fr_auto] gap-2">
          <Button variant="outline" size="sm" icon={AudioLines} onClick={() => setShowVoicePicker(true)}>
            选择音色
          </Button>
          <Button variant="outline" size="sm" icon={Upload} onClick={() => fileRef.current?.click()} disabled={uploading}>
            {uploading ? "上传中" : "上传本地音色"}
          </Button>
          {!recording ? (
            <Button variant="outline" size="sm" icon={Mic} onClick={startRecording} title="录制一段参考音频" />
          ) : (
            <Button variant="outline" size="sm" icon={Square} onClick={stopRecording} title="停止录制" className="text-red-600 border-red-300">
              {recordSecs}s
            </Button>
          )}
          <input ref={fileRef} type="file" accept="audio/*" className="hidden"
            onChange={e => { const f = e.target.files?.[0]; if (f) handleFileSelect(f); e.target.value = ""; }} />
        </div>

        {recording && (
          <div className="flex items-center gap-2 text-xs text-red-600">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" /> 正在录制... {recordSecs}秒
          </div>
        )}

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
          <p className="mt-1 text-[0.6875rem] text-gray-400">只影响该角色的发言，1.0x 为正常速度。</p>
        </div>
      </CardContent>

      <VoicePicker open={showVoicePicker} onClose={() => setShowVoicePicker(false)}
        currentPath={config.voice_path} voiceFiles={voiceFiles} presetVoices={presetVoices}
        onSelect={handleVoiceSelect} onPreview={preview} playingName={playingName}
        onRename={voice => {
          const next = window.prompt("请输入新的音色名称", voice.name.replace(/\.[^.]+$/, ""));
          if (next?.trim()) onRenameVoice(voice.name, next.trim()).catch(e => alert("改名失败: " + e.message));
        }}
        onDelete={voice => {
          if (window.confirm(`确定删除音色“${voice.name}”吗？删除后不可恢复。`)) {
            onDeleteVoice(voice.name).catch(e => alert("删除失败: " + e.message));
          }
        }} />

      {uploadDialog && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => !uploading && setUploadDialog(null)}>
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm mx-4 p-4 space-y-3" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-gray-800">{uploadDialog.mode === "upload" ? "保存上传的音频" : "保存录制的音频"}</h3>
            <p className="text-[0.75rem] text-gray-400">原始文件: {uploadDialog.file.name}</p>
            <div>
              <Label className="text-[0.75rem]">音频名称</Label>
              <input type="text" value={renameValue} onChange={e => setRenameValue(e.target.value)}
                className="h-9 w-full rounded-lg border border-gray-300 bg-white px-3 text-sm" autoFocus
                onKeyDown={e => { if (e.key === "Enter") confirmUpload(); }} />
            </div>
            <div className="flex gap-2 justify-end">
              <Button variant="outline" size="sm" onClick={() => setUploadDialog(null)} disabled={uploading}>取消</Button>
              <Button size="sm" onClick={confirmUpload} disabled={uploading || !renameValue.trim()}>
                {uploading ? "保存中..." : "保存并使用"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

export function SpeakerPanel({ speakers, onChange, voiceFiles, onUpload, onRenameVoice, onDeleteVoice }: SpeakerPanelProps) {
  const [presetVoices, setPresetVoices] = useState<PresetVoices>({ female: [], male: [], emotion: [] });
  useEffect(() => {
    api.listPresetVoices().then(r => setPresetVoices(r.categories || { female: [], male: [], emotion: [] })).catch(() => {});
  }, []);
  return (
    <div className="space-y-3">
      <SpeakerCard speakerKey="A" config={speakers.A} voiceFiles={voiceFiles} presetVoices={presetVoices}
        onChange={c => onChange("A", c)} onUpload={onUpload} onRenameVoice={onRenameVoice} onDeleteVoice={onDeleteVoice} />
      <SpeakerCard speakerKey="B" config={speakers.B} voiceFiles={voiceFiles} presetVoices={presetVoices}
        onChange={c => onChange("B", c)} onUpload={onUpload} onRenameVoice={onRenameVoice} onDeleteVoice={onDeleteVoice} />
    </div>
  );
}
