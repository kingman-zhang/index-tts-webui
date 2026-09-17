import { useState } from "react";
import {
  Save, Check, Loader2, Radio, CheckCircle2, XCircle, Pencil, FolderOpen,
  ChevronsUpDown, FileText, Trash2,
} from "lucide-react";
import { Input, Button, Badge } from "./ui";
import type { MonoProjectSnapshot } from "@/lib/projectStore";
import { cn } from "@/lib/utils";

interface HeaderProps {
  name: string;
  onRename: (name: string) => void;
  /** 页面标题/副标题（双人播客 / 单人配音各自传入） */
  title?: string;
  subtitle?: string;
  /** 是否显示「项目」「保存」按钮（双人播客后端项目用） */
  showProjectActions?: boolean;
  onSave?: () => void;
  onLoadProject?: () => void;
  ttsOnline: boolean | null;
  ttsInfo: { model_loaded: boolean } | null;
  saving?: boolean;
  /** 配音模式：项目名右侧的保存图标（点击保存当前项目配置） */
  onSaveProject?: () => void;
  /** 保存成功闪现（短暂显示 ✓） */
  projectSaved?: boolean;
  /** 切换项目下拉：已保存的项目列表 */
  projects?: MonoProjectSnapshot[];
  onSwitchProject?: (id: string) => void;
  onDeleteProject?: (id: string) => void;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function Header({
  name, onRename,
  title = "双人播客工作室", subtitle = "Podcast Studio · powered by IndexTTS2",
  showProjectActions = true, onSave, onLoadProject,
  ttsOnline, ttsInfo, saving = false,
  onSaveProject, projectSaved = false,
  projects, onSwitchProject, onDeleteProject,
}: HeaderProps) {
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const hasSwitcher = !!onSwitchProject;
  // 当前项目 = 与当前项目名同名的存档（保存时同名覆盖，天然对齐）
  const currentId = projects?.find(p => p.name === name)?.id ?? null;

  return (
    <header className="h-14 bg-white border-b border-gray-200 flex items-center justify-between px-4 shrink-0">
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center">
          <Radio className="w-4.5 h-4.5 text-white" />
        </div>
        <div>
          <h1 className="text-sm font-semibold text-gray-800 leading-none">{title}</h1>
          <p className="text-[0.6875rem] text-gray-400 mt-0.5">{subtitle}</p>
        </div>
      </div>

      <div className="flex-1 max-w-sm mx-6 flex items-center gap-1.5">
        <div className="relative flex-1">
          <Input
            value={name}
            onChange={e => onRename(e.target.value)}
            placeholder="项目名称"
            className="text-center font-medium pr-[4.25rem]"
          />
          {/* 名称框内右侧：编辑（聚焦输入框）+ 保存当前项目 */}
          <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-0.5">
            <button
              type="button"
              title="点击修改项目名称"
              aria-label="编辑项目名称"
              onMouseDown={e => e.preventDefault()}
              onClick={e => {
                const input = e.currentTarget.closest(".relative")?.querySelector("input");
                input?.focus();
                input?.select();
              }}
              className="p-1 rounded text-gray-300 hover:text-emerald-600 transition-colors"
            >
              <Pencil className="w-3.5 h-3.5" />
            </button>
            {onSaveProject && (
              <button
                type="button"
                title="保存当前项目"
                aria-label="保存当前项目"
                onMouseDown={e => e.preventDefault()}
                onClick={onSaveProject}
                className={cn(
                  "p-1 rounded transition-colors",
                  projectSaved ? "text-emerald-600" : "text-gray-300 hover:text-emerald-600"
                )}
              >
                {projectSaved ? <Check className="w-3.5 h-3.5" /> : <Save className="w-3.5 h-3.5" />}
              </button>
            )}
          </div>
        </div>

        {/* 名称框外侧：切换项目下拉 */}
        {hasSwitcher && (
          <div className="relative">
            <button
              type="button"
              title="切换项目"
              aria-label="切换项目"
              onMouseDown={e => e.preventDefault()}
              onClick={() => setSwitcherOpen(v => !v)}
              className={cn(
                "h-9 w-9 flex items-center justify-center rounded-lg border transition-colors",
                switcherOpen
                  ? "border-emerald-300 bg-emerald-50 text-emerald-700"
                  : "border-gray-300 bg-white text-gray-400 hover:border-emerald-300 hover:text-emerald-600"
              )}
            >
              <ChevronsUpDown className="w-3.5 h-3.5" />
            </button>
            {switcherOpen && (
              <>
                <div className="fixed inset-0 z-30" onClick={() => setSwitcherOpen(false)} />
                <div className="absolute right-0 top-full mt-1.5 z-40 w-72 rounded-xl border border-gray-200 bg-white shadow-lg p-1.5">
                  <p className="px-2 py-1.5 text-[0.6875rem] text-gray-400">
                    切换到已保存的项目（{(projects?.length ?? 0)} 份）
                  </p>
                  {!projects || projects.length === 0 ? (
                    <p className="px-2 pb-2 pt-1 text-xs text-gray-400">
                      暂无存档，点击项目名右侧的保存图标可保存当前项目
                    </p>
                  ) : (
                    <div className="max-h-64 overflow-y-auto scrollbar-thin space-y-0.5">
                      {projects.map(p => {
                        const isCurrent = p.id === currentId;
                        return (
                          <div
                            key={p.id}
                            onClick={() => {
                              if (isCurrent) return;
                              onSwitchProject!(p.id);
                              setSwitcherOpen(false);
                            }}
                            className={cn(
                              "group flex items-center gap-2 px-2 py-1.5 rounded-lg transition-colors",
                              isCurrent
                                ? "bg-emerald-50 cursor-not-allowed"
                                : "hover:bg-indigo-50/60 cursor-pointer"
                            )}
                          >
                            <FileText className={cn("w-3.5 h-3.5 shrink-0", isCurrent ? "text-emerald-500" : "text-gray-400")} />
                            <div className="min-w-0 flex-1">
                              <p className={cn("text-xs font-medium truncate", isCurrent ? "text-emerald-700" : "text-gray-700")}>
                                {p.name}
                              </p>
                              <p className="text-[0.6875rem] text-gray-400">
                                {fmtTime(p.savedAt)} · {p.text.replace(/\s/g, "").length} 字
                              </p>
                            </div>
                            {isCurrent ? (
                              <span className="text-[0.625rem] text-emerald-600 font-medium shrink-0 px-1.5 py-0.5 rounded-full bg-emerald-100">
                                当前
                              </span>
                            ) : (
                              <button
                                type="button"
                                title="删除该项目存档"
                                onClick={e => { e.stopPropagation(); onDeleteProject?.(p.id); }}
                                className="text-gray-300 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                              >
                                <Trash2 className="w-3.5 h-3.5" />
                              </button>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2">
        {/* TTS 状态 */}
        <div className="flex items-center gap-1.5 mr-2">
          {ttsOnline === null ? (
            <Loader2 className="w-3.5 h-3.5 text-gray-400 animate-spin" />
          ) : ttsOnline ? (
            <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />
          ) : (
            <XCircle className="w-3.5 h-3.5 text-red-400" />
          )}
          <span className="text-xs text-gray-500">
            {ttsOnline === null ? "检测中" : ttsOnline ? (ttsInfo?.model_loaded ? "TTS 就绪" : "模型加载中") : "TTS 离线"}
          </span>
        </div>

        <Badge color={ttsOnline ? "green" : "red"}>
          {ttsOnline ? "在线" : "离线"}
        </Badge>

        {showProjectActions && (
          <>
            <Button variant="outline" size="sm" onClick={onLoadProject}>
              项目
            </Button>
            <Button size="sm" icon={saving ? Loader2 : Save} onClick={onSave} disabled={saving}>
              {saving ? "保存中" : "保存"}
            </Button>
          </>
        )}
      </div>
    </header>
  );
}
