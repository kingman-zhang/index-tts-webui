import { useRef, useState, useEffect } from "react";
import { Upload, Play, User, Mic2, Circle, Settings2, Mic, Square, Save, FolderOpen, ChevronDown, Pencil, Trash2 } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, Button, Badge, Label } from "./ui";
import { EmotionEditor } from "./EmotionEditor";
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
  A: { ring: "border-indigo-300 bg-indigo-50", dot: "bg-indigo-500", text: "text-indigo-700", badge: "indigo" as const },
  B: { ring: "border-teal-300 bg-teal-50", dot: "bg-teal-500", text: "text-teal-700", badge: "blue" as const },
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
  const [emoCollapsed, setEmoCollapsed] = useState(true);
  const [recording, setRecording] = useState(false);
  const [recordSecs, setRecordSecs] = useState(0);
  const [showVoicePicker, setShowVoicePicker] = useState(false);
  const [showSavePreset, setShowSavePreset] = useState(false);
  const [presetName, setPresetName] = useState("");
  const [savedPresets, setSavedPresets] = useState<any[]>([]);
  const [showPresetList, setShowPresetList] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [uploadDialog, setUploadDialog] = useState<{ file: File; mode: "upload" | "record" } | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const recordChunksRef = useRef<Blob[]>([]);
  const recordTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const c = COLORS[speakerKey];

  useEffect(() => {
    api.listVoicePresets().then(r => setSavedPresets(r.presets)).catch(() => {});
  }, []);

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
        role_speed: config.speed ?? 1.0,
        voice_path: config.voice_path,
        voice_name: config.voice_name,
        emotion: config.emotion,
      });
      const r = await api.listVoicePresets(); setSavedPresets(r.presets);
      setShowSavePreset(false); setPresetName("");
    } catch (e) { alert("保存失败: " + e); }
  };

  const loadPreset = async (id: string) => {
    try { const p = await api.getVoicePreset(id); onChange({ voice_path: p.voice_path, voice_name: p.voice_name, speed: p.role_speed ?? p.speed ?? 1.0, emotion: p.emotion }); setShowPresetList(false); }
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

  const currentVoice = voiceFiles.find(v => v.name === config.voice_name || v.path === config.voice_path);
  const isPresetVoice = Boolean(currentVoice?.source === "preset") || presetVoices.female.concat(presetVoices.male, presetVoices.emotion).some(v => v.name === config.voice_name || v.path === config.voice_path);

  const emoBadgeText = ["跟随音色", "参考音频", "情感向量", "文本描述"][config.emotion.mode];

  return (
    <Card className={cn("border-2", c.ring)}>
      <CardHeader className="flex flex-row items-center justify-between">
        <div className="flex items-center gap-2">
          <div className={cn("w-7 h-7 rounded-full flex items-center justify-center", c.dot)}>
            <User className="w-4 h-4 text-white" />
          </div>
          <CardTitle className={c.text}>主持人 {speakerKey}</CardTitle>
        </div>
        <div className="flex items-center gap-1.5">
          {savedPresets.length > 0 && (
            <button onClick={() => setShowPresetList(!showPresetList)} className="flex items-center gap-1 px-2 py-1 rounded text-xs text-indigo-600 hover:bg-indigo-50" title="打开角色预设列表">
              <FolderOpen className="w-3.5 h-3.5" />
              角色预设
            </button>
          )}
          {config.voice_path && (
            <button onClick={() => setShowSavePreset(!showSavePreset)} className="p-1.5 rounded text-gray-400 hover:text-green-600 hover:bg-green-50" title="保存角色预设">
              <Save className="w-3.5 h-3.5" />
            </button>
          )}
          <Badge color={c.badge}><Circle className="w-2 h-2 mr-1 fill-current" /> {speakerKey}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div>
          <Label>角色名称</Label>
          <input type="text" value={config.name} onChange={e => onChange({ name: e.target.value })}
            className="h-9 w-full rounded-lg border border-gray-300 bg-white px-3 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            placeholder={`主持人${speakerKey}的名字`} />
          <p className="mt-1 text-[10px] text-gray-400">用于脚本中的说话人标识，并会随当前项目保存。</p>
        </div>

        {showPresetList && savedPresets.length > 0 && (
          <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-2 space-y-1">
            <p className="text-[11px] font-medium text-indigo-700 px-1">已保存的角色预设</p>
            {savedPresets.map(p => (
              <div key={p.id} onClick={() => loadPreset(p.id)} className="flex items-center justify-between px-2 py-1.5 rounded bg-white cursor-pointer hover:bg-indigo-100 group">
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-gray-700 truncate">{p.name}</p>
                  <p className="text-[10px] text-gray-400 truncate">{p.voice_name}</p>
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
            <Label className="text-[11px] text-green-700">保存当前角色配置（含音色+默认情感）</Label>
            <div className="flex gap-2">
              <input type="text" value={presetName} onChange={e => setPresetName(e.target.value)}
                className="h-8 flex-1 rounded border border-green-300 bg-white px-2 text-xs" placeholder="如 温柔女声" autoFocus />
              <Button size="sm" onClick={savePreset} disabled={!presetName.trim()}>保存</Button>
            </div>
          </div>
        )}

        <div>
          <Label>角色语速</Label>
          <div className="flex items-center gap-2">
            <input type="range" min={0.5} max={2} step={0.05} value={config.speed}
              onChange={e => onChange({ speed: Number(e.target.value) })}
              className="flex-1 accent-indigo-600" />
            <span className="w-12 text-right text-xs font-medium text-gray-600">{config.speed.toFixed(2)}x</span>
          </div>
          <p className="mt-1 text-[10px] text-gray-400">只影响“{config.name || `角色${speakerKey}`}”的发言，1.0x 为正常速度。</p>
        </div>

        <div>
          <Label>音色参考音频</Label>
          <button onClick={() => setShowVoicePicker(true)}
            className="w-full h-9 flex items-center justify-between px-3 rounded-lg border border-gray-300 bg-white text-sm hover:border-indigo-400 transition-colors">
            <span className={cn("truncate", config.voice_name ? "text-gray-700" : "text-gray-400")}>
              {config.voice_name || "- 选择音色 -"}
            </span>
            <ChevronDown className="w-4 h-4 text-gray-400 shrink-0" />
          </button>
        </div>

        {config.voice_name && (
          <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-gray-50 border border-gray-100">
            <Mic2 className="w-3.5 h-3.5 text-gray-400 shrink-0" />
            <span className="text-xs text-gray-600 truncate flex-1">{config.voice_name}</span>
            <span className="text-[10px] text-gray-400">{isPresetVoice ? "预设音色" : "我的音色"}</span>
          </div>
        )}

        <div className="flex gap-2">
          <Button variant="outline" size="sm" icon={Upload} onClick={() => fileRef.current?.click()} disabled={uploading} className="flex-1">
            {uploading ? "保存中" : "上传"}
          </Button>
          {!recording ? (
            <Button variant="outline" size="sm" icon={Mic} onClick={startRecording} className="flex-1">录制</Button>
          ) : (
            <Button variant="outline" size="sm" icon={Square} onClick={stopRecording} className="flex-1 text-red-600 border-red-300">{recordSecs}s 停止</Button>
          )}
          <Button variant="outline" size="sm" icon={Play} onClick={() => preview()} disabled={!config.voice_name} className="flex-1">
            {playing ? "停止" : "试听"}
          </Button>
          <input ref={fileRef} type="file" accept="audio/*" className="hidden"
            onChange={e => { const f = e.target.files?.[0]; if (f) handleFileSelect(f); e.target.value = ""; }} />
        </div>

        {recording && (
          <div className="flex items-center gap-2 text-xs text-red-600">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" /> 正在录制... {recordSecs}秒
          </div>
        )}

        <div className="rounded-lg bg-white/60 border border-gray-100">
          <button onClick={() => setEmoCollapsed(!emoCollapsed)} className="w-full flex items-center justify-between px-3 py-2">
            <span className="flex items-center gap-1.5 text-xs font-medium text-gray-600">
              <Settings2 className="w-3.5 h-3.5 text-indigo-500" /> 默认情感 <Badge color="indigo">{emoBadgeText}</Badge>
            </span>
            <span className="text-[11px] text-gray-400">新发言行将继承此设置</span>
          </button>
          {!emoCollapsed && (
            <div className="px-2 pb-2">
              <EmotionEditor emotion={config.emotion} onChange={emo => onChange({ emotion: emo })}
                voiceFiles={voiceFiles} collapsed={false} onToggle={() => setEmoCollapsed(true)} />
            </div>
          )}
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
            <p className="text-[11px] text-gray-400">原始文件: {uploadDialog.file.name}</p>
            <div>
              <Label className="text-[11px]">音频名称</Label>
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
