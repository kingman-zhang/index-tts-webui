/** 【临时探针】把真实的 DubbingPage（单人配音页）单独挂到页面上，用于「切换项目确认」的取证。
 *
 *  这是上一轮遗留的「未逐处真机取证」三处之一：切换存档会覆盖画布，原先用原生
 *  `window.confirm`，现改为平台统一样式弹窗。这里加载真页面、走真 Header 下拉。
 *
 *  预置 localStorage：画布有文稿（才会弹确认）+ 一份存档。所有 /api/* 由 mock 兜底。
 *  验证完即删（不进任何提交）。用法见 outputs/verify-switch-project.js。
 */
import { createRoot } from "react-dom/client";
import DubbingPage from "./pages/DubbingPage";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

/** 画布预置非空文稿 —— 覆盖确认只在「已有内容」时才该弹。 */
const CANVAS = Array.from(
  { length: 30 },
  (_, i) => `画布中已有的第 ${i + 1} 段文稿，用于验证切换存档前是否会先弹确认框。`
).join("\n");

const ARCHIVE_TEXT = "这是存档甲的文稿内容，切换成功后画布应当变成它。";

localStorage.setItem("wb-mono-draft-v2", JSON.stringify({
  name: "当前项目",
  voice: { voice_path: null, voice_name: null },
  speed: 1.0,
  text: CANVAS,
}));

localStorage.setItem("wb-mono-projects", JSON.stringify([{
  id: "p_probe_1",
  name: "存档甲",
  voice: { voice_path: null, voice_name: null },
  speed: 1.0,
  text: ARCHIVE_TEXT,
  savedAt: new Date().toISOString(),
}]));

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText = "position:relative;background:#fff";
document.body.appendChild(host);

createRoot(host).render(<DubbingPage />);
