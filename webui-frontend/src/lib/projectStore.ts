/**
 * 单人配音「项目存档」localStorage 存取（key: wb-mono-projects）。
 * 快照 = 项目名 + 音色 + 语速 + 文稿；同名覆盖，最多保留 20 份。
 * 保存 / 切换入口都在顶部 Header（项目名右侧），不再走左栏卡片。
 */
import type { MonoVoice } from "@/components/MonoVoiceCard";

export interface MonoProjectSnapshot {
  id: string;
  name: string;
  voice: MonoVoice;
  speed: number;
  text: string;
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

/** 保存当前项目（同名覆盖置顶），返回更新后的列表 */
export function saveProject(cur: {
  name: string;
  voice: MonoVoice;
  speed: number;
  text: string;
}): MonoProjectSnapshot[] {
  const list = loadProjects().filter(p => p.name !== cur.name);
  list.unshift({
    id: `p_${Date.now()}`,
    name: cur.name || "未命名项目",
    voice: cur.voice,
    speed: cur.speed,
    text: cur.text,
    savedAt: new Date().toISOString(),
  });
  const trimmed = list.slice(0, MAX_PROJECTS);
  localStorage.setItem(LIST_KEY, JSON.stringify(trimmed));
  return trimmed;
}

/** 删除指定项目，返回更新后的列表 */
export function removeProject(id: string): MonoProjectSnapshot[] {
  const list = loadProjects().filter(p => p.id !== id);
  localStorage.setItem(LIST_KEY, JSON.stringify(list));
  return list;
}
