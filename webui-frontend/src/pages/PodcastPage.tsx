import { useState, useCallback } from "react";
import { FolderOpen, Trash2, X, FileText } from "lucide-react";
import { Header } from "../components/Header";
import { SpeakerPanel } from "../components/SpeakerPanel";
import { ScriptEditor } from "../components/ScriptEditor";
import { OutputPanel } from "../components/OutputPanel";
import { GlossaryPanel } from "../components/GlossaryPanel";
import { QueuePanel } from "../components/QueuePanel";
import { Card, EmptyState, Badge } from "../components/ui";
import { api } from "../api/client";
import { useAppInit, useToast, ToastNode } from "../hooks/useAppInit";
import {
  defaultProject, defaultEmotion,
  type PodcastProject, type PodcastLine, type TaskInfo,
} from "../types";
import { useAuth, refreshUser } from "@/lib/auth";

/** 双人播客页（/podcast）：角色配置 + 对话脚本 + 输出。静音/生成参数由后端 .env 默认值控制，不再暴露 UI。 */
export default function PodcastPage() {
  const [project, setProject] = useState<PodcastProject>(defaultProject());
  const [saving, setSaving] = useState(false);
  const [task, setTask] = useState<TaskInfo | null>(null);
  const [generating, setGenerating] = useState(false);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [durationSec, setDurationSec] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [glossaryCollapsed, setGlossaryCollapsed] = useState(false);
  const [queueCollapsed, setQueueCollapsed] = useState(false);
  const [queueRefreshKey, setQueueRefreshKey] = useState(0);
  const [showProjects, setShowProjects] = useState(false);
  const [projectList, setProjectList] = useState<any[]>([]);

  const { voiceFiles, ttsOnline, ttsInfo, reloadVoices, memberEnforce, memberPer1000 } = useAppInit();
  const { user } = useAuth();
  const { toast, showToast } = useToast();

  // ─── 项目操作 ─────────────────────────────────────────────
  const updateProject = (patch: Partial<PodcastProject>) => setProject(p => ({ ...p, ...patch }));
  const updateSpeaker = (key: "A" | "B", config: Partial<typeof project.voices.A>) =>
    setProject(p => ({ ...p, voices: { ...p.voices, [key]: { ...p.voices[key], ...config } } }));

  const handleUploadVoice = async (file: File, customName?: string): Promise<{ name: string; path: string } | null> => {
    try {
      const result = await api.uploadVoice(file, customName);
      await reloadVoices();
      showToast(`音频已保存：${result.name}`);
      return result;
    } catch (e: any) {
      showToast(`上传失败: ${e.message}`);
      return null;
    }
  };

  const handleRenameVoice = async (oldName: string, newName: string) => {
    const result = await api.renameVoice(oldName, newName);
    await reloadVoices();
    showToast(`音频已改名：${result.name}`);
  };

  const handleDeleteVoice = async (name: string) => {
    await api.deleteVoice(name);
    await reloadVoices();
    setProject(p => ({
      ...p,
      voices: {
        A: p.voices.A.voice_name === name ? { ...p.voices.A, voice_name: null, voice_path: null } : p.voices.A,
        B: p.voices.B.voice_name === name ? { ...p.voices.B, voice_name: null, voice_path: null } : p.voices.B,
      },
    }));
    showToast(`已删除音色：${name}`);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await api.saveProject(project);
      updateProject({ id: result.id });
      showToast("项目已保存");
    } catch (e: any) {
      showToast(`保存失败: ${e.message}`);
    } finally { setSaving(false); }
  };

  const handleOpenProjects = async () => {
    try {
      const r = await api.listProjects();
      setProjectList(r.projects);
      setShowProjects(true);
    } catch (e: any) { showToast(`加载项目列表失败: ${e.message}`); }
  };

  const handleLoadProject = async (id: string) => {
    try {
      const p = await api.getProject(id);
      // 补全 voices 的 emotion 字段（兼容旧项目）
      const fallbackEmo = { mode: 0 as const, audio_path: null, vector: Array(8).fill(0), weight: 0.65, text: null, random: false };
      if (!p.voices.A.emotion) p.voices.A.emotion = fallbackEmo;
      if (!p.voices.B.emotion) p.voices.B.emotion = fallbackEmo;
      // 补全 lines 的 id
      p.lines = p.lines.map((l: any) => ({
        ...l, id: l.id || Math.random().toString(36).slice(2, 10),
        emotion: l.emotion || { ...fallbackEmo },
      }));
      setProject(p);
      setShowProjects(false);
      setTask(null); setAudioUrl(null); setError(null); setGenerating(false);
      showToast(`已加载: ${p.name}`);
    } catch (e: any) { showToast(`加载失败: ${e.message}`); }
  };

  const handleDeleteProject = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await api.deleteProject(id);
      setProjectList(projectList.filter(p => p.id !== id));
      showToast("已删除");
    } catch (e2: any) { showToast(`删除失败: ${e2.message}`); }
  };

  const handleImportConfig = useCallback((config: {
    voices?: Record<string, string>;
    silence?: Partial<PodcastProject["silence"]>;
    params?: Record<string, unknown>;
    lines?: PodcastLine[];
    projectName?: string;
  }) => {
    setProject(prev => {
      const nextVoices = { ...prev.voices };
      for (const key of ["A", "B"] as const) {
        const path = config.voices?.[key];
        if (!path) continue;
        const fileName = path.split("/").pop()?.split("\\\\").pop() || path;
        nextVoices[key] = {
          ...nextVoices[key],
          voice_path: path,
          voice_name: fileName,
        };
      }
      // 静音/生成参数已由后端 .env 默认值统一管理，导入时只取角色语速
      const speakerSpeeds = (config.params as { speaker_speeds?: Record<string, number> })?.speaker_speeds || {};
      for (const key of ["A", "B"] as const) {
        const speed = speakerSpeeds[key];
        if (Number.isFinite(Number(speed))) nextVoices[key] = { ...nextVoices[key], speed: Number(speed) };
      }
      return {
        ...prev,
        name: config.projectName || prev.name,
        voices: nextVoices,
      };
    });
    showToast("已导入脚本及音色、角色语速（静音/生成参数使用系统默认）");
  }, [showToast]);

  // ─── 生成播客（提交到任务队列） ────────────────────────────
  // 根据脚本中实际出现的角色判断哪些角色需要音色，而非硬编码 A 和 B 都必须有。
  const activeSpeakers = Array.from(new Set(
    project.lines.filter(l => l.text.trim().length > 0).map(l => l.speaker)
  )) as ("A" | "B")[];
  // 积分预估（与后端 estimate_task_cost 同口径：行文本 trim 后按 1000 字向上取整）
  const totalChars = project.lines.reduce((n, l) => n + l.text.trim().length, 0);
  const pointsCost = memberEnforce && memberPer1000 > 0
    ? Math.ceil(totalChars / 1000) * memberPer1000
    : 0;
  const balance = user?.points ?? null;
  const pointsInsufficient = pointsCost > 0 && balance != null && pointsCost > balance;
  const pointsInfo = pointsCost > 0 ? { cost: pointsCost, balance } : null;
  const canGenerate =
    activeSpeakers.length > 0 &&
    activeSpeakers.every(s => !!project.voices[s]?.voice_path) &&
    !pointsInsufficient;

  const handleGenerate = async () => {
    setError(null);
    const blankLines = project.lines
      .map((line, index) => ({ line, index: index + 1 }))
      .filter(({ line }) => !line.text.trim());
    if (blankLines.length > 0) {
      const indexes = blankLines.slice(0, 10).map(({ index }) => index).join(", ");
      const suffix = blankLines.length > 10 ? " 等" : "";
      const message = `第 ${indexes}${suffix} 行台词为空，请补充内容后再提交`;
      setError(message);
      showToast(message);
      return;
    }
    setGenerating(true);
    try {
      const voices: Record<string, string> = {};
      for (const s of activeSpeakers) {
        if (project.voices[s]?.voice_path) voices[s] = project.voices[s].voice_path;
      }
      const lines = project.lines.map(l => ({
        speaker: l.speaker,
        text: l.text,
        emotion: l.emotion,
        silence_after_ms: l.silence_after_ms,
      }));

      const speakerSpeeds: Record<string, number> = {};
      for (const s of activeSpeakers) {
        speakerSpeeds[s] = project.voices[s]?.speed ?? 1.0;
      }

      // 静音/生成参数不在前端配置：省略后由后端 .env 默认值（PODCAST_SILENCE_* / PODCAST_GEN_PARAMS）兜底
      const result = await api.submitToQueue({
        project_name: project.name,
        lines,
        voices,
        params: { speaker_speeds: speakerSpeeds },
        glossary_enabled: true,
      });
      showToast(`已加入队列（位置 ${result.queue_position}）`);
      setQueueRefreshKey(k => k + 1);  // 触发队列面板刷新
      setQueueCollapsed(false);  // 展开队列面板
      setGenerating(false);
      refreshUser();  // 扣费后刷新余额
    } catch (e: any) {
      setGenerating(false);
      setError(e.message);
      if ((e.message || "").includes("积分不足")) {
        showToast("积分不足：可在右上角菜单进入个人中心，用兑换码充值");
      } else {
        showToast(`提交失败: ${e.message}`);
      }
    }
  };

  const handleReset = () => {
    setTask(null); setAudioUrl(null); setError(null); setGenerating(false); setDurationSec(0);
  };

  // ─── 布局 ─────────────────────────────────────────────────
  return (
    <div className="h-screen flex flex-col bg-gray-50 overflow-hidden">
      <Header
        name={project.name}
        onRename={name => updateProject({ name })}
        onSave={handleSave}
        onLoadProject={handleOpenProjects}
        ttsOnline={ttsOnline}
        ttsInfo={ttsInfo}
        saving={saving}
      />

      <div className="flex-1 flex gap-3 p-3 overflow-hidden">
        {/* 左侧：角色/音色配置 + 术语表 */}
        <aside className="w-96 shrink-0 overflow-y-auto scrollbar-thin">
          <div className="mb-2 flex items-center gap-2">
            <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">角色与音色</h2>
          </div>
          <SpeakerPanel
            speakers={project.voices}
            onChange={updateSpeaker}
            voiceFiles={voiceFiles}
            onUpload={handleUploadVoice}
            onRenameVoice={handleRenameVoice}
            onDeleteVoice={handleDeleteVoice}
          />
          <div className="mt-3">
            <GlossaryPanel
              collapsed={glossaryCollapsed}
              onToggle={() => setGlossaryCollapsed(!glossaryCollapsed)}
            />
          </div>
        </aside>

        {/* 中间：脚本编辑器 */}
        <main className="flex-1 min-w-0">
          <Card className="h-full flex flex-col overflow-hidden">
            <ScriptEditor
              lines={project.lines}
              speakers={project.voices}
              voiceFiles={voiceFiles}
              onChange={lines => updateProject({ lines })}
              onImportConfig={handleImportConfig}
            />
          </Card>
        </main>

        {/* 右侧：输出 + 队列（静音/生成参数已收编为后端 .env 默认值） */}
        <aside className="w-80 shrink-0 overflow-y-auto scrollbar-thin space-y-3">
          <div className="flex items-center gap-2">
            <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">生成</h2>
          </div>
          <OutputPanel
            onGenerate={handleGenerate}
            canGenerate={canGenerate}
            task={task}
            generating={generating}
            audioUrl={audioUrl}
            durationSec={durationSec}
            error={error}
            onReset={handleReset}
            pointsInfo={pointsInfo}
          />
          <QueuePanel
            collapsed={queueCollapsed}
            onToggle={() => setQueueCollapsed(!queueCollapsed)}
            refreshKey={queueRefreshKey}
          />
        </aside>
      </div>

      {/* 项目列表弹窗 */}
      {showProjects && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setShowProjects(false)}>
          <Card className="w-full max-w-lg mx-4" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
              <h3 className="text-sm font-semibold text-gray-800 flex items-center gap-2">
                <FolderOpen className="w-4 h-4 text-indigo-500" /> 我的项目
              </h3>
              <button onClick={() => setShowProjects(false)} className="text-gray-400 hover:text-gray-600">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-2 max-h-96 overflow-y-auto scrollbar-thin">
              {projectList.length === 0 ? (
                <EmptyState icon={FileText} title="还没有保存的项目" hint="点击右上角保存按钮即可创建项目" />
              ) : (
                projectList.map(p => (
                  <div
                    key={p.id}
                    onClick={() => handleLoadProject(p.id)}
                    className="flex items-center justify-between px-3 py-2.5 rounded-lg hover:bg-indigo-50 cursor-pointer transition-colors group"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-gray-700 truncate">{p.name}</p>
                      <div className="flex items-center gap-2 mt-0.5">
                        <Badge color="gray">{p.line_count} 行</Badge>
                        <span className="text-[0.75rem] text-gray-400">
                          {p.updated_at ? new Date(p.updated_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : ""}
                        </span>
                      </div>
                    </div>
                    <button
                      onClick={e => handleDeleteProject(p.id, e)}
                      className="p-1.5 rounded opacity-0 group-hover:opacity-100 hover:bg-red-50 transition-all"
                    >
                      <Trash2 className="w-3.5 h-3.5 text-red-400" />
                    </button>
                  </div>
                ))
              )}
            </div>
          </Card>
        </div>
      )}

      <ToastNode toast={toast} />
    </div>
  );
}
