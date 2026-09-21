import { useState, useEffect, useRef } from "react";
import { ListVideo, Trash2, Square, CheckCircle2, XCircle, Clock, Loader2, Download, Play, RefreshCw, GripVertical, Pause, PlayCircle, AlertCircle, X } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, Button, Badge } from "./ui";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";

interface QueueTask {
  id: string;
  project_name: string;
  kind?: string; // podcast=双人播客；mono=单音色配音
  status: string;
  progress: number;
  current_line: number;
  total_lines: number;
  message: string;
  audio_url?: string;
  duration_sec?: number;
  error?: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  lines?: { speaker: string; text: string }[];
  voices?: Record<string, string>;
  silence?: any;
  params?: any;
  glossary_enabled?: boolean;
  queue_position?: number | null;
  cancel_requested?: boolean;
}

interface QueuePanelProps {
  collapsed: boolean;
  onToggle: () => void;
  refreshKey: number;
  /** 初始展示的任务类型 tab：播客页传 podcast、配音页传 mono（默认 podcast） */
  defaultKind?: "podcast" | "mono";
}

const STATUS_CONFIG: Record<string, {
  icon: typeof Clock;
  color: string;
  label: string;
  spin?: boolean;
}> = {
  queued: { icon: Clock, color: "blue", label: "排队中" },
  paused: { icon: Pause, color: "gray", label: "已暂停" },
  running: { icon: Loader2, color: "amber", label: "合成中", spin: true },
  success: { icon: CheckCircle2, color: "green", label: "完成" },
  failed: { icon: XCircle, color: "red", label: "失败" },
  syncing: { icon: RefreshCw, color: "amber", label: "等待同步", spin: true },
  interrupted: { icon: XCircle, color: "red", label: "已中断" },
  cancelled: { icon: Square, color: "gray", label: "已取消" },
};

export function QueuePanel({ collapsed, onToggle, refreshKey, defaultKind = "podcast" }: QueuePanelProps) {
  const [tasks, setTasks] = useState<QueueTask[]>([]);
  const [activeKind, setActiveKind] = useState<"podcast" | "mono">(defaultKind);
  const [current, setCurrent] = useState<string | null>(null);
  const [queued, setQueued] = useState(0);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [activeFilter, setActiveFilter] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  // 报错详情弹窗（失败自动弹出；队列内详情图标可再次打开）
  const [errorModal, setErrorModal] = useState<{ name: string; error: string } | null>(null);
  const prevStatusRef = useRef<Map<string, string>>(new Map());
  // 行级进度控制台输出去重：仅在进度实际变化时打印
  const lastProgressRef = useRef<Map<string, string>>(new Map());

  // 拖拽状态
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);
  const isDraggingRef = useRef(false);

  const load = async () => {
    if (isDraggingRef.current) return; // 拖拽中不刷新
    try {
      const r = await api.listQueue();
      setTasks(r.tasks);
      setCurrent(r.current);
      setQueued(r.queued);
      // 任务从进行中转为失败时自动弹出报错详情（首次加载不弹，避免历史失败打扰）
      const prev = prevStatusRef.current;
      if (prev.size > 0) {
        for (const t of r.tasks) {
          const before = prev.get(t.id);
          if (
            before &&
            before !== t.status &&
            (t.status === "failed" || t.status === "interrupted") &&
            t.error
          ) {
            setErrorModal({ name: t.project_name, error: t.error });
            break; // 一次只弹一个，其余通过队列详情图标查看
          }
        }
      }
      prevStatusRef.current = new Map(r.tasks.map(t => [t.id, t.status]));
      // 行级进度（如 第 1/5 行）输出到浏览器控制台，界面不再显示
      for (const t of r.tasks) {
        if ((t.status === "running" || t.status === "syncing") && t.total_lines > 0) {
          const key = `${t.current_line}/${t.total_lines}`;
          if (lastProgressRef.current.get(t.id) !== key) {
            lastProgressRef.current.set(t.id, key);
            console.log(`[进度] ${t.project_name}: 第 ${key} 行（${Math.round(t.progress * 100)}%）`);
          }
        }
      }
    } catch {}
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [refreshKey]);

  const kindOf = (t: QueueTask): "podcast" | "mono" => (t.kind === "mono" ? "mono" : "podcast");

  // 当前 tab 的任务列表（双人播客 / 单人配音 各自独立）
  const kindTasks = tasks.filter(t => kindOf(t) === activeKind);
  const kindCounts = {
    podcast: tasks.filter(t => kindOf(t) === "podcast").length,
    mono: tasks.filter(t => kindOf(t) === "mono").length,
  };

  const stats = {
    total: kindTasks.length,
    success: kindTasks.filter(t => t.status === "success").length,
    failed: kindTasks.filter(t => t.status === "failed" || t.status === "interrupted").length,
    queued: kindTasks.filter(t => t.status === "queued").length,
    paused: kindTasks.filter(t => t.status === "paused").length,
  };

  const filteredTasks = activeFilter === "failed"
    ? kindTasks.filter(t => t.status === "failed" || t.status === "interrupted")
    : activeFilter
      ? kindTasks.filter(t => t.status === activeFilter)
      : kindTasks;

  const toggleFilter = (status: string) => {
    setActiveFilter(current => current === status ? null : status);
  };

  const filterButtonClass = (status: string) => cn(
    "rounded-md px-2 py-1 text-[0.75rem] transition-colors",
    activeFilter === status ? "bg-indigo-100 text-indigo-700 ring-1 ring-indigo-200" : "hover:bg-gray-100 text-gray-500"
  );

  if (collapsed) {
    return (
      <Card>
        <CardHeader className="cursor-pointer" onClick={onToggle}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ListVideo className="w-4 h-4 text-indigo-600" />
              <CardTitle>任务队列</CardTitle>
              {stats.queued > 0 && <Badge color="amber">{stats.queued} 排队</Badge>}
            </div>
            <span className="text-xs text-gray-400">{tasks.length} 个任务</span>
          </div>
        </CardHeader>
      </Card>
    );
  }

  const cancel = async (id: string) => {
    await api.cancelQueueTask(id);
    load();
  };

  const clearFinished = async () => {
    await api.clearFinishedTasks();
    load();
  };

  const retry = async (id: string) => {
    await api.retryQueueTask(id);
    load();
  };

  const bulkPause = async () => {
    if (!stats.queued || bulkBusy) return;
    setBulkBusy(true);
    try {
      await api.pauseQueuedTasks(activeKind);
      await load();
    } catch (e: any) {
      window.alert(`暂停排队任务失败: ${e.message}`);
    } finally {
      setBulkBusy(false);
    }
  };

  const bulkResume = async () => {
    if (!tasks.some(t => t.status === "paused") || bulkBusy) return;
    setBulkBusy(true);
    try {
      await api.resumePausedTasks(activeKind);
      await load();
    } catch (e: any) {
      window.alert(`恢复暂停任务失败: ${e.message}`);
    } finally {
      setBulkBusy(false);
    }
  };

  const beginEditName = (task: QueueTask) => {
    if (!["queued", "failed", "interrupted", "paused", "cancelled"].includes(task.status)) return;
    setEditingId(task.id);
    setEditingName(task.project_name);
  };

  const saveName = async (task: QueueTask) => {
    if (editingId !== task.id) return;
    const nextName = editingName.trim();
    setEditingId(null);
    if (!nextName || nextName === task.project_name) return;
    try {
      await api.updateQueueTaskName(task.id, nextName);
      load();
    } catch (e: any) {
      window.alert(`修改任务名称失败: ${e.message}`);
      load();
    }
  };

  const play = (task: QueueTask) => {
    if (playingId === task.id) {
      setPlayingId(null);
    } else {
      setPlayingId(task.id);
    }
  };

  // ─── 拖拽排序 ───

  const handleDragStart = (e: React.DragEvent, taskId: string) => {
    isDraggingRef.current = true;
    setDraggingId(taskId);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", taskId);
  };

  const handleDragOver = (e: React.DragEvent, taskId: string) => {
    if (!draggingId || taskId === draggingId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverId(taskId);
  };

  const handleDrop = async (e: React.DragEvent, targetId: string) => {
    e.preventDefault();
    if (!draggingId || draggingId === targetId) {
      resetDrag();
      return;
    }

    // 仅重排当前 tab 类型的排队任务（其他类型任务的相对顺序由后端保持）
    const kindQueued = tasks.filter(t => t.status === "queued" && kindOf(t) === activeKind).map(t => t.id);
    const fromIdx = kindQueued.indexOf(draggingId);
    const toIdx = kindQueued.indexOf(targetId);
    if (fromIdx === -1 || toIdx === -1) {
      resetDrag();
      return;
    }

    // 计算当前类型的新顺序
    const newKindOrder = [...kindQueued];
    newKindOrder.splice(fromIdx, 1);
    newKindOrder.splice(toIdx, 0, draggingId);

    // optimistic 更新：按全局排队顺序，把当前类型的排队任务替换为新顺序
    setTasks(prev => {
      const globalQueued = prev.filter(t => t.status === "queued").map(t => t.id);
      const merged: string[] = [];
      let k = 0;
      for (const id of globalQueued) {
        const t = prev.find(x => x.id === id);
        if (t && kindOf(t) === activeKind) merged.push(newKindOrder[k++]);
        else merged.push(id);
      }
      const running = prev.filter(t => t.status === "running" || t.status === "syncing");
      const queuedTasks = merged.map(id => {
        const t = prev.find(x => x.id === id)!;
        return { ...t, queue_position: merged.indexOf(id) + 1 };
      });
      const terminal = prev.filter(t => !["queued", "running", "syncing"].includes(t.status));
      return [...running, ...queuedTasks, ...terminal];
    });

    resetDrag();

    try {
      await api.reorderQueue(newKindOrder, activeKind);
    } catch (e: any) {
      window.alert(`排序失败: ${e.message}`);
      load();
    }
  };

  const resetDrag = () => {
    isDraggingRef.current = false;
    setDraggingId(null);
    setDragOverId(null);
  };

  const handleDragEnd = () => {
    resetDrag();
  };

  return (
    <Card>
      <CardHeader className="cursor-pointer" onClick={onToggle}>
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <ListVideo className="w-4 h-4 shrink-0 text-indigo-600" />
            <CardTitle>任务队列</CardTitle>
            <span className="text-[0.75rem] text-gray-400">{kindCounts[activeKind] ? `共 ${kindCounts[activeKind]} 个` : ""}</span>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {stats.queued > 0 && (
              <button onClick={(e) => { e.stopPropagation(); bulkPause(); }} disabled={bulkBusy} className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-[0.75rem] text-gray-600 hover:bg-gray-100 disabled:opacity-50" title="暂停全部排队任务">
                <Pause className="w-3 h-3" /> 暂停全部
              </button>
            )}
            {stats.paused > 0 && (
              <button onClick={(e) => { e.stopPropagation(); bulkResume(); }} disabled={bulkBusy} className="inline-flex items-center gap-1 rounded-md border border-indigo-200 px-2 py-1 text-[0.75rem] text-indigo-600 hover:bg-indigo-50 disabled:opacity-50" title="将全部暂停任务重新加入队列">
                <PlayCircle className="w-3 h-3" /> 全部入队
              </button>
            )}
          </div>
        </div>
        <div className="mt-2 flex items-center gap-1 border-t border-gray-100 pt-2" onClick={e => e.stopPropagation()}>
          <button
            className={cn(
              "rounded-md px-2.5 py-1 text-[0.75rem] font-medium transition-colors",
              activeKind === "podcast"
                ? "bg-indigo-600 text-white shadow-sm"
                : "text-gray-500 hover:bg-gray-100"
            )}
            onClick={() => setActiveKind("podcast")}
          >
            双人播客 <b>{kindCounts.podcast}</b>
          </button>
          <button
            className={cn(
              "rounded-md px-2.5 py-1 text-[0.75rem] font-medium transition-colors",
              activeKind === "mono"
                ? "bg-indigo-600 text-white shadow-sm"
                : "text-gray-500 hover:bg-gray-100"
            )}
            onClick={() => setActiveKind("mono")}
          >
            单人配音 <b>{kindCounts.mono}</b>
          </button>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1" onClick={e => e.stopPropagation()}>
          <button className={filterButtonClass("success")} onClick={() => toggleFilter("success")}>成功 <b>{stats.success}</b></button>
          <button className={filterButtonClass("failed")} onClick={() => toggleFilter("failed")}>失败 <b>{stats.failed}</b></button>
          <button className={filterButtonClass("queued")} onClick={() => toggleFilter("queued")}>排队 <b>{stats.queued}</b></button>
          <button className={filterButtonClass("paused")} onClick={() => toggleFilter("paused")}>暂停 <b>{stats.paused}</b></button>
          {activeFilter && <span className="ml-auto text-[0.6875rem] text-indigo-500">再次点击可取消筛选</span>}
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {tasks.length === 0 ? (
          <p className="text-xs text-gray-400 text-center py-6">暂无任务</p>
        ) : (
          <>
            {/* 任务列表 */}
            <div className="space-y-1.5 max-h-[520px] overflow-y-auto scrollbar-thin">
              {filteredTasks.length === 0 ? (
                <p className="py-8 text-center text-xs text-gray-400">
                  {activeFilter ? "当前状态暂无任务" : activeKind === "mono" ? "暂无配音任务" : "暂无播客任务"}
                </p>
              ) : filteredTasks.map(task => {
                // 已请求取消的运行中任务显示「取消中」（等待进行中的分段合成结束）
                const cancelling = !!task.cancel_requested && (task.status === "running" || task.status === "syncing");
                const cfg = cancelling
                  ? { icon: Loader2, color: "gray", label: "取消中", spin: true }
                  : STATUS_CONFIG[task.status as keyof typeof STATUS_CONFIG] || STATUS_CONFIG.queued;
                const Icon = cfg.icon;
                const isQueued = task.status === "queued";
                const isDragging = draggingId === task.id;
                const isDragOver = dragOverId === task.id && draggingId !== task.id;
                return (
                  <div
                    key={task.id}
                    draggable={isQueued}
                    onDragStart={isQueued ? (e) => handleDragStart(e, task.id) : undefined}
                    onDragOver={isQueued ? (e) => handleDragOver(e, task.id) : undefined}
                    onDrop={isQueued ? (e) => handleDrop(e, task.id) : undefined}
                    onDragEnd={handleDragEnd}
                    className={cn(
                      "rounded-lg border p-2.5 transition-colors",
                      task.status === "running" || task.status === "syncing" ? "border-amber-300 bg-amber-50" :
                      task.status === "success" ? "border-green-200 bg-green-50" :
                      task.status === "failed" || task.status === "interrupted" ? "border-red-200 bg-red-50" :
                      task.status === "paused" ? "border-gray-300 bg-gray-100" :
                      "border-gray-200 bg-gray-50",
                      isDragging && "opacity-40",
                      isDragOver && "border-t-2 border-t-indigo-400",
                      isQueued && "cursor-grab active:cursor-grabbing"
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        {isQueued && (
                          <GripVertical className="w-3.5 h-3.5 shrink-0 text-gray-300" />
                        )}
                        {isQueued && task.queue_position != null && (
                          <span className="shrink-0 w-4 h-4 flex items-center justify-center rounded-full bg-indigo-100 text-indigo-600 text-[0.6875rem] font-medium">
                            {task.queue_position}
                          </span>
                        )}
                        <Icon className={cn("w-3.5 h-3.5 shrink-0", cfg.spin && "animate-spin")} />
                        {editingId === task.id ? (
                          <input
                            value={editingName}
                            onChange={e => setEditingName(e.target.value)}
                            onBlur={() => saveName(task)}
                            onKeyDown={e => {
                              if (e.key === "Enter") e.currentTarget.blur();
                              if (e.key === "Escape") setEditingId(null);
                            }}
                            autoFocus
                            maxLength={100}
                            className="h-6 min-w-0 flex-1 rounded border border-indigo-300 bg-white px-1.5 text-xs text-gray-700 focus:outline-none focus:ring-1 focus:ring-indigo-400"
                          />
                        ) : (
                          <span
                            className={cn("text-xs font-medium text-gray-700 truncate", ["queued", "failed", "interrupted", "paused", "cancelled"].includes(task.status) && "cursor-text")}
                            onDoubleClick={() => beginEditName(task)}
                            title={["queued", "failed", "interrupted", "paused", "cancelled"].includes(task.status) ? "双击修改任务名称" : task.project_name}
                          >
                            {task.project_name}
                          </span>
                        )}
                        <Badge color={cfg.color as any} className="shrink-0">{cfg.label}</Badge>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {(task.status === "failed" || task.status === "interrupted") && task.error && (
                          <button
                            onClick={() => setErrorModal({ name: task.project_name, error: task.error! })}
                            className="p-1 text-red-500 hover:bg-red-100 rounded"
                            title="查看报错详情"
                          >
                            <AlertCircle className="w-3.5 h-3.5" />
                          </button>
                        )}
                        {task.status === "success" && task.audio_url && (
                          <>
                            <button onClick={() => play(task)} className="p-1 text-indigo-600 hover:bg-indigo-100 rounded">
                              <Play className="w-3.5 h-3.5" />
                            </button>
                            <a href={task.audio_url} download className="p-1 text-green-600 hover:bg-green-100 rounded">
                              <Download className="w-3.5 h-3.5" />
                            </a>
                          </>
                        )}
                        {(task.status === "queued" || task.status === "running") && !task.cancel_requested && (
                          <button onClick={() => cancel(task.id)} className="p-1 text-red-500 hover:bg-red-100 rounded" title="取消">
                            <Square className="w-3.5 h-3.5" />
                          </button>
                        )}
                        {(task.status === "failed" || task.status === "interrupted" || task.status === "paused" || task.status === "cancelled") && (
                          <button onClick={() => retry(task.id)} className="p-1 text-indigo-600 hover:bg-indigo-100 rounded" title="重新提交">
                            <RefreshCw className="w-3.5 h-3.5" />
                          </button>
                        )}
                        {(task.status === "success" || task.status === "failed" || task.status === "interrupted" || task.status === "cancelled") && (
                          <button onClick={() => cancel(task.id)} className="p-1 text-gray-400 hover:bg-gray-200 rounded" title="删除">
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>
                    </div>

                    {/* 进度信息（行级进度 1/5 输出到浏览器控制台，界面只显示百分比） */}
                    {(task.status === "running" || task.status === "syncing") && task.total_lines > 0 && (
                      <div className="mt-1.5">
                        {cancelling && (
                          <p className="text-[0.75rem] text-gray-500 mb-0.5">{task.message || "正在取消，等待进行中的合成结束"}</p>
                        )}
                        <div className={cn("text-right text-[0.75rem] mb-0.5 tabular-nums", cancelling ? "text-gray-400" : "text-gray-500")}>
                          {Math.round(task.progress * 100)}%
                        </div>
                        <div className={cn("h-1.5 rounded-full overflow-hidden", cancelling ? "bg-gray-200" : "bg-amber-100")}>
                          <div
                            className={cn("h-full transition-all duration-300", cancelling ? "bg-gray-400" : "bg-amber-500")}
                            style={{ width: `${task.progress * 100}%` }}
                          />
                        </div>
                      </div>
                    )}

                    {/* 完成信息 */}
                    {task.status === "success" && task.duration_sec && (
                      <p className="text-[0.75rem] text-gray-500 mt-1">时长 {task.duration_sec.toFixed(1)} 秒</p>
                    )}
                    {(task.status === "failed" || task.status === "interrupted") && task.error && (
                      <p className="text-[0.75rem] text-red-500 mt-1 truncate">{task.error}</p>
                    )}

                    {/* 内嵌播放器 */}
                    {playingId === task.id && task.audio_url && (
                      <audio
                        src={task.audio_url}
                        controls
                        autoPlay
                        className="w-full mt-2 h-8"
                        onEnded={() => setPlayingId(null)}
                      />
                    )}
                  </div>
                );
              })}
            </div>

            {/* 清空按钮 */}
            {(stats.success > 0 || stats.failed > 0) && !activeFilter && (
              <Button variant="outline" size="sm" icon={Trash2} onClick={clearFinished} className="w-full">
                清空已完成任务
              </Button>
            )}
          </>
        )}
      </CardContent>

      {/* 报错详情弹窗：失败自动弹出，也可通过队列项的详情图标再次打开 */}
      {errorModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/40" onClick={() => setErrorModal(null)} />
          <div className="relative w-full max-w-lg rounded-2xl bg-white shadow-xl p-5">
            <div className="flex items-center justify-between gap-3 mb-3">
              <div className="flex items-center gap-2 min-w-0">
                <AlertCircle className="w-4 h-4 text-red-500 shrink-0" />
                <h3 className="text-sm font-semibold text-gray-800 truncate">
                  {errorModal.name} · 任务失败
                </h3>
              </div>
              <button
                type="button"
                aria-label="关闭"
                onClick={() => setErrorModal(null)}
                className="p-1 rounded text-gray-400 hover:text-gray-600 transition-colors shrink-0"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="max-h-[50vh] overflow-y-auto scrollbar-thin rounded-lg bg-red-50 border border-red-100 px-3 py-2.5">
              <p className="text-xs text-red-600 leading-5 whitespace-pre-wrap break-words">
                {errorModal.error}
              </p>
            </div>
            <div className="mt-3 flex justify-end">
              <button
                type="button"
                onClick={() => setErrorModal(null)}
                className="h-8 px-4 rounded-lg bg-red-500 text-white text-xs hover:bg-red-400 transition-colors"
              >
                我知道了
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
