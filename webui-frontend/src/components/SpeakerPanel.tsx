/**
 * 双人播客左栏角色卡：与单人配音 MonoVoiceCard 同风格——
 * 头像悬停试听 + 选择音色/上传本地音色两键 + 对数刻度语速滑条。
 * 角色默认情感不再在此配置（新行默认"跟随音色"，行级可在脚本芯片单独调整）。
 */
import { useEffect, useRef, useState } from "react";
import { Upload, Play, Square, AudioLines, FolderOpen, Pencil, Trash2 } from "lucide-react";
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
  const [showVoicePicker, setShowVoicePicker] = useState(false);
  const [savedPresets, setSavedPresets] = useState<any[]>([]);
  const [showPresetList, setShowPresetList] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [uploadDialog, setUploadDialog] = useState<{ file: File } | null>(null);
  const [speedText, setSpeedText] = useState((config.speed ?? 1.0).toFixed(2));
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const c = COLORS[speakerKey];

  useEffect(() => {
    api.listVoicePresets().then(r => setSavedPresets(r.presets)).catch(() => {});
  }, []);

  // 卸载时停止试听（tab 切换会卸载非当前角色的卡片，避免声音残留）
  useEffect(() => () => { audioRef.current?.pause(); }, []);

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
    // 试听目标：优先显式文件名；否则用 voice_path 的文件名（BreezeBlue 音色的 voice_name 是显示名，不是文件名）
    const target = name || (config.voice_path ? config.voice_path.split(/[\\/]/).pop() || "" : "") || config.voice_name;
    if (!target) return;
    if (playing && playingName === target) {
      audioRef.current?.pause(); setPlaying(false); setPlayingName(null); return;
    }
    if (!audioRef.current) audioRef.current = new Audio();
    audioRef.current.src = `/api/audio/${target}`;
    audioRef.current.play(); setPlaying(true); setPlayingName(target);
    audioRef.current.onended = () => { setPlaying(false); setPlayingName(null); };
  };

  const handleFileSelect = (file: File) => {
    setRenameValue(file.name.replace(/\.[^.]+$/, ""));
    setUploadDialog({ file });
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
        {/* 角色名称（一行排布）+ A/B 徽标（卡片右上角）+ 预设小按钮 */}
        <div className="flex items-center gap-2">
          <input type="text" value={config.name} onChange={e => onChange({ name: e.target.value })}
            className="h-9 min-w-0 flex-1 rounded-lg border border-gray-300 bg-white px-3 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            placeholder={`主持人${speakerKey}的名字`} />
          <div className="flex items-center gap-0.5 shrink-0">
            {savedPresets.length > 0 && (
              <button onClick={() => setShowPresetList(!showPresetList)} className="p-1.5 rounded text-gray-400 hover:text-indigo-600 hover:bg-indigo-50" title="打开角色预设列表">
                <FolderOpen className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
          <Badge color={c.badge} className="shrink-0">{speakerKey}</Badge>
        </div>
        <p className="-mt-2 text-[0.6875rem] text-gray-400">用于脚本中的说话人标识，并会随当前项目保存。</p>

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
        </div>

        {/* 操作两键 */}
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" size="sm" icon={AudioLines} onClick={() => setShowVoicePicker(true)}>
            选择音色
          </Button>
          <Button variant="outline" size="sm" icon={Upload} onClick={() => fileRef.current?.click()} disabled={uploading}>
            {uploading ? "上传中" : "上传本地音色"}
          </Button>
          <input ref={fileRef} type="file" accept="audio/*" className="hidden"
            onChange={e => { const f = e.target.files?.[0]; if (f) handleFileSelect(f); e.target.value = ""; }} />
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
            <h3 className="text-sm font-semibold text-gray-800">保存上传的音频</h3>
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
  // A/B 叠放于同一面板区域，顶部 tab 切换当前编辑的角色
  const [active, setActive] = useState<"A" | "B">("A");
  useEffect(() => {
    api.listPresetVoices().then(r => setPresetVoices(r.categories || { female: [], male: [], emotion: [] })).catch(() => {});
  }, []);
  const activeCfg = COLORS[active];
  return (
    <div className="space-y-2">
      {/* A/B 切换 tab：色点 + 角色名（未命名时显示 主持人A/B）+ 角色字母 */}
      <div className="grid grid-cols-2 gap-1 rounded-xl bg-gray-100 p-1">
        {(["A", "B"] as const).map(k => {
          const cc = COLORS[k];
          const isActive = active === k;
          return (
            <button
              key={k}
              onClick={() => setActive(k)}
              className={cn(
                "flex h-9 min-w-0 items-center justify-center gap-1.5 rounded-lg px-2 text-[0.8125rem] font-medium transition-all",
                isActive
                  ? cn("border bg-white shadow-sm", cc.ring, cc.text)
                  : "border border-transparent text-gray-500 hover:bg-white/60 hover:text-gray-700"
              )}
              title={`切换到角色 ${k}`}
            >
              {/* A/B 字母放最左，常亮角色主题色，强化标识 */}
              <span className={cn("shrink-0 rounded px-1.5 py-0.5 text-[0.625rem] font-bold leading-none", cc.avatar)}>
                {k}
              </span>
              <span className="truncate">{speakers[k].name?.trim() || `主持人${k}`}</span>
            </button>
          );
        })}
      </div>
      <SpeakerCard
        key={active}
        speakerKey={active}
        config={speakers[active]}
        voiceFiles={voiceFiles}
        presetVoices={presetVoices}
        onChange={c => onChange(active, c)}
        onUpload={onUpload}
        onRenameVoice={onRenameVoice}
        onDeleteVoice={onDeleteVoice}
      />
    </div>
  );
}
