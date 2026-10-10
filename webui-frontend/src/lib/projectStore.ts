/**
 * 单人配音「项目存档」localStorage 存取（key: wb-mono-projects）。
 * 快照 = 项目名 + 音色 + 语速 + 文稿；同名覆盖，最多保留 20 份。
 * 保存 / 切换入口都在顶部 Header（项目名右侧），不再走左栏卡片。
 *
 * 书稿模式（导入整本书后）：快照额外带 `book`（全书章节）+ `activeChapterId`。
 * 书稿是「几十万字」量级的数据，localStorage 配额（多数浏览器 5MB）会被撑爆，
 * 所以写盘统一走 `persist()`：超限时从**最旧**的存档开始丢，直到能写进去。
 */
import type { MonoChapter } from "@/types";
import type { MonoVoice } from "@/components/MonoVoiceCard";

export interface MonoProjectSnapshot {
  id: string;
  name: string;
  voice: MonoVoice;
  speed: number;
  /** 当前画布文本。书稿模式下 = 当前章的正文（冗余存一份，便于列表预览与非书稿读取） */
  text: string;
  /** 书稿模式：整本书的章节；缺省 = 普通单篇 */
  book?: MonoChapter[];
  /** 书稿模式：上次编辑到哪一章 */
  activeChapterId?: string;
  savedAt: string; // ISO
}

const LIST_KEY = "wb-mono-projects";
const MAX_PROJECTS = 20;

export function loadProjects(): MonoProjectSnapshot[] {
  try {
    const raw = localStorage.getItem(LIST_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

/**
 * 写盘：超配额时从最旧的开始丢弃重试。
 *
 * 为什么必须做：一份 50 万字的书稿 JSON 约 1MB，20 份就是 20MB —— 远超 localStorage
 * 的 5MB 上限。若不处理，`setItem` 会抛 QuotaExceededError，**整批存档连最新的都写不进去**
 * （用户看到的现象是「点了保存但刷新后没了」）。宁可丢最旧的，也不能丢刚存的。
 *
 * @returns 实际写进 localStorage 的列表（可能比传入的短）
 */
function persist(list: MonoProjectSnapshot[]): MonoProjectSnapshot[] {
  let arr = list;
  while (arr.length > 0) {
    try {
      localStorage.setItem(LIST_KEY, JSON.stringify(arr));
      return arr;
    } catch {
      // 容量不足：先丢最旧（数组尾部）的一条再试；只剩一条仍失败就放弃
      if (arr.length === 1) {
        try { localStorage.removeItem(LIST_KEY); } catch { /* 忽略 */ }
        return [];
      }
      arr = arr.slice(0, arr.length - 1);
    }
  }
  return [];
}

/** 保存当前项目（同名覆盖置顶），返回实际落盘的列表 */
export function saveProject(cur: {
  name: string;
  voice: MonoVoice;
  speed: number;
  text: string;
  book?: MonoChapter[] | null;
  activeChapterId?: string | null;
}): MonoProjectSnapshot[] {
  const list = loadProjects().filter(p => p.name !== cur.name);
  const snap: MonoProjectSnapshot = {
    id: `p_${Date.now()}`,
    name: cur.name || "未命名项目",
    voice: cur.voice,
    speed: cur.speed,
    text: cur.text,
    savedAt: new Date().toISOString(),
  };
  if (cur.book && cur.book.length) {
    snap.book = cur.book;
    snap.activeChapterId = cur.activeChapterId ?? cur.book[0]?.id;
  }
  list.unshift(snap);
  return persist(list.slice(0, MAX_PROJECTS));
}

/** 删除指定项目，返回更新后的列表 */
export function removeProject(id: string): MonoProjectSnapshot[] {
  const list = loadProjects().filter(p => p.id !== id);
  return persist(list);
}
