import { useState, useRef, useMemo, type DragEvent } from "react";
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
  onImportConfig?: (config: {
    voices?: Record<string, string>;
    silence?: Partial<{ within_segment: number; between_lines: number; speaker_switch: number }>;
    params?: Record<string, unknown>;
    lines?: PodcastLine[];
    projectName?: string;
  }) => void;
}

/** 可导入格式自动识别：JSON 文档 / JSONL / 纯文本。 */
export type ImportFormat = "json" | "jsonl" | "text";

interface ImportResult {
  format: ImportFormat;
  lines: PodcastLine[];
  /** 无法解析的行（仅 JSON/JSONL 模式），将被跳过 */
  badLines: { no: number; snippet: string }[];
  /** JSON 文档模式下的顶层配置（voices/silence/params/project_name） */
  docConfig: any | null;
}

const genId = () => Math.random().toString(36).slice(2, 10);

/** 行前缀匹配：^([^:：]{1,12})[:：] —— 限 12 字符防止 "https://..." 误切 */
const SPEAKER_PREFIX_RE = /^([^:：]{1,12})[:：]\s*(.*)$/;

/** 解析导入文本（JSON 文档 / JSONL / 纯文本三格式自动识别）。
 *  JSON 模式：支持每行一个 JSON 对象，也支持包含 lines 数组的 WebUI 队列文件。
 *  纯文本模式：每行一段对话，"A:" / "B:" / 角色名前缀指定说话人，无前缀交替分配。
 */
function parseImportText(text: string, speakers: { A: SpeakerConfig; B: SpeakerConfig }): ImportResult {
  const t = text.trim();
  const badLines: { no: number; snippet: string }[] = [];
  const lines: PodcastLine[] = [];
  let format: ImportFormat = "text";
  let docConfig: any | null = null;
  let rawLines: string[] | null = null;

  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      const doc = JSON.parse(t);
      rawLines = (Array.isArray(doc) ? doc : (doc && Array.isArray(doc.lines) ? doc.lines : [doc]))
        .map((record: unknown) => JSON.stringify(record));
      if (doc && !Array.isArray(doc) && Array.isArray(doc.lines)) docConfig = doc;
      format = "json";
    } catch {
      rawLines = null; // JSONL 由逐行解析兜底
    }
  }
  if (rawLines === null) {
    rawLines = t.split("\n");
    format = t.startsWith("{") ? "jsonl" : "text";
  }

  if (format !== "text") {
    for (let idx = 0; idx < rawLines.length; idx++) {
      const trimmed = rawLines[idx].trim();
      if (!trimmed) continue;
      if (!trimmed.startsWith("{")) {
        badLines.push({ no: idx + 1, snippet: trimmed.slice(0, 40) });
        continue;
      }
      try {
        const obj = JSON.parse(trimmed);
        if (!obj.text) {
          badLines.push({ no: idx + 1, snippet: trimmed.slice(0, 40) });
          continue;
        }

        // 判断说话人：优先 role 字段，其次 speaker，最后 voice（兼容旧格式）；识别不出 = 未标注
        let speaker: "A" | "B" | null = null;
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
        const baseEmo = speakers[speaker ?? "A"].emotion;
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

        const hasSilenceField = Number.isFinite(Number(obj.silence_after_ms));
        lines.push({
          id: genId(),
          speaker,
          text: String(obj.text),
          emotion: emo,
          emotion_from_code: hasEmotionField,
          silence_after_ms: hasSilenceField ? Math.max(0, Number(obj.silence_after_ms)) : undefined,
          silence_from_code: hasSilenceField,
        });
      } catch {
        badLines.push({ no: idx + 1, snippet: trimmed.slice(0, 40) });
      }
    }
  } else {
    // 纯文本："A:" / "B:" / 角色名前缀指定说话人；无前缀/未识别前缀 = 未标注行（提交前需手动标注）
    for (const raw of rawLines) {
      const line = raw.trim();
      if (!line) continue;
      let speaker: "A" | "B" | null = null;
      let content = line;
      const m = line.match(SPEAKER_PREFIX_RE);
      if (m) {
        const prefix = m[1].trim();
        content = m[2].trim();
        const nameA = (speakers.A.name || "A").trim();
        const nameB = (speakers.B.name || "B").trim();
        if (prefix === nameA || prefix.toUpperCase() === "A") speaker = "A";
        else if (prefix === nameB || prefix.toUpperCase() === "B") speaker = "B";
      }
      if (!content) continue;
      const baseEmo = speakers[speaker ?? "A"].emotion;
      lines.push({
        id: genId(),
        speaker,
        text: content,
        emotion: { ...baseEmo, vector: [...baseEmo.vector] },
        emotion_from_code: false,
        silence_after_ms: undefined,
        silence_from_code: false,
      });
    }
  }
  return { format, lines, badLines, docConfig };
}

export function ScriptEditor({ lines, speakers, voiceFiles, onChange, onImportConfig }: ScriptEditorProps) {
  const [collapsedEmos, setCollapsedEmos] = useState<Set<string>>(new Set());
  const [showImport, setShowImport] = useState(false);
  const [importText, setImportText] = useState("");
  const [importMode, setImportMode] = useState<"append" | "replace">("append");
  const [fileLoading, setFileLoading] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [showClearConfirm, setShowClearConfirm] = useState(false);

  const totalChars = lines.reduce((s, l) => s + l.text.length, 0);

  // 实时解析预览：输入变化即重解析，弹窗内直接反馈行数/分布/错误行
  const preview = useMemo(
    () => (importText.trim() ? parseImportText(importText, speakers) : null),
    [importText, speakers],
  );

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

  const handleImport = () => {
    if (!preview || preview.lines.length === 0) {
      setImportError("没有可导入的内容：请检查文本格式，或查看上方解析提示");
      return;
    }
    onChange(importMode === "replace" ? preview.lines : [...lines, ...preview.lines]);
    // JSON 文档模式的顶层配置同步给左侧面板（仅替换模式应用，避免追加时覆盖配置）
    if (preview.docConfig && onImportConfig) {
      onImportConfig({
        voices: preview.docConfig.voices,
        silence: preview.docConfig.silence,
        params: preview.docConfig.params,
        lines: preview.lines,
        projectName: preview.docConfig.project_name,
      });
    }
    setImportText("");
    setImportError(null);
    setShowImport(false);
  };

  /** doc/docx/pdf 已下线，仅支持 .txt 直接读取 */
  const handleFileImport = async (file: File) => {
    setImportError(null);
    setFileLoading(true);
    try {
      setImportText(await file.text());
    } catch (e: any) {
      setImportError(`读取文件失败：${e.message}`);
    } finally {
      setFileLoading(false);
    }
  };

  const speakerColor = (spk: "A" | "B" | null) => spk === "A"
    ? { bg: "bg-indigo-500", text: "text-indigo-600", border: "border-indigo-200", light: "bg-indigo-50" }
    : spk === "B"
    ? { bg: "bg-teal-500", text: "text-teal-600", border: "border-teal-200", light: "bg-teal-50" }
    : { bg: "bg-gray-300", text: "text-gray-500", border: "border-gray-200", light: "bg-gray-50" };

  return (
    <div className="flex flex-col h-full">
      {/* 工具栏 */}
      <div className="flex items-center justify-between gap-2 px-1 py-2 shrink-0">
        <div className="flex items-center gap-2 whitespace-nowrap shrink-0">
          <ListChecks className="w-4 h-4 text-gray-400 shrink-0" />
          <span className="text-sm font-semibold text-gray-700">对话脚本</span>
          <Badge color="gray">{lines.length} 行</Badge>
          <Badge color="gray">{totalChars} 字</Badge>
        </div>
        <div className="flex gap-1.5 flex-wrap justify-end min-w-0">
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
          <Button size="sm" icon={Plus} onClick={() => onChange([...lines, makeLine(null, "", speakers.A.emotion)])} className="bg-indigo-600">
            新行
          </Button>
        </div>
      </div>

      {/* 对话列表 */}
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

                  {/* 主持人标识块：标注本行归属；点击切换 A/B（不可删除） */}
                  <button
                    onClick={() => updateLine(line.id, { speaker: line.speaker === "A" ? "B" : "A" })}
                    title="点击切换主持人"
                    className={cn(
                      "flex items-center gap-1.5 px-2.5 h-7 rounded-lg border text-xs font-medium transition-colors shrink-0",
                      line.speaker === "A" && "bg-indigo-50 border-indigo-200 text-indigo-700 hover:bg-indigo-100",
                      line.speaker === "B" && "bg-teal-50 border-teal-200 text-teal-700 hover:bg-teal-100",
                      line.speaker === null && "bg-gray-50 border-dashed border-gray-300 text-gray-400 hover:bg-gray-100",
                    )}
                  >
                    <span className={cn("w-2 h-2 rounded-full", sc.bg)} />
                    {line.speaker === "A" && (speakers.A.name || "主持人A")}
                    {line.speaker === "B" && (speakers.B.name || "主持人B")}
                    {line.speaker === null && "未指定主持人"}
                  </button>

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

                {/* 行内标注：+ A 发言 / + B 发言（已有标识时不可重复插入）+ 情感块 */}
                <div className="mt-2 flex items-stretch gap-1.5">
                  <div className="flex flex-col gap-1 shrink-0">
                    <button
                      onClick={() => updateLine(line.id, { speaker: "A" })}
                      disabled={line.speaker !== null}
                      className="px-2 h-[26px] rounded-md border border-indigo-200 bg-indigo-50 text-indigo-600 text-[0.7rem] font-medium hover:bg-indigo-100 disabled:opacity-30 disabled:cursor-not-allowed whitespace-nowrap"
                      title={line.speaker !== null ? "本行已有主持人标识" : "标注为主持人A"}
                    >
                      + A 发言
                    </button>
                    <button
                      onClick={() => updateLine(line.id, { speaker: "B" })}
                      disabled={line.speaker !== null}
                      className="px-2 h-[26px] rounded-md border border-teal-200 bg-teal-50 text-teal-600 text-[0.7rem] font-medium hover:bg-teal-100 disabled:opacity-30 disabled:cursor-not-allowed whitespace-nowrap"
                      title={line.speaker !== null ? "本行已有主持人标识" : "标注为主持人B"}
                    >
                      + B 发言
                    </button>
                  </div>
                  <div className="flex-1 min-w-0">
                    <EmotionEditor
                      emotion={line.emotion}
                      onChange={emo => updateLine(line.id, { emotion: emo })}
                      voiceFiles={voiceFiles}
                      collapsed={!collapsedEmos.has(line.id)}
                      onToggle={() => toggleEmo(line.id)}
                    />
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* 批量导入弹窗 */}
      {showImport && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setShowImport(false)}>
          <Card className="w-full max-w-xl mx-4 max-h-[90vh] overflow-y-auto scrollbar-thin" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100 sticky top-0 bg-white rounded-t-xl">
              <h3 className="text-sm font-semibold text-gray-800">批量导入对话</h3>
              <button onClick={() => setShowImport(false)} className="text-gray-400 hover:text-gray-600">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div
              className="p-4 space-y-3"
              onDragOver={e => { e.preventDefault(); setDragActive(true); }}
              onDragLeave={() => setDragActive(false)}
              onDrop={e => {
                e.preventDefault(); setDragActive(false);
                const f = e.dataTransfer.files?.[0];
                if (f) handleFileImport(f);
              }}
            >
              {/* 格式说明 */}
              <div className="rounded-lg bg-indigo-50 border border-indigo-100 p-3">
                <p className="text-[0.75rem] text-indigo-600">
                  <strong>纯文本</strong>：每行一段对话，用 <code className="px-1 bg-white/60 rounded">A:</code> / <code className="px-1 bg-white/60 rounded">B:</code>（A 代表 {speakers.A.name || "主持人A"}，B 代表 {speakers.B.name || "主持人B"}）开头指定说话人；无前缀的行导入后未标注，可在行内点击「+ A 发言」/「+ B 发言」指定。
                </p>
              </div>

              {/* 文件上传（点击 / 拖拽） */}
              <div className={cn(
                "flex items-center gap-2 rounded-lg border border-dashed px-3 py-2.5 transition-colors",
                dragActive ? "border-indigo-400 bg-indigo-50" : "border-gray-200",
              )}>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".txt"
                  className="hidden"
                  onChange={e => { const f = e.target.files?.[0]; if (f) handleFileImport(f); e.target.value = ""; }}
                />
                <Button variant="outline" size="sm" icon={UploadCloud} onClick={() => fileRef.current?.click()} disabled={fileLoading}>
                  {fileLoading ? "读取中..." : "选择文件"}
                </Button>
                <span className="text-[0.75rem] text-gray-400">或把 .txt 文件拖到这里</span>
              </div>

              <Textarea
                value={importText}
                onChange={e => { setImportText(e.target.value); setImportError(null); }}
                placeholder={'A:哈喽,大家好，欢迎收听我们的播客。\nB:大家好，我是晓风。\nA:今天咱们聊一个很现实的话题。'}
                className="min-h-[160px] font-mono text-xs"
                autoFocus
              />

              {/* 实时解析预览 */}
              {preview && (
                <div className={cn(
                  "rounded-lg border p-3 space-y-1.5",
                  preview.lines.length > 0 ? "bg-gray-50 border-gray-200" : "bg-red-50 border-red-200",
                )}>
                  {preview.lines.length > 0 ? (
                    <>
                      <p className="text-xs text-gray-700">
                        解析出 <strong>{preview.lines.length}</strong> 行对话
                        （{speakers.A.name || "A"} {preview.lines.filter(l => l.speaker === "A").length} 行 ·{" "}
                        {speakers.B.name || "B"} {preview.lines.filter(l => l.speaker === "B").length} 行）
                        <Badge color="blue" className="ml-2">{preview.format === "json" ? "JSON" : preview.format === "jsonl" ? "JSONL" : "纯文本"}</Badge>
                      </p>
                      {preview.badLines.length > 0 && (
                        <p className="text-xs text-amber-600">
                          ⚠ {preview.badLines.length} 行无法解析将被跳过：
                          {preview.badLines.slice(0, 3).map(b => `第${b.no}行「${b.snippet}…`).join("、")}
                          {preview.badLines.length > 3 && " 等"}
                        </p>
                      )}
                      {importMode === "replace" && lines.length > 0 && (
                        <p className="text-xs text-amber-600">替换模式：导入后将覆盖现有 {lines.length} 行对话</p>
                      )}
                    </>
                  ) : (
                    <p className="text-xs text-red-600">没有解析出任何对话行，请检查格式</p>
                  )}
                </div>
              )}

              {importError && <p className="text-xs text-red-600">{importError}</p>}

              {/* 导入模式 + 操作 */}
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="inline-flex rounded-lg border border-gray-200 bg-gray-100 p-0.5">
                  <button
                    type="button"
                    onClick={() => setImportMode("append")}
                    className={cn("px-3 py-1.5 rounded-md text-xs font-medium", importMode === "append" ? "bg-white text-gray-800 shadow-sm" : "text-gray-500")}
                  >
                    追加到现有
                  </button>
                  <button
                    type="button"
                    onClick={() => setImportMode("replace")}
                    className={cn("px-3 py-1.5 rounded-md text-xs font-medium", importMode === "replace" ? "bg-white text-gray-800 shadow-sm" : "text-gray-500")}
                  >
                    替换现有
                  </button>
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" onClick={() => setShowImport(false)}>取消</Button>
                  <Button
                    onClick={handleImport}
                    disabled={fileLoading || !preview || preview.lines.length === 0}
                  >
                    {importMode === "append" ? `追加导入${preview?.lines.length ? `（${preview.lines.length} 行）` : ""}` : `替换导入${preview?.lines.length ? `（${preview.lines.length} 行）` : ""}`}
                  </Button>
                </div>
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
