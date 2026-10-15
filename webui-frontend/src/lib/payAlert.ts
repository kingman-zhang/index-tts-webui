/** 「同一面额被占用」告知弹窗的内容组装。
 *
 *  ⚠️ 为什么单独一个模块：这个弹窗在**两个页面**各有一个（个人中心「购买」、
 *  全部订单页「去支付」），而它该说什么**同时取决于两件事**：
 *    ① 用户刚才在干什么（下单 / 发起支付）→ 决定标题；
 *    ② 占着这个面额的是**谁的单**（后端下发 `self` / `other`）→ 决定那句指引给不给。
 *  把这两层判断散在两个页面里迟早会漂 —— 尤其「别人的单」这种情形，漏掉一处就是在
 *  给用户指一条根本不存在的路（用户原话："我明明没下过单，为什么要让我找最近订单"）。
 *
 *  正文（`description`）的单一真源在**后端**：只有它知道占位的是哪一笔，也只有它
 *  能负责"该不该说成你自己的"。这里只负责标题和"那你现在能做什么"。
 */
import { sameAmountBusyKind } from "@/api/members";

export interface PayAlert {
  title: string;
  description: string;
  /** 缺省 = 弹窗里不显示这一行（见下方 `order.other`） */
  hint?: string;
}

/** 用户上一个动作：`order` = 点了「购买」要下单；`pay` = 点了某张待付单的「去支付」。 */
export type PayAlertAction = "order" | "pay";

const TITLES: Record<PayAlertAction, string> = {
  order: "暂时无法下单",
  pay: "暂时无法发起支付",
};

/** 指引语的**唯一表格**。
 *  `self`  = 占着面额的是用户自己那笔单 ⇒ 他手里有单可付，可以指引他去找；
 *  `other` = 是别人的单占着 ⇒ 他手里什么都没有。
 *  ⚠️ `order.other` **故意留空**：这时候让他"去最近订单里找那笔待付单"＝指一条不存在的路。
 */
const HINTS: Record<PayAlertAction, { self: string; other?: string }> = {
  order: {
    self: "你可以先在下方「最近订单」里找到那笔订单，直接点「去支付」。",
  },
  pay: {
    self: "你自己还有一笔同面额的订单未支付，先把它付掉。",
    other: "那笔占着同一面额的订单支付成功或过期后即可再试。",
  },
};

/** 这个错误是「同面额被占」就返回弹窗内容；不是就返回 null（调用方走 toast）。 */
export function sameAmountAlert(e: unknown, action: PayAlertAction): PayAlert | null {
  const kind = sameAmountBusyKind(e);
  if (!kind) return null;
  return {
    title: TITLES[action],
    description: e instanceof Error ? e.message : "",
    hint: HINTS[action][kind],
  };
}
