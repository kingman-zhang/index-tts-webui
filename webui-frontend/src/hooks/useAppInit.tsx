import { useState, useEffect, useCallback } from "react";
import { api } from "../api/client";
import type { VoiceFile } from "../types";

/**
 * 页面级公共初始化：参考音频列表（30s 轮询）+ TTS 状态（热开关按需探测）。
 * 双人播客页与单人配音页共用。
 *
 * TTS 探测默认关闭（轮询 /api/config 会去探 tts-server，白跑浪费）；
 * ttsWatch=true 时立即探测一次并保持 30s 轮询，关掉即停并清空状态。
 */
export function useAppInit() {
  const [voiceFiles, setVoiceFiles] = useState<VoiceFile[]>([]);
  const [ttsOnline, setTtsOnline] = useState<boolean | null>(null);
  const [ttsInfo, setTtsInfo] = useState<{ model_loaded: boolean } | null>(null);
  const [ttsWatch, setTtsWatch] = useState(false);

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

  // TTS 状态：仅开关开启时探测
  useEffect(() => {
    if (!ttsWatch) {
      setTtsOnline(null);
      setTtsInfo(null);
      return;
    }
    const probe = async () => {
      try {
        const cfg = await api.getConfig();
        setTtsOnline(cfg.tts_online);
        setTtsInfo(cfg.tts_info);
      } catch { setTtsOnline(false); }
    };
    probe();
    const timer = setInterval(probe, 30000);
    return () => clearInterval(timer);
  }, [ttsWatch]);

  const reloadVoices = useCallback(async () => {
    try { const v = await api.listVoices(); setVoiceFiles(v.voices); } catch {}
  }, []);

  return { voiceFiles, ttsOnline, ttsInfo, ttsWatch, setTtsWatch, reloadVoices };
}

/** 轻量 toast：2.5s 自动消失 */
export function useToast() {
  const [toast, setToast] = useState<string | null>(null);
  const showToast = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2500);
  }, []);
  return { toast, showToast };
}

/** Toast 展示层（配合 useToast 使用） */
export function ToastNode({ toast }: { toast: string | null }) {
  if (!toast) return null;
  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50">
      <div className="bg-gray-800 text-white text-sm px-4 py-2 rounded-lg shadow-lg">
        {toast}
      </div>
    </div>
  );
}
