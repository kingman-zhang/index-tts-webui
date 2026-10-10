/**
 * 全局轻量 toast。
 *
 * 为什么不是页面级 hook：原来 `useToast()` 的状态挂在**页面组件**里，只有页面自己
 * 能弹；深处的组件（音色卡、角色卡、队列卡、音色选择器）拿不到，于是那些位置只能
 * 退化成原生 `alert` —— 样式与平台不一致，还会阻塞主线程。这里把状态提到模块级
 * 单例，任何组件 `import { toast }` 就能弹，页面侧的 `useToast()` 签名保持不变。
 *
 * 三种语义按颜色区分：info 灰 / success 绿 / error 红。错误默认停留更久（要读得完）。
 */
import { useSyncExternalStore } from "react";

export type ToastKind = "info" | "success" | "error";

export interface ToastState {
  /** 每次调用自增：既用于触发重渲染，也可做 key 重置进入动画 */
  id: number;
  message: string;
  kind: ToastKind;
}

let current: ToastState | null = null;
let seq = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

/** 命令式弹一条 toast（后一条覆盖前一条）。组件层可直接调用 `toast.error(...)`。 */
export function showToast(message: string, kind: ToastKind = "info", duration = 2500) {
  if (timer) clearTimeout(timer);
  seq += 1;
  current = { id: seq, message, kind };
  timer = setTimeout(() => {
    timer = null;
    current = null;
    emit();
  }, duration);
  emit();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function getSnapshot() {
  return current;
}

/** 订阅当前 toast 状态（`useToast` 与 `ToastNode` 用）。 */
export function useToastState(): ToastState | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** 命令式入口：`toast.error("上传失败: ...")` / `toast.success(...)` / `toast.info(...)`。 */
export const toast = {
  info: (message: string, duration = 2500) => showToast(message, "info", duration),
  success: (message: string, duration = 2500) => showToast(message, "success", duration),
  /** 错误默认多停 1s：比成功提示更需要读清楚 */
  error: (message: string, duration = 3500) => showToast(message, "error", duration),
};
