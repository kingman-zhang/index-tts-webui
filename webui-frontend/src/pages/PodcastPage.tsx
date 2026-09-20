import { useState, useEffect } from "react";
import { FolderOpen, Trash2, X, FileText } from "lucide-react";
import { Header } from "../components/Header";
import { SpeakerPanel } from "../components/SpeakerPanel";
import { MonoEditor } from "../components/MonoEditor";
import { GlossaryPanel } from "../components/GlossaryPanel";
import { QueuePanel } from "../components/QueuePanel";
import { Card, EmptyState, Badge } from "../components/ui";
import { api } from "../api/client";
import { useAppInit, useToast, ToastNode } from "../hooks/useAppInit";
import {
  defaultProject, defaultEmotion, defaultParams, defaultSilence,
  textToPodcastSegments, podcastScriptIssues, podcastLinesToScript,
  dialogTextToScript, emotionFromLabel,
  type PodcastProject, type PodcastLine, type SpeakerConfig,
} from "../types";
import { useAuth, refreshUser } from "@/lib/auth";

const PODCAST_DRAFT_KEY = "wb-podcast-draft-v1";

interface PodcastDraft {
  name: string;
  voices: { A: SpeakerConfig; B: SpeakerConfig };
  script: string;
}

function loadPodcastDraft(): PodcastDraft {
  const empty: PodcastDraft = {
    name: defaultProject().name,
    voices: defaultProject().voices,
    script: "",
  };
  try {
    const raw = localStorage.getItem(PODCAST_DRAFT_KEY);
    if (raw) {
      const d = JSON.parse(raw);
      if (d && typeof d === "object" && typeof d.script === "string") {
        return {
          name: typeof d.name === "string" && d.name ? d.name : empty.name,
          voices: {
            A: { ...empty.voices.A, ...(d.voices?.A || {}) },
            B: { ...empty.voices.B, ...(d.voices?.B || {}) },
          },
          script: d.script,
        };
      }
    }
  } catch { /* 忽略损坏的草稿 */ }
  return empty;
}

/** 双人播客页（/podcast）：与单人配音同构的画布编辑器 + 主持人标识块。
 *  画布文本是唯一真源；静音/生成参数由后端 .env 默认值控制。 */
export default function PodcastPage() {
  const initial = loadPodcastDraft();
  const [name, setName] = useState(initial.name);
  const [voices, setVoices] = useState<PodcastDraft["voices"]>(initial.voices);
  const [script, setScript] = useState(initial.script);
  const [saving, setSaving] = useState(false);
  const [projectId, setProjectId] = useState<string | undefined>(undefined);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [glossaryCollapsed, setGlossaryCollapsed] = useState(false);
  const [queueCollapsed, setQueueCollapsed] = useState(false);
  const [queueRefreshKey, setQueueRefreshKey] = useState(0);
  const [showProjects, setShowProjects] = useState(false);
  const [projectList, setProjectList] = useState<any[]>([]);

  const { voiceFiles, ttsOnline, ttsInfo, reloadVoices, memberEnforce, memberPer1000 } = useAppInit();
  const { user } = useAuth();
  const { toast, showToast } = useToast();

  // 草稿自动保存（刷新不丢；script 为画布文本唯一真源）
  useEffect(() => {
    localStorage.setItem(PODCAST_DRAFT_KEY, JSON.stringify({ name, voices, script }));
  }, [name, voices, script]);

  // ─── 角色与音色 ───────────────────────────────────────────
  const updateSpeaker = (key: "A" | "B", config: Partial<SpeakerConfig>) =>
    setVoices(v => ({ ...v, [key]: { ...v[key], ...config } }));

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
    setVoices(v => ({
      A: v.A.voice_name === name ? { ...v.A, voice_name: null, voice_path: null } : v.A,
      B: v.B.voice_name === name ? { ...v.B, voice_name: null, voice_path: null } : v.B,
    }));
    showToast(`已删除音色：${name}`);
  };

  // ─── 项目存档（后端存储；画布文本随项目保存） ───────────────
  const parsed = textToPodcastSegments(script);

  const buildProject = (): PodcastProject => ({
    id: projectId,
    name,
    voices,
    // lines 供后端 line_count 统计与旧版兼容；真源是 script
    lines: parsed.map(s => ({
      id: Math.random().toString(36).slice(2, 10),
      speaker: s.speaker,
      text: s.text,
      emotion: s.emotion_label ? emotionFromLabel(s.emotion_label) : defaultEmotion(),
      ...(s.silence_after_ms !== undefined ? { silence_after_ms: s.silence_after_ms } : {}),
    })),
    script,
    silence: defaultSilence(),
    params: defaultParams(),
  });

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await api.saveProject(buildProject());
      setProjectId(result.id);
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
      const vA: SpeakerConfig = { ...defaultProject().voices.A, ...p.voices.A };
      const vB: SpeakerConfig = { ...defaultProject().voices.B, ...p.voices.B };
      if (!vA.emotion) vA.emotion = fallbackEmo;
      if (!vB.emotion) vB.emotion = fallbackEmo;
      // 画布文本：新项目带 script；旧项目从 lines 迁移
      const scriptText = typeof p.script === "string" ? p.script : podcastLinesToScript(p.lines || []);
      setProjectId(p.id);
      setName(p.name);
      setVoices({ A: vA, B: vB });
      setScript(scriptText);
      setShowProjects(false);
      setError(null); setGenerating(false);
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

  // ─── 生成播客（提交到任务队列；kind 默认 podcast） ──────────
  const activeSpeakers = Array.from(new Set(
    parsed.map(s => s.speaker).filter((s): s is "A" | "B" => s !== null)
  ));
  // 积分预估（与后端 estimate_task_cost 同口径：行文本长度求和后按 1000 字向上取整）
  const totalChars = parsed.reduce((n, s) => n + s.text.trim().length, 0);
  const pointsCost = memberEnforce && memberPer1000 > 0
    ? Math.ceil(totalChars / 1000) * memberPer1000
    : 0;
  const balance = user?.points ?? null;
  const pointsInsufficient = pointsCost > 0 && balance != null && pointsCost > balance;
  const pointsInfo = pointsCost > 0 ? { cost: pointsCost, balance } : null;
  const canGenerate =
    parsed.length > 0 &&
    activeSpeakers.length > 0 &&
    activeSpeakers.every(s => !!voices[s]?.voice_path) &&
    !pointsInsufficient;

  const handleGenerate = async () => {
    setError(null);
    // 每个非空视觉行必须有主持人标识（画布行首【A】/【B】）
    const issues = podcastScriptIssues(script);
    if (issues.length > 0) {
      const indexes = issues.slice(0, 10).join(", ");
      const suffix = issues.length > 10 ? " 等" : "";
      const message = `第 ${indexes}${suffix} 行未指定主持人，请把光标放到该行后用工具栏「A 发言 / B 发言」标注`;
      setError(message);
      showToast(message);
      return;
    }
    setGenerating(true);
    try {
      const voicesPayload: Record<string, string> = {};
      for (const s of activeSpeakers) {
        if (voices[s]?.voice_path) voicesPayload[s] = voices[s].voice_path;
      }
      const lines = parsed.map(s => ({
        speaker: s.speaker,
        text: s.text,
        ...(s.emotion_label ? { emotion: emotionFromLabel(s.emotion_label) } : {}),
        ...(s.silence_after_ms !== undefined ? { silence_after_ms: s.silence_after_ms } : {}),
      }));
      const speakerSpeeds: Record<string, number> = {};
      for (const s of activeSpeakers) {
        speakerSpeeds[s] = voices[s]?.speed ?? 1.0;
      }
      // 静音/生成参数不在前端配置：省略后由后端 .env 默认值（PODCAST_SILENCE_* / PODCAST_GEN_PARAMS）兜底
      const result = await api.submitToQueue({
        project_name: name,
        lines,
        voices: voicesPayload,
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

  // ─── 布局 ─────────────────────────────────────────────────
  return (
    <div className="h-screen flex flex-col bg-gray-50 overflow-hidden">
      <Header
        name={name}
        onRename={setName}
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
            speakers={voices}
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

        {/* 中间：画布编辑器（含情绪/停顿/导入/生成配音工具条） */}
        <main className="flex-1 min-w-0">
          <MonoEditor
            text={script}
            onChange={setScript}
            onGenerate={handleGenerate}
            canGenerate={canGenerate}
            generating={generating}
            error={error}
            pointsInfo={pointsInfo}
            speakers={{ A: voices.A.name, B: voices.B.name }}
            importTransform={dialogTextToScript}
          />
        </main>

        {/* 右侧：队列（静音/生成参数已收编为后端 .env 默认值） */}
        <aside className="w-80 shrink-0 overflow-y-auto scrollbar-thin">
          <div className="flex items-center gap-2">
            <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">生成</h2>
          </div>
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
