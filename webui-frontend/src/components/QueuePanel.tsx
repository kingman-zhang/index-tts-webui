import { useState, useEffect, useRef } from "react";
import { ListVideo, Trash2, Square, CheckCircle2, XCircle, Clock, Loader2, Download, Play, RefreshCw, GripVertical, Pause, PlayCircle } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, Button, Badge } from "./ui";
import { api } from "@/api/client";
import { cn } from "@/lib/utils";

interface QueueTask {
  id: string;
  project_name: string;
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
}

interface QueuePanelProps {
  collapsed: boolean;
  onToggle: () => void;
  refreshKey: number;
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

export function QueuePanel({ collapsed, onToggle, refreshKey }: QueuePanelProps) {
  const [tasks, setTasks] = useState<QueueTask[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [queued, setQueued] = useState(0);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [activeFilter, setActiveFilter] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");

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
    } catch {}
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [refreshKey]);

  const stats = {
    total: tasks.length,
    success: tasks.filter(t => t.status === "success").length,
    failed: tasks.filter(t => t.status === "failed" || t.status === "interrupted").length,
    queued: tasks.filter(t => t.status === "queued").length,
    paused: tasks.filter(t => t.status === "paused").length,
  };

  const filteredTasks = activeFilter === "failed"
    ? tasks.filter(t => t.status === "failed" || t.status === "interrupted")
    : activeFilter
      ? tasks.filter(t => t.status === activeFilter)
      : tasks;

  const toggleFilter = (status: string) => {
    setActiveFilter(current => current === status ? null : status);
  };

  const filterButtonClass = (status: string) => cn(
    "rounded-md px-2 py-1 text-[11px] transition-colors",
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
            <span className="text-xs text-gray-400">{stats.total} 个任务</span>
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
      await api.pauseQueuedTasks();
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
      await api.resumePausedTasks();
      await load();
    } catch (e: any) {
      window.alert(`恢复暂停任务失败: ${e.message}`);
    } finally {
      setBulkBusy(false);
    }
  };

  const viewContent = (task: QueueTask) => {
    const lines = task.lines || [];
    const preview = lines.slice(0, 3).map(line => `${line.speaker}: ${line.text}`).join("\n");
    const suffix = lines.length > 3 ? `\n... 共 ${lines.length} 行` : `\n共 ${lines.length} 行`;
    window.alert(`${task.project_name}\n\n${preview || "暂无台词内容"}${suffix}`);
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

    // 取出所有 queued 任务 ID（按当前展示顺序）
    const queuedIds = tasks.filter(t => t.status === "queued").map(t => t.id);
    const fromIdx = queuedIds.indexOf(draggingId);
    const toIdx = queuedIds.indexOf(targetId);
    if (fromIdx === -1 || toIdx === -1) {
      resetDrag();
      return;
    }

    // 计算新顺序
    const newOrder = [...queuedIds];
    newOrder.splice(fromIdx, 1);
    newOrder.splice(toIdx, 0, draggingId);

    // optimistic 更新：重排 tasks 中的 queued 任务
    setTasks(prev => {
      const running = prev.filter(t => t.status === "running" || t.status === "syncing");
      const queuedTasks = newOrder.map(id => {
        const t = prev.find(x => x.id === id)!;
        return { ...t, queue_position: newOrder.indexOf(id) + 1 };
      });
      const terminal = prev.filter(t => !["queued", "running", "syncing"].includes(t.status));
      return [...running, ...queuedTasks, ...terminal];
    });

    resetDrag();

    try {
      await api.reorderQueue(newOrder);
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
            <span className="text-[11px] text-gray-400">共 {stats.total} 个</span>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {stats.queued > 0 && (
              <button onClick={(e) => { e.stopPropagation(); bulkPause(); }} disabled={bulkBusy} className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-100 disabled:opacity-50" title="暂停全部排队任务">
                <Pause className="w-3 h-3" /> 暂停全部
              </button>
            )}
            {stats.paused > 0 && (
              <button onClick={(e) => { e.stopPropagation(); bulkResume(); }} disabled={bulkBusy} className="inline-flex items-center gap-1 rounded-md border border-indigo-200 px-2 py-1 text-[11px] text-indigo-600 hover:bg-indigo-50 disabled:opacity-50" title="将全部暂停任务重新加入队列">
                <PlayCircle className="w-3 h-3" /> 全部入队
              </button>
            )}
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1 border-t border-gray-100 pt-2" onClick={e => e.stopPropagation()}>
          <button className={filterButtonClass("success")} onClick={() => toggleFilter("success")}>成功 <b>{stats.success}</b></button>
          <button className={filterButtonClass("failed")} onClick={() => toggleFilter("failed")}>失败 <b>{stats.failed}</b></button>
          <button className={filterButtonClass("queued")} onClick={() => toggleFilter("queued")}>排队 <b>{stats.queued}</b></button>
          <button className={filterButtonClass("paused")} onClick={() => toggleFilter("paused")}>暂停 <b>{stats.paused}</b></button>
          {activeFilter && <span className="ml-auto text-[10px] text-indigo-500">再次点击可取消筛选</span>}
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
                  {activeFilter ? "当前状态暂无任务" : "暂无任务"}
                </p>
              ) : filteredTasks.map(task => {
                const cfg = STATUS_CONFIG[task.status as keyof typeof STATUS_CONFIG] || STATUS_CONFIG.queued;
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
                          <span className="shrink-0 w-4 h-4 flex items-center justify-center rounded-full bg-indigo-100 text-indigo-600 text-[10px] font-medium">
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
                        <button onClick={() => viewContent(task)} className="px-1.5 py-0.5 text-[10px] text-gray-500 hover:bg-gray-200 rounded" title="查看任务内容">
                          查看内容
                        </button>
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
                        {(task.status === "queued" || task.status === "running") && (
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

                    {/* 进度信息 */}
                    {(task.status === "running" || task.status === "syncing") && task.total_lines > 0 && (
                      <div className="mt-1.5">
                        <div className="flex items-center justify-between text-[11px] text-gray-500 mb-0.5">
                          <span>{task.message || `第 ${task.current_line}/${task.total_lines} 行`}</span>
                          <span>{Math.round(task.progress * 100)}%</span>
                        </div>
                        <div className="h-1.5 bg-amber-100 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-amber-500 transition-all duration-300"
                            style={{ width: `${task.progress * 100}%` }}
                          />
                        </div>
                      </div>
                    )}

                    {/* 完成信息 */}
                    {task.status === "success" && task.duration_sec && (
                      <p className="text-[11px] text-gray-500 mt-1">时长 {task.duration_sec.toFixed(1)} 秒</p>
                    )}
                    {(task.status === "failed" || task.status === "interrupted") && task.error && (
                      <p className="text-[11px] text-red-500 mt-1 truncate">{task.error}</p>
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
    </Card>
  );
}
