import { useState, useEffect, useCallback } from "react";
import { FolderOpen, Trash2, X, FileText } from "lucide-react";
import { Header } from "./components/Header";
import { SpeakerPanel } from "./components/SpeakerPanel";
import { ScriptEditor } from "./components/ScriptEditor";
import { ParamsPanel } from "./components/ParamsPanel";
import { OutputPanel } from "./components/OutputPanel";
import { GlossaryPanel } from "./components/GlossaryPanel";
import { QueuePanel } from "./components/QueuePanel";
import { Button, Card, EmptyState, Badge } from "./components/ui";
import { api } from "./api/client";
import {
  defaultProject, makeLine, defaultEmotion, defaultParams, defaultSilence, type PodcastProject, type PodcastLine,
  type VoiceFile, type TaskInfo,
} from "./types";

export default function App() {
  const [project, setProject] = useState<PodcastProject>(defaultProject());
  const [voiceFiles, setVoiceFiles] = useState<VoiceFile[]>([]);
  const [ttsOnline, setTtsOnline] = useState<boolean | null>(null);
  const [ttsInfo, setTtsInfo] = useState<{ model_loaded: boolean } | null>(null);
  const [saving, setSaving] = useState(false);

  // 生成相关
  const [task, setTask] = useState<TaskInfo | null>(null);
  const [generating, setGenerating] = useState(false);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [durationSec, setDurationSec] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // 折叠状态
  const [glossaryCollapsed, setGlossaryCollapsed] = useState(true);
  const [queueCollapsed, setQueueCollapsed] = useState(false);
  const [queueRefreshKey, setQueueRefreshKey] = useState(0);

  // 项目列表
  const [showProjects, setShowProjects] = useState(false);
  const [projectList, setProjectList] = useState<any[]>([]);
  const [toast, setToast] = useState<string | null>(null);

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(null), 2500); };

  // ─── 初始化：检查 TTS 状态 + 加载参考音频 ─────────────────
  useEffect(() => {
    const init = async () => {
      try {
        const cfg = await api.getConfig();
        setTtsOnline(cfg.tts_online);
        setTtsInfo(cfg.tts_info);
      } catch { setTtsOnline(false); }
      try {
        const v = await api.listVoices();
        setVoiceFiles(v.voices);
      } catch { /* 忽略 */ }
    };
    init();
    const timer = setInterval(init, 30000); // 每 30 秒刷新状态
    return () => clearInterval(timer);
  }, []);

  const reloadVoices = useCallback(async () => {
    try { const v = await api.listVoices(); setVoiceFiles(v.voices); } catch {}
  }, []);

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

  const handleImportText = async (text: string): Promise<PodcastLine[]> => {
    const r = await api.importText(text, project.voices.A.name, project.voices.B.name);
    return r.lines.map((l: any) => {
      const spk = (l.speaker as "A" | "B") || "A";
      const defaultEmo = project.voices[spk]?.emotion;
      return {
        ...l, id: Math.random().toString(36).slice(2, 10),
        emotion: defaultEmo
          ? { ...defaultEmo, vector: [...defaultEmo.vector] }
          : { mode: 0, audio_path: null, vector: Array(8).fill(0), weight: 0.65, text: null, random: false },
      };
    });
  };

  const handleImportConfig = (config: {
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
          name: key === "A" ? nextVoices[key].name : nextVoices[key].name,
        };
      }
      const rawParams = config.params || {};
      const importedParams = { ...defaultParams(), ...rawParams };
      const speakerSpeeds = (rawParams as { speaker_speeds?: Record<string, number> }).speaker_speeds || {};
      for (const key of ["A", "B"] as const) {
        const speed = speakerSpeeds[key];
        if (Number.isFinite(Number(speed))) nextVoices[key] = { ...nextVoices[key], speed: Number(speed) };
      }
      const importedSilence = { ...defaultSilence(), ...(config.silence || {}) };
      const importedLines = config.lines || [];
      const nextVoicesWithEmotions = { ...nextVoices };
      for (const key of ["A", "B"] as const) {
        const firstLine = importedLines.find(line => line.speaker === key);
        if (firstLine) nextVoicesWithEmotions[key] = {
          ...nextVoicesWithEmotions[key],
          emotion: { ...defaultEmotion(), ...firstLine.emotion, vector: [...firstLine.emotion.vector] },
        };
      }
      return {
        ...prev,
        name: config.projectName || prev.name,
        voices: nextVoicesWithEmotions,
        silence: importedSilence,
        params: importedParams,
      };
    });
    showToast("已导入脚本及音色、情感、静音和生成参数");
  };

  // ─── 生成播客（提交到任务队列） ────────────────────────────
  // 根据脚本中实际出现的角色判断哪些角色需要音色，而非硬编码 A 和 B 都必须有。
  const activeSpeakers = Array.from(new Set(
    project.lines.filter(l => l.text.trim().length > 0).map(l => l.speaker)
  )) as ("A" | "B")[];
  const canGenerate =
    activeSpeakers.length > 0 &&
    activeSpeakers.every(s => !!project.voices[s]?.voice_path);

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
        speakerSpeeds[s] = project.voices[s]?.speed ?? project.params.speed ?? 1.0;
      }

      // 调试用：输出最终提交的 JSONL 到控制台
      const debugJSONL = lines.map(l => JSON.stringify({
        text: l.text,
        role: l.speaker,
        ...(l.emotion ? { emotion: l.emotion } : {}),
        ...(l.silence_after_ms !== undefined ? { silence_after_ms: l.silence_after_ms } : {}),
      })).join("\n");
      console.log("%c[播客提交] 最终 JSONL：", "color: #6366f1; font-weight: bold;");
      console.log(debugJSONL);
      console.log("%c[播客提交] voices：", "color: #6366f1; font-weight: bold;", voices);
      console.log("%c[播客提交] params：", "color: #6366f1; font-weight: bold;", {
        ...project.params,
        speaker_speeds: speakerSpeeds,
      });

      const result = await api.submitToQueue({
        project_name: project.name,
        lines,
        voices,
        silence: project.silence,
        params: {
          ...project.params,
          speaker_speeds: speakerSpeeds,
        },
        glossary_enabled: true,
      });
      showToast(`已加入队列（位置 ${result.queue_position}）`);
      setQueueRefreshKey(k => k + 1);  // 触发队列面板刷新
      setQueueCollapsed(false);  // 展开队列面板
      setGenerating(false);
    } catch (e: any) {
      setGenerating(false);
      setError(e.message);
      showToast(`提交失败: ${e.message}`);
    }
  };

  const handleReset = () => {
    setTask(null); setAudioUrl(null); setError(null); setGenerating(false); setDurationSec(0);
  };

  // ─── 布局 ─────────────────────────────────────────────────
  return (
    <div className="h-screen flex flex-col bg-gray-50 overflow-hidden">
      <Header
        project={project}
        onRename={name => updateProject({ name })}
        onSave={handleSave}
        onLoadProject={handleOpenProjects}
        ttsOnline={ttsOnline}
        ttsInfo={ttsInfo}
        saving={saving}
      />

      <div className="flex-1 flex gap-3 p-3 overflow-hidden">
        {/* 左侧：角色配置 + 术语表 */}
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
              onImport={handleImportText}
              onImportConfig={handleImportConfig}
            />
          </Card>
        </main>

        {/* 右侧：参数 + 输出 */}
        <aside className="w-80 shrink-0 overflow-y-auto scrollbar-thin space-y-3">
          <div className="flex items-center gap-2">
            <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">参数与生成</h2>
          </div>
          <ParamsPanel
            silence={project.silence}
            params={project.params}
            onSilenceChange={silence => updateProject({ silence })}
            onParamsChange={params => updateProject({ params })}
          />
          <OutputPanel
            onGenerate={handleGenerate}
            canGenerate={canGenerate}
            task={task}
            generating={generating}
            audioUrl={audioUrl}
            durationSec={durationSec}
            error={error}
            onReset={handleReset}
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
                        <span className="text-[11px] text-gray-400">
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

      {/* Toast 提示 */}
      {toast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50">
          <div className="bg-gray-800 text-white text-sm px-4 py-2 rounded-lg shadow-lg">
            {toast}
          </div>
        </div>
      )}
    </div>
  );
}
