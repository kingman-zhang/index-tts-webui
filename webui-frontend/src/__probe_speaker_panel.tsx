/** 【临时探针】把真实的 SpeakerPanel 单独挂到页面上，用于「组件层弹窗统一」的接线取证。
 *
 *  要证的是**接线**：原来角色卡里用原生 `window.prompt` 改名、用 `alert` 报错、
 *  用自绘弹窗做删除确认 —— 现在三处是否都落到了平台统一件上（ConfirmDialog /
 *  PromptDialog / 全局 toast），且删除确认「取消不删、确认才删」。
 *
 *  只有 /api/voice-presets、/api/preset-voices 由 fetch mock 喂数据。
 *  验证完即删（不进任何提交）。用法见 outputs/verify-speaker-dialogs.js。
 */
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { SpeakerPanel } from "./components/SpeakerPanel";
import { defaultProject } from "./types";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

function Probe() {
  const [speakers, setSpeakers] = useState(() => defaultProject().voices);
  return (
    <SpeakerPanel
      speakers={speakers}
      onChange={(key, patch) => setSpeakers(s => ({ ...s, [key]: { ...s[key], ...patch } }))}
      voiceFiles={[]}
      onUpload={async () => null}
      onRenameVoice={async (v, n) => { (window as any).__voiceRenamed = { name: v.name, next: n }; }}
      onDeleteVoice={async (v) => { (window as any).__voiceDeleted = v.name; }}
    />
  );
}

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText = "width:420px;padding:12px;background:#f8fafc;font-family:system-ui,sans-serif";
document.body.appendChild(host);

createRoot(host).render(<Probe />);
