/**
 * 章节目录（左栏卡片，**仅在「书稿模式」出现**）。
 *
 * 定位：一本书导入并分章确认后，左栏出现这份目录。它是**双重入口**——
 *  ① 点标题 = 切到该章编辑（画布只装当前章）；
 *  ② 勾选若干章 = 批量生成（每章一个队列任务，各扣各的积分）。
 *
 * 为什么把两件事放同一份列表：它们本来就是同一批对象上的两个动作，
 * 分成两块 UI 只会让人来回对照「我编辑的是哪章 / 我选中的是哪几章」。
 *
 * 纯展示层：不提交、不改数据，所有动作通过回调上抛。
 */
import { useEffect, useMemo, useRef } from "react";
import { BookOpen, ChevronDown, ChevronRight, CheckCircle2, Loader2, X, Zap } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, Badge } from "./ui";
import { cn } from "@/lib/utils";
import {
  type MonoChapter,
  estimateMinutes,
  estimatePoints,
  formatChars,
  formatMinutes,
} from "@/types";

export interface ChapterListProps {
  chapters: MonoChapter[];
  /** 当前正在编辑的章 id */
  activeId: string | null;
  /** 勾选（批量生成）的章 id 集合 */
  selected: Set<string>;
  /** 本次会话内已提交入队的章 id —— 列表上打勾，避免重复提交 */
  submitted: Set<string>;
  collapsed: boolean;
  onToggleCollapse: () => void;
  /** 切换到该章（画布换文本） */
  onSelect: (id: string) => void;
  onToggle: (id: string) => void;
  onToggleAll: (on: boolean) => void;
  /** 单章生成 */
  onGenerate: (id: string) => void;
  /** 批量生成选中的章 */
  onGenerateSelected: () => void;
  /** 批量提交进行中 */
  submitting: boolean;
  /** 正在单章提交的章 id（该行按钮转圈） */
  submittingId: string | null;
  /** 是否具备生成前提（已选音色等），false 时按钮禁用 */
  canGenerate: boolean;
  /** 积分环境；null = 未开启按量计费，不展示积分 */
  pointsConfig?: { per1000: number; minCharge: number } | null;
  balance?: number | null;
  /** 退出书稿模式（回到普通单篇） */
  onExit: () => void;
}

/** 与后端 count_chars 同口径：不计空白 */
function charsOf(text: string): number {
  return text.replace(/\s/g, "").length;
}

export function ChapterList(props: ChapterListProps) {
  const {
    chapters, activeId, selected, submitted, collapsed, onToggleCollapse,
    onSelect, onToggle, onToggleAll, onGenerate, onGenerateSelected,
    submitting, submittingId, canGenerate, pointsConfig, balance, onExit,
  } = props;

  const listRef = useRef<HTMLDivElement>(null);
  const activeRowRef = useRef<HTMLDivElement>(null);

  // 切章后把当前章滚进视野（几十章时不做这一步，用户会找不到自己在哪）
  useEffect(() => {
    if (collapsed) return;
    activeRowRef.current?.scrollIntoView({ block: "nearest" });
  }, [activeId, collapsed]);

  const stats = useMemo(() => {
    const per = chapters.map(c => charsOf(c.text));
    const totalChars = per.reduce((n, v) => n + v, 0);
    const picked = chapters.filter(c => selected.has(c.id));
    const pickedChars = picked.reduce((n, c) => n + charsOf(c.text), 0);
    const showPoints = !!pointsConfig && pointsConfig.per1000 > 0;
    const pickedCost = showPoints
      ? picked.reduce((n, c) => n + estimatePoints(charsOf(c.text), pointsConfig!.per1000, pointsConfig!.minCharge), 0)
      : 0;
    return { totalChars, pickedChars, picked, pickedCost, showPoints };
  }, [chapters, selected, pointsConfig]);

  const { totalChars, pickedChars, picked, pickedCost, showPoints } = stats;
  const allSelected = chapters.length > 0 && selected.size === chapters.length;
  const insufficient = pickedCost > 0 && balance != null && pickedCost > balance;
  const doneCount = chapters.filter(c => submitted.has(c.id)).length;

  return (
    <Card>
      <CardHeader className="cursor-pointer" onClick={onToggleCollapse}>
        <div className="flex items-center gap-2">
          {collapsed
            ? <ChevronRight className="w-3.5 h-3.5 text-gray-400 shrink-0" />
            : <ChevronDown className="w-3.5 h-3.5 text-gray-400 shrink-0" />}
          <BookOpen className="w-4 h-4 text-emerald-600 shrink-0" />
          <CardTitle>章节目录</CardTitle>
          <Badge color="green">{chapters.length} 章</Badge>
          <span className="ml-auto text-[11px] text-gray-400 tabular-nums">
            {formatChars(totalChars)}
          </span>
        </div>
        {collapsed && (
          <p className="mt-1 ml-5 text-[11px] text-gray-400">
            当前：{chapters.find(c => c.id === activeId)?.title ?? "—"}
            {doneCount > 0 && ` · 已提交 ${doneCount} 章`}
          </p>
        )}
      </CardHeader>

      {!collapsed && (
        <CardContent className="p-0">
          {/* 全选行 */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-100 text-xs text-gray-500">
            <label className="inline-flex items-center gap-1.5 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={e => onToggleAll(e.target.checked)}
                className="accent-emerald-500"
              />
              全选
            </label>
            <span className="text-gray-400">已选 {selected.size} / {chapters.length}</span>
            <button
              type="button"
              onClick={onExit}
              title="退出书稿模式（回到普通单篇）"
              className="ml-auto inline-flex items-center gap-0.5 text-[11px] text-gray-400 hover:text-red-500"
            >
              <X className="w-3 h-3" />
              退出书稿
            </button>
          </div>

          {/* 章节列表 */}
          <div ref={listRef} className="max-h-72 overflow-y-auto scrollbar-thin divide-y divide-gray-50">
            {chapters.map((ch, i) => {
              const on = selected.has(ch.id);
              const isActive = ch.id === activeId;
              const isSubmitted = submitted.has(ch.id);
              const busy = submittingId === ch.id;
              return (
                <div
                  key={ch.id}
                  ref={isActive ? activeRowRef : undefined}
                  className={cn(
                    "group flex items-center gap-1.5 pl-2 pr-1.5 py-1.5 text-xs transition-colors",
                    isActive ? "bg-emerald-50" : on ? "bg-emerald-50/30" : "hover:bg-gray-50"
                  )}
                >
                  {/* 当前章左侧色条 */}
                  <span className={cn("w-0.5 h-5 rounded-full shrink-0", isActive ? "bg-emerald-500" : "bg-transparent")} />
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => onToggle(ch.id)}
                    onClick={e => e.stopPropagation()}
                    className="accent-emerald-500 shrink-0"
                  />
                  <button
                    type="button"
                    onClick={() => onSelect(ch.id)}
                    title={`${ch.title}（${formatChars(charsOf(ch.text))}）`}
                    className="flex-1 min-w-0 flex items-center gap-1.5 text-left"
                  >
                    <span className="shrink-0 text-[10px] text-gray-400 tabular-nums w-4 text-right">{i + 1}</span>
                    <span className={cn("truncate", isActive ? "font-medium text-emerald-700" : "text-gray-700")}>
                      {ch.title}
                    </span>
                  </button>
                  {isSubmitted && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0" />}
                  <button
                    type="button"
                    disabled={!canGenerate || busy || submitting}
                    onClick={() => onGenerate(ch.id)}
                    title={canGenerate ? "生成这一章" : "请先选择配音音色"}
                    className={cn(
                      "shrink-0 p-1 rounded transition-colors disabled:opacity-30",
                      "text-gray-300 hover:text-emerald-600 hover:bg-emerald-50",
                      "opacity-0 group-hover:opacity-100 focus:opacity-100",
                      busy && "opacity-100"
                    )}
                  >
                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin text-emerald-500" /> : <Zap className="w-3.5 h-3.5" />}
                  </button>
                </div>
              );
            })}
          </div>

          {/* 合计 + 批量生成 */}
          <div className="border-t border-gray-100 px-3 py-2">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px] text-gray-500">
              <span>已选 <span className="font-medium text-gray-700">{picked.length}</span> 章</span>
              <span>{formatChars(pickedChars)}</span>
              <span>约 {formatMinutes(estimateMinutes(pickedChars))}</span>
              {showPoints && <span>共 <span className="font-medium text-gray-700">{pickedCost}</span> 积分</span>}
            </div>
            {insufficient && (
              <p className="mt-1 text-[11px] text-red-600">
                余额不足（差 {pickedCost - (balance ?? 0)} 积分），请减少勾选或先充值
              </p>
            )}
            <button
              type="button"
              disabled={!canGenerate || picked.length === 0 || submitting || insufficient}
              onClick={onGenerateSelected}
              className="mt-2 w-full inline-flex items-center justify-center gap-1.5 h-8 rounded-lg bg-emerald-500 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {submitting
                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                : <Zap className="w-3.5 h-3.5" />}
              生成选中的 {picked.length} 章
            </button>
          </div>
        </CardContent>
      )}
    </Card>
  );
}
