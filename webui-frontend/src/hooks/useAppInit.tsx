import { useState, useEffect, useCallback } from "react";
import { api } from "../api/client";
import { showToast as emitToast, useToastState, type ToastState } from "../lib/toast";
import type { VoiceFile } from "../types";

/**
 * 页面级公共初始化：参考音频列表（30s 轮询）+ TTS 状态（10s 轮询）。
 * 双人播客页与单人配音页共用。
 *
 * TTS 探测开关在后端 .env（TTS_STATUS_POLL）：关闭时 /api/config 不探测、
 * tts_online 返回 null（前端静默不显示）；开启时探测，tts_online=true 才显示状态。
 */
export function useAppInit() {
  const [voiceFiles, setVoiceFiles] = useState<VoiceFile[]>([]);
  const [ttsOnline, setTtsOnline] = useState<boolean | null>(null);
  const [ttsInfo, setTtsInfo] = useState<{ model_loaded: boolean } | null>(null);
  const [memberEnforce, setMemberEnforce] = useState(false);
  const [memberPer1000, setMemberPer1000] = useState(0);
  const [memberMinCharge, setMemberMinCharge] = useState(0);

  // 音色列表：初始 + 30s 轮询（不涉及 TTS 探测）
  useEffect(() => {
    const loadVoices = async () => {
      try {
        const v = await api.listVoices();
        setVoiceFiles(v.voices);
      } catch { /* 忽略 */ }
    };
    loadVoices();
    const timer = setInterval(loadVoices, 30000);
    return () => clearInterval(timer);
  }, []);

  // TTS 状态 + 积分定价：10s 轮询（探测与否由后端开关决定；null = 开关关闭/未检测）
  useEffect(() => {
    const probe = async () => {
      try {
        const cfg = await api.getConfig();
        setTtsOnline(cfg.tts_online ?? null);
        setTtsInfo(cfg.tts_info ?? null);
        setMemberEnforce(!!cfg.member_enforce);
        setMemberPer1000(cfg.member_points_per_1000_chars ?? 0);
        setMemberMinCharge(cfg.member_min_charge ?? 0);
      } catch { setTtsOnline(null); }
    };
    probe();
    const timer = setInterval(probe, 10000);
    return () => clearInterval(timer);
  }, []);

  const reloadVoices = useCallback(async () => {
    try { const v = await api.listVoices(); setVoiceFiles(v.voices); } catch {}
  }, []);

  return { voiceFiles, ttsOnline, ttsInfo, memberEnforce, memberPer1000, memberMinCharge, reloadVoices };
}

/**
 * 轻量 toast：2.5s 自动消失。
 *
 * 状态已提到 `lib/toast.ts` 的**模块级单例**，所以深处组件也能 `import { toast }`
 * 直接弹（不必层层下传 showToast）。这里保持原有签名 `{ toast, showToast }` 不变，
 * 页面侧调用零改动。传字符串一律按 info 处理。
 */
export function useToast() {
  const toast = useToastState();
  const showToast = useCallback((msg: string) => emitToast(msg), []);
  return { toast, showToast };
}

/** Toast 展示层（配合 useToast 使用）；按语义上色：info 灰 / success 绿 / error 红 */
export function ToastNode({ toast }: { toast: ToastState | null }) {
  if (!toast) return null;
  const tone =
    toast.kind === "error"
      ? "bg-red-600"
      : toast.kind === "success"
        ? "bg-emerald-600"
        : "bg-gray-800";
  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50">
      <div key={toast.id} className={`${tone} text-white text-sm px-4 py-2 rounded-lg shadow-lg`}>
        {toast.message}
      </div>
    </div>
  );
}
