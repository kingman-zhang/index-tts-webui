import { useState, useEffect, useCallback } from "react";
import { api } from "../api/client";
import type { VoiceFile } from "../types";

/**
 * 页面级公共初始化：TTS 在线状态 + 参考音频列表（30s 轮询刷新）。
 * 双人播客页与单人配音页共用。
 */
export function useAppInit() {
  const [voiceFiles, setVoiceFiles] = useState<VoiceFile[]>([]);
  const [ttsOnline, setTtsOnline] = useState<boolean | null>(null);
  const [ttsInfo, setTtsInfo] = useState<{ model_loaded: boolean } | null>(null);

  useEffect(() => {
    const init = async () => {
      try {
        const cfg = await api.getConfig();
        setTtsOnline(cfg.tts_online);
        setTtsInfo(cfg.tts_info);
      } catch { setTtsOnline(false); }
      try {
        const v = await api.listVoices();
        setVoiceFiles(v.voices);
      } catch { /* 忽略 */ }
    };
    init();
    const timer = setInterval(init, 30000);
    return () => clearInterval(timer);
  }, []);

  const reloadVoices = useCallback(async () => {
    try { const v = await api.listVoices(); setVoiceFiles(v.voices); } catch {}
  }, []);

  return { voiceFiles, ttsOnline, ttsInfo, reloadVoices };
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
