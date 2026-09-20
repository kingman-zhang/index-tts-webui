import { useState, useEffect, useRef } from "react";
import { Header } from "../components/Header";
import { MonoEditor } from "../components/MonoEditor";
import { MonoVoiceCard, type MonoVoice } from "../components/MonoVoiceCard";
import { GlossaryPanel } from "../components/GlossaryPanel";
import { QueuePanel } from "../components/QueuePanel";
import { api } from "../api/client";
import { useAppInit, useToast, ToastNode } from "../hooks/useAppInit";
import {
  defaultProjectName, defaultParams, defaultSilence,
  textToMonoLines, monoLinesToText,
} from "../types";
import {
  loadProjects, saveProject, removeProject, type MonoProjectSnapshot,
} from "@/lib/projectStore";

const MONO_DRAFT_KEY = "wb-mono-draft-v2";

interface MonoDraft {
  name: string;
  voice: MonoVoice;
  speed: number;
  text: string;
}

/** v2 草稿 = { name?, voice, speed, text }；读到 v1（逐段模型）时迁移为标记文本 */
function loadMonoDraft(): MonoDraft {
  const empty: MonoDraft = { name: defaultProjectName(), voice: { voice_path: null, voice_name: null }, speed: 1.0, text: "" };
  try {
    const raw = localStorage.getItem(MONO_DRAFT_KEY);
    if (raw) {
      const d = JSON.parse(raw);
      if (d && typeof d === "object" && typeof d.text === "string") {
        return {
          name: typeof d.name === "string" && d.name ? d.name : defaultProjectName(),
          voice: { voice_path: d.voice?.voice_path ?? null, voice_name: d.voice?.voice_name ?? null },
          speed: Number(d.speed) > 0 ? Number(d.speed) : 1.0,
          text: d.text,
        };
      }
    }
    const old = localStorage.getItem("wb-mono-draft-v1");
    if (old) {
      const d = JSON.parse(old);
      if (d && typeof d === "object") {
        return {
          ...empty,
          voice: { voice_path: d.voice?.voice_path ?? null, voice_name: d.voice?.voice_name ?? null },
          speed: Number(d.speed) > 0 ? Number(d.speed) : 1.0,
          text: Array.isArray(d.lines) ? monoLinesToText(d.lines) : "",
        };
      }
    }
  } catch { /* 忽略损坏的草稿 */ }
  return empty;
}

/** 单人配音页（/dubbing）：单音色 + 所见即所得画布 + 队列 */
export default function DubbingPage() {
  const initial = loadMonoDraft();
  const [name, setName] = useState(initial.name);
  const [monoVoice, setMonoVoice] = useState<MonoVoice>(initial.voice);
  const [monoSpeed, setMonoSpeed] = useState<number>(initial.speed);
  const [monoText, setMonoText] = useState<string>(initial.text);

  const { voiceFiles, ttsOnline, ttsInfo } = useAppInit();
  const { toast, showToast } = useToast();

  // 草稿自动保存（含项目名，刷新不丢；text 为画布文本唯一真源）
  useEffect(() => {
    localStorage.setItem(MONO_DRAFT_KEY, JSON.stringify({ name, voice: monoVoice, speed: monoSpeed, text: monoText }));
  }, [name, monoVoice, monoSpeed, monoText]);

  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [glossaryCollapsed, setGlossaryCollapsed] = useState(false);
  const [queueCollapsed, setQueueCollapsed] = useState(false);
  const [queueRefreshKey, setQueueRefreshKey] = useState(0);

  // ─── 项目存档（保存/切换入口在顶部 Header 项目名右侧） ─────
  const [projects, setProjects] = useState<MonoProjectSnapshot[]>(loadProjects);
  const [projectSaved, setProjectSaved] = useState(false);
  const savedFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleSaveProject = () => {
    setProjects(saveProject({ name, voice: monoVoice, speed: monoSpeed, text: monoText }));
    setProjectSaved(true);
    if (savedFlashTimer.current) clearTimeout(savedFlashTimer.current);
    savedFlashTimer.current = setTimeout(() => setProjectSaved(false), 1500);
  };

  const handleSwitchProject = (id: string) => {
    const snap = projects.find(p => p.id === id);
    if (!snap) return;
    if (monoText.trim() && !window.confirm(`切换到「${snap.name}」将替换当前文稿内容，是否继续？`)) return;
    setName(snap.name);
    setMonoVoice(snap.voice);
    setMonoSpeed(snap.speed);
    setMonoText(snap.text);
    showToast(`已切换到「${snap.name}」`);
  };

  const handleDeleteProject = (id: string) => {
    const target = projects.find(p => p.id === id);
    setProjects(removeProject(id));
    if (target) showToast(`已删除存档「${target.name}」`);
  };

  const parsed = textToMonoLines(monoText);
  const canGenerate =
    !!monoVoice.voice_path &&
    parsed.length > 0 &&
    parsed.every(l => l.text.trim().length > 0);

  // ─── 提交（kind=mono，后端走引擎适配层） ───────────────────
  const handleGenerate = async () => {
    setError(null);
    if (!monoVoice.voice_path) {
      const message = "请先在左侧选择配音音色";
      setError(message); showToast(message);
      return;
    }
    const lines = parsed.map(l => ({
      speaker: "A" as const,
      text: l.text,
      emotion: l.emotion_label ? { label: l.emotion_label } : null,
    }));
    setGenerating(true);
    try {
      const params = { ...defaultParams(), speed: monoSpeed, speaker_speeds: { A: monoSpeed } };
      const result = await api.submitToQueue({
        project_name: name,
        kind: "mono",
        lines,
        voices: { A: monoVoice.voice_path },
        silence: defaultSilence(),
        params,
        glossary_enabled: true,
      });
      showToast(`已加入队列（位置 ${result.queue_position}）`);
      setQueueRefreshKey(k => k + 1);
      setQueueCollapsed(false);
      setGenerating(false);
    } catch (e: any) {
      setGenerating(false);
      setError(e.message);
      showToast(`提交失败: ${e.message}`);
    }
  };

  const handleUploadVoice = async (file: File, customName?: string): Promise<{ name: string; path: string } | null> => {
    try {
      const result = await api.uploadVoice(file, customName);
      showToast(`音频已保存：${result.name}`);
      return result;
    } catch (e: any) {
      showToast(`上传失败: ${e.message}`);
      return null;
    }
  };

  // ─── 布局：左音色 / 中画布 / 右队列 ───────────────────────
  return (
    <div className="h-screen flex flex-col bg-gray-50 overflow-hidden">
      <Header
        name={name}
        onRename={setName}
        showProjectActions={false}
        ttsOnline={ttsOnline}
        ttsInfo={ttsInfo}
        onSaveProject={handleSaveProject}
        projectSaved={projectSaved}
        projects={projects}
        onSwitchProject={handleSwitchProject}
        onDeleteProject={handleDeleteProject}
      />

      <div className="flex-1 flex gap-4 p-4 overflow-hidden">
        <aside className="w-72 shrink-0 overflow-y-auto scrollbar-thin space-y-4">
          <MonoVoiceCard
            voice={monoVoice}
            speed={monoSpeed}
            onChange={patch => {
              if (patch.speed !== undefined) setMonoSpeed(patch.speed);
              setMonoVoice(v => ({ ...v, voice_path: patch.voice_path !== undefined ? patch.voice_path : v.voice_path, voice_name: patch.voice_name !== undefined ? patch.voice_name : v.voice_name }));
            }}
            voiceFiles={voiceFiles}
            onUpload={handleUploadVoice}
          />
          <GlossaryPanel
            collapsed={glossaryCollapsed}
            onToggle={() => setGlossaryCollapsed(!glossaryCollapsed)}
          />
        </aside>

        <main className="flex-1 min-w-0">
          <MonoEditor
            text={monoText}
            onChange={setMonoText}
            onGenerate={handleGenerate}
            canGenerate={canGenerate}
            generating={generating}
            error={error}
          />
        </main>

        <aside className="w-80 shrink-0 overflow-y-auto scrollbar-thin">
          <QueuePanel
            collapsed={queueCollapsed}
            onToggle={() => setQueueCollapsed(!queueCollapsed)}
            refreshKey={queueRefreshKey}
          />
        </aside>
      </div>

      <ToastNode toast={toast} />
    </div>
  );
}
