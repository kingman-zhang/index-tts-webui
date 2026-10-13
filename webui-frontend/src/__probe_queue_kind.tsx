/** 【临时探针】把真实的 QueuePanel 同时按「播客页 / 配音页」两种页面模式各挂一个，
 *  用于「任务队列跟随页面模式、不再有类型切换 tab」的取证。
 *
 *  为什么不用静态复刻：本次要证的是**行为**——队列只渲染本页类型的任务、
 *  且面板里不再出现「双人播客 / 单人配音」切换按钮。这里加载真组件、跑真状态机，
 *  只有 /api/queue 由 fetch mock 喂一个**混合 kind** 的任务列表。
 *
 *  与 verify 脚本配套：outputs/verify-queue-kind-follow-page.js（按惯例一起入库）。
 */
import { createRoot } from "react-dom/client";
import { QueuePanel } from "./components/QueuePanel";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

document.body.style.cssText = "display:flex;gap:16px;align-items:flex-start;background:#f8fafc;padding:12px";

const mkHost = (id: string) => {
  const host = document.createElement("div");
  host.id = id;
  host.style.cssText = "width:340px;font-family:system-ui,sans-serif";
  document.body.appendChild(host);
  return host;
};

// 左：播客页（/podcast）；右：配音页（/dubbing）
createRoot(mkHost("probe-podcast")).render(
  <QueuePanel collapsed={false} onToggle={() => {}} refreshKey={0} defaultKind="podcast" />
);
createRoot(mkHost("probe-mono")).render(
  <QueuePanel collapsed={false} onToggle={() => {}} refreshKey={0} defaultKind="mono" />
);
