import { useState, useRef, type DragEvent } from "react";
import {
  Plus, Trash2, Copy, ArrowUp, ArrowDown, FileText, X, GripVertical, ListChecks, UploadCloud, Eraser
} from "lucide-react";
import { Button, Card, EmptyState, Textarea, Badge } from "./ui";
import { EmotionEditor } from "./EmotionEditor";
import { makeLine, type PodcastLine, type VoiceFile, type SpeakerConfig, type EmotionConfig } from "@/types";
import { cn } from "@/lib/utils";

interface ScriptEditorProps {
  lines: PodcastLine[];
  speakers: { A: SpeakerConfig; B: SpeakerConfig };
  voiceFiles: VoiceFile[];
  onChange: (lines: PodcastLine[]) => void;
  onImport: (text: string) => Promise<PodcastLine[]>;
  onImportConfig?: (config: {
    voices?: Record<string, string>;
    silence?: Partial<{ within_segment: number; between_lines: number; speaker_switch: number }>;
    params?: Record<string, unknown>;
    lines?: PodcastLine[];
    projectName?: string;
  }) => void;
}

/** 解析 JSONL 格式（indextts2 batch 兼容格式 + 扩展）。
 * 支持两种形式：
 *   1. 原生格式：{"text":"...","voice":"/path/voice.wav","silence_after_ms":400}
 *   2. 扩展格式：{"text":"...","role":"A","emotion":{"mode":2,"vector":[...]}}
 * role 字段（推荐）："A" / "B" / 主持人名
 * 兼容旧格式：voice / speaker 字段也表示角色
 */
function parseJSONL(text: string, speakers: { A: SpeakerConfig; B: SpeakerConfig }): PodcastLine[] {
  const result: PodcastLine[] = [];
  const trimmedText = text.trim();
  let rawLines: string[];
  // 同时支持 JSONL，以及 WebUI 队列/项目导出的单个 JSON 文档：
  // { "lines": [{ "speaker": "A", "text": "..." }, ...] }
  try {
    const document = JSON.parse(trimmedText);
    const records = Array.isArray(document)
      ? document
      : (document && Array.isArray(document.lines) ? document.lines : [document]);
    rawLines = records.map((record: unknown) => JSON.stringify(record));
  } catch {
    rawLines = trimmedText.split("\n");
  }
  for (const raw of rawLines) {
    const trimmed = raw.trim();
    if (!trimmed || !trimmed.startsWith("{")) continue;
    try {
      const obj = JSON.parse(trimmed);
      if (!obj.text) continue;

      // 判断说话人：优先 role 字段，其次 speaker，最后 voice（兼容旧格式）
      let speaker: "A" | "B" = result.length % 2 === 0 ? "A" : "B";
      const speakerHint = obj.role || obj.speaker || obj.voice;
      if (speakerHint) {
        const hint = String(speakerHint);
        const hintLower = hint.toLowerCase();
        const fileName = hint.split("/").pop()?.split("\\").pop() || "";
        if (hint === "A" || hint === "a" || hintLower === "a" || hint.includes(speakers.A.name)) {
          speaker = "A";
        } else if (hint === "B" || hint === "b" || hintLower === "b" || hint.includes(speakers.B.name)) {
          speaker = "B";
        } else if (speakers.A.voice_path && hint === speakers.A.voice_path) {
          speaker = "A";
        } else if (speakers.B.voice_path && hint === speakers.B.voice_path) {
          speaker = "B";
        } else if (speakers.A.voice_name && fileName === speakers.A.voice_name) {
          speaker = "A";
        } else if (speakers.B.voice_name && fileName === speakers.B.voice_name) {
          speaker = "B";
        }
      }

      // 情感：兼容 WebUI 嵌套 emotion，以及旧 JSONL 的 emotion_text/emotion_weight
      // 只有 JSONL 中显式写了情感字段时才标记 emotion_from_code，
      // 否则使用角色默认值并在序列化时省略（除非用户在可视化中改过）。
      const baseEmo = speakers[speaker].emotion;
      const hasEmotionField = !!(obj.emotion || obj.emotion_text || obj.emotion_weight);
      const sourceEmotion = obj.emotion || (obj.emotion_text
        ? { mode: 3, text: obj.emotion_text, weight: obj.emotion_weight }
        : null);
      const emo = sourceEmotion
        ? {
            mode: (Number(sourceEmotion.mode ?? baseEmo.mode) as 0 | 1 | 2 | 3),
            audio_path: sourceEmotion.audio_path ?? baseEmo.audio_path,
            vector: Array.isArray(sourceEmotion.vector) ? sourceEmotion.vector.map(Number) : [...baseEmo.vector],
            weight: Number(sourceEmotion.weight ?? baseEmo.weight),
            text: sourceEmotion.text ?? baseEmo.text,
            random: Boolean(sourceEmotion.random ?? baseEmo.random),
          }
        : { ...baseEmo, vector: [...baseEmo.vector] };

      // 行级静音：只有 JSONL 中显式写了才标记 silence_from_code
      const hasSilenceField = Number.isFinite(Number(obj.silence_after_ms));

      result.push({
        id: Math.random().toString(36).slice(2, 10),
        speaker,
        text: String(obj.text),
        emotion: emo,
        emotion_from_code: hasEmotionField,
        silence_after_ms: hasSilenceField
          ? Math.max(0, Number(obj.silence_after_ms))
          : undefined,
        silence_from_code: hasSilenceField,
      });
    } catch {
      // 跳过无法解析的行
    }
  }
  return result;
}

export function ScriptEditor({ lines, speakers, voiceFiles, onChange, onImport, onImportConfig }: ScriptEditorProps) {
  const [collapsedEmos, setCollapsedEmos] = useState<Set<string>>(new Set());
  const [scriptView, setScriptView] = useState<"visual" | "code">("visual");
  const [codeText, setCodeText] = useState("");
  const [codeError, setCodeError] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [importText, setImportText] = useState("");
  const [importing, setImporting] = useState(false);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [showClearConfirm, setShowClearConfirm] = useState(false);

  const totalChars = lines.reduce((s, l) => s + l.text.length, 0);

  /** 比较两个情感配置是否完全一致（用于判断是否偏离角色默认值）。
   *  vector 用近似比较避免浮点误差。
   */
  const emotionEquals = (a: EmotionConfig, b: EmotionConfig): boolean => {
    if (a.mode !== b.mode) return false;
    if ((a.audio_path ?? null) !== (b.audio_path ?? null)) return false;
    if (Math.abs((a.weight ?? 0) - (b.weight ?? 0)) > 0.001) return false;
    if ((a.text ?? null) !== (b.text ?? null)) return false;
    if (!!a.random !== !!b.random) return false;
    const va = a.vector || [];
    const vb = b.vector || [];
    if (va.length !== vb.length) return false;
    for (let i = 0; i < va.length; i++) {
      if (Math.abs((va[i] ?? 0) - (vb[i] ?? 0)) > 0.001) return false;
    }
    return true;
  };

  const lineToJSON = (line: PodcastLine) => {
    const obj: Record<string, unknown> = {
      text: line.text,
      role: line.speaker,
    };

    // 情感：JSONL 中原本写了情感 或 用户在可视化中修改了情感（偏离角色默认值）时才输出
    const speakerCfg = line.speaker === "A" ? speakers.A : speakers.B;
    const baseEmo = speakerCfg.emotion;
    const emotionChanged = !emotionEquals(line.emotion, baseEmo);
    if (line.emotion_from_code || emotionChanged) {
      obj.emotion = line.emotion;
    }

    // 行级静音：JSONL 中原本写了 或 用户在可视化中设置过时才输出
    if (line.silence_from_code || line.silence_after_ms !== undefined) {
      obj.silence_after_ms = line.silence_after_ms ?? 0;
    }

    return obj;
  };

  const serializeLines = (items: PodcastLine[]) =>
    items.map(line => JSON.stringify(lineToJSON(line))).join("\n");

  const syncCodeFromLines = () => {
    setCodeText(serializeLines(lines));
    setCodeError(null);
  };

  const applyCode = () => {
    if (!codeText.trim()) {
      onChange([]);
      setCodeError(null);
      return;
    }
    const parsed = parseJSONL(codeText, speakers);
    const sourceLines = codeText.split("\n").filter(line => line.trim());
    if (parsed.length !== sourceLines.length) {
      setCodeError(`代码中有无法解析的行：已识别 ${parsed.length}/${sourceLines.length} 行`);
      return;
    }
    onChange(parsed);
    setCodeError(null);
  };

  const add = (speaker: "A" | "B") =>
    onChange([...lines, makeLine(speaker, "", speakers[speaker].emotion)]);
  const remove = (id: string) => onChange(lines.filter(l => l.id !== id));
  const duplicate = (id: string) => {
    const idx = lines.findIndex(l => l.id === id);
    if (idx < 0) return;
    const copy = { ...lines[idx], id: Math.random().toString(36).slice(2, 10), emotion: { ...lines[idx].emotion, vector: [...lines[idx].emotion.vector] } };
    const next = [...lines];
    next.splice(idx + 1, 0, copy);
    onChange(next);
  };
  const move = (id: string, dir: -1 | 1) => {
    const idx = lines.findIndex(l => l.id === id);
    const target = idx + dir;
    if (idx < 0 || target < 0 || target >= lines.length) return;
    const next = [...lines];
    [next[idx], next[target]] = [next[target], next[idx]];
    onChange(next);
  };
  const updateLine = (id: string, patch: Partial<PodcastLine>) =>
    onChange(lines.map(l => l.id === id ? { ...l, ...patch } : l));
  const toggleEmo = (id: string) => {
    const next = new Set(collapsedEmos);
    next.has(id) ? next.delete(id) : next.add(id);
    setCollapsedEmos(next);
  };

  // 拖拽排序
  const onDragStart = (i: number) => setDragIndex(i);
  const onDragOver = (e: DragEvent, i: number) => { e.preventDefault(); if (dragIndex !== null && dragIndex !== i) {
    const next = [...lines]; const [item] = next.splice(dragIndex, 1); next.splice(i, 0, item);
    setDragIndex(i); onChange(next);
  }};
  const onDragEnd = () => setDragIndex(null);

  const fileRef = useRef<HTMLInputElement>(null);

  const handleImport = async () => {
    if (!importText.trim()) return;
    setImporting(true);
    try {
      const trimmed = importText.trim();
      let imported: PodcastLine[];
      let documentConfig: any = null;
      if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
        // 支持 JSONL，以及单个 JSON 文档/数组格式，前端直接解析。
        // 完整队列 JSON 还要把顶层配置同步给左侧面板。
        try {
          const parsed = JSON.parse(trimmed);
          if (parsed && !Array.isArray(parsed) && Array.isArray(parsed.lines)) {
            documentConfig = parsed;
          }
        } catch {
          // JSONL 由 parseJSONL 逐行解析。
        }
        imported = parseJSONL(importText, speakers);
      } else {
        // 纯文本格式，调用后端解析
        imported = await onImport(importText);
      }
      if (imported.length > 0) {
        onChange(documentConfig ? imported : [...lines, ...imported]);
        if (documentConfig && onImportConfig) {
          onImportConfig({
            voices: documentConfig.voices,
            silence: documentConfig.silence,
            params: documentConfig.params,
            lines: imported,
            projectName: documentConfig.project_name,
          });
        }
        setImportText("");
        setShowImport(false);
      }
    } finally { setImporting(false); }
  };

  const handleFileImport = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      setImportText(String(reader.result || ""));
    };
    reader.readAsText(file);
  };

  const speakerColor = (spk: "A" | "B") => spk === "A"
    ? { bg: "bg-indigo-500", text: "text-indigo-600", border: "border-indigo-200", light: "bg-indigo-50" }
    : { bg: "bg-teal-500", text: "text-teal-600", border: "border-teal-200", light: "bg-teal-50" };

  return (
    <div className="flex flex-col h-full">
      {/* 工具栏 */}
      <div className="flex items-center justify-between px-1 py-2 shrink-0">
        <div className="flex items-center gap-2">
          <ListChecks className="w-4 h-4 text-gray-400" />
          <span className="text-sm font-semibold text-gray-700">对话脚本</span>
          <Badge color="gray">{lines.length} 行</Badge>
          <Badge color="gray">{totalChars} 字</Badge>
        </div>
        <div className="flex gap-1.5">
          <Button variant="outline" size="sm" icon={FileText} onClick={() => setShowImport(true)}>
            批量导入
          </Button>
          {lines.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              icon={Eraser}
              onClick={() => setShowClearConfirm(true)}
              className="text-red-500 hover:bg-red-50 hover:border-red-300"
            >
              一键清空
            </Button>
          )}
          <Button size="sm" icon={Plus} onClick={() => add("A")} className="bg-indigo-600">
            A 发言
          </Button>
          <Button size="sm" icon={Plus} onClick={() => add("B")} className="bg-teal-600 hover:bg-teal-700">
            B 发言
          </Button>
        </div>
      </div>

      {/* 同一份脚本数据的双视图 */}
      <div className="flex items-center justify-between px-1 pb-2">
        <div className="inline-flex rounded-lg border border-gray-200 bg-gray-100 p-0.5">
          <button
            type="button"
            onClick={() => setScriptView("visual")}
            className={cn("px-3 py-1.5 rounded-md text-xs font-medium", scriptView === "visual" ? "bg-white text-gray-800 shadow-sm" : "text-gray-500")}
          >
            可视化编辑
          </button>
          <button
            type="button"
            onClick={() => { syncCodeFromLines(); setScriptView("code"); }}
            className={cn("px-3 py-1.5 rounded-md text-xs font-medium", scriptView === "code" ? "bg-white text-gray-800 shadow-sm" : "text-gray-500")}
          >
            JSONL 代码
          </button>
        </div>
        {scriptView === "code" && <span className="text-[11px] text-gray-400">每行一个 JSON 对象，修改后点击应用</span>}
      </div>

      {scriptView === "code" ? (
        <div className="flex-1 min-h-0 px-1 pb-2 flex flex-col gap-2">
          <textarea
            value={codeText}
            onChange={e => { setCodeText(e.target.value); setCodeError(null); }}
            spellCheck={false}
            aria-label="JSONL 对话脚本代码"
            className="flex-1 min-h-[360px] w-full resize-none rounded-xl border border-gray-700 bg-gray-950 px-4 py-3 font-mono text-xs leading-6 text-gray-100 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            placeholder={'{"text":"大家好","role":"A"}\n{"text":"今天我们来聊聊AI","role":"B","emotion_text":"relaxed, cheerful","emotion_weight":0.7,"silence_after_ms":350}'}
          />
          {codeError && <p className="text-xs text-red-600">{codeError}</p>}
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-gray-400">必填：text、role（A/B）；可选：emotion、silence_after_ms 等</span>
            <Button size="sm" onClick={applyCode}>应用到可视化</Button>
          </div>
        </div>
      ) : (
      /* 对话列表 */
      <div className="flex-1 overflow-y-auto scrollbar-thin px-1 pb-2 space-y-2">
        {lines.length === 0 ? (
          <Card className="border-dashed">
            <EmptyState
              icon={FileText}
              title="还没有对话内容"
              hint='点击 "A 发言" / "B 发言" 添加对话，或用"批量导入"快速生成'
            />
          </Card>
        ) : (
          lines.map((line, i) => {
            const sc = speakerColor(line.speaker);
            return (
              <div
                key={line.id}
                draggable
                onDragStart={() => onDragStart(i)}
                onDragOver={e => onDragOver(e, i)}
                onDragEnd={onDragEnd}
                className={cn(
                  "rounded-xl border bg-white p-3 transition-shadow hover:shadow-sm",
                  sc.border, dragIndex === i && "opacity-50"
                )}
              >
                <div className="flex items-center gap-2 mb-2">
                  <GripVertical className="w-4 h-4 text-gray-300 cursor-grab active:cursor-grabbing" />

                  {/* 说话人切换 */}
                  <div className="flex rounded-lg overflow-hidden border border-gray-200">
                    <button
                      onClick={() => updateLine(line.id, { speaker: "A" })}
                      className={cn("px-2.5 h-7 text-xs font-medium transition-colors",
                        line.speaker === "A" ? "bg-indigo-500 text-white" : "bg-white text-gray-400 hover:bg-gray-50")}
                    >
                      {speakers.A.name || "A"}
                    </button>
                    <button
                      onClick={() => updateLine(line.id, { speaker: "B" })}
                      className={cn("px-2.5 h-7 text-xs font-medium transition-colors",
                        line.speaker === "B" ? "bg-teal-500 text-white" : "bg-white text-gray-400 hover:bg-gray-50")}
                    >
                      {speakers.B.name || "B"}
                    </button>
                  </div>

                  <span className="text-xs text-gray-300">#{i + 1}</span>
                  <span className={cn("text-xs", sc.text)}>{line.text.length} 字</span>

                  <div className="flex-1" />

                  {/* 行操作 */}
                  <button onClick={() => move(line.id, -1)} disabled={i === 0}
                    className="p-1 rounded hover:bg-gray-100 disabled:opacity-30">
                    <ArrowUp className="w-3.5 h-3.5 text-gray-400" />
                  </button>
                  <button onClick={() => move(line.id, 1)} disabled={i === lines.length - 1}
                    className="p-1 rounded hover:bg-gray-100 disabled:opacity-30">
                    <ArrowDown className="w-3.5 h-3.5 text-gray-400" />
                  </button>
                  <button onClick={() => duplicate(line.id)} className="p-1 rounded hover:bg-gray-100">
                    <Copy className="w-3.5 h-3.5 text-gray-400" />
                  </button>
                  <button onClick={() => remove(line.id)} className="p-1 rounded hover:bg-red-50">
                    <Trash2 className="w-3.5 h-3.5 text-red-400" />
                  </button>
                </div>

                <Textarea
                  value={line.text}
                  onChange={e => updateLine(line.id, { text: e.target.value })}
                  placeholder="输入这一行的台词..."
                  className="min-h-[44px] text-sm"
                />

                <div className="mt-2">
                  <EmotionEditor
                    emotion={line.emotion}
                    onChange={emo => updateLine(line.id, { emotion: emo })}
                    voiceFiles={voiceFiles}
                    collapsed={!collapsedEmos.has(line.id)}
                    onToggle={() => toggleEmo(line.id)}
                  />
                </div>
              </div>
            );
          })
        )}
      </div>
      )}

      {/* 批量导入弹窗 */}
      {showImport && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setShowImport(false)}>
          <Card className="w-full max-w-lg mx-4" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
              <h3 className="text-sm font-semibold text-gray-800">批量导入对话</h3>
              <button onClick={() => setShowImport(false)} className="text-gray-400 hover:text-gray-600">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 space-y-3">
              {/* 格式说明 */}
              <div className="rounded-lg bg-indigo-50 border border-indigo-100 p-3 space-y-1.5">
                <p className="text-xs font-medium text-indigo-700">支持两种格式（自动识别）：</p>
                <p className="text-[11px] text-indigo-600">
                  <strong>1. JSON / JSONL 格式</strong>：支持每行一个 JSON 对象，也支持包含 <code className="px-1 bg-white/60 rounded">lines</code> 数组的 WebUI 队列文件；对话项必填 <code className="px-1 bg-white/60 rounded">text</code>，可用 <code className="px-1 bg-white/60 rounded">role</code> 或 <code className="px-1 bg-white/60 rounded">speaker</code> 指定说话人。
                  可选字段：<code className="px-1 bg-white/60 rounded">emotion</code>、<code className="px-1 bg-white/60 rounded">silence_after_ms</code> 等，不填则使用界面角色和参数。
                  兼容旧格式的 <code className="px-1 bg-white/60 rounded">voice</code> / <code className="px-1 bg-white/60 rounded">speaker</code> 字段。
                </p>
                <p className="text-[11px] text-indigo-600">
                  <strong>2. 纯文本格式</strong>：每行一段对话，用 <code className="px-1 bg-white/60 rounded">A:</code> 或 <code className="px-1 bg-white/60 rounded">B:</code> 开头指定说话人。
                </p>
              </div>

              {/* 文件上传 */}
              <div className="flex items-center gap-2">
                <input
                  ref={fileRef}
                  type="file"
                  accept=".jsonl,.txt,.json"
                  className="hidden"
                  onChange={e => { const f = e.target.files?.[0]; if (f) handleFileImport(f); e.target.value = ""; }}
                />
                <Button variant="outline" size="sm" icon={UploadCloud} onClick={() => fileRef.current?.click()}>
                  选择文件
                </Button>
                <span className="text-[11px] text-gray-400">支持 .json / .jsonl / .txt 文件</span>
              </div>

              <Textarea
                value={importText}
                onChange={e => setImportText(e.target.value)}
                placeholder={'{"text":"大家好，欢迎收听今天的节目。","role":"A"}\n{"text":"今天我们来聊聊人工智能。","role":"B","emotion_text":"relaxed, cheerful","emotion_weight":0.7,"silence_after_ms":350}\n\n- 或纯文本格式 -\nA: 大家好，欢迎收听今天的节目。\nB: 今天我们来聊聊人工智能。'}
                className="min-h-[200px] font-mono text-xs"
                autoFocus
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => setShowImport(false)}>取消</Button>
                <Button onClick={handleImport} disabled={importing || !importText.trim()}>
                  {importing ? "导入中..." : "追加导入"}
                </Button>
              </div>
            </div>
          </Card>
        </div>
      )}

      {/* 一键清空确认弹窗 */}
      {showClearConfirm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setShowClearConfirm(false)}>
          <Card className="w-full max-w-sm mx-4" onClick={e => e.stopPropagation()}>
            <div className="p-5 space-y-3 text-center">
              <div className="w-12 h-12 mx-auto rounded-full bg-red-50 flex items-center justify-center">
                <Eraser className="w-5 h-5 text-red-500" />
              </div>
              <h3 className="text-sm font-semibold text-gray-800">确认清空所有脚本？</h3>
              <p className="text-xs text-gray-500">
                当前共 {lines.length} 行对话，清空后无法撤销。
              </p>
              <div className="flex justify-center gap-2 pt-1">
                <Button variant="outline" onClick={() => setShowClearConfirm(false)}>取消</Button>
                <Button
                  className="bg-red-500 hover:bg-red-600"
                  icon={Trash2}
                  onClick={() => { onChange([]); setShowClearConfirm(false); }}
                >
                  确认清空
                </Button>
              </div>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
