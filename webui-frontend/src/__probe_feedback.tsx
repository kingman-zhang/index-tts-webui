/** 【临时探针】「使用文档开新标签页」+「举报与反馈弹窗」的交互取证。
 *
 *  为什么要挂真组件：这两件事都是**行为**，不是长相 ——
 *    · 文档入口：点击后当前页**不能**跳走（曾经因为 `window.open(..., "noopener")`
 *      恒返回 null 而误触发「退回当前页跳转」的兜底）；
 *    · 反馈弹窗：提交要打真 API、拿回执、失败要显示后端给的原因。
 *  照抄 HTML 一条都证不到，所以这里挂 `UserMenu` 与 `PointsCard` 两份真组件，
 *  fetch 由 outputs/verify-feedback.js 注入的 mock 拦截（全程无真实后端）。
 *
 *  ✅ 入库：本文件与 verify-feedback.js 成对存在（脚本靠 `import()` 反向引用它）。
 */
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { UserMenu } from "./components/UserMenu";
import { PointsCard } from "./pages/AccountPage";

/** 给 puppeteer 读的点击日志（toast / 兑换提示之类都落这里）。 */
function Probe() {
  const [log, setLog] = useState<string[]>([]);
  (window as any).__probeLog = log;

  return (
    <div style={{ width: 720, padding: 16 }}>
      {/* 顶栏用户菜单：举报与反馈的入口就在这里 */}
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14 }}>
        <UserMenu />
      </div>
      {/* 个人中心的积分卡：文档入口在卡片底部（本次要证的就是它不跳走） */}
      <PointsCard showToast={(m) => setLog(l => [...l, m])} />
    </div>
  );
}

// 隐藏真实 app（落地页），避免干扰截图与选择器
const app = document.getElementById("root");
if (app) app.style.display = "none";

const host = document.createElement("div");
host.id = "probe-root";
document.body.appendChild(host);
createRoot(host).render(<Probe />);
