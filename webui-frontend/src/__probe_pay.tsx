/** 【临时探针】积分商城「真实支付出码」链路的交互取证。
 *
 *  为什么不用静态复刻：本次要证的全是**交互**（点购买 → 出码弹窗 → 轮询到已支付
 *  → 自动关闭 + 刷新），照抄 HTML 只能证明长相、证明不了这条状态机。
 *  这里挂**真组件**（AccountPage 的 ShopCard、PayDialog），fetch 由
 *  outputs/verify-pay-dialog.js 注入的 mock 拦截 —— 全程无真实后端。
 *
 *  ✅ 入库：本文件与 verify-pay-dialog.js 成对存在（脚本靠 `import()` 反向引用它）。
 */
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { QRCodeSVG } from "qrcode.react";
import { ShopCard } from "./pages/AccountPage";
import { PayDialog } from "./components/PayDialog";
import { toast, useToastState } from "./lib/toast";
import { ToastNode } from "./hooks/useAppInit";
import type { PayCharge, PointsOrder } from "./api/members";

/** 与 verify 脚本里 mock 的下单响应**故意各写一份**：两边对不上时测试就会红。 */
const QR_VALUE = "https://mpay.lizigege.com/pay/T20260001";

const ORDER: PointsOrder = {
  order_id: "ord_probe00001", pack_id: "p1", pack_name: "体验包", points: 1000,
  amount_fen: 1000, status: "pending", provider: "mpay",
  out_trade_no: "ord_probe00001", created_at: "2026-10-14T10:00:00", paid_at: null,
};

const mkCharge = (pay_type: PayCharge["pay_type"], pay_info: string): PayCharge =>
  ({ pay_type, pay_info, trade_no: "T20260001", reused: false });

const VARIANTS = {
  qrcode: mkCharge("qrcode", QR_VALUE),
  jump: mkCharge("jump", "https://mpay.lizigege.com/cashier/T20260001"),
  urlscheme: mkCharge("urlscheme", "weixin://dl/business/?t=probe"),
  unknown: mkCharge("", "raw-payload-from-channel"),
} as const;

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

const BTN: React.CSSProperties = {
  padding: "6px 12px", border: "1px solid #cbd5e1", borderRadius: 8,
  background: "#fff", fontSize: 13, cursor: "pointer",
};

function Probe() {
  const [variant, setVariant] = useState<keyof typeof VARIANTS | null>(null);
  const [packsKey, setPacksKey] = useState(0);
  const [log, setLog] = useState<string[]>([]);
  const toastState = useToastState();

  // 供 puppeteer 读取「点击后到底发生了什么」
  (window as any).__probeLog = log;

  /** 切换商城看到的支付通道状态（channel_ready / mock / 双开）。 */
  (window as any).__setProbePacks = (p: Record<string, unknown>) => {
    (window as any).__packsOverride = p;
    setPacksKey(k => k + 1);       // 换 key = 重挂 ShopCard = 重新拉 packs
  };

  return (
    <div style={{ width: 980 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <button style={BTN} onClick={() => setVariant("qrcode")}>PayDialog:qrcode</button>
        <button style={BTN} onClick={() => setVariant("jump")}>PayDialog:jump</button>
        <button style={BTN} onClick={() => setVariant("urlscheme")}>PayDialog:urlscheme</button>
        <button style={BTN} onClick={() => setVariant("unknown")}>PayDialog:unknown</button>
      </div>

      <ShopCard key={packsKey} showToast={(m) => {
        setLog(l => [...l, m]);
        toast.success(m);
      }} />

      {/* 支付码形态弹窗（跑真轮询） */}
      {variant && (
        <PayDialog
          key={variant}
          order={ORDER}
          charge={VARIANTS[variant]}
          showToast={(m) => { setLog(l => [...l, `toast:${m}`]); toast.info(m); }}
          onPaid={() => setLog(l => [...l, "onPaid"])}
          onClose={() => setVariant(null)}
        />
      )}

      {/* 参照二维码：内容与上面 qrcode 形态一致。两张 SVG 必须逐字节相同，
          这才证明弹窗里的码**编的就是**通道给的 pay_info，而不是别的东西。 */}
      <div id="probe-ref-qr" style={{ position: "absolute", left: -9999, top: -9999 }}>
        <QRCodeSVG value={QR_VALUE} size={168} level="M" marginSize={0} />
      </div>

      <ToastNode toast={toastState} />
    </div>
  );
}

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText = "width:1000px;padding:12px;background:#f8fafc;font-family:system-ui,sans-serif";
document.body.appendChild(host);

createRoot(host).render(<Probe />);
