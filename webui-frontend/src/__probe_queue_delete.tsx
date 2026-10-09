/** 【临时探针】把真实的 QueuePanel 单独挂到页面上，用于「删除确认弹窗」的交互取证。
 *
 *  为什么不用静态复刻：本次要证的是**交互**（点删除 → 弹窗、取消/Esc/遮罩 → 关闭、
 *  确认 → 真的发出 DELETE），照抄 HTML 只能证明视觉、证明不了行为。
 *  这里加载的是真组件、跑的是真状态机，只有 /api/queue 由 fetch mock 喂数据。
 *
 *  验证完即删（不进任何提交）。用法见 outputs/verify-queue-delete-confirm.js。
 */
import { createRoot } from "react-dom/client";
import { QueuePanel } from "./components/QueuePanel";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText = "width:320px;padding:12px;background:#f8fafc;font-family:system-ui,sans-serif";
document.body.appendChild(host);

createRoot(host).render(
  <QueuePanel collapsed={false} onToggle={() => {}} refreshKey={0} defaultKind="podcast" />
);
