/** 支付出码弹窗（真实支付通道）。
 *
 *  链路：后端出码（`POST /api/points/orders/{id}/pay`）→ 这里按 `pay_type` 呈现
 *  → 用户付款 → **支付平台回调后端**入账 → 前端**轮询**发现已支付。
 *
 *  为什么必须轮询：回调是「支付平台 → 我们的后端」的服务端呼叫，浏览器既看不到
 *  也收不到。若不轮询，用户付完钱得手动刷新页面才看到积分。轮询只读订单状态，
 *  **不具备入账能力**，所以它造不出假账（假账只可能来自回调验签被绕过）。
 *
 *  四种 `pay_type`：
 *    - `qrcode`    → 把 pay_info 渲染成二维码（桌面主路径，微信扫一扫）
 *    - `image`     → pay_info 是收款码**图片**地址，按 <img> 显示（同一条扫码路径，
 *                    只是通道只传了图片没传内容；**不能**当二维码内容编码）
 *    - `jump`      → 跳转链接（收银台，仅当上面两种都拿不到时的降级）
 *    - `urlscheme` → 唤起小程序（移动端）
 *  四者只会返回其一；`""` 属异常态（后端在无可支付内容时直接报错，正常不会出现），
 *  这里仍兜底显示原文，避免用户面对一个空白弹窗。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { CheckCircle2, Copy, ExternalLink, Loader2, RefreshCw, Smartphone, X } from "lucide-react";
import { Button } from "./ui";
import { memberApi, type PayCharge, type PointsOrder } from "@/api/members";
import { cn } from "@/lib/utils";

/** 轮询间隔：前 2 分钟 2s（回调通常几秒内到达，做到「付款即见」），之后 5s 降频。 */
const POLL_FAST_MS = 2000;
const POLL_SLOW_MS = 5000;
const POLL_SWITCH_MS = 2 * 60 * 1000;
/** 「等待较久」提示阈值：**按有效期成比例**（有效期过半时提示）。
 *  ⚠️ 曾经硬编码 3 分钟，而有效期是平台侧配置 —— 两边一旦不同步，就会出现
 *  「界面说码已失效、其实还能付」的错位。凡是时长都从 `valid_seconds` 推。
 *  不做自动重新出码：后端复用窗口内重新出码只会拿到同一张，无意义。 */
function staleHintMs(validSec: number): number {
  if (!Number.isFinite(validSec) || validSec <= 0) return Infinity;
  return Math.max(60_000, validSec * 500);
}

/** 秒 → 「N 分钟」。后端下发的有效期按分钟呈现（不足 1 分钟按 1 分钟说）。 */
function fmtValidMinutes(sec: number): string {
  return `${Math.max(1, Math.round(sec / 60))} 分钟`;
}

function fmtYuan(fen: number): string {
  return `¥${(fen / 100).toFixed(2).replace(/\.00$/, "")}`;
}

/** 看起来像可以直接打开的链接（http/https/自定义 scheme）。 */
function looksLikeLink(s: string): boolean {
  return /^(https?:\/\/|weixin:\/\/|alipays?:\/\/|[\w.-]+:\/\/)/i.test(s.trim());
}

export function PayDialog({
  order,
  charge,
  showToast,
  onPaid,
  onClose,
}: {
  order: PointsOrder;
  charge: PayCharge;
  showToast: (msg: string) => void;
  /** 轮询确认入账后回调（父组件用它刷新余额与订单列表）；只触发一次 */
  onPaid: () => void;
  onClose: () => void;
}) {
  const [paid, setPaid] = useState(false);
  const [checking, setChecking] = useState(false);
  const [stale, setStale] = useState(false);
  const startedAt = useRef(Date.now());
  const paidRef = useRef(false);        // 防止轮询与手动查询同时命中 → 重复回调

  // 支付码有效期（秒）：由后端下发。0 = 后端没给（老版本），此时**不承诺任何时长**，
  // 而不是编一个数字 —— 界面上的每个时长都必须有出处。
  const validSec = charge.valid_seconds ?? 0;
  const staleHint = staleHintMs(validSec);

  // 把回调塞进 ref：父组件每次重渲染都会产生新的函数身份，若直接进 useEffect
  // 依赖，轮询计时器会被反复重建（用户就看到「一直在等」却不查）。
  const onPaidRef = useRef(onPaid);
  onPaidRef.current = onPaid;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const markPaid = useCallback(() => {
    if (paidRef.current) return;
    paidRef.current = true;
    setPaid(true);
    onPaidRef.current();
  }, []);

  // ── 轮询 ────────────────────────────────────────────────
  useEffect(() => {
    if (paid) return;
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;

    const tick = async () => {
      if (cancelled || paidRef.current) return;
      try {
        const { order: fresh } = await memberApi.order(order.order_id);
        if (fresh.status === "paid") { markPaid(); return; }
      } catch {
        // 单次查询失败（网络抖动 / 后端重启）不打断轮询，下一拍继续 ——
        // 轮询是尽力而为的，失败了最多晚几秒看到结果。
      }
      const elapsed = Date.now() - startedAt.current;
      if (elapsed >= staleHint) setStale(true);
      if (cancelled) return;
      timer = setTimeout(tick, elapsed >= POLL_SWITCH_MS ? POLL_SLOW_MS : POLL_FAST_MS);
    };

    timer = setTimeout(tick, POLL_FAST_MS);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [order.order_id, paid, markPaid, staleHint]);

  // ── 已支付：短暂展示成功态后自动关闭 ──────────────────────
  useEffect(() => {
    if (!paid) return;
    const t = setTimeout(() => onCloseRef.current(), 1600);
    return () => clearTimeout(t);
  }, [paid]);

  // Esc 关闭（支付中允许关闭：订单还在，重开弹窗能继续）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCloseRef.current(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const manualCheck = async () => {
    setChecking(true);
    try {
      const { order: fresh } = await memberApi.order(order.order_id);
      if (fresh.status === "paid") markPaid();
      else showToast("暂未收到付款，请确认已完成支付");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "查询失败");
    } finally {
      setChecking(false);
    }
  };

  const copyInfo = async () => {
    try {
      await navigator.clipboard.writeText(charge.pay_info);
      showToast("支付链接已复制");
    } catch {
      showToast("复制失败，请手动选择复制");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={() => { if (!paid) onClose(); }} />
      <div className="relative w-full max-w-sm rounded-2xl bg-white shadow-xl p-5">
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭"
          className="absolute top-3 right-3 p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
        >
          <X className="w-4 h-4" />
        </button>

        <div className="text-center">
          <p className="text-xs text-gray-500">{order.pack_name}</p>
          <p className="mt-1 text-2xl font-bold text-amber-600 tabular-nums">
            {fmtYuan(order.amount_fen)}
            <span className="ml-1.5 text-xs font-normal text-gray-400">{order.points} 积分</span>
          </p>
        </div>

        <div className="mt-4">
          {paid ? (
            <div className="flex flex-col items-center gap-2 py-8">
              <CheckCircle2 className="w-10 h-10 text-green-500" />
              <p className="text-sm font-medium text-gray-800">支付成功，积分已到账</p>
              <p className="text-xs text-gray-400">+{order.points} 积分</p>
            </div>
          ) : (
            <PayBody
              charge={charge}
              stale={stale}
              onCopy={copyInfo}
            />
          )}
        </div>

        {!paid && (
          <div className="mt-4 space-y-2">
            <div className="flex items-center justify-center gap-1.5 text-[0.6875rem] text-gray-400">
              <Loader2 className="w-3 h-3 animate-spin" />
              等待付款结果…（付款后自动到账，无需手动刷新）
            </div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" className="flex-1"
                icon={checking ? Loader2 : RefreshCw}
                disabled={checking} onClick={() => void manualCheck()}>
                我已完成支付
              </Button>
              <Button variant="ghost" size="sm" onClick={onClose}>稍后支付</Button>
            </div>
            <p className="text-[0.625rem] text-gray-400 text-center leading-4">
              订单号 <span className="tabular-nums">{order.order_id}</span>
              {charge.reused && validSec > 0 &&
                `（沿用本次会话已生成的支付码，${fmtValidMinutes(validSec)}内有效）`}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/** 按 `pay_type` 渲染支付内容。抽出来只为了让上面的主结构一眼看得完。 */
function PayBody({
  charge,
  stale,
  onCopy,
}: {
  charge: PayCharge;
  stale: boolean;
  onCopy: () => void;
}) {
  const hint = (
    <p className={cn("mt-2 text-[0.6875rem] text-center", stale ? "text-amber-600" : "text-gray-400")}>
      {stale
        ? "等待时间较长，若支付码已失效请关闭后重试"
        : (charge.valid_seconds ?? 0) > 0
          ? `请在 ${fmtValidMinutes(charge.valid_seconds as number)}内完成支付`
          : "请尽快完成支付"}
    </p>
  );

  if (charge.pay_type === "qrcode") {
    return (
      <div className="flex flex-col items-center">
        <div className="p-2.5 bg-white rounded-xl border border-gray-200">
          <QRCodeSVG value={charge.pay_info} size={168} level="M" marginSize={0} />
        </div>
        <p className="mt-2.5 text-xs text-gray-600 flex items-center gap-1">
          <Smartphone className="w-3.5 h-3.5" />请用微信「扫一扫」完成支付
        </p>
        {looksLikeLink(charge.pay_info) && (
          <button type="button" onClick={onCopy}
            className="mt-1.5 text-[0.6875rem] text-gray-400 hover:text-indigo-600 transition-colors flex items-center gap-1">
            <Copy className="w-3 h-3" />复制支付链接
          </button>
        )}
        {hint}
      </div>
    );
  }

  if (charge.pay_type === "image") {
    return (
      <div className="flex flex-col items-center">
        <div className="p-2.5 bg-white rounded-xl border border-gray-200">
          <img src={charge.pay_info} alt="收款码"
            className="w-[168px] h-[168px] object-contain" />
        </div>
        <p className="mt-2.5 text-xs text-gray-600 flex items-center gap-1">
          <Smartphone className="w-3.5 h-3.5" />请用微信「扫一扫」完成支付
        </p>
        {hint}
      </div>
    );
  }

  if (charge.pay_type === "urlscheme") {
    return (
      <div className="flex flex-col items-center py-2">
        <Smartphone className="w-8 h-8 text-indigo-500" />
        <p className="mt-2 text-xs text-gray-600 text-center">在手机上打开小程序完成支付</p>
        <Button size="sm" className="mt-3 w-full" icon={ExternalLink}
          onClick={() => { window.location.href = charge.pay_info; }}>
          打开小程序支付
        </Button>
        {hint}
      </div>
    );
  }

  if (charge.pay_type === "jump") {
    return (
      <div className="flex flex-col items-center py-2">
        <p className="text-xs text-gray-600 text-center">点击下方按钮前往收银台完成支付</p>
        <Button size="sm" className="mt-3 w-full" icon={ExternalLink}
          onClick={() => window.open(charge.pay_info, "_blank", "noopener,noreferrer")}>
          打开支付页面
        </Button>
        <button type="button" onClick={onCopy}
          className="mt-2 text-[0.6875rem] text-gray-400 hover:text-indigo-600 transition-colors flex items-center gap-1">
          <Copy className="w-3 h-3" />复制支付链接
        </button>
        {hint}
      </div>
    );
  }

  // 正常不会走到这里（后端在无可支付内容时会直接报错）；留着避免空白弹窗。
  return (
    <div className="flex flex-col items-center py-2 text-center">
      <p className="text-xs text-gray-500">支付通道返回了未知的支付形态（{charge.pay_type || "空"}）</p>
      <p className="mt-2 text-[0.6875rem] text-gray-400 break-all max-w-full">{charge.pay_info}</p>
      {charge.pay_info && (
        <Button size="sm" variant="outline" className="mt-3" icon={Copy} onClick={onCopy}>
          复制内容
        </Button>
      )}
    </div>
  );
}
