import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatTime(seconds: number): string {
  if (!seconds || seconds <= 0) return "0:00"
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, "0")}`
}

/**
 * 下载音频时的「另存为」文件名：用项目名，而不是服务端落盘的 mono_<task_id>.wav。
 *
 * 纯前端行为 —— `<a download>` 只影响浏览器保存到本地的名字，**不碰服务器上的
 * 任何文件**（后端仍是 mono_/podcast_<task_id>.wav，播放与续期逻辑都不受影响）。
 * 同源 URL 才会生效，这里走的是自家 nginx 反代的 /api/，满足条件。
 *
 * 扩展名取自 URL；后端产物固定 .wav（`/api/mono/audio/{id}` 不带后缀），故缺省 wav。
 * 项目名里的 / \ : * ? " < > | 与控制字符会被浏览器截断或拒绝，统一换成下划线。
 */
export function audioDownloadName(projectName: string | undefined, url: string): string {
  const ext = (url.split("?")[0].match(/\.(wav|mp3|m4a|ogg|flac)$/i)?.[1] || "wav").toLowerCase()
  // eslint-disable-next-line no-control-regex
  const base = (projectName || "").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim()
  return `${base || "audio"}.${ext}`
}
