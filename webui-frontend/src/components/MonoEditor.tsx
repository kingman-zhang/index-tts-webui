/**
 * 配音模式编辑器：contentEditable 所见即所得画布（MiniMax 式作用域模型）。
 * - 情绪芯片【喜悦】是作用域起点：其后直到下一个芯片/行尾的文字带淡色高亮，
 *   一行内可有多个情绪段；已覆盖区域内禁止再插情绪（不支持嵌套/叠加）。
 * - 停顿 [pause:1] 是点状芯片，可插在任意位置（含作用域内）。
 * - 光标紧邻芯片按 Backspace/Delete 时整块删除，所属行重渲染以更新作用域归属。
 * 画布 DOM 是显示层，标记文本（props.text）仍是唯一真源。
 */
import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Pause,
  Sparkles,
  Loader2,
  AlertCircle,
} from "lucide-react";
import {
  MONO_EMOTION_META,
  MONO_EMOTION_MARKERS,
  textToMonoLines,
} from "@/types";
import { cn } from "@/lib/utils";

interface MonoEditorProps {
  text: string;
  onChange: (text: string) => void;
  onGenerate: () => void;
  canGenerate: boolean;
  generating: boolean;
  error: string | null;
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

const PAUSE_TOKEN_RE = /\[pause:\s*([\d.]+)\s*\]/g;
const EMOTION_TOKEN_RE = /【[^【】]+】/g;

function pauseChipHtml(sec: string): string {
  return (
    `<span contenteditable="false" data-marker="[pause:${sec}]" ` +
    `class="mono-chip mono-chip-pause"><i class="not-italic" style="font-size:12px">⏸</i>${sec}s</span>`
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

/** 无情绪区间：[pause] 转芯片、未知【xx】按普通文本转义 */
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
  inner += escapeHtml(txt.slice(last));
  return inner;
}

/** 单行标记文本 → 行内 HTML；情绪芯片后的文字包进作用域高亮 span */
function lineToHtml(line: string): string {
  const chips: { idx: number; end: number; meta: EmotionMeta }[] = [];
  for (const m of line.matchAll(EMOTION_TOKEN_RE)) {
    const value = MONO_EMOTION_MARKERS[m[0].slice(1, -1)];
    const meta = value ? MONO_EMOTION_META[value] : null;
    if (meta) chips.push({ idx: m.index ?? 0, end: (m.index ?? 0) + m[0].length, meta });
  }
  let out = "";
  let pos = 0;
  for (let i = 0; i <= chips.length; i++) {
    const segEnd = i < chips.length ? chips[i].idx : line.length;
    const seg = line.slice(pos, segEnd);
    const scopeMeta = i > 0 ? chips[i - 1].meta : null;
    const inner = renderPlain(seg, true);
    if (scopeMeta && inner) {
      out += `<span class="mono-scope ${scopeMeta.scope}">${inner}</span>`;
    } else {
      out += inner;
    }
    if (i < chips.length) {
      out += emotionChipHtml(chips[i].meta);
      pos = chips[i].end;
    }
  }
  return out;
}

/** 标记文本 → 画布 innerHTML（每行一个 div，与 Chrome 的 Enter 行为一致） */
export function markerTextToHtml(text: string): string {
  return text
    .split("\n")
    .map(line => `<div>${lineToHtml(line) || "<br>"}</div>`)
    .join("");
}

function serializeInline(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (!(node instanceof HTMLElement)) return "";
  const marker = node.getAttribute("data-marker");
  if (marker) return marker;
  if (node.tagName === "BR") return "\n";
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
      n.childNodes.forEach(c => {
        cur += serializeInline(c);
      });
      flush();
      return;
    }
    cur += serializeInline(n);
  });
  lines.push(cur);
  return lines.join("\n");
}

/** target 节点之前的标记文本长度（chip 按 marker 长度计） */
function serializeBefore(root: HTMLElement, target: Node): number {
  let acc = 0;
  const walk = (node: Node): boolean => {
    if (node === target) return true;
    if (node.nodeType === Node.TEXT_NODE) {
      acc += node.textContent?.length ?? 0;
      return false;
    }
    if (node instanceof HTMLElement) {
      const mk = node.getAttribute("data-marker");
      if (mk) {
        acc += mk.length;
        return false;
      }
      if (node.tagName === "BR") {
        acc += 1;
        return false;
      }
      for (const c of Array.from(node.childNodes)) {
        if (walk(c)) return true;
      }
    }
    return false;
  };
  walk(root);
  return acc;
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
        rest -= 1;
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
  "inline-flex items-center gap-1 h-8 px-3 rounded-full border border-gray-200 bg-white text-xs text-gray-600 transition-colors hover:border-emerald-300 hover:bg-emerald-50 hover:text-emerald-700 active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed";

function formatDur(sec: number): string {
  if (sec < 60) return `${sec} 秒`;
  return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
}

/** 预估时长：汉语播报约 4.3 字/秒（含标点停顿，粗估值） */
const CHARS_PER_SEC = 4.3;

export function MonoEditor({ text, onChange, onGenerate, canGenerate, generating, error }: MonoEditorProps) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const savedRange = useRef<Range | null>(null);
  const hintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [emoOpen, setEmoOpen] = useState(false);
  const [hint, setHint] = useState<string | null>(null);

  const parsed = textToMonoLines(text);
  const totalChars = parsed.reduce(
    (acc, l) => acc + l.text.replace(/\[pause:\s*[\d.]+\s*\]/g, "").length,
    0
  );
  const estSec = Math.round(totalChars / CHARS_PER_SEC);
  const isEmpty = text.trim().length === 0;

  // 外部 text 变化（导入/草稿迁移）时同步画布 DOM；
  // 内部输入触发的 onChange 会在这里因 serialize 相等而被跳过，不扰动光标。
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    if (serializeCanvas(el) !== text) {
      el.innerHTML = markerTextToHtml(text);
      savedRange.current = null;
    }
  }, [text]);

  const showHint = (msg: string) => {
    setHint(msg);
    if (hintTimer.current) clearTimeout(hintTimer.current);
    hintTimer.current = setTimeout(() => setHint(null), 2500);
  };

  const emitChange = () => {
    const el = canvasRef.current;
    if (!el) return;
    // 清理删除后残留的空作用域壳
    el.querySelectorAll(".mono-scope").forEach(sp => {
      if ((sp.textContent?.length ?? 0) === 0 && !sp.querySelector("[data-marker]")) sp.remove();
    });
    onChange(serializeCanvas(el));
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

  /** 在光标处插入停顿芯片（允许插在作用域内） */
  const insertPause = (html: string) => {
    if (!restoreSelection()) return;
    document.execCommand("insertHTML", false, html);
    saveSelection();
    emitChange();
  };

  /** 情绪作用域操作。
   * - 无选区：光标处插芯片，光标后到行尾的内容包进该情绪的作用域（继续输入即属于该情绪）
   * - 有选区：选区整体变成该情绪的作用域（MiniMax 式"选中文字套情绪"）
   * - 任何与已有情绪作用域的重叠都被拒绝（不支持嵌套/叠加）
   */
  const insertEmotion = (meta: EmotionMeta) => {
    const el = canvasRef.current;
    if (!el) return;
    if (!restoreSelection()) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const r = sel.getRangeAt(0);

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
      const frag = r.extractContents();
      const scopeEl = document.createElement("span");
      scopeEl.className = `mono-scope ${meta.scope}`;
      scopeEl.appendChild(frag);
      r.insertNode(htmlToElement(emotionChipHtml(meta)));
      r.collapse(false);
      r.insertNode(scopeEl);
      const after = document.createRange();
      after.setStartAfter(scopeEl);
      after.collapse(true);
      sel.removeAllRanges();
      sel.addRange(after);
      saveSelection();
      emitChange();
      return;
    }

    // ── 无选区：任意位置都可插芯片（插在已有作用域内时，原作用域被截断到芯片前，
    //    重渲染后自动形成"一行多情绪段"）─────────────────────
    const marker = `【${meta.label}】`;
    const sameBefore = el.querySelectorAll(`[data-marker="${marker}"]`).length;
    document.execCommand("insertHTML", false, emotionChipHtml(meta));
    // 序列化 → 重渲染整画布：lineToHtml 会把芯片后到行尾的文字包进作用域
    el.innerHTML = markerTextToHtml(serializeCanvas(el));
    const target = el.querySelectorAll(`[data-marker="${marker}"]`)[sameBefore];
    if (target) {
      const at = document.createRange();
      at.setStartAfter(target);
      at.collapse(true);
      sel.removeAllRanges();
      sel.addRange(at);
    }
    saveSelection();
    emitChange();
  };

  // 紧邻芯片时整块删除，并重渲染所属行以更新作用域归属
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "Backspace" && e.key !== "Delete") return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || !sel.isCollapsed) return; // 有选区时浏览器默认整体删
    const chip = adjacentChip(sel.getRangeAt(0), e.key === "Backspace" ? -1 : 1);
    if (!chip) return;
    e.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    // 行容器：画布的行 div；裸文本结构时为画布自身
    const line = lineOfNode(chip, canvas) ?? canvas;
    const before = serializeBefore(line, chip);
    chip.remove();
    if (line === canvas) {
      canvas.innerHTML = markerTextToHtml(serializeCanvas(canvas));
    } else {
      line.innerHTML = lineToHtml(serializeCanvas(line));
    }
    setCaretAtMarkerOffset(line, before);
    saveSelection();
    emitChange();
  };

  // 粘贴一律转纯文本，避免外部 HTML 破坏芯片结构
  const handlePaste = (e: React.ClipboardEvent) => {
    e.preventDefault();
    const txt = e.clipboardData.getData("text/plain");
    document.execCommand("insertText", false, txt);
  };

  return (
    <div className="h-full flex flex-col min-h-0 gap-4">
      <div className="flex-1 min-h-0 rounded-2xl border border-gray-200 bg-white shadow-sm flex flex-col overflow-hidden">
        {/* 所见即所得画布 */}
        <div className="flex-1 min-h-0 px-7 py-6 overflow-y-auto scrollbar-thin">
          <div
            ref={canvasRef}
            contentEditable
            suppressContentEditableWarning
            role="textbox"
            aria-multiline="true"
            aria-label="配音文稿"
            data-placeholder={
              "在这里粘贴或输入要配音的文稿……\n每行一段；行首加【喜悦】等情绪标记，行内可插入停顿。"
            }
            className={cn(
              "mono-canvas w-full min-h-full text-[15px] leading-8 text-gray-800",
              isEmpty && "text-gray-300"
            )}
            onInput={emitChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
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
            onClick={() => insertPause(pauseChipHtml("1"))}
            className={TOOL_BTN}
          >
            <Pause className="w-3.5 h-3.5" />
            停顿 1s
          </button>

          <div className="ml-auto flex items-center gap-4">
            <span className="text-xs text-gray-400 tabular-nums hidden sm:inline">
              {totalChars} 字 · {parsed.length} 段
              {estSec > 0 && <span className="hidden md:inline"> · 约 {formatDur(estSec)}</span>}
            </span>
            <button
              type="button"
              onClick={onGenerate}
              disabled={!canGenerate || generating}
              className="inline-flex items-center gap-1.5 h-9 px-5 rounded-full bg-emerald-600 text-white text-sm font-medium shadow-sm transition-colors hover:bg-emerald-500 active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed"
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
