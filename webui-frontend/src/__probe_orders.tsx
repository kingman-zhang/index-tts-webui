/** 【临时探针】本批 4 条改动的交互取证（挂**真组件**，fetch 由 verify-order-flow.js 注入的 mock 拦截）。
 *
 *  为什么不用静态复刻：要证的全是交互与状态机 ——
 *    ① 同面额被占时点「购买」→ 应弹**只有确认按钮**的告知弹窗（而不是toast）
 *    ② 弹窗正文应等于**后端下发的原句**（文案单一真源在后端，前端不复制）
 *    ③ ★弹窗里那句「去最近订单付款」的指引，随**占位的是谁**给不给：
 *         别人的单占着（same_amount_busy_other）→ **不给**（用户根本没下过单）
 *         自己那笔占着（same_amount_busy_self）→ 才给
 *    ④ 被拦时不应产生新订单（列表条数不变）
 *    ⑤ 「最近订单」右侧的「查看更多」点击后路由切到 /account/orders
 *    ⑥ 全部订单页渲染完整字段（订单号 / 交易号 / 通道 / 时间）并能「加载更多」
 *  照抄 HTML 只能证明长相，证明不了这些。
 *
 *  ✅ 入库：本文件与 outputs/verify-order-flow.js 成对存在（脚本靠 `import()` 反向引用它）。
 */
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ShopCard } from "./pages/AccountPage";
import OrdersPage from "./pages/OrdersPage";
import { toast, useToastState } from "./lib/toast";
import { ToastNode } from "./hooks/useAppInit";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

const BTN: React.CSSProperties = {
  padding: "6px 12px", border: "1px solid #cbd5e1", borderRadius: 8,
  background: "#fff", fontSize: 13, cursor: "pointer",
};

function Probe() {
  const [view, setView] = useState<"shop" | "orders">("shop");
  const toastState = useToastState();
  // 供 puppeteer 确认当前挂的是哪个视图
  (window as any).__probeView = view;
  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <button style={BTN} onClick={() => setView("shop")}>看商城</button>
        <button style={BTN} onClick={() => setView("orders")}>看订单页</button>
      </div>
      {view === "shop"
        ? <ShopCard showToast={(m: string) => toast.info(m)} />
        : <OrdersPage />}
      <ToastNode toast={toastState} />
    </div>
  );
}

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText = "width:1000px;padding:12px;background:#f8fafc;font-family:system-ui,sans-serif";
document.body.appendChild(host);

createRoot(host).render(<Probe />);
