/** 【临时探针】把真实的 DubbingPage（单人配音页）挂到页面上，用于「书稿模式」的交互取证。
 *
 *  为什么用真页面：书稿状态（章节数组 / 当前章 / 勾选）都在 DubbingPage 里，
 *  独立挂 MonoEditor 取不到。这里加载真页面、走真导入弹窗、真章节点击，
 *  只有 /api/mono/extract（喂分章结果）与 /api/queue/submit（记账）由 fetch mock 接管。
 *
 *  预置：
 *   - 空画布（导入时不必先弹「替换确认」）；
 *   - 已选音色（否则章节目录的生成按钮会被禁用，批量提交证不了）。
 *
 *  验证完即删（不进任何提交）。用法见 outputs/verify-book-flow.js。
 */
import { createRoot } from "react-dom/client";
import DubbingPage from "./pages/DubbingPage";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

localStorage.setItem("wb-mono-draft-v2", JSON.stringify({
  name: "探针书稿",
  voice: { voice_path: "/probe/voice.wav", voice_name: "探针音色" },
  speed: 1.0,
  text: "",
}));
localStorage.removeItem("wb-mono-projects");

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText = "position:relative;background:#fff";
document.body.appendChild(host);

createRoot(host).render(<DubbingPage />);
