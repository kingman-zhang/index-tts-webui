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

/** 下载提示与后端命名保持一致；实际保存名由 Content-Disposition 决定。 */
export function audioDownloadName(projectName: string | undefined, url: string): string {
  const legacy = url.split("?")[0].match(/\/(mono|podcast)\/audio\/([^/]+)$/)
  const fallback = legacy ? `${legacy[1]}_${legacy[2]}` : "audio"
  // eslint-disable-next-line no-control-regex
  let base = (projectName || fallback).replace(/[\\/:*?"<>|\u0000-\u001f\u007f-\u009f]/g, "_").trim().replace(/[. ]+$/, "")
  base = base.replace(/(?:\.wav)+$/i, "").replace(/[. ]+$/, "") || "audio"
  if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(base)) base = `_${base}`
  const encoder = new TextEncoder()
  let result = ""
  for (const char of base) {
    if (encoder.encode(result + char).length > 240) break
    result += char
  }
  return `${result.replace(/[. ]+$/, "")}.wav`
}
