import { useState, useEffect, useRef } from "react";
import { Header } from "../components/Header";
import { MonoEditor } from "../components/MonoEditor";
import { MonoVoiceCard, type MonoVoice } from "../components/MonoVoiceCard";
import { GlossaryPanel } from "../components/GlossaryPanel";
import { QueuePanel } from "../components/QueuePanel";
import { ChapterList } from "../components/ChapterList";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { api } from "../api/client";
import { useAppInit, useToast, ToastNode } from "../hooks/useAppInit";
import {
  defaultProjectName, defaultParams, defaultSilence,
  textToMonoLines, monoLinesToText, billableChars, estimatePoints,
  newChapterId, type MonoChapter,
} from "../types";
import {
  loadProjects, saveProject, removeProject, type MonoProjectSnapshot,
} from "@/lib/projectStore";
import { useAuth, refreshUser } from "@/lib/auth";

const MONO_DRAFT_KEY = "wb-mono-draft-v2";

interface MonoDraft {
  name: string;
  voice: MonoVoice;
  speed: number;
  text: string;
  /** 书稿模式：整本书的章节（普通单篇时缺省） */
  book?: MonoChapter[];
  activeChapterId?: string;
}

/** v2 草稿 = { name?, voice, speed, text[, book] }；读到 v1（逐段模型）时迁移为标记文本 */
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
          book: Array.isArray(d.book) && d.book.length ? d.book : undefined,
          activeChapterId: typeof d.activeChapterId === "string" ? d.activeChapterId : undefined,
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

/** 单人配音页（/dubbing）：单音色 + 所见即所得画布 + 队列。
 *
 *  两种工作形态：
 *  - 普通单篇（book = null）：画布文本 = 整篇文稿，与旧行为一致。
 *  - 书稿模式（book = 章节数组）：画布文本**恒等于当前章**；左栏出现章节目录，
 *    可逐章编辑 / 逐章生成 / 勾选批量生成（每章一个队列任务，各扣各的积分）。 */
export default function DubbingPage() {
  const initial = loadMonoDraft();
  const [name, setName] = useState(initial.name);
  const [monoVoice, setMonoVoice] = useState<MonoVoice>(initial.voice);
  const [monoSpeed, setMonoSpeed] = useState<number>(initial.speed);
  const [monoText, setMonoText] = useState<string>(initial.text);

  // ─── 书稿模式 ─────────────────────────────────────────────
  const [book, setBook] = useState<MonoChapter[] | null>(initial.book ?? null);
  const [activeChapterId, setActiveChapterId] = useState<string | null>(
    initial.book?.length ? (initial.activeChapterId ?? initial.book[0].id) : null
  );
  const [chapterSelected, setChapterSelected] = useState<Set<string>>(new Set());
  const [chapterSubmitted, setChapterSubmitted] = useState<Set<string>>(new Set());
  const [bookCollapsed, setBookCollapsed] = useState(false);
  const [submittingBatch, setSubmittingBatch] = useState(false);
  const [submittingChapterId, setSubmittingChapterId] = useState<string | null>(null);

  const { voiceFiles, ttsOnline, ttsInfo, memberEnforce, memberPer1000, memberMinCharge } = useAppInit();
  const { toast, showToast } = useToast();
  const { user } = useAuth();

  // 草稿自动保存（含书稿；刷新不丢）。
  // 书稿是几十万字量级 → 必须防抖：否则每次按键都要序列化 ~1MB JSON，输入会卡。
  useEffect(() => {
    const t = setTimeout(() => {
      const draft: MonoDraft = {
        name, voice: monoVoice, speed: monoSpeed, text: monoText,
        book: book ?? undefined,
        activeChapterId: activeChapterId ?? undefined,
      };
      try {
        localStorage.setItem(MONO_DRAFT_KEY, JSON.stringify(draft));
      } catch { /* 配额不足（书稿过大）时放弃本次草稿保存，不打断编辑 */ }
    }, 600);
    return () => clearTimeout(t);
  }, [name, monoVoice, monoSpeed, monoText, book, activeChapterId]);

  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [glossaryCollapsed, setGlossaryCollapsed] = useState(false);
  const [queueCollapsed, setQueueCollapsed] = useState(false);
  const [queueRefreshKey, setQueueRefreshKey] = useState(0);

  // ─── 项目存档（保存/切换入口在顶部 Header 项目名右侧） ─────
  const [projects, setProjects] = useState<MonoProjectSnapshot[]>(loadProjects);
  const [projectSaved, setProjectSaved] = useState(false);
  const savedFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 切换项目会覆盖画布 → 用平台统一样式弹窗确认（替代原生 confirm）；
  // 确认后执行 run()。不确认则什么都不发生。
  const [pendingSwitch, setPendingSwitch] = useState<{ name: string; run: () => void } | null>(null);

  const handleSaveProject = () => {
    setProjects(saveProject({
      name, voice: monoVoice, speed: monoSpeed, text: monoText,
      book, activeChapterId,
    }));
    setProjectSaved(true);
    if (savedFlashTimer.current) clearTimeout(savedFlashTimer.current);
    savedFlashTimer.current = setTimeout(() => setProjectSaved(false), 1500);
  };

  const handleSwitchProject = (id: string) => {
    const snap = projects.find(p => p.id === id);
    if (!snap) return;
    const doSwitch = () => {
      setName(snap.name);
      setMonoVoice(snap.voice);
      setMonoSpeed(snap.speed);
      setMonoText(snap.text);
      const b = snap.book ?? null;
      setBook(b);
      setActiveChapterId(b?.length ? (snap.activeChapterId ?? b[0].id) : null);
      setChapterSelected(b ? new Set(b.map(c => c.id)) : new Set());
      setChapterSubmitted(new Set());
      showToast(`已切换到「${snap.name}」`);
    };
    // 画布有内容时才需确认（平台统一样式弹窗，替代原生 confirm）
    if (monoText.trim()) {
      setPendingSwitch({ name: snap.name, run: doSwitch });
      return;
    }
    doSwitch();
  };

  const handleDeleteProject = (id: string) => {
    const target = projects.find(p => p.id === id);
    setProjects(removeProject(id));
    if (target) showToast(`已删除存档「${target.name}」`);
  };

  // ─── 书稿：画布文本 ⇄ 当前章 ──────────────────────────────
  const activeIndex = book && activeChapterId ? book.findIndex(c => c.id === activeChapterId) : -1;
  const activeChapter = activeIndex >= 0 ? book![activeIndex] : null;

  /** 画布编辑：写回「当前章」，保证 book 与画布不漂移 */
  const handleTextChange = (next: string) => {
    setMonoText(next);
    if (activeChapterId) {
      setBook(prev => prev
        ? prev.map(c => (c.id === activeChapterId ? { ...c, text: next } : c))
        : prev);
    }
  };

  const switchChapter = (id: string) => {
    const ch = book?.find(c => c.id === id);
    if (!ch) return;
    setActiveChapterId(id);
    setMonoText(ch.text);
  };

  /** 导入确认后收到的整本书稿 → 落成书稿模式，当前章 = 第 1 章 */
  const handleImportChapters = (chs: { title: string; text: string }[]) => {
    const chapters: MonoChapter[] = chs.map((c, i) => ({
      id: newChapterId(),
      title: c.title?.trim() || `第 ${i + 1} 章`,
      text: c.text,
    }));
    setBook(chapters);
    setActiveChapterId(chapters[0].id);
    setMonoText(chapters[0].text);
    setChapterSelected(new Set(chapters.map(c => c.id)));
    setChapterSubmitted(new Set());
    showToast(`已导入 ${chapters.length} 章到书稿：可逐章编辑，或勾选后批量生成`);
  };

  /** 退出书稿模式：画布保留当前章文本，书稿本身可从存档再取 */
  const exitBook = () => {
    setBook(null);
    setActiveChapterId(null);
    setChapterSelected(new Set());
    setChapterSubmitted(new Set());
    showToast("已退出书稿模式（画布保留当前章内容）");
  };

  const toggleChapterSel = (id: string) => {
    setChapterSelected(prev => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  const parsed = textToMonoLines(monoText);
  // 积分预估：与后端 estimate_task_cost 同式（剥离 [pause:N] 后按实际字数
  // 线性计费、向上取整到 1 积分，并有最低收费地板）
  const totalChars = billableChars(parsed.map(l => l.text));
  const pointsCost = memberEnforce
    ? estimatePoints(totalChars, memberPer1000, memberMinCharge)
    : 0;
  const balance = user?.points ?? null;
  const pointsInsufficient = pointsCost > 0 && balance != null && pointsCost > balance;
  const pointsInfo = pointsCost > 0 ? { cost: pointsCost, balance } : null;
  const canGenerate =
    !!monoVoice.voice_path &&
    parsed.length > 0 &&
    parsed.every(l => l.text.trim().length > 0) &&
    !pointsInsufficient;

  // ─── 提交（kind=mono，后端走引擎适配层） ───────────────────
  /** 把一段文本提交为一个队列任务（画布当前章 / 单章 / 批量，三处共用）。
   *  project_name 用章标题，便于在队列里对上「这是第几章」。 */
  const submitText = (text: string, projectName: string) => {
    if (!monoVoice.voice_path) throw new Error("请先在左侧选择配音音色");
    const lines = textToMonoLines(text).map(l => ({
      speaker: "A" as const,
      text: l.text,
      emotion: l.emotion_label ? { label: l.emotion_label } : null,
    }));
    if (!lines.length) throw new Error("这一章没有可合成的内容");
    const params = { ...defaultParams(), speed: monoSpeed, speaker_speeds: { A: monoSpeed } };
    return api.submitToQueue({
      project_name: projectName,
      kind: "mono",
      lines,
      voices: { A: monoVoice.voice_path },
      silence: defaultSilence(),
      params,
      glossary_enabled: true,
    });
  };

  const handleGenerate = async () => {
    setError(null);
    if (!monoVoice.voice_path) {
      const message = "请先在左侧选择配音音色";
      setError(message); showToast(message);
      return;
    }
    setGenerating(true);
    try {
      const result = await submitText(monoText, name);
      if (activeChapterId) setChapterSubmitted(prev => new Set(prev).add(activeChapterId));
      showToast(`已加入队列（位置 ${result.queue_position}）`);
      setQueueRefreshKey(k => k + 1);
      setQueueCollapsed(false);
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

  /** 单章生成（章节目录里每行的闪电按钮） */
  const generateChapter = async (id: string) => {
    const ch = book?.find(c => c.id === id);
    if (!ch) return;
    setError(null);
    setSubmittingChapterId(id);
    try {
      const result = await submitText(ch.text, ch.title);
      setChapterSubmitted(prev => new Set(prev).add(id));
      showToast(`「${ch.title}」已加入队列（位置 ${result.queue_position}）`);
      setQueueRefreshKey(k => k + 1);
      setQueueCollapsed(false);
      refreshUser();
    } catch (e: any) {
      setError(e.message);
      showToast(`「${ch.title}」提交失败: ${e.message}`);
    } finally {
      setSubmittingChapterId(null);
    }
  };

  /** 批量生成：选中的章逐个提交（每章一个任务、各扣各的）。
   *  首个失败即停 —— 多半是余额不足，继续只会连带失败。 */
  const generateSelectedChapters = async () => {
    if (!book) return;
    const list = book.filter(c => chapterSelected.has(c.id));
    if (!list.length) return;
    setError(null);
    setSubmittingBatch(true);
    let ok = 0;
    let failTitle = "";
    let failMsg = "";
    for (const ch of list) {
      try {
        await submitText(ch.text, ch.title);
        setChapterSubmitted(prev => new Set(prev).add(ch.id));
        ok += 1;
      } catch (e: any) {
        failTitle = ch.title;
        failMsg = e.message;
        break;
      }
    }
    setSubmittingBatch(false);
    setQueueRefreshKey(k => k + 1);
    setQueueCollapsed(false);
    refreshUser();
    if (failTitle) {
      setError(failMsg);
      showToast(ok > 0
        ? `已提交 ${ok} 章；「${failTitle}」失败：${failMsg}`
        : `提交失败：${failMsg}`);
    } else {
      showToast(`已提交 ${ok} 章到队列`);
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
        projects={projects.map(p => ({
          id: p.id, name: p.name, savedAt: p.savedAt,
          meta: p.book?.length
            ? `${p.book.length} 章 · ${p.book.reduce((n, c) => n + c.text.replace(/\s/g, "").length, 0)} 字`
            : `${p.text.replace(/\s/g, "").length} 字`,
        }))}
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

          {book && book.length > 0 && (
            <ChapterList
              chapters={book}
              activeId={activeChapterId}
              selected={chapterSelected}
              submitted={chapterSubmitted}
              collapsed={bookCollapsed}
              onToggleCollapse={() => setBookCollapsed(v => !v)}
              onSelect={switchChapter}
              onToggle={toggleChapterSel}
              onToggleAll={on => setChapterSelected(on ? new Set(book.map(c => c.id)) : new Set())}
              onGenerate={generateChapter}
              onGenerateSelected={generateSelectedChapters}
              submitting={submittingBatch}
              submittingId={submittingChapterId}
              canGenerate={!!monoVoice.voice_path}
              pointsConfig={memberEnforce ? { per1000: memberPer1000, minCharge: memberMinCharge } : null}
              balance={balance}
              onExit={exitBook}
            />
          )}

          <GlossaryPanel
            collapsed={glossaryCollapsed}
            onToggle={() => setGlossaryCollapsed(!glossaryCollapsed)}
          />
        </aside>

        <main className="flex-1 min-w-0">
          <MonoEditor
            text={monoText}
            onChange={handleTextChange}
            onGenerate={handleGenerate}
            canGenerate={canGenerate}
            generating={generating}
            error={error}
            pointsInfo={pointsInfo}
            pointsEnv={memberEnforce ? { per1000: memberPer1000, minCharge: memberMinCharge, balance } : null}
            onImportChapters={handleImportChapters}
            title={activeChapter ? `${activeIndex + 1}. ${activeChapter.title}` : undefined}
          />
        </main>

        <aside className="w-80 shrink-0 overflow-y-auto scrollbar-thin">
          <QueuePanel
            collapsed={queueCollapsed}
            onToggle={() => setQueueCollapsed(!queueCollapsed)}
            refreshKey={queueRefreshKey}
            defaultKind="mono"
          />
        </aside>
      </div>

      {/* 切换项目前的覆盖确认（平台统一样式，替代原生 confirm） */}
      <ConfirmDialog
        open={!!pendingSwitch}
        tone="default"
        title="切换项目？"
        description={
          <>
            将加载存档「
            <span className="font-medium text-gray-800">{pendingSwitch?.name}</span>
            」，替换画布中现有的 {totalChars} 字文稿。
          </>
        }
        confirmText="继续切换"
        onCancel={() => setPendingSwitch(null)}
        onConfirm={() => {
          const run = pendingSwitch?.run;
          setPendingSwitch(null);
          run?.();
        }}
      />

      <ToastNode toast={toast} />
    </div>
  );
}
