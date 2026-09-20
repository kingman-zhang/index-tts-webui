/**
 * 配音模式编辑器：contentEditable 所见即所得画布（MiniMax 式作用域模型）。
 * - 情绪芯片【喜悦】是作用域起点：其后直到【/】终止符/下一个芯片/行尾的文字带淡色高亮，
 *   一行内可有多个情绪段；已覆盖区域内禁止再插情绪（不支持嵌套/叠加）。
 *   选区套情绪时作用域终点以【/】写进标记文本（换情绪/刷新/撤销/提交都不丢范围）。
 * - 停顿 [pause:1] 是点状芯片，可插在任意位置（含作用域内）。
 * - 点击芯片弹出浮层：情绪芯片可更换情绪或删除；停顿芯片可选预设时长
 *   （0.25/0.5/1.0/1.5s）、自定义（两位小数，0.05–5s）或删除。
 * - 光标紧邻芯片按 Backspace/Delete 时整块删除，所属行重渲染以更新作用域归属。
 * - 程序化 DOM 修改会破坏浏览器原生 undo 栈，因此自维护 undo/redo
 *   （Cmd/Ctrl+Z 撤回、Cmd/Ctrl+Shift+Z 或 Ctrl+Y 重做），恢复文本与光标。
 * 画布 DOM 是显示层，标记文本（props.text）仍是唯一真源。
 */
import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Pause,
  Sparkles,
  Loader2,
  AlertCircle,
  X,
  FileUp,
  CloudUpload,
} from "lucide-react";
import {
  MONO_EMOTION_META,
  MONO_EMOTION_MARKERS,
  MONO_SCOPE_END,
  textToMonoLines,
  textToPodcastSegments,
} from "@/types";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";

/** 导入文档限制：≤20MB、解析后 ≤1 万字 */
const IMPORT_MAX_BYTES = 20 * 1024 * 1024;

interface MonoEditorProps {
  text: string;
  onChange: (text: string) => void;
  onGenerate: () => void;
  canGenerate: boolean;
  generating: boolean;
  error: string | null;
  /** 积分预估（MEMBER_ENFORCE=1 时由页面传入；null = 不展示） */
  pointsInfo?: { cost: number; balance: number | null } | null;
  /** 播客模式：启用主持人标识块（行首【A】/【B】芯片，点击切换 A↔B，不可删除），
   *  值为两位主持人的显示名；不传 = 单人配音模式 */
  speakers?: { A: string; B: string };
  /** 导入文档解析后的纯文本 → 画布文本转换（播客模式用于识别 A:/B: 前缀） */
  importTransform?: (raw: string) => string;
  /** 画布区标题（如"对话脚本"；不传则不显示） */
  title?: string;
}

type EmotionMeta = { label: string; chip: string; dot: string; scope: string };

// ─── 标记文本 ↔ 富文本 DOM 互转 ─────────────────────────────

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 主持人标识 marker → A/B（其余返回 null） */
function speakerMarkerKey(marker: string): "A" | "B" | null {
  const m = marker.match(/^【([AB])】$/);
  return m ? (m[1] as "A" | "B") : null;
}

const PAUSE_TOKEN_RE = /\[pause:\s*([\d.]+)\s*\]/g;
const EMOTION_TOKEN_RE = /【[^【】]+】/g;

/** 秒数 → 标记字符串：两位小数、去掉多余尾零（0.50→"0.5"、1.00→"1"） */
function fmtSec(v: number): string {
  return String(Math.round(v * 100) / 100);
}

function pauseChipHtml(sec: string): string {
  return (
    `<span contenteditable="false" data-marker="[pause:${sec}]" ` +
    `class="mono-chip mono-chip-pause"><i class="not-italic" style="font-size:0.72em">⏸</i>${sec}s</span>`
  );
}

function emotionChipHtml(meta: EmotionMeta): string {
  return (
    `<span contenteditable="false" data-marker="【${meta.label}】" ` +
    `class="mono-chip ${meta.chip}">` +
    `<i class="not-italic w-1.5 h-1.5 rounded-full ${meta.dot}"></i>${escapeHtml(meta.label)}` +
    `</span>`
  );
}

/** 主持人标识块：A=靛蓝 / B=青绿（与左栏角色卡配色一致），徽标字母 + 显示名 */
function speakerChipHtml(key: "A" | "B", label: string): string {
  const badge =
    key === "A"
      ? "bg-indigo-500 text-white"
      : "bg-teal-500 text-white";
  return (
    `<span contenteditable="false" data-marker="【${key}】" ` +
    `class="mono-chip mono-chip-speaker ${key === "A" ? "border-indigo-200 bg-indigo-50 text-indigo-700" : "border-teal-200 bg-teal-50 text-teal-700"}">` +
    `<i class="not-italic w-4 h-4 rounded-full ${badge} text-[0.6rem] flex items-center justify-center font-bold">${key}</i>` +
    escapeHtml(label) +
    `</span>`
  );
}

/** 无情绪区间：[pause] 转芯片、未知【xx】按普通文本转义；【/】终止符不渲染 */
function renderPlain(txt: string, pauseAsChip: boolean): string {
  if (!txt) return "";
  let inner = "";
  let last = 0;
  for (const p of txt.matchAll(PAUSE_TOKEN_RE)) {
    const i = p.index ?? 0;
    inner += escapeHtml(txt.slice(last, i));
    inner += pauseAsChip ? pauseChipHtml(p[1]) : escapeHtml(p[0]);
    last = i + p[0].length;
  }
  inner += escapeHtml(txt.slice(last).split(MONO_SCOPE_END).join(""));
  return inner;
}

/**
 * 单行标记文本 → 行内 HTML。
 * 作用域终点 = 【/】终止符、下一个情绪标记、或行尾三者中最早出现的；
 * 无【/】时与旧语义一致（到下一个标记/行尾），旧草稿无缝兼容。
 */
function lineToHtml(line: string, speakerNames?: { A: string; B: string }): string {
  type Ev = { idx: number; end: number; kind: "emo" | "end" | "speaker"; meta?: EmotionMeta; speaker?: "A" | "B" };
  const events: Ev[] = [];
  for (const m of line.matchAll(EMOTION_TOKEN_RE)) {
    const idx = m.index ?? 0;
    if (m[0] === MONO_SCOPE_END) {
      events.push({ idx, end: idx + m[0].length, kind: "end" });
      continue;
    }
    const spk = speakerMarkerKey(m[0]);
    if (spk) {
      events.push({ idx, end: idx + m[0].length, kind: "speaker", speaker: spk });
      continue;
    }
    const value = MONO_EMOTION_MARKERS[m[0].slice(1, -1)];
    const meta = value ? MONO_EMOTION_META[value] : null;
    if (meta) events.push({ idx, end: idx + m[0].length, kind: "emo", meta });
  }
  events.sort((a, b) => a.idx - b.idx);

  let out = "";
  let pos = 0;
  let scopeMeta: EmotionMeta | null = null;
  const emit = (stop: number) => {
    if (stop > pos) {
      // 段内可能残留未知【xx】（保留为文本）；终止符/情绪标记已被段边界切开，不会出现
      const inner = renderPlain(line.slice(pos, stop), true);
      if (inner) {
        out += scopeMeta ? `<span class="mono-scope ${scopeMeta.scope}">${inner}</span>` : inner;
      }
    }
    pos = stop;
  };
  for (const ev of events) {
    emit(ev.idx);
    if (ev.kind === "emo") {
      out += emotionChipHtml(ev.meta!);
      scopeMeta = ev.meta!;
    } else if (ev.kind === "speaker") {
      out += speakerChipHtml(ev.speaker!, speakerNames?.[ev.speaker!] || `主持人${ev.speaker!}`);
      // 主持人标识不引入情绪作用域
    } else {
      scopeMeta = null; // 【/】：作用域显式终止，其后文字跟随音色
    }
    pos = ev.end;
  }
  emit(line.length);
  return out;
}

/** 标记文本 → 画布 innerHTML（每行一个 div，与 Chrome 的 Enter 行为一致） */
export function markerTextToHtml(text: string, speakerNames?: { A: string; B: string }): string {
  return text
    .split("\n")
    .map(line => `<div>${lineToHtml(line, speakerNames) || "<br>"}</div>`)
    .join("");
}

/** BR 的序列化语义：行 div 内的 BR 是空行占位符（空字符串），
 *  画布根级裸文本结构的 BR 才代表换行（"\n"）。三者（序列化/偏移统计/光标恢复）必须一致 */
function brSerChar(node: Node): string {
  const p = node.parentElement?.tagName;
  return p === "DIV" || p === "P" ? "" : "\n";
}

function serializeInline(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (!(node instanceof HTMLElement)) return "";
  const marker = node.getAttribute("data-marker");
  if (marker) return marker;
  if (node.tagName === "BR") return brSerChar(node);
  // 作用域 span 是模型的一部分：序列化时输出终止符，把作用域终点写进文本真源
  if (node.classList.contains("mono-scope")) {
    let inner = "";
    node.childNodes.forEach(n => {
      inner += serializeInline(n);
    });
    return inner + MONO_SCOPE_END;
  }
  let out = "";
  node.childNodes.forEach(n => {
    out += serializeInline(n);
  });
  return out;
}

/** 画布 DOM（或单个行容器）→ 标记文本；scope span 对序列化透明 */
function serializeCanvas(root: HTMLElement): string {
  const lines: string[] = [];
  let cur = "";
  const flush = () => {
    lines.push(cur);
    cur = "";
  };
  root.childNodes.forEach(n => {
    if (n instanceof HTMLBRElement) {
      flush();
      return;
    }
    if (n instanceof HTMLDivElement || n instanceof HTMLParagraphElement) {
      // 先关闭挂起的裸文本行：Chrome 粘贴/首行会产生"根级裸文本 + 行 div"混合结构，
      // 不先 flush 会把裸文本与该 div 的内容并进同一行（实测吞行 bug）
      if (cur.length > 0) flush();
      n.childNodes.forEach(c => {
        cur += serializeInline(c);
      });
      flush();
      return;
    }
    cur += serializeInline(n);
  });
  // 末节点是行 div/BR 时 cur 已 flush 过（为空），再 push 会凭空多出一个尾部换行
  if (cur.length > 0 || lines.length === 0) lines.push(cur);
  return lines.join("\n");
}
/** target 节点之前的标记文本长度（chip 按 marker 长度计；行边界计 1 个 \n，
 *  与 serializeCanvas 的分行规则严格一致，否则混合结构下偏移会错位） */
function serializeBefore(root: HTMLElement, target: Node): number {
  let acc = 0; // 已完成行的总长（含每行末尾的 \n）
  let cur = 0; // 当前行已累积长度
  const walk = (node: Node): boolean => {
    if (node === target) return true;
    if (node.nodeType === Node.TEXT_NODE) {
      cur += node.textContent?.length ?? 0;
      return false;
    }
    if (node instanceof HTMLElement) {
      const mk = node.getAttribute("data-marker");
      if (mk) {
        cur += mk.length;
        return false;
      }
      if (node.tagName === "BR") {
        cur += brSerChar(node).length;
        return false;
      }
      if (node.classList.contains("mono-scope")) {
        // 作用域 span：与 serializeCanvas（serializeInline）对齐，补计终止符长度，
        // 否则偏移会差 3×(前方 scope 数)，插入/删除定位错行
        for (const c of Array.from(node.childNodes)) {
          if (walk(c)) return true;
        }
        cur += MONO_SCOPE_END.length;
        return false;
      }
      if ((node.tagName === "DIV" || node.tagName === "P") && node.parentElement === root) {
        // 行容器：先关闭挂起的裸文本行（同 serializeCanvas）
        if (cur > 0) {
          acc += cur + 1;
          cur = 0;
        }
        for (const c of Array.from(node.childNodes)) {
          if (walk(c)) return true;
        }
        acc += cur + 1; // 行内容 + 行尾 \n（空行也占 1）
        cur = 0;
        return false;
      }
      for (const c of Array.from(node.childNodes)) {
        if (walk(c)) return true;
      }
    }
    return false;
  };
  walk(root);
  return acc + cur;
}

/** 当前光标的标记文本偏移（undo 恢复用）；不在画布内返回 null */
function markerOffsetAt(root: HTMLElement, container: Node, offset: number): number | null {
  if (!root.contains(container)) return null;
  if (container.nodeType === Node.TEXT_NODE) {
    return serializeBefore(root, container) + offset;
  }
  if (container instanceof HTMLElement) {
    const child = container.childNodes[offset] ?? null;
    if (child) return serializeBefore(root, child);
    // offset 落在容器末尾：取最后一个子节点序列化之后的位置。
    // 之前直接 serializeBefore(root, container) 会返回容器起点，
    // 导致"行尾/画布末尾"光标处的插入全部错位到行首/文档开头（实测 bug）
    const kids = container.childNodes;
    if (kids.length === 0) {
      return container === root ? serializeCanvas(root).length : serializeBefore(root, container);
    }
    const last = kids[kids.length - 1];
    const before = serializeBefore(root, last);
    if (last.nodeType === Node.TEXT_NODE) {
      return before + (last.textContent?.length ?? 0);
    }
    if (last instanceof HTMLElement) {
      const mk = last.getAttribute("data-marker");
      if (mk) return before + mk.length;
      if (last.tagName === "BR") return before + brSerChar(last).length;
      return before + serializeCanvas(last).length;
    }
    return before;
  }
  return null;
}

function caretOffsetIn(root: HTMLElement): number | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const r = sel.getRangeAt(0);
  return markerOffsetAt(root, r.startContainer, r.startOffset);
}

/** 把光标放到标记文本偏移 offset 处（chip 按整体计，落点在中间时放到其后；支持画布级跨行） */
function setCaretAtMarkerOffset(root: HTMLElement, offset: number): void {
  const sel = window.getSelection();
  if (!sel) return;
  let rest = offset;
  const set = (fn: () => Range) => {
    const r = fn();
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
  };
  const walk = (node: Node, isRoot: boolean): boolean => {
    if (node.nodeType === Node.TEXT_NODE) {
      const len = node.textContent?.length ?? 0;
      if (rest <= len) {
        const at = Math.min(rest, len);
        set(() => {
          const r = document.createRange();
          r.setStart(node, at);
          return r;
        });
        return true;
      }
      rest -= len;
      return false;
    }
    if (node instanceof HTMLElement) {
      const mk = node.getAttribute("data-marker");
      if (mk) {
        if (rest <= mk.length) {
          set(() => {
            const r = document.createRange();
            r.setStartAfter(node);
            return r;
          });
          return true;
        }
        rest -= mk.length;
        return false;
      }
      if (node.tagName === "BR") {
        rest -= brSerChar(node).length;
        return false;
      }
      // 行容器：递归内容后补换行长度（serializeCanvas 的行由块级元素边界决定）
      if ((node.tagName === "DIV" || node.tagName === "P") && !isRoot) {
        for (const c of Array.from(node.childNodes)) {
          if (walk(c, false)) return true;
        }
        rest -= 1;
        return false;
      }
      for (const c of Array.from(node.childNodes)) {
        if (walk(c, false)) return true;
      }
    }
    return false;
  };
  if (walk(root, true)) return;
  // 兜底：光标到末尾
  set(() => {
    const r = document.createRange();
    r.selectNodeContents(root);
    return r;
  });
}

// ─── 芯片整块删除 ───────────────────────────────────────────

function isChip(node: Node | null): node is HTMLElement {
  return (
    node instanceof HTMLElement &&
    node.getAttribute("data-marker") !== null &&
    node.getAttribute("contenteditable") === "false"
  );
}

const isScope = (node: Node | null): node is HTMLElement =>
  node instanceof HTMLElement && node.classList.contains("mono-scope");

/** HTML 字符串 → 元素 */
function htmlToElement(html: string): HTMLElement {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstChild as HTMLElement;
}

/** 光标紧邻方向上的芯片（穿透作用域 span）；没有则返回 null */
function adjacentChip(range: Range, dir: -1 | 1): HTMLElement | null {
  const { startContainer, startOffset } = range;
  if (startContainer.nodeType === Node.TEXT_NODE) {
    const len = startContainer.textContent?.length ?? 0;
    if (dir === -1 ? startOffset > 0 : startOffset < len) return null;
    const parent = startContainer.parentElement;
    if (!parent) return null;
    const idx = Array.from(parent.childNodes).indexOf(startContainer as ChildNode);
    const target = parent.childNodes[dir === -1 ? idx - 1 : idx + 1] ?? null;
    if (isChip(target)) return target;
    // 光标在 scope span 的首/尾：看 scope 外层的相邻节点
    if (!target && isScope(parent)) {
      const outer = dir === -1 ? parent.previousSibling : parent.nextSibling;
      if (isChip(outer)) return outer;
    }
    return null;
  }
  if (!(startContainer instanceof HTMLElement)) return null;
  const target = startContainer.childNodes[dir === -1 ? startOffset - 1 : startOffset] ?? null;
  if (isChip(target)) return target;
  // 指向的是空 scope：看 scope 外层的相邻节点
  if (isScope(target) && target.childNodes.length === 0) {
    const outer = dir === -1 ? target.previousSibling : target.nextSibling;
    if (isChip(outer)) return outer;
  }
  return null;
}

/** 节点所在的行容器：画布的行 div；裸文本结构（无行 div）时返回画布自身 */
function lineOfNode(node: Node, canvas: HTMLElement): HTMLElement | null {
  let el = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as HTMLElement);
  if (!el || !canvas.contains(el)) return null;
  if (el === canvas) return canvas;
  while (el.parentElement && el.parentElement !== canvas) el = el.parentElement;
  return el.parentElement === canvas ? el : null;
}

// ─── 组件 ───────────────────────────────────────────────────

const TOOL_BTN =
  "inline-flex items-center gap-1 h-8 px-3 rounded-full border border-gray-200 bg-white text-xs text-gray-600 whitespace-nowrap shrink-0 transition-colors hover:border-emerald-300 hover:bg-emerald-50 hover:text-emerald-700 active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed";

/** 停顿预设（秒），与 MiniMax 一致 + 自定义 */
const PAUSE_PRESETS = [0.25, 0.5, 1.0, 1.5];
const PAUSE_MIN = 0.05;
const PAUSE_MAX = 5;

type Popover =
  | { kind: "emotion"; chip: HTMLElement; rect: DOMRect }
  | { kind: "pause"; chip: HTMLElement; rect: DOMRect; sec: number };

interface HistoryEntry {
  text: string;
  caret: number | null;
}

export function MonoEditor({ text, onChange, onGenerate, canGenerate, generating, error, pointsInfo, speakers, importTransform, title }: MonoEditorProps) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const savedRange = useRef<Range | null>(null);
  const hintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const textRef = useRef(text);
  const undoStack = useRef<HistoryEntry[]>([]);
  const redoStack = useRef<HistoryEntry[]>([]);
  const [emoOpen, setEmoOpen] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [popover, setPopover] = useState<Popover | null>(null);
  const [customSec, setCustomSec] = useState("");
  const [customOpen, setCustomOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [importing, setImporting] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  /** 播客模式导入弹窗的文本框内容（.txt 文件内容 / 手动粘贴） */
  const [importText, setImportText] = useState("");

  const parsed = textToMonoLines(text);
  const podcastSegs = speakers ? textToPodcastSegments(text) : null;
  const totalChars = podcastSegs
    ? podcastSegs.reduce((acc, s) => acc + s.text.length, 0)
    : parsed.reduce(
        (acc, l) => acc + l.text.replace(/\[pause:\s*[\d.]+\s*\]/g, "").length,
        0
      );
  const segCount = podcastSegs ? podcastSegs.length : parsed.length;
  const isEmpty = text.trim().length === 0;
  const speakerNames = speakers
    ? {
        A: speakers.A || "主持人A",
        B: speakers.B || "主持人B",
      }
    : undefined;

  // ─── 导入文档 ─────────────────────────────────────────────
  // 播客模式：弹窗 = 提示语 + .txt 文件/拖拽 + 粘贴文本框，经 importTransform
  // （A:/B: 前缀 → 主持人标识块）后整体填入画布。
  // 单人模式：弹窗 = 文档上传（doc/docx/pdf/txt/md），走后端解析。

  /** 播客模式：把文本框内容导入画布（替换现有，非空时确认） */
  const applyImportText = (raw: string) => {
    if (!raw.trim()) {
      setImportError("没有可导入的内容：请在下方粘贴对话文本，或选择 .txt 文件");
      return;
    }
    if (textRef.current.trim() && !window.confirm("导入将替换当前文稿内容，是否继续？")) return;
    // 入 undo 栈，导入后可 Cmd+Z 撤回
    undoStack.current.push({ text: textRef.current, caret: null });
    if (undoStack.current.length > 100) undoStack.current.shift();
    redoStack.current = [];
    onChange(importTransform ? importTransform(raw) : raw);
    setImportOpen(false);
    setImportText("");
    setImportError(null);
  };

  /** 播客模式：读取 .txt 文件内容填入文本框（预览后再点「导入」生效） */
  const handleImportTxtFile = async (file: File) => {
    if (file.size > IMPORT_MAX_BYTES) {
      setImportError("文件超过 20MB 上限");
      return;
    }
    setImportError(null);
    setImporting(true);
    try {
      setImportText(await file.text());
    } catch (e: any) {
      setImportError(`读取失败：${e.message}`);
    } finally {
      setImporting(false);
    }
  };

  const handleImportFile = async (file: File) => {
    if (file.size > IMPORT_MAX_BYTES) {
      setImportError("文件超过 20MB 上限");
      return;
    }
    if (textRef.current.trim() && !window.confirm("导入将替换当前文稿内容，是否继续？")) return;
    setImportError(null);
    setImporting(true);
    try {
      const r = await api.extractDocument(file);
      // 入 undo 栈，导入后可 Cmd+Z 撤回
      undoStack.current.push({ text: textRef.current, caret: null });
      if (undoStack.current.length > 100) undoStack.current.shift();
      redoStack.current = [];
      onChange(importTransform ? importTransform(r.text) : r.text);
      setImportOpen(false);
    } catch (e: any) {
      setImportError(`导入失败：${e.message}`);
    } finally {
      setImporting(false);
    }
  };

  // 外部 text 变化（导入/草稿迁移/undo 恢复）时同步画布 DOM；
  // 内部输入触发的 onChange 会在这里因 serialize 相等而被跳过，不扰动光标。
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    if (serializeCanvas(el) !== text) {
      el.innerHTML = markerTextToHtml(text, speakerNames);
      savedRange.current = null;
    }
  }, [text]);

  // 主持人显示名变化（改名）时重绘标识块文字；text 真源不变
  useEffect(() => {
    if (!speakerNames) return;
    const el = canvasRef.current;
    if (!el) return;
    el.innerHTML = markerTextToHtml(textRef.current, speakerNames);
    savedRange.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speakerNames?.A, speakerNames?.B]);

  // text 变化后芯片 DOM 可能已被替换，弹窗引用失效 → 关闭
  useEffect(() => {
    textRef.current = text;
    setPopover(null);
    setCustomOpen(false);
  }, [text]);

  // 弹窗打开时支持 Esc 关闭
  useEffect(() => {
    if (!popover) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPopover(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [popover]);

  const showHint = (msg: string) => {
    setHint(msg);
    if (hintTimer.current) clearTimeout(hintTimer.current);
    hintTimer.current = setTimeout(() => setHint(null), 2500);
  };

  // ─── undo / redo（自维护栈；程序化 DOM 修改会破坏浏览器原生 undo）───

  const applyHistory = (entry: HistoryEntry) => {
    textRef.current = entry.text;
    onChange(entry.text); // 触发 App 状态更新 → useEffect 重渲染画布
    requestAnimationFrame(() => {
      const el = canvasRef.current;
      if (!el) return;
      if (serializeCanvas(el) !== entry.text) {
        el.innerHTML = markerTextToHtml(entry.text, speakerNames);
      }
      if (entry.caret !== null) setCaretAtMarkerOffset(el, entry.caret);
      savedRange.current = null;
      saveSelection();
    });
  };

  const undo = () => {
    const entry = undoStack.current.pop();
    if (!entry) {
      showHint("没有可撤回的编辑");
      return;
    }
    const el = canvasRef.current;
    redoStack.current.push({ text: textRef.current, caret: el ? caretOffsetIn(el) : null });
    applyHistory(entry);
  };

  const redo = () => {
    const entry = redoStack.current.pop();
    if (!entry) {
      showHint("没有可重做的编辑");
      return;
    }
    const el = canvasRef.current;
    undoStack.current.push({ text: textRef.current, caret: el ? caretOffsetIn(el) : null });
    applyHistory(entry);
  };

  const emitChange = () => {
    const el = canvasRef.current;
    if (!el) return;
    // 清理删除后残留的空作用域壳
    el.querySelectorAll(".mono-scope").forEach(sp => {
      if ((sp.textContent?.length ?? 0) === 0 && !sp.querySelector("[data-marker]")) sp.remove();
    });
    const newText = serializeCanvas(el);
    // Chrome 全选删除后画布会残留 <br>/空行 div，:empty 不成立导致占位符消失；
    // 序列化结果为空时把 DOM 归位为真空
    if (newText === "" && el.innerHTML !== "") {
      el.innerHTML = "";
    }
    const prev = textRef.current;
    if (newText === prev) return;
    // 入 undo 栈（记录当前光标，撤回时恢复）
    undoStack.current.push({ text: prev, caret: caretOffsetIn(el) });
    if (undoStack.current.length > 100) undoStack.current.shift();
    redoStack.current = [];
    textRef.current = newText;
    onChange(newText);
  };

  const saveSelection = () => {
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0 && canvasRef.current?.contains(sel.anchorNode)) {
      savedRange.current = sel.getRangeAt(0).cloneRange();
    }
  };

  const restoreSelection = (): boolean => {
    const el = canvasRef.current;
    if (!el) return false;
    // 优先采信画布内的实时选区：程序化 DOM 选区（或未触发 keyup/mouseup/blur 的场景）
    // 不会经过 saveSelection，savedRange 可能过期
    const live = window.getSelection();
    if (live && live.rangeCount > 0 && el.contains(live.anchorNode)) {
      savedRange.current = live.getRangeAt(0).cloneRange();
    }
    el.focus();
    const sel = window.getSelection();
    if (!sel) return false;
    if (savedRange.current) {
      try {
        sel.removeAllRanges();
        sel.addRange(savedRange.current);
        return true;
      } catch {
        savedRange.current = null;
      }
    }
    // 无保存光标：放末尾
    const r = document.createRange();
    r.selectNodeContents(el);
    r.collapse(false);
    sel.removeAllRanges();
    sel.addRange(r);
    return true;
  };

  // ─── 行重渲染与芯片操作 ────────────────────────────────────

  /** 重渲染单个行容器（更新作用域归属/配色）；裸文本结构时重渲染整个画布 */
  const rerenderLine = (line: HTMLElement) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (line === canvas) {
      canvas.innerHTML = markerTextToHtml(serializeCanvas(canvas), speakerNames);
    } else {
      line.innerHTML = lineToHtml(serializeCanvas(line), speakerNames);
    }
  };

  /** 删除芯片并重渲染所属行，光标落回原芯片位置 */
  const deleteChip = (chip: HTMLElement) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const line = lineOfNode(chip, canvas) ?? canvas;
    const before = serializeBefore(line, chip);
    chip.remove();
    rerenderLine(line);
    setCaretAtMarkerOffset(line, before);
    saveSelection();
    emitChange();
  };

  /** 更换情绪芯片（替换 marker 与配色，重渲染行更新作用域颜色） */
  const replaceEmotionChip = (chip: HTMLElement, meta: EmotionMeta) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const line = lineOfNode(chip, canvas) ?? canvas;
    const before = serializeBefore(line, chip);
    chip.replaceWith(htmlToElement(emotionChipHtml(meta)));
    rerenderLine(line);
    setCaretAtMarkerOffset(line, before);
    saveSelection();
    emitChange();
  };

  /** 修改停顿芯片时长（支持两位小数） */
  const updatePauseChip = (chip: HTMLElement, sec: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const line = lineOfNode(chip, canvas) ?? canvas;
    const before = serializeBefore(line, chip);
    chip.replaceWith(htmlToElement(pauseChipHtml(fmtSec(sec))));
    rerenderLine(line);
    setCaretAtMarkerOffset(line, before);
    saveSelection();
    emitChange();
  };

  /** 点击主持人标识块：切换 A↔B（不可删除） */
  const toggleSpeakerChip = (chip: HTMLElement) => {
    const canvas = canvasRef.current;
    if (!canvas || !speakerNames) return;
    const key = speakerMarkerKey(chip.getAttribute("data-marker") ?? "");
    if (!key) return;
    const next: "A" | "B" = key === "A" ? "B" : "A";
    const line = lineOfNode(chip, canvas) ?? canvas;
    const before = serializeBefore(line, chip);
    chip.replaceWith(htmlToElement(speakerChipHtml(next, speakerNames[next])));
    rerenderLine(line);
    setCaretAtMarkerOffset(line, before);
    saveSelection();
    emitChange();
  };

  /** 在光标所在行的行首插入/替换主持人标识块（每行至多一个）。
   *  纯文本模型拼接（同 insertPause/insertEmotion），不做 DOM 手术。 */
  const insertSpeaker = (key: "A" | "B") => {
    const el = canvasRef.current;
    if (!el) return;
    if (!restoreSelection()) {
      showHint("请先把光标放回文稿内再插入主持人标识");
      return;
    }
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const r = sel.getRangeAt(0);
    if (!el.contains(r.startContainer)) {
      showHint("请先把光标放回文稿内再插入主持人标识");
      return;
    }
    const off = caretOffsetIn(el);
    if (off === null) {
      showHint("请先把光标放回文稿内再插入主持人标识");
      return;
    }
    const prev = textRef.current;
    const lineStart = prev.lastIndexOf("\n", Math.max(0, off - 1)) + 1;
    const marker = `【${key}】`;
    const existing = prev.slice(lineStart).match(/^【([AB])】/);
    if (existing) {
      // 已有标识：原地替换
      const newText = prev.slice(0, lineStart) + marker + prev.slice(lineStart + existing[0].length);
      applyMarkerInsert(newText, lineStart, marker.length);
    } else {
      applyMarkerInsert(prev.slice(0, lineStart) + marker + prev.slice(lineStart), lineStart, marker.length);
    }
  };

  // ─── 光标处插入 ────────────────────────────────────────────

  /** 光标是否处于某个情绪作用域内（含 scope 尾边界） */
  const caretInScope = (): boolean => {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return false;
    const r = sel.getRangeAt(0);
    const node = r.startContainer;
    if (node.nodeType === Node.TEXT_NODE) {
      return !!node.parentElement?.closest(".mono-scope");
    }
    if (node instanceof HTMLElement) {
      const prev = node.childNodes[r.startOffset - 1] ?? null;
      return isScope(prev);
    }
    return false;
  };

  /** 在光标处插入停顿芯片（允许插在作用域内）。
   *  纯文本模型拼接而非 DOM 手术：Chrome 的 Range.insertNode 在"裸文本 + 行 div"
   *  混合结构（粘贴后/首行未换行时）会重组块级结构，把下一行并进当前行（实测 bug），
   *  execCommand("insertHTML") 也有同类问题。直接在标记文本上拼接再整体重渲染。 */
  const insertPause = (sec: string) => {
    const el = canvasRef.current;
    if (!el) return;
    if (!restoreSelection()) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const r = sel.getRangeAt(0);
    if (!el.contains(r.startContainer) || !el.contains(r.endContainer)) return;
    const offA = markerOffsetAt(el, r.startContainer, r.startOffset);
    if (offA === null) return;
    let offB = offA;
    if (!r.collapsed) {
      const end = markerOffsetAt(el, r.endContainer, r.endOffset);
      if (end === null || end < offA) return;
      offB = end;
    }
    const marker = `[pause:${sec}]`;
    const prev = textRef.current;
    applyMarkerInsert(prev.slice(0, offA) + marker + prev.slice(offB), offA, marker.length);
  };

  /** 纯文本模型插入的收尾：整体重渲染 + 光标落到插入内容之后 + 入 undo 栈。
   *  onChange 触发 App 状态更新后，useEffect 因 serialize 相等而跳过，不扰动 DOM。
   *  注意真源必须取插入后 DOM 的序列化结果：插情绪芯片时 DOM 会生成作用域 span，
   *  其序列化带【/】终止符，若直接用 newText 会与 DOM 不等 → effect 重渲染 → 光标被
   *  Chrome 重置到画布开头（实测 bug）。 */
  const applyMarkerInsert = (newText: string, at: number, insertedLen: number) => {
    const el = canvasRef.current;
    if (!el) return;
    undoStack.current.push({ text: textRef.current, caret: at });
    if (undoStack.current.length > 100) undoStack.current.shift();
    redoStack.current = [];
    el.innerHTML = markerTextToHtml(newText, speakerNames);
    setCaretAtMarkerOffset(el, at + insertedLen);
    const finalText = serializeCanvas(el);
    textRef.current = finalText;
    saveSelection();
    onChange(finalText);
  };

  /** 情绪作用域操作。
   * - 无选区：光标处插芯片，光标后到行尾的内容包进该情绪的作用域（继续输入即属于该情绪）
   * - 有选区：选区整体变成该情绪的作用域（MiniMax 式"选中文字套情绪"）
   * - 任何与已有情绪作用域的重叠都被拒绝（不支持嵌套/叠加）
   * 两条路径都是纯文本模型拼接（marker + 内容 + 【/】 终止符写进文本真源），
   * 绝不做 DOM 手术——Chrome 的 Range.insertNode / execCommand("insertHTML") 在
   * "裸文本 + 行 div"混合结构下会重组块级结构导致吞并相邻行（实测 bug）。 */
  const insertEmotion = (meta: EmotionMeta) => {
    const el = canvasRef.current;
    if (!el) return;
    if (!restoreSelection()) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const r = sel.getRangeAt(0);
    if (!el.contains(r.startContainer) || !el.contains(r.endContainer)) return;

    const nodeInScope = (node: Node, atEnd = false): boolean => {
      if (node.nodeType === Node.TEXT_NODE) {
        return !!node.parentElement?.closest(".mono-scope");
      }
      if (node instanceof HTMLElement) {
        if (node.classList.contains("mono-scope")) return true;
        const neighbor = node.childNodes[atEnd ? r.endOffset - 1 : r.startOffset] ?? null;
        return isScope(neighbor);
      }
      return false;
    };

    const prev = textRef.current;
    const marker = `【${meta.label}】`;

    // ── 有选区：选区 → 作用域 ──────────────────────────────
    if (!r.collapsed) {
      const startLine = lineOfNode(r.startContainer, el);
      const endLine = lineOfNode(r.endContainer, el);
      if (!startLine || !endLine || startLine !== endLine) {
        showHint("情绪作用域暂不支持跨行，请按行选择");
        return;
      }
      if (
        nodeInScope(r.startContainer) ||
        nodeInScope(r.endContainer, true) ||
        r.cloneContents().querySelector('[data-marker^="【"]')
      ) {
        showHint("该区域已包含情绪，暂不支持情绪嵌套或叠加");
        return;
      }
      const offA = markerOffsetAt(el, r.startContainer, r.startOffset);
      const offB = markerOffsetAt(el, r.endContainer, r.endOffset);
      if (offA === null || offB === null || offB < offA) return;
      if (prev.slice(offA, offB).includes("\n")) {
        showHint("情绪作用域暂不支持跨行，请按行选择");
        return;
      }
      const inserted =
        prev.slice(0, offA) + marker + prev.slice(offA, offB) + MONO_SCOPE_END + prev.slice(offB);
      applyMarkerInsert(inserted, offA, marker.length + (offB - offA) + MONO_SCOPE_END.length);
      return;
    }

    // ── 无选区：任意位置都可插芯片（插在已有作用域内时，原作用域被截断到芯片前，
    //    重渲染后自动形成"一行多情绪段"）─────────────────────
    const off = caretOffsetIn(el);
    if (off === null) {
      showHint("请先把光标放回文稿内再插入情绪");
      return;
    }
    applyMarkerInsert(prev.slice(0, off) + marker + prev.slice(off), off, marker.length);
  };

  // 键盘：undo/redo 快捷键 + 紧邻芯片整块删除
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey) {
      const k = e.key.toLowerCase();
      if (k === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
        return;
      }
      if (k === "y") {
        e.preventDefault();
        redo();
        return;
      }
    }
    if (e.key !== "Backspace" && e.key !== "Delete") return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || !sel.isCollapsed) return; // 有选区时浏览器默认整体删
    const chip = adjacentChip(sel.getRangeAt(0), e.key === "Backspace" ? -1 : 1);
    if (!chip) return;
    e.preventDefault();
    // 主持人标识块不可删除（点击可切换 A↔B）
    if (speakerMarkerKey(chip.getAttribute("data-marker") ?? "")) return;
    deleteChip(chip);
  };

  // 点击芯片弹出编辑浮层（不移动光标）；主持人标识块点击 = 切换 A↔B
  const handleCanvasMouseDown = (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    const chip = target.closest?.("[data-marker]") as HTMLElement | null;
    if (!chip || !canvasRef.current?.contains(chip)) return;
    e.preventDefault();
    const marker = chip.getAttribute("data-marker") ?? "";
    if (speakerMarkerKey(marker)) {
      toggleSpeakerChip(chip);
      return;
    }
    if (marker.startsWith("【")) {
      setPopover({ kind: "emotion", chip, rect: chip.getBoundingClientRect() });
    } else {
      const m = marker.match(/\[pause:\s*([\d.]+)\s*\]/);
      const sec = m ? parseFloat(m[1]) : 1;
      setCustomSec(m ? fmtSec(sec) : "1");
      setCustomOpen(false);
      setPopover({ kind: "pause", chip, rect: chip.getBoundingClientRect(), sec });
    }
  };

  // 粘贴一律转纯文本，避免外部 HTML 破坏芯片结构
  const handlePaste = (e: React.ClipboardEvent) => {
    e.preventDefault();
    const txt = e.clipboardData.getData("text/plain");
    document.execCommand("insertText", false, txt);
  };

  // ─── 芯片弹窗 ──────────────────────────────────────────────

  /** 弹窗 fixed 定位：贴芯片下方，右缘不越出视口 */
  const popoverStyle = (rect: DOMRect, width: number): React.CSSProperties => ({
    left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 12)),
    top: rect.bottom + 6,
  });

  const curEmotionValue = (() => {
    if (popover?.kind !== "emotion") return null;
    const label = (popover.chip.getAttribute("data-marker") ?? "").slice(1, -1);
    return MONO_EMOTION_MARKERS[label] ?? null;
  })();

  const applyCustomPause = () => {
    if (popover?.kind !== "pause") return;
    const v = parseFloat(customSec);
    if (!Number.isFinite(v) || v < PAUSE_MIN || v > PAUSE_MAX) {
      showHint(`停顿时长需在 ${PAUSE_MIN}–${PAUSE_MAX} 秒之间（支持两位小数）`);
      return;
    }
    updatePauseChip(popover.chip, v);
    setPopover(null);
  };

  return (
    <div className="h-full flex flex-col min-h-0 gap-4">
      {title && (
        <h2 className="shrink-0 text-xs font-semibold text-gray-400 uppercase tracking-wider">{title}</h2>
      )}
      <div className="flex-1 min-h-0 rounded-2xl border border-gray-200 bg-white shadow-sm flex flex-col overflow-hidden">
        {/* 所见即所得画布 */}
        <div className="flex-1 min-h-0 px-7 py-6 overflow-y-auto scrollbar-thin">
          <div
            ref={canvasRef}
            contentEditable
            suppressContentEditableWarning
            role="textbox"
            aria-multiline="true"
            data-placeholder={
              speakerNames
                ? "在这里粘贴或输入播客对话稿……\n每行一位主持人的发言：用工具栏「A 发言 / B 发言」在行首插主持人标识；行内可加情绪与停顿。"
                : "在这里粘贴或输入要配音的文稿……\n每行一段；行首加【喜悦】等情绪标记，行内可插入停顿。"
            }
            aria-label={speakerNames ? "播客对话稿" : "配音文稿"}
            className={cn(
              "mono-canvas w-full min-h-full text-[0.9375rem] leading-8 text-gray-800",
              isEmpty && "text-gray-300"
            )}
            onInput={emitChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onMouseDown={handleCanvasMouseDown}
            onKeyUp={saveSelection}
            onMouseUp={saveSelection}
            onFocus={saveSelection}
            onBlur={saveSelection}
          />
        </div>

        {/* 嵌套限制等临时提示 */}
        <div className="shrink-0 px-6 h-6 flex items-center">
          {hint && (
            <p className="flex items-center gap-1.5 text-xs text-amber-600">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              {hint}
            </p>
          )}
        </div>

        {/* 工具条 */}
        <div className="shrink-0 border-t border-gray-100 bg-white px-5 py-3 flex items-center gap-2">
          {speakerNames &&
            (["A", "B"] as const).map(k => (
              <button
                key={k}
                type="button"
                onMouseDown={e => e.preventDefault()}
                onClick={() => insertSpeaker(k)}
                className={TOOL_BTN}
              >
                <span className={cn("w-2 h-2 rounded-full shrink-0", k === "A" ? "bg-indigo-500" : "bg-teal-500")} />
                {k} 发言
              </button>
            ))}
          <div className="relative">
            <button
              type="button"
              onMouseDown={e => e.preventDefault()}
              onClick={() => setEmoOpen(v => !v)}
              className={cn(TOOL_BTN, emoOpen && "border-emerald-300 bg-emerald-50 text-emerald-700")}
            >
              <ChevronDown className="w-3.5 h-3.5" />
              情绪
            </button>
            {emoOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setEmoOpen(false)} />
                <div className="absolute bottom-full left-0 mb-2 z-20 w-48 rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg grid grid-cols-2 gap-1">
                  {Object.entries(MONO_EMOTION_META).map(([, m]) => (
                    <button
                      key={m.label}
                      type="button"
                      onMouseDown={e => e.preventDefault()}
                      onClick={() => {
                        insertEmotion(m);
                        setEmoOpen(false);
                      }}
                      className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50 text-left"
                    >
                      <span className={cn("w-1.5 h-1.5 rounded-full shrink-0", m.dot)} />
                      {m.label}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
          <button
            type="button"
            onMouseDown={e => e.preventDefault()}
            onClick={() => insertPause("0.5")}
            className={TOOL_BTN}
          >
            <Pause className="w-3.5 h-3.5" />
            停顿 0.5s
          </button>
          <button
            type="button"
            onMouseDown={e => e.preventDefault()}
            onClick={() => {
              setImportError(null);
              setImportText("");
              setImportOpen(true);
            }}
            disabled={importing}
            className={TOOL_BTN}
          >
            {importing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileUp className="w-3.5 h-3.5" />}
            导入
          </button>
          <input ref={fileRef} type="file" accept=".doc,.docx,.pdf,.txt,.md" className="hidden"
            onChange={e => { const f = e.target.files?.[0]; if (f) handleImportFile(f); e.target.value = ""; }} />

          <div className="ml-auto flex items-center gap-3 shrink-0">
            <span className="text-xs text-gray-400 tabular-nums whitespace-nowrap hidden sm:inline">
              {totalChars} 字 · {segCount} 段
              {pointsInfo && pointsInfo.cost > 0 ? ` · 约 ${pointsInfo.cost} 积分` : ""}
            </span>
            {pointsInfo && pointsInfo.balance != null && pointsInfo.cost > pointsInfo.balance && (
              <span className="text-xs text-red-500 whitespace-nowrap">
                积分不足（余额 {pointsInfo.balance} / 需 {pointsInfo.cost}），可用兑换码充值
              </span>
            )}
            <button
              type="button"
              onClick={onGenerate}
              disabled={!canGenerate || generating}
              className="inline-flex items-center gap-1.5 h-9 px-5 rounded-full bg-emerald-600 text-white text-sm font-medium whitespace-nowrap shrink-0 shadow-sm transition-colors hover:bg-emerald-500 active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {generating ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Sparkles className="w-4 h-4" />
              )}
              {generating ? "提交中" : "生成配音"}
            </button>
          </div>
        </div>
      </div>

      {/* 情绪芯片弹窗：更换情绪 / 删除 */}
      {popover?.kind === "emotion" && (
        <>
          <div className="fixed inset-0 z-30" onMouseDown={() => setPopover(null)} />
          <div
            className="fixed z-40 w-60 rounded-xl border border-gray-200 bg-white p-2 shadow-lg"
            style={popoverStyle(popover.rect, 240)}
          >
            <div className="flex items-center justify-between px-1 pb-1.5 mb-1.5 border-b border-gray-100">
              {(() => {
                const cur = curEmotionValue ? MONO_EMOTION_META[curEmotionValue] : null;
                return cur ? (
                  <span className={cn("mono-chip", cur.chip)}>
                    <i className={cn("not-italic w-1.5 h-1.5 rounded-full", cur.dot)} />
                    {cur.label}
                  </span>
                ) : (
                  <span className="text-xs text-gray-400">情绪</span>
                );
              })()}
              <button
                type="button"
                onMouseDown={e => e.preventDefault()}
                onClick={() => {
                  deleteChip(popover.chip);
                  setPopover(null);
                }}
                className="flex items-center gap-1 text-xs text-gray-400 hover:text-red-600 transition-colors"
              >
                <X className="w-3.5 h-3.5" />
                删除
              </button>
            </div>
            <div className="grid grid-cols-2 gap-1">
              {Object.entries(MONO_EMOTION_META).map(([value, m]) => (
                <button
                  key={value}
                  type="button"
                  onMouseDown={e => e.preventDefault()}
                  onClick={() => {
                    if (popover.chip.isConnected) replaceEmotionChip(popover.chip, m);
                    setPopover(null);
                  }}
                  className={cn(
                    "flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-left transition-colors",
                    value === curEmotionValue
                      ? cn(m.chip, "font-medium ring-1 ring-gray-300")
                      : "text-gray-600 hover:bg-gray-50"
                  )}
                >
                  <span className={cn("w-1.5 h-1.5 rounded-full shrink-0", m.dot)} />
                  {m.label}
                </button>
              ))}
            </div>
          </div>
        </>
      )}

      {/* 停顿芯片弹窗：预设时长 / 自定义 / 删除 */}
      {popover?.kind === "pause" && (
        <>
          <div className="fixed inset-0 z-30" onMouseDown={() => setPopover(null)} />
          <div
            className="fixed z-40 w-64 rounded-xl border border-gray-200 bg-white p-2 shadow-lg"
            style={popoverStyle(popover.rect, 256)}
          >
            <div className="flex items-center justify-between px-1 pb-1.5 mb-1.5 border-b border-gray-100">
              <span className="mono-chip mono-chip-pause">
                <i className="not-italic" style={{ fontSize: "0.72em" }}>⏸</i>
                {fmtSec(popover.sec)}s
              </span>
              <button
                type="button"
                onMouseDown={e => e.preventDefault()}
                onClick={() => {
                  deleteChip(popover.chip);
                  setPopover(null);
                }}
                className="flex items-center gap-1 text-xs text-gray-400 hover:text-red-600 transition-colors"
              >
                <X className="w-3.5 h-3.5" />
                删除
              </button>
            </div>
            <div className="flex items-center gap-1">
              {PAUSE_PRESETS.map(s => (
                <button
                  key={s}
                  type="button"
                  onMouseDown={e => e.preventDefault()}
                  onClick={() => {
                    if (popover.chip.isConnected) updatePauseChip(popover.chip, s);
                    setPopover(null);
                  }}
                  className={cn(
                    "flex-1 h-7 rounded-lg text-xs tabular-nums transition-colors",
                    Math.abs(popover.sec - s) < 0.005
                      ? "bg-emerald-600 text-white font-medium"
                      : "bg-gray-100 text-gray-600 hover:bg-emerald-50 hover:text-emerald-700"
                  )}
                >
                  {fmtSec(s)}s
                </button>
              ))}
              <button
                type="button"
                onMouseDown={e => e.preventDefault()}
                onClick={() => setCustomOpen(v => !v)}
                className={cn(
                  "h-7 px-2 rounded-lg text-xs transition-colors",
                  customOpen
                    ? "bg-emerald-600 text-white font-medium"
                    : "bg-gray-100 text-gray-600 hover:bg-emerald-50 hover:text-emerald-700"
                )}
              >
                自定义
              </button>
            </div>
            {customOpen && (
              <div className="mt-2 flex items-center gap-1.5">
                <input
                  type="number"
                  step={0.01}
                  min={PAUSE_MIN}
                  max={PAUSE_MAX}
                  value={customSec}
                  onChange={e => setCustomSec(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      applyCustomPause();
                    }
                  }}
                  autoFocus
                  className="w-20 h-7 px-2 rounded-lg border border-gray-300 text-xs tabular-nums focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
                />
                <span className="text-[0.75rem] text-gray-400">秒（{PAUSE_MIN}–{PAUSE_MAX}）</span>
                <button
                  type="button"
                  onMouseDown={e => e.preventDefault()}
                  onClick={applyCustomPause}
                  className="ml-auto h-7 px-3 rounded-lg bg-emerald-600 text-white text-xs hover:bg-emerald-500 transition-colors"
                >
                  确定
                </button>
              </div>
            )}
          </div>
        </>
      )}

      {/* 导入弹窗。播客模式：提示语 + .txt 选择/拖拽 + 粘贴文本框（旧版批量导入形态）；
          单人模式：文档上传。 */}
      {importOpen && importTransform && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => { if (!importing) setImportOpen(false); }}>
          <div
            className="relative w-full max-w-xl rounded-2xl bg-white shadow-xl max-h-[90vh] overflow-y-auto scrollbar-thin"
            onClick={e => e.stopPropagation()}
            onDragOver={e => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={e => {
              e.preventDefault(); setDragOver(false);
              if (importing) return;
              const f = e.dataTransfer.files?.[0];
              if (f) handleImportTxtFile(f);
            }}
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100 sticky top-0 bg-white rounded-t-2xl">
              <h3 className="text-sm font-semibold text-gray-800">导入对话脚本</h3>
              <button
                type="button"
                aria-label="关闭"
                onClick={() => { if (!importing) setImportOpen(false); }}
                className="p-1 rounded text-gray-400 hover:text-gray-600 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 space-y-3">
              {/* 格式说明 */}
              <div className="rounded-lg bg-blue-50 border border-blue-100 p-3">
                <p className="text-[0.75rem] text-blue-600">
                  纯文本：每行一段对话，用 <code className="px-1 bg-white/70 rounded">A:</code> /{" "}
                  <code className="px-1 bg-white/70 rounded">B:</code>（A 代表 {speakerNames?.A ?? "主持人A"}，B 代表{" "}
                  {speakerNames?.B ?? "主持人B"}）开头指定说话人，无前缀自动交替分配。
                </p>
              </div>

              {/* 文件选择（仅 .txt，支持拖拽） */}
              <div className={cn(
                "flex items-center gap-2 rounded-lg border border-dashed px-3 py-2.5 transition-colors",
                dragOver ? "border-emerald-400 bg-emerald-50" : "border-gray-200",
              )}>
                <button
                  type="button"
                  onClick={() => fileRef.current?.click()}
                  disabled={importing}
                  className="inline-flex items-center gap-1.5 h-8 px-3 rounded-full border border-gray-200 bg-white text-xs text-gray-600 transition-colors hover:border-emerald-300 hover:bg-emerald-50 hover:text-emerald-700 disabled:opacity-40"
                >
                  {importing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CloudUpload className="w-3.5 h-3.5" />}
                  {importing ? "读取中..." : "选择 txt 文件"}
                </button>
                <span className="text-[0.75rem] text-gray-400">或把 .txt 文件拖到这里，内容会填入下方文本框</span>
              </div>

              {/* 粘贴文本框（placeholder 即格式示例） */}
              <textarea
                value={importText}
                onChange={e => { setImportText(e.target.value); setImportError(null); }}
                placeholder={
                  "也可以直接把脚本粘贴到这里，例如：\nA:哈喽，大家好，欢迎收听我们的播客。\nB:大家好，我是主持人B。\nA:今天咱们聊一个很现实的话题。"
                }
                className="w-full min-h-[160px] rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-xs focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
                autoFocus
              />

              {importError && (
                <p className="flex items-center gap-1.5 text-xs text-red-600">
                  <AlertCircle className="w-3.5 h-3.5 shrink-0" />
                  {importError}
                </p>
              )}

              {/* 操作 */}
              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setImportOpen(false)}
                  className="h-9 px-4 rounded-full border border-gray-200 bg-white text-sm text-gray-600 hover:bg-gray-50 transition-colors"
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={() => applyImportText(importText)}
                  disabled={importing || !importText.trim()}
                  className="h-9 px-5 rounded-full bg-emerald-600 text-white text-sm font-medium shadow-sm transition-colors hover:bg-emerald-500 active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  导入到画布{importText.trim() ? `（${importText.split("\n").filter(l => l.trim()).length} 行）` : ""}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {importOpen && !importTransform && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => { if (!importing) setImportOpen(false); }}
          />
          <div className="relative w-full max-w-md rounded-2xl bg-white shadow-xl p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold text-gray-800">导入文档</h3>
              <button
                type="button"
                aria-label="关闭"
                onClick={() => { if (!importing) setImportOpen(false); }}
                className="p-1 rounded text-gray-400 hover:text-gray-600 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <p className="text-xs text-gray-500 mb-2">上传文件</p>
            <div
              role="button"
              tabIndex={0}
              onClick={() => !importing && fileRef.current?.click()}
              onKeyDown={e => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  if (!importing) fileRef.current?.click();
                }
              }}
              onDragOver={e => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={e => {
                e.preventDefault();
                setDragOver(false);
                if (importing) return;
                const f = e.dataTransfer.files?.[0];
                if (f) handleImportFile(f);
              }}
              className={cn(
                "flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-10 text-center cursor-pointer transition-colors",
                dragOver
                  ? "border-emerald-400 bg-emerald-50"
                  : "border-gray-200 hover:border-emerald-300 hover:bg-emerald-50/40"
              )}
            >
              {importing ? (
                <Loader2 className="w-8 h-8 text-emerald-500 animate-spin" />
              ) : (
                <CloudUpload className="w-8 h-8 text-emerald-500" />
              )}
              <p className="text-sm text-gray-700">
                {importing ? "正在解析文档…" : "点击或拖拽上传到这里"}
              </p>
              <p className="text-xs text-gray-400">
                支持 .doc / .docx / .pdf / .txt / .md · ≤ 20MB · ≤ 1 万字
              </p>
            </div>
            {importError && (
              <p className="mt-3 flex items-center gap-1.5 text-xs text-red-600">
                <AlertCircle className="w-3.5 h-3.5 shrink-0" />
                {importError}
              </p>
            )}
          </div>
        </div>
      )}

      {/* 错误提示 */}
      {error && (
        <div className="shrink-0 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5">
          <AlertCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
          <p className="text-xs text-red-600 leading-5">{error}</p>
        </div>
      )}
    </div>
  );
}
