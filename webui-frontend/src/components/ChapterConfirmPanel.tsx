/**
 * 导入「分段确认页」：导入文档后的必经过渡步骤。
 *
 * 为什么要有这一步：一本书自动切分不可能永远切对，而切错要等合成完才发现就太贵了
 * （一章几十分钟、几百积分）。所以导入**只解析 + 切分**，把结果摊开给用户看、
 * 让他改标题、调每章上限重切、勾选要做的章，确认之后才可能进入生成。
 *
 * 本组件是纯展示层：不改文、不提交、不生成 —— 所有动作通过回调上抛。
 */
import { useMemo, useState } from "react";
import { AlertCircle, ArrowLeft, Check, Loader2, Merge } from "lucide-react";
import {
  type ChapterMeta,
  chapterTextOf,
  estimateMinutes,
  estimatePoints,
  formatChars,
  formatMinutes,
} from "@/types";
import { cn } from "@/lib/utils";

export interface ChapterConfirmPanelProps {
  /** 解析出的全文（章节正文按行号从它还原） */
  text: string;
  chapters: ChapterMeta[];
  /** 当前「单章字数上限」，调它再点重新切分 */
  chapterMaxChars: number;
  /** 选中的章节 index 集合 */
  selected: Set<number>;
  /** 积分单价配置；null = 未开启按量计费，不展示积分列 */
  pointsConfig?: { per1000: number; minCharge: number } | null;
  /** 当前余额（登录且开启会员时才有） */
  balance?: number | null;
  splitting: boolean;
  error: string | null;
  confirmLabel: string;
  confirmHint?: string;
  onToggle: (index: number) => void;
  onToggleAll: (on: boolean) => void;
  onRename: (index: number, title: string) => void;
  /** 与上一章合并（index 为 0 时无效） */
  onMerge: (index: number) => void;
  onChapterMaxChange: (n: number) => void;
  onResplit: () => void;
  onConfirm: () => void;
  onBack: () => void;
}

/** 「每章上限」快捷档位。默认值（`DEFAULT_CHAPTER_MAX_CHARS` = 2 万）在其中一档上，
 *  手填仍可输入任意值。上限只是「不得超过」——调大**不会**合并已识别出的标题章。 */
const CHAPTER_MAX_PRESETS = [10000, 20000, 30000, 50000];

export function ChapterConfirmPanel(props: ChapterConfirmPanelProps) {
  const {
    text, chapters, chapterMaxChars, selected, pointsConfig, balance,
    splitting, error, confirmLabel, confirmHint,
    onToggle, onToggleAll, onRename, onMerge, onChapterMaxChange, onResplit,
    onConfirm, onBack,
  } = props;

  const [customMax, setCustomMax] = useState("");

  const lines = useMemo(
    () => text.replace(/\r\n?/g, "\n").split("\n"),
    [text]
  );

  const rows = useMemo(
    () =>
      chapters.map(ch => {
        const body = chapterTextOf(lines, ch);
        const chars = body.replace(/\s/g, "").length;
        return { ch, chars, title: ch.title };
      }),
    [chapters, lines]
  );

  const picked = rows.filter(r => selected.has(r.ch.index));
  const totalChars = picked.reduce((n, r) => n + r.chars, 0);
  const totalCost =
    pointsConfig && pointsConfig.per1000 > 0
      ? picked.reduce(
          (n, r) => n + estimatePoints(r.chars, pointsConfig.per1000, pointsConfig.minCharge),
          0
        )
      : 0;
  const showPoints = !!pointsConfig && pointsConfig.per1000 > 0;
  const affordableChars =
    showPoints && balance != null
      ? Math.floor((balance * 1000) / pointsConfig!.per1000)
      : null;
  const insufficient = totalCost > 0 && balance != null && totalCost > balance;
  const allSelected = chapters.length > 0 && selected.size === chapters.length;

  const applyCustomMax = () => {
    const n = parseInt(customMax, 10);
    if (!Number.isFinite(n) || n <= 0) return;
    onChapterMaxChange(n);
    setCustomMax("");
  };

  return (
    <div className="flex flex-col min-h-0 max-h-[70vh]">
      <div className="shrink-0">
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-base font-semibold text-gray-800">确认分段</h3>
          <button
            type="button"
            onClick={onBack}
            className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            重新选文件
          </button>
        </div>
        <p className="text-xs text-gray-500 leading-5">
          已解析 <span className="font-medium text-gray-700">{formatChars(rows.reduce((n, r) => n + r.chars, 0))}</span>
          ，自动切成 <span className="font-medium text-gray-700">{chapters.length} 章</span>。
          <span className="text-gray-400">确认前不会生成任何音频。</span>
        </p>
      </div>

      {/* 切分旋钮 */}
      <div className="shrink-0 mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-gray-200 bg-gray-50/70 px-3 py-2">
        <span
          className="text-xs text-gray-500 cursor-help border-b border-dotted border-gray-300"
          title="只是上限：超过它的章会被再切碎；已识别出的标题章不会被合并到一起去。"
        >
          每章上限
        </span>
        {CHAPTER_MAX_PRESETS.map(n => (
          <button
            key={n}
            type="button"
            disabled={splitting}
            onClick={() => onChapterMaxChange(n)}
            className={cn(
              "h-7 px-2.5 rounded-full border text-xs transition-colors disabled:opacity-40",
              chapterMaxChars === n
                ? "border-emerald-400 bg-emerald-50 text-emerald-700"
                : "border-gray-200 bg-white text-gray-600 hover:border-emerald-300"
            )}
          >
            {n / 10000 >= 1 ? `${n / 10000} 万` : `${n}`} 字
          </button>
        ))}
        <input
          value={customMax}
          disabled={splitting}
          onChange={e => setCustomMax(e.target.value.replace(/[^\d]/g, ""))}
          onKeyDown={e => { if (e.key === "Enter") applyCustomMax(); }}
          placeholder="自定义"
          className="h-7 w-20 rounded-full border border-gray-200 bg-white px-2.5 text-xs text-gray-700 outline-none focus:border-emerald-300 disabled:opacity-40"
        />
        <button
          type="button"
          disabled={splitting}
          onClick={onResplit}
          className="inline-flex items-center gap-1 h-7 px-3 rounded-full border border-emerald-200 bg-emerald-50 text-xs text-emerald-700 hover:bg-emerald-100 disabled:opacity-40"
        >
          {splitting ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
          重新切分
        </button>
        <span className="text-xs text-gray-400 ml-auto">
          约 {formatMinutes(estimateMinutes(chapterMaxChars))}/章
        </span>
      </div>

      {/* 章节列表 */}
      <div className="shrink-0 mt-2 flex items-center gap-3 px-1 text-xs text-gray-500">
        <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={e => onToggleAll(e.target.checked)}
            className="accent-emerald-500"
          />
          全选
        </label>
        <span className="text-gray-400">已选 {selected.size} / {chapters.length} 章</span>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-thin mt-1 rounded-xl border border-gray-200 divide-y divide-gray-100">
        {rows.map(r => {
          const on = selected.has(r.ch.index);
          const cost = showPoints
            ? estimatePoints(r.chars, pointsConfig!.per1000, pointsConfig!.minCharge)
            : 0;
          return (
            <div
              key={r.ch.index}
              className={cn("flex items-center gap-2 px-3 py-2", on ? "bg-emerald-50/40" : "bg-white")}
            >
              <input
                type="checkbox"
                checked={on}
                onChange={() => onToggle(r.ch.index)}
                className="accent-emerald-500 shrink-0"
              />
              <span className="w-8 shrink-0 text-right text-xs text-gray-400 tabular-nums">
                {r.ch.index + 1}
              </span>
              <input
                value={r.title}
                onChange={e => onRename(r.ch.index, e.target.value)}
                className="flex-1 min-w-0 h-7 rounded-lg border border-transparent bg-transparent px-2 text-sm text-gray-800 outline-none hover:border-gray-200 focus:border-emerald-300 focus:bg-white"
              />
              <span className="shrink-0 text-xs text-gray-400 tabular-nums w-20 text-right">
                {formatChars(r.chars)}
              </span>
              <span className="shrink-0 text-xs text-gray-400 tabular-nums w-20 text-right">
                {formatMinutes(estimateMinutes(r.chars))}
              </span>
              {showPoints && (
                <span className="shrink-0 text-xs text-gray-400 tabular-nums w-16 text-right">
                  {cost} 分
                </span>
              )}
              <button
                type="button"
                disabled={r.ch.index === 0}
                onClick={() => onMerge(r.ch.index)}
                title="与上一章合并"
                className="shrink-0 p-1 rounded text-gray-300 hover:text-emerald-600 hover:bg-emerald-50 disabled:opacity-30 disabled:hover:bg-transparent"
              >
                <Merge className="w-3.5 h-3.5" />
              </button>
            </div>
          );
        })}
      </div>

      {error && (
        <p className="shrink-0 mt-2 flex items-center gap-1.5 text-xs text-red-600">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" />
          {error}
        </p>
      )}

      {/* 合计 + 动作 */}
      <div className="shrink-0 mt-3 rounded-xl border border-gray-200 bg-gray-50/70 px-3 py-2.5">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs text-gray-600">
          <span>已选 <span className="font-medium text-gray-800">{picked.length}</span> 章</span>
          <span><span className="font-medium text-gray-800">{formatChars(totalChars)}</span></span>
          <span>约 <span className="font-medium text-gray-800">{formatMinutes(estimateMinutes(totalChars))}</span></span>
          {showPoints && (
            <span>共 <span className="font-medium text-gray-800">{totalCost}</span> 积分</span>
          )}
          {affordableChars != null && (
            <span className="text-gray-400 ml-auto">
              余额 {balance} 积分 ≈ 可合成 {formatChars(affordableChars)}
            </span>
          )}
        </div>
        {insufficient && (
          <p className="mt-1.5 flex items-center gap-1.5 text-xs text-red-600">
            <AlertCircle className="w-3.5 h-3.5 shrink-0" />
            选中的 {picked.length} 章共需 {totalCost} 积分，余额不足（差 {totalCost - (balance ?? 0)}）
          </p>
        )}
      </div>

      <div className="shrink-0 mt-3 flex items-center justify-end gap-2">
        {confirmHint && <span className="mr-auto text-xs text-gray-400">{confirmHint}</span>}
        <button
          type="button"
          onClick={onBack}
          className="h-9 px-4 rounded-full border border-gray-200 bg-white text-sm text-gray-600 hover:border-gray-300"
        >
          取消
        </button>
        <button
          type="button"
          disabled={picked.length === 0 || insufficient}
          onClick={onConfirm}
          className="inline-flex items-center gap-1.5 h-9 px-4 rounded-full bg-emerald-500 text-sm text-white hover:bg-emerald-600 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Check className="w-4 h-4" />
          {confirmLabel}
        </button>
      </div>
    </div>
  );
}
