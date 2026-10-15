/** 全部订单页（/account/orders）：完整订单列表 + 分页 + 待支付单可直接付款。
 *
 *  为什么单开一页：个人中心是纵向长页，那里的「最近订单」只放 5 条，全铺会把页面撑爆；
 *  而用户需要能看到全部历史订单。列表能力后端本来就有（`GET /api/points/orders`
 *  支持 offset/limit），这里只是给它一个落点。
 *
 *  付款复用 `PayDialog`（与个人中心同一条链路）：所以这一页不只是"看"—— 在这里看到
 *  待支付订单可以直接付掉，不必退回个人中心。
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, Loader2, QrCode, Receipt } from "lucide-react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "@/components/ui";
import { Header } from "@/components/Header";
import { PayDialog } from "@/components/PayDialog";
import { AlertDialog } from "@/components/AlertDialog";
import { ToastNode, useToast } from "@/hooks/useAppInit";
import { navigate, refreshUser } from "@/lib/auth";
import {
  memberApi, type PayCharge, type PointsOrder,
} from "@/api/members";
import { sameAmountAlert, type PayAlert } from "@/lib/payAlert";
import { fmtTime, fmtYuan } from "@/lib/format";

/** 每页条数。与个人中心的 5 条不是一个量级 —— 这里就是给人翻的。 */
const PAGE_SIZE = 20;

/** 过期单单独一色，别和「待支付」都做成灰的 —— 用户一眼要能看出"这张还能付"。 */
function StatusBadge({ status }: { status: PointsOrder["status"] }) {
  return (
    <Badge color={status === "paid" ? "green" : status === "expired" ? "gray" : "indigo"}>
      {status === "paid" ? "已支付" : status === "expired" ? "已过期" : "待支付"}
    </Badge>
  );
}

export default function OrdersPage() {
  const { toast, showToast } = useToast();
  const [orders, setOrders] = useState<PointsOrder[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [pay, setPay] = useState<{ order: PointsOrder; charge: PayCharge } | null>(null);
  const [busyAlert, setBusyAlert] = useState<PayAlert | null>(null);

  const load = useCallback(async (offset: number) => {
    setLoading(true);
    try {
      const body = await memberApi.orders(offset, PAGE_SIZE);
      setOrders(prev => (offset === 0 ? body.orders : [...prev, ...body.orders]));
      setTotal(body.total);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "订单加载失败");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => { void load(0); }, [load]);

  const openPay = async (order: PointsOrder) => {
    setBusyId(order.order_id);
    try {
      const res = await memberApi.payOrder(order.order_id);
      setPay({ order: res.order, charge: res.pay });
    } catch (e) {
      // 订单已支付/已过期，或同面额被别人占了 → 回到第 1 页让状态与顺序回正
      await load(0);
      const busy = sameAmountAlert(e, "pay");
      if (busy) { setBusyAlert(busy); return; }
      showToast(e instanceof Error ? e.message : "发起支付失败");
    } finally {
      setBusyId("");
    }
  };

  /** 轮询确认入账后由弹窗回调：刷新余额与订单列表。 */
  const onPaid = useCallback(async (points: number) => {
    await refreshUser();
    await load(0);
    showToast(`支付成功，+${points} 积分`);
  }, [load, showToast]);

  return (
    <div className="min-h-screen bg-gray-50">
      <Header
        name=""
        onRename={() => {}}
        showNameInput={false}
        showTts={false}
        showProjectActions={false}
        ttsOnline={null}
        ttsInfo={null}
      />

      <main className="max-w-4xl mx-auto px-4 py-8">
        <button type="button"
          onClick={() => navigate("/account")}
          className="mb-4 flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-800 transition-colors">
          <ArrowLeft className="w-3.5 h-3.5" />返回个人中心
        </button>

        <Card>
          <CardHeader className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-1.5">
              <Receipt className="w-4 h-4 text-indigo-500" />全部订单
            </CardTitle>
            <Badge color="gray">{total} 笔</Badge>
          </CardHeader>
          <CardContent className="p-0">
            {loading && orders.length === 0 ? (
              <div className="flex justify-center py-10">
                <Loader2 className="w-5 h-5 text-gray-300 animate-spin" />
              </div>
            ) : orders.length === 0 ? (
              <p className="text-xs text-gray-400 text-center py-10">还没有订单</p>
            ) : (
              <div className="divide-y divide-gray-50">
                {orders.map(o => (
                  <div key={o.order_id} className="flex items-start gap-3 px-4 py-3">
                    <StatusBadge status={o.status} />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs text-gray-700">
                        {o.pack_name} · {o.points} 积分 · {fmtYuan(o.amount_fen)}
                      </p>
                      <p className="text-[0.625rem] text-gray-400 mt-0.5">
                        下单 {fmtTime(o.created_at)}
                        {o.paid_at && ` · 支付 ${fmtTime(o.paid_at)}`}
                        {o.status === "expired" && " · 需重新下单"}
                      </p>
                      {/* 订单号/交易号对账要用：等宽字体 + 允许换行，长号不撑破卡片 */}
                      <p className="text-[0.625rem] text-gray-400 font-mono mt-0.5 break-all">
                        {o.order_id}
                        {o.trade_no && ` · 交易号 ${o.trade_no}`}
                        {o.provider && ` · ${o.provider}`}
                      </p>
                    </div>
                    {o.status === "pending" && (
                      <Button size="sm" variant="outline"
                        icon={busyId === o.order_id ? Loader2 : QrCode}
                        disabled={!!busyId} onClick={() => void openPay(o)}>
                        去支付
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
            {orders.length < total && (
              <div className="p-3 border-t border-gray-50 text-center">
                <Button size="sm" variant="ghost" disabled={loading}
                  onClick={() => void load(orders.length)}>
                  {loading ? "加载中…" : `加载更多（还有 ${total - orders.length} 笔）`}
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      </main>

      <ToastNode toast={toast} />

      {pay && (
        <PayDialog
          key={pay.order.order_id}
          order={pay.order}
          charge={pay.charge}
          showToast={showToast}
          onPaid={() => void onPaid(pay.order.points)}
          onClose={() => setPay(null)}
        />
      )}

      <AlertDialog
        open={!!busyAlert}
        title={busyAlert?.title ?? ""}
        description={busyAlert?.description ?? ""}
        hint={busyAlert?.hint}
        onClose={() => setBusyAlert(null)}
      />
    </div>
  );
}
